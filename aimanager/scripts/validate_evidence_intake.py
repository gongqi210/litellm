from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from aimanager.redaction import contains_secret_like, sanitize_text, sanitize_value

EvidenceStatus = Literal["PASS", "FAIL", "BLOCKED"]

_EXIT_CODES = {"PASS": 0, "FAIL": 1, "BLOCKED": 2}
_DEFAULT_INPUT_DIR = Path("/tmp/aimanager-evidence-template-pack")
_TEMPLATE_MARKERS = (
    "TEMPLATE_DO_NOT_SUBMIT",
    "replace-with-",
    "replace with real",
    '"status": "TEMPLATE"',
    "'status': 'TEMPLATE'",
)
_FORBIDDEN_EXACT_KEYS = {
    "authorization",
    "cookie",
    "headers",
    "message",
    "messages",
    "prompt",
    "prompttext",
    "rawrequest",
    "rawresponse",
    "rawprompt",
    "requestbody",
    "response",
    "responsebody",
    "assistantresponse",
    "customercontent",
}


@dataclass(frozen=True)
class FileFinding:
    status: EvidenceStatus
    reason: str
    detail: str


@dataclass(frozen=True)
class FileCheck:
    path: str
    status: EvidenceStatus
    detail: str
    findings: list[FileFinding] = field(default_factory=list)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate filled AiManager external evidence files before production readiness intake."
    )
    parser.add_argument("--input-dir", type=Path, default=_DEFAULT_INPUT_DIR)
    parser.add_argument("--output-json-file", type=Path)
    parser.add_argument("--output-markdown-file", type=Path)
    parser.add_argument("--generated-at")
    args = parser.parse_args(argv)

    result = validate_evidence_intake(input_dir=args.input_dir, generated_at=args.generated_at)
    if args.output_json_file:
        _write_json(args.output_json_file, result)
    if args.output_markdown_file:
        _write_text(args.output_markdown_file, str(result["markdown"]))
    status = str(result["status"])
    summary = _mapping(result["summary"])
    print(
        f"{status} evidence intake: files={summary.get('files', 0)} "
        f"fail={summary.get('FAIL', 0)} blocked={summary.get('BLOCKED', 0)}"
    )
    return _EXIT_CODES.get(status, 1)


def validate_evidence_intake(
    *,
    input_dir: Path | None = None,
    input_files: Sequence[Path] | None = None,
    generated_at: str | None = None,
) -> dict[str, object]:
    checks = _collect_file_checks(input_files) if input_files is not None else _collect_checks(input_dir or _DEFAULT_INPUT_DIR)
    result = {
        "status": _overall_status(check.status for check in checks),
        "generated_at": generated_at or _now_iso(),
        "summary": _summary(checks),
        "checks": [asdict(check) for check in checks],
    }
    if input_files is not None:
        result["input_files"] = [str(path) for path in input_files]
    else:
        result["input_dir"] = str(input_dir or _DEFAULT_INPUT_DIR)
    result["markdown"] = _markdown(result)
    return sanitize_value(result)


def _collect_checks(input_dir: Path) -> list[FileCheck]:
    if not input_dir.exists():
        return [
            FileCheck(
                path=str(input_dir),
                status="BLOCKED",
                detail="evidence input directory does not exist",
            )
        ]
    if not input_dir.is_dir():
        return [
            FileCheck(
                path=str(input_dir),
                status="FAIL",
                detail="evidence input path is not a directory",
            )
        ]
    files = sorted(path for path in input_dir.rglob("*") if path.is_file())
    if not files:
        return [
            FileCheck(
                path=str(input_dir),
                status="BLOCKED",
                detail="evidence input directory contains no files",
            )
        ]
    return [_validate_file(path, root=input_dir) for path in files]


def _collect_file_checks(input_files: Sequence[Path]) -> list[FileCheck]:
    files = tuple(dict.fromkeys(Path(path) for path in input_files))
    checks: list[FileCheck] = []
    for path in sorted(files, key=str):
        if not path.exists():
            checks.append(
                FileCheck(
                    path=str(path),
                    status="BLOCKED",
                    detail="evidence file does not exist",
                )
            )
            continue
        if not path.is_file():
            checks.append(
                FileCheck(
                    path=str(path),
                    status="FAIL",
                    detail="evidence path is not a file",
                )
            )
            continue
        checks.append(_validate_file(path, root=path.parent))
    return checks


def _validate_file(path: Path, *, root: Path) -> FileCheck:
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return _file_check(path, root=root, status="FAIL", findings=[_finding("FAIL", "unreadable_file", "file is not UTF-8 text")])
    except Exception as exc:
        return _file_check(
            path,
            root=root,
            status="FAIL",
            findings=[_finding("FAIL", "unreadable_file", f"file could not be read: {type(exc).__name__}")],
        )

    findings: list[FileFinding] = []
    findings.extend(_template_findings(content))
    if contains_secret_like(content):
        findings.append(_finding("FAIL", "secret_like_value", _secret_detail(content)))
    suffix = path.suffix.lower()
    if suffix == ".json":
        findings.extend(_json_findings(path, content))
    elif suffix == ".csv":
        findings.extend(_csv_findings(path))

    status = _overall_status(finding.status for finding in findings)
    return _file_check(path, root=root, status=status, findings=findings)


def _json_findings(path: Path, content: str) -> list[FileFinding]:
    try:
        payload = json.loads(content)
    except Exception as exc:
        return [_finding("FAIL", "invalid_json", f"JSON could not be parsed: {type(exc).__name__}")]
    findings: list[FileFinding] = []
    findings.extend(_forbidden_key_findings(payload))
    if isinstance(payload, Mapping):
        if str(payload.get("template_marker") or ""):
            findings.append(_finding("BLOCKED", "template_marker", "JSON still contains template_marker"))
        if str(payload.get("status") or "").strip().upper() == "TEMPLATE":
            findings.append(_finding("BLOCKED", "template_status", "JSON status is TEMPLATE"))
    return findings


def _csv_findings(path: Path) -> list[FileFinding]:
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            headers = [header or "" for header in (reader.fieldnames or [])]
            rows = list(reader)
    except Exception as exc:
        return [_finding("FAIL", "invalid_csv", f"CSV could not be parsed: {type(exc).__name__}")]

    findings: list[FileFinding] = []
    if not headers:
        findings.append(_finding("BLOCKED", "empty_csv", "CSV has no header row"))
        return findings
    for header in headers:
        if _is_forbidden_key(header):
            findings.append(_finding("FAIL", "forbidden_field", f"CSV header `{header}` is not allowed in evidence intake"))
    data_rows = [row for row in rows if any(str(value or "").strip() for value in row.values())]
    if not data_rows:
        findings.append(_finding("BLOCKED", "empty_csv", "CSV has no data rows; template header-only files are not evidence"))
    return findings


def _template_findings(content: str) -> list[FileFinding]:
    findings: list[FileFinding] = []
    lowered = content.lower()
    for marker in _TEMPLATE_MARKERS:
        needle = marker.lower()
        if needle in lowered:
            if marker == "replace-with-":
                matches = sorted(set(re.findall(r"replace-with-[A-Za-z0-9_-]+", content)))
                for placeholder in matches:
                    findings.append(
                        _finding("BLOCKED", "template_placeholder", f"file still contains `{placeholder}`")
                    )
                continue
            detail = marker
            findings.append(_finding("BLOCKED", "template_placeholder", f"file still contains `{detail}`"))
    return findings


def _forbidden_key_findings(value: Any, *, path: str = "$") -> list[FileFinding]:
    findings: list[FileFinding] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}"
            if _is_forbidden_key(key_text):
                findings.append(
                    _finding(
                        "FAIL",
                        "forbidden_field",
                        f"field `{child_path}` is not allowed; evidence must not contain raw prompts, responses, headers, cookies, or keys",
                    )
                )
            findings.extend(_forbidden_key_findings(item, path=child_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            findings.extend(_forbidden_key_findings(item, path=f"{path}[{index}]"))
    return findings


def _is_forbidden_key(key: str) -> bool:
    normalized = "".join(ch for ch in key.lower() if ch.isalnum())
    if normalized in _FORBIDDEN_EXACT_KEYS:
        return True
    if normalized.startswith("raw") and any(fragment in normalized for fragment in ("prompt", "request", "response")):
        return True
    return normalized.endswith("body") and any(fragment in normalized for fragment in ("request", "response"))


def _file_check(path: Path, *, root: Path, status: EvidenceStatus, findings: list[FileFinding]) -> FileCheck:
    detail = "evidence file is intake-safe" if status == "PASS" else "; ".join(finding.detail for finding in findings)
    return FileCheck(
        path=str(path.relative_to(root) if path.is_relative_to(root) else path),
        status=status,
        detail=sanitize_text(detail),
        findings=findings,
    )


def _finding(status: EvidenceStatus, reason: str, detail: str) -> FileFinding:
    return FileFinding(status=status, reason=reason, detail=sanitize_text(detail))


def _secret_detail(content: str) -> str:
    for line in content.splitlines():
        sanitized = sanitize_text(line)
        if sanitized != line or contains_secret_like(line):
            return f"secret-like value detected: {sanitized.strip()}"
    return "secret-like value detected"


def _overall_status(statuses: Sequence[str] | object) -> EvidenceStatus:
    status_list = [str(status) for status in statuses]
    if any(status == "FAIL" for status in status_list):
        return "FAIL"
    if any(status == "BLOCKED" for status in status_list):
        return "BLOCKED"
    return "PASS"


def _summary(checks: Sequence[FileCheck]) -> dict[str, int]:
    summary = {"PASS": 0, "FAIL": 0, "BLOCKED": 0, "files": len(checks)}
    for check in checks:
        summary[check.status] += 1
    return summary


def _markdown(result: Mapping[str, object]) -> str:
    summary = _mapping(result.get("summary"))
    if "input_files" in result:
        input_line = f"- Input Files: {len(result.get('input_files') or [])}"
    else:
        input_line = f"- Input Directory: `{result.get('input_dir')}`"
    lines = [
        "# AiManager Evidence Intake Validation",
        "",
        f"- Status: {result.get('status')}",
        f"- Generated At: {result.get('generated_at')}",
        input_line,
        f"- Files: {summary.get('files', 0)}",
        f"- PASS/FAIL/BLOCKED: {summary.get('PASS', 0)}/{summary.get('FAIL', 0)}/{summary.get('BLOCKED', 0)}",
        "",
        "| Status | Path | Detail |",
        "| --- | --- | --- |",
    ]
    for check in _mapping_list(result.get("checks")):
        lines.append(f"| {check.get('status')} | `{check.get('path')}` | {check.get('detail')} |")
    return sanitize_text("\n".join(lines) + "\n")


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sanitize_value(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sanitize_text(content), encoding="utf-8")


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _mapping_list(value: object) -> list[Mapping[str, object]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())

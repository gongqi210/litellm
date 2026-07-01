from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping, Sequence

from aimanager.redaction import sanitize_text as sanitize_secret_text
from aimanager.redaction import sanitize_value as sanitize_secret_value

_EXIT_CODES = {"PASS": 0, "FAIL": 1, "BLOCKED": 2}
_STATUS_ORDER = {"FAIL": 0, "BLOCKED": 1, "PASS": 2}
_DEFAULT_LAUNCH_GAP_PLAN_FILE = Path("/tmp/aimanager-acceptance-gate/launch-gap-plan.json")
_DEFAULT_OUTPUT_DIR = Path("/tmp/aimanager-evidence-handoff")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate owner-specific AiManager evidence handoff files.")
    parser.add_argument("--launch-gap-plan-file", type=Path, default=_DEFAULT_LAUNCH_GAP_PLAN_FILE)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    parser.add_argument("--generated-at")
    args = parser.parse_args(argv)

    try:
        result = collect_evidence_handoff(
            launch_gap_plan_file=args.launch_gap_plan_file,
            output_dir=args.output_dir,
            generated_at=args.generated_at,
        )
        args.output_dir.mkdir(parents=True, exist_ok=True)
        _write_json(args.output_dir / "evidence-handoff.json", result)
        _write_text(args.output_dir / "evidence-handoff.md", str(result["markdown"]))
        for owner in _owner_groups(result):
            markdown_file = Path(str(owner["markdown_file"]))
            _write_text(markdown_file, str(owner["markdown"]))
        status = str(result["status"])
        print(f"{status} evidence handoff: owners={result['summary']['owners']} gaps={result['summary']['gaps']} output={args.output_dir}")
        return _EXIT_CODES.get(status, 1)
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        print(f"FAIL evidence handoff: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def collect_evidence_handoff(
    *,
    launch_gap_plan_file: Path,
    output_dir: Path | None = None,
    generated_at: str | None = None,
) -> dict[str, object]:
    generated = generated_at or _now_iso()
    launch = _load_launch_gap_plan(launch_gap_plan_file)
    gaps = _gap_list(launch, launch_gap_plan_file)
    groups = _group_by_owner(gaps, output_dir)
    summary = _summary(groups)
    result = {
        "status": _overall_status([str(group["status"]) for group in groups]),
        "generated_at": generated,
        "source": str(launch_gap_plan_file),
        "summary": summary,
        "owners": groups,
    }
    result["markdown"] = _render_manifest_markdown(result)
    return sanitize_secret_value(result)


def _load_launch_gap_plan(path: Path) -> Mapping[str, object]:
    if not path.exists():
        return {
            "status": "BLOCKED",
            "gaps": [
                {
                    "id": "INPUT:launch_gap_plan",
                    "name": "missing_launch_gap_plan",
                    "status": "BLOCKED",
                    "owner": "project_owner",
                    "detail": f"{path} does not exist",
                    "required_files": [str(path)],
                    "required_env": [],
                    "command": "make acceptance-gate",
                    "next_action": "Run the acceptance gate first, then regenerate the evidence handoff.",
                    "sources": ["evidence_handoff"],
                }
            ],
        }
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {
            "status": "FAIL",
            "gaps": [
                {
                    "id": "INPUT:launch_gap_plan",
                    "name": "bad_launch_gap_plan_json",
                    "status": "FAIL",
                    "owner": "project_owner",
                    "detail": f"{path} could not be loaded: {exc}",
                    "required_files": [str(path)],
                    "required_env": [],
                    "command": "make acceptance-gate",
                    "next_action": "Regenerate a valid launch gap plan JSON.",
                    "sources": ["evidence_handoff"],
                }
            ],
        }
    if isinstance(loaded, Mapping):
        return loaded
    return {
        "status": "FAIL",
        "gaps": [
            _input_gap(
                name="invalid_launch_gap_plan",
                status="FAIL",
                detail=f"{path} must contain a JSON object, got {type(loaded).__name__}",
                required_files=[str(path)],
                next_action="Regenerate a valid launch gap plan JSON.",
            )
        ],
    }


def _gap_list(launch: Mapping[str, object], source_file: Path) -> list[dict[str, object]]:
    raw_gaps = launch.get("gaps")
    if not isinstance(raw_gaps, list):
        return [
            {
                "id": "INPUT:launch_gap_plan",
                "name": "missing_launch_gap_plan_gaps",
                "status": "FAIL",
                "owner": "project_owner",
                "detail": f"{source_file} does not contain a gaps list",
                "required_files": [str(source_file)],
                "required_env": [],
                "command": "make acceptance-gate",
                "next_action": "Regenerate launch gap plan with the current script version.",
                "sources": ["evidence_handoff"],
            }
        ]
    gaps: list[dict[str, object]] = []
    invalid_gap_entries = 0
    for item in raw_gaps:
        if not isinstance(item, Mapping):
            invalid_gap_entries += 1
            continue
        status = _coerce_status(item.get("status"))
        if status == "PASS":
            continue
        command = _rerun_command(item)
        gaps.append(
            {
                "id": str(item.get("id") or "UNKNOWN"),
                "name": str(item.get("name") or "unknown_gap"),
                "status": status,
                "owner": str(item.get("owner") or "project_owner"),
                "detail": str(item.get("detail") or ""),
                "required_env": _string_list(item.get("required_env")),
                "required_files": _string_list(item.get("required_files")),
                "command": command,
                "rerun_command": command,
                "next_action": str(item.get("next_action") or ""),
                "sources": _string_list(item.get("sources")),
            }
        )
    if invalid_gap_entries:
        gaps.append(
            _input_gap(
                name="invalid_launch_gap_plan_gaps",
                status="FAIL",
                detail=f"{source_file} contains non-object gap entries",
                required_files=[str(source_file)],
                next_action="Regenerate launch gap plan with object entries in the gaps list.",
            )
        )
    if gaps:
        return gaps
    launch_status = _coerce_status(launch.get("status"))
    if launch_status != "PASS":
        return [
            _input_gap(
                name="empty_launch_gap_plan_gaps",
                status=launch_status,
                detail=f"{source_file} has top-level status {launch_status} but no non-PASS gap entries",
                required_files=[str(source_file)],
                next_action="Regenerate launch gap plan so every non-PASS status has an actionable gap.",
            )
        ]
    return []


def _input_gap(
    *,
    name: str,
    status: str,
    detail: str,
    required_files: Sequence[str],
    next_action: str,
) -> dict[str, object]:
    return {
        "id": "INPUT:launch_gap_plan",
        "name": name,
        "status": status,
        "owner": "project_owner",
        "detail": detail,
        "required_files": list(required_files),
        "required_env": [],
        "command": "make acceptance-gate",
        "next_action": next_action,
        "sources": ["evidence_handoff"],
    }


def _group_by_owner(gaps: Sequence[dict[str, object]], output_dir: Path | None) -> list[dict[str, object]]:
    by_owner: dict[str, list[dict[str, object]]] = {}
    for gap in gaps:
        by_owner.setdefault(str(gap["owner"]), []).append(gap)
    groups: list[dict[str, object]] = []
    for owner in sorted(by_owner):
        owner_gaps = sorted(by_owner[owner], key=lambda gap: (str(gap["status"]), str(gap["id"])))
        group = {
            "owner": owner,
            "status": _overall_status([str(gap["status"]) for gap in owner_gaps]),
            "gaps": owner_gaps,
            "markdown_file": str((output_dir / f"{_slugify(owner)}.md") if output_dir else f"{_slugify(owner)}.md"),
        }
        group["markdown"] = _render_owner_markdown(group)
        groups.append(group)
    return groups


def _summary(groups: Sequence[Mapping[str, object]]) -> dict[str, int]:
    gaps = [gap for group in groups for gap in _mapping_list(group.get("gaps"))]
    return {
        "owners": len(groups),
        "gaps": len(gaps),
        "PASS": 0,
        "FAIL": sum(1 for gap in gaps if gap.get("status") == "FAIL"),
        "BLOCKED": sum(1 for gap in gaps if gap.get("status") == "BLOCKED"),
    }


def _render_manifest_markdown(result: Mapping[str, object]) -> str:
    lines = [
        "# AiManager Evidence Handoff",
        "",
        "Evidence request only; this is not a PASS artifact and does not replace acceptance evidence.",
        "",
        f"- Status: {result.get('status')}",
        f"- Generated At: {result.get('generated_at')}",
        f"- Source: `{result.get('source')}`",
        "",
        "| Owner | Status | Gaps | File |",
        "| --- | --- | ---: | --- |",
    ]
    for owner in _mapping_list(result.get("owners")):
        lines.append(
            f"| {owner.get('owner')} | {owner.get('status')} | {len(_mapping_list(owner.get('gaps')))} | `{owner.get('markdown_file')}` |"
        )
    return sanitize_secret_text("\n".join(lines) + "\n")


def _render_owner_markdown(owner_group: Mapping[str, object]) -> str:
    lines = [
        f"# AiManager Evidence Request: {owner_group.get('owner')}",
        "",
        "Evidence request only; this is not a PASS artifact.",
        "Do not paste secrets, tokens, cookies, raw prompts, raw responses, or customer content into evidence files.",
        "",
        f"- Status: {owner_group.get('status')}",
        "",
    ]
    for gap in _mapping_list(owner_group.get("gaps")):
        lines.extend(
            [
                f"## {gap.get('id')} {gap.get('name')}",
                "",
                f"- Status: {gap.get('status')}",
                f"- Detail: {gap.get('detail')}",
                f"- Next Action: {gap.get('next_action')}",
                f"- Command: `{gap.get('command')}`" if gap.get("command") else "- Command: ",
                f"- Required Env: {_format_list(_string_list(gap.get('required_env')))}",
                f"- Required Files: {_format_list(_string_list(gap.get('required_files')))}",
                f"- Sources: {_format_list(_string_list(gap.get('sources')))}",
                "",
            ]
        )
    return sanitize_secret_text("\n".join(lines))


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _rerun_command(source: Mapping[str, object]) -> str:
    return str(source.get("rerun_command") or source.get("command") or "")


def _overall_status(statuses: Sequence[str]) -> str:
    if not statuses:
        return "PASS"
    status = "PASS"
    for candidate in statuses:
        candidate = _coerce_status(candidate)
        if _STATUS_ORDER[candidate] < _STATUS_ORDER[status]:
            status = candidate
    return status


def _coerce_status(value: object) -> str:
    return value if value in _EXIT_CODES else "FAIL"  # type: ignore[return-value]


def _string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    if isinstance(value, tuple):
        return [str(item) for item in value if str(item)]
    if isinstance(value, str) and value:
        return [value]
    return []


def _mapping_list(value: object) -> list[Mapping[str, object]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _format_list(values: Sequence[str]) -> str:
    return ", ".join(f"`{value}`" for value in values) if values else "_none_"


def _slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()).strip("._-")
    return slug or "project_owner"


def _owner_groups(result: Mapping[str, object]) -> list[Mapping[str, object]]:
    return _mapping_list(result.get("owners"))


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())

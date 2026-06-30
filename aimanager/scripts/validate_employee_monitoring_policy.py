from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from aimanager.employee_monitoring import validate_employee_monitoring_controls

_EXIT_CODES = {
    "PASS": 0,
    "FAIL": 1,
    "BLOCKED": 2,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate AiManager AC-26 employee monitoring notice, anomaly rules, and permission boundaries."
    )
    parser.add_argument("--policy-file", required=True, help="JSON employee monitoring policy.")
    parser.add_argument("--employee-roster-file", required=True, help="CSV or JSON active employee roster.")
    parser.add_argument("--acknowledgment-file", required=True, help="CSV or JSON employee notice acknowledgments.")
    parser.add_argument("--output-json-file", required=True, help="Destination JSON validation result path.")
    parser.add_argument("--output-markdown-file", help="Optional destination Markdown validation report path.")
    args = parser.parse_args(argv)

    output_json = Path(args.output_json_file)
    output_markdown = Path(args.output_markdown_file) if args.output_markdown_file else None
    result = _collect_result(
        policy_file=Path(args.policy_file),
        employee_roster_file=Path(args.employee_roster_file),
        acknowledgment_file=Path(args.acknowledgment_file),
    )
    _write_json(output_json, result)
    if output_markdown is not None:
        _write_text(output_markdown, str(result.get("markdown") or ""))
    status = str(result["status"])
    print(f"{status} employee monitoring policy: {result['detail']}", file=sys.stderr if status == "FAIL" else sys.stdout)
    return _EXIT_CODES.get(status, 1)


def _collect_result(*, policy_file: Path, employee_roster_file: Path, acknowledgment_file: Path) -> dict[str, object]:
    missing = _missing_input(policy_file=policy_file, employee_roster_file=employee_roster_file, acknowledgment_file=acknowledgment_file)
    if missing:
        return _blocked(detail=f"missing {missing} file")
    try:
        return validate_employee_monitoring_controls(
            policy=_load_json_object(policy_file),
            employee_roster=_load_records(employee_roster_file),
            acknowledgments=_load_records(acknowledgment_file),
        )
    except Exception as exc:
        return _failed(detail=f"{type(exc).__name__}: {exc}")


def _missing_input(*, policy_file: Path, employee_roster_file: Path, acknowledgment_file: Path) -> str:
    if not policy_file.exists():
        return "policy"
    if not employee_roster_file.exists():
        return "employee roster"
    if not acknowledgment_file.exists():
        return "acknowledgment"
    return ""


def _blocked(*, detail: str) -> dict[str, object]:
    return _status_result(status="BLOCKED", detail=detail)


def _failed(*, detail: str) -> dict[str, object]:
    return _status_result(status="FAIL", detail=detail)


def _status_result(*, status: str, detail: str) -> dict[str, object]:
    result = {
        "status": status,
        "detail": detail,
        "policy_id": "",
        "policy_version": "",
        "summary": {
            "active_employee_count": 0,
            "acknowledged_employee_count": 0,
            "missing_acknowledgment_count": 0,
            "rule_count": 0,
            "retention_days": 0,
        },
        "controls": {
            "notice_channels": [],
            "monitored_metadata_fields": [],
            "prohibited_monitoring_fields": [],
            "rules": [],
            "permission_boundary": {},
        },
        "errors": [],
        "warnings": [],
        "acknowledgment_gaps": [],
        "markdown": "",
    }
    result["markdown"] = f"# AiManager 员工监控制度验收 - unknown\n\n- 状态：{status}\n- 说明：{detail}\n"
    return result


def _load_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _load_records(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError(f"{path} must contain a JSON array")
        return _json_records(payload, path=path)
    if suffix == ".csv":
        with path.open(encoding="utf-8", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    raise ValueError(f"unsupported file type for {path}; expected .json or .csv")


def _json_records(payload: list[object], *, path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError(f"{path} must contain only JSON objects")
        records.append(item)
    return records


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())

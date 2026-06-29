from __future__ import annotations

import argparse
import csv
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

from aimanager.observability import build_observability_report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export AiManager observability metrics and alerts as JSON."
    )
    parser.add_argument("--audit-log-file", help="Plain-text log file containing aimanager_audit_event entries.")
    parser.add_argument("--request-status-file", help="JSON or CSV file containing request status rows.")
    parser.add_argument("--output-file", required=True, help="Destination JSON report path.")
    parser.add_argument("--failure-rate-alert-threshold", default="0.05")
    args = parser.parse_args(argv)

    try:
        if not args.audit_log_file and not args.request_status_file:
            raise ValueError("at least one of --audit-log-file or --request-status-file is required")
        audit_log_lines = _load_log_lines(Path(args.audit_log_file)) if args.audit_log_file else []
        request_rows = _load_records(Path(args.request_status_file)) if args.request_status_file else []
        report = build_observability_report(
            audit_log_lines=audit_log_lines,
            request_rows=request_rows,
            failure_rate_alert_threshold=Decimal(str(args.failure_rate_alert_threshold)),
        )
        output_file = Path(args.output_file)
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(
            json.dumps(_json_ready(report), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"FAIL observability export: {exc}", file=sys.stderr)
        return 1

    print(f"PASS observability export wrote {output_file}")
    return 0


def _load_log_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _load_records(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return _load_json_records(path)
    if suffix == ".csv":
        return _load_csv_records(path)
    raise ValueError(f"unsupported file type for {path}; expected .json or .csv")


def _load_json_records(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"{path} must contain a JSON array")
    records: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError(f"{path} must contain only JSON objects")
        records.append(item)
    return records


def _load_csv_records(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _json_ready(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    return value


if __name__ == "__main__":
    raise SystemExit(main())

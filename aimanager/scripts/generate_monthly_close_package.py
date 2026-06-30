from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from aimanager.monthly_close import build_monthly_close_package

_EXIT_CODES = {
    "PASS": 0,
    "FAIL": 1,
    "BLOCKED": 2,
}

_ADJUSTED_CSV_FIELDNAMES = [
    "month",
    "department_id",
    "project_id",
    "cost_center_id",
    "user_id",
    "key_alias",
    "model",
    "endpoint",
    "spend",
    "currency",
    "pricing_version",
    "request_count",
    "close_status",
    "source",
    "adjustment_id",
]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate an AiManager monthly close package from finance, reconciliation, and adjustment files."
    )
    parser.add_argument("--finance-monthly-file", required=True, help="aimanager_finance_monthly.csv from export_finance.")
    parser.add_argument("--reconciliation-file", required=True, help="aimanager_reconciliation.csv from export_finance.")
    parser.add_argument("--adjustment-file", required=True, help="CSV or JSON monthly adjustment ledger.")
    parser.add_argument("--month", required=True, help="Month in YYYY-MM format.")
    parser.add_argument("--output-json-file", required=True, help="Destination JSON monthly close package.")
    parser.add_argument("--output-adjusted-csv-file", help="Optional adjusted monthly CSV output path.")
    parser.add_argument("--output-markdown-file", help="Optional Markdown summary output path.")
    args = parser.parse_args(argv)

    output_json = Path(args.output_json_file)
    output_csv = Path(args.output_adjusted_csv_file) if args.output_adjusted_csv_file else None
    output_markdown = Path(args.output_markdown_file) if args.output_markdown_file else None
    result = _collect_result(
        finance_monthly_file=Path(args.finance_monthly_file),
        reconciliation_file=Path(args.reconciliation_file),
        adjustment_file=Path(args.adjustment_file),
        month=args.month,
    )
    _write_json(output_json, result)
    if output_csv is not None:
        _write_csv(output_csv, list(result.get("adjusted_monthly_rows") or []))
    if output_markdown is not None:
        _write_text(output_markdown, str(result.get("markdown") or ""))

    status = str(result["status"])
    print(f"{status} monthly close: {result['detail']}", file=sys.stderr if status == "FAIL" else sys.stdout)
    return _EXIT_CODES.get(status, 1)


def _collect_result(
    *,
    finance_monthly_file: Path,
    reconciliation_file: Path,
    adjustment_file: Path,
    month: str,
) -> dict[str, object]:
    missing = _missing_inputs(
        finance_monthly_file=finance_monthly_file,
        reconciliation_file=reconciliation_file,
        adjustment_file=adjustment_file,
    )
    if missing:
        return _status_result(status="BLOCKED", month=month, detail=f"missing {missing} file")
    try:
        return build_monthly_close_package(
            monthly_rows=_load_records(finance_monthly_file),
            reconciliation_rows=_load_records(reconciliation_file),
            adjustment_rows=_load_records(adjustment_file),
            month=month,
        )
    except Exception as exc:
        return _status_result(status="FAIL", month=month, detail=f"{type(exc).__name__}: {exc}")


def _missing_inputs(*, finance_monthly_file: Path, reconciliation_file: Path, adjustment_file: Path) -> str:
    if not finance_monthly_file.exists():
        return "finance monthly"
    if not reconciliation_file.exists():
        return "reconciliation"
    if not adjustment_file.exists():
        return "adjustment"
    return ""


def _status_result(*, status: str, month: str, detail: str) -> dict[str, object]:
    return {
        "status": status,
        "month": month,
        "detail": detail,
        "close_status": "blocked" if status == "BLOCKED" else "failed",
        "summary": {},
        "operation_counts": {
            "supplemental": 0,
            "reversal": 0,
            "attribution_adjustment": 0,
            "difference_resolution": 0,
        },
        "blocking_items": [],
        "unresolved_reconciliation": [],
        "reconciliation_resolutions": [],
        "adjusted_monthly_rows": [],
        "audit_trail": [],
        "markdown": "",
    }


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


def _write_csv(path: Path, rows: list[object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_ADJUSTED_CSV_FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            if isinstance(row, dict):
                writer.writerow(row)


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())

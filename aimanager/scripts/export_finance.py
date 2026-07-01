from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

from aimanager.finance import build_finance_export_bundle, format_money


_FIELDNAMES: dict[str, list[str]] = {
    "aimanager_usage_daily.csv": [
        "date",
        "department_id",
        "project_id",
        "cost_center_id",
        "user_id",
        "key_alias",
        "model",
        "endpoint",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "image_count",
        "spend",
        "currency",
        "pricing_version",
        "request_count",
        "successful_requests",
        "failed_requests",
    ],
    "aimanager_finance_monthly.csv": [
        "month",
        "department_id",
        "project_id",
        "cost_center_id",
        "user_id",
        "key_alias",
        "model",
        "endpoint",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "image_count",
        "spend",
        "currency",
        "pricing_version",
        "request_count",
        "successful_requests",
        "failed_requests",
        "close_status",
    ],
    "aimanager_reconciliation.csv": [
        "month",
        "model",
        "endpoint",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "image_count",
        "aimanager_amount",
        "ycapi_amount",
        "difference",
        "difference_rate",
        "currency",
        "status",
    ],
}


@dataclass(frozen=True)
class FinanceExportArtifacts:
    output_files: list[str]
    spend_row_count: int
    ycapi_bill_row_count: int
    aimanager_billable_row_count: int
    ycapi_billable_row_count: int
    reconciliation_row_count: int
    reconciliation_needs_review_count: int


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export AiManager finance CSVs from LiteLLM spend rows and ycapi bills."
    )
    parser.add_argument(
        "--spend-file", required=True, help="JSON or CSV file containing LiteLLM SpendLogs-shaped rows."
    )
    parser.add_argument("--ycapi-bill-file", required=True, help="JSON or CSV file containing ycapi monthly bill rows.")
    parser.add_argument("--output-dir", required=True, help="Directory for generated finance CSV files.")
    args = parser.parse_args(argv)

    try:
        artifacts = export_finance_csvs(
            spend_file=Path(args.spend_file),
            ycapi_bill_file=Path(args.ycapi_bill_file),
            output_dir=Path(args.output_dir),
        )
    except Exception as exc:
        print(f"FAIL finance export: {exc}", file=sys.stderr)
        return 1

    print(f"PASS finance export wrote {len(artifacts.output_files)} file(s) to {args.output_dir}")
    return 0


def export_finance_csvs(*, spend_file: Path, ycapi_bill_file: Path, output_dir: Path) -> FinanceExportArtifacts:
    spend_rows = _load_records(spend_file)
    ycapi_bill_rows = _load_records(ycapi_bill_file)
    output_dir.mkdir(parents=True, exist_ok=True)

    bundle = build_finance_export_bundle(spend_rows=spend_rows, ycapi_bill_rows=ycapi_bill_rows)
    reconciliation_rows = bundle["aimanager_reconciliation.csv"]
    for filename, rows in bundle.items():
        _write_csv(output_dir / filename, rows, _FIELDNAMES[filename])
    return FinanceExportArtifacts(
        output_files=sorted(bundle.keys()),
        spend_row_count=len(spend_rows),
        ycapi_bill_row_count=len(ycapi_bill_rows),
        aimanager_billable_row_count=sum(
            1 for row in reconciliation_rows if _is_positive_decimal(row.get("aimanager_amount"))
        ),
        ycapi_billable_row_count=sum(
            1 for row in reconciliation_rows if _is_positive_decimal(row.get("ycapi_amount"))
        ),
        reconciliation_row_count=len(reconciliation_rows),
        reconciliation_needs_review_count=sum(
            1 for row in reconciliation_rows if str(row.get("status") or "").strip() == "needs_review"
        ),
    )


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


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_value(row.get(field)) for field in fieldnames})


def _csv_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format_money(value)
    return value


def _is_positive_decimal(value: Any) -> bool:
    try:
        return Decimal(str(value)) > 0
    except Exception:
        return False


if __name__ == "__main__":
    raise SystemExit(main())

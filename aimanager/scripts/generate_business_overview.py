from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from aimanager.business_overview import build_business_overview

_EXIT_CODES = {
    "PASS": 0,
    "FAIL": 1,
    "BLOCKED": 2,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a CEO-readable AiManager business overview from finance, budget, and observability files."
    )
    parser.add_argument("--finance-monthly-file", required=True, help="aimanager_finance_monthly.csv from export_finance.")
    parser.add_argument("--budget-file", required=True, help="CSV or JSON budget rows for the target month.")
    parser.add_argument("--observability-report-file", required=True, help="observability JSON report from export_observability.")
    parser.add_argument("--month", required=True, help="Month in YYYY-MM format.")
    parser.add_argument("--output-json-file", required=True, help="Destination JSON overview path.")
    parser.add_argument("--output-markdown-file", help="Optional destination Markdown overview path.")
    parser.add_argument("--top-n", type=int, default=5)
    args = parser.parse_args(argv)

    output_json = Path(args.output_json_file)
    output_markdown = Path(args.output_markdown_file) if args.output_markdown_file else None
    result = _collect_result(
        finance_monthly_file=Path(args.finance_monthly_file),
        budget_file=Path(args.budget_file),
        observability_report_file=Path(args.observability_report_file),
        month=args.month,
        top_n=args.top_n,
    )
    _write_json(output_json, result)
    if output_markdown is not None:
        _write_text(output_markdown, str(result.get("markdown") or ""))
    status = str(result["status"])
    print(f"{status} business overview: {result['detail']}", file=sys.stderr if status == "FAIL" else sys.stdout)
    return _EXIT_CODES.get(status, 1)


def _collect_result(
    *,
    finance_monthly_file: Path,
    budget_file: Path,
    observability_report_file: Path,
    month: str,
    top_n: int,
) -> dict[str, object]:
    missing = _missing_inputs(
        finance_monthly_file=finance_monthly_file,
        budget_file=budget_file,
        observability_report_file=observability_report_file,
    )
    if missing:
        return _blocked(month=month, detail=f"missing {missing} file")
    try:
        observability_report = _load_json_object(observability_report_file)
        return build_business_overview(
            monthly_rows=_load_records(finance_monthly_file),
            budget_rows=_load_records(budget_file),
            observability_report=observability_report,
            month=month,
            top_n=top_n,
        )
    except Exception as exc:
        return _failed(month=month, detail=f"{type(exc).__name__}: {exc}")


def _missing_inputs(*, finance_monthly_file: Path, budget_file: Path, observability_report_file: Path) -> str:
    if not finance_monthly_file.exists():
        return "finance monthly"
    if not budget_file.exists():
        return "budget"
    if not observability_report_file.exists():
        return "observability report"
    return ""


def _blocked(*, month: str, detail: str) -> dict[str, object]:
    return _status_result(status="BLOCKED", month=month, detail=detail)


def _failed(*, month: str, detail: str) -> dict[str, object]:
    return _status_result(status="FAIL", month=month, detail=detail)


def _status_result(*, status: str, month: str, detail: str) -> dict[str, object]:
    return {
        "status": status,
        "month": month,
        "detail": detail,
        "summary": {},
        "budget_utilization": [],
        "top_departments": [],
        "top_projects": [],
        "top_keys": [],
        "anomalies": [],
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


def _load_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())

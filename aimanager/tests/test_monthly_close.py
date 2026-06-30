from __future__ import annotations

import csv
import json

from aimanager.monthly_close import build_monthly_close_package
from aimanager.scripts.generate_monthly_close_package import main


def test_monthly_close_applies_adjustments_and_resolves_reconciliation() -> None:
    result = build_monthly_close_package(
        monthly_rows=_monthly_rows(),
        reconciliation_rows=_reconciliation_rows(),
        adjustment_rows=_adjustment_rows(),
        month="2026-06",
    )

    assert result["status"] == "PASS"
    assert result["month"] == "2026-06"
    assert result["close_status"] == "ready"
    assert result["summary"] == {
        "base_spend": "15.5",
        "adjustment_delta": "-1",
        "adjusted_spend": "14.5",
        "currency": "CNY",
        "monthly_row_count": 2,
        "adjustment_count": 4,
    }
    assert result["operation_counts"] == {
        "supplemental": 1,
        "reversal": 1,
        "attribution_adjustment": 1,
        "difference_resolution": 1,
    }
    assert result["blocking_items"] == []
    assert result["unresolved_reconciliation"] == []
    assert result["adjusted_monthly_rows"] == [
        {
            "month": "2026-06",
            "department_id": "dept_market",
            "project_id": "proj_launch",
            "cost_center_id": "cc_growth",
            "user_id": "u_market_1",
            "key_alias": "market-key",
            "model": "deepseek-chat",
            "endpoint": "/v1/chat/completions",
            "spend": "0",
            "currency": "CNY",
            "pricing_version": "m1-2026-06",
            "request_count": 2,
            "close_status": "ready",
            "source": "attribution_adjustment",
            "adjustment_id": "adj-attribution-1",
        },
        {
            "month": "2026-06",
            "department_id": "dept_market",
            "project_id": "proj_launch",
            "cost_center_id": "cc_growth",
            "user_id": "u_market_1",
            "key_alias": "market-key",
            "model": "gemini-2.5-flash",
            "endpoint": "/v1/chat/completions",
            "spend": "14.5",
            "currency": "CNY",
            "pricing_version": "m1-2026-06",
            "request_count": 8,
            "close_status": "ready",
            "source": "monthly_close",
            "adjustment_id": "",
        },
    ]
    assert result["reconciliation_resolutions"] == [
        {
            "reconciliation_key": "2026-06|gemini-2.5-flash|/v1/chat/completions|CNY",
            "status": "resolved",
            "resolution_status": "manual_correction",
            "adjustment_id": "adj-difference-1",
            "difference": "2",
            "approver": "finance-owner",
        }
    ]
    assert result["audit_trail"][0] == {
        "adjustment_id": "adj-supplemental-1",
        "operation_type": "supplemental",
        "effective_date": "2026-06-30",
        "approver": "finance-owner",
        "reason": "Late approved marketing usage invoice",
    }
    assert "月结状态：ready" in result["markdown"]
    assert "补记：1" in result["markdown"]
    assert "差异处理：1" in result["markdown"]


def test_monthly_close_blocks_unresolved_reconciliation() -> None:
    result = build_monthly_close_package(
        monthly_rows=_monthly_rows(),
        reconciliation_rows=_reconciliation_rows(),
        adjustment_rows=[row for row in _adjustment_rows() if row["operation_type"] != "difference_resolution"],
        month="2026-06",
    )

    assert result["status"] == "BLOCKED"
    assert result["close_status"] == "blocked_unresolved_reconciliation"
    assert result["unresolved_reconciliation"] == [
        {
            "reconciliation_key": "2026-06|gemini-2.5-flash|/v1/chat/completions|CNY",
            "model": "gemini-2.5-flash",
            "endpoint": "/v1/chat/completions",
            "currency": "CNY",
            "difference": "2",
            "status": "needs_review",
        }
    ]


def test_monthly_close_reversal_follows_attribution_destination_without_dimension_overrides() -> None:
    adjustments = _adjustment_rows()
    adjustments[1] = {
        key: value
        for key, value in adjustments[1].items()
        if key
        not in {
            "department_id",
            "project_id",
            "cost_center_id",
            "user_id",
            "key_alias",
            "pricing_version",
        }
    }

    result = build_monthly_close_package(
        monthly_rows=_monthly_rows(),
        reconciliation_rows=_reconciliation_rows(),
        adjustment_rows=adjustments,
        month="2026-06",
    )

    assert result["status"] == "PASS"
    assert result["adjusted_monthly_rows"][0]["department_id"] == "dept_market"
    assert result["adjusted_monthly_rows"][0]["key_alias"] == "market-key"
    assert result["adjusted_monthly_rows"][0]["spend"] == "0"


def test_monthly_close_blocks_residual_unassigned_spend() -> None:
    result = build_monthly_close_package(
        monthly_rows=_monthly_rows(),
        reconciliation_rows=[
            row for row in _reconciliation_rows() if row["status"] == "matched"
        ],
        adjustment_rows=[
            row for row in _adjustment_rows() if row["operation_type"] == "supplemental"
        ],
        month="2026-06",
    )

    assert result["status"] == "BLOCKED"
    assert result["close_status"] == "blocked_unassigned"
    assert result["blocking_items"][0]["code"] == "blocked_unassigned"
    assert result["blocking_items"][0]["rows"][0]["department_id"] == "unassigned"
    assert result["blocking_items"][0]["rows"][0]["spend"] == "3"


def test_monthly_close_fails_invalid_reversal_without_target() -> None:
    adjustments = _adjustment_rows()
    adjustments[1] = {key: value for key, value in adjustments[1].items() if key != "reverses_entry_id"}

    result = build_monthly_close_package(
        monthly_rows=_monthly_rows(),
        reconciliation_rows=_reconciliation_rows(),
        adjustment_rows=adjustments,
        month="2026-06",
    )

    assert result["status"] == "FAIL"
    assert "reverses_entry_id" in result["detail"]


def test_monthly_close_fails_duplicate_adjustment_id() -> None:
    adjustments = _adjustment_rows()
    adjustments[1]["adjustment_id"] = "adj-supplemental-1"

    result = build_monthly_close_package(
        monthly_rows=_monthly_rows(),
        reconciliation_rows=_reconciliation_rows(),
        adjustment_rows=adjustments,
        month="2026-06",
    )

    assert result["status"] == "FAIL"
    assert "duplicate adjustment_id" in result["detail"]


def test_monthly_close_fails_unsupported_difference_resolution_status() -> None:
    adjustments = _adjustment_rows()
    adjustments[3]["resolution_status"] = "ignore_it"

    result = build_monthly_close_package(
        monthly_rows=_monthly_rows(),
        reconciliation_rows=_reconciliation_rows(),
        adjustment_rows=adjustments,
        month="2026-06",
    )

    assert result["status"] == "FAIL"
    assert "unsupported resolution_status" in result["detail"]


def test_monthly_close_outputs_do_not_serialize_token_like_extra_fields() -> None:
    monthly_rows = _monthly_rows()
    monthly_rows[0]["api_key"] = "sk-monthly-should-not-leak"
    reconciliation_rows = _reconciliation_rows()
    reconciliation_rows[0]["authorization"] = "Bearer reconciliation-should-not-leak"
    adjustments = _adjustment_rows()
    adjustments[0]["YCAPI_API_TOKEN"] = "sk-should-not-leak"
    adjustments[0]["authorization"] = "Bearer should-not-leak"

    result = build_monthly_close_package(
        monthly_rows=monthly_rows,
        reconciliation_rows=reconciliation_rows,
        adjustment_rows=adjustments,
        month="2026-06",
    )

    encoded = json.dumps(result, ensure_ascii=False)
    assert result["status"] == "PASS"
    assert "sk-monthly-should-not-leak" not in encoded
    assert "Bearer reconciliation-should-not-leak" not in encoded
    assert "sk-should-not-leak" not in encoded
    assert "Bearer should-not-leak" not in encoded
    assert "YCAPI_API_TOKEN" not in encoded


def test_monthly_close_cli_writes_json_csv_and_markdown(tmp_path) -> None:
    finance_file = tmp_path / "aimanager_finance_monthly.csv"
    reconciliation_file = tmp_path / "aimanager_reconciliation.csv"
    adjustments_file = tmp_path / "adjustments.csv"
    output_json = tmp_path / "monthly_close.json"
    output_csv = tmp_path / "monthly_close_adjusted.csv"
    output_markdown = tmp_path / "monthly_close.md"
    _write_csv(finance_file, _monthly_rows())
    _write_csv(reconciliation_file, _reconciliation_rows())
    _write_csv(adjustments_file, _adjustment_rows())

    exit_code = main(
        [
            "--finance-monthly-file",
            str(finance_file),
            "--reconciliation-file",
            str(reconciliation_file),
            "--adjustment-file",
            str(adjustments_file),
            "--month",
            "2026-06",
            "--output-json-file",
            str(output_json),
            "--output-adjusted-csv-file",
            str(output_csv),
            "--output-markdown-file",
            str(output_markdown),
        ]
    )

    assert exit_code == 0
    payload = json.loads(output_json.read_text(encoding="utf-8"))
    assert payload["status"] == "PASS"
    assert payload["summary"]["adjusted_spend"] == "14.5"
    assert output_markdown.read_text(encoding="utf-8") == payload["markdown"]
    with output_csv.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["source"] == "attribution_adjustment"
    assert rows[1]["spend"] == "14.5"


def test_monthly_close_cli_blocks_when_adjustment_file_missing(tmp_path) -> None:
    finance_file = tmp_path / "aimanager_finance_monthly.csv"
    reconciliation_file = tmp_path / "aimanager_reconciliation.csv"
    output_json = tmp_path / "monthly_close.json"
    _write_csv(finance_file, _monthly_rows())
    _write_csv(reconciliation_file, _reconciliation_rows())

    exit_code = main(
        [
            "--finance-monthly-file",
            str(finance_file),
            "--reconciliation-file",
            str(reconciliation_file),
            "--adjustment-file",
            str(tmp_path / "missing-adjustments.csv"),
            "--month",
            "2026-06",
            "--output-json-file",
            str(output_json),
        ]
    )

    assert exit_code == 2
    payload = json.loads(output_json.read_text(encoding="utf-8"))
    assert payload["status"] == "BLOCKED"
    assert "missing adjustment file" in payload["detail"]


def _monthly_rows() -> list[dict[str, object]]:
    return [
        {
            "month": "2026-06",
            "department_id": "dept_market",
            "project_id": "proj_launch",
            "cost_center_id": "cc_growth",
            "user_id": "u_market_1",
            "key_alias": "market-key",
            "model": "gemini-2.5-flash",
            "endpoint": "/v1/chat/completions",
            "spend": "12.50",
            "currency": "CNY",
            "pricing_version": "m1-2026-06",
            "request_count": "7",
            "close_status": "ready",
        },
        {
            "month": "2026-06",
            "department_id": "unassigned",
            "project_id": "unassigned",
            "cost_center_id": "unassigned",
            "user_id": "u_market_1",
            "key_alias": "shadow-key",
            "model": "deepseek-chat",
            "endpoint": "/v1/chat/completions",
            "spend": "3.00",
            "currency": "CNY",
            "pricing_version": "m1-2026-06",
            "request_count": "2",
            "close_status": "blocked_unassigned",
        },
    ]


def _reconciliation_rows() -> list[dict[str, object]]:
    return [
        {
            "month": "2026-06",
            "model": "gemini-2.5-flash",
            "endpoint": "/v1/chat/completions",
            "aimanager_amount": "12.50",
            "ycapi_amount": "14.50",
            "difference": "2.00",
            "difference_rate": "0.137931",
            "currency": "CNY",
            "status": "needs_review",
        },
        {
            "month": "2026-06",
            "model": "deepseek-chat",
            "endpoint": "/v1/chat/completions",
            "aimanager_amount": "3.00",
            "ycapi_amount": "3.00",
            "difference": "0",
            "difference_rate": "0",
            "currency": "CNY",
            "status": "matched",
        },
    ]


def _adjustment_rows() -> list[dict[str, object]]:
    return [
        {
            "adjustment_id": "adj-supplemental-1",
            "month": "2026-06",
            "operation_type": "supplemental",
            "amount": "2.00",
            "currency": "CNY",
            "department_id": "dept_market",
            "project_id": "proj_launch",
            "cost_center_id": "cc_growth",
            "user_id": "u_market_1",
            "key_alias": "market-key",
            "model": "gemini-2.5-flash",
            "endpoint": "/v1/chat/completions",
            "pricing_version": "m1-2026-06",
            "effective_date": "2026-06-30",
            "approver": "finance-owner",
            "reason": "Late approved marketing usage invoice",
        },
        {
            "adjustment_id": "adj-reversal-1",
            "month": "2026-06",
            "operation_type": "reversal",
            "amount": "-3.00",
            "currency": "CNY",
            "reverses_entry_id": _unassigned_row_key(),
            "department_id": "dept_market",
            "project_id": "proj_launch",
            "cost_center_id": "cc_growth",
            "user_id": "u_market_1",
            "key_alias": "market-key",
            "model": "deepseek-chat",
            "endpoint": "/v1/chat/completions",
            "pricing_version": "m1-2026-06",
            "effective_date": "2026-06-30",
            "approver": "finance-owner",
            "reason": "Reverse duplicate shadow-key allocation",
        },
        {
            "adjustment_id": "adj-attribution-1",
            "month": "2026-06",
            "operation_type": "attribution_adjustment",
            "amount": "3.00",
            "currency": "CNY",
            "source_entry_id": _unassigned_row_key(),
            "from_department_id": "unassigned",
            "to_department_id": "dept_market",
            "from_project_id": "unassigned",
            "to_project_id": "proj_launch",
            "from_cost_center_id": "unassigned",
            "to_cost_center_id": "cc_growth",
            "from_key_alias": "shadow-key",
            "to_key_alias": "market-key",
            "effective_date": "2026-06-30",
            "approver": "finance-owner",
            "reason": "Assign shadow key spend to approved launch project",
        },
        {
            "adjustment_id": "adj-difference-1",
            "month": "2026-06",
            "operation_type": "difference_resolution",
            "amount": "2.00",
            "currency": "CNY",
            "reconciliation_key": "2026-06|gemini-2.5-flash|/v1/chat/completions|CNY",
            "resolution_status": "manual_correction",
            "effective_date": "2026-06-30",
            "approver": "finance-owner",
            "reason": "Supplemental entry reconciles ycapi invoice delta",
        },
    ]


def _unassigned_row_key() -> str:
    return (
        "2026-06|unassigned|unassigned|unassigned|u_market_1|shadow-key|"
        "deepseek-chat|/v1/chat/completions|CNY|m1-2026-06"
    )


def _write_csv(path, rows: list[dict[str, object]]) -> None:
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

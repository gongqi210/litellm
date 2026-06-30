from __future__ import annotations

import csv
import json

from aimanager.business_overview import build_business_overview
from aimanager.scripts.generate_business_overview import main


def test_business_overview_summarizes_finance_budget_observability_for_ceo() -> None:
    result = build_business_overview(
        monthly_rows=_monthly_rows(),
        budget_rows=_budget_rows(),
        observability_report=_observability_report(),
        month="2026-06",
    )

    assert result["status"] == "PASS"
    assert result["month"] == "2026-06"
    assert result["summary"]["total_spend"] == "15.5"
    assert result["summary"]["currency"] == "CNY"
    assert result["summary"]["budget_amount"] == "20"
    assert result["summary"]["budget_utilization_rate"] == "0.775000"
    assert result["top_departments"][0] == {
        "department_id": "dept_market",
        "spend": "12.5",
        "request_count": 7,
        "budget_amount": "14",
        "budget_utilization_rate": "0.892857",
    }
    assert result["top_projects"][0]["project_id"] == "proj_launch"
    assert result["top_projects"][0]["spend"] == "12.5"
    assert result["top_keys"][0]["key_alias"] == "market-key"
    anomaly_codes = [item["code"] for item in result["anomalies"]]
    assert anomaly_codes == [
        "aimanager_budget_blocked_seen",
        "aimanager_http_429_seen",
        "aimanager_http_5xx_seen",
    ]
    assert anomaly_codes.count("aimanager_budget_blocked_seen") == 1
    assert {item["code"]: item["severity"] for item in result["anomalies"]}["aimanager_http_5xx_seen"] == "high"
    assert "# AiManager 经营总览 - 2026-06" in result["markdown"]
    assert "月费用：15.5 CNY" in result["markdown"]
    assert "预算消耗：15.5 / 20 CNY (77.500000%)" in result["markdown"]
    assert "dept_market" in result["markdown"]
    assert "aimanager_budget_blocked_seen" in result["markdown"]


def test_business_overview_prefers_canonical_alerts_over_metric_derivation() -> None:
    report = {
        "metrics": {
            "request_count": 4,
            "failed_requests": 2,
            "failure_rate": "0.500000",
            "http_429_count": 1,
            "http_5xx_count": 1,
            "budget_blocked_count": 1,
            "passthrough_blocked_count": 1,
            "missing_request_id_count": 1,
        },
        "alerts": [
            {"code": "aimanager_http_429_seen", "severity": "warning", "count": 1},
            {"code": "aimanager_http_5xx_seen", "severity": "high", "count": 1},
            {"code": "aimanager_budget_blocked_seen", "severity": "high", "count": 1},
            {"code": "aimanager_passthrough_blocked_seen", "severity": "warning", "count": 1},
            {"code": "aimanager_request_id_missing", "severity": "high", "count": 1},
        ],
    }

    result = build_business_overview(
        monthly_rows=_monthly_rows(),
        budget_rows=_budget_rows(),
        observability_report=report,
        month="2026-06",
    )

    codes = [item["code"] for item in result["anomalies"]]
    assert codes == [
        "aimanager_http_429_seen",
        "aimanager_http_5xx_seen",
        "aimanager_budget_blocked_seen",
        "aimanager_passthrough_blocked_seen",
        "aimanager_request_id_missing",
    ]
    assert len(codes) == len(set(codes))
    assert {item["code"]: item["severity"] for item in result["anomalies"]} == {
        "aimanager_http_429_seen": "warning",
        "aimanager_http_5xx_seen": "high",
        "aimanager_budget_blocked_seen": "high",
        "aimanager_passthrough_blocked_seen": "warning",
        "aimanager_request_id_missing": "high",
    }


def test_business_overview_budget_fallback_does_not_stack_overlapping_scopes() -> None:
    result = build_business_overview(
        monthly_rows=_monthly_rows(),
        budget_rows=[
            {
                "month": "2026-06",
                "scope_type": "project",
                "scope_id": "proj_launch",
                "budget_amount": "30",
                "currency": "CNY",
            },
            {
                "month": "2026-06",
                "scope_type": "key",
                "scope_id": "market-key",
                "budget_amount": "30",
                "currency": "CNY",
            },
        ],
        observability_report=_observability_report(),
        month="2026-06",
    )

    assert result["status"] == "PASS"
    assert result["summary"]["budget_amount"] == "30"
    assert result["summary"]["budget_utilization_rate"] == "0.516667"


def test_business_overview_top_rankings_use_stable_tie_breaks() -> None:
    rows = [
        {
            "month": "2026-06",
            "department_id": department_id,
            "project_id": project_id,
            "cost_center_id": "cc_shared",
            "user_id": f"user_{department_id}",
            "key_alias": key_alias,
            "model": "gemini-2.5-flash",
            "endpoint": "/v1/chat/completions",
            "spend": "5",
            "currency": "CNY",
            "request_count": "1",
            "failed_requests": "0",
        }
        for department_id, project_id, key_alias in (
            ("dept_d", "proj_d", "key_d"),
            ("dept_b", "proj_b", "key_b"),
            ("dept_c", "proj_c", "key_c"),
            ("dept_a", "proj_a", "key_a"),
        )
    ]

    result = build_business_overview(
        monthly_rows=rows,
        budget_rows=[
            {
                "month": "2026-06",
                "scope_type": "company",
                "scope_id": "company",
                "budget_amount": "40",
                "currency": "CNY",
            }
        ],
        observability_report={"metrics": {}, "alerts": []},
        month="2026-06",
        top_n=3,
    )

    assert [row["department_id"] for row in result["top_departments"]] == ["dept_a", "dept_b", "dept_c"]
    assert [row["project_id"] for row in result["top_projects"]] == ["proj_a", "proj_b", "proj_c"]
    assert [row["key_alias"] for row in result["top_keys"]] == ["key_a", "key_b", "key_c"]


def test_business_overview_zero_budget_with_spend_is_over_budget() -> None:
    result = build_business_overview(
        monthly_rows=_monthly_rows(),
        budget_rows=[
            {
                "month": "2026-06",
                "scope_type": "company",
                "scope_id": "company",
                "budget_amount": "0",
                "currency": "CNY",
            }
        ],
        observability_report=_observability_report(),
        month="2026-06",
    )

    assert result["status"] == "PASS"
    assert result["summary"]["budget_utilization_rate"] == "over_budget"
    assert "预算消耗：15.5 / 0 CNY (over budget)" in result["markdown"]


def test_business_overview_accepts_null_alerts_and_uses_metric_fallback() -> None:
    result = build_business_overview(
        monthly_rows=_monthly_rows(),
        budget_rows=_budget_rows(),
        observability_report={
            "metrics": {
                "http_429_count": 0,
                "http_5xx_count": 1,
                "budget_blocked_count": 0,
                "passthrough_blocked_count": 0,
                "missing_request_id_count": 1,
            },
            "alerts": None,
        },
        month="2026-06",
    )

    assert result["status"] == "PASS"
    assert [item["code"] for item in result["anomalies"]] == [
        "aimanager_http_5xx_seen",
        "aimanager_request_id_missing",
    ]


def test_business_overview_blocks_without_budget_rows() -> None:
    result = build_business_overview(
        monthly_rows=_monthly_rows(),
        budget_rows=[],
        observability_report=_observability_report(),
        month="2026-06",
    )

    assert result["status"] == "BLOCKED"
    assert "budget" in result["detail"].lower()
    assert result["budget_utilization"] == []


def test_business_overview_fails_on_mixed_currency() -> None:
    rows = [
        *_monthly_rows(),
        {
            "month": "2026-06",
            "department_id": "dept_engineering",
            "project_id": "proj_platform",
            "cost_center_id": "cc_platform",
            "user_id": "u_engineering_1",
            "key_alias": "engineering-key",
            "model": "deepseek-chat",
            "endpoint": "/v1/chat/completions",
            "spend": "1.00",
            "currency": "USD",
            "request_count": "1",
            "failed_requests": "0",
        },
    ]

    result = build_business_overview(
        monthly_rows=rows,
        budget_rows=_budget_rows(),
        observability_report=_observability_report(),
        month="2026-06",
    )

    assert result["status"] == "FAIL"
    assert "mixed currency" in result["detail"]


def test_business_overview_cli_writes_json_and_markdown(tmp_path) -> None:
    finance_file = tmp_path / "aimanager_finance_monthly.csv"
    budget_file = tmp_path / "budgets.csv"
    observability_file = tmp_path / "observability.json"
    output_json = tmp_path / "business_overview.json"
    output_markdown = tmp_path / "business_overview.md"
    _write_csv(finance_file, _monthly_rows())
    _write_csv(budget_file, _budget_rows())
    observability_file.write_text(json.dumps(_observability_report()), encoding="utf-8")

    exit_code = main(
        [
            "--finance-monthly-file",
            str(finance_file),
            "--budget-file",
            str(budget_file),
            "--observability-report-file",
            str(observability_file),
            "--month",
            "2026-06",
            "--output-json-file",
            str(output_json),
            "--output-markdown-file",
            str(output_markdown),
        ]
    )

    assert exit_code == 0
    payload = json.loads(output_json.read_text(encoding="utf-8"))
    assert payload["status"] == "PASS"
    assert payload["summary"]["total_spend"] == "15.5"
    assert output_markdown.read_text(encoding="utf-8") == payload["markdown"]


def test_business_overview_cli_blocks_when_budget_file_missing(tmp_path) -> None:
    finance_file = tmp_path / "aimanager_finance_monthly.csv"
    observability_file = tmp_path / "observability.json"
    output_json = tmp_path / "business_overview.json"
    _write_csv(finance_file, _monthly_rows())
    observability_file.write_text(json.dumps(_observability_report()), encoding="utf-8")

    exit_code = main(
        [
            "--finance-monthly-file",
            str(finance_file),
            "--budget-file",
            str(tmp_path / "missing-budget.csv"),
            "--observability-report-file",
            str(observability_file),
            "--month",
            "2026-06",
            "--output-json-file",
            str(output_json),
        ]
    )

    assert exit_code == 2
    payload = json.loads(output_json.read_text(encoding="utf-8"))
    assert payload["status"] == "BLOCKED"
    assert "missing budget file" in payload["detail"]


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
            "request_count": "7",
            "failed_requests": "1",
        },
        {
            "month": "2026-06",
            "department_id": "dept_engineering",
            "project_id": "proj_platform",
            "cost_center_id": "cc_platform",
            "user_id": "u_engineering_1",
            "key_alias": "engineering-key",
            "model": "deepseek-chat",
            "endpoint": "/v1/chat/completions",
            "spend": "3.00",
            "currency": "CNY",
            "request_count": "3",
            "failed_requests": "0",
        },
    ]


def _budget_rows() -> list[dict[str, object]]:
    return [
        {
            "month": "2026-06",
            "scope_type": "company",
            "scope_id": "company",
            "budget_amount": "20.00",
            "currency": "CNY",
        },
        {
            "month": "2026-06",
            "scope_type": "department",
            "scope_id": "dept_market",
            "budget_amount": "14.00",
            "currency": "CNY",
        },
        {
            "month": "2026-06",
            "scope_type": "department",
            "scope_id": "dept_engineering",
            "budget_amount": "6.00",
            "currency": "CNY",
        },
        {
            "month": "2026-06",
            "scope_type": "project",
            "scope_id": "proj_launch",
            "budget_amount": "15.00",
            "currency": "CNY",
        },
    ]


def _observability_report() -> dict[str, object]:
    return {
        "metrics": {
            "request_count": 10,
            "failed_requests": 1,
            "failure_rate": "0.100000",
            "http_429_count": 1,
            "http_5xx_count": 1,
            "budget_blocked_count": 1,
            "passthrough_blocked_count": 0,
            "missing_request_id_count": 0,
        },
        "alerts": [
            {"code": "aimanager_budget_blocked_seen", "severity": "high", "count": 1},
            {"code": "aimanager_http_429_seen", "severity": "warning", "count": 1},
            {"code": "aimanager_http_5xx_seen", "severity": "high", "count": 1},
        ],
    }


def _write_csv(path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

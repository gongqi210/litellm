from __future__ import annotations

import json

from aimanager.scripts import production_readiness_bundle
from aimanager.scripts.production_readiness_bundle import (
    CheckResult,
    collect_production_readiness,
    main,
)


def test_production_readiness_blocks_without_required_inputs() -> None:
    bundle = collect_production_readiness(env={})

    assert bundle["status"] == "BLOCKED"
    statuses = {check["id"]: check["status"] for check in bundle["checks"]}
    assert statuses == {
        "AC-15": "BLOCKED",
        "AC-19": "BLOCKED",
        "AC-16-WECOM": "BLOCKED",
        "AC-12-13-FINANCE": "BLOCKED",
    }
    assert "YCAPI_API_TOKEN" in bundle["checks"][1]["detail"]
    assert bundle["checks"][0]["evidence"]["result_count"] == 2
    assert "AIMANAGER_WECOM_WEBHOOK_URL" in json.dumps(bundle, ensure_ascii=False)


def test_production_readiness_overall_status_prioritizes_fail_over_blocked() -> None:
    bundle = collect_production_readiness(
        env={},
        admin_boundary_runner=lambda **kwargs: [
            _script_result(status="PASS", detail="business/admin edge checks passed")
        ],
        live_ycapi_runner=lambda **kwargs: _script_result(status="FAIL", detail="ycapi returned HTTP 500"),
        wecom_router=lambda **kwargs: _script_result(status="BLOCKED", detail="missing webhook"),
        finance_runner=lambda **kwargs: CheckResult(
            id="AC-12-13-FINANCE",
            name="finance_export_reconciliation",
            status="PASS",
            detail="finance files exported",
            evidence={"output_files": ["aimanager_usage_daily.csv"]},
        ),
    )

    assert bundle["status"] == "FAIL"
    statuses = {check["id"]: check["status"] for check in bundle["checks"]}
    assert statuses["AC-19"] == "FAIL"
    assert statuses["AC-16-WECOM"] == "BLOCKED"


def test_production_readiness_redacts_token_and_webhook_values(tmp_path) -> None:
    report_file = tmp_path / "observability.json"
    report_file.write_text(
        json.dumps(
            {
                "metrics": {"request_count": 1},
                "alerts": [{"code": "aimanager_failure_rate_high", "severity": "high"}],
            }
        ),
        encoding="utf-8",
    )
    env = {
        "YCAPI_API_TOKEN": "ycapi-redaction-value",
        "AIMANAGER_WECOM_WEBHOOK_URL": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=wecom-redaction-value",
        "AIMANAGER_OBSERVABILITY_REPORT_FILE": str(report_file),
    }

    bundle = collect_production_readiness(
        env=env,
        admin_boundary_runner=lambda **kwargs: [
            _script_result(status="PASS", detail="business/admin edge checks passed")
        ],
        live_ycapi_runner=lambda **kwargs: _script_result(
            status="FAIL",
            detail="request failed with ycapi-redaction-value",
        ),
        wecom_router=lambda **kwargs: _script_result(
            status="FAIL",
            detail="bad webhook https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=wecom-redaction-value",
        ),
        finance_runner=lambda **kwargs: CheckResult(
            id="AC-12-13-FINANCE",
            name="finance_export_reconciliation",
            status="BLOCKED",
            detail="missing files",
            evidence={},
        ),
    )

    payload = json.dumps(bundle, ensure_ascii=False)
    assert "ycapi-redaction-value" not in payload
    assert "wecom-redaction-value" not in payload
    assert "https://qyapi.weixin.qq.com/cgi-bin/webhook/send" not in payload
    assert "[redacted:YCAPI_API_TOKEN]" in payload
    assert "[redacted:AIMANAGER_WECOM_WEBHOOK_URL]" in payload


def test_production_readiness_cli_writes_json_and_returns_blocked(monkeypatch, tmp_path, capsys) -> None:
    output_file = tmp_path / "readiness.json"
    monkeypatch.delenv("YCAPI_API_TOKEN", raising=False)
    monkeypatch.delenv("AIMANAGER_BUSINESS_BASE_URL", raising=False)
    monkeypatch.delenv("AIMANAGER_PUBLIC_ADMIN_URL", raising=False)
    monkeypatch.delenv("AIMANAGER_WECOM_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("AIMANAGER_OBSERVABILITY_REPORT_FILE", raising=False)
    monkeypatch.delenv("AIMANAGER_SPEND_FILE", raising=False)
    monkeypatch.delenv("AIMANAGER_YCAPI_BILL_FILE", raising=False)

    exit_code = main(["--output-json-file", str(output_file)])

    output = capsys.readouterr().out
    assert exit_code == 2
    assert "BLOCKED production readiness bundle" in output
    bundle = json.loads(output_file.read_text(encoding="utf-8"))
    assert bundle["status"] == "BLOCKED"
    assert {check["id"] for check in bundle["checks"]} == {
        "AC-15",
        "AC-19",
        "AC-16-WECOM",
        "AC-12-13-FINANCE",
    }


def test_wecom_readiness_blocks_when_webhook_missing_even_if_alerts_are_below_threshold(tmp_path) -> None:
    report_file = tmp_path / "observability.json"
    report_file.write_text(
        json.dumps(
            {
                "metrics": {"request_count": 1},
                "alerts": [{"code": "aimanager_info_only", "severity": "info"}],
            }
        ),
        encoding="utf-8",
    )

    bundle = collect_production_readiness(
        env={
            "AIMANAGER_OBSERVABILITY_REPORT_FILE": str(report_file),
            "AIMANAGER_WECOM_MIN_SEVERITY": "warning",
        },
        admin_boundary_runner=lambda **kwargs: [
            _script_result(status="PASS", detail="business/admin edge checks passed")
        ],
        live_ycapi_runner=lambda **kwargs: _script_result(status="PASS", detail="ycapi /models returned 3 models"),
        finance_runner=lambda **kwargs: CheckResult(
            id="AC-12-13-FINANCE",
            name="finance_export_reconciliation",
            status="PASS",
            detail="finance files exported",
            evidence={"output_files": ["aimanager_usage_daily.csv"]},
        ),
    )

    wecom_check = next(check for check in bundle["checks"] if check["id"] == "AC-16-WECOM")
    assert bundle["status"] == "BLOCKED"
    assert wecom_check["status"] == "BLOCKED"
    assert "AIMANAGER_WECOM_WEBHOOK_URL" in wecom_check["detail"]


def test_wecom_readiness_blocks_when_no_alert_was_delivered(tmp_path) -> None:
    report_file = tmp_path / "observability.json"
    report_file.write_text(
        json.dumps(
            {
                "metrics": {"request_count": 1},
                "alerts": [{"code": "aimanager_info_only", "severity": "info"}],
            }
        ),
        encoding="utf-8",
    )

    bundle = collect_production_readiness(
        env={
            "AIMANAGER_OBSERVABILITY_REPORT_FILE": str(report_file),
            "AIMANAGER_WECOM_WEBHOOK_URL": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=redaction",
            "AIMANAGER_WECOM_MIN_SEVERITY": "warning",
        },
        admin_boundary_runner=lambda **kwargs: [
            _script_result(status="PASS", detail="business/admin edge checks passed")
        ],
        live_ycapi_runner=lambda **kwargs: _script_result(status="PASS", detail="ycapi /models returned 3 models"),
        finance_runner=lambda **kwargs: CheckResult(
            id="AC-12-13-FINANCE",
            name="finance_export_reconciliation",
            status="PASS",
            detail="finance files exported",
            evidence={"output_files": ["aimanager_usage_daily.csv"]},
        ),
    )

    wecom_check = next(check for check in bundle["checks"] if check["id"] == "AC-16-WECOM")
    assert bundle["status"] == "BLOCKED"
    assert wecom_check["status"] == "BLOCKED"
    assert "delivered_count=0" in wecom_check["detail"]


def test_finance_evidence_passes_with_real_export_files(tmp_path) -> None:
    spend_file = tmp_path / "spend.json"
    bill_file = tmp_path / "ycapi_bill.json"
    output_dir = tmp_path / "finance"
    spend_file.write_text(
        json.dumps(
            [
                {
                    "startTime": "2026-06-30T02:15:00Z",
                    "call_type": "completion",
                    "user": "u_market_1",
                    "model": "openai/gemini-2.5-flash",
                    "prompt_tokens": 150,
                    "completion_tokens": 40,
                    "total_tokens": 190,
                    "spend": "2.00",
                    "currency": "CNY",
                    "metadata": {
                        "user_api_key_alias": "market-campaign-key",
                        "user_api_key_metadata": {
                            "department_id": "dept_market",
                            "project_id": "proj_launch",
                            "cost_center_id": "cc_growth",
                            "pricing_version": "m1-2026-06",
                        },
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    bill_file.write_text(
        json.dumps(
            [
                {
                    "billing_month": "2026-06",
                    "model_name": "gemini-2.5-flash",
                    "api_path": "/v1/chat/completions",
                    "prompt_tokens": "150",
                    "completion_tokens": "40",
                    "total_tokens": "190",
                    "amount": "2.10",
                    "currency": "CNY",
                }
            ]
        ),
        encoding="utf-8",
    )

    bundle = collect_production_readiness(
        env={
            "AIMANAGER_SPEND_FILE": str(spend_file),
            "AIMANAGER_YCAPI_BILL_FILE": str(bill_file),
            "AIMANAGER_FINANCE_OUTPUT_DIR": str(output_dir),
        },
        admin_boundary_runner=lambda **kwargs: [
            _script_result(status="PASS", detail="business/admin edge checks passed")
        ],
        live_ycapi_runner=lambda **kwargs: _script_result(status="PASS", detail="ycapi /models returned 3 models"),
        wecom_router=lambda **kwargs: _script_result(status="PASS", detail="sent 1 alert to WeCom webhook"),
    )

    finance_check = next(check for check in bundle["checks"] if check["id"] == "AC-12-13-FINANCE")
    assert finance_check["status"] == "PASS"
    assert sorted(path.name for path in output_dir.iterdir()) == [
        "aimanager_finance_monthly.csv",
        "aimanager_reconciliation.csv",
        "aimanager_usage_daily.csv",
    ]


def test_finance_evidence_blocks_empty_production_inputs(tmp_path) -> None:
    spend_file = tmp_path / "spend.json"
    bill_file = tmp_path / "ycapi_bill.json"
    output_dir = tmp_path / "finance"
    spend_file.write_text("[]", encoding="utf-8")
    bill_file.write_text("[]", encoding="utf-8")

    bundle = collect_production_readiness(
        env={
            "AIMANAGER_SPEND_FILE": str(spend_file),
            "AIMANAGER_YCAPI_BILL_FILE": str(bill_file),
            "AIMANAGER_FINANCE_OUTPUT_DIR": str(output_dir),
        },
        admin_boundary_runner=lambda **kwargs: [
            _script_result(status="PASS", detail="business/admin edge checks passed")
        ],
        live_ycapi_runner=lambda **kwargs: _script_result(status="PASS", detail="ycapi /models returned 3 models"),
        wecom_router=lambda **kwargs: _script_result(status="PASS", detail="sent 1 alert to WeCom webhook"),
    )

    finance_check = next(check for check in bundle["checks"] if check["id"] == "AC-12-13-FINANCE")
    assert bundle["status"] == "BLOCKED"
    assert finance_check["status"] == "BLOCKED"
    assert "empty" in finance_check["detail"]


def test_finance_evidence_blocks_non_billable_placeholder_inputs(tmp_path) -> None:
    spend_file = tmp_path / "spend.json"
    bill_file = tmp_path / "ycapi_bill.json"
    output_dir = tmp_path / "finance"
    spend_file.write_text(
        json.dumps(
            [
                {
                    "startTime": "2026-06-30T02:15:00Z",
                    "model": "openai/gemini-2.5-flash",
                }
            ]
        ),
        encoding="utf-8",
    )
    bill_file.write_text(
        json.dumps(
            [
                {
                    "billing_month": "2026-06",
                    "model_name": "gemini-2.5-flash",
                    "api_path": "/v1/chat/completions",
                }
            ]
        ),
        encoding="utf-8",
    )

    bundle = collect_production_readiness(
        env={
            "AIMANAGER_SPEND_FILE": str(spend_file),
            "AIMANAGER_YCAPI_BILL_FILE": str(bill_file),
            "AIMANAGER_FINANCE_OUTPUT_DIR": str(output_dir),
        },
        admin_boundary_runner=lambda **kwargs: [
            _script_result(status="PASS", detail="business/admin edge checks passed")
        ],
        live_ycapi_runner=lambda **kwargs: _script_result(status="PASS", detail="ycapi /models returned 3 models"),
        wecom_router=lambda **kwargs: _script_result(status="PASS", detail="sent 1 alert to WeCom webhook"),
    )

    finance_check = next(check for check in bundle["checks"] if check["id"] == "AC-12-13-FINANCE")
    assert bundle["status"] == "BLOCKED"
    assert finance_check["status"] == "BLOCKED"
    assert "billable" in finance_check["detail"]


def _script_result(*, status: str, detail: str):
    return type(
        "ScriptResult",
        (),
        {
            "status": status,
            "detail": detail,
            "status_code": 200 if status == "PASS" else None,
            "model_count": 3 if status == "PASS" else 0,
            "observed_models": ("gemini-2.5-flash", "deepseek-chat", "ycapi-image-1") if status == "PASS" else (),
            "alert_count": 1,
            "delivered_count": 1 if status == "PASS" else 0,
            "payload": None,
            "name": "script_case",
            "surface": "business",
            "method": "GET",
            "path": "/ui",
            "policy_code": "aimanager_route_not_allowed",
        },
    )()


def test_module_exports_main() -> None:
    assert production_readiness_bundle.main is main

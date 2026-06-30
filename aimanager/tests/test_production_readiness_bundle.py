from __future__ import annotations

import json
from pathlib import Path

from aimanager.scripts import production_readiness_bundle
from aimanager.scripts.production_readiness_bundle import (
    CheckResult,
    collect_production_readiness,
    main,
)
from aimanager.scripts.smoke_admin_boundary import AdminBoundaryResult


def test_production_readiness_blocks_without_required_inputs() -> None:
    bundle = collect_production_readiness(env={})

    assert bundle["status"] == "BLOCKED"
    statuses = {check["id"]: check["status"] for check in bundle["checks"]}
    assert statuses == {
        "AC-15": "BLOCKED",
        "AC-19": "BLOCKED",
        "AC-08-KEY-INVENTORY": "BLOCKED",
        "AC-16-WECOM": "BLOCKED",
        "AC-12-13-FINANCE": "BLOCKED",
        "AC-POLICY": "BLOCKED",
    }
    assert "YCAPI_API_TOKEN" in bundle["checks"][1]["detail"]
    assert bundle["checks"][0]["evidence"]["result_count"] == 2
    assert "AIMANAGER_WECOM_WEBHOOK_URL" in json.dumps(bundle, ensure_ascii=False)


def test_production_readiness_blocks_without_key_inventory() -> None:
    bundle = collect_production_readiness(
        env={},
        admin_boundary_runner=lambda **kwargs: [
            _script_result(status="PASS", detail="business/admin edge checks passed")
        ],
        live_ycapi_runner=lambda **kwargs: _script_result(status="PASS", detail="ycapi /models returned 3 models"),
        wecom_router=lambda **kwargs: _script_result(status="PASS", detail="sent 1 alert to WeCom webhook"),
        finance_runner=lambda **kwargs: CheckResult(
            id="AC-12-13-FINANCE",
            name="finance_export_reconciliation",
            status="PASS",
            detail="finance files exported",
            evidence={"output_files": ["aimanager_usage_daily.csv"]},
        ),
    )

    key_inventory = next(check for check in bundle["checks"] if check["id"] == "AC-08-KEY-INVENTORY")
    assert bundle["status"] == "BLOCKED"
    assert key_inventory["status"] == "BLOCKED"
    assert "AIMANAGER_KEY_INVENTORY_FILE" in key_inventory["detail"]


def test_production_readiness_blocks_empty_key_inventory(tmp_path) -> None:
    inventory_file = tmp_path / "key-inventory.json"
    policy_file = tmp_path / "production-policy.json"
    report_file = tmp_path / "observability.json"
    inventory_file.write_text(json.dumps({"keys": []}), encoding="utf-8")
    policy_file.write_text(json.dumps(_valid_policy_attestation()), encoding="utf-8")
    report_file.write_text(json.dumps(_observability_report_with_alert()), encoding="utf-8")

    bundle = collect_production_readiness(
        env={
            "AIMANAGER_KEY_INVENTORY_FILE": str(inventory_file),
            "AIMANAGER_PRODUCTION_POLICY_ATTESTATION_FILE": str(policy_file),
            "AIMANAGER_OBSERVABILITY_REPORT_FILE": str(report_file),
            "AIMANAGER_WECOM_WEBHOOK_URL": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=redaction",
        },
        admin_boundary_runner=lambda **kwargs: [
            _script_result(status="PASS", detail="business/admin edge checks passed")
        ],
        live_ycapi_runner=lambda **kwargs: _script_result(status="PASS", detail="ycapi /models returned 3 models"),
        wecom_router=lambda **kwargs: _script_result(status="PASS", detail="sent 1 alert to WeCom webhook"),
        finance_runner=lambda **kwargs: CheckResult(
            id="AC-12-13-FINANCE",
            name="finance_export_reconciliation",
            status="PASS",
            detail="finance files exported",
            evidence={"output_files": ["aimanager_usage_daily.csv"]},
        ),
    )

    key_inventory = next(check for check in bundle["checks"] if check["id"] == "AC-08-KEY-INVENTORY")
    assert bundle["status"] == "BLOCKED"
    assert key_inventory["status"] == "BLOCKED"
    assert key_inventory["evidence"]["active_key_count"] == 0


def test_production_readiness_passes_with_governed_key_inventory(tmp_path) -> None:
    inventory_file = tmp_path / "key-inventory.json"
    policy_file = tmp_path / "production-policy.json"
    report_file = tmp_path / "observability.json"
    inventory_file.write_text(json.dumps(_valid_key_inventory()), encoding="utf-8")
    policy_file.write_text(json.dumps(_valid_policy_attestation()), encoding="utf-8")
    report_file.write_text(json.dumps(_observability_report_with_alert()), encoding="utf-8")

    bundle = collect_production_readiness(
        env={
            "AIMANAGER_KEY_INVENTORY_FILE": str(inventory_file),
            "AIMANAGER_PRODUCTION_POLICY_ATTESTATION_FILE": str(policy_file),
            "AIMANAGER_OBSERVABILITY_REPORT_FILE": str(report_file),
            "AIMANAGER_WECOM_WEBHOOK_URL": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=redaction",
        },
        admin_boundary_runner=lambda **kwargs: [
            _script_result(status="PASS", detail="business/admin edge checks passed")
        ],
        live_ycapi_runner=lambda **kwargs: _script_result(status="PASS", detail="ycapi /models returned 3 models"),
        wecom_router=lambda **kwargs: _script_result(status="PASS", detail="sent 1 alert to WeCom webhook"),
        finance_runner=lambda **kwargs: CheckResult(
            id="AC-12-13-FINANCE",
            name="finance_export_reconciliation",
            status="PASS",
            detail="finance files exported",
            evidence={"output_files": ["aimanager_usage_daily.csv"]},
        ),
    )

    key_inventory = next(check for check in bundle["checks"] if check["id"] == "AC-08-KEY-INVENTORY")
    assert bundle["status"] == "PASS"
    assert key_inventory["status"] == "PASS"
    assert key_inventory["evidence"]["active_key_count"] == 1
    assert key_inventory["evidence"]["violation_count"] == 0


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


def test_admin_boundary_evidence_keeps_each_probe_result() -> None:
    bundle = collect_production_readiness(
        env={},
        admin_boundary_runner=lambda **kwargs: [
            AdminBoundaryResult(
                name="business_admin_ui",
                surface="business",
                method="GET",
                path="/ui",
                status="PASS",
                detail="blocked by AiManager business-surface policy",
                status_code=403,
                policy_code="aimanager_route_not_allowed",
            ),
            AdminBoundaryResult(
                name="public_admin_ui",
                surface="public_admin",
                method="GET",
                path="/ui",
                status="PASS",
                detail="allowed SSO redirect to https://sso.example.com/login",
                status_code=302,
                policy_code=None,
            ),
        ],
        live_ycapi_runner=lambda **kwargs: _script_result(status="PASS", detail="ycapi /models returned 3 models"),
        wecom_router=lambda **kwargs: _script_result(status="PASS", detail="sent 1 alert to WeCom webhook"),
        finance_runner=lambda **kwargs: CheckResult(
            id="AC-12-13-FINANCE",
            name="finance_export_reconciliation",
            status="PASS",
            detail="finance files exported",
            evidence={"output_files": ["aimanager_usage_daily.csv"]},
        ),
    )

    ac15 = next(check for check in bundle["checks"] if check["id"] == "AC-15")
    assert ac15["status"] == "PASS"
    assert ac15["evidence"]["results"] == [
        {
            "name": "business_admin_ui",
            "surface": "business",
            "method": "GET",
            "path": "/ui",
            "status": "PASS",
            "status_code": 403,
            "policy_code": "aimanager_route_not_allowed",
            "detail": "blocked by AiManager business-surface policy",
        },
        {
            "name": "public_admin_ui",
            "surface": "public_admin",
            "method": "GET",
            "path": "/ui",
            "status": "PASS",
            "status_code": 302,
            "policy_code": None,
            "detail": "allowed SSO redirect to https://sso.example.com/login",
        },
    ]


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
    monkeypatch.delenv("AIMANAGER_PRODUCTION_POLICY_ATTESTATION_FILE", raising=False)
    monkeypatch.delenv("AIMANAGER_KEY_INVENTORY_FILE", raising=False)

    exit_code = main(["--output-json-file", str(output_file)])

    output = capsys.readouterr().out
    assert exit_code == 2
    assert "BLOCKED production readiness bundle" in output
    bundle = json.loads(output_file.read_text(encoding="utf-8"))
    assert bundle["status"] == "BLOCKED"
    assert {check["id"] for check in bundle["checks"]} == {
        "AC-15",
        "AC-19",
        "AC-08-KEY-INVENTORY",
        "AC-16-WECOM",
        "AC-12-13-FINANCE",
        "AC-POLICY",
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


def test_production_policy_attestation_passes_with_required_manual_checks(tmp_path) -> None:
    policy_file = tmp_path / "production_policy_attestation.json"
    report_file = tmp_path / "observability.json"
    inventory_file = tmp_path / "key-inventory.json"
    policy_file.write_text(json.dumps(_valid_policy_attestation()), encoding="utf-8")
    report_file.write_text(json.dumps(_observability_report_with_alert()), encoding="utf-8")
    inventory_file.write_text(json.dumps(_valid_key_inventory()), encoding="utf-8")

    bundle = collect_production_readiness(
        env={
            "AIMANAGER_KEY_INVENTORY_FILE": str(inventory_file),
            "AIMANAGER_PRODUCTION_POLICY_ATTESTATION_FILE": str(policy_file),
            "AIMANAGER_OBSERVABILITY_REPORT_FILE": str(report_file),
            "AIMANAGER_WECOM_WEBHOOK_URL": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=redaction",
        },
        admin_boundary_runner=lambda **kwargs: [
            _script_result(status="PASS", detail="business/admin edge checks passed")
        ],
        live_ycapi_runner=lambda **kwargs: _script_result(status="PASS", detail="ycapi /models returned 3 models"),
        wecom_router=lambda **kwargs: _script_result(status="PASS", detail="sent 1 alert to WeCom webhook"),
        finance_runner=lambda **kwargs: CheckResult(
            id="AC-12-13-FINANCE",
            name="finance_export_reconciliation",
            status="PASS",
            detail="finance files exported",
            evidence={"output_files": ["aimanager_usage_daily.csv"]},
        ),
    )

    policy_check = next(check for check in bundle["checks"] if check["id"] == "AC-POLICY")
    assert bundle["status"] == "PASS"
    assert policy_check["status"] == "PASS"
    assert policy_check["evidence"] == {
        "policy_id": "aimanager-production-policy",
        "policy_version": "2026-06",
        "approver_count": 3,
        "chargeback_mode": "showback",
        "data_boundary_count": 3,
        "ycapi_token_limit_confirmed": True,
        "employee_virtual_key_only": True,
    }


def test_production_policy_attestation_rejects_missing_required_confirmation(tmp_path) -> None:
    policy_file = tmp_path / "production_policy_attestation.json"
    payload = _valid_policy_attestation()
    payload["employee_virtual_key_only"]["confirmed"] = False
    policy_file.write_text(json.dumps(payload), encoding="utf-8")

    bundle = collect_production_readiness(
        env={"AIMANAGER_PRODUCTION_POLICY_ATTESTATION_FILE": str(policy_file)},
        admin_boundary_runner=lambda **kwargs: [
            _script_result(status="PASS", detail="business/admin edge checks passed")
        ],
        live_ycapi_runner=lambda **kwargs: _script_result(status="PASS", detail="ycapi /models returned 3 models"),
        wecom_router=lambda **kwargs: _script_result(status="PASS", detail="sent 1 alert to WeCom webhook"),
        finance_runner=lambda **kwargs: CheckResult(
            id="AC-12-13-FINANCE",
            name="finance_export_reconciliation",
            status="PASS",
            detail="finance files exported",
            evidence={"output_files": ["aimanager_usage_daily.csv"]},
        ),
    )

    policy_check = next(check for check in bundle["checks"] if check["id"] == "AC-POLICY")
    assert bundle["status"] == "FAIL"
    assert policy_check["status"] == "FAIL"
    assert "employee_virtual_key_only.confirmed" in policy_check["detail"]


def test_production_policy_attestation_redacts_secret_like_values(tmp_path) -> None:
    policy_file = tmp_path / "production_policy_attestation.json"
    payload = _valid_policy_attestation()
    payload["approvals"][0]["approval_ref"] = "Bearer should-not-leak"
    policy_file.write_text(json.dumps(payload), encoding="utf-8")

    bundle = collect_production_readiness(
        env={"AIMANAGER_PRODUCTION_POLICY_ATTESTATION_FILE": str(policy_file)},
        admin_boundary_runner=lambda **kwargs: [
            _script_result(status="PASS", detail="business/admin edge checks passed")
        ],
        live_ycapi_runner=lambda **kwargs: _script_result(status="PASS", detail="ycapi /models returned 3 models"),
        wecom_router=lambda **kwargs: _script_result(status="PASS", detail="sent 1 alert to WeCom webhook"),
        finance_runner=lambda **kwargs: CheckResult(
            id="AC-12-13-FINANCE",
            name="finance_export_reconciliation",
            status="PASS",
            detail="finance files exported",
            evidence={"output_files": ["aimanager_usage_daily.csv"]},
        ),
    )

    serialized = json.dumps(bundle, ensure_ascii=False)
    assert bundle["status"] == "FAIL"
    assert "should-not-leak" not in serialized
    assert "[redacted:secret-like-value]" in serialized


def test_production_policy_attestation_blocks_when_file_is_missing(tmp_path) -> None:
    missing_policy_file = tmp_path / "missing-policy.json"

    bundle = _policy_bundle_for_env({"AIMANAGER_PRODUCTION_POLICY_ATTESTATION_FILE": str(missing_policy_file)})

    policy_check = next(check for check in bundle["checks"] if check["id"] == "AC-POLICY")
    assert bundle["status"] == "BLOCKED"
    assert policy_check["status"] == "BLOCKED"
    assert "does not exist" in policy_check["detail"]


def test_production_policy_attestation_rejects_malformed_json(tmp_path) -> None:
    policy_file = tmp_path / "production_policy_attestation.json"
    policy_file.write_text("{", encoding="utf-8")

    bundle = _policy_bundle_for_env({"AIMANAGER_PRODUCTION_POLICY_ATTESTATION_FILE": str(policy_file)})

    policy_check = next(check for check in bundle["checks"] if check["id"] == "AC-POLICY")
    assert bundle["status"] == "FAIL"
    assert policy_check["status"] == "FAIL"
    assert "could not be loaded" in policy_check["detail"]


def test_production_policy_attestation_rejects_visible_ycapi_token_to_employee(tmp_path) -> None:
    payload = _valid_policy_attestation()
    payload["employee_virtual_key_only"]["ycapi_token_visible_to_employee"] = True

    policy_check = _policy_check_for_payload(tmp_path, payload)

    assert policy_check["status"] == "FAIL"
    assert "employee_virtual_key_only.ycapi_token_visible_to_employee" in policy_check["detail"]


def test_production_policy_attestation_rejects_missing_required_approval_role(tmp_path) -> None:
    payload = _valid_policy_attestation()
    payload["approvals"] = [
        {"role": "finance", "approver": "finance-controller", "approval_ref": "FIN-APPROVED"},
        {"role": "security", "approver": "security-owner", "approval_ref": "SEC-APPROVED"},
        {"role": "operations", "approver": "ops-owner", "approval_ref": "OPS-ACK"},
    ]

    policy_check = _policy_check_for_payload(tmp_path, payload)

    assert policy_check["status"] == "FAIL"
    assert "approvals.legal" in policy_check["detail"]


def test_production_policy_attestation_rejects_invalid_chargeback_mode(tmp_path) -> None:
    payload = _valid_policy_attestation()
    payload["chargeback"]["mode"] = "informal-tracking"

    policy_check = _policy_check_for_payload(tmp_path, payload)

    assert policy_check["status"] == "FAIL"
    assert "chargeback.mode" in policy_check["detail"]


def test_production_policy_attestation_rejects_non_positive_ycapi_limits(tmp_path) -> None:
    payload = _valid_policy_attestation()
    payload["ycapi_token_limit"]["monthly_budget_cny"] = "0"
    payload["ycapi_token_limit"]["rpm_limit"] = 0

    policy_check = _policy_check_for_payload(tmp_path, payload)

    assert policy_check["status"] == "FAIL"
    assert "ycapi_token_limit.monthly_budget_cny" in policy_check["detail"]
    assert "ycapi_token_limit.rpm_limit" in policy_check["detail"]


def test_production_policy_attestation_rejects_non_finite_ycapi_limits(tmp_path) -> None:
    payload = _valid_policy_attestation()
    payload["ycapi_token_limit"]["monthly_budget_cny"] = float("nan")
    payload["ycapi_token_limit"]["rpm_limit"] = float("inf")

    policy_check = _policy_check_for_payload(tmp_path, payload)

    assert policy_check["status"] == "FAIL"
    assert "ycapi_token_limit.monthly_budget_cny" in policy_check["detail"]
    assert "ycapi_token_limit.rpm_limit" in policy_check["detail"]


def test_production_policy_attestation_requires_distinct_approvers_for_required_roles(tmp_path) -> None:
    payload = _valid_policy_attestation()
    for approval in payload["approvals"]:
        approval["approver"] = "same-person"

    policy_check = _policy_check_for_payload(tmp_path, payload)

    assert policy_check["status"] == "FAIL"
    assert "approvals.distinct_approvers" in policy_check["detail"]


def test_production_policy_attestation_normalizes_approver_names_for_distinctness(tmp_path) -> None:
    payload = _valid_policy_attestation()
    payload["approvals"][0]["approver"] = "Alice"
    payload["approvals"][1]["approver"] = "alice"
    payload["approvals"][2]["approver"] = " ALICE "

    policy_check = _policy_check_for_payload(tmp_path, payload)

    assert policy_check["status"] == "FAIL"
    assert "approvals.distinct_approvers" in policy_check["detail"]


def test_production_policy_attestation_example_file_is_valid() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    policy_file = repo_root / "docs" / "aimanager" / "production_policy_attestation.example.json"

    bundle = collect_production_readiness(
        env={"AIMANAGER_PRODUCTION_POLICY_ATTESTATION_FILE": str(policy_file)},
        admin_boundary_runner=lambda **kwargs: [
            _script_result(status="PASS", detail="business/admin edge checks passed")
        ],
        live_ycapi_runner=lambda **kwargs: _script_result(status="PASS", detail="ycapi /models returned 3 models"),
        wecom_router=lambda **kwargs: _script_result(status="PASS", detail="sent 1 alert to WeCom webhook"),
        finance_runner=lambda **kwargs: CheckResult(
            id="AC-12-13-FINANCE",
            name="finance_export_reconciliation",
            status="PASS",
            detail="finance files exported",
            evidence={"output_files": ["aimanager_usage_daily.csv"]},
        ),
    )

    policy_check = next(check for check in bundle["checks"] if check["id"] == "AC-POLICY")
    assert policy_check["status"] == "PASS"


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


def _policy_check_for_payload(tmp_path, payload: dict[str, object]) -> dict[str, object]:
    policy_file = tmp_path / "production_policy_attestation.json"
    policy_file.write_text(json.dumps(payload), encoding="utf-8")
    bundle = _policy_bundle_for_env({"AIMANAGER_PRODUCTION_POLICY_ATTESTATION_FILE": str(policy_file)})
    return next(check for check in bundle["checks"] if check["id"] == "AC-POLICY")


def _policy_bundle_for_env(env: dict[str, str]) -> dict[str, object]:
    return collect_production_readiness(
        env=env,
        admin_boundary_runner=lambda **kwargs: [
            _script_result(status="PASS", detail="business/admin edge checks passed")
        ],
        live_ycapi_runner=lambda **kwargs: _script_result(status="PASS", detail="ycapi /models returned 3 models"),
        wecom_router=lambda **kwargs: _script_result(status="PASS", detail="sent 1 alert to WeCom webhook"),
        finance_runner=lambda **kwargs: CheckResult(
            id="AC-12-13-FINANCE",
            name="finance_export_reconciliation",
            status="PASS",
            detail="finance files exported",
            evidence={"output_files": ["aimanager_usage_daily.csv"]},
        ),
    )


def _valid_policy_attestation() -> dict[str, object]:
    return {
        "policy_id": "aimanager-production-policy",
        "policy_version": "2026-06",
        "approved_at": "2026-06-30T09:00:00+08:00",
        "chargeback": {
            "mode": "showback",
            "confirmed": True,
            "policy_ref": "FIN-AI-2026-06",
            "effective_month": "2026-06",
        },
        "pricing_approval": {
            "confirmed": True,
            "pricing_version": "m1-2026-06",
            "approval_ref": "FIN-PRICE-2026-06",
            "approver": "finance-controller",
        },
        "ycapi_token_limit": {
            "confirmed": True,
            "limit_ref": "YCAPI-LIMIT-2026-06",
            "monthly_budget_cny": "10000",
            "rpm_limit": 600,
        },
        "employee_virtual_key_only": {
            "confirmed": True,
            "distribution_channel": "aimanager-admin",
            "ycapi_token_visible_to_employee": False,
        },
        "data_boundaries": {
            "confirmed": True,
            "policy_ref": "SEC-DATA-AI-2026-06",
            "categories": ["personal_information", "customer_data", "trade_secret"],
        },
        "approvals": [
            {"role": "finance", "approver": "finance-controller", "approval_ref": "FIN-APPROVED"},
            {"role": "security", "approver": "security-owner", "approval_ref": "SEC-APPROVED"},
            {"role": "legal", "approver": "legal-owner", "approval_ref": "LEGAL-APPROVED"},
        ],
    }


def _observability_report_with_alert() -> dict[str, object]:
    return {
        "metrics": {"request_count": 1},
        "alerts": [{"code": "aimanager_failure_rate_high", "severity": "high"}],
    }


def _valid_key_inventory() -> dict[str, object]:
    return {
        "exported_at": "2026-07-01T10:00:00+08:00",
        "keys": [
            {
                "key_alias": "market-campaign-key",
                "blocked": False,
                "user_id": "employee-1",
                "team_id": "team-marketing",
                "models": ["gemini-2.5-flash"],
                "max_budget": 100,
                "rpm_limit": 60,
                "tpm_limit": 120000,
                "metadata": {
                    "owner": "alice",
                    "department_id": "dept_marketing",
                    "project_id": "proj_launch",
                    "cost_center_id": "cc_growth",
                    "scenario_l1": "marketing",
                    "scenario_l2": "campaign_brief",
                    "approver": "finance-controller",
                    "internal_or_external": "internal",
                },
            }
        ],
    }


def test_module_exports_main() -> None:
    assert production_readiness_bundle.main is main

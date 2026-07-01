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
from aimanager.scripts.smoke_work_context_enforcement import WorkContextSmokeCase, WorkContextSmokeResult

GENERATED_AT = "2026-07-01T02:30:00Z"


def test_production_readiness_blocks_without_required_inputs() -> None:
    bundle = collect_production_readiness(env={})

    assert bundle["status"] == "BLOCKED"
    statuses = {check["id"]: check["status"] for check in bundle["checks"]}
    assert statuses == {
        "AC-15": "BLOCKED",
        "AC-20": "BLOCKED",
        "AC-19": "BLOCKED",
        "AC-08-KEY-INVENTORY": "BLOCKED",
        "AC-16-WECOM": "BLOCKED",
        "AC-12-13-FINANCE": "BLOCKED",
        "AC-POLICY": "BLOCKED",
    }
    ac19 = next(check for check in bundle["checks"] if check["id"] == "AC-19")
    ac20 = next(check for check in bundle["checks"] if check["id"] == "AC-20")
    assert "YCAPI_API_TOKEN" in ac19["detail"]
    assert "AIMANAGER_BUSINESS_BASE_URL" in ac20["detail"]
    assert "AIMANAGER_EMPLOYEE_VIRTUAL_KEY" in ac20["detail"]
    assert ac20["evidence"]["required_env"] == [
        "AIMANAGER_BUSINESS_BASE_URL",
        "AIMANAGER_EMPLOYEE_VIRTUAL_KEY",
    ]
    assert bundle["checks"][0]["evidence"]["result_count"] == 2
    assert "AIMANAGER_WECOM_WEBHOOK_URL" in json.dumps(bundle, ensure_ascii=False)


def test_production_readiness_runs_work_context_enforcement_smoke_and_redacts_employee_key() -> None:
    calls: list[dict[str, object]] = []

    def runner(**kwargs):
        calls.append(kwargs)
        return [
            _work_context_result(detail="blocked with employee key sk-employee-redaction-value"),
            _work_context_result(path="/v1/images/generations", model="ycapi-image-1"),
        ]

    bundle = collect_production_readiness(
        env=_work_context_env(),
        admin_boundary_runner=lambda **kwargs: [
            _script_result(status="PASS", detail="business/admin edge checks passed")
        ],
        live_ycapi_runner=lambda **kwargs: _script_result(status="PASS", detail="ycapi /models returned 3 models"),
        wecom_router=lambda **kwargs: _script_result(status="BLOCKED", detail="missing webhook"),
        finance_runner=lambda **kwargs: CheckResult(
            id="AC-12-13-FINANCE",
            name="finance_export_reconciliation",
            status="PASS",
            detail="finance files exported",
            evidence={"output_files": ["aimanager_usage_daily.csv"]},
        ),
        work_context_runner=runner,
    )

    ac20 = next(check for check in bundle["checks"] if check["id"] == "AC-20")
    serialized = json.dumps(bundle, ensure_ascii=False)
    assert ac20["status"] == "PASS"
    assert calls[0]["base_url"] == "https://aimanager.example.com"
    assert calls[0]["employee_key"] == "sk-employee-redaction-value"
    assert calls[0]["request_marker"] == "production-readiness"
    assert "sk-employee-redaction-value" not in serialized
    assert "DO_NOT_ECHO" not in serialized
    assert "[redacted:AIMANAGER_EMPLOYEE_VIRTUAL_KEY]" in serialized
    assert ac20["evidence"]["result_count"] == 2
    assert ac20["evidence"]["pass_count"] == 2
    assert ac20["evidence"]["results"][0]["path"] == "/v1/chat/completions"


def test_production_readiness_work_context_evidence_includes_valid_roundtrip_metadata() -> None:
    calls: list[dict[str, object]] = []

    def runner(**kwargs):
        calls.append(kwargs)
        return [
            _work_context_result(path="/v1/chat/completions"),
            _work_context_result(path="/v1/images/generations", model="ycapi-image-1"),
            _work_context_result(
                path="/v1/chat/completions",
                detail="valid work context chat roundtrip passed",
                status_code=200,
                policy_code=None,
                check_type="valid_context_roundtrip",
                request_id="chat-call-id",
                usage_present=True,
                work_context_present=True,
            ),
            _work_context_result(
                path="/v1/images/generations",
                model="ycapi-image-1",
                detail="valid work context image roundtrip passed",
                status_code=200,
                policy_code=None,
                check_type="valid_context_roundtrip",
                request_id="image-call-id",
                image_result_count=1,
                work_context_present=True,
            ),
        ]

    bundle = collect_production_readiness(
        env=_work_context_env(),
        admin_boundary_runner=lambda **kwargs: [
            _script_result(status="PASS", detail="business/admin edge checks passed")
        ],
        live_ycapi_runner=lambda **kwargs: _script_result(status="PASS", detail="ycapi ok"),
        wecom_router=lambda **kwargs: _script_result(status="BLOCKED", detail="missing webhook"),
        finance_runner=lambda **kwargs: CheckResult(
            id="AC-12-13-FINANCE",
            name="finance_export_reconciliation",
            status="BLOCKED",
            detail="missing files",
            evidence={},
        ),
        work_context_runner=runner,
    )

    ac20 = next(check for check in bundle["checks"] if check["id"] == "AC-20")
    serialized = json.dumps(bundle, ensure_ascii=False)
    assert calls[0]["run_valid_context_roundtrip"] is True
    assert ac20["status"] == "PASS"
    assert ac20["evidence"]["result_count"] == 4
    assert ac20["evidence"]["pass_count"] == 4
    valid_results = [
        result
        for result in ac20["evidence"]["results"]
        if result["check_type"] == "valid_context_roundtrip"
    ]
    assert valid_results == [
        {
            "check_type": "valid_context_roundtrip",
            "method": "POST",
            "path": "/v1/chat/completions",
            "model": "gemini-2.5-flash",
            "status": "PASS",
            "status_code": 200,
            "policy_code": None,
            "request_id": "chat-call-id",
            "usage_present": True,
            "image_result_count": 0,
            "work_context_present": True,
            "detail": "valid work context chat roundtrip passed",
        },
        {
            "check_type": "valid_context_roundtrip",
            "method": "POST",
            "path": "/v1/images/generations",
            "model": "ycapi-image-1",
            "status": "PASS",
            "status_code": 200,
            "policy_code": None,
            "request_id": "image-call-id",
            "usage_present": False,
            "image_result_count": 1,
            "work_context_present": True,
            "detail": "valid work context image roundtrip passed",
        },
    ]
    assert "sk-employee-redaction-value" not in serialized
    assert "prompt" not in serialized.lower()
    assert "assistant" not in serialized.lower()


def test_production_readiness_live_ycapi_evidence_includes_inference_roundtrip_metadata() -> None:
    calls: list[dict[str, object]] = []

    def live_ycapi_runner(**kwargs):
        calls.append(kwargs)
        return _script_result(
            status="PASS",
            detail="ycapi /models, chat, and image roundtrip passed",
            status_code=200,
            model_count=3,
            observed_models=("gemini-2.5-flash", "deepseek-chat", "ycapi-image-1"),
            inference_checked=True,
            chat_status_code=200,
            image_status_code=200,
            chat_usage_present=True,
            image_result_count=1,
            roundtrip_request_ids=("aimanager-live-ycapi-smoke-chat", "aimanager-live-ycapi-smoke-image"),
        )

    bundle = collect_production_readiness(
        env={"YCAPI_API_TOKEN": "ycapi-redaction-value"},
        admin_boundary_runner=lambda **kwargs: [
            _script_result(status="PASS", detail="business/admin edge checks passed")
        ],
        live_ycapi_runner=live_ycapi_runner,
        wecom_router=lambda **kwargs: _script_result(status="BLOCKED", detail="missing webhook"),
        finance_runner=lambda **kwargs: CheckResult(
            id="AC-12-13-FINANCE",
            name="finance_export_reconciliation",
            status="BLOCKED",
            detail="missing files",
            evidence={},
        ),
    )

    ac19 = next(check for check in bundle["checks"] if check["id"] == "AC-19")
    assert calls[0]["run_inference_roundtrip"] is True
    assert ac19["status"] == "PASS"
    assert ac19["evidence"]["inference_checked"] is True
    assert ac19["evidence"]["chat_usage_present"] is True
    assert ac19["evidence"]["image_result_count"] == 1
    assert ac19["evidence"]["roundtrip_request_ids"] == [
        "aimanager-live-ycapi-smoke-chat",
        "aimanager-live-ycapi-smoke-image",
    ]
    assert "ycapi-redaction-value" not in json.dumps(bundle, ensure_ascii=False)


def test_production_readiness_fails_when_work_context_smoke_reaches_provider() -> None:
    bundle = collect_production_readiness(
        env=_work_context_env(),
        admin_boundary_runner=lambda **kwargs: [
            _script_result(status="PASS", detail="business/admin edge checks passed")
        ],
        live_ycapi_runner=lambda **kwargs: _script_result(status="PASS", detail="ycapi /models returned 3 models"),
        wecom_router=lambda **kwargs: _script_result(status="BLOCKED", detail="missing webhook"),
        finance_runner=lambda **kwargs: CheckResult(
            id="AC-12-13-FINANCE",
            name="finance_export_reconciliation",
            status="PASS",
            detail="finance files exported",
            evidence={"output_files": ["aimanager_usage_daily.csv"]},
        ),
        work_context_runner=lambda **kwargs: [
            _work_context_result(
                passed=False,
                detail="expected 400, got 200",
                status_code=200,
                policy_code=None,
            )
        ],
    )

    ac20 = next(check for check in bundle["checks"] if check["id"] == "AC-20")
    assert bundle["status"] == "FAIL"
    assert ac20["status"] == "FAIL"
    assert "expected 400, got 200" in ac20["detail"]


def test_production_readiness_fails_closed_when_work_context_smoke_raises() -> None:
    def runner(**_: object):
        raise OSError("network unavailable")

    bundle = collect_production_readiness(
        env=_work_context_env(),
        admin_boundary_runner=lambda **kwargs: [
            _script_result(status="PASS", detail="business/admin edge checks passed")
        ],
        live_ycapi_runner=lambda **kwargs: _script_result(status="PASS", detail="ycapi /models returned 3 models"),
        wecom_router=lambda **kwargs: _script_result(status="BLOCKED", detail="missing webhook"),
        finance_runner=lambda **kwargs: CheckResult(
            id="AC-12-13-FINANCE",
            name="finance_export_reconciliation",
            status="PASS",
            detail="finance files exported",
            evidence={"output_files": ["aimanager_usage_daily.csv"]},
        ),
        work_context_runner=runner,
    )

    ac20 = next(check for check in bundle["checks"] if check["id"] == "AC-20")
    assert bundle["status"] == "FAIL"
    assert ac20["status"] == "FAIL"
    assert "OSError" in ac20["detail"]


def test_production_readiness_blocks_when_work_context_smoke_returns_no_cases() -> None:
    bundle = collect_production_readiness(
        env=_work_context_env(),
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
        work_context_runner=lambda **kwargs: [],
    )

    ac20 = next(check for check in bundle["checks"] if check["id"] == "AC-20")
    assert bundle["status"] == "BLOCKED"
    assert ac20["status"] == "BLOCKED"
    assert ac20["evidence"]["result_count"] == 0


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
    inventory_file.write_text(
        json.dumps(
            {
                "exported_at": "2026-07-01T10:00:00+08:00",
                "export_source": "litellm-production-verification-token-table",
                "export_scope": "all_virtual_keys",
                "exported_by": "security-ops",
                "expected_total_key_count": 0,
                "keys": [],
            }
        ),
        encoding="utf-8",
    )
    policy_file.write_text(json.dumps(_valid_policy_attestation()), encoding="utf-8")
    report_file.write_text(json.dumps(_observability_report_with_alert()), encoding="utf-8")

    bundle = collect_production_readiness(
        generated_at=GENERATED_AT,
        env={
            "AIMANAGER_KEY_INVENTORY_FILE": str(inventory_file),
            "AIMANAGER_PRODUCTION_POLICY_ATTESTATION_FILE": str(policy_file),
            "AIMANAGER_OBSERVABILITY_REPORT_FILE": str(report_file),
            "AIMANAGER_WECOM_WEBHOOK_URL": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=redaction",
            **_work_context_env(),
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
        work_context_runner=lambda **kwargs: _passing_work_context_results(),
    )

    key_inventory = next(check for check in bundle["checks"] if check["id"] == "AC-08-KEY-INVENTORY")
    assert bundle["status"] == "BLOCKED"
    assert key_inventory["status"] == "BLOCKED"
    assert key_inventory["evidence"]["active_key_count"] == 0


def test_production_readiness_passes_with_governed_key_inventory(tmp_path) -> None:
    inventory_file = tmp_path / "key-inventory.json"
    policy_file = tmp_path / "production-policy.json"
    report_file = tmp_path / "observability.json"
    employee_policy_file, roster_file, acknowledgment_file = _employee_acknowledgment_files(tmp_path)
    inventory_file.write_text(json.dumps(_valid_key_inventory()), encoding="utf-8")
    policy_file.write_text(json.dumps(_valid_policy_attestation()), encoding="utf-8")
    report_file.write_text(json.dumps(_observability_report_with_alert()), encoding="utf-8")

    bundle = collect_production_readiness(
        generated_at=GENERATED_AT,
        env={
            "AIMANAGER_KEY_INVENTORY_FILE": str(inventory_file),
            "AIMANAGER_PRODUCTION_POLICY_ATTESTATION_FILE": str(policy_file),
            "AIMANAGER_OBSERVABILITY_REPORT_FILE": str(report_file),
            "AIMANAGER_WECOM_WEBHOOK_URL": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=redaction",
            "AIMANAGER_EMPLOYEE_MONITORING_POLICY_FILE": str(employee_policy_file),
            "AIMANAGER_EMPLOYEE_ROSTER_FILE": str(roster_file),
            "AIMANAGER_EMPLOYEE_ACKNOWLEDGMENT_FILE": str(acknowledgment_file),
            **_work_context_env(),
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
        work_context_runner=lambda **kwargs: _passing_work_context_results(),
    )

    key_inventory = next(check for check in bundle["checks"] if check["id"] == "AC-08-KEY-INVENTORY")
    assert bundle["status"] == "PASS"
    assert key_inventory["status"] == "PASS"
    assert key_inventory["evidence"]["active_key_count"] == 1
    assert key_inventory["evidence"]["violation_count"] == 0
    assert key_inventory["evidence"]["export_scope"] == "all_virtual_keys"
    assert key_inventory["evidence"]["employee_acknowledgment"]["acknowledged_employee_count"] == 2


def test_production_readiness_blocks_key_inventory_without_employee_acknowledgment_evidence(tmp_path) -> None:
    inventory_file = tmp_path / "key-inventory.json"
    policy_file = tmp_path / "production-policy.json"
    report_file = tmp_path / "observability.json"
    inventory_file.write_text(json.dumps(_valid_key_inventory()), encoding="utf-8")
    policy_file.write_text(json.dumps(_valid_policy_attestation()), encoding="utf-8")
    report_file.write_text(json.dumps(_observability_report_with_alert()), encoding="utf-8")

    bundle = collect_production_readiness(
        generated_at=GENERATED_AT,
        env={
            "AIMANAGER_KEY_INVENTORY_FILE": str(inventory_file),
            "AIMANAGER_PRODUCTION_POLICY_ATTESTATION_FILE": str(policy_file),
            "AIMANAGER_OBSERVABILITY_REPORT_FILE": str(report_file),
            "AIMANAGER_WECOM_WEBHOOK_URL": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=redaction",
            **_work_context_env(),
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
        work_context_runner=lambda **kwargs: _passing_work_context_results(),
    )

    key_inventory = next(check for check in bundle["checks"] if check["id"] == "AC-08-KEY-INVENTORY")
    assert bundle["status"] == "BLOCKED"
    assert key_inventory["status"] == "BLOCKED"
    assert "employee roster and acknowledgment evidence" in key_inventory["detail"]
    assert "AIMANAGER_EMPLOYEE_ACKNOWLEDGMENT_FILE" in key_inventory["evidence"]["required_env"]


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
    monkeypatch.delenv("AIMANAGER_EMPLOYEE_VIRTUAL_KEY", raising=False)
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
        "AC-20",
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


def test_finance_evidence_fails_when_reconciliation_needs_review(tmp_path) -> None:
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
                    "amount": "999.99",
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
    assert finance_check["status"] == "FAIL"
    assert bundle["status"] == "FAIL"
    assert "needs_review" in finance_check["detail"]
    assert finance_check["evidence"]["reconciliation_needs_review_count"] == 1


def test_production_policy_attestation_passes_with_required_manual_checks(tmp_path) -> None:
    policy_file = tmp_path / "production_policy_attestation.json"
    report_file = tmp_path / "observability.json"
    inventory_file = tmp_path / "key-inventory.json"
    employee_policy_file, roster_file, acknowledgment_file = _employee_acknowledgment_files(tmp_path)
    policy_file.write_text(json.dumps(_valid_policy_attestation()), encoding="utf-8")
    report_file.write_text(json.dumps(_observability_report_with_alert()), encoding="utf-8")
    inventory_file.write_text(json.dumps(_valid_key_inventory()), encoding="utf-8")

    bundle = collect_production_readiness(
        generated_at=GENERATED_AT,
        env={
            "AIMANAGER_KEY_INVENTORY_FILE": str(inventory_file),
            "AIMANAGER_PRODUCTION_POLICY_ATTESTATION_FILE": str(policy_file),
            "AIMANAGER_OBSERVABILITY_REPORT_FILE": str(report_file),
            "AIMANAGER_WECOM_WEBHOOK_URL": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=redaction",
            "AIMANAGER_EMPLOYEE_MONITORING_POLICY_FILE": str(employee_policy_file),
            "AIMANAGER_EMPLOYEE_ROSTER_FILE": str(roster_file),
            "AIMANAGER_EMPLOYEE_ACKNOWLEDGMENT_FILE": str(acknowledgment_file),
            **_work_context_env(),
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
        work_context_runner=lambda **kwargs: _passing_work_context_results(),
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


def _script_result(*, status: str, detail: str, **overrides: object):
    fields = {
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
    }
    fields.update(overrides)
    return type(
        "ScriptResult",
        (),
        fields,
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


def _work_context_env() -> dict[str, str]:
    return {
        "AIMANAGER_BUSINESS_BASE_URL": "https://aimanager.example.com",
        "AIMANAGER_EMPLOYEE_VIRTUAL_KEY": "sk-employee-redaction-value",
    }


def _passing_work_context_results() -> list[WorkContextSmokeResult]:
    return [
        _work_context_result(path="/v1/chat/completions"),
        _work_context_result(path="/v1/images/generations", model="ycapi-image-1"),
    ]


def _work_context_result(
    *,
    path: str = "/v1/chat/completions",
    model: str = "gemini-2.5-flash",
    passed: bool = True,
    detail: str = "missing work context rejected before provider dispatch",
    status_code: int | None = 400,
    policy_code: str | None = "aimanager_work_context_invalid",
    check_type: str = "missing_context_block",
    request_id: str | None = None,
    usage_present: bool = False,
    image_result_count: int = 0,
    work_context_present: bool = False,
) -> WorkContextSmokeResult:
    return WorkContextSmokeResult(
        case=WorkContextSmokeCase(
            method="POST",
            path=path,
            model=model,
            prompt="DO_NOT_ECHO_WORK_CONTEXT_SMOKE_TEST",
            check_type=check_type,  # type: ignore[arg-type]
        ),
        passed=passed,
        detail=detail,
        status_code=status_code,
        policy_code=policy_code,
        request_id=request_id,
        usage_present=usage_present,
        image_result_count=image_result_count,
        work_context_present=work_context_present,
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
        "export_source": "litellm-production-verification-token-table",
        "export_scope": "all_virtual_keys",
        "exported_by": "security-ops",
        "expected_total_key_count": 1,
        "keys": [
            {
                "key_alias": "market-campaign-key",
                "blocked": False,
                "user_id": "u_market_1",
                "team_id": "team-marketing",
                "models": ["gemini-2.5-flash"],
                "max_budget": 100,
                "rpm_limit": 60,
                "tpm_limit": 120000,
                "duration": "30d",
                "metadata": {
                    "owner": "alice",
                    "department_id": "dept_market",
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


def _employee_acknowledgment_files(tmp_path) -> tuple:
    policy_file = tmp_path / "employee-monitoring-policy.json"
    roster_file = tmp_path / "employee-roster.json"
    acknowledgment_file = tmp_path / "employee-acknowledgments.json"
    policy_file.write_text(
        json.dumps(
            {
                "policy_id": "aimanager-employee-monitoring-v1",
                "version": "2026-06",
                "title": "AiManager employee monitoring notice and permission boundary",
                "published_at": "2026-06-30",
                "effective_at": "2026-07-01",
                "owner": "ai-platform",
                "notice_url": "https://intranet.internal/policies/aimanager-employee-monitoring-v1",
                "notice_channels": ["wecom", "employee-handbook"],
                "monitored_metadata_fields": [
                    "request_id",
                    "timestamp",
                    "employee_id",
                    "department_id",
                    "key_alias",
                    "scenario_l1",
                    "cost_center_id",
                    "spend",
                    "ip_hash",
                    "user_agent_hash",
                ],
                "prohibited_monitoring_fields": [
                    "prompt_text",
                    "response_text",
                    "raw_ip",
                    "raw_user_agent",
                    "customer_content",
                ],
                "retention_days": 90,
                "rules": [
                    {
                        "rule_id": "off-hours-usage-v1",
                        "type": "off_hours_usage",
                        "enabled": True,
                        "severity": "warning",
                        "timezone": "Asia/Shanghai",
                        "workday_start": "09:00",
                        "workday_end": "18:30",
                        "threshold_request_count": 20,
                        "threshold_spend": "50",
                        "lookback_hours": 24,
                        "requires_exception_ticket": True,
                    },
                    {
                        "rule_id": "key-sharing-v1",
                        "type": "key_sharing",
                        "enabled": True,
                        "severity": "high",
                        "lookback_hours": 24,
                        "distinct_ip_hash_threshold": 3,
                        "distinct_user_agent_hash_threshold": 3,
                        "requires_disposition": True,
                    },
                ],
                "permission_boundary": {
                    "allowed_review_roles": ["ai_platform_admin", "security", "audit", "hr", "legal"],
                    "prohibited_roles": ["direct_manager"],
                    "raw_prompt_access": "prohibited",
                    "customer_content_access": "prohibited",
                    "requires_hr_or_legal_for_disciplinary_action": True,
                    "employee_appeal_channel": "wecom://ai-compliance-helpdesk",
                },
            }
        ),
        encoding="utf-8",
    )
    roster_file.write_text(
        json.dumps(
            [
                {"employee_id": "u_market_1", "department_id": "dept_market", "status": "active"},
                {"employee_id": "u_sales_1", "department_id": "dept_sales", "status": "active"},
            ]
        ),
        encoding="utf-8",
    )
    acknowledgment_file.write_text(
        json.dumps(
            [
                {
                    "employee_id": "u_market_1",
                    "notice_version": "2026-06",
                    "acknowledged_at": "2026-06-30T10:00:00+08:00",
                    "understood_purpose": "true",
                    "understood_scope": "true",
                    "understood_appeal": "true",
                    "understood_no_raw_content": "true",
                },
                {
                    "employee_id": "u_sales_1",
                    "notice_version": "2026-06",
                    "acknowledged_at": "2026-06-30T10:05:00+08:00",
                    "understood_purpose": "true",
                    "understood_scope": "true",
                    "understood_appeal": "true",
                    "understood_no_raw_content": "true",
                },
            ]
        ),
        encoding="utf-8",
    )
    return policy_file, roster_file, acknowledgment_file


def test_module_exports_main() -> None:
    assert production_readiness_bundle.main is main

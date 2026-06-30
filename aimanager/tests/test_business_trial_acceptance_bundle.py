from __future__ import annotations

import json

import pytest

from aimanager.scripts.business_trial_acceptance_bundle import collect_business_trial_acceptance, main


def test_business_trial_acceptance_blocks_without_trial_or_monitoring_evidence() -> None:
    bundle = collect_business_trial_acceptance(
        env={},
        generated_at="2026-06-30T00:00:00Z",
        production_readiness_collector=lambda **kwargs: _production_bundle(ac19_status="PASS"),
    )

    assert bundle["status"] == "BLOCKED"
    assert bundle["generated_at"] == "2026-06-30T00:00:00Z"
    statuses = {check["id"]: check["status"] for check in bundle["checks"]}
    assert statuses["AC-23"] == "BLOCKED"
    assert statuses["AC-26"] == "BLOCKED"
    assert bundle["summary"]["BLOCKED"] == 2


@pytest.mark.parametrize(
    ("field_path", "bad_value", "expected_detail"),
    [
        (("operator", "uses_sdk"), True, "uses_sdk"),
        (("identity_injection", "ycapi_token_exposed"), True, "ycapi_token_exposed"),
        (("identity_injection", "employee_virtual_key_used"), False, "employee_virtual_key_used"),
        (("request_evidence", "spend"), 0, "spend"),
        (("request_evidence", "model"), "unapproved-model", "model"),
        (("compliance", "work_context_status"), "FAIL", "work_context_status"),
        (("live_ycapi"), False, "live_ycapi"),
    ],
)
def test_ac23_invalid_invariants_fail(tmp_path, field_path: tuple[str, ...] | str, bad_value: object, expected_detail: str) -> None:
    trial_file = tmp_path / "trial.json"
    monitoring_file = tmp_path / "monitoring.json"
    evidence = _valid_trial_evidence()
    _set_nested(evidence, field_path=field_path, value=bad_value)
    trial_file.write_text(json.dumps(evidence), encoding="utf-8")
    monitoring_file.write_text(json.dumps(_monitoring_result("PASS")), encoding="utf-8")

    bundle = collect_business_trial_acceptance(
        env={
            "AIMANAGER_LIGHTWEIGHT_TRIAL_EVIDENCE_FILE": str(trial_file),
            "AIMANAGER_EMPLOYEE_MONITORING_RESULT_FILE": str(monitoring_file),
        },
        production_readiness_collector=lambda **kwargs: _production_bundle(ac19_status="PASS"),
    )

    ac23 = next(check for check in bundle["checks"] if check["id"] == "AC-23")
    assert ac23["status"] == "FAIL"
    assert expected_detail in ac23["detail"]
    assert bundle["status"] == "FAIL"


def test_ac23_valid_trial_requires_same_run_live_ycapi_pass(tmp_path) -> None:
    trial_file = tmp_path / "trial.json"
    monitoring_file = tmp_path / "monitoring.json"
    trial_file.write_text(json.dumps(_valid_trial_evidence()), encoding="utf-8")
    monitoring_file.write_text(json.dumps(_monitoring_result("PASS")), encoding="utf-8")
    env = {
        "AIMANAGER_LIGHTWEIGHT_TRIAL_EVIDENCE_FILE": str(trial_file),
        "AIMANAGER_EMPLOYEE_MONITORING_RESULT_FILE": str(monitoring_file),
    }

    pass_bundle = collect_business_trial_acceptance(
        env=env,
        production_readiness_collector=lambda **kwargs: _production_bundle(ac19_status="PASS"),
    )
    blocked_bundle = collect_business_trial_acceptance(
        env=env,
        production_readiness_collector=lambda **kwargs: _production_bundle(ac19_status="BLOCKED"),
    )

    assert next(check for check in pass_bundle["checks"] if check["id"] == "AC-23")["status"] == "PASS"
    blocked_ac23 = next(check for check in blocked_bundle["checks"] if check["id"] == "AC-23")
    assert blocked_ac23["status"] == "BLOCKED"
    assert "AC-19" in blocked_ac23["detail"]
    assert blocked_bundle["status"] == "BLOCKED"


def test_business_trial_acceptance_redacts_env_and_file_only_secret_values(tmp_path) -> None:
    trial_file = tmp_path / "trial.json"
    monitoring_file = tmp_path / "monitoring.json"
    evidence = _valid_trial_evidence()
    evidence["attestation"]["notes"] = "observer saw file-only key sk-file-only-secret and bearer Bearer file-only-token"
    evidence["identity_injection"]["employee_virtual_key_alias"] = "sk-env-employee-secret"
    trial_file.write_text(json.dumps(evidence), encoding="utf-8")
    monitoring_file.write_text(
        json.dumps({**_monitoring_result("PASS"), "detail": "policy ok with sk-file-only-monitoring-secret"}),
        encoding="utf-8",
    )

    bundle = collect_business_trial_acceptance(
        env={
            "YCAPI_API_TOKEN": "ycapi-env-secret",
            "AIMANAGER_WECOM_WEBHOOK_URL": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=wecom-env-secret",
            "AIMANAGER_EMPLOYEE_VIRTUAL_KEY": "sk-env-employee-secret",
            "AIMANAGER_LIGHTWEIGHT_TRIAL_EVIDENCE_FILE": str(trial_file),
            "AIMANAGER_EMPLOYEE_MONITORING_RESULT_FILE": str(monitoring_file),
        },
        production_readiness_collector=lambda **kwargs: _production_bundle(
            ac19_status="PASS",
            ac19_detail="live token ycapi-env-secret",
        ),
    )

    payload = json.dumps(bundle, ensure_ascii=False)
    assert "ycapi-env-secret" not in payload
    assert "wecom-env-secret" not in payload
    assert "sk-env-employee-secret" not in payload
    assert "sk-file-only-secret" not in payload
    assert "file-only-token" not in payload
    assert "sk-file-only-monitoring-secret" not in payload
    assert "[redacted:YCAPI_API_TOKEN]" in payload
    assert "[redacted:secret-like]" in payload


def test_ac23_rejects_token_like_fields_without_echoing_secret(tmp_path) -> None:
    trial_file = tmp_path / "trial.json"
    monitoring_file = tmp_path / "monitoring.json"
    evidence = _valid_trial_evidence()
    evidence["authorization"] = "Bearer should-not-appear"
    trial_file.write_text(json.dumps(evidence), encoding="utf-8")
    monitoring_file.write_text(json.dumps(_monitoring_result("PASS")), encoding="utf-8")

    bundle = collect_business_trial_acceptance(
        env={
            "AIMANAGER_LIGHTWEIGHT_TRIAL_EVIDENCE_FILE": str(trial_file),
            "AIMANAGER_EMPLOYEE_MONITORING_RESULT_FILE": str(monitoring_file),
        },
        production_readiness_collector=lambda **kwargs: _production_bundle(ac19_status="PASS"),
    )

    ac23 = next(check for check in bundle["checks"] if check["id"] == "AC-23")
    payload = json.dumps(bundle, ensure_ascii=False)
    assert ac23["status"] == "FAIL"
    assert "forbidden" in ac23["detail"]
    assert "should-not-appear" not in payload
    assert "Bearer" not in payload


def test_ac23_missing_file_is_blocked_but_malformed_json_fails(tmp_path) -> None:
    malformed_file = tmp_path / "trial.json"
    monitoring_file = tmp_path / "monitoring.json"
    malformed_file.write_text("{not-json", encoding="utf-8")
    monitoring_file.write_text(json.dumps(_monitoring_result("PASS")), encoding="utf-8")

    missing_bundle = collect_business_trial_acceptance(
        env={"AIMANAGER_LIGHTWEIGHT_TRIAL_EVIDENCE_FILE": str(tmp_path / "missing.json")},
        production_readiness_collector=lambda **kwargs: _production_bundle(ac19_status="PASS"),
    )
    malformed_bundle = collect_business_trial_acceptance(
        env={
            "AIMANAGER_LIGHTWEIGHT_TRIAL_EVIDENCE_FILE": str(malformed_file),
            "AIMANAGER_EMPLOYEE_MONITORING_RESULT_FILE": str(monitoring_file),
        },
        production_readiness_collector=lambda **kwargs: _production_bundle(ac19_status="PASS"),
    )

    assert next(check for check in missing_bundle["checks"] if check["id"] == "AC-23")["status"] == "BLOCKED"
    assert next(check for check in malformed_bundle["checks"] if check["id"] == "AC-23")["status"] == "FAIL"


def test_ac26_result_status_propagates_to_overall(tmp_path) -> None:
    trial_file = tmp_path / "trial.json"
    monitoring_file = tmp_path / "monitoring.json"
    trial_file.write_text(json.dumps(_valid_trial_evidence()), encoding="utf-8")
    monitoring_file.write_text(json.dumps(_monitoring_result("FAIL", detail="missing legal approval")), encoding="utf-8")

    bundle = collect_business_trial_acceptance(
        env={
            "AIMANAGER_LIGHTWEIGHT_TRIAL_EVIDENCE_FILE": str(trial_file),
            "AIMANAGER_EMPLOYEE_MONITORING_RESULT_FILE": str(monitoring_file),
        },
        production_readiness_collector=lambda **kwargs: _production_bundle(ac19_status="PASS"),
    )

    ac26 = next(check for check in bundle["checks"] if check["id"] == "AC-26")
    assert ac26["status"] == "FAIL"
    assert "missing legal approval" in ac26["detail"]
    assert bundle["status"] == "FAIL"


def test_business_trial_acceptance_cli_writes_json_and_returns_blocked(monkeypatch, tmp_path, capsys) -> None:
    output_file = tmp_path / "business-trial.json"
    for name in (
        "AIMANAGER_LIGHTWEIGHT_TRIAL_EVIDENCE_FILE",
        "AIMANAGER_EMPLOYEE_MONITORING_RESULT_FILE",
        "AIMANAGER_EMPLOYEE_MONITORING_POLICY_FILE",
        "AIMANAGER_EMPLOYEE_ROSTER_FILE",
        "AIMANAGER_EMPLOYEE_ACKNOWLEDGMENT_FILE",
        "YCAPI_API_TOKEN",
        "AIMANAGER_WECOM_WEBHOOK_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    exit_code = main(["--output-json-file", str(output_file)])

    output = capsys.readouterr().out
    assert exit_code == 2
    assert "BLOCKED business trial acceptance bundle" in output
    bundle = json.loads(output_file.read_text(encoding="utf-8"))
    assert bundle["status"] == "BLOCKED"
    assert {"AC-23", "AC-26"}.issubset({check["id"] for check in bundle["checks"]})


def _production_bundle(*, ac19_status: str, ac19_detail: str = "live ycapi preflight completed") -> dict[str, object]:
    return {
        "status": "PASS" if ac19_status == "PASS" else ac19_status,
        "generated_at": "2026-06-30T00:00:00Z",
        "summary": {"PASS": 4 if ac19_status == "PASS" else 3, "FAIL": 1 if ac19_status == "FAIL" else 0, "BLOCKED": 1 if ac19_status == "BLOCKED" else 0},
        "checks": [
            {"id": "AC-15", "name": "production_admin_boundary", "status": "PASS", "detail": "admin boundary ok", "evidence": {}},
            {"id": "AC-19", "name": "live_ycapi_preflight", "status": ac19_status, "detail": ac19_detail, "evidence": {"observed_models": ["gemini-2.5-flash"]}},
            {"id": "AC-16-WECOM", "name": "wecom_alert_routing", "status": "PASS", "detail": "WeCom alert delivered", "evidence": {"delivered_count": 1}},
            {"id": "AC-12-13-FINANCE", "name": "finance_export_reconciliation", "status": "PASS", "detail": "finance ok", "evidence": {"aimanager_billable_row_count": 1, "ycapi_billable_row_count": 1}},
        ],
    }


def _valid_trial_evidence() -> dict[str, object]:
    return {
        "trial_id": "trial-20260630-001",
        "ac": "AC-23",
        "entry_channel": "lightweight_web",
        "operator": {
            "employee_id": "emp_market_001",
            "department_id": "dept_marketing",
            "role": "marketing",
            "uses_sdk": False,
        },
        "identity_injection": {
            "source": "sso",
            "employee_virtual_key_used": True,
            "employee_virtual_key_alias": "market-trial-key",
            "ycapi_token_exposed": False,
        },
        "work_context": {
            "scenario_l1": "marketing",
            "scenario_l2": "campaign_brief",
            "project_id": "proj_launch",
            "cost_center_id": "cc_growth",
        },
        "request_evidence": {
            "request_id": "req-trial-001",
            "model": "gemini-2.5-flash",
            "endpoint": "/v1/chat/completions",
            "http_status": 200,
            "spend": 0.12,
            "currency": "CNY",
            "pricing_version": "m2-2026-06",
        },
        "timing": {
            "started_at": "2026-06-30T10:00:00+08:00",
            "completed_at": "2026-06-30T10:04:30+08:00",
        },
        "compliance": {
            "work_context_status": "PASS",
            "brand_safety_confirmed": True,
            "no_secret_echo": True,
            "html_escaped": True,
        },
        "live_ycapi": True,
        "attestation": {
            "observer": "ops-manager",
            "captured_at": "2026-06-30T10:05:00+08:00",
        },
    }


def _monitoring_result(status: str, *, detail: str = "employee monitoring evidence validated") -> dict[str, object]:
    return {
        "status": status,
        "detail": detail,
        "policy_id": "aimanager-employee-monitoring",
        "policy_version": "2026-06",
        "summary": {
            "active_employee_count": 2,
            "acknowledged_employee_count": 2,
            "missing_acknowledgment_count": 0,
            "rule_count": 2,
            "retention_days": 90,
        },
    }


def _set_nested(payload: dict[str, object], *, field_path: tuple[str, ...] | str, value: object) -> None:
    if isinstance(field_path, str):
        payload[field_path] = value
        return
    target = payload
    for key in field_path[:-1]:
        next_value = target[key]
        assert isinstance(next_value, dict)
        target = next_value
    target[field_path[-1]] = value

from __future__ import annotations

import json

from aimanager.scripts.business_trial_acceptance_bundle import collect_business_trial_acceptance
from aimanager.scripts.capture_lightweight_trial_evidence import build_trial_evidence, main


def test_capture_trial_evidence_omits_prompt_response_and_secret_values(tmp_path, capsys) -> None:
    result_file = tmp_path / "lightweight-entry-result.json"
    output_file = tmp_path / "trial-evidence.json"
    monitoring_file = tmp_path / "monitoring.json"
    result_file.write_text(json.dumps(_successful_submission_result()), encoding="utf-8")
    monitoring_file.write_text(json.dumps(_monitoring_result()), encoding="utf-8")

    exit_code = main(
        [
            "--lightweight-entry-result-file",
            str(result_file),
            "--request-id",
            "req-trial-001",
            "--spend",
            "0.13",
            "--operator-role",
            "marketing",
            "--identity-source",
            "sso",
            "--employee-virtual-key-alias",
            "market-trial-key",
            "--started-at",
            "2026-06-30T10:00:00+08:00",
            "--completed-at",
            "2026-06-30T10:04:00+08:00",
            "--observer",
            "ops-manager",
            "--captured-at",
            "2026-06-30T10:05:00+08:00",
            "--live-ycapi-confirmed",
            "--brand-safety-confirmed",
            "--no-secret-echo-confirmed",
            "--html-escaped-confirmed",
            "--output-json-file",
            str(output_file),
        ]
    )

    stdout = capsys.readouterr().out
    assert exit_code == 0
    assert "PASS lightweight trial evidence" in stdout
    evidence = json.loads(output_file.read_text(encoding="utf-8"))
    assert evidence["trial_id"] == "trial-mk-2026-q3-launch-001-req-trial-001"
    assert evidence["operator"]["employee_id"] == "u_market_1"
    assert evidence["request_evidence"]["model"] == "gemini-2.5-flash"
    assert evidence["request_evidence"]["spend"] == "0.13"
    serialized = json.dumps(evidence, ensure_ascii=False)
    assert "Write a launch article" not in serialized
    assert "assistant raw draft" not in serialized
    assert "Bearer" not in serialized
    assert "sk-secret" not in serialized

    bundle = collect_business_trial_acceptance(
        env={
            "AIMANAGER_LIGHTWEIGHT_TRIAL_EVIDENCE_FILE": str(output_file),
            "AIMANAGER_EMPLOYEE_MONITORING_RESULT_FILE": str(monitoring_file),
        },
        generated_at="2026-06-30T10:05:00+08:00",
        production_readiness_collector=lambda **kwargs: _production_bundle(ac19_status="PASS"),
    )
    assert next(check for check in bundle["checks"] if check["id"] == "AC-23")["status"] == "PASS"


def test_capture_trial_evidence_accepts_customer_context_without_project(tmp_path, capsys) -> None:
    result_file = tmp_path / "lightweight-entry-result.json"
    output_file = tmp_path / "trial-evidence.json"
    payload = _successful_submission_result()
    metadata = payload["entry"]["request"]["body"]["metadata"]
    metadata.pop("project_id")
    metadata["customer_id"] = "cust_launch_q3"
    result_file.write_text(json.dumps(payload), encoding="utf-8")

    exit_code = main(
        [
            "--lightweight-entry-result-file",
            str(result_file),
            "--request-id",
            "req-trial-001",
            "--spend",
            "0.13",
            "--operator-role",
            "marketing",
            "--identity-source",
            "sso",
            "--employee-virtual-key-alias",
            "market-trial-key",
            "--started-at",
            "2026-06-30T10:00:00+08:00",
            "--completed-at",
            "2026-06-30T10:04:00+08:00",
            "--observer",
            "ops-manager",
            "--captured-at",
            "2026-06-30T10:05:00+08:00",
            "--live-ycapi-confirmed",
            "--brand-safety-confirmed",
            "--no-secret-echo-confirmed",
            "--html-escaped-confirmed",
            "--output-json-file",
            str(output_file),
        ]
    )

    assert exit_code == 0
    assert "PASS lightweight trial evidence" in capsys.readouterr().out
    evidence = json.loads(output_file.read_text(encoding="utf-8"))
    assert evidence["work_context"]["customer_id"] == "cust_launch_q3"
    assert evidence["work_context"]["project_id"] == ""


def test_capture_trial_evidence_blocks_when_submission_did_not_pass(tmp_path, capsys) -> None:
    result_file = tmp_path / "lightweight-entry-result.json"
    output_file = tmp_path / "trial-evidence.json"
    payload = _successful_submission_result()
    payload["status"] = "BLOCKED"
    payload["detail"] = "missing employee key"
    result_file.write_text(json.dumps(payload), encoding="utf-8")

    exit_code = main(
        [
            "--lightweight-entry-result-file",
            str(result_file),
            "--request-id",
            "req-trial-001",
            "--spend",
            "0.13",
            "--operator-role",
            "marketing",
            "--identity-source",
            "sso",
            "--employee-virtual-key-alias",
            "market-trial-key",
            "--started-at",
            "2026-06-30T10:00:00+08:00",
            "--completed-at",
            "2026-06-30T10:04:00+08:00",
            "--observer",
            "ops-manager",
            "--captured-at",
            "2026-06-30T10:05:00+08:00",
            "--live-ycapi-confirmed",
            "--brand-safety-confirmed",
            "--no-secret-echo-confirmed",
            "--html-escaped-confirmed",
            "--output-json-file",
            str(output_file),
        ]
    )

    assert exit_code == 2
    assert "BLOCKED lightweight trial evidence" in capsys.readouterr().out
    assert not output_file.exists()


def test_capture_trial_evidence_rejects_non_2xx_http_status(tmp_path, capsys) -> None:
    result_file = tmp_path / "lightweight-entry-result.json"
    output_file = tmp_path / "trial-evidence.json"
    payload = _successful_submission_result()
    payload["status_code"] = 500
    result_file.write_text(json.dumps(payload), encoding="utf-8")

    exit_code = main(
        [
            "--lightweight-entry-result-file",
            str(result_file),
            "--request-id",
            "req-trial-001",
            "--spend",
            "0.13",
            "--operator-role",
            "marketing",
            "--identity-source",
            "sso",
            "--employee-virtual-key-alias",
            "market-trial-key",
            "--started-at",
            "2026-06-30T10:00:00+08:00",
            "--completed-at",
            "2026-06-30T10:04:00+08:00",
            "--observer",
            "ops-manager",
            "--captured-at",
            "2026-06-30T10:05:00+08:00",
            "--live-ycapi-confirmed",
            "--brand-safety-confirmed",
            "--no-secret-echo-confirmed",
            "--html-escaped-confirmed",
            "--output-json-file",
            str(output_file),
        ]
    )

    assert exit_code == 1
    assert "FAIL lightweight trial evidence" in capsys.readouterr().out
    assert not output_file.exists()


def test_capture_trial_evidence_rejects_malformed_or_unsafe_result_without_echo(tmp_path, capsys) -> None:
    result_file = tmp_path / "lightweight-entry-result.json"
    output_file = tmp_path / "trial-evidence.json"
    payload = _successful_submission_result()
    payload["entry"]["request"]["headers"] = {"Authorization": "Bearer should-not-appear"}
    result_file.write_text(json.dumps(payload), encoding="utf-8")

    exit_code = main(
        [
            "--lightweight-entry-result-file",
            str(result_file),
            "--request-id",
            "req-trial-001",
            "--spend",
            "0.13",
            "--operator-role",
            "marketing",
            "--identity-source",
            "sso",
            "--employee-virtual-key-alias",
            "market-trial-key",
            "--started-at",
            "2026-06-30T10:00:00+08:00",
            "--completed-at",
            "2026-06-30T10:04:00+08:00",
            "--observer",
            "ops-manager",
            "--captured-at",
            "2026-06-30T10:05:00+08:00",
            "--live-ycapi-confirmed",
            "--brand-safety-confirmed",
            "--no-secret-echo-confirmed",
            "--html-escaped-confirmed",
            "--output-json-file",
            str(output_file),
        ]
    )

    stdout = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL lightweight trial evidence" in stdout
    assert "should-not-appear" not in stdout
    assert not output_file.exists()


def test_capture_trial_evidence_rejects_secret_echo_in_assistant_text_without_echo(tmp_path, capsys) -> None:
    result_file = tmp_path / "lightweight-entry-result.json"
    output_file = tmp_path / "trial-evidence.json"
    payload = _successful_submission_result()
    payload["assistant_text"] = "assistant text includes sk-secret-response-value"
    result_file.write_text(json.dumps(payload), encoding="utf-8")

    exit_code = main(
        [
            "--lightweight-entry-result-file",
            str(result_file),
            "--request-id",
            "req-trial-001",
            "--spend",
            "0.13",
            "--operator-role",
            "marketing",
            "--identity-source",
            "sso",
            "--employee-virtual-key-alias",
            "market-trial-key",
            "--started-at",
            "2026-06-30T10:00:00+08:00",
            "--completed-at",
            "2026-06-30T10:04:00+08:00",
            "--observer",
            "ops-manager",
            "--captured-at",
            "2026-06-30T10:05:00+08:00",
            "--live-ycapi-confirmed",
            "--brand-safety-confirmed",
            "--no-secret-echo-confirmed",
            "--html-escaped-confirmed",
            "--output-json-file",
            str(output_file),
        ]
    )

    stdout = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL lightweight trial evidence" in stdout
    assert "sk-secret-response-value" not in stdout
    assert not output_file.exists()


def test_capture_trial_evidence_rejects_secret_like_metadata_without_output(tmp_path, capsys) -> None:
    result_file = tmp_path / "lightweight-entry-result.json"
    output_file = tmp_path / "trial-evidence.json"
    payload = _successful_submission_result()
    payload["entry"]["request"]["body"]["metadata"]["project_id"] = "sk-secret-project-value"
    result_file.write_text(json.dumps(payload), encoding="utf-8")

    exit_code = main(
        [
            "--lightweight-entry-result-file",
            str(result_file),
            "--request-id",
            "req-trial-001",
            "--spend",
            "0.13",
            "--operator-role",
            "marketing",
            "--identity-source",
            "sso",
            "--employee-virtual-key-alias",
            "market-trial-key",
            "--started-at",
            "2026-06-30T10:00:00+08:00",
            "--completed-at",
            "2026-06-30T10:04:00+08:00",
            "--observer",
            "ops-manager",
            "--captured-at",
            "2026-06-30T10:05:00+08:00",
            "--live-ycapi-confirmed",
            "--brand-safety-confirmed",
            "--no-secret-echo-confirmed",
            "--html-escaped-confirmed",
            "--output-json-file",
            str(output_file),
        ]
    )

    stdout = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL lightweight trial evidence" in stdout
    assert "sk-secret-project-value" not in stdout
    assert not output_file.exists()


def test_capture_trial_evidence_rejects_secret_like_key_alias() -> None:
    result = build_trial_evidence(
        submission_result=_successful_submission_result(),
        request_id="req-trial-001",
        spend="0.13",
        operator_role="marketing",
        identity_source="sso",
        employee_virtual_key_alias="sk-secret-alias-value",
        started_at="2026-06-30T10:00:00+08:00",
        completed_at="2026-06-30T10:04:00+08:00",
        observer="ops-manager",
        captured_at="2026-06-30T10:05:00+08:00",
        live_ycapi_confirmed=True,
        brand_safety_confirmed=True,
        no_secret_echo_confirmed=True,
        html_escaped_confirmed=True,
    )

    assert result["status"] == "FAIL"
    assert "employee virtual key alias" in str(result["detail"])


def test_build_trial_evidence_requires_live_ycapi_and_confirmation_flags() -> None:
    result = build_trial_evidence(
        submission_result=_successful_submission_result(),
        request_id="req-trial-001",
        spend="0.13",
        operator_role="marketing",
        identity_source="sso",
        employee_virtual_key_alias="market-trial-key",
        started_at="2026-06-30T10:00:00+08:00",
        completed_at="2026-06-30T10:04:00+08:00",
        observer="ops-manager",
        captured_at="2026-06-30T10:05:00+08:00",
        live_ycapi_confirmed=False,
        brand_safety_confirmed=True,
        no_secret_echo_confirmed=True,
        html_escaped_confirmed=True,
    )

    assert result["status"] == "BLOCKED"
    assert "live ycapi" in str(result["detail"])


def test_build_trial_evidence_requires_validated_work_context_proof() -> None:
    missing_context_payload = _successful_submission_result()
    missing_context_payload["entry"].pop("work_context")
    failed_context_payload = _successful_submission_result()
    failed_context_payload["entry"]["work_context"]["status"] = "FAIL"

    for payload in (missing_context_payload, failed_context_payload):
        result = _build_trial_evidence(payload)

        assert result["status"] == "BLOCKED"
        assert "work context" in str(result["detail"])


def test_build_trial_evidence_rejects_invalid_preflight_proof_details() -> None:
    metadata_mode_payload = _successful_submission_result()
    metadata_mode_payload["entry"]["request"]["body"]["metadata"]["workflow_mode"] = "closure"
    normalized_mode_payload = _successful_submission_result()
    normalized_mode_payload["entry"]["work_context"]["normalized_context"]["workflow_mode"] = "closure"
    mismatched_id_payload = _successful_submission_result()
    mismatched_id_payload["entry"]["work_context"]["normalized_context"]["work_item_id"] = "other-work-item"

    for payload in (metadata_mode_payload, normalized_mode_payload, mismatched_id_payload):
        result = _build_trial_evidence(payload)

        assert result["status"] == "BLOCKED"
        assert "work context" in str(result["detail"])


def test_build_trial_evidence_accepts_casefolded_work_context_proof() -> None:
    payload = _successful_submission_result()
    metadata = payload["entry"]["request"]["body"]["metadata"]
    metadata["work_item_id"] = "MK-2026-Q3-LAUNCH-001"
    metadata["employee_id"] = "U_MARKET_1"
    metadata["department_id"] = "DEPT_MARKET"
    metadata["end_user_principal"] = "U_MARKET_1"
    metadata["scenario_l1"] = "MARKETING"
    metadata["scenario_l2"] = "WECHAT_ARTICLE"
    payload["entry"]["request"]["body"]["user"] = "U_MARKET_1"

    result = _build_trial_evidence(payload)

    assert result["status"] == "PASS"
    assert result["evidence"]["trial_id"] == "trial-MK-2026-Q3-LAUNCH-001-req-trial-001"
    assert result["evidence"]["operator"]["employee_id"] == "U_MARKET_1"


def _build_trial_evidence(submission_result: dict[str, object]) -> dict[str, object]:
    return build_trial_evidence(
        submission_result=submission_result,
        request_id="req-trial-001",
        spend="0.13",
        operator_role="marketing",
        identity_source="sso",
        employee_virtual_key_alias="market-trial-key",
        started_at="2026-06-30T10:00:00+08:00",
        completed_at="2026-06-30T10:04:00+08:00",
        observer="ops-manager",
        captured_at="2026-06-30T10:05:00+08:00",
        live_ycapi_confirmed=True,
        brand_safety_confirmed=True,
        no_secret_echo_confirmed=True,
        html_escaped_confirmed=True,
    )


def _successful_submission_result() -> dict[str, object]:
    return {
        "status": "PASS",
        "detail": "lightweight entry request succeeded through AiManager business gateway",
        "errors": [],
        "warnings": [],
        "checked_endpoint": "/v1/chat/completions",
        "status_code": 200,
        "assistant_text": "assistant raw draft without secret markers",
        "entry": {
            "status": "PASS",
            "detail": "lightweight entry is ready for AiManager governed chat submission",
            "errors": [],
            "warnings": [],
            "work_context": {
                "status": "PASS",
                "normalized_context": {
                    "workflow_mode": "preflight",
                    "work_item_id": "mk-2026-q3-launch-001",
                    "employee_id": "u_market_1",
                    "department_id": "dept_market",
                    "scenario_l1": "marketing",
                    "scenario_l2": "wechat_article",
                },
            },
            "request": {
                "method": "POST",
                "path": "/v1/chat/completions",
                "body": {
                    "model": "gemini-2.5-flash",
                    "messages": [{"role": "user", "content": "Write a launch article draft for internal review."}],
                    "max_tokens": 256,
                    "user": "u_market_1",
                    "metadata": {
                        "entry_type": "lightweight_non_sdk",
                        "work_item_id": "mk-2026-q3-launch-001",
                        "employee_id": "u_market_1",
                        "department_id": "dept_market",
                        "end_user_principal": "u_market_1",
                        "cost_center_id": "cc_marketing_growth",
                        "currency": "CNY",
                        "pricing_version": "m2-trial-v1",
                        "scenario_l1": "marketing",
                        "scenario_l2": "wechat_article",
                        "workflow_mode": "preflight",
                        "project_id": "proj_launch_q3",
                    },
                },
            },
        },
    }


def _monitoring_result() -> dict[str, object]:
    return {
        "status": "PASS",
        "detail": "employee monitoring evidence validated",
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


def _production_bundle(*, ac19_status: str) -> dict[str, object]:
    return {
        "status": "PASS" if ac19_status == "PASS" else ac19_status,
        "generated_at": "2026-06-30T00:00:00Z",
        "summary": {"PASS": 4 if ac19_status == "PASS" else 3, "FAIL": 0, "BLOCKED": 1 if ac19_status == "BLOCKED" else 0},
        "checks": [
            {"id": "AC-15", "name": "production_admin_boundary", "status": "PASS", "detail": "admin boundary ok", "evidence": {}},
            {"id": "AC-19", "name": "live_ycapi_preflight", "status": ac19_status, "detail": "live ycapi ok", "evidence": {}},
            {"id": "AC-16-WECOM", "name": "wecom_alert_routing", "status": "PASS", "detail": "WeCom alert delivered", "evidence": {}},
            {"id": "AC-12-13-FINANCE", "name": "finance_export_reconciliation", "status": "PASS", "detail": "finance ok", "evidence": {}},
        ],
    }

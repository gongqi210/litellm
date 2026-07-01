from __future__ import annotations

import json

from aimanager.scripts.generate_evidence_template_pack import collect_evidence_template_pack
from aimanager.scripts.validate_evidence_intake import validate_evidence_intake


def test_evidence_intake_blocks_when_no_env_pointed_files_are_configured() -> None:
    result = validate_evidence_intake(input_files=[], generated_at="2026-07-01T00:00:00Z")

    assert result["status"] == "BLOCKED"
    assert result["summary"] == {"PASS": 0, "FAIL": 0, "BLOCKED": 1, "files": 1}
    assert "no evidence files were configured" in str(result["markdown"])


def test_evidence_intake_blocks_unchanged_template_pack(tmp_path) -> None:
    launch_file = tmp_path / "launch-gap-plan.json"
    template_dir = tmp_path / "template-pack"
    launch_file.write_text(
        json.dumps(
            {
                "status": "BLOCKED",
                "gaps": [
                    _gap("AC-12-13-FINANCE", "finance", "finance"),
                    _gap("AC-23", "trial", "business_owner/market/ops"),
                    _gap("AC-POLICY", "policy", "general_manager/finance/security/legal"),
                ],
            }
        ),
        encoding="utf-8",
    )
    collect_evidence_template_pack(launch_gap_plan_file=launch_file, output_dir=template_dir)

    result = validate_evidence_intake(input_dir=template_dir, generated_at="2026-07-01T00:00:00Z")

    assert result["status"] == "BLOCKED"
    assert result["summary"]["BLOCKED"] >= 1
    details = json.dumps(result, ensure_ascii=False)
    assert "TEMPLATE_DO_NOT_SUBMIT" in details
    assert "replace-with-real-request-id" in details
    assert "templates/finance/aimanager-spend.template.csv" in details


def test_evidence_intake_fails_secret_like_and_raw_prompt_without_echoing_secret(tmp_path) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "trial.json").write_text(
        json.dumps(
            {
                "trial_id": "trial-1",
                "prompt": "raw prompt must not be accepted",
                "raw_prompt": "raw prompt field must not be accepted",
                "request": {"Authorization": "Bearer should-not-leak"},
            }
        ),
        encoding="utf-8",
    )

    result = validate_evidence_intake(input_dir=evidence_dir, generated_at="2026-07-01T00:00:00Z")

    serialized = json.dumps(result, ensure_ascii=False)
    assert result["status"] == "FAIL"
    assert "prompt" in serialized
    assert "raw_prompt" in serialized
    assert "should-not-leak" not in serialized
    assert "Bearer [redacted:secret-like-value]" in serialized


def test_evidence_intake_fails_bare_environment_secret_value_without_echoing_it(tmp_path) -> None:
    secret = "ycapi-live-7f3c9a2b8e1d4506"
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "owner-note.md").write_text(
        f"Operator recorded ycapi token {secret} while collecting evidence.\n",
        encoding="utf-8",
    )

    result = validate_evidence_intake(
        input_dir=evidence_dir,
        env={"YCAPI_API_TOKEN": secret},
        generated_at="2026-07-01T00:00:00Z",
    )

    serialized = json.dumps(result, ensure_ascii=False)
    assert result["status"] == "FAIL"
    assert secret not in serialized
    assert "YCAPI_API_TOKEN" in serialized


def test_evidence_intake_ignores_common_secret_named_env_values(tmp_path) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "owner-note.md").write_text(
        "Finance marked reconciliation as true after comparing non-zero totals.\n",
        encoding="utf-8",
    )

    result = validate_evidence_intake(
        input_dir=evidence_dir,
        env={"AIMANAGER_SECRET_FLAG": "true"},
        generated_at="2026-07-01T00:00:00Z",
    )

    assert result["status"] == "PASS"
    assert result["summary"] == {"PASS": 1, "FAIL": 0, "BLOCKED": 0, "files": 1}


def test_evidence_intake_allows_policy_boundaries_that_prohibit_raw_content_access(tmp_path) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "employee-monitoring-policy.json").write_text(
        json.dumps(
            {
                "permission_boundary": {
                    "raw_prompt_access": "prohibited",
                    "customer_content_access": "prohibited",
                }
            }
        ),
        encoding="utf-8",
    )

    result = validate_evidence_intake(input_dir=evidence_dir, generated_at="2026-07-01T00:00:00Z")

    assert result["status"] == "PASS"
    assert result["summary"] == {"PASS": 1, "FAIL": 0, "BLOCKED": 0, "files": 1}


def test_evidence_intake_fails_schema_invalid_ac23_trial_evidence(tmp_path) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "ac23-trial-evidence.json").write_text(
        json.dumps(
            {
                "trial_id": "trial-1",
                "ac": "AC-23",
                "entry_channel": "lightweight_web",
                "operator": {
                    "employee_id": "u_market_1",
                    "department_id": "dept_market",
                    "role": "marketing",
                    "uses_sdk": True,
                },
                "identity_injection": {
                    "source": "sso",
                    "employee_virtual_key_used": True,
                    "employee_virtual_key_alias": "marketing-key-alias",
                    "ycapi_token_exposed": False,
                },
                "work_context": {
                    "scenario_l1": "marketing",
                    "scenario_l2": "external_copy_review",
                    "project_id": "proj_campaign",
                    "customer_id": "",
                    "cost_center_id": "cc_market",
                },
                "request_evidence": {
                    "request_id": "req-1",
                    "model": "gemini-2.5-flash",
                    "endpoint": "/v1/chat/completions",
                    "http_status": 200,
                    "spend": "0",
                    "currency": "CNY",
                    "pricing_version": "m1",
                },
                "timing": {
                    "started_at": "2026-07-01T10:00:00+08:00",
                    "completed_at": "2026-07-01T10:00:00+08:00",
                },
                "compliance": {
                    "work_context_status": "PASS",
                    "brand_safety_confirmed": False,
                    "no_secret_echo": True,
                    "html_escaped": True,
                },
                "live_ycapi": True,
                "attestation": {
                    "observer": "ops-reviewer",
                    "captured_at": "2026-07-01T10:01:00+08:00",
                },
            }
        ),
        encoding="utf-8",
    )

    result = validate_evidence_intake(input_dir=evidence_dir, generated_at="2026-07-01T00:00:00Z")

    serialized = json.dumps(result, ensure_ascii=False)
    assert result["status"] == "FAIL"
    assert "schema_validation" in serialized
    assert "AC-23 trial evidence invalid" in serialized
    assert "operator.uses_sdk" in serialized


def test_evidence_intake_fails_schema_invalid_production_policy_attestation(tmp_path) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "production-policy-attestation.json").write_text(
        json.dumps(
            {
                "policy_id": "policy-1",
                "policy_version": "2026-07",
                "approved_at": "2026-07-01T10:00:00+08:00",
                "chargeback": {
                    "mode": "showback",
                    "confirmed": False,
                    "policy_ref": "policy-doc-1",
                    "effective_month": "2026-07",
                },
                "pricing_approval": {
                    "confirmed": True,
                    "pricing_version": "m1",
                    "approval_ref": "pricing-approval-1",
                    "approver": "Finance A",
                },
                "ycapi_token_limit": {
                    "confirmed": True,
                    "limit_ref": "limit-1",
                    "monthly_budget_cny": "0",
                    "rpm_limit": 0,
                },
                "employee_virtual_key_only": {
                    "confirmed": True,
                    "distribution_channel": "wecom",
                    "ycapi_token_visible_to_employee": True,
                },
                "data_boundaries": {
                    "confirmed": True,
                    "policy_ref": "data-policy-1",
                    "categories": ["metadata-only"],
                },
                "approvals": [
                    {"role": "finance", "approver": "Finance A", "approval_ref": "finance-1"},
                    {"role": "security", "approver": "Security B", "approval_ref": "security-1"},
                    {"role": "legal", "approver": "Legal C", "approval_ref": "legal-1"},
                ],
            }
        ),
        encoding="utf-8",
    )

    result = validate_evidence_intake(input_dir=evidence_dir, generated_at="2026-07-01T00:00:00Z")

    serialized = json.dumps(result, ensure_ascii=False)
    assert result["status"] == "FAIL"
    assert "schema_validation" in serialized
    assert "production policy attestation invalid" in serialized
    assert "employee_virtual_key_only.ycapi_token_visible_to_employee" in serialized


def test_evidence_intake_passes_filled_safe_files(tmp_path) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "aimanager-spend.csv").write_text(
        "\n".join(
            [
                "startTime,status,model,call_type,user,key_alias,spend,currency,metadata",
                "2026-07-01T09:00:00Z,success,openai/gemini-2.5-flash,completion,u1,key-alias,1.23,CNY,{}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (evidence_dir / "observability.json").write_text(
        json.dumps(
            {
                "status": "PASS",
                "alerts": [{"code": "aimanager_failure_rate_high", "severity": "warning"}],
            }
        ),
        encoding="utf-8",
    )
    (evidence_dir / "owner-note.md").write_text(
        "Finance confirmed non-zero July AiManager spend and ycapi bill reconciliation.\n",
        encoding="utf-8",
    )

    result = validate_evidence_intake(input_dir=evidence_dir, generated_at="2026-07-01T00:00:00Z")

    assert result["status"] == "PASS"
    assert result["summary"] == {"PASS": 3, "FAIL": 0, "BLOCKED": 0, "files": 3}


def _gap(gap_id: str, name: str, owner: str) -> dict[str, object]:
    return {
        "id": gap_id,
        "name": name,
        "status": "BLOCKED",
        "owner": owner,
        "detail": "missing external evidence",
        "required_env": [],
        "required_files": [],
        "command": "make acceptance-gate",
        "next_action": "collect real external evidence",
        "sources": ["launch_gap_plan"],
    }

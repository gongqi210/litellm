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

from __future__ import annotations

import json

from aimanager.scripts.generate_final_acceptance_report import collect_final_acceptance_report, main


def test_final_acceptance_report_blocks_without_input_files(tmp_path, capsys) -> None:
    output_json = tmp_path / "final-acceptance.json"
    output_markdown = tmp_path / "final-acceptance.md"

    exit_code = main(
        [
            "--output-json-file",
            str(output_json),
            "--output-markdown-file",
            str(output_markdown),
            "--generated-at",
            "2026-06-30T00:00:00Z",
        ]
    )

    result = json.loads(output_json.read_text(encoding="utf-8"))
    assert exit_code == 2
    assert "BLOCKED final acceptance report" in capsys.readouterr().out
    assert result["status"] == "BLOCKED"
    assert result["conclusion"] == "CONDITIONAL"
    assert result["summary"]["blockers"] == 3
    assert [blocker["id"] for blocker in result["blockers"]] == [
        "INPUT:business_trial",
        "INPUT:launch_gap_plan",
        "INPUT:acceptance_coverage",
    ]
    assert "Final conclusion: CONDITIONAL" in output_markdown.read_text(encoding="utf-8")


def test_final_acceptance_report_passes_only_when_all_gates_pass_and_no_gaps(tmp_path) -> None:
    business_file = tmp_path / "business.json"
    launch_file = tmp_path / "launch.json"
    coverage_file = tmp_path / "coverage.json"
    business_file.write_text(json.dumps(_business_bundle("PASS")), encoding="utf-8")
    launch_file.write_text(json.dumps(_launch_plan("PASS", gaps=[])), encoding="utf-8")
    coverage_file.write_text(json.dumps(_coverage_matrix("PASS")), encoding="utf-8")

    result = collect_final_acceptance_report(
        business_trial_file=business_file,
        launch_gap_plan_file=launch_file,
        acceptance_coverage_file=coverage_file,
        generated_at="2026-06-30T00:00:00Z",
    )

    assert result["status"] == "PASS"
    assert result["conclusion"] == "PASS"
    assert result["summary"]["blockers"] == 0
    assert result["stage_scores"]["business_trial_acceptance"] == 100
    assert result["stage_scores"]["launch_gap_closure"] == 100
    assert result["stage_scores"]["acceptance_coverage"] == 100
    assert "No unresolved acceptance blockers remain" in result["markdown"]


def test_final_acceptance_report_fails_on_failed_inputs_and_redacts_secrets(tmp_path) -> None:
    business_file = tmp_path / "business.json"
    launch_file = tmp_path / "launch.json"
    coverage_file = tmp_path / "coverage.json"
    secret_detail = (
        "ycapi echoed Bearer should-not-leak, sk-secret-value, "
        "postgresql://aimanager:db-secret@example/db and "
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=wecom-secret-key"
    )
    business_file.write_text(
        json.dumps(_business_bundle("FAIL", checks=[_check("AC-23", "nontechnical_lightweight_trial", "FAIL", secret_detail)])),
        encoding="utf-8",
    )
    launch_file.write_text(json.dumps(_launch_plan("PASS", gaps=[])), encoding="utf-8")
    coverage_file.write_text(json.dumps(_coverage_matrix("PASS")), encoding="utf-8")

    result = collect_final_acceptance_report(
        business_trial_file=business_file,
        launch_gap_plan_file=launch_file,
        acceptance_coverage_file=coverage_file,
    )
    serialized = json.dumps(result, ensure_ascii=False)

    assert result["status"] == "FAIL"
    assert result["conclusion"] == "FAIL"
    assert result["blockers"][0]["id"] == "AC-23"
    assert result["blockers"][0]["source"] == "business_trial_acceptance"
    assert "should-not-leak" not in serialized
    assert "sk-secret-value" not in serialized
    assert "db-secret" not in serialized
    assert "wecom-secret-key" not in serialized
    assert "[redacted:secret-like-value]" in serialized
    assert "[redacted:AIMANAGER_WECOM_WEBHOOK_URL]" in serialized


def test_final_acceptance_report_fails_when_supplied_evidence_intake_fails(tmp_path) -> None:
    business_file = tmp_path / "business.json"
    launch_file = tmp_path / "launch.json"
    coverage_file = tmp_path / "coverage.json"
    intake_file = tmp_path / "evidence-intake.json"
    business_file.write_text(json.dumps(_business_bundle("PASS")), encoding="utf-8")
    launch_file.write_text(json.dumps(_launch_plan("PASS", gaps=[])), encoding="utf-8")
    coverage_file.write_text(json.dumps(_coverage_matrix("PASS")), encoding="utf-8")
    intake_file.write_text(
        json.dumps(
            {
                "status": "FAIL",
                "generated_at": "2026-06-30T00:00:00Z",
                "summary": {"PASS": 0, "FAIL": 1, "BLOCKED": 0, "files": 1},
                "checks": [
                    {
                        "path": "owner/policy.json",
                        "status": "FAIL",
                        "detail": "found Authorization: Bearer should-not-leak",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = collect_final_acceptance_report(
        business_trial_file=business_file,
        launch_gap_plan_file=launch_file,
        acceptance_coverage_file=coverage_file,
        evidence_intake_file=intake_file,
    )
    serialized = json.dumps(result, ensure_ascii=False)

    assert result["status"] == "FAIL"
    assert result["conclusion"] == "FAIL"
    assert result["summary"]["inputs"] == 4
    assert result["input_statuses"]["evidence_intake_validation"]["status"] == "FAIL"
    assert result["stage_scores"]["evidence_intake_validation"] == 0
    assert result["blockers"][0]["id"] == "EVIDENCE-INTAKE:owner/policy.json"
    assert result["blockers"][0]["source"] == "evidence_intake_validation"
    assert "should-not-leak" not in serialized
    assert "Bearer [redacted:secret-like-value]" in serialized


def test_final_acceptance_report_blocks_when_launch_gap_plan_has_unresolved_gaps(tmp_path) -> None:
    business_file = tmp_path / "business.json"
    launch_file = tmp_path / "launch.json"
    coverage_file = tmp_path / "coverage.json"
    business_file.write_text(json.dumps(_business_bundle("BLOCKED", checks=[_check("AC-23", "trial", "BLOCKED", "missing timed trial evidence")])), encoding="utf-8")
    launch_file.write_text(
        json.dumps(
            _launch_plan(
                "BLOCKED",
                gaps=[
                    {
                        "id": "AC-23",
                        "name": "nontechnical_lightweight_trial",
                        "status": "BLOCKED",
                        "owner": "business_owner/market/ops",
                        "detail": "missing timed trial evidence",
                        "sources": ["business_trial"],
                        "required_env": ["AIMANAGER_LIGHTWEIGHT_TRIAL_EVIDENCE_FILE"],
                        "required_files": [],
                        "command": "legacy trial command",
                        "rerun_command": "python -m aimanager.scripts.capture_lightweight_trial_evidence",
                        "next_action": "run a 0-300 second nontechnical trial",
                    },
                    {
                        "id": "AC-26",
                        "name": "employee_monitoring_policy_evidence",
                        "status": "BLOCKED",
                        "owner": "HR/legal/security",
                        "detail": "missing employee acknowledgment export",
                        "sources": ["business_trial"],
                        "required_env": ["AIMANAGER_EMPLOYEE_ACKNOWLEDGMENT_FILE"],
                        "required_files": [],
                        "command": "python -m aimanager.scripts.validate_employee_monitoring_policy",
                        "next_action": "publish monitoring policy and export acknowledgments",
                    },
                ],
            )
        ),
        encoding="utf-8",
    )
    coverage_file.write_text(json.dumps(_coverage_matrix("BLOCKED", blocked=2)), encoding="utf-8")

    result = collect_final_acceptance_report(
        business_trial_file=business_file,
        launch_gap_plan_file=launch_file,
        acceptance_coverage_file=coverage_file,
    )

    assert result["status"] == "BLOCKED"
    assert result["conclusion"] == "CONDITIONAL"
    assert result["summary"]["blockers"] == 4
    assert result["summary"]["unique_blockers"] == 3
    assert [blocker["id"] for blocker in result["blockers"][:2]] == ["AC-23", "AC-26"]
    assert result["blockers"][0]["owner"] == "business_owner/market/ops"
    assert result["blockers"][0]["command"] == "python -m aimanager.scripts.capture_lightweight_trial_evidence"
    assert result["blockers"][0]["rerun_command"] == result["blockers"][0]["command"]
    assert "AIMANAGER_EMPLOYEE_ACKNOWLEDGMENT_FILE" in result["markdown"]


def test_final_acceptance_report_cli_fails_on_malformed_json(tmp_path) -> None:
    business_file = tmp_path / "business.json"
    output_json = tmp_path / "final-acceptance.json"
    business_file.write_text("{bad json", encoding="utf-8")

    exit_code = main(
        [
            "--business-trial-file",
            str(business_file),
            "--output-json-file",
            str(output_json),
        ]
    )

    result = json.loads(output_json.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert result["status"] == "FAIL"
    assert result["blockers"][0]["id"] == "INPUT:business_trial"
    assert "could not be loaded" in result["blockers"][0]["detail"]


def test_final_acceptance_report_rejects_pass_coverage_without_criteria(tmp_path) -> None:
    business_file = tmp_path / "business.json"
    launch_file = tmp_path / "launch.json"
    coverage_file = tmp_path / "coverage.json"
    business_file.write_text(json.dumps(_business_bundle("PASS")), encoding="utf-8")
    launch_file.write_text(json.dumps(_launch_plan("PASS", gaps=[])), encoding="utf-8")
    coverage_file.write_text(
        json.dumps(
            {
                "status": "PASS",
                "generated_at": "2026-06-30T00:00:00Z",
                "summary": {"criteria": 0, "PASS": 0, "FAIL": 0, "BLOCKED": 0},
                "coverage_failures": [],
            }
        ),
        encoding="utf-8",
    )

    result = collect_final_acceptance_report(
        business_trial_file=business_file,
        launch_gap_plan_file=launch_file,
        acceptance_coverage_file=coverage_file,
    )

    assert result["status"] == "FAIL"
    assert result["conclusion"] == "FAIL"
    assert result["blockers"][0]["id"] == "acceptance_coverage:CRITERIA"
    assert "criteria must be a non-empty list" in result["blockers"][0]["detail"]


def test_final_acceptance_report_rejects_pass_coverage_with_non_mapping_criteria(tmp_path) -> None:
    business_file = tmp_path / "business.json"
    launch_file = tmp_path / "launch.json"
    coverage_file = tmp_path / "coverage.json"
    business_file.write_text(json.dumps(_business_bundle("PASS")), encoding="utf-8")
    launch_file.write_text(json.dumps(_launch_plan("PASS", gaps=[])), encoding="utf-8")
    coverage_file.write_text(
        json.dumps(
            {
                "status": "PASS",
                "generated_at": "2026-06-30T00:00:00Z",
                "summary": {"criteria": 1, "PASS": 1, "FAIL": 0, "BLOCKED": 0},
                "coverage_failures": [],
                "criteria": ["junk"],
            }
        ),
        encoding="utf-8",
    )

    result = collect_final_acceptance_report(
        business_trial_file=business_file,
        launch_gap_plan_file=launch_file,
        acceptance_coverage_file=coverage_file,
    )

    assert result["status"] == "FAIL"
    assert result["blockers"][0]["id"] == "acceptance_coverage:CRITERIA"


def test_final_acceptance_report_rejects_pass_coverage_with_mixed_criteria_types(tmp_path) -> None:
    business_file = tmp_path / "business.json"
    launch_file = tmp_path / "launch.json"
    coverage_file = tmp_path / "coverage.json"
    business_file.write_text(json.dumps(_business_bundle("PASS")), encoding="utf-8")
    launch_file.write_text(json.dumps(_launch_plan("PASS", gaps=[])), encoding="utf-8")
    coverage = _coverage_matrix("PASS")
    coverage["criteria"].append("junk")
    coverage_file.write_text(json.dumps(coverage), encoding="utf-8")

    result = collect_final_acceptance_report(
        business_trial_file=business_file,
        launch_gap_plan_file=launch_file,
        acceptance_coverage_file=coverage_file,
    )

    assert result["status"] == "FAIL"
    assert result["blockers"][0]["id"] == "acceptance_coverage:CRITERIA"


def _business_bundle(status: str, *, checks: list[dict[str, object]] | None = None) -> dict[str, object]:
    checks = checks or [_check("AC-23", "nontechnical_lightweight_trial", status, "trial status")]
    return {
        "status": status,
        "generated_at": "2026-06-30T00:00:00Z",
        "summary": _summary(checks),
        "checks": checks,
    }


def _launch_plan(status: str, *, gaps: list[dict[str, object]]) -> dict[str, object]:
    return {
        "status": status,
        "generated_at": "2026-06-30T00:00:00Z",
        "summary": {
            "PASS": 0,
            "FAIL": sum(1 for gap in gaps if gap["status"] == "FAIL"),
            "BLOCKED": sum(1 for gap in gaps if gap["status"] == "BLOCKED"),
        },
        "gaps": gaps,
    }


def _coverage_matrix(status: str, *, blocked: int = 0) -> dict[str, object]:
    criteria = [
        {
            "id": "AC-01",
            "status": status,
            "owner": "architecture",
            "gate_kind": "local_test",
            "detail": "coverage status",
        }
    ]
    return {
        "status": status,
        "generated_at": "2026-06-30T00:00:00Z",
        "summary": {
            "criteria": len(criteria),
            "PASS": 0 if status != "PASS" else len(criteria),
            "FAIL": 1 if status == "FAIL" else 0,
            "BLOCKED": blocked or (1 if status == "BLOCKED" else 0),
        },
        "coverage_failures": [],
        "criteria": criteria,
    }


def _check(check_id: str, name: str, status: str, detail: str) -> dict[str, object]:
    return {
        "id": check_id,
        "name": name,
        "status": status,
        "detail": detail,
        "evidence": {},
    }


def _summary(checks: list[dict[str, object]]) -> dict[str, int]:
    return {
        status: sum(1 for check in checks if check["status"] == status)
        for status in ("PASS", "FAIL", "BLOCKED")
    }

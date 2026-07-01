from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from aimanager.scripts.acceptance_coverage_matrix import DEFAULT_GATE_REGISTRY
from aimanager.scripts.business_trial_acceptance_bundle import collect_business_trial_acceptance
from aimanager.scripts.production_readiness_bundle import collect_production_readiness
from aimanager.scripts.run_acceptance_gate import collect_acceptance_gate, main


PROJECT_ROOT = Path(__file__).resolve().parents[2]
GENERATED_AT = "2026-06-30T00:00:00Z"
GOLDEN_GENERATED_AT = "2026-06-30T03:00:00Z"
GOLDEN_FIXTURE_DIR = PROJECT_ROOT / "aimanager/tests/fixtures/acceptance_golden"
M1_CHECK_IDS = (
    "AC-15",
    "AC-20",
    "AC-19",
    "AC-08-KEY-INVENTORY",
    "AC-16-WECOM",
    "AC-12-13-FINANCE",
    "AC-POLICY",
)
M2_CHECK_IDS = (*M1_CHECK_IDS, "AC-23", "AC-26")


def test_acceptance_gate_writes_run_scoped_artifacts_and_reuses_production_readiness(tmp_path: Path) -> None:
    production_calls: list[str] = []
    business_seen_production: list[dict[str, Any]] = []

    def production_collector(*, generated_at: str, **_: object) -> dict[str, Any]:
        production_calls.append(generated_at)
        return _production_bundle("BLOCKED")

    def business_collector(
        *, production_readiness_collector: object, generated_at: str, **kwargs: object
    ) -> dict[str, Any]:
        cached_production = production_readiness_collector(generated_at=generated_at, **kwargs)  # type: ignore[operator]
        business_seen_production.append(cached_production)
        return _business_bundle("BLOCKED", production_checks=cached_production["checks"])

    result = collect_acceptance_gate(
        output_dir=tmp_path,
        project_directory=PROJECT_ROOT,
        acceptance_doc_file=PROJECT_ROOT / "docs/aimanager/1_acceptance_criteria.md",
        generated_at=GENERATED_AT,
        env={},
        production_readiness_collector=production_collector,
        business_trial_collector=business_collector,
        local_test_results_collector=_passing_local_test_results_collector,
    )

    assert production_calls == [GENERATED_AT]
    assert business_seen_production == [
        json.loads((tmp_path / "production-readiness.json").read_text(encoding="utf-8"))
    ]
    assert result["status"] == "BLOCKED"
    assert result["summary"]["steps"] == 7
    assert result["summary"]["PASS"] == 1
    assert result["summary"]["FAIL"] == 0
    assert result["summary"]["BLOCKED"] == 6
    assert result["evidence_intake"]["status"] == "BLOCKED"
    assert result["evidence_handoff"]["json_file"] == str(tmp_path / "evidence-handoff/evidence-handoff.json")
    assert result["evidence_template_pack"]["json_file"] == str(
        tmp_path / "evidence-template-pack/evidence-template-pack.json"
    )
    assert result["evidence_intake"]["json_file"] == str(tmp_path / "evidence-intake.json")
    assert [step["id"] for step in result["steps"]] == [
        "production_readiness",
        "business_trial_acceptance",
        "launch_gap_plan",
        "evidence_intake",
        "local_acceptance_tests",
        "acceptance_coverage_matrix",
        "final_acceptance_report",
    ]
    for artifact in (
        "production-readiness.json",
        "business-trial-acceptance.json",
        "launch-gap-plan.json",
        "launch-gap-plan.md",
        "acceptance-coverage.json",
        "acceptance-coverage.md",
        "final-acceptance-report.json",
        "final-acceptance-report.md",
        "evidence-handoff/evidence-handoff.json",
        "evidence-handoff/evidence-handoff.md",
        "evidence-template-pack/evidence-template-pack.json",
        "evidence-template-pack/evidence-template-pack.md",
        "evidence-template-pack/evidence-env.template",
        "evidence-template-pack/README.md",
        "evidence-intake.json",
        "evidence-intake.md",
        "local-test-results.json",
        "acceptance-gate.json",
        "acceptance-gate.md",
    ):
        assert (tmp_path / artifact).exists(), artifact
    final_report = json.loads((tmp_path / "final-acceptance-report.json").read_text(encoding="utf-8"))
    assert final_report["status"] == "BLOCKED"
    assert result["final_report"]["json_file"] == str(tmp_path / "final-acceptance-report.json")


def test_acceptance_gate_overwrites_stale_artifacts_and_can_reach_green_path(tmp_path: Path) -> None:
    stale = {
        "status": "FAIL",
        "generated_at": "1999-01-01T00:00:00Z",
        "checks": [_check("POISON", "stale", "FAIL", "stale file should not be consumed")],
    }
    (tmp_path / "business-trial-acceptance.json").write_text(json.dumps(stale), encoding="utf-8")
    safe_evidence = tmp_path / "safe-spend.csv"
    safe_evidence.write_text(
        "\n".join(
            [
                "startTime,status,model,call_type,user,key_alias,spend,currency,metadata",
                "2026-07-01T09:00:00Z,success,openai/gemini-2.5-flash,completion,u1,key-alias,1.23,CNY,{}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = collect_acceptance_gate(
        output_dir=tmp_path,
        project_directory=PROJECT_ROOT,
        acceptance_doc_file=PROJECT_ROOT / "docs/aimanager/1_acceptance_criteria.md",
        generated_at=GENERATED_AT,
        env={"AIMANAGER_SPEND_FILE": str(safe_evidence)},
        production_readiness_collector=lambda **_: _production_bundle("PASS"),
        business_trial_collector=lambda **kwargs: _business_bundle(
            "PASS",
            production_checks=kwargs["production_readiness_collector"]()["checks"],
        ),
        local_test_results_collector=_passing_local_test_results_collector,
    )

    business = json.loads((tmp_path / "business-trial-acceptance.json").read_text(encoding="utf-8"))
    final_report = json.loads((tmp_path / "final-acceptance-report.json").read_text(encoding="utf-8"))
    assert result["status"] == "PASS"
    assert business["status"] == "PASS"
    assert "POISON" not in json.dumps(business)
    assert final_report["status"] == "PASS"
    assert final_report["summary"]["blockers"] == 0


def test_acceptance_gate_reaches_pass_with_real_collectors_and_golden_evidence(tmp_path: Path) -> None:
    production_calls: list[str] = []

    def production_collector(**kwargs: object) -> dict[str, Any]:
        production_calls.append(str(kwargs.get("generated_at")))
        return collect_production_readiness(
            **kwargs,
            admin_boundary_runner=_passing_admin_boundary_runner,
            work_context_runner=_passing_work_context_runner,
            live_ycapi_runner=_passing_live_ycapi_runner,
            wecom_router=_passing_wecom_router,
        )

    result = collect_acceptance_gate(
        output_dir=tmp_path,
        project_directory=PROJECT_ROOT,
        acceptance_doc_file=PROJECT_ROOT / "docs/aimanager/1_acceptance_criteria.md",
        generated_at=GOLDEN_GENERATED_AT,
        env=_golden_acceptance_env(tmp_path),
        production_readiness_collector=production_collector,
        business_trial_collector=collect_business_trial_acceptance,
        local_test_results_collector=_passing_local_test_results_collector,
    )

    production = json.loads((tmp_path / "production-readiness.json").read_text(encoding="utf-8"))
    business = json.loads((tmp_path / "business-trial-acceptance.json").read_text(encoding="utf-8"))
    launch = json.loads((tmp_path / "launch-gap-plan.json").read_text(encoding="utf-8"))
    intake = json.loads((tmp_path / "evidence-intake.json").read_text(encoding="utf-8"))
    final_report = json.loads((tmp_path / "final-acceptance-report.json").read_text(encoding="utf-8"))

    assert production_calls == [GOLDEN_GENERATED_AT]
    assert result["status"] == "PASS"
    assert production["status"] == "PASS"
    assert business["status"] == "PASS"
    assert {check["id"]: check["status"] for check in production["checks"]} == {
        check_id: "PASS" for check_id in M1_CHECK_IDS
    }
    assert {check["id"]: check["status"] for check in business["checks"]} == {
        check_id: "PASS" for check_id in M2_CHECK_IDS
    }
    assert launch["gaps"] == []
    assert intake["status"] == "PASS"
    assert intake["summary"]["files"] >= 9
    assert final_report["status"] == "PASS"
    assert final_report["summary"]["blockers"] == 0


def test_acceptance_gate_fails_when_ycapi_bill_amount_diverges_from_golden_spend(tmp_path: Path) -> None:
    fixture_dir = tmp_path / "mutated-golden-evidence"
    shutil.copytree(GOLDEN_FIXTURE_DIR, fixture_dir)
    bill_file = fixture_dir / "ycapi-bill.json"
    bill = json.loads(bill_file.read_text(encoding="utf-8"))
    bill[0]["amount"] = "999.99"
    bill_file.write_text(json.dumps(bill), encoding="utf-8")

    result = _collect_real_golden_acceptance_gate(tmp_path, fixture_dir=fixture_dir)

    production = json.loads((tmp_path / "production-readiness.json").read_text(encoding="utf-8"))
    finance_check = _check_by_id(production, "AC-12-13-FINANCE")
    reconciliation = (tmp_path / "finance-output/aimanager_reconciliation.csv").read_text(encoding="utf-8")
    assert result["status"] == "FAIL"
    assert production["status"] == "FAIL"
    assert finance_check["status"] == "FAIL"
    assert "needs_review" in finance_check["detail"]
    assert "needs_review" in reconciliation


def test_acceptance_gate_fails_when_key_inventory_user_is_not_in_employee_roster(tmp_path: Path) -> None:
    def mutate(fixture_dir: Path) -> None:
        inventory_file = fixture_dir / "key-inventory.json"
        inventory = json.loads(inventory_file.read_text(encoding="utf-8"))
        inventory["keys"][0]["user_id"] = "u_unknown"
        inventory_file.write_text(json.dumps(inventory), encoding="utf-8")

    _assert_rejected_golden_mutation(tmp_path, mutate, expected_check_id="AC-08-KEY-INVENTORY")


def test_acceptance_gate_fails_when_production_policy_lacks_legal_approval(tmp_path: Path) -> None:
    def mutate(fixture_dir: Path) -> None:
        policy_file = fixture_dir / "production-policy-attestation.json"
        policy = json.loads(policy_file.read_text(encoding="utf-8"))
        policy["approvals"] = [approval for approval in policy["approvals"] if approval["role"] != "legal"]
        policy_file.write_text(json.dumps(policy), encoding="utf-8")

    _assert_rejected_golden_mutation(tmp_path, mutate, expected_check_id="AC-POLICY")


def test_acceptance_gate_fails_when_trial_attestation_is_after_gate_run(tmp_path: Path) -> None:
    def mutate(fixture_dir: Path) -> None:
        trial_file = fixture_dir / "lightweight-trial.json"
        trial = json.loads(trial_file.read_text(encoding="utf-8"))
        trial["attestation"]["captured_at"] = "2026-06-30T12:00:00+08:00"
        trial_file.write_text(json.dumps(trial), encoding="utf-8")

    _assert_rejected_golden_mutation(tmp_path, mutate, expected_check_id="AC-23")


def test_acceptance_gate_fails_when_employee_acknowledgment_predates_policy(tmp_path: Path) -> None:
    def mutate(fixture_dir: Path) -> None:
        acknowledgment_file = fixture_dir / "employee-acknowledgments.csv"
        rows = acknowledgment_file.read_text(encoding="utf-8").splitlines()
        acknowledgment_file.write_text(
            rows[0]
            + "\n"
            + "\n".join(
                ",".join(
                    [fields[0], fields[1], "2026-06-29T23:00:00+08:00", *fields[3:]]
                )
                for fields in (row.split(",") for row in rows[1:])
            )
            + "\n",
            encoding="utf-8",
        )

    _assert_rejected_golden_mutation(tmp_path, mutate, expected_check_id="AC-26")


def test_acceptance_gate_fails_when_env_pointed_evidence_fails_intake(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    owner_dir = tmp_path / "owner-evidence"
    owner_dir.mkdir()
    unsafe_policy = owner_dir / "unsafe-policy.json"
    unsafe_policy.write_text(
        json.dumps(
            {
                "status": "PASS",
                "request": {"Authorization": "Bearer should-not-leak"},
            }
        ),
        encoding="utf-8",
    )

    result = collect_acceptance_gate(
        output_dir=artifact_dir,
        project_directory=PROJECT_ROOT,
        acceptance_doc_file=PROJECT_ROOT / "docs/aimanager/1_acceptance_criteria.md",
        generated_at=GENERATED_AT,
        env={"AIMANAGER_PRODUCTION_POLICY_ATTESTATION_FILE": str(unsafe_policy)},
        production_readiness_collector=lambda **_: _production_bundle("PASS"),
        business_trial_collector=lambda **kwargs: _business_bundle(
            "PASS",
            production_checks=kwargs["production_readiness_collector"]()["checks"],
        ),
        local_test_results_collector=_passing_local_test_results_collector,
    )

    intake_step = _step_by_id(result, "evidence_intake")
    intake = json.loads((artifact_dir / "evidence-intake.json").read_text(encoding="utf-8"))
    final_report = json.loads((artifact_dir / "final-acceptance-report.json").read_text(encoding="utf-8"))
    serialized = json.dumps(result, ensure_ascii=False)
    for path in (path for path in artifact_dir.rglob("*") if path.is_file()):
        serialized += path.read_text(encoding="utf-8")
    assert result["status"] == "FAIL"
    assert intake_step["status"] == "FAIL"
    assert intake["status"] == "FAIL"
    assert final_report["status"] == "FAIL"
    assert final_report["blockers"][0]["source"] == "evidence_intake_validation"
    assert "should-not-leak" not in serialized
    assert "Bearer [redacted:secret-like-value]" in serialized


def test_acceptance_gate_intake_scans_env_pointed_key_inventory(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    owner_dir = tmp_path / "owner-evidence"
    owner_dir.mkdir()
    unsafe_inventory = owner_dir / "unsafe-key-inventory.json"
    unsafe_inventory.write_text(
        json.dumps(
            {
                "keys": [
                    {
                        "key_alias": "raw-key-export",
                        "token": "sk-live-raw-secret-that-must-not-leak",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = collect_acceptance_gate(
        output_dir=artifact_dir,
        project_directory=PROJECT_ROOT,
        acceptance_doc_file=PROJECT_ROOT / "docs/aimanager/1_acceptance_criteria.md",
        generated_at=GENERATED_AT,
        env={"AIMANAGER_KEY_INVENTORY_FILE": str(unsafe_inventory)},
        production_readiness_collector=lambda **_: _production_bundle("PASS"),
        business_trial_collector=lambda **kwargs: _business_bundle(
            "PASS",
            production_checks=kwargs["production_readiness_collector"]()["checks"],
        ),
        local_test_results_collector=_passing_local_test_results_collector,
    )

    intake = json.loads((artifact_dir / "evidence-intake.json").read_text(encoding="utf-8"))
    serialized = json.dumps(result, ensure_ascii=False)
    for path in (path for path in artifact_dir.rglob("*") if path.is_file()):
        serialized += path.read_text(encoding="utf-8")
    assert result["status"] == "FAIL"
    assert intake["status"] == "FAIL"
    assert "sk-live-raw-secret" not in serialized
    assert "[redacted:secret-like-value]" in serialized


def test_acceptance_gate_passes_safe_env_pointed_evidence_without_validating_generated_templates(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    owner_dir = tmp_path / "owner-evidence"
    owner_dir.mkdir()
    safe_spend = owner_dir / "safe-spend.csv"
    safe_spend.write_text(
        "\n".join(
            [
                "startTime,status,model,call_type,user,key_alias,spend,currency,metadata",
                "2026-07-01T09:00:00Z,success,openai/gemini-2.5-flash,completion,u1,key-alias,1.23,CNY,{}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = collect_acceptance_gate(
        output_dir=artifact_dir,
        project_directory=PROJECT_ROOT,
        acceptance_doc_file=PROJECT_ROOT / "docs/aimanager/1_acceptance_criteria.md",
        generated_at=GENERATED_AT,
        env={"AIMANAGER_SPEND_FILE": str(safe_spend)},
        production_readiness_collector=lambda **_: _production_bundle("PASS"),
        business_trial_collector=lambda **kwargs: _business_bundle(
            "PASS",
            production_checks=kwargs["production_readiness_collector"]()["checks"],
        ),
        local_test_results_collector=_passing_local_test_results_collector,
    )

    intake_step = _step_by_id(result, "evidence_intake")
    intake = json.loads((artifact_dir / "evidence-intake.json").read_text(encoding="utf-8"))
    template_pack = json.loads((artifact_dir / "evidence-template-pack/evidence-template-pack.json").read_text(encoding="utf-8"))
    assert result["status"] == "PASS"
    assert intake_step["status"] == "PASS"
    assert intake["summary"] == {"PASS": 1, "FAIL": 0, "BLOCKED": 0, "files": 1}
    assert template_pack["status"] == "PASS"


def test_acceptance_gate_fails_when_mapped_local_acceptance_test_fails(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    owner_dir = tmp_path / "owner-evidence"
    owner_dir.mkdir()
    safe_spend = owner_dir / "safe-spend.csv"
    safe_spend.write_text(
        "\n".join(
            [
                "startTime,status,model,call_type,user,key_alias,spend,currency,metadata",
                "2026-07-01T09:00:00Z,success,openai/gemini-2.5-flash,completion,u1,key-alias,1.23,CNY,{}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = collect_acceptance_gate(
        output_dir=artifact_dir,
        project_directory=PROJECT_ROOT,
        acceptance_doc_file=PROJECT_ROOT / "docs/aimanager/1_acceptance_criteria.md",
        generated_at=GENERATED_AT,
        env={"AIMANAGER_SPEND_FILE": str(safe_spend)},
        production_readiness_collector=lambda **_: _production_bundle("PASS"),
        business_trial_collector=lambda **kwargs: _business_bundle(
            "PASS",
            production_checks=kwargs["production_readiness_collector"]()["checks"],
        ),
        local_test_results_collector=lambda **_: _local_test_results_with_failure("AC-10"),
    )

    local_results = json.loads((artifact_dir / "local-test-results.json").read_text(encoding="utf-8"))
    coverage = json.loads((artifact_dir / "acceptance-coverage.json").read_text(encoding="utf-8"))
    final_report = json.loads((artifact_dir / "final-acceptance-report.json").read_text(encoding="utf-8"))
    assert result["status"] == "FAIL"
    assert _step_by_id(result, "local_acceptance_tests")["status"] == "FAIL"
    assert local_results["status"] == "FAIL"
    assert _criterion_by_id(coverage, "AC-10")["status"] == "FAIL"
    assert final_report["status"] == "FAIL"
    assert "AC-10" in [blocker["id"] for blocker in final_report["blockers"]]


def test_acceptance_gate_redacts_secret_like_values_in_all_written_artifacts(tmp_path: Path) -> None:
    secret_detail = (
        "leaked Bearer should-not-leak and sk-secret-value plus "
        "postgresql://aimanager:db-secret@example/db and "
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=wecom-secret-key"
    )

    result = collect_acceptance_gate(
        output_dir=tmp_path,
        project_directory=PROJECT_ROOT,
        acceptance_doc_file=PROJECT_ROOT / "docs/aimanager/1_acceptance_criteria.md",
        generated_at=GENERATED_AT,
        env={},
        production_readiness_collector=lambda **_: _production_bundle("FAIL", detail=secret_detail),
        business_trial_collector=lambda **kwargs: _business_bundle(
            "FAIL",
            production_checks=kwargs["production_readiness_collector"]()["checks"],
            detail=secret_detail,
        ),
        local_test_results_collector=_passing_local_test_results_collector,
    )

    serialized = json.dumps(result, ensure_ascii=False)
    for path in (path for path in tmp_path.rglob("*") if path.is_file()):
        serialized += path.read_text(encoding="utf-8")
    assert result["status"] == "FAIL"
    assert "should-not-leak" not in serialized
    assert "sk-secret-value" not in serialized
    assert "db-secret" not in serialized
    assert "wecom-secret-key" not in serialized
    assert "[redacted:secret-like-value]" in serialized
    assert "[redacted:AIMANAGER_WECOM_WEBHOOK_URL]" in serialized


def test_acceptance_gate_redacts_secret_env_values_and_common_dsn_passwords(tmp_path: Path) -> None:
    secret_detail = "token supersecret-value and mysql://aimanager:mysql-secret@example/db should not leak"

    collect_acceptance_gate(
        output_dir=tmp_path,
        project_directory=PROJECT_ROOT,
        acceptance_doc_file=PROJECT_ROOT / "docs/aimanager/1_acceptance_criteria.md",
        generated_at=GENERATED_AT,
        env={"YCAPI_API_TOKEN": "supersecret-value"},
        production_readiness_collector=lambda **_: _production_bundle("FAIL", detail=secret_detail),
        business_trial_collector=lambda **kwargs: _business_bundle(
            "FAIL",
            production_checks=kwargs["production_readiness_collector"]()["checks"],
            detail=secret_detail,
        ),
        local_test_results_collector=_passing_local_test_results_collector,
    )

    serialized = ""
    for path in (path for path in tmp_path.rglob("*") if path.is_file()):
        serialized += path.read_text(encoding="utf-8")
    assert "supersecret-value" not in serialized
    assert "mysql-secret" not in serialized
    assert "[redacted:YCAPI_API_TOKEN]" in serialized
    assert "[redacted:secret-like-value]" in serialized


def test_acceptance_gate_folds_fail_ahead_of_blocked_when_steps_are_mixed(tmp_path: Path) -> None:
    result = collect_acceptance_gate(
        output_dir=tmp_path,
        project_directory=PROJECT_ROOT,
        acceptance_doc_file=PROJECT_ROOT / "docs/aimanager/1_acceptance_criteria.md",
        generated_at=GENERATED_AT,
        env={},
        production_readiness_collector=lambda **_: _production_bundle("FAIL"),
        business_trial_collector=lambda **kwargs: _business_bundle(
            "BLOCKED",
            production_checks=kwargs["production_readiness_collector"]()["checks"],
        ),
        local_test_results_collector=_passing_local_test_results_collector,
    )

    assert result["status"] == "FAIL"
    assert result["summary"]["FAIL"] >= 1
    assert result["summary"]["BLOCKED"] >= 1
    assert result["final_report"]["status"] == "FAIL"


def test_acceptance_gate_cli_reports_failure_when_output_dir_cannot_be_created(tmp_path: Path, capsys) -> None:
    output_dir = tmp_path / "not-a-directory"
    output_dir.write_text("occupied", encoding="utf-8")

    exit_code = main(["--output-dir", str(output_dir)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "FAIL acceptance gate: FileExistsError" in captured.err


def test_acceptance_gate_cli_runs_empty_env_without_production_secrets(tmp_path: Path, monkeypatch, capsys) -> None:
    for name in (
        "AIMANAGER_BUSINESS_BASE_URL",
        "AIMANAGER_EMPLOYEE_VIRTUAL_KEY",
        "AIMANAGER_PUBLIC_ADMIN_URL",
        "AIMANAGER_OBSERVABILITY_REPORT_FILE",
        "AIMANAGER_WECOM_WEBHOOK_URL",
        "AIMANAGER_SPEND_FILE",
        "AIMANAGER_YCAPI_BILL_FILE",
        "AIMANAGER_KEY_INVENTORY_FILE",
        "AIMANAGER_PRODUCTION_POLICY_ATTESTATION_FILE",
        "AIMANAGER_LIGHTWEIGHT_TRIAL_EVIDENCE_FILE",
        "AIMANAGER_EMPLOYEE_MONITORING_POLICY_FILE",
        "AIMANAGER_EMPLOYEE_ROSTER_FILE",
        "AIMANAGER_EMPLOYEE_ACKNOWLEDGMENT_FILE",
        "YCAPI_API_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    supplied_local_results = tmp_path / "supplied-local-test-results.json"
    supplied_local_results.write_text(json.dumps(_local_test_results_with_failure()), encoding="utf-8")
    monkeypatch.setenv("AIMANAGER_LOCAL_TEST_RESULTS_FILE", str(supplied_local_results))

    exit_code = main(
        [
            "--output-dir",
            str(tmp_path),
            "--project-directory",
            str(PROJECT_ROOT),
            "--acceptance-doc-file",
            str(PROJECT_ROOT / "docs/aimanager/1_acceptance_criteria.md"),
            "--generated-at",
            GENERATED_AT,
        ]
    )

    output = capsys.readouterr().out
    result = json.loads((tmp_path / "acceptance-gate.json").read_text(encoding="utf-8"))
    assert exit_code == 2
    assert "BLOCKED acceptance gate" in output
    assert result["status"] == "BLOCKED"
    assert result["final_report"]["status"] == "BLOCKED"
    assert result["summary"]["unique_blockers"] >= 8


def _production_bundle(status: str, *, detail: str | None = None) -> dict[str, Any]:
    checks = [
        _check(check_id, check_id.lower(), status, detail or f"{check_id} {status.lower()}")
        for check_id in M1_CHECK_IDS
    ]
    return _bundle(status=status, checks=checks)


def _business_bundle(
    status: str,
    *,
    production_checks: list[dict[str, Any]],
    detail: str | None = None,
) -> dict[str, Any]:
    checks = [
        *production_checks,
        _check("AC-23", "nontechnical_lightweight_trial", status, detail or f"AC-23 {status.lower()}"),
        _check("AC-26", "employee_monitoring_policy_evidence", status, detail or f"AC-26 {status.lower()}"),
    ]
    assert {check["id"] for check in checks} == set(M2_CHECK_IDS)
    return _bundle(status=status, checks=checks)


def _bundle(*, status: str, checks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "status": status,
        "generated_at": GENERATED_AT,
        "summary": {
            "PASS": sum(1 for check in checks if check["status"] == "PASS"),
            "FAIL": sum(1 for check in checks if check["status"] == "FAIL"),
            "BLOCKED": sum(1 for check in checks if check["status"] == "BLOCKED"),
        },
        "checks": checks,
    }


def _check(check_id: str, name: str, status: str, detail: str) -> dict[str, Any]:
    return {
        "id": check_id,
        "name": name,
        "status": status,
        "detail": detail,
        "evidence": {},
    }


def _golden_acceptance_env(tmp_path: Path) -> dict[str, str]:
    finance_output_dir = tmp_path / "finance-output"
    return {
        "AIMANAGER_BUSINESS_BASE_URL": "https://aimanager-business.internal.invalid",
        "AIMANAGER_PUBLIC_ADMIN_URL": "https://aimanager-admin.internal.invalid",
        "AIMANAGER_ALLOWED_SSO_REDIRECT_HOSTS": "sso.company.internal",
        "AIMANAGER_EMPLOYEE_VIRTUAL_KEY": "synthetic-employee-virtual-key-from-secure-env",
        "YCAPI_API_TOKEN": "synthetic-ycapi-credential-from-secure-env",
        "AIMANAGER_KEY_INVENTORY_FILE": str(GOLDEN_FIXTURE_DIR / "key-inventory.json"),
        "AIMANAGER_OBSERVABILITY_REPORT_FILE": str(GOLDEN_FIXTURE_DIR / "observability.json"),
        "AIMANAGER_WECOM_WEBHOOK_URL": "https://wecom.internal.invalid/aimanager-alerts",
        "AIMANAGER_SPEND_FILE": str(GOLDEN_FIXTURE_DIR / "spend.json"),
        "AIMANAGER_YCAPI_BILL_FILE": str(GOLDEN_FIXTURE_DIR / "ycapi-bill.json"),
        "AIMANAGER_FINANCE_OUTPUT_DIR": str(finance_output_dir),
        "AIMANAGER_PRODUCTION_POLICY_ATTESTATION_FILE": str(
            GOLDEN_FIXTURE_DIR / "production-policy-attestation.json"
        ),
        "AIMANAGER_LIGHTWEIGHT_TRIAL_EVIDENCE_FILE": str(GOLDEN_FIXTURE_DIR / "lightweight-trial.json"),
        "AIMANAGER_EMPLOYEE_MONITORING_POLICY_FILE": str(GOLDEN_FIXTURE_DIR / "employee-monitoring-policy.json"),
        "AIMANAGER_EMPLOYEE_ROSTER_FILE": str(GOLDEN_FIXTURE_DIR / "employee-roster.csv"),
        "AIMANAGER_EMPLOYEE_ACKNOWLEDGMENT_FILE": str(GOLDEN_FIXTURE_DIR / "employee-acknowledgments.csv"),
    }


def _golden_acceptance_env_for_fixtures(tmp_path: Path, fixture_dir: Path) -> dict[str, str]:
    env = _golden_acceptance_env(tmp_path)
    env.update(
        {
            "AIMANAGER_KEY_INVENTORY_FILE": str(fixture_dir / "key-inventory.json"),
            "AIMANAGER_OBSERVABILITY_REPORT_FILE": str(fixture_dir / "observability.json"),
            "AIMANAGER_SPEND_FILE": str(fixture_dir / "spend.json"),
            "AIMANAGER_YCAPI_BILL_FILE": str(fixture_dir / "ycapi-bill.json"),
            "AIMANAGER_PRODUCTION_POLICY_ATTESTATION_FILE": str(
                fixture_dir / "production-policy-attestation.json"
            ),
            "AIMANAGER_LIGHTWEIGHT_TRIAL_EVIDENCE_FILE": str(fixture_dir / "lightweight-trial.json"),
            "AIMANAGER_EMPLOYEE_MONITORING_POLICY_FILE": str(fixture_dir / "employee-monitoring-policy.json"),
            "AIMANAGER_EMPLOYEE_ROSTER_FILE": str(fixture_dir / "employee-roster.csv"),
            "AIMANAGER_EMPLOYEE_ACKNOWLEDGMENT_FILE": str(fixture_dir / "employee-acknowledgments.csv"),
        }
    )
    return env


def _collect_real_golden_acceptance_gate(tmp_path: Path, *, fixture_dir: Path) -> dict[str, object]:
    def production_collector(**kwargs: object) -> dict[str, Any]:
        return collect_production_readiness(
            **kwargs,
            admin_boundary_runner=_passing_admin_boundary_runner,
            work_context_runner=_passing_work_context_runner,
            live_ycapi_runner=_passing_live_ycapi_runner,
            wecom_router=_passing_wecom_router,
        )

    return collect_acceptance_gate(
        output_dir=tmp_path,
        project_directory=PROJECT_ROOT,
        acceptance_doc_file=PROJECT_ROOT / "docs/aimanager/1_acceptance_criteria.md",
        generated_at=GOLDEN_GENERATED_AT,
        env=_golden_acceptance_env_for_fixtures(tmp_path, fixture_dir),
        production_readiness_collector=production_collector,
        business_trial_collector=collect_business_trial_acceptance,
        local_test_results_collector=_passing_local_test_results_collector,
    )


def _assert_rejected_golden_mutation(
    tmp_path: Path, mutate: Callable[[Path], None], *, expected_check_id: str
) -> None:
    fixture_dir = tmp_path / "mutated-golden-evidence"
    shutil.copytree(GOLDEN_FIXTURE_DIR, fixture_dir)
    mutate(fixture_dir)

    result = _collect_real_golden_acceptance_gate(tmp_path, fixture_dir=fixture_dir)

    business = json.loads((tmp_path / "business-trial-acceptance.json").read_text(encoding="utf-8"))
    check = _check_by_id(business, expected_check_id)
    assert result["status"] == "FAIL"
    assert business["status"] == "FAIL"
    assert check["status"] == "FAIL"


def _passing_admin_boundary_runner(**_: object) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            name="business_admin_route_blocked",
            surface="business",
            method="GET",
            path="/ui",
            status="PASS",
            status_code=403,
            policy_code="aimanager_route_not_allowed",
            detail="business surface blocks admin UI and management routes",
        ),
        SimpleNamespace(
            name="public_admin_sso_protected",
            surface="public_admin",
            method="GET",
            path="/ui",
            status="PASS",
            status_code=302,
            policy_code="sso_redirect",
            detail="public admin URL redirects only to allowlisted SSO host",
        ),
    ]


def _passing_live_ycapi_runner(**_: object) -> SimpleNamespace:
    return SimpleNamespace(
        status="PASS",
        detail="ycapi /models returned all expected models",
        status_code=200,
        model_count=3,
        observed_models=("gemini-2.5-flash", "deepseek-chat", "ycapi-image-1"),
    )


def _passing_work_context_runner(**_: object) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            case=SimpleNamespace(method="POST", path="/v1/chat/completions", model="gemini-2.5-flash"),
            passed=True,
            detail="chat missing metadata rejected before provider dispatch",
            status_code=400,
            policy_code="aimanager_work_context_invalid",
        ),
        SimpleNamespace(
            case=SimpleNamespace(method="POST", path="/v1/images/generations", model="ycapi-image-1"),
            passed=True,
            detail="image missing metadata rejected before provider dispatch",
            status_code=400,
            policy_code="aimanager_work_context_invalid",
        ),
    ]


def _passing_wecom_router(**_: object) -> SimpleNamespace:
    return SimpleNamespace(
        status="PASS",
        detail="sent 1 alert to WeCom webhook",
        alert_count=1,
        delivered_count=1,
        payload=None,
    )


def _local_test_results_with_failure(failed_id: str | None = None) -> dict[str, object]:
    criteria: list[dict[str, object]] = []
    for gate in DEFAULT_GATE_REGISTRY:
        if gate.gate_kind not in {"local_test", "runtime_smoke"}:
            continue
        status = "FAIL" if gate.criterion_id == failed_id else "PASS"
        criteria.append(
            {
                "id": gate.criterion_id,
                "status": status,
                "detail": f"synthetic local tests {status.lower()} for {gate.criterion_id}",
                "targets": [
                    artifact
                    for artifact in gate.local_artifacts
                    if artifact.startswith("aimanager/tests/") and artifact.endswith(".py")
                ],
                "command": "synthetic pytest",
                "returncode": 1 if status == "FAIL" else 0,
            }
        )
    return {
        "status": "FAIL" if failed_id else "PASS",
        "generated_at": GENERATED_AT,
        "summary": {
            "criteria": len(criteria),
            "PASS": sum(1 for item in criteria if item["status"] == "PASS"),
            "FAIL": sum(1 for item in criteria if item["status"] == "FAIL"),
            "BLOCKED": 0,
        },
        "criteria": criteria,
    }


def _passing_local_test_results_collector(**_: object) -> dict[str, object]:
    return _local_test_results_with_failure()


def _criterion_by_id(coverage: dict[str, Any], criterion_id: str) -> dict[str, Any]:
    for criterion in coverage["criteria"]:
        if isinstance(criterion, dict) and criterion.get("id") == criterion_id:
            return criterion
    raise AssertionError(f"missing criterion {criterion_id}")


def _step_by_id(result: dict[str, object], step_id: str) -> dict[str, Any]:
    for step in result["steps"]:  # type: ignore[index]
        if isinstance(step, dict) and step.get("id") == step_id:
            return step
    raise AssertionError(f"missing step {step_id}")


def _check_by_id(bundle: dict[str, Any], check_id: str) -> dict[str, Any]:
    for check in bundle["checks"]:
        if isinstance(check, dict) and check.get("id") == check_id:
            return check
    raise AssertionError(f"missing check {check_id}")

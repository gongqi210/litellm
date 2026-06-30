from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aimanager.scripts.run_acceptance_gate import collect_acceptance_gate, main


PROJECT_ROOT = Path(__file__).resolve().parents[2]
GENERATED_AT = "2026-06-30T00:00:00Z"
M1_CHECK_IDS = (
    "AC-15",
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
    )

    assert production_calls == [GENERATED_AT]
    assert business_seen_production == [
        json.loads((tmp_path / "production-readiness.json").read_text(encoding="utf-8"))
    ]
    assert result["status"] == "BLOCKED"
    assert result["summary"]["steps"] == 6
    assert result["summary"]["PASS"] == 1
    assert result["summary"]["FAIL"] == 0
    assert result["summary"]["BLOCKED"] == 5
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

    result = collect_acceptance_gate(
        output_dir=tmp_path,
        project_directory=PROJECT_ROOT,
        acceptance_doc_file=PROJECT_ROOT / "docs/aimanager/1_acceptance_criteria.md",
        generated_at=GENERATED_AT,
        env={},
        production_readiness_collector=lambda **_: _production_bundle("PASS"),
        business_trial_collector=lambda **kwargs: _business_bundle(
            "PASS",
            production_checks=kwargs["production_readiness_collector"]()["checks"],
        ),
    )

    business = json.loads((tmp_path / "business-trial-acceptance.json").read_text(encoding="utf-8"))
    final_report = json.loads((tmp_path / "final-acceptance-report.json").read_text(encoding="utf-8"))
    assert result["status"] == "PASS"
    assert business["status"] == "PASS"
    assert "POISON" not in json.dumps(business)
    assert final_report["status"] == "PASS"
    assert final_report["summary"]["blockers"] == 0


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
    )

    intake_step = _step_by_id(result, "evidence_intake")
    intake = json.loads((artifact_dir / "evidence-intake.json").read_text(encoding="utf-8"))
    template_pack = json.loads((artifact_dir / "evidence-template-pack/evidence-template-pack.json").read_text(encoding="utf-8"))
    assert result["status"] == "PASS"
    assert intake_step["status"] == "PASS"
    assert intake["summary"] == {"PASS": 1, "FAIL": 0, "BLOCKED": 0, "files": 1}
    assert template_pack["status"] == "PASS"


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


def _step_by_id(result: dict[str, object], step_id: str) -> dict[str, Any]:
    for step in result["steps"]:  # type: ignore[index]
        if isinstance(step, dict) and step.get("id") == step_id:
            return step
    raise AssertionError(f"missing step {step_id}")

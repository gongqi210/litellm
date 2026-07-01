from __future__ import annotations

import json

from aimanager.scripts.acceptance_coverage_matrix import (
    AcceptanceGate,
    collect_acceptance_coverage_matrix,
    main,
)


def test_acceptance_coverage_matrix_covers_real_acceptance_doc() -> None:
    result = collect_acceptance_coverage_matrix(
        acceptance_doc_file=_project_root() / "docs/aimanager/1_acceptance_criteria.md",
        project_directory=_project_root(),
        generated_at="2026-06-30T00:00:00Z",
    )

    expected_ids = [f"AC-{number:02d}" for number in range(1, 27)] + ["AC-POLICY"]
    assert [item["id"] for item in result["criteria"]] == expected_ids
    assert result["doc_coverage"]["missing_in_registry"] == []
    assert result["doc_coverage"]["missing_in_doc"] == []
    assert result["status"] == "BLOCKED"
    assert result["summary"]["FAIL"] == 0
    assert result["summary"]["BLOCKED"] == 9


def test_acceptance_coverage_matrix_overlays_bundle_checks_and_merged_ids(tmp_path) -> None:
    production_file = tmp_path / "production-readiness.json"
    business_file = tmp_path / "business-trial.json"
    production_file.write_text(
        json.dumps(
            _bundle(
                [
                    _check("AC-15", "production_admin_boundary", "BLOCKED", "missing production URLs"),
                    _check("AC-20", "production_work_context_enforcement", "PASS", "work context smoke ok"),
                    _check("AC-19", "live_ycapi_preflight", "PASS", "ycapi ok"),
                    _check("AC-08-KEY-INVENTORY", "production_key_inventory_governance", "PASS", "key inventory ok"),
                    _check("AC-16-WECOM", "wecom_alert_routing", "PASS", "alert delivered"),
                    _check("AC-12-13-FINANCE", "finance_reconciliation", "PASS", "real bill reconciled"),
                    _check("AC-POLICY", "production_policy_attestation", "PASS", "policy approved"),
                ]
            )
        ),
        encoding="utf-8",
    )
    business_file.write_text(
        json.dumps(
            _bundle(
                [
                    _check("AC-15", "production_admin_boundary", "BLOCKED", "still missing URLs"),
                    _check("AC-19", "live_ycapi_preflight", "PASS", "ycapi ok"),
                    _check("AC-16-WECOM", "wecom_alert_routing", "PASS", "alert delivered"),
                    _check("AC-12-13-FINANCE", "finance_reconciliation", "PASS", "real bill reconciled"),
                    _check("AC-POLICY", "production_policy_attestation", "PASS", "policy approved"),
                    _check("AC-23", "nontechnical_lightweight_trial", "FAIL", "operator took too long"),
                    _check("AC-26", "employee_monitoring_policy_evidence", "PASS", "HR/legal ok"),
                ]
            )
        ),
        encoding="utf-8",
    )

    result = collect_acceptance_coverage_matrix(
        acceptance_doc_file=_project_root() / "docs/aimanager/1_acceptance_criteria.md",
        project_directory=_project_root(),
        production_readiness_file=production_file,
        business_trial_file=business_file,
        generated_at="2026-06-30T00:00:00Z",
    )

    by_id = {item["id"]: item for item in result["criteria"]}
    assert result["status"] == "FAIL"
    assert by_id["AC-08"]["status"] == "PASS"
    assert by_id["AC-08"]["bundle_check_id"] == "AC-08-KEY-INVENTORY"
    assert by_id["AC-12"]["status"] == "PASS"
    assert by_id["AC-13"]["bundle_check_id"] == "AC-12-13-FINANCE"
    assert by_id["AC-15"]["status"] == "BLOCKED"
    assert by_id["AC-20"]["status"] == "PASS"
    assert by_id["AC-20"]["bundle_check_id"] == "AC-20"
    assert by_id["AC-23"]["status"] == "FAIL"
    assert by_id["AC-23"]["detail"] == "operator took too long"
    assert "AC-23" in result["markdown"]


def test_acceptance_coverage_matrix_fails_when_supplied_bundle_omits_registered_check(tmp_path) -> None:
    production_file = tmp_path / "production-readiness.json"
    production_file.write_text(
        json.dumps(
            _bundle(
                [
                    _check("AC-15", "production_admin_boundary", "PASS", "ok"),
                    _check("AC-20", "production_work_context_enforcement", "PASS", "ok"),
                    _check("AC-19", "live_ycapi_preflight", "PASS", "ok"),
                    _check("AC-08-KEY-INVENTORY", "production_key_inventory_governance", "PASS", "ok"),
                    _check("AC-12-13-FINANCE", "finance_reconciliation", "PASS", "ok"),
                    _check("AC-POLICY", "production_policy_attestation", "PASS", "ok"),
                ]
            )
        ),
        encoding="utf-8",
    )

    result = collect_acceptance_coverage_matrix(
        acceptance_doc_file=_project_root() / "docs/aimanager/1_acceptance_criteria.md",
        project_directory=_project_root(),
        production_readiness_file=production_file,
        generated_at="2026-06-30T00:00:00Z",
    )

    by_id = {item["id"]: item for item in result["criteria"]}
    assert result["status"] == "FAIL"
    assert by_id["AC-16"]["status"] == "FAIL"
    assert by_id["AC-16"]["bundle_check_id"] == "AC-16-WECOM"
    assert "registered bundle check AC-16-WECOM is absent" in by_id["AC-16"]["detail"]


def test_acceptance_coverage_matrix_fails_on_unregistered_bundle_checks(tmp_path) -> None:
    production_file = tmp_path / "production-readiness.json"
    business_file = tmp_path / "business-trial.json"
    production_file.write_text(
        json.dumps(
            _bundle(
                [
                    _check("AC-15", "production_admin_boundary", "PASS", "ok"),
                    _check("AC-20", "production_work_context_enforcement", "PASS", "ok"),
                    _check("AC-19", "live_ycapi_preflight", "PASS", "ok"),
                    _check("AC-08-KEY-INVENTORY", "production_key_inventory_governance", "PASS", "ok"),
                    _check("AC-16-WECOM", "wecom_alert_routing", "PASS", "ok"),
                    _check("AC-12-13-FINANCE", "finance_reconciliation", "PASS", "ok"),
                    _check("AC-POLICY", "production_policy_attestation", "PASS", "ok"),
                    _check("AC-NEW-PROD", "new_production_gate", "FAIL", "new gate failed"),
                ]
            )
        ),
        encoding="utf-8",
    )
    business_file.write_text(
        json.dumps(_bundle([_check("AC-23", "trial", "PASS", "ok"), _check("AC-26", "monitoring", "PASS", "ok")])),
        encoding="utf-8",
    )

    result = collect_acceptance_coverage_matrix(
        acceptance_doc_file=_project_root() / "docs/aimanager/1_acceptance_criteria.md",
        project_directory=_project_root(),
        production_readiness_file=production_file,
        business_trial_file=business_file,
        generated_at="2026-06-30T00:00:00Z",
    )

    assert result["status"] == "FAIL"
    assert result["coverage_failures"][0]["id"] == "BUNDLE-REGISTRY-production_readiness"
    assert result["coverage_failures"][0]["unregistered_checks"] == ["AC-NEW-PROD"]


def test_acceptance_coverage_matrix_keeps_missing_artifact_detail_when_bundle_passes(tmp_path) -> None:
    doc_file = tmp_path / "acceptance.md"
    doc_file.write_text("| AC-99 | synthetic | test |\n", encoding="utf-8")
    production_file = tmp_path / "production.json"
    production_file.write_text(json.dumps(_bundle([_check("AC-99-GATE", "synthetic", "PASS", "bundle passed")])), encoding="utf-8")

    result = collect_acceptance_coverage_matrix(
        acceptance_doc_file=doc_file,
        project_directory=tmp_path,
        production_readiness_file=production_file,
        gate_registry=[
            AcceptanceGate(
                criterion_id="AC-99",
                owner="qa",
                gate_kind="bundle_check",
                local_artifacts=("missing_test.py",),
                bundle_check_id="AC-99-GATE",
                bundle_source="production_readiness",
            )
        ],
        generated_at="2026-06-30T00:00:00Z",
    )

    assert result["status"] == "FAIL"
    assert result["criteria"][0]["detail"] == "missing registered local artifacts: missing_test.py"


def test_acceptance_coverage_matrix_treats_missing_bundle_file_as_blocked(tmp_path) -> None:
    doc_file = tmp_path / "acceptance.md"
    artifact = tmp_path / "existing_test.py"
    artifact.write_text("# test placeholder\n", encoding="utf-8")
    doc_file.write_text("| AC-99 | synthetic | test |\n", encoding="utf-8")

    result = collect_acceptance_coverage_matrix(
        acceptance_doc_file=doc_file,
        project_directory=tmp_path,
        production_readiness_file=tmp_path / "missing-production.json",
        gate_registry=[
            AcceptanceGate(
                criterion_id="AC-99",
                owner="qa",
                gate_kind="bundle_check",
                local_artifacts=("existing_test.py",),
                bundle_check_id="AC-99-GATE",
                bundle_source="production_readiness",
            )
        ],
        generated_at="2026-06-30T00:00:00Z",
    )

    assert result["status"] == "BLOCKED"
    assert result["criteria"][0]["status"] == "BLOCKED"
    assert "bundle file does not exist" in result["criteria"][0]["detail"]


def test_acceptance_coverage_matrix_detects_missing_local_artifacts(tmp_path) -> None:
    doc_file = tmp_path / "acceptance.md"
    doc_file.write_text("| AC-99 | synthetic | test |\n", encoding="utf-8")

    result = collect_acceptance_coverage_matrix(
        acceptance_doc_file=doc_file,
        project_directory=tmp_path,
        gate_registry=[
            AcceptanceGate(
                criterion_id="AC-99",
                owner="qa",
                gate_kind="local_test",
                local_artifacts=("missing_test.py",),
            )
        ],
        generated_at="2026-06-30T00:00:00Z",
    )

    assert result["status"] == "FAIL"
    assert result["criteria"][0]["status"] == "FAIL"
    assert result["criteria"][0]["missing_artifacts"] == ["missing_test.py"]


def test_acceptance_coverage_matrix_redacts_secret_like_bundle_details(tmp_path) -> None:
    production_file = tmp_path / "production-readiness.json"
    production_file.write_text(
        json.dumps(
            _bundle(
                [
                    _check(
                        "AC-19",
                        "live_ycapi_preflight",
                        "FAIL",
                        "Bearer should-not-leak sk-secret-value "
                        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=wecom-secret-key",
                    )
                ]
            )
        ),
        encoding="utf-8",
    )

    result = collect_acceptance_coverage_matrix(
        acceptance_doc_file=_project_root() / "docs/aimanager/1_acceptance_criteria.md",
        project_directory=_project_root(),
        production_readiness_file=production_file,
        generated_at="2026-06-30T00:00:00Z",
    )
    serialized = json.dumps(result, ensure_ascii=False)

    assert result["status"] == "FAIL"
    assert "should-not-leak" not in serialized
    assert "sk-secret-value" not in serialized
    assert "wecom-secret-key" not in serialized
    assert "[redacted:secret-like-value]" in serialized
    assert "[redacted:AIMANAGER_WECOM_WEBHOOK_URL]" in serialized


def test_acceptance_coverage_matrix_keeps_markdown_table_on_one_line(tmp_path) -> None:
    production_file = tmp_path / "production-readiness.json"
    production_file.write_text(json.dumps(_bundle([_check("AC-19", "live_ycapi_preflight", "FAIL", "line one\nline two")])), encoding="utf-8")

    result = collect_acceptance_coverage_matrix(
        acceptance_doc_file=_project_root() / "docs/aimanager/1_acceptance_criteria.md",
        project_directory=_project_root(),
        production_readiness_file=production_file,
        generated_at="2026-06-30T00:00:00Z",
    )

    assert "line one<br>line two" in result["markdown"]


def test_acceptance_coverage_matrix_cli_writes_json_and_markdown(tmp_path, capsys) -> None:
    output_json = tmp_path / "coverage.json"
    output_markdown = tmp_path / "coverage.md"

    exit_code = main(
        [
            "--acceptance-doc-file",
            str(_project_root() / "docs/aimanager/1_acceptance_criteria.md"),
            "--project-directory",
            str(_project_root()),
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
    assert result["status"] == "BLOCKED"
    assert "BLOCKED acceptance coverage matrix" in capsys.readouterr().out
    assert "AC-POLICY" in output_markdown.read_text(encoding="utf-8")


def _project_root():
    return __import__("pathlib").Path(__file__).resolve().parents[2]


def _bundle(checks: list[dict[str, object]]) -> dict[str, object]:
    summary = {
        status: sum(1 for check in checks if check["status"] == status)
        for status in ("PASS", "FAIL", "BLOCKED")
    }
    return {
        "status": "FAIL" if summary["FAIL"] else "BLOCKED" if summary["BLOCKED"] else "PASS",
        "generated_at": "2026-06-30T00:00:00Z",
        "summary": summary,
        "checks": checks,
    }


def _check(check_id: str, name: str, status: str, detail: str) -> dict[str, object]:
    return {
        "id": check_id,
        "name": name,
        "status": status,
        "detail": detail,
        "evidence": {},
    }

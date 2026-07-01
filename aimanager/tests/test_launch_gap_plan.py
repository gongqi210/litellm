from __future__ import annotations

import json

from aimanager.scripts.generate_launch_gap_plan import collect_launch_gap_plan, main


def test_launch_gap_plan_dedupes_production_checks_and_groups_actionable_gaps(tmp_path) -> None:
    production_file = tmp_path / "production-readiness.json"
    business_file = tmp_path / "business-trial.json"
    production_file.write_text(
        json.dumps(
            _bundle(
                checks=[
                    _check(
                        "AC-15",
                        "production_admin_boundary",
                        "BLOCKED",
                        "missing production URLs",
                        {"required_env": ["AIMANAGER_BUSINESS_BASE_URL", "AIMANAGER_PUBLIC_ADMIN_URL"]},
                    ),
                    _check("AC-19", "live_ycapi_preflight", "PASS", "ycapi ok"),
                ]
            )
        ),
        encoding="utf-8",
    )
    business_file.write_text(
        json.dumps(
            _bundle(
                checks=[
                    _check("AC-15", "production_admin_boundary", "BLOCKED", "same blocker from M2"),
                    _check("AC-23", "nontechnical_lightweight_trial", "BLOCKED", "missing timed trial evidence"),
                    _check("AC-26", "employee_monitoring_policy_evidence", "FAIL", "missing legal acknowledgment"),
                ]
            )
        ),
        encoding="utf-8",
    )

    result = collect_launch_gap_plan(
        production_readiness_file=production_file,
        business_trial_file=business_file,
        generated_at="2026-06-30T00:00:00Z",
    )

    assert result["status"] == "FAIL"
    assert result["summary"] == {"PASS": 1, "FAIL": 1, "BLOCKED": 2}
    assert [gap["id"] for gap in result["gaps"]] == ["AC-26", "AC-15", "AC-23"]
    ac15 = next(gap for gap in result["gaps"] if gap["id"] == "AC-15")
    assert ac15["owner"] == "architecture/security/ops"
    assert ac15["required_env"] == ["AIMANAGER_BUSINESS_BASE_URL", "AIMANAGER_PUBLIC_ADMIN_URL"]
    assert ac15["sources"] == ["business_trial", "production_readiness"]
    assert "smoke_admin_boundary" in ac15["command"]
    assert "生产业务 URL" in ac15["next_action"]
    assert "AC-26" in result["markdown"]
    assert "HR/legal/security" in result["markdown"]


def test_launch_gap_plan_redacts_secret_like_values_from_json_and_markdown(tmp_path) -> None:
    production_file = tmp_path / "production-readiness.json"
    production_file.write_text(
        json.dumps(
            _bundle(
                checks=[
                    _check(
                        "AC-19",
                        "live_ycapi_preflight",
                        "FAIL",
                        "upstream echoed Bearer should-not-leak, sk-secret-value, and "
                        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=wecom-secret-key",
                    )
                ]
            )
        ),
        encoding="utf-8",
    )

    result = collect_launch_gap_plan(production_readiness_file=production_file)
    serialized = json.dumps(result, ensure_ascii=False)

    assert result["status"] == "FAIL"
    assert "should-not-leak" not in serialized
    assert "sk-secret-value" not in serialized
    assert "wecom-secret-key" not in serialized
    assert "[redacted:secret-like-value]" in serialized
    assert "[redacted:AIMANAGER_WECOM_WEBHOOK_URL]" in serialized


def test_launch_gap_plan_assigns_key_inventory_gap_to_security_ops(tmp_path) -> None:
    production_file = tmp_path / "production-readiness.json"
    production_file.write_text(
        json.dumps(
            _bundle(
                checks=[
                    _check(
                        "AC-08-KEY-INVENTORY",
                        "production_key_inventory_governance",
                        "BLOCKED",
                        "missing AIMANAGER_KEY_INVENTORY_FILE",
                        {"required_env": ["AIMANAGER_KEY_INVENTORY_FILE"]},
                    )
                ]
            )
        ),
        encoding="utf-8",
    )

    result = collect_launch_gap_plan(production_readiness_file=production_file)

    assert result["status"] == "BLOCKED"
    gap = result["gaps"][0]
    assert gap["id"] == "AC-08-KEY-INVENTORY"
    assert gap["owner"] == "security/ops"
    assert gap["required_env"] == ["AIMANAGER_KEY_INVENTORY_FILE"]
    assert "export_key_inventory" in gap["command"]
    assert "validate_key_inventory" in gap["command"]
    assert gap["command"].index("export_key_inventory") < gap["command"].index("validate_key_inventory")
    assert "export_key_inventory" in gap["next_action"]
    assert "virtual key" in gap["next_action"]
    assert "expected_total_key_count" in gap["next_action"]


def test_launch_gap_plan_assigns_finance_gap_to_export_before_readiness(tmp_path) -> None:
    production_file = tmp_path / "production-readiness.json"
    production_file.write_text(
        json.dumps(
            _bundle(
                checks=[
                    _check(
                        "AC-12-13-FINANCE",
                        "finance_evidence",
                        "BLOCKED",
                        "missing spend and ycapi bill files",
                        {"required_env": ["AIMANAGER_SPEND_FILE", "AIMANAGER_YCAPI_BILL_FILE"]},
                    )
                ]
            )
        ),
        encoding="utf-8",
    )

    result = collect_launch_gap_plan(production_readiness_file=production_file)

    assert result["status"] == "BLOCKED"
    gap = result["gaps"][0]
    assert gap["id"] == "AC-12-13-FINANCE"
    assert gap["owner"] == "finance"
    assert gap["required_env"] == ["AIMANAGER_SPEND_FILE", "AIMANAGER_YCAPI_BILL_FILE"]
    assert "aimanager.scripts.export_finance" in gap["command"]
    assert "--spend-file \"$AIMANAGER_SPEND_FILE\"" in gap["command"]
    assert "--ycapi-bill-file \"$AIMANAGER_YCAPI_BILL_FILE\"" in gap["command"]
    assert "--output-dir \"${AIMANAGER_FINANCE_OUTPUT_DIR:-/tmp/aimanager-finance-export}\"" in gap["command"]
    assert "aimanager.scripts.production_readiness_bundle" in gap["command"]
    assert gap["command"].index("export_finance") < gap["command"].index("production_readiness_bundle")
    assert "export_finance" in gap["next_action"]
    assert "非零计费金额" in gap["next_action"]


def test_launch_gap_plan_keeps_worst_duplicate_status_and_preserves_existing_evidence(tmp_path) -> None:
    production_file = tmp_path / "production-readiness.json"
    business_file = tmp_path / "business-trial.json"
    production_file.write_text(
        json.dumps(
            _bundle(
                checks=[
                    _check(
                        "CUSTOM-GAP",
                        "custom_launch_gate",
                        "PASS",
                        "custom gate initially passed",
                        {"required_env": ["CUSTOM_REQUIRED_ENV"], "required_files": ["custom-evidence.json"]},
                    )
                ]
            )
        ),
        encoding="utf-8",
    )
    business_file.write_text(
        json.dumps(_bundle(checks=[_check("CUSTOM-GAP", "custom_launch_gate", "FAIL", "custom gate failed later")])),
        encoding="utf-8",
    )

    result = collect_launch_gap_plan(production_readiness_file=production_file, business_trial_file=business_file)

    assert result["status"] == "FAIL"
    assert result["summary"] == {"PASS": 0, "FAIL": 1, "BLOCKED": 0}
    assert len(result["gaps"]) == 1
    gap = result["gaps"][0]
    assert gap["id"] == "CUSTOM-GAP"
    assert gap["status"] == "FAIL"
    assert gap["required_env"] == ["CUSTOM_REQUIRED_ENV"]
    assert gap["required_files"] == ["custom-evidence.json"]
    assert gap["sources"] == ["business_trial", "production_readiness"]


def test_launch_gap_plan_cli_writes_blocked_plan_without_input_files(tmp_path, capsys) -> None:
    output_json = tmp_path / "launch-gap-plan.json"
    output_markdown = tmp_path / "launch-gap-plan.md"

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

    output = capsys.readouterr().out
    result = json.loads(output_json.read_text(encoding="utf-8"))
    assert exit_code == 2
    assert "BLOCKED launch gap plan" in output
    assert result["status"] == "BLOCKED"
    assert result["gaps"][0]["id"] == "INPUT"
    assert "production_readiness" in output_markdown.read_text(encoding="utf-8")


def _bundle(*, checks: list[dict[str, object]]) -> dict[str, object]:
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


def _check(
    check_id: str,
    name: str,
    status: str,
    detail: str,
    evidence: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "id": check_id,
        "name": name,
        "status": status,
        "detail": detail,
        "evidence": evidence or {},
    }

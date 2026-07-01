from __future__ import annotations

import json

from aimanager.scripts.generate_evidence_template_pack import collect_evidence_template_pack, main
from aimanager.scripts.production_readiness_bundle import collect_production_readiness


def test_evidence_template_pack_writes_safe_templates_without_secret_echo(tmp_path) -> None:
    launch_file = tmp_path / "launch-gap-plan.json"
    output_dir = tmp_path / "template-pack"
    launch_file.write_text(
        json.dumps(
            {
                "status": "BLOCKED",
                "gaps": [
                    _gap(
                        "AC-19",
                        "live_ycapi_spend",
                        "ops",
                        detail="operator must provide YCAPI_API_TOKEN=do-not-leak-token from secure shell",
                        required_env=["YCAPI_API_TOKEN"],
                    ),
                    _gap(
                        "AC-12-13-FINANCE",
                        "finance_export_reconciliation",
                        "finance",
                        required_env=["AIMANAGER_SPEND_FILE", "AIMANAGER_YCAPI_BILL_FILE"],
                    ),
                    _gap(
                        "AC-08-KEY-INVENTORY",
                        "production_key_inventory_governance",
                        "security/ops",
                        required_env=["AIMANAGER_KEY_INVENTORY_FILE"],
                    ),
                    _gap(
                        "AC-15",
                        "production_admin_boundary",
                        "architecture/security/ops",
                        required_env=[
                            "AIMANAGER_BUSINESS_BASE_URL",
                            "AIMANAGER_PUBLIC_ADMIN_URL",
                            "AIMANAGER_ALLOWED_SSO_REDIRECT_HOSTS",
                        ],
                    ),
                    _gap(
                        "AC-16-WECOM",
                        "wecom_alert_routing",
                        "ops",
                        detail="use webhook https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=do-not-leak",
                        required_env=["AIMANAGER_OBSERVABILITY_REPORT_FILE", "AIMANAGER_WECOM_WEBHOOK_URL"],
                    ),
                    _gap(
                        "AC-23",
                        "nontechnical_lightweight_trial",
                        "business_owner/market/ops",
                        required_env=["AIMANAGER_LIGHTWEIGHT_TRIAL_EVIDENCE_FILE"],
                    ),
                    _gap(
                        "AC-26",
                        "employee_monitoring_policy_evidence",
                        "HR/legal/security",
                        required_env=[
                            "AIMANAGER_EMPLOYEE_MONITORING_POLICY_FILE",
                            "AIMANAGER_EMPLOYEE_ROSTER_FILE",
                            "AIMANAGER_EMPLOYEE_ACKNOWLEDGMENT_FILE",
                        ],
                    ),
                    _gap(
                        "AC-POLICY",
                        "production_policy_attestation",
                        "general_manager/finance/security/legal",
                        required_env=["AIMANAGER_PRODUCTION_POLICY_ATTESTATION_FILE"],
                    ),
                ],
            }
        ),
        encoding="utf-8",
    )

    result = collect_evidence_template_pack(
        launch_gap_plan_file=launch_file,
        output_dir=output_dir,
        generated_at="2026-07-01T00:00:00Z",
    )

    serialized = json.dumps(result, ensure_ascii=False) + "".join(
        path.read_text(encoding="utf-8") for path in sorted(output_dir.rglob("*")) if path.is_file()
    )
    assert result["status"] == "BLOCKED"
    assert result["summary"]["templates"] >= 8
    assert (output_dir / "evidence-env.template").exists()
    assert (output_dir / "templates/security-ops/key-inventory.template.json").exists()
    assert (output_dir / "templates/finance/aimanager-spend.template.csv").exists()
    assert (output_dir / "templates/finance/ycapi-bill.template.csv").exists()
    assert (output_dir / "templates/ops/observability-report.template.json").exists()
    assert (output_dir / "templates/business-trial/ac23-trial-evidence.template.json").exists()
    assert (output_dir / "templates/hr-legal-security/employee-roster.template.csv").exists()
    assert (output_dir / "templates/hr-legal-security/employee-acknowledgments.template.csv").exists()
    assert (output_dir / "templates/policy/production-policy-attestation.template.json").exists()
    assert "TEMPLATE_DO_NOT_SUBMIT" in serialized
    assert "do-not-leak" not in serialized
    assert "[redacted:AIMANAGER_WECOM_WEBHOOK_URL]" in serialized
    env_template = (output_dir / "evidence-env.template").read_text(encoding="utf-8")
    assert "export AIMANAGER_BUSINESS_BASE_URL=\"\"" in env_template
    assert "export AIMANAGER_PUBLIC_ADMIN_URL=\"\"" in env_template
    assert "export AIMANAGER_ALLOWED_SSO_REDIRECT_HOSTS=\"\"" in env_template
    assert "export AIMANAGER_KEY_INVENTORY_FILE=" in env_template
    assert "export AIMANAGER_WECOM_WEBHOOK_URL=\"\"" in env_template
    assert "# YCAPI_API_TOKEN must be injected by a secret manager or secure shell" in env_template
    assert "export YCAPI_API_TOKEN" not in env_template
    assert "do-not-leak-token" not in env_template
    key_inventory_template = json.loads(
        (output_dir / "templates/security-ops/key-inventory.template.json").read_text(encoding="utf-8")
    )
    assert key_inventory_template["export_scope"] == "all_virtual_keys"
    assert key_inventory_template["expected_total_key_count"] == len(key_inventory_template["keys"])
    assert key_inventory_template["export_source"]
    assert key_inventory_template["exported_by"]


def test_evidence_template_pack_cli_writes_manifest_and_exits_non_pass(tmp_path, capsys) -> None:
    launch_file = tmp_path / "launch-gap-plan.json"
    output_dir = tmp_path / "template-pack"
    launch_file.write_text(json.dumps({"status": "BLOCKED", "gaps": [_gap("AC-19", "live_ycapi", "ops")]}))

    exit_code = main(["--launch-gap-plan-file", str(launch_file), "--output-dir", str(output_dir)])

    output = capsys.readouterr().out
    manifest = json.loads((output_dir / "evidence-template-pack.json").read_text(encoding="utf-8"))
    assert exit_code == 2
    assert "BLOCKED evidence template pack" in output
    assert manifest["status"] == "BLOCKED"


def test_generated_templates_cannot_satisfy_production_readiness(tmp_path) -> None:
    launch_file = tmp_path / "launch-gap-plan.json"
    output_dir = tmp_path / "template-pack"
    launch_file.write_text(
        json.dumps(
            {
                "status": "BLOCKED",
                "gaps": [
                    _gap("AC-08-KEY-INVENTORY", "key-inventory", "security/ops"),
                    _gap("AC-12-13-FINANCE", "finance", "finance"),
                    _gap("AC-POLICY", "policy", "general_manager/finance/security/legal"),
                ],
            }
        ),
        encoding="utf-8",
    )
    collect_evidence_template_pack(launch_gap_plan_file=launch_file, output_dir=output_dir)

    bundle = collect_production_readiness(
        env={
            "AIMANAGER_KEY_INVENTORY_FILE": str(output_dir / "templates/security-ops/key-inventory.template.json"),
            "AIMANAGER_SPEND_FILE": str(output_dir / "templates/finance/aimanager-spend.template.csv"),
            "AIMANAGER_YCAPI_BILL_FILE": str(output_dir / "templates/finance/ycapi-bill.template.csv"),
            "AIMANAGER_PRODUCTION_POLICY_ATTESTATION_FILE": str(
                output_dir / "templates/policy/production-policy-attestation.template.json"
            ),
        }
    )

    checks = {check["id"]: check for check in bundle["checks"]}
    assert bundle["status"] == "FAIL"
    assert checks["AC-08-KEY-INVENTORY"]["status"] == "FAIL"
    assert checks["AC-12-13-FINANCE"]["status"] == "BLOCKED"
    assert checks["AC-POLICY"]["status"] == "FAIL"


def _gap(
    gap_id: str,
    name: str,
    owner: str,
    *,
    detail: str = "missing external evidence",
    required_env: list[str] | None = None,
) -> dict[str, object]:
    return {
        "id": gap_id,
        "name": name,
        "status": "BLOCKED",
        "owner": owner,
        "detail": detail,
        "required_env": required_env or [],
        "required_files": [],
        "command": "make acceptance-gate",
        "next_action": "collect real external evidence",
        "sources": ["launch_gap_plan"],
    }

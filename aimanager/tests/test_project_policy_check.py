from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
POLICY_SCRIPT = PROJECT_ROOT / "scripts" / "project_policy_check.py"
CLI_MAIN = PROJECT_ROOT / "cli" / "main.py"
MAKEFILE = PROJECT_ROOT / "Makefile"
LIGHTWEIGHT_TRIAL_CONFIRMATION_ENV = (
    "AIMANAGER_LIGHTWEIGHT_TRIAL_LIVE_YCAPI_CONFIRMED",
    "AIMANAGER_LIGHTWEIGHT_TRIAL_BRAND_SAFETY_CONFIRMED",
    "AIMANAGER_LIGHTWEIGHT_TRIAL_NO_SECRET_ECHO_CONFIRMED",
    "AIMANAGER_LIGHTWEIGHT_TRIAL_HTML_ESCAPED_CONFIRMED",
)


def _load_policy_module():
    spec = importlib.util.spec_from_file_location("project_policy_check", POLICY_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_project_policy_gate_passes_current_aimanager_scaffold() -> None:
    policy_check = _load_policy_module()

    results = policy_check.run_policy_checks(PROJECT_ROOT)

    by_name = {result.name: result for result in results}
    assert by_name["project_type"].status == "PASS"
    assert by_name["required_scaffold"].status == "PASS"
    assert by_name["cli_contract"].status == "PASS"
    assert by_name["allowed_deps"].status == "PASS"
    assert by_name["ycapi_boundary"].status == "PASS"
    assert by_name["env_example_secrets"].status == "PASS"
    assert not [result for result in results if result.status in {"FAIL", "BLOCKED"}]


def test_project_policy_gate_fails_when_required_scaffold_is_missing(tmp_path: Path) -> None:
    policy_check = _load_policy_module()
    (tmp_path / "scripts").mkdir()

    results = policy_check.run_policy_checks(tmp_path)

    by_name = {result.name: result for result in results}
    assert by_name["required_scaffold"].status == "FAIL"
    assert "PROJECT_TYPE" in by_name["required_scaffold"].message
    assert policy_check.exit_code_for(results) == 1


def test_aimanager_cli_entry_emits_machine_readable_status() -> None:
    completed = subprocess.run(
        [sys.executable, str(CLI_MAIN), "--json"],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        capture_output=True,
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["status"] == "PASS"
    assert "finance_export" in payload["commands"]
    finance_export = payload["commands"]["finance_export"]
    assert "aimanager.scripts.export_finance" in finance_export
    assert "--spend-file \"$AIMANAGER_SPEND_FILE\"" in finance_export
    assert "--ycapi-bill-file \"$AIMANAGER_YCAPI_BILL_FILE\"" in finance_export
    assert "--output-dir \"${AIMANAGER_FINANCE_OUTPUT_DIR:-/tmp/aimanager-finance-export}\"" in finance_export
    assert "wecom_alert_route" in payload["commands"]
    wecom_alert_route = payload["commands"]["wecom_alert_route"]
    assert "aimanager.scripts.route_observability_alerts" in wecom_alert_route
    assert "--report-file \"$AIMANAGER_OBSERVABILITY_REPORT_FILE\"" in wecom_alert_route
    assert "--webhook-url \"$AIMANAGER_WECOM_WEBHOOK_URL\"" in wecom_alert_route
    assert "--min-severity \"${AIMANAGER_WECOM_MIN_SEVERITY:-warning}\"" in wecom_alert_route
    assert "--title \"AiManager production readiness alerts\"" in wecom_alert_route
    assert "--output-payload-file /tmp/aimanager-wecom-alert-payload.json" in wecom_alert_route
    assert "admin_boundary_smoke" in payload["commands"]
    admin_boundary_smoke = payload["commands"]["admin_boundary_smoke"]
    assert "aimanager.scripts.smoke_admin_boundary" in admin_boundary_smoke
    assert "--business-base-url \"$AIMANAGER_BUSINESS_BASE_URL\"" in admin_boundary_smoke
    assert "--public-admin-url \"$AIMANAGER_PUBLIC_ADMIN_URL\"" in admin_boundary_smoke
    assert "AIMANAGER_ALLOWED_SSO_REDIRECT_HOSTS=\"${AIMANAGER_ALLOWED_SSO_REDIRECT_HOSTS:-}\"" in admin_boundary_smoke
    assert "--require-business-base-url" in admin_boundary_smoke
    assert "--require-public-admin-url" in admin_boundary_smoke
    assert "<sso-host>" not in admin_boundary_smoke
    assert payload["commands"]["production_readiness"] == "make production-readiness"
    assert payload["commands"]["key_inventory_readiness"] == "make key-inventory-readiness"
    assert payload["commands"]["finance_readiness"] == "make finance-readiness"
    assert payload["commands"]["admin_boundary_readiness"] == "make admin-boundary-readiness"
    assert payload["commands"]["wecom_alert_readiness"] == "make wecom-alert-readiness"
    assert payload["commands"]["live_ycapi_preflight"] == "make live-ycapi-preflight"
    assert payload["commands"]["work_context_enforcement_smoke"] == "make work-context-enforcement-smoke"
    assert payload["commands"]["work_context_enforcement_readiness"] == "make work-context-enforcement-readiness"
    assert payload["commands"]["production_policy_readiness"] == "make production-policy-readiness"
    assert payload["commands"]["employee_monitoring_validate"] == "make employee-monitoring-validate"
    assert payload["commands"]["lightweight_trial_evidence_capture"] == "make lightweight-trial-evidence-capture"
    assert "acceptance_gate" in payload["commands"]


def test_makefile_exposes_finance_export_operator_target() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "finance-export:" in makefile
    assert "aimanager.scripts.export_finance" in makefile
    assert "--spend-file \"$${AIMANAGER_SPEND_FILE}\"" in makefile
    assert "--ycapi-bill-file \"$${AIMANAGER_YCAPI_BILL_FILE}\"" in makefile
    assert "--output-dir \"$${AIMANAGER_FINANCE_OUTPUT_DIR:-/tmp/aimanager-finance-export}\"" in makefile


def test_makefile_exposes_remaining_production_readiness_operator_targets() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "production-readiness:" in makefile
    assert "aimanager.scripts.production_readiness_bundle" in makefile

    assert "key-inventory-readiness:" in makefile
    assert "$(MAKE) key-inventory-export" in makefile
    assert "aimanager.scripts.validate_key_inventory" in makefile
    assert "--inventory-file \"$${AIMANAGER_KEY_INVENTORY_FILE:-/tmp/aimanager-key-inventory.json}\"" in makefile

    assert "finance-readiness:" in makefile
    assert "$(MAKE) finance-export" in makefile
    assert "$(MAKE) production-readiness" in makefile

    assert "admin-boundary-readiness:" in makefile
    assert "$(MAKE) admin-boundary-smoke || true" in makefile

    assert "wecom-alert-readiness:" in makefile
    assert "$(MAKE) wecom-alert-route AIMANAGER_WECOM_DRY_RUN=true || true" in makefile

    assert "work-context-enforcement-readiness:" in makefile
    assert "$(MAKE) work-context-enforcement-smoke || true" in makefile
    assert "$(MAKE) production-readiness" in makefile


def test_makefile_exposes_wecom_alert_route_operator_target() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "wecom-alert-route:" in makefile
    assert "aimanager.scripts.route_observability_alerts" in makefile
    assert "--report-file \"$${AIMANAGER_OBSERVABILITY_REPORT_FILE}\"" in makefile
    assert "--webhook-url \"$${AIMANAGER_WECOM_WEBHOOK_URL}\"" in makefile
    assert "--min-severity \"$${AIMANAGER_WECOM_MIN_SEVERITY:-warning}\"" in makefile
    assert "--title \"AiManager production readiness alerts\"" in makefile
    assert "--output-payload-file /tmp/aimanager-wecom-alert-payload.json" in makefile


def test_makefile_exposes_admin_boundary_operator_target() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "admin-boundary-smoke:" in makefile
    assert "aimanager.scripts.smoke_admin_boundary" in makefile
    assert "--business-base-url \"$${AIMANAGER_BUSINESS_BASE_URL}\"" in makefile
    assert "--public-admin-url \"$${AIMANAGER_PUBLIC_ADMIN_URL}\"" in makefile
    assert "AIMANAGER_ALLOWED_SSO_REDIRECT_HOSTS=\"$${AIMANAGER_ALLOWED_SSO_REDIRECT_HOSTS:-}\"" in makefile
    assert "--require-business-base-url" in makefile
    assert "--require-public-admin-url" in makefile
    assert "<sso-host>" not in makefile


def test_makefile_exposes_remaining_external_evidence_operator_targets() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "live-ycapi-preflight:" in makefile
    assert "aimanager.scripts.smoke_live_ycapi" in makefile
    assert "--expect-model gemini-2.5-flash" in makefile
    assert "--expect-model deepseek-chat" in makefile
    assert "--expect-model ycapi-image-1" in makefile

    assert "work-context-enforcement-smoke:" in makefile
    assert "aimanager.scripts.smoke_work_context_enforcement" in makefile
    assert "--base-url \"$${AIMANAGER_BUSINESS_BASE_URL:-http://localhost:4000}\"" in makefile
    assert "--employee-key \"$${AIMANAGER_EMPLOYEE_VIRTUAL_KEY}\"" not in makefile

    assert "production-policy-readiness:" in makefile
    assert "AIMANAGER_PRODUCTION_POLICY_ATTESTATION_FILE" in makefile
    assert "aimanager.scripts.production_readiness_bundle" in makefile

    assert "employee-monitoring-validate:" in makefile
    assert "aimanager.scripts.validate_employee_monitoring_policy" in makefile
    assert "--policy-file \"$${AIMANAGER_EMPLOYEE_MONITORING_POLICY_FILE:-docs/aimanager/aimanager-employee-monitoring-policy.json}\"" in makefile
    assert "--employee-roster-file \"$${AIMANAGER_EMPLOYEE_ROSTER_FILE}\"" in makefile
    assert "--acknowledgment-file \"$${AIMANAGER_EMPLOYEE_ACKNOWLEDGMENT_FILE}\"" in makefile
    assert "--output-json-file \"$${AIMANAGER_EMPLOYEE_MONITORING_RESULT_FILE:-/tmp/aimanager-employee-monitoring.json}\"" in makefile

    assert "lightweight-trial-evidence-capture:" in makefile
    assert "aimanager.scripts.capture_lightweight_trial_evidence" in makefile
    assert "--lightweight-entry-result-file \"$${AIMANAGER_LIGHTWEIGHT_ENTRY_RESULT_FILE:-/tmp/aimanager-lightweight-entry-submit.json}\"" in makefile
    assert "--request-id \"$${AIMANAGER_LIGHTWEIGHT_TRIAL_REQUEST_ID}\"" in makefile
    assert "--spend \"$${AIMANAGER_LIGHTWEIGHT_TRIAL_SPEND}\"" in makefile
    assert "--employee-virtual-key-alias \"$${AIMANAGER_LIGHTWEIGHT_TRIAL_KEY_ALIAS}\"" in makefile
    assert "--output-json-file \"$${AIMANAGER_LIGHTWEIGHT_TRIAL_EVIDENCE_FILE:-/tmp/aimanager-ac23-trial-evidence.json}\"" in makefile
    assert "BLOCKED AC-23" in makefile


def test_lightweight_trial_make_target_blocks_before_capture_without_human_confirmations(tmp_path: Path) -> None:
    entry_result = tmp_path / "entry-result.json"
    entry_result.write_text("{}", encoding="utf-8")
    completed = subprocess.run(
        ["make", "lightweight-trial-evidence-capture"],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        capture_output=True,
        env=_lightweight_trial_make_env(
            tmp_path,
            entry_result_file=entry_result,
            confirmations={},
        ),
    )

    assert completed.returncode == 2
    assert "BLOCKED AC-23" in completed.stderr
    assert "lightweight trial evidence:" not in completed.stdout


def test_lightweight_trial_make_target_passes_confirmation_flags_only_when_all_confirmed(tmp_path: Path) -> None:
    entry_result = tmp_path / "entry-result.json"
    entry_result.write_text("{}", encoding="utf-8")

    completed = subprocess.run(
        ["make", "lightweight-trial-evidence-capture"],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        capture_output=True,
        env=_lightweight_trial_make_env(
            tmp_path,
            entry_result_file=entry_result,
            confirmations={name: "true" for name in LIGHTWEIGHT_TRIAL_CONFIRMATION_ENV},
        ),
    )

    assert completed.returncode == 2
    assert "BLOCKED AC-23" not in completed.stderr
    assert "lightweight entry submission did not PASS" in completed.stdout


def _lightweight_trial_make_env(
    tmp_path: Path,
    *,
    entry_result_file: Path,
    confirmations: dict[str, str],
) -> dict[str, str]:
    env = os.environ.copy()
    for name in LIGHTWEIGHT_TRIAL_CONFIRMATION_ENV:
        env.pop(name, None)
    env.update(confirmations)
    env.update(
        {
            "AIMANAGER_LIGHTWEIGHT_ENTRY_RESULT_FILE": str(entry_result_file),
            "AIMANAGER_LIGHTWEIGHT_TRIAL_EVIDENCE_FILE": str(tmp_path / "trial-evidence.json"),
            "AIMANAGER_LIGHTWEIGHT_TRIAL_REQUEST_ID": "req-test",
            "AIMANAGER_LIGHTWEIGHT_TRIAL_SPEND": "0.01",
            "AIMANAGER_LIGHTWEIGHT_TRIAL_OPERATOR_ROLE": "marketing",
            "AIMANAGER_LIGHTWEIGHT_TRIAL_IDENTITY_SOURCE": "sso",
            "AIMANAGER_LIGHTWEIGHT_TRIAL_KEY_ALIAS": "employee-key-alias",
            "AIMANAGER_LIGHTWEIGHT_TRIAL_STARTED_AT": "2026-07-01T00:00:00Z",
            "AIMANAGER_LIGHTWEIGHT_TRIAL_COMPLETED_AT": "2026-07-01T00:01:00Z",
            "AIMANAGER_LIGHTWEIGHT_TRIAL_OBSERVER": "ops",
            "AIMANAGER_LIGHTWEIGHT_TRIAL_CAPTURED_AT": "2026-07-01T00:01:30Z",
        }
    )
    return env

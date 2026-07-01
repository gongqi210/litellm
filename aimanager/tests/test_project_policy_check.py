from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
POLICY_SCRIPT = PROJECT_ROOT / "scripts" / "project_policy_check.py"
CLI_MAIN = PROJECT_ROOT / "cli" / "main.py"
MAKEFILE = PROJECT_ROOT / "Makefile"


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
    assert "production_readiness" in payload["commands"]
    assert "acceptance_gate" in payload["commands"]


def test_makefile_exposes_finance_export_operator_target() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "finance-export:" in makefile
    assert "aimanager.scripts.export_finance" in makefile
    assert "--spend-file \"$${AIMANAGER_SPEND_FILE}\"" in makefile
    assert "--ycapi-bill-file \"$${AIMANAGER_YCAPI_BILL_FILE}\"" in makefile
    assert "--output-dir \"$${AIMANAGER_FINANCE_OUTPUT_DIR:-/tmp/aimanager-finance-export}\"" in makefile

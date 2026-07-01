from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

TOKEN_ENV_NAMES = frozenset({"LITELLM_MASTER_KEY", "YCAPI_API_TOKEN"})


def build_gap_template_bindings(
    gaps: Sequence[Mapping[str, object]], *, output_dir: Path | None = None
) -> list[dict[str, object]]:
    defaults = evidence_env_defaults(output_dir)
    bindings: list[dict[str, object]] = []
    for gap in gaps:
        required_env = _string_list(gap.get("required_env"))
        template_files: list[str] = []
        secret_env: list[str] = []
        manual_env: list[str] = []
        preset_env: list[str] = []
        for env_name in required_env:
            if env_name in TOKEN_ENV_NAMES:
                secret_env.append(env_name)
                continue
            value = str(defaults.get(env_name, ""))
            if not value:
                manual_env.append(env_name)
                continue
            relative_template = _relative_output_path(value, output_dir=output_dir)
            if relative_template:
                template_files.append(relative_template)
            else:
                preset_env.append(f"{env_name}={value}")
        bindings.append(
            {
                "gap_id": str(gap.get("id") or "UNKNOWN"),
                "owner": str(gap.get("owner") or "project_owner"),
                "status": str(gap.get("status") or "FAIL"),
                "command": str(gap.get("rerun_command") or gap.get("command") or ""),
                "required_env": required_env,
                "template_files": _dedupe(template_files),
                "secret_env": secret_env,
                "manual_env": manual_env,
                "preset_env": preset_env,
                "required_files": _string_list(gap.get("required_files")),
            }
        )
    return bindings


def evidence_env_defaults(output_dir: Path | None = None) -> Mapping[str, object]:
    def template_path(relative_path: str) -> Path:
        path = Path(relative_path)
        return output_dir / path if output_dir is not None else path

    return {
        "AIMANAGER_SPEND_FILE": template_path("templates/finance/aimanager-spend.template.csv"),
        "AIMANAGER_YCAPI_BILL_FILE": template_path("templates/finance/ycapi-bill.template.csv"),
        "AIMANAGER_KEY_INVENTORY_FILE": template_path("templates/security-ops/key-inventory.template.json"),
        "AIMANAGER_OBSERVABILITY_REPORT_FILE": template_path("templates/ops/observability-report.template.json"),
        "AIMANAGER_LIGHTWEIGHT_TRIAL_EVIDENCE_FILE": template_path(
            "templates/business-trial/ac23-trial-evidence.template.json"
        ),
        "AIMANAGER_LIGHTWEIGHT_ENTRY_RESULT_FILE": "/tmp/aimanager-lightweight-entry-submit.json",
        "AIMANAGER_LIGHTWEIGHT_TRIAL_REQUEST_ID": "",
        "AIMANAGER_LIGHTWEIGHT_TRIAL_SPEND": "",
        "AIMANAGER_LIGHTWEIGHT_TRIAL_OPERATOR_ROLE": "marketing",
        "AIMANAGER_LIGHTWEIGHT_TRIAL_IDENTITY_SOURCE": "sso",
        "AIMANAGER_LIGHTWEIGHT_TRIAL_KEY_ALIAS": "",
        "AIMANAGER_LIGHTWEIGHT_TRIAL_STARTED_AT": "",
        "AIMANAGER_LIGHTWEIGHT_TRIAL_COMPLETED_AT": "",
        "AIMANAGER_LIGHTWEIGHT_TRIAL_OBSERVER": "",
        "AIMANAGER_LIGHTWEIGHT_TRIAL_CAPTURED_AT": "",
        "AIMANAGER_LIGHTWEIGHT_TRIAL_LIVE_YCAPI_CONFIRMED": "false",
        "AIMANAGER_LIGHTWEIGHT_TRIAL_BRAND_SAFETY_CONFIRMED": "false",
        "AIMANAGER_LIGHTWEIGHT_TRIAL_NO_SECRET_ECHO_CONFIRMED": "false",
        "AIMANAGER_LIGHTWEIGHT_TRIAL_HTML_ESCAPED_CONFIRMED": "false",
        "AIMANAGER_EMPLOYEE_MONITORING_POLICY_FILE": Path(
            "docs/aimanager/aimanager-employee-monitoring-policy.json"
        ),
        "AIMANAGER_EMPLOYEE_ROSTER_FILE": template_path(
            "templates/hr-legal-security/employee-roster.template.csv"
        ),
        "AIMANAGER_EMPLOYEE_ACKNOWLEDGMENT_FILE": template_path(
            "templates/hr-legal-security/employee-acknowledgments.template.csv"
        ),
        "AIMANAGER_PRODUCTION_POLICY_ATTESTATION_FILE": template_path(
            "templates/policy/production-policy-attestation.template.json"
        ),
    }


def _relative_output_path(value: str, *, output_dir: Path | None) -> str:
    path = Path(value)
    if output_dir is None:
        text = str(path)
        return text if not path.is_absolute() and text.startswith("templates/") else ""
    try:
        return str(path.relative_to(output_dir))
    except ValueError:
        return ""


def _string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    if isinstance(value, tuple):
        return [str(item) for item in value if str(item)]
    if isinstance(value, str) and value:
        return [value]
    return []


def _dedupe(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped

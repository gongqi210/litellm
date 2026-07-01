from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping, Sequence

from aimanager.redaction import sanitize_text as sanitize_secret_text
from aimanager.redaction import sanitize_value as sanitize_secret_value
from aimanager.scripts.generate_evidence_handoff import collect_evidence_handoff

_EXIT_CODES = {"PASS": 0, "FAIL": 1, "BLOCKED": 2}
_DEFAULT_LAUNCH_GAP_PLAN_FILE = Path("/tmp/aimanager-acceptance-gate/launch-gap-plan.json")
_DEFAULT_OUTPUT_DIR = Path("/tmp/aimanager-evidence-template-pack")
_TOKEN_ENV_NAMES = {"LITELLM_MASTER_KEY", "YCAPI_API_TOKEN"}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate safe AiManager external evidence input templates.")
    parser.add_argument("--launch-gap-plan-file", type=Path, default=_DEFAULT_LAUNCH_GAP_PLAN_FILE)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    parser.add_argument("--generated-at")
    args = parser.parse_args(argv)

    try:
        result = collect_evidence_template_pack(
            launch_gap_plan_file=args.launch_gap_plan_file,
            output_dir=args.output_dir,
            generated_at=args.generated_at,
        )
        status = str(result["status"])
        summary = _mapping(result["summary"])
        print(
            f"{status} evidence template pack: "
            f"templates={summary.get('templates', 0)} output={args.output_dir}"
        )
        return _EXIT_CODES.get(status, 1)
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        print(f"FAIL evidence template pack: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def collect_evidence_template_pack(
    *,
    launch_gap_plan_file: Path,
    output_dir: Path,
    generated_at: str | None = None,
) -> dict[str, object]:
    generated = generated_at or _now_iso()
    output_dir.mkdir(parents=True, exist_ok=True)
    handoff = collect_evidence_handoff(
        launch_gap_plan_file=launch_gap_plan_file,
        output_dir=output_dir / "handoff",
        generated_at=generated,
    )
    gaps = _gaps(handoff)
    files: list[dict[str, object]] = []

    _write_text(output_dir / "README.md", _readme(gaps=gaps, source=launch_gap_plan_file))
    files.append(_file_record(output_dir, output_dir / "README.md", "operator_readme", "all"))

    env_file = output_dir / "evidence-env.template"
    _write_text(env_file, _env_template(gaps, output_dir=output_dir))
    files.append(_file_record(output_dir, env_file, "env_template", "all"))

    for writer in (
        _key_inventory_templates,
        _finance_templates,
        _ops_templates,
        _business_trial_templates,
        _employee_monitoring_templates,
        _policy_templates,
    ):
        files.extend(writer(gaps, output_dir=output_dir))

    result = {
        "status": str(handoff["status"]),
        "generated_at": generated,
        "source": str(launch_gap_plan_file),
        "summary": _summary(files=files, gaps=gaps),
        "files": files,
        "gaps": gaps,
    }
    result["markdown"] = _manifest_markdown(result)
    sanitized = sanitize_secret_value(result)
    _write_json(output_dir / "evidence-template-pack.json", sanitized)
    _write_text(output_dir / "evidence-template-pack.md", str(sanitized["markdown"]))
    return sanitized


def _key_inventory_templates(gaps: Sequence[Mapping[str, object]], *, output_dir: Path) -> list[dict[str, object]]:
    if not _has_gap(gaps, "AC-08-KEY-INVENTORY"):
        return []
    inventory_file = output_dir / "templates" / "security-ops" / "key-inventory.template.json"
    _write_json(
        inventory_file,
        {
            "template_marker": "TEMPLATE_DO_NOT_SUBMIT",
            "exported_at": "2026-07-01T00:00:00+08:00",
            "export_source": "replace-with-litellm-production-export-source",
            "export_scope": "all_virtual_keys",
            "exported_by": "replace-with-export-operator",
            "expected_total_key_count": 1,
            "keys": [
                {
                    "key_alias": "replace-with-key-alias-not-raw-key",
                    "blocked": False,
                    "user_id": "replace-with-real-employee-or-system-id",
                    "team_id": "replace-with-real-team-id",
                    "models": ["gemini-2.5-flash"],
                    "max_budget": 0,
                    "rpm_limit": 0,
                    "tpm_limit": 0,
                    "duration": "replace-with-real-key-duration-or-expiry-policy",
                    "metadata": {
                        "owner": "replace-with-owner",
                        "department_id": "replace-with-department-id",
                        "project_id": "replace-with-project-id",
                        "cost_center_id": "replace-with-cost-center-id",
                        "scenario_l1": "marketing",
                        "scenario_l2": "campaign_brief",
                        "approver": "replace-with-approver",
                        "internal_or_external": "internal",
                        "shared_key": False,
                        "enforced_params": [],
                    },
                }
            ],
            "note": "Replace with real LiteLLM virtual key metadata export. Do not include raw keys, tokens, headers, prompts, or responses.",
        },
    )
    return [_file_record(output_dir, inventory_file, "key_inventory_template", "security/ops")]


def _finance_templates(gaps: Sequence[Mapping[str, object]], *, output_dir: Path) -> list[dict[str, object]]:
    if not _has_gap(gaps, "AC-12-13-FINANCE"):
        return []
    template_dir = output_dir / "templates" / "finance"
    spend_file = template_dir / "aimanager-spend.template.csv"
    bill_file = template_dir / "ycapi-bill.template.csv"
    _write_csv(
        spend_file,
        [
            "startTime",
            "status",
            "model",
            "call_type",
            "user",
            "key_alias",
            "spend",
            "currency",
            "metadata",
        ],
    )
    _write_csv(
        bill_file,
        [
            "month",
            "model",
            "endpoint",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "image_count",
            "spend",
            "currency",
        ],
    )
    return [
        _file_record(output_dir, spend_file, "finance_spend_csv_template", "finance"),
        _file_record(output_dir, bill_file, "ycapi_bill_csv_template", "finance"),
    ]


def _ops_templates(gaps: Sequence[Mapping[str, object]], *, output_dir: Path) -> list[dict[str, object]]:
    if not _has_gap(gaps, "AC-16-WECOM"):
        return []
    report_file = output_dir / "templates" / "ops" / "observability-report.template.json"
    _write_json(
        report_file,
        {
            "template_marker": "TEMPLATE_DO_NOT_SUBMIT",
            "status": "TEMPLATE",
            "alerts": [],
            "note": "Replace with a real export_observability report containing at least one real alert.",
        },
    )
    return [_file_record(output_dir, report_file, "observability_report_template", "ops")]


def _business_trial_templates(gaps: Sequence[Mapping[str, object]], *, output_dir: Path) -> list[dict[str, object]]:
    if not _has_gap(gaps, "AC-23"):
        return []
    trial_file = output_dir / "templates" / "business-trial" / "ac23-trial-evidence.template.json"
    _write_json(
        trial_file,
        {
            "template_marker": "TEMPLATE_DO_NOT_SUBMIT",
            "trial_id": "replace-with-real-trial-id",
            "ac": "AC-23",
            "entry_channel": "lightweight_web",
            "operator": {
                "employee_id": "replace-with-real-employee-id",
                "department_id": "replace-with-real-department-id",
                "role": "marketing",
                "uses_sdk": False,
            },
            "identity_injection": {
                "source": "sso",
                "employee_virtual_key_used": True,
                "employee_virtual_key_alias": "replace-with-key-alias-not-raw-key",
                "ycapi_token_exposed": False,
            },
            "work_context": {
                "scenario_l1": "marketing",
                "scenario_l2": "external_copy_review",
                "project_id": "replace-with-real-project-id",
                "customer_id": "",
                "cost_center_id": "replace-with-real-cost-center-id",
            },
            "request_evidence": {
                "request_id": "replace-with-real-request-id",
                "model": "gemini-2.5-flash",
                "endpoint": "/v1/chat/completions",
                "http_status": 0,
                "spend": "0",
                "currency": "CNY",
                "pricing_version": "replace-with-real-pricing-version",
            },
            "timing": {
                "started_at": "2026-07-01T00:00:00+08:00",
                "completed_at": "2026-07-01T00:00:00+08:00",
            },
            "compliance": {
                "work_context_status": "PASS",
                "brand_safety_confirmed": False,
                "no_secret_echo": False,
                "html_escaped": False,
            },
            "live_ycapi": False,
            "attestation": {
                "observer": "replace-with-real-observer",
                "captured_at": "2026-07-01T00:00:00+08:00",
            },
        },
    )
    return [_file_record(output_dir, trial_file, "ac23_trial_evidence_template", "business_owner/market/ops")]


def _employee_monitoring_templates(gaps: Sequence[Mapping[str, object]], *, output_dir: Path) -> list[dict[str, object]]:
    if not _has_gap(gaps, "AC-26"):
        return []
    template_dir = output_dir / "templates" / "hr-legal-security"
    roster_file = template_dir / "employee-roster.template.csv"
    ack_file = template_dir / "employee-acknowledgments.template.csv"
    _write_csv(roster_file, ["employee_id", "department_id", "status"])
    _write_csv(ack_file, ["employee_id", "policy_id", "policy_version", "acknowledged_at"])
    return [
        _file_record(output_dir, roster_file, "employee_roster_template", "HR/legal/security"),
        _file_record(output_dir, ack_file, "employee_acknowledgment_template", "HR/legal/security"),
    ]


def _policy_templates(gaps: Sequence[Mapping[str, object]], *, output_dir: Path) -> list[dict[str, object]]:
    if not _has_gap(gaps, "AC-POLICY"):
        return []
    policy_file = output_dir / "templates" / "policy" / "production-policy-attestation.template.json"
    _write_json(
        policy_file,
        {
            "template_marker": "TEMPLATE_DO_NOT_SUBMIT",
            "policy_id": "replace-with-real-policy-id",
            "policy_version": "replace-with-real-policy-version",
            "approved_at": "",
            "chargeback": {
                "mode": "showback",
                "confirmed": False,
                "policy_ref": "",
                "effective_month": "",
            },
            "pricing_approval": {
                "confirmed": False,
                "pricing_version": "",
                "approval_ref": "",
                "approver": "",
            },
            "ycapi_token_limit": {
                "confirmed": False,
                "limit_ref": "",
                "monthly_budget_cny": "0",
                "rpm_limit": 0,
            },
            "employee_virtual_key_only": {
                "confirmed": False,
                "distribution_channel": "",
                "ycapi_token_visible_to_employee": True,
            },
            "data_boundaries": {
                "confirmed": False,
                "policy_ref": "",
                "categories": [],
            },
            "approvals": [
                {"role": "finance", "approver": "", "approval_ref": ""},
                {"role": "security", "approver": "", "approval_ref": ""},
                {"role": "legal", "approver": "", "approval_ref": ""},
            ],
        },
    )
    return [_file_record(output_dir, policy_file, "production_policy_attestation_template", "general_manager/finance/security/legal")]


def _env_template(gaps: Sequence[Mapping[str, object]], *, output_dir: Path) -> str:
    env_names = sorted({env for gap in gaps for env in _string_list(gap.get("required_env"))})
    lines = [
        "# AiManager external evidence env template",
        "# TEMPLATE_DO_NOT_SUBMIT: fill with real production evidence paths and secrets outside git.",
        "# Do not paste raw prompts, raw responses, employee virtual keys, ycapi tokens, cookies, or customer content.",
        "",
    ]
    file_defaults = {
        "AIMANAGER_SPEND_FILE": output_dir / "templates/finance/aimanager-spend.template.csv",
        "AIMANAGER_YCAPI_BILL_FILE": output_dir / "templates/finance/ycapi-bill.template.csv",
        "AIMANAGER_KEY_INVENTORY_FILE": output_dir / "templates/security-ops/key-inventory.template.json",
        "AIMANAGER_OBSERVABILITY_REPORT_FILE": output_dir / "templates/ops/observability-report.template.json",
        "AIMANAGER_LIGHTWEIGHT_TRIAL_EVIDENCE_FILE": output_dir
        / "templates/business-trial/ac23-trial-evidence.template.json",
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
        "AIMANAGER_EMPLOYEE_MONITORING_POLICY_FILE": Path("docs/aimanager/aimanager-employee-monitoring-policy.json"),
        "AIMANAGER_EMPLOYEE_ROSTER_FILE": output_dir / "templates/hr-legal-security/employee-roster.template.csv",
        "AIMANAGER_EMPLOYEE_ACKNOWLEDGMENT_FILE": output_dir
        / "templates/hr-legal-security/employee-acknowledgments.template.csv",
        "AIMANAGER_PRODUCTION_POLICY_ATTESTATION_FILE": output_dir
        / "templates/policy/production-policy-attestation.template.json",
    }
    for name in env_names:
        if name in _TOKEN_ENV_NAMES:
            lines.append(f"# {name} must be injected by a secret manager or secure shell, not written to this file.")
            continue
        value = str(file_defaults.get(name, ""))
        lines.append(f"export {name}=\"{value}\"")
    return sanitize_secret_text("\n".join(lines) + "\n")


def _readme(*, gaps: Sequence[Mapping[str, object]], source: Path) -> str:
    lines = [
        "# AiManager Evidence Template Pack",
        "",
        "These files are templates only. They are not acceptance evidence and must not be submitted unchanged.",
        "",
        f"- Source: `{source}`",
        f"- Open Gaps: {len(gaps)}",
        "",
        "Rules:",
        "",
        "- Replace every `TEMPLATE_DO_NOT_SUBMIT` and placeholder with real evidence.",
        "- Do not paste secrets, raw prompts, raw responses, employee keys, ycapi tokens, cookies, or customer content.",
        "- Re-run `make acceptance-gate` after filling real evidence paths and secure environment values.",
        "",
    ]
    if gaps:
        lines.extend(["Open gap ids:", ""])
        lines.extend(f"- {gap.get('id')} ({gap.get('owner')})" for gap in gaps)
        lines.append("")
    return sanitize_secret_text("\n".join(lines))


def _manifest_markdown(result: Mapping[str, object]) -> str:
    summary = _mapping(result.get("summary"))
    lines = [
        "# AiManager Evidence Template Pack",
        "",
        "Template pack only; this is not a PASS artifact.",
        "",
        f"- Status: {result.get('status')}",
        f"- Generated At: {result.get('generated_at')}",
        f"- Files: {summary.get('templates', 0)}",
        "",
        "| Kind | Owner | Path |",
        "| --- | --- | --- |",
    ]
    for item in _mapping_list(result.get("files")):
        lines.append(f"| {item.get('kind')} | {item.get('owner')} | `{item.get('path')}` |")
    return sanitize_secret_text("\n".join(lines) + "\n")


def _summary(*, files: Sequence[Mapping[str, object]], gaps: Sequence[Mapping[str, object]]) -> dict[str, int]:
    return {
        "templates": len(files),
        "gaps": len(gaps),
        "owners": len({str(gap.get("owner") or "") for gap in gaps if gap.get("owner")}),
    }


def _gaps(handoff: Mapping[str, object]) -> list[Mapping[str, object]]:
    return [
        gap
        for owner in _mapping_list(handoff.get("owners"))
        for gap in _mapping_list(owner.get("gaps"))
    ]


def _has_gap(gaps: Sequence[Mapping[str, object]], gap_id: str) -> bool:
    return any(str(gap.get("id")) == gap_id for gap in gaps)


def _file_record(output_dir: Path, path: Path, kind: str, owner: str) -> dict[str, object]:
    return {
        "kind": kind,
        "owner": owner,
        "path": str(path.relative_to(output_dir)),
        "template_only": True,
    }


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sanitize_secret_value(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sanitize_secret_text(content), encoding="utf-8")


def _write_csv(path: Path, headers: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file_obj:
        writer = csv.writer(file_obj)
        writer.writerow(headers)


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _mapping_list(value: object) -> list[Mapping[str, object]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _string_list(value: object) -> list[str]:
    return [str(item) for item in value if str(item)] if isinstance(value, list) else []


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())

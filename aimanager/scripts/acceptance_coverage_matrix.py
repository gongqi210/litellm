from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Mapping, Sequence

from aimanager.redaction import sanitize_value as sanitize_secret_value

Status = Literal["PASS", "FAIL", "BLOCKED"]
BundleSource = Literal["production_readiness", "business_trial"]

_EXIT_CODES: dict[str, int] = {"PASS": 0, "FAIL": 1, "BLOCKED": 2}
_STATUS_ORDER: dict[str, int] = {"FAIL": 0, "BLOCKED": 1, "PASS": 2}
_AC_PATTERN = re.compile(r"\b(?:AC-\d{2}|AC-POLICY)\b")


@dataclass(frozen=True)
class AcceptanceGate:
    criterion_id: str
    owner: str
    gate_kind: str
    local_artifacts: tuple[str, ...] = ()
    bundle_check_id: str | None = None
    bundle_source: BundleSource | None = None


DEFAULT_GATE_REGISTRY: tuple[AcceptanceGate, ...] = (
    AcceptanceGate("AC-01", "architecture", "local_test", ("aimanager/scripts/validate_config.py", "aimanager/tests/test_config.py")),
    AcceptanceGate(
        "AC-02",
        "architecture/security",
        "runtime_smoke",
        (
            "aimanager/scripts/smoke_blocked_routes.py",
            "aimanager/tests/test_policy.py",
            "aimanager/tests/test_key_disposition_guard.py",
            "aimanager/tests/test_blocked_route_smoke.py",
        ),
    ),
    AcceptanceGate("AC-03", "engineering", "runtime_smoke", ("aimanager/scripts/mock_ycapi.py", "aimanager/tests/test_mock_ycapi.py", "aimanager/tests/test_config.py")),
    AcceptanceGate("AC-04", "engineering/finance", "runtime_smoke", ("aimanager/scripts/mock_ycapi.py", "aimanager/scripts/smoke_spend_logs.py", "aimanager/tests/test_spend_log_smoke.py")),
    AcceptanceGate(
        "AC-05",
        "engineering/finance",
        "runtime_smoke",
        ("aimanager/scripts/mock_ycapi.py", "aimanager/scripts/smoke_spend_logs.py", "aimanager/tests/test_spend_log_smoke.py", "aimanager/tests/test_asgi_runtime.py"),
    ),
    AcceptanceGate(
        "AC-06",
        "engineering",
        "runtime_smoke",
        ("aimanager/scripts/smoke_sdk_compat.py", "aimanager/scripts/smoke_runtime_sdk_compat.py", "aimanager/tests/test_sdk_compat_smoke.py", "aimanager/tests/test_runtime_sdk_compat_smoke.py"),
    ),
    AcceptanceGate("AC-07", "engineering", "local_test", ("aimanager/tests/test_policy.py",)),
    AcceptanceGate(
        "AC-08",
        "architecture/security",
        "bundle_check",
        (
            "aimanager/governance.py",
            "aimanager/scripts/validate_key_inventory.py",
            "aimanager/tests/test_governance.py",
            "aimanager/tests/test_key_inventory.py",
            "aimanager/tests/test_admin_ui_key_creation_smoke.py",
        ),
        bundle_check_id="AC-08-KEY-INVENTORY",
        bundle_source="production_readiness",
    ),
    AcceptanceGate("AC-09", "finance/architecture", "runtime_smoke", ("aimanager/scripts/validate_config.py", "aimanager/tests/test_config.py", "aimanager/tests/test_spend_log_smoke.py")),
    AcceptanceGate("AC-10", "finance/engineering", "runtime_smoke", ("aimanager/scripts/smoke_budget_block.py", "aimanager/tests/test_budget_block_smoke.py")),
    AcceptanceGate(
        "AC-11",
        "security/engineering",
        "runtime_smoke",
        (
            "aimanager/scripts/smoke_key_lifecycle.py",
            "aimanager/tests/test_key_lifecycle_smoke.py",
            "aimanager/tests/test_key_disposition_guard.py",
            "aimanager/tests/test_audit.py",
        ),
    ),
    AcceptanceGate(
        "AC-12",
        "finance",
        "bundle_check",
        ("aimanager/finance.py", "aimanager/scripts/export_finance.py", "aimanager/tests/test_finance.py", "aimanager/tests/test_finance_export_script.py"),
        bundle_check_id="AC-12-13-FINANCE",
        bundle_source="production_readiness",
    ),
    AcceptanceGate(
        "AC-13",
        "finance",
        "bundle_check",
        ("aimanager/finance.py", "aimanager/scripts/export_finance.py", "aimanager/tests/test_finance.py", "aimanager/tests/test_finance_export_script.py"),
        bundle_check_id="AC-12-13-FINANCE",
        bundle_source="production_readiness",
    ),
    AcceptanceGate("AC-14", "security/architecture", "local_test", ("aimanager/policy.py", "aimanager/tests/test_policy.py", "aimanager/tests/test_config.py")),
    AcceptanceGate(
        "AC-15",
        "architecture/security/ops",
        "bundle_check",
        ("aimanager/scripts/smoke_admin_boundary.py", "aimanager/tests/test_admin_boundary_smoke.py", "aimanager/scripts/production_readiness_bundle.py"),
        bundle_check_id="AC-15",
        bundle_source="production_readiness",
    ),
    AcceptanceGate(
        "AC-16",
        "ops",
        "bundle_check",
        ("aimanager/observability.py", "aimanager/scripts/export_observability.py", "aimanager/scripts/route_observability_alerts.py", "aimanager/tests/test_observability.py", "aimanager/tests/test_observability_alert_route_script.py"),
        bundle_check_id="AC-16-WECOM",
        bundle_source="production_readiness",
    ),
    AcceptanceGate("AC-17", "engineering/ops", "runtime_smoke", ("aimanager/asgi.py", "aimanager/scripts/smoke_postgres_down.py", "aimanager/tests/test_policy.py", "aimanager/tests/test_postgres_down_smoke.py")),
    AcceptanceGate("AC-18", "security", "local_test", ("aimanager/.env.example", "aimanager/tests/test_config.py", "aimanager/tests/test_project_policy_check.py")),
    AcceptanceGate(
        "AC-19",
        "ops",
        "bundle_check",
        ("aimanager/scripts/smoke_live_ycapi.py", "aimanager/tests/test_live_ycapi_smoke.py", "aimanager/scripts/production_readiness_bundle.py"),
        bundle_check_id="AC-19",
        bundle_source="production_readiness",
    ),
    AcceptanceGate(
        "AC-20",
        "product/engineering/security/ops",
        "bundle_check",
        (
            "aimanager/work_context.py",
            "aimanager/scripts/validate_work_context.py",
            "aimanager/scripts/smoke_work_context_enforcement.py",
            "aimanager/tests/test_work_context.py",
            "aimanager/tests/test_policy_chat_body.py",
            "aimanager/tests/test_work_context_enforcement_smoke.py",
        ),
        bundle_check_id="AC-20",
        bundle_source="production_readiness",
    ),
    AcceptanceGate("AC-21", "product/marketing", "local_test", ("aimanager/work_context.py", "aimanager/tests/test_work_context.py")),
    AcceptanceGate("AC-22", "marketing/legal", "local_test", ("aimanager/work_context.py", "aimanager/tests/test_work_context.py")),
    AcceptanceGate(
        "AC-23",
        "product/marketing",
        "bundle_check",
        ("aimanager/lightweight_entry.py", "aimanager/lightweight_web.py", "aimanager/scripts/capture_lightweight_trial_evidence.py", "aimanager/tests/test_lightweight_entry.py", "aimanager/tests/test_lightweight_web.py", "aimanager/tests/test_lightweight_trial_evidence_capture.py"),
        bundle_check_id="AC-23",
        bundle_source="business_trial",
    ),
    AcceptanceGate("AC-24", "ceo/finance", "local_test", ("aimanager/business_overview.py", "aimanager/scripts/generate_business_overview.py", "aimanager/tests/test_business_overview.py")),
    AcceptanceGate("AC-25", "finance", "local_test", ("aimanager/monthly_close.py", "aimanager/scripts/generate_monthly_close_package.py", "aimanager/tests/test_monthly_close.py")),
    AcceptanceGate(
        "AC-26",
        "HR/legal/security",
        "bundle_check",
        ("aimanager/employee_monitoring.py", "aimanager/scripts/validate_employee_monitoring_policy.py", "aimanager/tests/test_employee_monitoring.py", "docs/aimanager/3_employee_monitoring_policy_v1.md"),
        bundle_check_id="AC-26",
        bundle_source="business_trial",
    ),
    AcceptanceGate(
        "AC-POLICY",
        "finance/security/legal",
        "bundle_check",
        ("aimanager/scripts/production_readiness_bundle.py", "aimanager/tests/test_production_readiness_bundle.py", "docs/aimanager/production_policy_attestation.example.json"),
        bundle_check_id="AC-POLICY",
        bundle_source="production_readiness",
    ),
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate an executable AiManager acceptance coverage matrix.")
    parser.add_argument("--acceptance-doc-file", default="docs/aimanager/1_acceptance_criteria.md")
    parser.add_argument("--project-directory", default=".")
    parser.add_argument("--production-readiness-file", help="JSON output from production_readiness_bundle.")
    parser.add_argument("--business-trial-file", help="JSON output from business_trial_acceptance_bundle.")
    parser.add_argument("--local-test-results-file", help="JSON output from run_acceptance_gate local acceptance tests.")
    parser.add_argument("--output-json-file", required=True)
    parser.add_argument("--output-markdown-file")
    parser.add_argument("--generated-at", help="Override generated_at timestamp for deterministic tests.")
    args = parser.parse_args(argv)

    result = collect_acceptance_coverage_matrix(
        acceptance_doc_file=Path(args.acceptance_doc_file),
        project_directory=Path(args.project_directory),
        production_readiness_file=Path(args.production_readiness_file) if args.production_readiness_file else None,
        business_trial_file=Path(args.business_trial_file) if args.business_trial_file else None,
        local_test_results_file=Path(args.local_test_results_file) if args.local_test_results_file else None,
        generated_at=args.generated_at,
    )
    _write_json(Path(args.output_json_file), result)
    if args.output_markdown_file:
        _write_text(Path(args.output_markdown_file), str(result.get("markdown") or ""))
    status = str(result["status"])
    summary = result["summary"]
    print(
        f"{status} acceptance coverage matrix: criteria={summary['criteria']} "
        f"fail={summary['FAIL']} blocked={summary['BLOCKED']}"
    )
    return _EXIT_CODES.get(status, 1)


def collect_acceptance_coverage_matrix(
    *,
    acceptance_doc_file: Path,
    project_directory: Path,
    production_readiness_file: Path | None = None,
    business_trial_file: Path | None = None,
    local_test_results_file: Path | None = None,
    generated_at: str | None = None,
    gate_registry: Sequence[AcceptanceGate] | None = None,
) -> dict[str, object]:
    registry = list(gate_registry or DEFAULT_GATE_REGISTRY)
    doc_ids, doc_error = _read_acceptance_ids(acceptance_doc_file)
    registry_ids = [gate.criterion_id for gate in registry]
    doc_coverage = {
        "doc_ids": doc_ids,
        "registry_ids": registry_ids,
        "missing_in_registry": sorted(set(doc_ids) - set(registry_ids), key=_criterion_sort_key),
        "missing_in_doc": sorted(set(registry_ids) - set(doc_ids), key=_criterion_sort_key),
    }
    bundles = {
        "production_readiness": _load_bundle(production_readiness_file),
        "business_trial": _load_bundle(business_trial_file),
    }
    local_test_results = _load_local_test_results(local_test_results_file)

    criteria = [
        _evaluate_gate(
            gate,
            project_directory=project_directory,
            bundles=bundles,
            local_test_results=local_test_results,
        )
        for gate in sorted(registry, key=lambda item: _criterion_sort_key(item.criterion_id))
    ]
    coverage_failures = []
    if doc_error:
        coverage_failures.append({"id": "DOC", "status": "FAIL", "detail": doc_error})
    if doc_coverage["missing_in_registry"]:
        coverage_failures.append(
            {
                "id": "DOC-REGISTRY",
                "status": "FAIL",
                "detail": "acceptance doc has criteria missing from DEFAULT_GATE_REGISTRY",
                "criteria": doc_coverage["missing_in_registry"],
            }
        )
    if doc_coverage["missing_in_doc"]:
        coverage_failures.append(
            {
                "id": "REGISTRY-DOC",
                "status": "FAIL",
                "detail": "DEFAULT_GATE_REGISTRY has criteria absent from acceptance doc",
                "criteria": doc_coverage["missing_in_doc"],
            }
        )
    coverage_failures.extend(_bundle_registry_failures(registry, bundles))

    statuses = [str(item["status"]) for item in criteria] + [str(item["status"]) for item in coverage_failures]
    result = {
        "status": _overall_status(statuses),
        "generated_at": generated_at or datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "summary": _summary(criteria, coverage_failures),
        "doc_coverage": doc_coverage,
        "local_test_results": _public_local_test_results(local_test_results),
        "coverage_failures": coverage_failures,
        "criteria": criteria,
    }
    result["markdown"] = _markdown(result)
    return _sanitize_value(result)


def _evaluate_gate(
    gate: AcceptanceGate,
    *,
    project_directory: Path,
    bundles: Mapping[str, Mapping[str, object]],
    local_test_results: Mapping[str, object],
) -> dict[str, object]:
    artifacts = [
        {
            "path": artifact,
            "exists": (project_directory / artifact).exists(),
        }
        for artifact in gate.local_artifacts
    ]
    missing_artifacts = [str(item["path"]) for item in artifacts if not item["exists"]]
    status: Status = "PASS"
    detail = "registered local artifacts are present"
    bundle_name = gate.bundle_source
    bundle_check_id = gate.bundle_check_id
    bundle_detail = ""
    local_test_detail = ""
    local_test_result: Mapping[str, object] | None = None
    sources = ["registry", "filesystem"]
    if missing_artifacts:
        status = "FAIL"
        detail = f"missing registered local artifacts: {', '.join(missing_artifacts)}"

    if gate.gate_kind in {"local_test", "runtime_smoke"} and local_test_results.get("supplied"):
        sources.append("local_test_results")
        load_status = _coerce_status(local_test_results.get("load_status"))
        if load_status != "PASS":
            status = _worst_status(status, load_status)
            local_test_detail = str(local_test_results.get("detail") or "local test results could not be loaded")
        else:
            result = _mapping(local_test_results.get("criteria_by_id")).get(gate.criterion_id)
            if not isinstance(result, Mapping):
                status = _worst_status(status, "FAIL")
                local_test_detail = f"local test result is absent for {gate.criterion_id}"
            else:
                local_test_result = result
                local_status = _coerce_status(result.get("status"))
                status = _worst_status(status, local_status)
                local_test_detail = str(result.get("detail") or f"local acceptance tests {local_status}")

    if bundle_name and bundle_check_id:
        sources.append(bundle_name)
        bundle = bundles[bundle_name]
        load_status = str(bundle.get("load_status") or "")
        if not bundle.get("supplied"):
            status = _worst_status(status, "BLOCKED")
            bundle_detail = f"{bundle_name} bundle was not supplied; run the corresponding gate before final acceptance"
        elif load_status != "PASS":
            blocked_or_failed = _coerce_status(load_status)
            status = _worst_status(status, blocked_or_failed)
            bundle_detail = str(bundle.get("detail") or f"{bundle_name} bundle could not be loaded")
        else:
            checks = _mapping(bundle.get("checks_by_id"))
            check = checks.get(bundle_check_id)
            if not isinstance(check, Mapping):
                status = _worst_status(status, "FAIL")
                bundle_detail = f"registered bundle check {bundle_check_id} is absent from supplied {bundle_name} bundle"
            else:
                check_status = _coerce_status(check.get("status"))
                status = _worst_status(status, check_status)
                bundle_detail = str(check.get("detail") or "")

    if local_test_detail and not missing_artifacts:
        detail = local_test_detail
    if bundle_detail and not missing_artifacts:
        detail = bundle_detail
    criterion = {
        "id": gate.criterion_id,
        "status": status,
        "owner": gate.owner,
        "gate_kind": gate.gate_kind,
        "bundle_source": bundle_name,
        "bundle_check_id": bundle_check_id,
        "status_count_key": bundle_check_id or gate.criterion_id,
        "detail": detail,
        "local_artifacts": artifacts,
        "missing_artifacts": missing_artifacts,
        "sources": sources,
    }
    if local_test_result is not None:
        criterion["local_test_result"] = {
            "status": _coerce_status(local_test_result.get("status")),
            "detail": str(local_test_result.get("detail") or ""),
            "targets": _string_list(local_test_result.get("targets")),
            "command": str(local_test_result.get("command") or ""),
            "returncode": int(local_test_result.get("returncode") or 0),
        }
    return criterion


def _load_bundle(path: Path | None) -> dict[str, object]:
    if path is None:
        return {
            "supplied": False,
            "load_status": "BLOCKED",
            "bundle_status": "BLOCKED",
            "checks_by_id": {},
            "detail": "bundle file was not supplied",
        }
    if not path.exists():
        return {
            "supplied": True,
            "load_status": "BLOCKED",
            "bundle_status": "BLOCKED",
            "checks_by_id": {},
            "detail": f"bundle file does not exist: {path}",
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "supplied": True,
            "load_status": "FAIL",
            "bundle_status": "FAIL",
            "checks_by_id": {},
            "detail": f"bundle could not be loaded: {type(exc).__name__}",
        }
    if not isinstance(data, Mapping):
        return {
            "supplied": True,
            "load_status": "FAIL",
            "bundle_status": "FAIL",
            "checks_by_id": {},
            "detail": "bundle root must be a JSON object",
        }
    checks = data.get("checks")
    if not isinstance(checks, list):
        return {
            "supplied": True,
            "load_status": "FAIL",
            "bundle_status": "FAIL",
            "checks_by_id": {},
            "detail": "bundle checks must be a list",
        }
    checks_by_id: dict[str, dict[str, object]] = {}
    for item in checks:
        if not isinstance(item, Mapping):
            continue
        check_id = str(item.get("id") or "")
        if not check_id:
            continue
        checks_by_id[check_id] = {
            "id": check_id,
            "name": str(item.get("name") or ""),
            "status": _coerce_status(item.get("status")),
            "detail": str(item.get("detail") or ""),
        }
    return {
        "supplied": True,
        "load_status": "PASS",
        "bundle_status": _coerce_status(data.get("status")),
        "checks_by_id": checks_by_id,
        "detail": "",
    }


def _load_local_test_results(path: Path | None) -> dict[str, object]:
    if path is None:
        return {
            "supplied": False,
            "load_status": "PASS",
            "results_status": "PASS",
            "criteria_by_id": {},
            "summary": {},
            "detail": "local test results file was not supplied",
        }
    if not path.exists():
        return {
            "supplied": True,
            "load_status": "BLOCKED",
            "results_status": "BLOCKED",
            "criteria_by_id": {},
            "summary": {},
            "detail": f"local test results file does not exist: {path}",
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "supplied": True,
            "load_status": "FAIL",
            "results_status": "FAIL",
            "criteria_by_id": {},
            "summary": {},
            "detail": f"local test results could not be loaded: {type(exc).__name__}",
        }
    if not isinstance(data, Mapping):
        return {
            "supplied": True,
            "load_status": "FAIL",
            "results_status": "FAIL",
            "criteria_by_id": {},
            "summary": {},
            "detail": "local test results root must be a JSON object",
        }
    criteria = data.get("criteria")
    if not isinstance(criteria, list):
        return {
            "supplied": True,
            "load_status": "FAIL",
            "results_status": "FAIL",
            "criteria_by_id": {},
            "summary": {},
            "detail": "local test results criteria must be a list",
        }
    criteria_by_id: dict[str, dict[str, object]] = {}
    for item in criteria:
        if not isinstance(item, Mapping):
            continue
        criterion_id = str(item.get("id") or "")
        if not criterion_id:
            continue
        returncode = int(item.get("returncode") or 0)
        status = _coerce_status(item.get("status"))
        criteria_by_id[criterion_id] = {
            "id": criterion_id,
            "status": "FAIL" if status == "PASS" and returncode != 0 else status,
            "detail": str(item.get("detail") or ""),
            "targets": _string_list(item.get("targets")),
            "command": str(item.get("command") or ""),
            "returncode": returncode,
        }
    return {
        "supplied": True,
        "load_status": "PASS",
        "results_status": _coerce_status(data.get("status")),
        "criteria_by_id": criteria_by_id,
        "summary": _mapping(data.get("summary")),
        "detail": "",
    }


def _public_local_test_results(results: Mapping[str, object]) -> dict[str, object]:
    return {
        "supplied": bool(results.get("supplied")),
        "load_status": _coerce_status(results.get("load_status")),
        "results_status": _coerce_status(results.get("results_status")),
        "summary": _mapping(results.get("summary")),
        "detail": str(results.get("detail") or ""),
    }


def _bundle_registry_failures(
    registry: Sequence[AcceptanceGate],
    bundles: Mapping[str, Mapping[str, object]],
) -> list[dict[str, object]]:
    registered_bundle_ids = {gate.bundle_check_id for gate in registry if gate.bundle_check_id}
    failures: list[dict[str, object]] = []
    for source, bundle in bundles.items():
        if not bundle.get("supplied") or bundle.get("load_status") != "PASS":
            continue
        checks = _mapping(bundle.get("checks_by_id"))
        unregistered = sorted(set(checks) - registered_bundle_ids, key=_criterion_sort_key)
        if unregistered:
            failures.append(
                {
                    "id": f"BUNDLE-REGISTRY-{source}",
                    "status": "FAIL",
                    "detail": f"{source} bundle emitted checks that are not registered in acceptance coverage",
                    "unregistered_checks": unregistered,
                }
            )
        bundle_status = _coerce_status(bundle.get("bundle_status"))
        mapped_statuses = [_coerce_status(item.get("status")) for check_id, item in checks.items() if check_id in registered_bundle_ids]
        if bundle_status != "PASS" and _overall_status(mapped_statuses) == "PASS":
            failures.append(
                {
                    "id": f"BUNDLE-STATUS-{source}",
                    "status": bundle_status,
                    "detail": f"{source} bundle status is {bundle_status} but no registered check carries that status",
                }
            )
    return failures


def _read_acceptance_ids(path: Path) -> tuple[list[str], str | None]:
    if not path.exists():
        return [], f"acceptance doc file does not exist: {path}"
    text = path.read_text(encoding="utf-8")
    return sorted(set(_AC_PATTERN.findall(text)), key=_criterion_sort_key), None


def _summary(criteria: Sequence[Mapping[str, object]], coverage_failures: Sequence[Mapping[str, object]]) -> dict[str, int]:
    by_key: dict[str, str] = {}
    for item in criteria:
        key = str(item.get("status_count_key") or item.get("id"))
        status = _coerce_status(item.get("status"))
        current = by_key.get(key)
        if current is None or _STATUS_ORDER[status] < _STATUS_ORDER[current]:
            by_key[key] = status
    for failure in coverage_failures:
        by_key[str(failure["id"])] = _coerce_status(failure.get("status"))
    return {
        "criteria": len(criteria),
        "coverage_failures": len(coverage_failures),
        "PASS": sum(1 for status in by_key.values() if status == "PASS"),
        "FAIL": sum(1 for status in by_key.values() if status == "FAIL"),
        "BLOCKED": sum(1 for status in by_key.values() if status == "BLOCKED"),
    }


def _markdown(result: Mapping[str, object]) -> str:
    summary = _mapping(result.get("summary"))
    lines = [
        "# AiManager Acceptance Coverage Matrix",
        "",
        f"- Status: `{result['status']}`",
        f"- Generated at: `{result['generated_at']}`",
        f"- Criteria: `{summary.get('criteria', 0)}`",
        f"- Gate summary: PASS `{summary.get('PASS', 0)}`, FAIL `{summary.get('FAIL', 0)}`, BLOCKED `{summary.get('BLOCKED', 0)}`",
        "",
        "| AC | Status | Owner | Gate | Bundle check | Detail |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in result.get("criteria", []):
        if not isinstance(item, Mapping):
            continue
        lines.append(
            "| {id} | {status} | {owner} | {gate} | {bundle} | {detail} |".format(
                id=item.get("id", ""),
                status=item.get("status", ""),
                owner=item.get("owner", ""),
                gate=item.get("gate_kind", ""),
                bundle=item.get("bundle_check_id") or "-",
                detail=_markdown_cell(item.get("detail") or ""),
            )
        )
    failures = result.get("coverage_failures", [])
    if isinstance(failures, list) and failures:
        lines.extend(["", "## Coverage Failures", ""])
        for failure in failures:
            if isinstance(failure, Mapping):
                lines.append(f"- `{failure.get('id')}` {failure.get('status')}: {failure.get('detail')}")
    return "\n".join(lines) + "\n"


def _markdown_cell(value: object) -> str:
    return str(value).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>").replace("|", "\\|")


def _write_json(path: Path, data: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _criterion_sort_key(value: str) -> tuple[int, str]:
    if value == "AC-POLICY":
        return (10_000, value)
    match = re.fullmatch(r"AC-(\d{2})", value)
    if match:
        return (int(match.group(1)), value)
    return (20_000, value)


def _coerce_status(value: object) -> Status:
    return value if value in _EXIT_CODES else "FAIL"  # type: ignore[return-value]


def _worst_status(left: Status, right: Status) -> Status:
    return left if _STATUS_ORDER[left] <= _STATUS_ORDER[right] else right


def _overall_status(statuses: Sequence[str]) -> Status:
    if any(status == "FAIL" for status in statuses):
        return "FAIL"
    if any(status == "BLOCKED" for status in statuses):
        return "BLOCKED"
    return "PASS"


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    if isinstance(value, tuple):
        return [str(item) for item in value if str(item)]
    if isinstance(value, str) and value:
        return [value]
    return []


def _sanitize_value(value: object) -> object:
    return sanitize_secret_value(value)


if __name__ == "__main__":
    raise SystemExit(main())

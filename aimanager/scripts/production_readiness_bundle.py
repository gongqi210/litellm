from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Sequence

from aimanager.redaction import (
    SECRET_LIKE_REDACTION,
    build_secret_redactions,
    contains_secret_like,
    sanitize_text as sanitize_secret_text,
    sanitize_value as sanitize_secret_value,
)
from aimanager.scripts.export_finance import export_finance_csvs
from aimanager.scripts.route_observability_alerts import route_observability_alerts
from aimanager.scripts.smoke_admin_boundary import run_admin_boundary_smoke
from aimanager.scripts.smoke_live_ycapi import DEFAULT_YCAPI_BASE_URL, run_live_ycapi_smoke
from aimanager.scripts.smoke_work_context_enforcement import run_work_context_enforcement_smoke
from aimanager.scripts.validate_key_inventory import _parse_datetime, collect_key_inventory_validation


ReadinessStatus = Literal["PASS", "FAIL", "BLOCKED"]
DEFAULT_EXPECTED_MODELS = ("gemini-2.5-flash", "deepseek-chat", "ycapi-image-1")
DEFAULT_FINANCE_OUTPUT_DIR = "/tmp/aimanager-production-readiness-finance"
_POLICY_APPROVAL_MAX_AGE = timedelta(days=90)
_POLICY_APPROVAL_FUTURE_SKEW = timedelta(minutes=5)


@dataclass(frozen=True)
class CheckResult:
    id: str
    name: str
    status: ReadinessStatus
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)


AdminBoundaryRunner = Callable[..., Sequence[Any]]
LiveYcapiRunner = Callable[..., Any]
WeComRouter = Callable[..., Any]
FinanceRunner = Callable[..., CheckResult]
WorkContextRunner = Callable[..., Sequence[Any]]
_SECRET_LIKE_REDACTION = SECRET_LIKE_REDACTION


def collect_production_readiness(
    *,
    env: Mapping[str, str] | None = None,
    generated_at: str | None = None,
    admin_boundary_runner: AdminBoundaryRunner = run_admin_boundary_smoke,
    live_ycapi_runner: LiveYcapiRunner = run_live_ycapi_smoke,
    wecom_router: WeComRouter = route_observability_alerts,
    finance_runner: FinanceRunner | None = None,
    work_context_runner: WorkContextRunner = run_work_context_enforcement_smoke,
) -> dict[str, Any]:
    current_env = os.environ if env is None else env
    generated = generated_at or datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    redactions = _redactions(current_env)
    checks = [
        _admin_boundary_check(current_env, admin_boundary_runner=admin_boundary_runner),
        _work_context_enforcement_check(current_env, work_context_runner=work_context_runner),
        _live_ycapi_check(current_env, live_ycapi_runner=live_ycapi_runner),
        _key_inventory_check(current_env, generated_at=generated),
        _wecom_routing_check(current_env, wecom_router=wecom_router),
        (finance_runner or _finance_evidence_check)(env=current_env),
        _production_policy_attestation_check(current_env, generated_at=generated),
    ]
    sanitized_checks = [_sanitize_check(check, redactions) for check in checks]
    return {
        "status": _overall_status(check["status"] for check in sanitized_checks),
        "generated_at": generated,
        "summary": _summary(sanitized_checks),
        "checks": sanitized_checks,
    }


def _admin_boundary_check(env: Mapping[str, str], *, admin_boundary_runner: AdminBoundaryRunner) -> CheckResult:
    try:
        results = list(
            admin_boundary_runner(
                business_base_url=_env_value(env, "AIMANAGER_BUSINESS_BASE_URL") or None,
                public_admin_url=_env_value(env, "AIMANAGER_PUBLIC_ADMIN_URL") or None,
                allowed_sso_redirect_hosts=_split_csv(_env_value(env, "AIMANAGER_ALLOWED_SSO_REDIRECT_HOSTS")),
                require_business_base_url=True,
                require_public_admin_url=True,
            )
        )
    except Exception as exc:
        return CheckResult(
            id="AC-15",
            name="production_admin_boundary",
            status="FAIL",
            detail=f"admin boundary preflight failed: {type(exc).__name__}",
        )

    if not results:
        return CheckResult(
            id="AC-15",
            name="production_admin_boundary",
            status="BLOCKED",
            detail=(
                "missing AIMANAGER_BUSINESS_BASE_URL and AIMANAGER_PUBLIC_ADMIN_URL; "
                "cannot verify production admin boundary"
            ),
            evidence={"required_env": ["AIMANAGER_BUSINESS_BASE_URL", "AIMANAGER_PUBLIC_ADMIN_URL"]},
        )

    counts = _count_statuses(getattr(result, "status", "FAIL") for result in results)
    details = [str(getattr(result, "detail", "")) for result in results if getattr(result, "detail", "")]
    return CheckResult(
        id="AC-15",
        name="production_admin_boundary",
        status=_overall_status(getattr(result, "status", "FAIL") for result in results),
        detail=_compact_detail(details, default="admin boundary preflight completed"),
        evidence={
            "result_count": len(results),
            **counts,
            "surfaces": sorted({str(getattr(result, "surface", "")) for result in results if getattr(result, "surface", "")}),
            "results": [_admin_boundary_result_evidence(result) for result in results],
        },
    )


def _admin_boundary_result_evidence(result: object) -> dict[str, object]:
    return {
        "name": getattr(result, "name", ""),
        "surface": getattr(result, "surface", ""),
        "method": getattr(result, "method", ""),
        "path": getattr(result, "path", ""),
        "status": _coerce_status(getattr(result, "status", "FAIL")),
        "status_code": getattr(result, "status_code", None),
        "policy_code": getattr(result, "policy_code", None),
        "detail": str(getattr(result, "detail", "")),
    }


def _work_context_enforcement_check(
    env: Mapping[str, str], *, work_context_runner: WorkContextRunner
) -> CheckResult:
    required_env = ["AIMANAGER_BUSINESS_BASE_URL", "AIMANAGER_EMPLOYEE_VIRTUAL_KEY"]
    missing = [name for name in required_env if not _env_value(env, name)]
    if missing:
        return CheckResult(
            id="AC-20",
            name="production_work_context_enforcement",
            status="BLOCKED",
            detail=(
                f"missing {', '.join(missing)}; cannot verify production business surface "
                "rejects missing work-context metadata"
            ),
            evidence={"required_env": required_env},
        )

    try:
        results = list(
            work_context_runner(
                base_url=_env_value(env, "AIMANAGER_BUSINESS_BASE_URL"),
                employee_key=_env_value(env, "AIMANAGER_EMPLOYEE_VIRTUAL_KEY"),
                request_marker=_env_value(env, "AIMANAGER_WORK_CONTEXT_SMOKE_MARKER") or "production-readiness",
                run_valid_context_roundtrip=True,
            )
        )
    except Exception as exc:
        return CheckResult(
            id="AC-20",
            name="production_work_context_enforcement",
            status="FAIL",
            detail=f"work-context enforcement smoke failed: {type(exc).__name__}",
            evidence={"required_env": required_env},
        )

    if not results:
        return CheckResult(
            id="AC-20",
            name="production_work_context_enforcement",
            status="BLOCKED",
            detail="work-context enforcement smoke returned no results",
            evidence={"required_env": required_env, "result_count": 0},
        )

    statuses = ["PASS" if getattr(result, "passed", False) else "FAIL" for result in results]
    details = [str(getattr(result, "detail", "")) for result in results if getattr(result, "detail", "")]
    return CheckResult(
        id="AC-20",
        name="production_work_context_enforcement",
        status=_overall_status(statuses),
        detail=_compact_detail(details, default="work-context enforcement smoke completed"),
        evidence={
            "required_env": required_env,
            "result_count": len(results),
            **_count_statuses(statuses),
            "results": [_work_context_result_evidence(result) for result in results],
        },
    )


def _work_context_result_evidence(result: object) -> dict[str, object]:
    case = getattr(result, "case", None)
    return {
        "check_type": getattr(case, "check_type", "missing_context_block"),
        "method": getattr(case, "method", ""),
        "path": getattr(case, "path", ""),
        "model": getattr(case, "model", ""),
        "status": "PASS" if getattr(result, "passed", False) else "FAIL",
        "status_code": getattr(result, "status_code", None),
        "policy_code": getattr(result, "policy_code", None),
        "request_id": getattr(result, "request_id", None),
        "usage_present": getattr(result, "usage_present", False),
        "image_result_count": getattr(result, "image_result_count", 0),
        "work_context_present": getattr(result, "work_context_present", False),
        "detail": str(getattr(result, "detail", "")),
    }


def _live_ycapi_check(env: Mapping[str, str], *, live_ycapi_runner: LiveYcapiRunner) -> CheckResult:
    try:
        result = live_ycapi_runner(
            base_url=_env_value(env, "YCAPI_BASE_URL") or DEFAULT_YCAPI_BASE_URL,
            api_token=_env_value(env, "YCAPI_API_TOKEN"),
            token_env_name="YCAPI_API_TOKEN",
            expected_models=DEFAULT_EXPECTED_MODELS,
            run_inference_roundtrip=True,
        )
    except Exception as exc:
        return CheckResult(
            id="AC-19",
            name="live_ycapi_preflight",
            status="FAIL",
            detail=f"live ycapi preflight failed: {type(exc).__name__}",
        )
    return CheckResult(
        id="AC-19",
        name="live_ycapi_preflight",
        status=_coerce_status(getattr(result, "status", "FAIL")),
        detail=str(getattr(result, "detail", "live ycapi preflight completed")),
        evidence={
            "status_code": getattr(result, "status_code", None),
            "model_count": getattr(result, "model_count", 0),
            "expected_models": list(DEFAULT_EXPECTED_MODELS),
            "observed_models": list(getattr(result, "observed_models", ())),
            "inference_checked": getattr(result, "inference_checked", False),
            "chat_status_code": getattr(result, "chat_status_code", None),
            "image_status_code": getattr(result, "image_status_code", None),
            "chat_usage_present": getattr(result, "chat_usage_present", False),
            "image_result_count": getattr(result, "image_result_count", 0),
            "roundtrip_request_ids": list(getattr(result, "roundtrip_request_ids", ())),
            "required_env": ["YCAPI_API_TOKEN"],
        },
    )


def _key_inventory_check(env: Mapping[str, str], *, generated_at: str | None) -> CheckResult:
    try:
        result = collect_key_inventory_validation(
            env=env,
            generated_at=generated_at,
            require_acknowledged_employees=True,
        )
    except Exception as exc:
        return CheckResult(
            id="AC-08-KEY-INVENTORY",
            name="production_key_inventory_governance",
            status="FAIL",
            detail=f"key inventory validation failed: {type(exc).__name__}",
        )
    summary = _mapping(result.get("summary"))
    evidence: dict[str, Any] = {
        "total_key_count": summary.get("total_key_count", 0),
        "active_key_count": summary.get("active_key_count", 0),
        "blocked_key_count": summary.get("blocked_key_count", 0),
        "violation_count": summary.get("violation_count", 0),
    }
    required_env = _string_list(result.get("required_env"))
    if required_env:
        evidence["required_env"] = required_env
    violations = result.get("violations")
    if isinstance(violations, list) and violations:
        evidence["violations"] = violations[:12]
    export_metadata = result.get("export_metadata")
    if isinstance(export_metadata, dict):
        for field_name in ("exported_at", "export_source", "export_scope", "exported_by", "expected_total_key_count"):
            if field_name in export_metadata:
                evidence[field_name] = export_metadata[field_name]
    employee_acknowledgment = result.get("employee_acknowledgment")
    if isinstance(employee_acknowledgment, dict):
        evidence["employee_acknowledgment"] = dict(employee_acknowledgment)
    return CheckResult(
        id="AC-08-KEY-INVENTORY",
        name="production_key_inventory_governance",
        status=_coerce_status(result.get("status")),
        detail=str(result.get("detail") or "key inventory validation completed"),
        evidence=evidence,
    )


def _wecom_routing_check(env: Mapping[str, str], *, wecom_router: WeComRouter) -> CheckResult:
    report_path = _env_value(env, "AIMANAGER_OBSERVABILITY_REPORT_FILE")
    if not report_path:
        return CheckResult(
            id="AC-16-WECOM",
            name="wecom_alert_routing",
            status="BLOCKED",
            detail=(
                "missing AIMANAGER_OBSERVABILITY_REPORT_FILE and AIMANAGER_WECOM_WEBHOOK_URL; "
                "cannot produce live WeCom alert-routing evidence"
            ),
            evidence={
                "required_env": ["AIMANAGER_OBSERVABILITY_REPORT_FILE", "AIMANAGER_WECOM_WEBHOOK_URL"],
            },
        )

    try:
        report = _load_json_object(Path(report_path))
    except FileNotFoundError:
        return CheckResult(
            id="AC-16-WECOM",
            name="wecom_alert_routing",
            status="BLOCKED",
            detail=f"observability report file does not exist: {report_path}",
        )
    except Exception as exc:
        return CheckResult(
            id="AC-16-WECOM",
            name="wecom_alert_routing",
            status="FAIL",
            detail=f"observability report could not be loaded: {type(exc).__name__}",
        )

    alerts = report.get("alerts", [])
    if not isinstance(alerts, list):
        return CheckResult(
            id="AC-16-WECOM",
            name="wecom_alert_routing",
            status="FAIL",
            detail="observability report alerts must be a list",
        )
    if not alerts:
        return CheckResult(
            id="AC-16-WECOM",
            name="wecom_alert_routing",
            status="BLOCKED",
            detail="observability report has no alerts; live WeCom delivery evidence requires at least one alert",
        )

    webhook_url = _env_value(env, "AIMANAGER_WECOM_WEBHOOK_URL")
    if not webhook_url:
        return CheckResult(
            id="AC-16-WECOM",
            name="wecom_alert_routing",
            status="BLOCKED",
            detail="missing AIMANAGER_WECOM_WEBHOOK_URL; cannot produce live WeCom alert-routing evidence",
            evidence={"required_env": ["AIMANAGER_WECOM_WEBHOOK_URL"]},
        )

    try:
        result = wecom_router(
            report=report,
            webhook_url=webhook_url,
            dry_run=False,
            min_severity=_env_value(env, "AIMANAGER_WECOM_MIN_SEVERITY") or "warning",
            title="AiManager production readiness alerts",
        )
    except Exception as exc:
        return CheckResult(
            id="AC-16-WECOM",
            name="wecom_alert_routing",
            status="FAIL",
            detail=f"WeCom alert routing failed: {type(exc).__name__}",
        )

    delivered_count = getattr(result, "delivered_count", 0)
    if getattr(result, "status", "FAIL") == "PASS" and delivered_count <= 0:
        return CheckResult(
            id="AC-16-WECOM",
            name="wecom_alert_routing",
            status="BLOCKED",
            detail="WeCom router returned PASS with delivered_count=0; live delivery evidence is still missing",
            evidence={
                "alert_count": getattr(result, "alert_count", len(alerts)),
                "delivered_count": delivered_count,
                "required_env": ["AIMANAGER_WECOM_WEBHOOK_URL"],
            },
        )

    return CheckResult(
        id="AC-16-WECOM",
        name="wecom_alert_routing",
        status=_coerce_status(getattr(result, "status", "FAIL")),
        detail=str(getattr(result, "detail", "WeCom alert routing completed")),
        evidence={
            "alert_count": getattr(result, "alert_count", len(alerts)),
            "delivered_count": delivered_count,
            "required_env": ["AIMANAGER_WECOM_WEBHOOK_URL"],
        },
    )


def _finance_evidence_check(*, env: Mapping[str, str]) -> CheckResult:
    spend_file = _env_value(env, "AIMANAGER_SPEND_FILE")
    bill_file = _env_value(env, "AIMANAGER_YCAPI_BILL_FILE")
    missing = [name for name, value in (("AIMANAGER_SPEND_FILE", spend_file), ("AIMANAGER_YCAPI_BILL_FILE", bill_file)) if not value]
    if missing:
        return CheckResult(
            id="AC-12-13-FINANCE",
            name="finance_export_reconciliation",
            status="BLOCKED",
            detail=f"missing {', '.join(missing)}; cannot produce production finance reconciliation evidence",
            evidence={"required_env": ["AIMANAGER_SPEND_FILE", "AIMANAGER_YCAPI_BILL_FILE"]},
        )

    output_dir = Path(_env_value(env, "AIMANAGER_FINANCE_OUTPUT_DIR") or DEFAULT_FINANCE_OUTPUT_DIR)
    try:
        artifacts = export_finance_csvs(
            spend_file=Path(spend_file),
            ycapi_bill_file=Path(bill_file),
            output_dir=output_dir,
        )
    except FileNotFoundError as exc:
        return CheckResult(
            id="AC-12-13-FINANCE",
            name="finance_export_reconciliation",
            status="BLOCKED",
            detail=f"finance input file does not exist: {exc.filename}",
        )
    except Exception as exc:
        return CheckResult(
            id="AC-12-13-FINANCE",
            name="finance_export_reconciliation",
            status="FAIL",
            detail=f"finance export failed: {type(exc).__name__}",
        )

    if artifacts.spend_row_count == 0 or artifacts.ycapi_bill_row_count == 0:
        return CheckResult(
            id="AC-12-13-FINANCE",
            name="finance_export_reconciliation",
            status="BLOCKED",
            detail=(
                "finance input is empty; production evidence requires non-empty "
                "AIMANAGER_SPEND_FILE and AIMANAGER_YCAPI_BILL_FILE"
            ),
            evidence={
                "spend_row_count": artifacts.spend_row_count,
                "ycapi_bill_row_count": artifacts.ycapi_bill_row_count,
                "output_dir": str(output_dir),
                "output_files": artifacts.output_files,
            },
        )
    if artifacts.aimanager_billable_row_count == 0 or artifacts.ycapi_billable_row_count == 0:
        return CheckResult(
            id="AC-12-13-FINANCE",
            name="finance_export_reconciliation",
            status="BLOCKED",
            detail=(
                "finance input has no billable usage; production evidence requires non-zero "
                "AiManager spend and ycapi bill amounts"
            ),
            evidence={
                "spend_row_count": artifacts.spend_row_count,
                "ycapi_bill_row_count": artifacts.ycapi_bill_row_count,
                "aimanager_billable_row_count": artifacts.aimanager_billable_row_count,
                "ycapi_billable_row_count": artifacts.ycapi_billable_row_count,
                "output_dir": str(output_dir),
                "output_files": artifacts.output_files,
            },
        )
    if artifacts.reconciliation_needs_review_count > 0:
        return CheckResult(
            id="AC-12-13-FINANCE",
            name="finance_export_reconciliation",
            status="FAIL",
            detail=(
                f"finance reconciliation contains {artifacts.reconciliation_needs_review_count} "
                "needs_review row(s); resolve ycapi bill vs AiManager spend differences"
            ),
            evidence={
                "spend_row_count": artifacts.spend_row_count,
                "ycapi_bill_row_count": artifacts.ycapi_bill_row_count,
                "aimanager_billable_row_count": artifacts.aimanager_billable_row_count,
                "ycapi_billable_row_count": artifacts.ycapi_billable_row_count,
                "reconciliation_row_count": artifacts.reconciliation_row_count,
                "reconciliation_needs_review_count": artifacts.reconciliation_needs_review_count,
                "output_dir": str(output_dir),
                "output_files": artifacts.output_files,
            },
        )

    return CheckResult(
        id="AC-12-13-FINANCE",
        name="finance_export_reconciliation",
        status="PASS",
        detail=f"finance export wrote {len(artifacts.output_files)} file(s) to {output_dir}",
        evidence={
            "spend_row_count": artifacts.spend_row_count,
            "ycapi_bill_row_count": artifacts.ycapi_bill_row_count,
            "aimanager_billable_row_count": artifacts.aimanager_billable_row_count,
            "ycapi_billable_row_count": artifacts.ycapi_billable_row_count,
            "reconciliation_row_count": artifacts.reconciliation_row_count,
            "reconciliation_needs_review_count": artifacts.reconciliation_needs_review_count,
            "output_dir": str(output_dir),
            "output_files": artifacts.output_files,
        },
    )


def _production_policy_attestation_check(env: Mapping[str, str], *, generated_at: str | None = None) -> CheckResult:
    path = _env_value(env, "AIMANAGER_PRODUCTION_POLICY_ATTESTATION_FILE")
    if not path:
        return CheckResult(
            id="AC-POLICY",
            name="production_policy_attestation",
            status="BLOCKED",
            detail=(
                "missing AIMANAGER_PRODUCTION_POLICY_ATTESTATION_FILE; cannot verify showback/chargeback, "
                "pricing approval, ycapi token limits, employee virtual-key-only distribution, and data boundaries"
            ),
            evidence={"required_env": ["AIMANAGER_PRODUCTION_POLICY_ATTESTATION_FILE"]},
        )

    try:
        payload = _load_json_object(Path(path))
    except FileNotFoundError:
        return CheckResult(
            id="AC-POLICY",
            name="production_policy_attestation",
            status="BLOCKED",
            detail=f"production policy attestation file does not exist: {path}",
        )
    except Exception as exc:
        return CheckResult(
            id="AC-POLICY",
            name="production_policy_attestation",
            status="FAIL",
            detail=f"production policy attestation could not be loaded: {type(exc).__name__}",
        )

    if _contains_secret_like_value(json.dumps(payload, ensure_ascii=False)):
        return CheckResult(
            id="AC-POLICY",
            name="production_policy_attestation",
            status="FAIL",
            detail=f"production policy attestation contains {_SECRET_LIKE_REDACTION}",
            evidence={"redaction_marker": _SECRET_LIKE_REDACTION},
        )

    violations = production_policy_violations(payload, reference_time=generated_at)
    if violations:
        return CheckResult(
            id="AC-POLICY",
            name="production_policy_attestation",
            status="FAIL",
            detail="production policy attestation invalid: " + ", ".join(violations[:5]),
        )

    chargeback = _mapping(payload.get("chargeback"))
    token_limit = _mapping(payload.get("ycapi_token_limit"))
    employee_key = _mapping(payload.get("employee_virtual_key_only"))
    data_boundaries = _mapping(payload.get("data_boundaries"))
    approvals = _list_of_mappings(payload.get("approvals"))
    return CheckResult(
        id="AC-POLICY",
        name="production_policy_attestation",
        status="PASS",
        detail="production policy attestation validated",
        evidence={
            "policy_id": _text(payload.get("policy_id")),
            "policy_version": _text(payload.get("policy_version")),
            "approver_count": len(approvals),
            "chargeback_mode": _text(chargeback.get("mode")),
            "data_boundary_count": len(_string_list(data_boundaries.get("categories"))),
            "ycapi_token_limit_confirmed": token_limit.get("confirmed") is True,
            "employee_virtual_key_only": employee_key.get("confirmed") is True
            and employee_key.get("ycapi_token_visible_to_employee") is False,
        },
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect AiManager production readiness evidence into one JSON bundle.")
    parser.add_argument("--output-json-file", help="Optional path to write the production readiness JSON bundle.")
    args = parser.parse_args(argv)

    bundle = collect_production_readiness()
    if args.output_json_file:
        try:
            output_path = Path(args.output_json_file)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except Exception as exc:
            print(f"FAIL production readiness bundle: result file not written: {type(exc).__name__}", file=sys.stderr)
            return 1

    print(
        f"{bundle['status']} production readiness bundle: "
        f"pass={bundle['summary']['PASS']} fail={bundle['summary']['FAIL']} blocked={bundle['summary']['BLOCKED']}"
    )
    if bundle["status"] == "FAIL":
        return 1
    if bundle["status"] == "BLOCKED":
        return 2
    return 0


def _sanitize_check(check: CheckResult, redactions: Mapping[str, str]) -> dict[str, Any]:
    payload = asdict(check)
    return _sanitize_value(payload, redactions)


def _sanitize_value(value: Any, redactions: Mapping[str, str]) -> Any:
    return sanitize_secret_value(value, redactions)


def _sanitize_text(value: str, redactions: Mapping[str, str]) -> str:
    return sanitize_secret_text(value, redactions)


def _redactions(env: Mapping[str, str]) -> dict[str, str]:
    return build_secret_redactions(env)


def _summary(checks: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    return {status: sum(1 for check in checks if check["status"] == status) for status in ("PASS", "FAIL", "BLOCKED")}


def _count_statuses(statuses: Sequence[str] | Any) -> dict[str, int]:
    values = list(statuses)
    return {
        "pass_count": sum(1 for status in values if status == "PASS"),
        "fail_count": sum(1 for status in values if status == "FAIL"),
        "blocked_count": sum(1 for status in values if status == "BLOCKED"),
    }


def _overall_status(statuses: Sequence[str] | Any) -> ReadinessStatus:
    values = list(statuses)
    if any(status == "FAIL" for status in values):
        return "FAIL"
    if any(status == "BLOCKED" for status in values):
        return "BLOCKED"
    return "PASS"


def _coerce_status(status: Any) -> ReadinessStatus:
    if status in {"PASS", "FAIL", "BLOCKED"}:
        return status
    return "FAIL"


def _compact_detail(details: Sequence[str], *, default: str) -> str:
    if not details:
        return default
    if len(details) <= 2:
        return "; ".join(details)
    return f"{details[0]}; {details[1]}; ... {len(details) - 2} more"


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _env_value(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "")
    return value.strip() if isinstance(value, str) else ""


def _load_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def production_policy_violations(
    payload: Mapping[str, Any],
    *,
    reference_time: str | datetime | None = None,
    max_approval_age: timedelta = _POLICY_APPROVAL_MAX_AGE,
    future_skew: timedelta = _POLICY_APPROVAL_FUTURE_SKEW,
) -> list[str]:
    violations: list[str] = []
    for field_name in ("policy_id", "policy_version"):
        if not _text(payload.get(field_name)):
            violations.append(field_name)
    _approved_at_violations(
        payload.get("approved_at"),
        reference_time=reference_time,
        max_age=max_approval_age,
        future_skew=future_skew,
        violations=violations,
    )

    chargeback = _required_mapping(payload, "chargeback", violations)
    _require_true(chargeback, "chargeback.confirmed", violations)
    if _text(chargeback.get("mode")) not in {"showback", "chargeback"}:
        violations.append("chargeback.mode")
    _require_text(chargeback, "policy_ref", "chargeback.policy_ref", violations)
    _require_text(chargeback, "effective_month", "chargeback.effective_month", violations)

    pricing = _required_mapping(payload, "pricing_approval", violations)
    _require_true(pricing, "pricing_approval.confirmed", violations)
    _require_text(pricing, "pricing_version", "pricing_approval.pricing_version", violations)
    _require_text(pricing, "approval_ref", "pricing_approval.approval_ref", violations)
    _require_text(pricing, "approver", "pricing_approval.approver", violations)

    token_limit = _required_mapping(payload, "ycapi_token_limit", violations)
    _require_true(token_limit, "ycapi_token_limit.confirmed", violations)
    _require_text(token_limit, "limit_ref", "ycapi_token_limit.limit_ref", violations)
    if _positive_number(token_limit.get("monthly_budget_cny")) <= 0:
        violations.append("ycapi_token_limit.monthly_budget_cny")
    if _positive_number(token_limit.get("rpm_limit")) <= 0:
        violations.append("ycapi_token_limit.rpm_limit")

    employee_key = _required_mapping(payload, "employee_virtual_key_only", violations)
    _require_true(employee_key, "employee_virtual_key_only.confirmed", violations)
    _require_text(employee_key, "distribution_channel", "employee_virtual_key_only.distribution_channel", violations)
    if employee_key.get("ycapi_token_visible_to_employee") is not False:
        violations.append("employee_virtual_key_only.ycapi_token_visible_to_employee")

    data_boundaries = _required_mapping(payload, "data_boundaries", violations)
    _require_true(data_boundaries, "data_boundaries.confirmed", violations)
    _require_text(data_boundaries, "policy_ref", "data_boundaries.policy_ref", violations)
    if not _string_list(data_boundaries.get("categories")):
        violations.append("data_boundaries.categories")

    approvals = _list_of_mappings(payload.get("approvals"))
    if len(approvals) < 3:
        violations.append("approvals")
    approval_roles = {_text(approval.get("role")) for approval in approvals}
    for required_role in ("finance", "security", "legal"):
        if required_role not in approval_roles:
            violations.append(f"approvals.{required_role}")
    required_approver_identities = {
        _text(approval.get("role")): _normalized_identity(approval.get("approver"))
        for approval in approvals
        if _text(approval.get("role")) in {"finance", "security", "legal"}
    }
    if len({approver for approver in required_approver_identities.values() if approver}) < 3:
        violations.append("approvals.distinct_approvers")
    for index, approval in enumerate(approvals):
        for key in ("role", "approver", "approval_ref"):
            if not _text(approval.get(key)):
                violations.append(f"approvals[{index}].{key}")
    return violations


def _approved_at_violations(
    value: Any,
    *,
    reference_time: str | datetime | None,
    max_age: timedelta,
    future_skew: timedelta,
    violations: list[str],
) -> None:
    approved_at_text = _text(value)
    if not approved_at_text:
        violations.append("approved_at")
        return
    approved_at = _parse_datetime(approved_at_text)
    if approved_at is None:
        violations.append("approved_at.iso8601")
        return
    current_time = _reference_time(reference_time)
    if approved_at - current_time > future_skew:
        violations.append("approved_at.future")
        return
    if current_time - approved_at > max_age:
        violations.append("approved_at.stale")


def _reference_time(value: str | datetime | None) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    return _parse_datetime(value) or datetime.now(UTC)


def _required_mapping(payload: Mapping[str, Any], field_name: str, violations: list[str]) -> dict[str, Any]:
    value = payload.get(field_name)
    if not isinstance(value, Mapping):
        violations.append(field_name)
        return {}
    return dict(value)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list_of_mappings(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _require_true(payload: Mapping[str, Any], field_path: str, violations: list[str]) -> None:
    field_name = field_path.rsplit(".", 1)[-1]
    if payload.get(field_name) is not True:
        violations.append(field_path)


def _require_text(payload: Mapping[str, Any], key: str, field_path: str, violations: list[str]) -> None:
    if not _text(payload.get(key)):
        violations.append(field_path)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _normalized_identity(value: Any) -> str:
    return " ".join(_text(value).casefold().split())


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _positive_number(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else 0.0
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return 0.0
        return number if math.isfinite(number) else 0.0
    return 0.0


def _contains_secret_like_value(value: str) -> bool:
    return contains_secret_like(value)


if __name__ == "__main__":
    raise SystemExit(main())

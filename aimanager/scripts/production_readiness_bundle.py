from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Sequence

from aimanager.scripts.export_finance import export_finance_csvs
from aimanager.scripts.route_observability_alerts import route_observability_alerts
from aimanager.scripts.smoke_admin_boundary import run_admin_boundary_smoke
from aimanager.scripts.smoke_live_ycapi import DEFAULT_YCAPI_BASE_URL, run_live_ycapi_smoke


ReadinessStatus = Literal["PASS", "FAIL", "BLOCKED"]
DEFAULT_EXPECTED_MODELS = ("gemini-2.5-flash", "deepseek-chat", "ycapi-image-1")
DEFAULT_FINANCE_OUTPUT_DIR = "/tmp/aimanager-production-readiness-finance"


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


def collect_production_readiness(
    *,
    env: Mapping[str, str] | None = None,
    generated_at: str | None = None,
    admin_boundary_runner: AdminBoundaryRunner = run_admin_boundary_smoke,
    live_ycapi_runner: LiveYcapiRunner = run_live_ycapi_smoke,
    wecom_router: WeComRouter = route_observability_alerts,
    finance_runner: FinanceRunner | None = None,
) -> dict[str, Any]:
    current_env = os.environ if env is None else env
    redactions = _redactions(current_env)
    checks = [
        _admin_boundary_check(current_env, admin_boundary_runner=admin_boundary_runner),
        _live_ycapi_check(current_env, live_ycapi_runner=live_ycapi_runner),
        _wecom_routing_check(current_env, wecom_router=wecom_router),
        (finance_runner or _finance_evidence_check)(env=current_env),
    ]
    sanitized_checks = [_sanitize_check(check, redactions) for check in checks]
    return {
        "status": _overall_status(check["status"] for check in sanitized_checks),
        "generated_at": generated_at or datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
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
        },
    )


def _live_ycapi_check(env: Mapping[str, str], *, live_ycapi_runner: LiveYcapiRunner) -> CheckResult:
    try:
        result = live_ycapi_runner(
            base_url=_env_value(env, "YCAPI_BASE_URL") or DEFAULT_YCAPI_BASE_URL,
            api_token=_env_value(env, "YCAPI_API_TOKEN"),
            token_env_name="YCAPI_API_TOKEN",
            expected_models=DEFAULT_EXPECTED_MODELS,
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
            "required_env": ["YCAPI_API_TOKEN"],
        },
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
            "output_dir": str(output_dir),
            "output_files": artifacts.output_files,
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
    if isinstance(value, str):
        return _sanitize_text(value, redactions)
    if isinstance(value, list):
        return [_sanitize_value(item, redactions) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_value(item, redactions) for item in value]
    if isinstance(value, dict):
        return {str(key): _sanitize_value(item, redactions) for key, item in value.items()}
    return value


def _sanitize_text(value: str, redactions: Mapping[str, str]) -> str:
    sanitized = value
    for secret, replacement in redactions.items():
        if secret:
            sanitized = sanitized.replace(secret, replacement)
    return sanitized


def _redactions(env: Mapping[str, str]) -> dict[str, str]:
    redactions: dict[str, str] = {}
    for name in ("YCAPI_API_TOKEN", "AIMANAGER_WECOM_WEBHOOK_URL"):
        value = _env_value(env, name)
        if value:
            redactions[value] = f"[redacted:{name}]"
    return redactions


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


if __name__ == "__main__":
    raise SystemExit(main())

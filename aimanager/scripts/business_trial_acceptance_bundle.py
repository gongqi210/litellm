from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from aimanager.redaction import contains_secret_like
from aimanager.scripts.production_readiness_bundle import (
    DEFAULT_EXPECTED_MODELS,
    CheckResult,
    ReadinessStatus,
    _coerce_status,
    _env_value,
    _load_json_object,
    _overall_status,
    _redactions,
    _sanitize_check,
    _summary,
    collect_production_readiness,
)
from aimanager.scripts.validate_employee_monitoring_policy import _collect_result as collect_employee_monitoring_result


ProductionReadinessCollector = Callable[..., dict[str, Any]]
EmployeeMonitoringCollector = Callable[..., dict[str, object]]

ALLOWED_ENTRY_CHANNELS = {"wecom", "lightweight_web", "department_admin_form"}
ALLOWED_IDENTITY_SOURCES = {"server_side_default", "sso", "wecom_sso", "vpn_sso", "reverse_proxy_sso"}
ALLOWED_NONTECHNICAL_ROLES = {
    "business",
    "ceo",
    "department_admin",
    "finance",
    "general_manager",
    "marketing",
    "operations",
    "sales",
}
ALLOWED_ENDPOINTS = {"/v1/chat/completions", "/v1/images/generations"}
ALLOWED_SECRET_META_KEYS = {
    "employee_virtual_key_alias",
    "employee_virtual_key_used",
    "no_secret_echo",
    "ycapi_token_exposed",
}
FORBIDDEN_EVIDENCE_KEYS = {
    "api_key",
    "api_token",
    "authorization",
    "bearer",
    "content",
    "cookie",
    "customer_content",
    "employee_virtual_key",
    "messages",
    "prompt",
    "raw_ip",
    "raw_prompt",
    "raw_response",
    "response",
    "secret",
    "token",
    "ycapi_api_key",
    "ycapi_api_token",
}


class OperatorEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    employee_id: str
    department_id: str
    role: str
    uses_sdk: bool

    @field_validator("employee_id", "department_id", "role")
    @classmethod
    def _non_empty_string(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must be non-empty")
        return value

    @model_validator(mode="after")
    def _validate_operator(self) -> OperatorEvidence:
        if self.uses_sdk:
            raise ValueError("operator.uses_sdk must be false for AC-23 nontechnical trial")
        if self.role not in ALLOWED_NONTECHNICAL_ROLES:
            raise ValueError("operator.role must identify a nontechnical business role")
        return self


class IdentityInjectionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    employee_virtual_key_used: bool
    employee_virtual_key_alias: str
    ycapi_token_exposed: bool

    @field_validator("source", "employee_virtual_key_alias")
    @classmethod
    def _non_empty_string(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must be non-empty")
        return value

    @model_validator(mode="after")
    def _validate_identity(self) -> IdentityInjectionEvidence:
        if self.source not in ALLOWED_IDENTITY_SOURCES:
            raise ValueError("identity_injection.source must be server-side or SSO controlled")
        if not self.employee_virtual_key_used:
            raise ValueError("identity_injection.employee_virtual_key_used must be true")
        if self.ycapi_token_exposed:
            raise ValueError("identity_injection.ycapi_token_exposed must be false")
        return self


class WorkContextEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_l1: str
    scenario_l2: str
    project_id: str = ""
    customer_id: str = ""
    cost_center_id: str

    @field_validator("scenario_l1", "scenario_l2", "cost_center_id")
    @classmethod
    def _non_empty_string(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must be non-empty")
        return value

    @field_validator("project_id", "customer_id")
    @classmethod
    def _optional_string(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _validate_project_or_customer(self) -> WorkContextEvidence:
        if not self.project_id and not self.customer_id:
            raise ValueError("work_context must include project_id or customer_id")
        return self


class RequestEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    model: str
    endpoint: str
    http_status: int
    spend: Decimal
    currency: Literal["CNY"]
    pricing_version: str

    @field_validator("request_id", "model", "endpoint", "pricing_version")
    @classmethod
    def _non_empty_string(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must be non-empty")
        return value

    @model_validator(mode="after")
    def _validate_request(self) -> RequestEvidence:
        if self.model not in DEFAULT_EXPECTED_MODELS:
            raise ValueError("request_evidence.model must be one of the ycapi-backed allowed models")
        if self.endpoint not in ALLOWED_ENDPOINTS:
            raise ValueError("request_evidence.endpoint must be an allowed business endpoint")
        if self.http_status < 200 or self.http_status >= 300:
            raise ValueError("request_evidence.http_status must be 2xx")
        if self.spend <= 0:
            raise ValueError("request_evidence.spend must be greater than zero")
        return self


class TimingEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    started_at: datetime
    completed_at: datetime

    @model_validator(mode="after")
    def _validate_timing(self) -> TimingEvidence:
        if self.started_at.tzinfo is None or self.completed_at.tzinfo is None:
            raise ValueError("timing timestamps must include timezone")
        duration = self.duration_seconds
        if duration <= 0 or duration > 300:
            raise ValueError("timing.duration_seconds must be greater than 0 and no more than 300")
        return self

    @property
    def duration_seconds(self) -> int:
        return int((self.completed_at - self.started_at).total_seconds())


class ComplianceEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    work_context_status: Literal["PASS"]
    brand_safety_confirmed: bool
    no_secret_echo: bool
    html_escaped: bool

    @model_validator(mode="after")
    def _validate_compliance(self) -> ComplianceEvidence:
        if not self.brand_safety_confirmed:
            raise ValueError("compliance.brand_safety_confirmed must be true")
        if not self.no_secret_echo:
            raise ValueError("compliance.no_secret_echo must be true")
        if not self.html_escaped:
            raise ValueError("compliance.html_escaped must be true")
        return self


class AttestationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observer: str
    captured_at: datetime

    @field_validator("observer")
    @classmethod
    def _non_empty_string(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must be non-empty")
        return value

    @model_validator(mode="after")
    def _validate_attestation(self) -> AttestationEvidence:
        if self.captured_at.tzinfo is None:
            raise ValueError("attestation.captured_at must include timezone")
        return self


class TrialEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trial_id: str
    ac: Literal["AC-23"]
    entry_channel: str
    operator: OperatorEvidence
    identity_injection: IdentityInjectionEvidence
    work_context: WorkContextEvidence
    request_evidence: RequestEvidence
    timing: TimingEvidence
    compliance: ComplianceEvidence
    live_ycapi: bool
    attestation: AttestationEvidence

    @field_validator("trial_id", "entry_channel")
    @classmethod
    def _non_empty_string(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must be non-empty")
        return value

    @model_validator(mode="after")
    def _validate_trial(self) -> TrialEvidence:
        if self.entry_channel not in ALLOWED_ENTRY_CHANNELS:
            raise ValueError("entry_channel must be a supported non-SDK entry")
        if not self.live_ycapi:
            raise ValueError("live_ycapi must be true")
        return self


def collect_business_trial_acceptance(
    *,
    env: Mapping[str, str] | None = None,
    generated_at: str | None = None,
    production_readiness_collector: ProductionReadinessCollector = collect_production_readiness,
    employee_monitoring_collector: EmployeeMonitoringCollector = collect_employee_monitoring_result,
) -> dict[str, Any]:
    current_env = os.environ if env is None else env
    redactions = _redactions(current_env)
    bundle_generated_at = generated_at or datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    production_checks = _production_checks(
        current_env,
        generated_at=bundle_generated_at,
        production_readiness_collector=production_readiness_collector,
    )
    ac19_status = _check_status(production_checks, "AC-19")
    checks = [
        *production_checks,
        _lightweight_trial_check(current_env, ac19_status=ac19_status),
        _employee_monitoring_check(current_env, employee_monitoring_collector=employee_monitoring_collector),
    ]
    sanitized_checks = [_sanitize_check(check, redactions) for check in checks]
    return {
        "status": _overall_status(check["status"] for check in sanitized_checks),
        "generated_at": bundle_generated_at,
        "summary": _summary(sanitized_checks),
        "checks": sanitized_checks,
    }


def _production_checks(
    env: Mapping[str, str],
    *,
    generated_at: str,
    production_readiness_collector: ProductionReadinessCollector,
) -> list[CheckResult]:
    try:
        production_bundle = production_readiness_collector(env=env, generated_at=generated_at)
    except Exception as exc:
        return [
            CheckResult(
                id="M1-PRODUCTION",
                name="production_readiness_bundle",
                status="FAIL",
                detail=f"production readiness collection failed: {type(exc).__name__}",
            )
        ]
    raw_checks = production_bundle.get("checks", [])
    if not isinstance(raw_checks, list):
        return [
            CheckResult(
                id="M1-PRODUCTION",
                name="production_readiness_bundle",
                status="FAIL",
                detail="production readiness bundle checks must be a list",
            )
        ]
    checks: list[CheckResult] = []
    for item in raw_checks:
        if not isinstance(item, Mapping):
            checks.append(
                CheckResult(
                    id="M1-PRODUCTION",
                    name="production_readiness_bundle",
                    status="FAIL",
                    detail="production readiness check must be an object",
                )
            )
            continue
        checks.append(
            CheckResult(
                id=str(item.get("id") or "M1-PRODUCTION"),
                name=str(item.get("name") or "production_readiness_check"),
                status=_coerce_status(item.get("status")),
                detail=str(item.get("detail") or ""),
                evidence=_mapping_evidence(item.get("evidence")),
            )
        )
    return checks


def _lightweight_trial_check(env: Mapping[str, str], *, ac19_status: ReadinessStatus) -> CheckResult:
    trial_file = _env_value(env, "AIMANAGER_LIGHTWEIGHT_TRIAL_EVIDENCE_FILE")
    if not trial_file:
        return CheckResult(
            id="AC-23",
            name="nontechnical_lightweight_trial",
            status="BLOCKED",
            detail="missing AIMANAGER_LIGHTWEIGHT_TRIAL_EVIDENCE_FILE; cannot verify timed nontechnical trial",
            evidence={"required_env": ["AIMANAGER_LIGHTWEIGHT_TRIAL_EVIDENCE_FILE"]},
        )
    try:
        raw_evidence = _load_json_object(Path(trial_file))
    except FileNotFoundError:
        return CheckResult(
            id="AC-23",
            name="nontechnical_lightweight_trial",
            status="BLOCKED",
            detail=f"trial evidence file does not exist: {trial_file}",
        )
    except Exception as exc:
        return CheckResult(
            id="AC-23",
            name="nontechnical_lightweight_trial",
            status="FAIL",
            detail=f"trial evidence could not be loaded: {type(exc).__name__}",
        )

    forbidden_paths = _find_forbidden_evidence_paths(raw_evidence)
    if forbidden_paths:
        return CheckResult(
            id="AC-23",
            name="nontechnical_lightweight_trial",
            status="FAIL",
            detail="forbidden token/raw-content evidence fields or values are present",
            evidence={
                "rejected_paths": forbidden_paths[:12],
                "redaction_marker": "[redacted:secret-like]",
            },
        )

    try:
        trial = TrialEvidence.model_validate(raw_evidence)
    except ValidationError as exc:
        return CheckResult(
            id="AC-23",
            name="nontechnical_lightweight_trial",
            status="FAIL",
            detail="invalid AC-23 trial evidence: " + _compact_validation_errors(exc),
        )

    if ac19_status != "PASS":
        return CheckResult(
            id="AC-23",
            name="nontechnical_lightweight_trial",
            status="BLOCKED",
            detail=f"valid trial evidence is present, but same-run AC-19 live ycapi status is {ac19_status}",
            evidence=_trial_safe_evidence(trial),
        )

    return CheckResult(
        id="AC-23",
        name="nontechnical_lightweight_trial",
        status="PASS",
        detail="nontechnical trial evidence is valid and same-run AC-19 live ycapi is PASS",
        evidence=_trial_safe_evidence(trial),
    )


def _employee_monitoring_check(
    env: Mapping[str, str],
    *,
    employee_monitoring_collector: EmployeeMonitoringCollector,
) -> CheckResult:
    result_file = _env_value(env, "AIMANAGER_EMPLOYEE_MONITORING_RESULT_FILE")
    if result_file:
        try:
            result = _load_json_object(Path(result_file))
        except FileNotFoundError:
            return CheckResult(
                id="AC-26",
                name="employee_monitoring_policy_evidence",
                status="BLOCKED",
                detail=f"employee monitoring result file does not exist: {result_file}",
            )
        except Exception as exc:
            return CheckResult(
                id="AC-26",
                name="employee_monitoring_policy_evidence",
                status="FAIL",
                detail=f"employee monitoring result could not be loaded: {type(exc).__name__}",
            )
    else:
        policy_file = _env_value(env, "AIMANAGER_EMPLOYEE_MONITORING_POLICY_FILE")
        roster_file = _env_value(env, "AIMANAGER_EMPLOYEE_ROSTER_FILE")
        acknowledgment_file = _env_value(env, "AIMANAGER_EMPLOYEE_ACKNOWLEDGMENT_FILE")
        missing = [
            name
            for name, value in (
                ("AIMANAGER_EMPLOYEE_MONITORING_POLICY_FILE", policy_file),
                ("AIMANAGER_EMPLOYEE_ROSTER_FILE", roster_file),
                ("AIMANAGER_EMPLOYEE_ACKNOWLEDGMENT_FILE", acknowledgment_file),
            )
            if not value
        ]
        if missing:
            return CheckResult(
                id="AC-26",
                name="employee_monitoring_policy_evidence",
                status="BLOCKED",
                detail=f"missing {', '.join(missing)}; cannot verify employee monitoring notice and acknowledgments",
                evidence={
                    "required_env": [
                        "AIMANAGER_EMPLOYEE_MONITORING_POLICY_FILE",
                        "AIMANAGER_EMPLOYEE_ROSTER_FILE",
                        "AIMANAGER_EMPLOYEE_ACKNOWLEDGMENT_FILE",
                    ]
                },
            )
        try:
            result = employee_monitoring_collector(
                policy_file=Path(policy_file),
                employee_roster_file=Path(roster_file),
                acknowledgment_file=Path(acknowledgment_file),
            )
        except Exception as exc:
            return CheckResult(
                id="AC-26",
                name="employee_monitoring_policy_evidence",
                status="FAIL",
                detail=f"employee monitoring validation failed: {type(exc).__name__}",
            )

    status = _coerce_status(result.get("status"))
    return CheckResult(
        id="AC-26",
        name="employee_monitoring_policy_evidence",
        status=status,
        detail=str(result.get("detail") or "employee monitoring validation completed"),
        evidence={
            "policy_id": result.get("policy_id", ""),
            "policy_version": result.get("policy_version", ""),
            "summary": result.get("summary", {}),
        },
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect AiManager business-trial acceptance evidence into one JSON gate.")
    parser.add_argument("--output-json-file", help="Optional path to write the business trial acceptance JSON bundle.")
    parser.add_argument("--lightweight-trial-evidence-file", help="AC-23 timed nontechnical trial evidence JSON.")
    parser.add_argument("--employee-monitoring-result-file", help="Existing AC-26 validation result JSON.")
    parser.add_argument("--employee-monitoring-policy-file", help="AC-26 employee monitoring policy JSON.")
    parser.add_argument("--employee-roster-file", help="AC-26 active employee roster CSV or JSON.")
    parser.add_argument("--employee-acknowledgment-file", help="AC-26 latest-version acknowledgment CSV or JSON.")
    args = parser.parse_args(argv)

    env = dict(os.environ)
    _apply_optional_env(env, "AIMANAGER_LIGHTWEIGHT_TRIAL_EVIDENCE_FILE", args.lightweight_trial_evidence_file)
    _apply_optional_env(env, "AIMANAGER_EMPLOYEE_MONITORING_RESULT_FILE", args.employee_monitoring_result_file)
    _apply_optional_env(env, "AIMANAGER_EMPLOYEE_MONITORING_POLICY_FILE", args.employee_monitoring_policy_file)
    _apply_optional_env(env, "AIMANAGER_EMPLOYEE_ROSTER_FILE", args.employee_roster_file)
    _apply_optional_env(env, "AIMANAGER_EMPLOYEE_ACKNOWLEDGMENT_FILE", args.employee_acknowledgment_file)

    bundle = collect_business_trial_acceptance(env=env)
    if args.output_json_file:
        try:
            output_path = Path(args.output_json_file)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except Exception as exc:
            print(f"FAIL business trial acceptance bundle: result file not written: {type(exc).__name__}", file=sys.stderr)
            return 1

    print(
        f"{bundle['status']} business trial acceptance bundle: "
        f"pass={bundle['summary']['PASS']} fail={bundle['summary']['FAIL']} blocked={bundle['summary']['BLOCKED']}"
    )
    if bundle["status"] == "FAIL":
        return 1
    if bundle["status"] == "BLOCKED":
        return 2
    return 0


def _trial_safe_evidence(trial: TrialEvidence) -> dict[str, Any]:
    return {
        "trial_id": trial.trial_id,
        "entry_channel": trial.entry_channel,
        "operator_role": trial.operator.role,
        "department_id": trial.operator.department_id,
        "identity_source": trial.identity_injection.source,
        "employee_virtual_key_alias": trial.identity_injection.employee_virtual_key_alias,
        "request_id": trial.request_evidence.request_id,
        "model": trial.request_evidence.model,
        "endpoint": trial.request_evidence.endpoint,
        "http_status": trial.request_evidence.http_status,
        "spend": str(trial.request_evidence.spend),
        "currency": trial.request_evidence.currency,
        "pricing_version": trial.request_evidence.pricing_version,
        "scenario_l1": trial.work_context.scenario_l1,
        "scenario_l2": trial.work_context.scenario_l2,
        "project_id": trial.work_context.project_id,
        "customer_id": trial.work_context.customer_id,
        "cost_center_id": trial.work_context.cost_center_id,
        "duration_seconds": trial.timing.duration_seconds,
        "observer": trial.attestation.observer,
        "captured_at": trial.attestation.captured_at.isoformat(),
    }


def _find_forbidden_evidence_paths(value: Any, *, path: str = "$") -> list[str]:
    violations: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            next_path = f"{path}.{key_text}" if path != "$" else key_text
            if _is_forbidden_evidence_key(key_text):
                violations.append(next_path)
                continue
            violations.extend(_find_forbidden_evidence_paths(item, path=next_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            violations.extend(_find_forbidden_evidence_paths(item, path=f"{path}[{index}]"))
    elif isinstance(value, str) and _contains_secret_like_value(value):
        violations.append(path)
    return violations


def _is_forbidden_evidence_key(key: str) -> bool:
    lower = key.lower()
    if lower in ALLOWED_SECRET_META_KEYS:
        return False
    if lower in FORBIDDEN_EVIDENCE_KEYS:
        return True
    if lower.endswith("_token") or lower.endswith("_api_key") or lower.endswith("_secret"):
        return True
    if "authorization" in lower or "cookie" in lower:
        return True
    if lower.endswith("_prompt") or lower.endswith("_response"):
        return True
    return False


def _contains_secret_like_value(value: str) -> bool:
    return contains_secret_like(value)


def _compact_validation_errors(exc: ValidationError) -> str:
    messages: list[str] = []
    for error in exc.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in error.get("loc", ()) if part != "__root__")
        message = str(error.get("msg") or "invalid")
        messages.append(f"{location}: {message}" if location else message)
    if not messages:
        return "validation failed"
    if len(messages) <= 3:
        return "; ".join(messages)
    return f"{messages[0]}; {messages[1]}; {messages[2]}; ... {len(messages) - 3} more"


def _check_status(checks: Sequence[CheckResult], check_id: str) -> ReadinessStatus:
    for check in checks:
        if check.id == check_id:
            return check.status
    return "BLOCKED"


def _mapping_evidence(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _apply_optional_env(env: dict[str, str], name: str, value: str | None) -> None:
    if value:
        env[name] = value


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

MonitoringStatus = Literal["PASS", "FAIL", "BLOCKED"]

_REQUIRED_POLICY_TEXT_FIELDS = (
    "policy_id",
    "version",
    "title",
    "published_at",
    "effective_at",
    "owner",
    "notice_url",
)

_ALLOWED_MONITORING_FIELDS = frozenset(
    {
        "request_id",
        "timestamp",
        "employee_id",
        "department_id",
        "end_user_principal",
        "key_alias",
        "model",
        "endpoint",
        "scenario_l1",
        "scenario_l2",
        "internal_or_external",
        "project_id",
        "customer_id",
        "cost_center_id",
        "currency",
        "spend",
        "request_count",
        "status_code",
        "failure_type",
        "ip_hash",
        "user_agent_hash",
        "device_hash",
        "workspace_hash",
    }
)
_REQUIRED_MONITORING_FIELDS = frozenset(
    {
        "request_id",
        "timestamp",
        "employee_id",
        "department_id",
        "key_alias",
        "scenario_l1",
        "cost_center_id",
        "spend",
        "ip_hash",
        "user_agent_hash",
    }
)
_PROHIBITED_MONITORING_FIELDS = frozenset(
    {
        "prompt_text",
        "response_text",
        "raw_ip",
        "raw_user_agent",
        "customer_content",
    }
)
_ALLOWED_REVIEW_ROLES = frozenset(
    {
        "ai_platform_admin",
        "security",
        "audit",
        "finance",
        "hr",
        "legal",
        "privacy_officer",
    }
)
_PROHIBITED_REVIEW_ROLES = frozenset({"direct_manager", "line_manager", "department_manager"})
_REQUIRED_ACK_FLAGS = (
    "understood_purpose",
    "understood_scope",
    "understood_appeal",
    "understood_no_raw_content",
)
_RULE_TYPES = {"off_hours_usage", "key_sharing"}
_SEVERITIES = {"info", "warning", "high", "critical"}


@dataclass(frozen=True)
class _ActiveEmployee:
    employee_id: str
    department_id: str


def validate_employee_monitoring_controls(
    *,
    policy: Mapping[str, object],
    employee_roster: Iterable[Mapping[str, object]],
    acknowledgments: Iterable[Mapping[str, object]],
) -> dict[str, object]:
    errors: list[str] = []

    if not isinstance(policy, Mapping):
        return _status_result(status="FAIL", detail="policy must be a JSON object", errors=["policy"])

    for field_name in _REQUIRED_POLICY_TEXT_FIELDS:
        if not _is_non_empty_text(policy.get(field_name)):
            errors.append(field_name)

    policy_id = _raw_text(policy.get("policy_id"))
    policy_version = _raw_text(policy.get("version"))
    published_at = _parse_datetime(policy.get("published_at"))
    if _is_non_empty_text(policy.get("published_at")) and published_at is None:
        errors.append("published_at")
    if _is_non_empty_text(policy.get("effective_at")) and _parse_datetime(policy.get("effective_at")) is None:
        errors.append("effective_at")

    notice_channels = _text_list(policy.get("notice_channels"))
    if not notice_channels:
        errors.append("notice_channels")

    monitored_fields = _text_list(policy.get("monitored_metadata_fields"))
    errors.extend(_monitoring_field_errors(monitored_fields))

    prohibited_fields = set(_text_list(policy.get("prohibited_monitoring_fields")))
    for field_name in sorted(_PROHIBITED_MONITORING_FIELDS - prohibited_fields):
        errors.append(f"prohibited_monitoring_fields.{field_name}")

    retention_days = _positive_int(policy.get("retention_days"))
    if retention_days is None or retention_days < 7 or retention_days > 180:
        errors.append("retention_days")

    rules, rule_errors = _validated_rules(policy.get("rules"))
    errors.extend(rule_errors)

    permission_boundary = _permission_boundary(policy.get("permission_boundary"))
    errors.extend(permission_boundary["errors"])

    active_employees, roster_errors = _active_employees(employee_roster)
    errors.extend(roster_errors)
    if not active_employees and not roster_errors:
        return _status_result(
            status="BLOCKED",
            detail="missing active employee roster",
            policy_id=policy_id,
            policy_version=policy_version,
            errors=[],
        )

    acknowledgment_gaps = _acknowledgment_gaps(
        active_employees=active_employees,
        acknowledgments=acknowledgments,
        policy_version=policy_version,
        published_at=published_at,
    )
    if acknowledgment_gaps:
        errors.append("acknowledgments.latest_notice")

    status: MonitoringStatus = "FAIL" if errors else "PASS"
    detail = (
        "employee monitoring controls failed validation"
        if errors
        else "employee monitoring notice, anomaly rules, and review boundaries are valid"
    )
    summary = {
        "active_employee_count": len(active_employees),
        "acknowledged_employee_count": len(active_employees) - len(acknowledgment_gaps),
        "missing_acknowledgment_count": len(acknowledgment_gaps),
        "rule_count": len(rules),
        "retention_days": retention_days or 0,
    }
    controls = {
        "notice_channels": notice_channels,
        "monitored_metadata_fields": monitored_fields,
        "prohibited_monitoring_fields": sorted(prohibited_fields),
        "rules": rules,
        "permission_boundary": permission_boundary["boundary"],
    }
    result = {
        "status": status,
        "detail": detail,
        "policy_id": policy_id,
        "policy_version": policy_version,
        "summary": summary,
        "controls": controls,
        "errors": sorted(set(errors)),
        "warnings": [],
        "acknowledgment_gaps": acknowledgment_gaps,
        "markdown": "",
    }
    result["markdown"] = _markdown(result)
    return result


def _monitoring_field_errors(monitored_fields: list[str]) -> list[str]:
    errors: list[str] = []
    if not monitored_fields:
        return ["monitored_metadata_fields"]
    monitored_set = set(monitored_fields)
    for field_name in sorted(monitored_set - _ALLOWED_MONITORING_FIELDS):
        errors.append(f"monitored_metadata_fields.{field_name}")
    for field_name in sorted(monitored_set & _PROHIBITED_MONITORING_FIELDS):
        errors.append(f"monitored_metadata_fields.{field_name}")
    for field_name in sorted(_REQUIRED_MONITORING_FIELDS - monitored_set):
        errors.append(f"monitored_metadata_fields.missing.{field_name}")
    return errors


def _validated_rules(value: object) -> tuple[list[dict[str, object]], list[str]]:
    if not isinstance(value, list) or not value:
        return [], ["rules"]

    errors: list[str] = []
    normalized_rules: list[dict[str, object]] = []
    seen_types: set[str] = set()
    seen_ids: set[str] = set()

    for index, item in enumerate(value):
        prefix = f"rules.{index}"
        if not isinstance(item, Mapping):
            errors.append(prefix)
            continue
        rule_id = _text(item.get("rule_id"))
        rule_type = _text(item.get("type"))
        severity = _text(item.get("severity"))
        if not rule_id:
            errors.append(f"{prefix}.rule_id")
        elif rule_id in seen_ids:
            errors.append(f"{prefix}.rule_id.duplicate")
        else:
            seen_ids.add(rule_id)
        if rule_type not in _RULE_TYPES:
            errors.append(f"{prefix}.type")
        else:
            seen_types.add(rule_type)
        if item.get("enabled") is not True:
            errors.append(f"{prefix}.enabled")
        if severity not in _SEVERITIES:
            errors.append(f"{prefix}.severity")

        normalized: dict[str, object] = {
            "rule_id": rule_id,
            "type": rule_type,
            "severity": severity,
            "enabled": item.get("enabled") is True,
        }
        if rule_type == "off_hours_usage":
            errors.extend(_off_hours_rule_errors(item, prefix=prefix))
            normalized.update(
                {
                    "timezone": _text(item.get("timezone")),
                    "workday_start": _text(item.get("workday_start")),
                    "workday_end": _text(item.get("workday_end")),
                    "threshold_request_count": _positive_int(item.get("threshold_request_count")) or 0,
                    "threshold_spend": _decimal_text(item.get("threshold_spend")),
                    "lookback_hours": _positive_int(item.get("lookback_hours")) or 0,
                    "requires_exception_ticket": item.get("requires_exception_ticket") is True,
                }
            )
        elif rule_type == "key_sharing":
            errors.extend(_key_sharing_rule_errors(item, prefix=prefix))
            normalized.update(
                {
                    "lookback_hours": _positive_int(item.get("lookback_hours")) or 0,
                    "distinct_ip_hash_threshold": _positive_int(item.get("distinct_ip_hash_threshold")) or 0,
                    "distinct_user_agent_hash_threshold": _positive_int(
                        item.get("distinct_user_agent_hash_threshold")
                    )
                    or 0,
                    "requires_disposition": item.get("requires_disposition") is True,
                }
            )
        normalized_rules.append(normalized)

    for rule_type in sorted(_RULE_TYPES - seen_types):
        errors.append(f"rules.{rule_type}")
    return normalized_rules, errors


def _off_hours_rule_errors(rule: Mapping[str, object], *, prefix: str) -> list[str]:
    errors: list[str] = []
    for field_name in ("timezone", "workday_start", "workday_end"):
        if not _is_non_empty_text(rule.get(field_name)):
            errors.append(f"{prefix}.{field_name}")
    if _is_non_empty_text(rule.get("workday_start")) and not _is_time_text(rule.get("workday_start")):
        errors.append(f"{prefix}.workday_start")
    if _is_non_empty_text(rule.get("workday_end")) and not _is_time_text(rule.get("workday_end")):
        errors.append(f"{prefix}.workday_end")
    for field_name in ("threshold_request_count", "lookback_hours"):
        if _positive_int(rule.get(field_name)) is None:
            errors.append(f"{prefix}.{field_name}")
    if _positive_decimal(rule.get("threshold_spend")) is None:
        errors.append(f"{prefix}.threshold_spend")
    if rule.get("requires_exception_ticket") is not True:
        errors.append(f"{prefix}.requires_exception_ticket")
    return errors


def _key_sharing_rule_errors(rule: Mapping[str, object], *, prefix: str) -> list[str]:
    errors: list[str] = []
    for field_name in ("distinct_ip_hash_threshold", "distinct_user_agent_hash_threshold", "lookback_hours"):
        if _positive_int(rule.get(field_name)) is None:
            errors.append(f"{prefix}.{field_name}")
    if rule.get("requires_disposition") is not True:
        errors.append(f"{prefix}.requires_disposition")
    return errors


def _permission_boundary(value: object) -> dict[str, object]:
    errors: list[str] = []
    if not isinstance(value, Mapping):
        return {
            "errors": ["permission_boundary"],
            "boundary": {
                "allowed_review_roles": [],
                "prohibited_roles": [],
                "raw_prompt_access": "",
                "customer_content_access": "",
                "requires_hr_or_legal_for_disciplinary_action": False,
                "employee_appeal_channel": "",
            },
        }

    allowed_roles = _text_list(value.get("allowed_review_roles"))
    prohibited_roles = _text_list(value.get("prohibited_roles"))
    for role in allowed_roles:
        if role in _PROHIBITED_REVIEW_ROLES:
            errors.append(f"permission_boundary.allowed_review_roles.{role}")
        if role not in _ALLOWED_REVIEW_ROLES:
            errors.append(f"permission_boundary.allowed_review_roles.unknown.{role}")
    if not allowed_roles:
        errors.append("permission_boundary.allowed_review_roles")
    if "audit" not in allowed_roles:
        errors.append("permission_boundary.allowed_review_roles.audit")
    if not ({"hr", "legal"} & set(allowed_roles)):
        errors.append("permission_boundary.allowed_review_roles.hr_or_legal")
    if "direct_manager" not in prohibited_roles:
        errors.append("permission_boundary.prohibited_roles.direct_manager")
    if _text(value.get("raw_prompt_access")) != "prohibited":
        errors.append("permission_boundary.raw_prompt_access")
    if _text(value.get("customer_content_access")) != "prohibited":
        errors.append("permission_boundary.customer_content_access")
    if value.get("requires_hr_or_legal_for_disciplinary_action") is not True:
        errors.append("permission_boundary.requires_hr_or_legal_for_disciplinary_action")
    if not _is_non_empty_text(value.get("employee_appeal_channel")):
        errors.append("permission_boundary.employee_appeal_channel")
    return {
        "errors": errors,
        "boundary": {
            "allowed_review_roles": allowed_roles,
            "prohibited_roles": prohibited_roles,
            "raw_prompt_access": _text(value.get("raw_prompt_access")),
            "customer_content_access": _text(value.get("customer_content_access")),
            "requires_hr_or_legal_for_disciplinary_action": value.get(
                "requires_hr_or_legal_for_disciplinary_action"
            )
            is True,
            "employee_appeal_channel": str(value.get("employee_appeal_channel")).strip()
            if _is_non_empty_text(value.get("employee_appeal_channel"))
            else "",
        },
    }


def _active_employees(rows: Iterable[Mapping[str, object]]) -> tuple[list[_ActiveEmployee], list[str]]:
    errors: list[str] = []
    employees: list[_ActiveEmployee] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        employee_id = _raw_text(row.get("employee_id"))
        department_id = _raw_text(row.get("department_id"))
        status = _text(row.get("status") or "active")
        if status not in {"active", "inactive", "terminated", "leave"}:
            errors.append(f"employee_roster.{index}.status")
            continue
        if status != "active":
            continue
        if not employee_id:
            errors.append(f"employee_roster.{index}.employee_id")
            continue
        if employee_id in seen:
            errors.append(f"employee_roster.{index}.employee_id.duplicate")
            continue
        if not department_id:
            errors.append(f"employee_roster.{index}.department_id")
            continue
        seen.add(employee_id)
        employees.append(_ActiveEmployee(employee_id=employee_id, department_id=department_id))
    employees.sort(key=lambda item: item.employee_id)
    return employees, errors


def _acknowledgment_gaps(
    *,
    active_employees: list[_ActiveEmployee],
    acknowledgments: Iterable[Mapping[str, object]],
    policy_version: str,
    published_at: datetime | None,
) -> list[dict[str, str]]:
    acknowledged: set[str] = set()
    for row in acknowledgments:
        employee_id = _raw_text(row.get("employee_id"))
        if not employee_id:
            continue
        if _raw_text(row.get("notice_version")) != policy_version:
            continue
        acknowledged_at = _parse_datetime(row.get("acknowledged_at"))
        if published_at is not None and (acknowledged_at is None or _is_before(acknowledged_at, published_at)):
            continue
        if not all(_truthy(row.get(field_name)) for field_name in _REQUIRED_ACK_FLAGS):
            continue
        acknowledged.add(employee_id)

    gaps = [
        {
            "employee_id": employee.employee_id,
            "department_id": employee.department_id,
            "reason": "missing latest notice acknowledgment",
        }
        for employee in active_employees
        if employee.employee_id not in acknowledged
    ]
    return gaps


def _status_result(
    *,
    status: MonitoringStatus,
    detail: str,
    policy_id: str = "",
    policy_version: str = "",
    errors: list[str],
) -> dict[str, object]:
    result = {
        "status": status,
        "detail": detail,
        "policy_id": policy_id,
        "policy_version": policy_version,
        "summary": {
            "active_employee_count": 0,
            "acknowledged_employee_count": 0,
            "missing_acknowledgment_count": 0,
            "rule_count": 0,
            "retention_days": 0,
        },
        "controls": {
            "notice_channels": [],
            "monitored_metadata_fields": [],
            "prohibited_monitoring_fields": [],
            "rules": [],
            "permission_boundary": {},
        },
        "errors": sorted(set(errors)),
        "warnings": [],
        "acknowledgment_gaps": [],
        "markdown": "",
    }
    result["markdown"] = _markdown(result)
    return result


def _markdown(result: Mapping[str, object]) -> str:
    summary = result.get("summary")
    summary_map = summary if isinstance(summary, Mapping) else {}
    controls = result.get("controls")
    controls_map = controls if isinstance(controls, Mapping) else {}
    rules = controls_map.get("rules")
    rule_rows = rules if isinstance(rules, list) else []
    lines = [
        f"# AiManager 员工监控制度验收 - {result.get('policy_version') or 'unknown'}",
        "",
        f"- 状态：{result.get('status')}",
        f"- 说明：{result.get('detail')}",
        f"- 活跃员工：{summary_map.get('active_employee_count', 0)}",
        f"- 已确认员工：{summary_map.get('acknowledged_employee_count', 0)}",
        f"- 未确认员工：{summary_map.get('missing_acknowledgment_count', 0)}",
        f"- 数据保留：{summary_map.get('retention_days', 0)} 天",
        "",
        "## 规则",
    ]
    for rule in rule_rows:
        if isinstance(rule, Mapping):
            lines.append(f"- {rule.get('type')} / {rule.get('severity')} / {rule.get('rule_id')}")
    gaps = result.get("acknowledgment_gaps")
    if isinstance(gaps, list) and gaps:
        lines.extend(["", "## 未完成确认"])
        for gap in gaps:
            if isinstance(gap, Mapping):
                lines.append(f"- {gap.get('employee_id')} ({gap.get('department_id')}): {gap.get('reason')}")
    errors = result.get("errors")
    if isinstance(errors, list) and errors:
        lines.extend(["", "## 错误"])
        for error in errors:
            lines.append(f"- {error}")
    return "\n".join(lines) + "\n"


def _text_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized = sorted({_text(item) for item in value if _text(item)})
    return normalized


def _text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lower()


def _raw_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _is_non_empty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _parse_datetime(value: object) -> datetime | None:
    if not _is_non_empty_text(value):
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        try:
            return datetime.fromisoformat(text + "T00:00:00")
        except ValueError:
            return None


def _is_before(left: datetime, right: datetime) -> bool:
    if (left.tzinfo is None) != (right.tzinfo is None):
        return left.replace(tzinfo=None) < right.replace(tzinfo=None)
    return left < right


def _is_time_text(value: object) -> bool:
    if not _is_non_empty_text(value):
        return False
    text = str(value).strip()
    parts = text.split(":")
    if len(parts) != 2:
        return False
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return False
    return 0 <= hour <= 23 and 0 <= minute <= 59


def _positive_int(value: object) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return number


def _positive_decimal(value: object) -> Decimal | None:
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    if amount <= 0:
        return None
    return amount


def _decimal_text(value: object) -> str:
    amount = _positive_decimal(value)
    return str(amount.normalize()) if amount is not None else "0"


def _truthy(value: object) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y", "acknowledged"}
    return False

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


UNASSIGNED = "unassigned"

EVENT_SEVERITY = {
    "passthrough_blocked": "warning",
    "policy_blocked": "warning",
    "enforced_params_blocked": "warning",
    "budget_blocked": "high",
    "key_frozen": "high",
    "key_revoked": "critical",
}

KEY_LIFECYCLE_EVENT_TYPES = {"key_frozen", "key_revoked"}


class AuditEventError(ValueError):
    """Raised when an audit event does not satisfy the AiManager schema."""


def build_audit_event(
    event_type: str,
    *,
    actor: str,
    subject_key_alias: str,
    team_id: str,
    department_id: str,
    project_id: str,
    cost_center_id: str,
    reason: str,
    request_id: str,
    event_id: str | None = None,
    occurred_at: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_event_type = _required_text(event_type, "event_type")
    if normalized_event_type not in EVENT_SEVERITY:
        raise AuditEventError(f"event_type {normalized_event_type!r} is not supported by AiManager audit")

    normalized_reason = _text(reason, default="")
    if not normalized_reason:
        if normalized_event_type in KEY_LIFECYCLE_EVENT_TYPES:
            raise AuditEventError("reason is required for key lifecycle audit events")
        raise AuditEventError("reason is required for AiManager audit events")

    return {
        "event_id": _text(event_id, default=f"evt_{uuid4().hex}"),
        "event_type": normalized_event_type,
        "severity": EVENT_SEVERITY[normalized_event_type],
        "occurred_at": _normalize_occurred_at(occurred_at),
        "actor": _required_text(actor, "actor"),
        "subject_key_alias": _dimension(subject_key_alias),
        "team_id": _dimension(team_id),
        "department_id": _dimension(department_id),
        "project_id": _dimension(project_id),
        "cost_center_id": _dimension(cost_center_id),
        "reason": normalized_reason,
        "request_id": _required_text(request_id, "request_id"),
        "metadata": _metadata(metadata),
    }


def _normalize_occurred_at(value: str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    normalized = _required_text(value, "occurred_at")
    try:
        datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuditEventError("occurred_at must be an ISO-8601 timestamp") from exc
    return normalized


def _metadata(value: dict[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise AuditEventError("metadata must be an object")
    return copy.deepcopy(value)


def _dimension(value: Any) -> str:
    return _text(value, default=UNASSIGNED)


def _required_text(value: Any, field_name: str) -> str:
    text_value = _text(value, default="")
    if not text_value:
        raise AuditEventError(f"{field_name} is required for AiManager audit events")
    return text_value


def _text(value: Any, *, default: str) -> str:
    if value is None:
        return default
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else default
    return str(value)

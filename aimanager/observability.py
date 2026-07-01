from __future__ import annotations

from collections import Counter
from decimal import Decimal, InvalidOperation
import json
from typing import Any, Iterable, Mapping


UNASSIGNED = "unassigned"
_OPENAI_PREFIX = "openai/"
_SIX_PLACES = Decimal("0.000001")
DEFAULT_FAILURE_RATE_ALERT_THRESHOLD = Decimal("0.05")
_SUCCESS_STATUSES = {"success", "succeeded", "ok", "completed"}
_FAILED_STATUSES = {"failed", "failure", "error", "errored", "timeout", "cancelled"}
_AUDIT_MARKER = "aimanager_audit_event="
_AUDIT_EVENT_TYPES = (
    "passthrough_blocked",
    "policy_blocked",
    "enforced_params_blocked",
    "budget_blocked",
    "key_frozen",
    "key_revoked",
)


class ObservabilityReportError(ValueError):
    """Raised when an observability input cannot be normalized."""


def parse_audit_events_from_log_lines(lines: Iterable[str]) -> list[dict[str, str]]:
    decoder = json.JSONDecoder()
    events: list[dict[str, str]] = []
    for line in lines:
        if _AUDIT_MARKER not in line:
            continue
        _, raw_event = line.split(_AUDIT_MARKER, 1)
        try:
            payload, _index = decoder.raw_decode(raw_event.strip())
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        event_type = payload.get("event_type")
        request_id = payload.get("request_id")
        reason = payload.get("reason")
        if not isinstance(event_type, str) or not isinstance(request_id, str) or not isinstance(reason, str):
            continue
        events.append(
            {
                "event_type": event_type.strip(),
                "request_id": request_id.strip(),
                "reason": reason.strip(),
            }
        )
    return [event for event in events if event["event_type"] and event["request_id"]]


def build_observability_report(
    *,
    audit_log_lines: Iterable[str] = (),
    request_rows: Iterable[dict[str, Any]] = (),
    failure_rate_alert_threshold: Decimal = DEFAULT_FAILURE_RATE_ALERT_THRESHOLD,
) -> dict[str, Any]:
    audit_events = parse_audit_events_from_log_lines(audit_log_lines)
    requests = [_normalize_request_record(row) for row in request_rows]
    audit_counts = Counter(event["event_type"] for event in audit_events)
    request_count = len(requests)
    failed_requests = sum(1 for row in requests if not row["successful"])
    successful_requests = request_count - failed_requests
    failure_rate = _rate(failed_requests, request_count)
    latency_values = [row["latency_ms"] for row in requests if row["latency_ms"] is not None]

    metrics: dict[str, Any] = {
        "request_count": request_count,
        "successful_requests": successful_requests,
        "failed_requests": failed_requests,
        "failure_rate": failure_rate,
        "http_429_count": sum(1 for row in requests if row["status_code"] == 429),
        "http_5xx_count": sum(1 for row in requests if _is_5xx(row["status_code"])),
        "missing_request_id_count": sum(1 for row in requests if not row["request_id"]),
        "average_latency_ms": _average(latency_values),
        "total_tokens": sum(row["total_tokens"] for row in requests),
        "image_count": sum(row["image_count"] for row in requests),
        "spend": sum((row["spend"] for row in requests), Decimal("0")),
        "audit_event_count": len(audit_events),
        "audit_events": {event_type: audit_counts[event_type] for event_type in _AUDIT_EVENT_TYPES},
        "observed_request_ids": sorted(
            {
                row["request_id"]
                for row in requests
                if row["request_id"]
            }
            | {
                event["request_id"]
                for event in audit_events
                if event["request_id"]
            }
        ),
    }
    for event_type in _AUDIT_EVENT_TYPES:
        metrics[f"{event_type}_count"] = metrics["audit_events"][event_type]

    return {
        "metrics": metrics,
        "alert_policy": {
            "failure_rate_alert_threshold": _quantize_six(
                _decimal(failure_rate_alert_threshold, "failure_rate_alert_threshold")
            ),
        },
        "by_key_model": _aggregate_by_key_model(requests),
        "alerts": derive_observability_alerts(metrics, failure_rate_alert_threshold=failure_rate_alert_threshold),
    }


def _normalize_request_record(row: dict[str, Any]) -> dict[str, Any]:
    status_code = _optional_int(row.get("status_code") or row.get("http_status") or row.get("response_status"))
    status = _text(row.get("status"), default="").lower()
    successful = _is_success(status=status, status_code=status_code)
    metadata = _metadata(row.get("metadata"))
    return {
        "request_id": _text(row.get("request_id") or row.get("x_request_id"), default=""),
        "status_code": status_code,
        "successful": successful,
        "team_id": _dimension(row, metadata, "team_id"),
        "key_alias": _dimension(row, metadata, "key_alias", aliases=("user_api_key_alias", "api_key_alias")),
        "model": _normalize_model_name(_text(row.get("model") or row.get("model_group"), default=UNASSIGNED)),
        "latency_ms": _optional_decimal(row.get("latency_ms") or row.get("duration_ms") or row.get("response_ms")),
        "total_tokens": _non_negative_int(row.get("total_tokens") or row.get("tokens"), "total_tokens"),
        "image_count": _non_negative_int(row.get("image_count") or row.get("images"), "image_count"),
        "spend": _decimal(row.get("spend") or row.get("cost"), "spend"),
    }


def _aggregate_by_key_model(requests: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in requests:
        key = (row["team_id"], row["key_alias"], row["model"])
        if key not in buckets:
            team_id, key_alias, model = key
            buckets[key] = {
                "team_id": team_id,
                "key_alias": key_alias,
                "model": model,
                "request_count": 0,
                "successful_requests": 0,
                "failed_requests": 0,
                "http_429_count": 0,
                "http_5xx_count": 0,
                "total_tokens": 0,
                "image_count": 0,
                "spend": Decimal("0"),
            }
        bucket = buckets[key]
        bucket["request_count"] += 1
        bucket["successful_requests"] += 1 if row["successful"] else 0
        bucket["failed_requests"] += 0 if row["successful"] else 1
        bucket["http_429_count"] += 1 if row["status_code"] == 429 else 0
        bucket["http_5xx_count"] += 1 if _is_5xx(row["status_code"]) else 0
        bucket["total_tokens"] += row["total_tokens"]
        bucket["image_count"] += row["image_count"]
        bucket["spend"] += row["spend"]
    return [buckets[key] for key in sorted(buckets)]


def derive_observability_alerts(
    metrics: Mapping[str, Any],
    *,
    failure_rate_alert_threshold: Any = DEFAULT_FAILURE_RATE_ALERT_THRESHOLD,
) -> list[dict[str, Any]]:
    normalized_metrics = _alert_metric_values(metrics)
    threshold = _decimal(failure_rate_alert_threshold, "failure_rate_alert_threshold")
    return _build_alerts(normalized_metrics, threshold)


def _alert_metric_values(metrics: Mapping[str, Any]) -> dict[str, Any]:
    request_count = _non_negative_int(metrics.get("request_count"), "metrics.request_count")
    failed_requests = _non_negative_int(metrics.get("failed_requests"), "metrics.failed_requests")
    if failed_requests > request_count:
        raise ObservabilityReportError("metrics.failed_requests cannot exceed metrics.request_count")
    computed_failure_rate = _rate(failed_requests, request_count)
    failure_rate_value = metrics.get("failure_rate")
    if failure_rate_value in (None, ""):
        failure_rate = computed_failure_rate
    else:
        failure_rate = _quantize_six(_decimal(failure_rate_value, "metrics.failure_rate"))
        if failure_rate != computed_failure_rate:
            raise ObservabilityReportError("metrics.failure_rate must equal failed_requests / request_count")
    return {
        "request_count": request_count,
        "failure_rate": failure_rate,
        "http_429_count": _non_negative_int(metrics.get("http_429_count"), "metrics.http_429_count"),
        "http_5xx_count": _non_negative_int(metrics.get("http_5xx_count"), "metrics.http_5xx_count"),
        "budget_blocked_count": _non_negative_int(metrics.get("budget_blocked_count"), "metrics.budget_blocked_count"),
        "passthrough_blocked_count": _non_negative_int(
            metrics.get("passthrough_blocked_count"),
            "metrics.passthrough_blocked_count",
        ),
        "enforced_params_blocked_count": _non_negative_int(
            metrics.get("enforced_params_blocked_count"),
            "metrics.enforced_params_blocked_count",
        ),
        "missing_request_id_count": _non_negative_int(
            metrics.get("missing_request_id_count"),
            "metrics.missing_request_id_count",
        ),
    }


def _build_alerts(metrics: dict[str, Any], failure_rate_alert_threshold: Decimal) -> list[dict[str, Any]]:
    threshold = _quantize_six(failure_rate_alert_threshold)
    alerts: list[dict[str, Any]] = []
    if metrics["request_count"] and metrics["failure_rate"] >= threshold:
        alerts.append(
            {
                "code": "aimanager_failure_rate_high",
                "severity": "high",
                "value": metrics["failure_rate"],
                "threshold": threshold,
            }
        )
    if metrics["http_429_count"]:
        alerts.append({"code": "aimanager_http_429_seen", "severity": "warning", "count": metrics["http_429_count"]})
    if metrics["http_5xx_count"]:
        alerts.append({"code": "aimanager_http_5xx_seen", "severity": "high", "count": metrics["http_5xx_count"]})
    if metrics["budget_blocked_count"]:
        alerts.append(
            {"code": "aimanager_budget_blocked_seen", "severity": "high", "count": metrics["budget_blocked_count"]}
        )
    if metrics["passthrough_blocked_count"]:
        alerts.append(
            {
                "code": "aimanager_passthrough_blocked_seen",
                "severity": "warning",
                "count": metrics["passthrough_blocked_count"],
            }
        )
    if metrics["enforced_params_blocked_count"]:
        alerts.append(
            {
                "code": "aimanager_enforced_params_blocked_seen",
                "severity": "warning",
                "count": metrics["enforced_params_blocked_count"],
            }
        )
    if metrics["missing_request_id_count"]:
        alerts.append(
            {
                "code": "aimanager_request_id_missing",
                "severity": "high",
                "count": metrics["missing_request_id_count"],
            }
        )
    return alerts


def _is_success(*, status: str, status_code: int | None) -> bool:
    if status_code is not None and status_code >= 400:
        return False
    if status in _FAILED_STATUSES:
        return False
    if status in _SUCCESS_STATUSES:
        return True
    if status_code is not None:
        return 200 <= status_code < 400
    return True


def _is_5xx(status_code: int | None) -> bool:
    return status_code is not None and 500 <= status_code <= 599


def _rate(numerator: int, denominator: int) -> Decimal:
    if denominator == 0:
        return Decimal("0.000000")
    return _quantize_six(Decimal(numerator) / Decimal(denominator))


def _average(values: list[Decimal]) -> Decimal:
    if not values:
        return Decimal("0.000000")
    return _quantize_six(sum(values, Decimal("0")) / Decimal(len(values)))


def _quantize_six(value: Decimal) -> Decimal:
    return value.quantize(_SIX_PLACES)


def _dimension(
    row: dict[str, Any],
    metadata: dict[str, Any],
    field_name: str,
    *,
    aliases: tuple[str, ...] = (),
) -> str:
    for key in (field_name, *aliases):
        value = row.get(key)
        if value not in (None, ""):
            return _text(value, default=UNASSIGNED)
    for key in (field_name, *aliases):
        value = metadata.get(key)
        if value not in (None, ""):
            return _text(value, default=UNASSIGNED)
    return UNASSIGNED


def _metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _normalize_model_name(value: str) -> str:
    if value.startswith(_OPENAI_PREFIX):
        return value[len(_OPENAI_PREFIX) :]
    return value


def _text(value: Any, *, default: str) -> str:
    if value is None:
        return default
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else default
    return str(value)


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ObservabilityReportError("status_code must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ObservabilityReportError("status_code must be an integer") from exc


def _non_negative_int(value: Any, field_name: str) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, bool):
        raise ObservabilityReportError(f"{field_name} must be a non-negative integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ObservabilityReportError(f"{field_name} must be a non-negative integer") from exc
    if number < 0:
        raise ObservabilityReportError(f"{field_name} must be a non-negative integer")
    return number


def _optional_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    return _decimal(value, "latency_ms")


def _decimal(value: Any, field_name: str) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    if isinstance(value, bool):
        raise ObservabilityReportError(f"{field_name} must be a decimal number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ObservabilityReportError(f"{field_name} must be a decimal number") from exc
    if not number.is_finite():
        raise ObservabilityReportError(f"{field_name} must be a finite decimal number")
    return number

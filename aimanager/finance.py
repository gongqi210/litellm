from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
from typing import Any, Iterable


UNASSIGNED = "unassigned"
DEFAULT_CURRENCY = "CNY"
DEFAULT_RECONCILIATION_AMOUNT_THRESHOLD = Decimal("10.00")
DEFAULT_RECONCILIATION_RATE_THRESHOLD = Decimal("0.01")

_SUCCESS_STATUSES = {"success", "succeeded", "ok", "completed"}


class FinanceReportError(ValueError):
    """Raised when a spend row cannot be normalized for finance reporting."""


def aggregate_daily_usage(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return _aggregate_usage(
        rows,
        period_field="date",
        period_value=lambda record: record["date"],
        dimensions=(
            "department_id",
            "project_id",
            "cost_center_id",
            "user_id",
            "key_alias",
            "model",
            "endpoint",
            "currency",
            "pricing_version",
        ),
    )


def aggregate_monthly_usage(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return _aggregate_usage(
        rows,
        period_field="month",
        period_value=lambda record: record["month"],
        dimensions=(
            "department_id",
            "project_id",
            "cost_center_id",
            "user_id",
            "key_alias",
            "model",
            "endpoint",
            "currency",
            "pricing_version",
        ),
    )


def reconcile_monthly_usage(
    aimanager_rows: Iterable[dict[str, Any]],
    ycapi_rows: Iterable[dict[str, Any]],
    *,
    amount_threshold: Decimal = DEFAULT_RECONCILIATION_AMOUNT_THRESHOLD,
    rate_threshold: Decimal = DEFAULT_RECONCILIATION_RATE_THRESHOLD,
) -> list[dict[str, Any]]:
    aimanager_index = _index_bill_rows(aimanager_rows)
    ycapi_index = _index_bill_rows(ycapi_rows)
    reconciliation_rows: list[dict[str, Any]] = []

    for key in sorted(set(aimanager_index) | set(ycapi_index)):
        month, model, endpoint, currency = key
        aimanager_amount = aimanager_index.get(key, Decimal("0"))
        ycapi_amount = ycapi_index.get(key, Decimal("0"))
        difference = ycapi_amount - aimanager_amount
        reference_amount = max(abs(aimanager_amount), abs(ycapi_amount), Decimal("1"))
        difference_rate = abs(difference) / reference_amount
        material_threshold = max(amount_threshold, reference_amount * rate_threshold)
        status = "matched" if abs(difference) <= material_threshold else "needs_review"

        reconciliation_rows.append(
            {
                "month": month,
                "model": model,
                "endpoint": endpoint,
                "aimanager_amount": aimanager_amount,
                "ycapi_amount": ycapi_amount,
                "difference": difference,
                "difference_rate": difference_rate,
                "currency": currency,
                "status": status,
            }
        )

    return reconciliation_rows


def normalize_spend_record(row: dict[str, Any]) -> dict[str, Any]:
    timestamp = _parse_timestamp(row)
    metadata = _coerce_metadata(row.get("metadata"))
    status = _text(row.get("status"), default="success").lower()
    successful = status in _SUCCESS_STATUSES

    return {
        "date": timestamp.date().isoformat(),
        "month": timestamp.strftime("%Y-%m"),
        "department_id": _dimension(row, metadata, "department_id"),
        "project_id": _dimension(row, metadata, "project_id"),
        "cost_center_id": _dimension(row, metadata, "cost_center_id"),
        "user_id": _dimension(row, metadata, "user_id"),
        "key_alias": _dimension(row, metadata, "key_alias"),
        "model": _text(row.get("model"), default=UNASSIGNED),
        "endpoint": _text(row.get("endpoint") or row.get("path"), default=UNASSIGNED),
        "prompt_tokens": _non_negative_int(row.get("prompt_tokens"), "prompt_tokens"),
        "completion_tokens": _non_negative_int(row.get("completion_tokens"), "completion_tokens"),
        "total_tokens": _non_negative_int(row.get("total_tokens"), "total_tokens"),
        "image_count": _non_negative_int(row.get("image_count"), "image_count"),
        "spend": _decimal(row.get("spend"), "spend"),
        "currency": _text(row.get("currency"), default=DEFAULT_CURRENCY).upper(),
        "pricing_version": _text(row.get("pricing_version"), default=UNASSIGNED),
        "request_count": 1,
        "successful_requests": 1 if successful else 0,
        "failed_requests": 0 if successful else 1,
    }


def format_money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.000001")), "f").rstrip("0").rstrip(".") or "0"


def _aggregate_usage(
    rows: Iterable[dict[str, Any]],
    *,
    period_field: str,
    period_value: Any,
    dimensions: tuple[str, ...],
) -> list[dict[str, Any]]:
    buckets: dict[tuple[Any, ...], dict[str, Any]] = {}
    numeric_fields = (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "image_count",
        "request_count",
        "successful_requests",
        "failed_requests",
    )

    for row in rows:
        record = normalize_spend_record(row)
        key = (period_value(record),) + tuple(record[field] for field in dimensions)
        if key not in buckets:
            bucket = {period_field: period_value(record)}
            for field in dimensions:
                bucket[field] = record[field]
            for field in numeric_fields:
                bucket[field] = 0
            bucket["spend"] = Decimal("0")
            buckets[key] = bucket
        bucket = buckets[key]
        for field in numeric_fields:
            bucket[field] += record[field]
        bucket["spend"] += record["spend"]

    return [buckets[key] for key in sorted(buckets)]


def _index_bill_rows(rows: Iterable[dict[str, Any]]) -> dict[tuple[str, str, str, str], Decimal]:
    index: defaultdict[tuple[str, str, str, str], Decimal] = defaultdict(lambda: Decimal("0"))
    for row in rows:
        key = (
            _text(row.get("month"), default=UNASSIGNED),
            _text(row.get("model"), default=UNASSIGNED),
            _text(row.get("endpoint"), default=UNASSIGNED),
            _text(row.get("currency"), default=DEFAULT_CURRENCY).upper(),
        )
        index[key] += _decimal(row.get("spend"), "spend")
    return dict(index)


def _parse_timestamp(row: dict[str, Any]) -> datetime:
    raw_timestamp = (
        row.get("started_at")
        or row.get("start_time")
        or row.get("created_at")
        or row.get("timestamp")
        or row.get("date")
    )
    if not isinstance(raw_timestamp, str) or not raw_timestamp.strip():
        raise FinanceReportError("started_at is required for finance reporting")
    value = raw_timestamp.strip().replace("Z", "+00:00")
    if len(value) == 10:
        value = f"{value}T00:00:00"
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise FinanceReportError(f"invalid timestamp: {raw_timestamp}") from exc


def _coerce_metadata(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
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


def _dimension(row: dict[str, Any], metadata: dict[str, Any], field_name: str) -> str:
    return _text(row.get(field_name) or metadata.get(field_name), default=UNASSIGNED)


def _text(value: Any, *, default: str) -> str:
    if value is None:
        return default
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else default
    return str(value)


def _non_negative_int(value: Any, field_name: str) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        raise FinanceReportError(f"{field_name} must be a non-negative integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise FinanceReportError(f"{field_name} must be a non-negative integer") from exc
    if number < 0:
        raise FinanceReportError(f"{field_name} must be a non-negative integer")
    return number


def _decimal(value: Any, field_name: str) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, bool):
        raise FinanceReportError(f"{field_name} must be a decimal number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise FinanceReportError(f"{field_name} must be a decimal number") from exc
    if not number.is_finite():
        raise FinanceReportError(f"{field_name} must be a finite decimal number")
    return number

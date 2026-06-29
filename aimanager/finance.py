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
_OPENAI_PREFIX = "openai/"
_CHAT_CALL_TYPES = {"acompletion", "completion", "chat", "chat_completion"}
_IMAGE_CALL_TYPES = {"aimage_generation", "image_generation", "image"}

FINANCE_EXPORT_FILENAMES = (
    "aimanager_usage_daily.csv",
    "aimanager_finance_monthly.csv",
    "aimanager_reconciliation.csv",
)


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
        aimanager_metrics = aimanager_index.get(key, _empty_bill_metrics())
        ycapi_metrics = ycapi_index.get(key, _empty_bill_metrics())
        aimanager_amount = aimanager_metrics["spend"]
        ycapi_amount = ycapi_metrics["spend"]
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
                "prompt_tokens": ycapi_metrics["prompt_tokens"] or aimanager_metrics["prompt_tokens"],
                "completion_tokens": ycapi_metrics["completion_tokens"] or aimanager_metrics["completion_tokens"],
                "total_tokens": ycapi_metrics["total_tokens"] or aimanager_metrics["total_tokens"],
                "image_count": ycapi_metrics["image_count"] or aimanager_metrics["image_count"],
                "aimanager_amount": aimanager_amount,
                "ycapi_amount": ycapi_amount,
                "difference": difference,
                "difference_rate": difference_rate,
                "currency": currency,
                "status": status,
            }
        )

    return reconciliation_rows


def build_finance_export_bundle(
    *,
    spend_rows: Iterable[dict[str, Any]],
    ycapi_bill_rows: Iterable[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    spend_row_list = list(spend_rows)
    daily_rows = aggregate_daily_usage(spend_row_list)
    monthly_rows = [_with_close_status(row) for row in aggregate_monthly_usage(spend_row_list)]
    ycapi_monthly_rows = aggregate_ycapi_monthly_bill(ycapi_bill_rows)
    reconciliation_rows = reconcile_monthly_usage(monthly_rows, ycapi_monthly_rows)
    return {
        "aimanager_usage_daily.csv": daily_rows,
        "aimanager_finance_monthly.csv": monthly_rows,
        "aimanager_reconciliation.csv": reconciliation_rows,
    }


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
        "user_id": _dimension(row, metadata, "user_id", aliases=("user", "end_user", "user_api_key_user_id")),
        "key_alias": _dimension(row, metadata, "key_alias", aliases=("user_api_key_alias", "api_key_alias")),
        "model": _normalize_model_name(_text(row.get("model") or row.get("model_group"), default=UNASSIGNED)),
        "endpoint": _endpoint(row),
        "prompt_tokens": _non_negative_int(row.get("prompt_tokens"), "prompt_tokens"),
        "completion_tokens": _non_negative_int(row.get("completion_tokens"), "completion_tokens"),
        "total_tokens": _non_negative_int(row.get("total_tokens"), "total_tokens"),
        "image_count": _non_negative_int(
            row.get("image_count") or _metadata_value(metadata, "image_count"), "image_count"
        ),
        "spend": _decimal(row.get("spend"), "spend"),
        "currency": _text(row.get("currency"), default=DEFAULT_CURRENCY).upper(),
        "pricing_version": _dimension(row, metadata, "pricing_version"),
        "request_count": 1,
        "successful_requests": 1 if successful else 0,
        "failed_requests": 0 if successful else 1,
    }


def normalize_ycapi_bill_record(row: dict[str, Any]) -> dict[str, Any]:
    prompt_tokens = _non_negative_int(
        row.get("prompt_tokens") or row.get("input_tokens"),
        "prompt_tokens",
    )
    completion_tokens = _non_negative_int(
        row.get("completion_tokens") or row.get("output_tokens"),
        "completion_tokens",
    )
    total_tokens = _non_negative_int(
        row.get("total_tokens") or row.get("tokens"),
        "total_tokens",
    )
    if total_tokens == 0 and (prompt_tokens or completion_tokens):
        total_tokens = prompt_tokens + completion_tokens

    return {
        "month": _parse_bill_month(row),
        "model": _normalize_model_name(
            _text(
                row.get("model_name") or row.get("model") or row.get("model_group"),
                default=UNASSIGNED,
            )
        ),
        "endpoint": _endpoint(row),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "image_count": _non_negative_int(row.get("image_count") or row.get("images"), "image_count"),
        "spend": _decimal(
            row.get("spend") or row.get("amount") or row.get("total_amount") or row.get("cost"),
            "spend",
        ),
        "currency": _text(row.get("currency"), default=DEFAULT_CURRENCY).upper(),
    }


def aggregate_ycapi_monthly_bill(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    numeric_fields = ("prompt_tokens", "completion_tokens", "total_tokens", "image_count")
    for row in rows:
        record = normalize_ycapi_bill_record(row)
        key = (record["month"], record["model"], record["endpoint"], record["currency"])
        if key not in buckets:
            month, model, endpoint, currency = key
            buckets[key] = {
                "month": month,
                "model": model,
                "endpoint": endpoint,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "image_count": 0,
                "spend": Decimal("0"),
                "currency": currency,
            }
        bucket = buckets[key]
        for field in numeric_fields:
            bucket[field] += record[field]
        bucket["spend"] += record["spend"]
    return [buckets[key] for key in sorted(buckets)]


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


def _index_bill_rows(rows: Iterable[dict[str, Any]]) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    index: defaultdict[tuple[str, str, str, str], dict[str, Any]] = defaultdict(_empty_bill_metrics)
    for row in rows:
        key = (
            _text(row.get("month"), default=UNASSIGNED),
            _normalize_model_name(_text(row.get("model"), default=UNASSIGNED)),
            _endpoint(row),
            _text(row.get("currency"), default=DEFAULT_CURRENCY).upper(),
        )
        bucket = index[key]
        bucket["prompt_tokens"] += _non_negative_int(row.get("prompt_tokens"), "prompt_tokens")
        bucket["completion_tokens"] += _non_negative_int(row.get("completion_tokens"), "completion_tokens")
        bucket["total_tokens"] += _non_negative_int(row.get("total_tokens"), "total_tokens")
        bucket["image_count"] += _non_negative_int(row.get("image_count"), "image_count")
        bucket["spend"] += _decimal(row.get("spend"), "spend")
    return dict(index)


def _parse_timestamp(row: dict[str, Any]) -> datetime:
    raw_timestamp = (
        row.get("started_at")
        or row.get("start_time")
        or row.get("startTime")
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
        value = _metadata_value(metadata, key)
        if value not in (None, ""):
            return _text(value, default=UNASSIGNED)
    return UNASSIGNED


def _metadata_value(metadata: dict[str, Any], field_name: str) -> Any:
    if field_name in metadata:
        return metadata[field_name]
    for nested_name in ("user_api_key_metadata", "spend_logs_metadata"):
        nested = metadata.get(nested_name)
        if isinstance(nested, dict) and field_name in nested:
            return nested[field_name]
    return None


def _endpoint(row: dict[str, Any]) -> str:
    explicit = row.get("endpoint") or row.get("path") or row.get("api_path")
    if explicit not in (None, ""):
        return _text(explicit, default=UNASSIGNED)
    call_type = _text(row.get("call_type") or row.get("api_type"), default="").lower()
    if call_type in _CHAT_CALL_TYPES:
        return "/v1/chat/completions"
    if call_type in _IMAGE_CALL_TYPES:
        return "/v1/images/generations"
    return UNASSIGNED


def _normalize_model_name(value: str) -> str:
    if value.startswith(_OPENAI_PREFIX):
        return value[len(_OPENAI_PREFIX) :]
    return value


def _parse_bill_month(row: dict[str, Any]) -> str:
    value = _text(
        row.get("billing_month") or row.get("month") or row.get("period") or row.get("date"),
        default="",
    )
    if len(value) == 7:
        return value
    if len(value) >= 10:
        return _parse_timestamp({"started_at": value}).strftime("%Y-%m")
    raise FinanceReportError("billing_month must be YYYY-MM or a parseable date")


def _empty_bill_metrics() -> dict[str, Any]:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "image_count": 0,
        "spend": Decimal("0"),
    }


def _with_close_status(row: dict[str, Any]) -> dict[str, Any]:
    required_dimensions = ("department_id", "project_id", "cost_center_id")
    close_status = "ready"
    if any(row.get(field) == UNASSIGNED for field in required_dimensions):
        close_status = "blocked_unassigned"
    return {**row, "close_status": close_status}


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

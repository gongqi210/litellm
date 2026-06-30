from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Literal

from aimanager.finance import format_money

MonthlyCloseStatus = Literal["PASS", "FAIL", "BLOCKED"]

_UNASSIGNED = "unassigned"
_OPERATIONS = (
    "supplemental",
    "reversal",
    "attribution_adjustment",
    "difference_resolution",
)
_OPERATION_LABELS = {
    "supplemental": "补记",
    "reversal": "冲销",
    "attribution_adjustment": "归属调整",
    "difference_resolution": "差异处理",
}
_RESOLUTION_STATUSES = {
    "manual_correction",
    "accepted_variance",
    "vendor_credit_pending",
    "supplemental_entry",
}


@dataclass(frozen=True)
class MonthlyCloseRow:
    month: str
    department_id: str
    project_id: str
    cost_center_id: str
    user_id: str
    key_alias: str
    model: str
    endpoint: str
    spend: Decimal
    currency: str
    pricing_version: str
    request_count: int
    close_status: str
    source: str
    adjustment_id: str
    entry_id: str


@dataclass(frozen=True)
class AdjustmentRow:
    adjustment_id: str
    month: str
    operation_type: str
    amount: Decimal
    currency: str
    effective_date: str
    approver: str
    reason: str
    raw: Mapping[str, object]


def build_monthly_close_package(
    *,
    monthly_rows: Iterable[Mapping[str, object]],
    reconciliation_rows: Iterable[Mapping[str, object]],
    adjustment_rows: Iterable[Mapping[str, object]],
    month: str,
) -> dict[str, object]:
    try:
        usage = [_monthly_row(row) for row in monthly_rows if _text(row.get("month")) == month]
        reconciliation = [_reconciliation_row(row) for row in reconciliation_rows if _text(row.get("month")) == month]
        adjustments = [_adjustment_row(row) for row in adjustment_rows if _text(row.get("month")) == month]
        _validate_unique_adjustments(adjustments)
        _validate_single_currency(usage=usage, reconciliation=reconciliation, adjustments=adjustments)
        adjusted_rows, resolutions = _apply_adjustments(usage, reconciliation, adjustments)
    except ValueError as exc:
        return _base_result(status="FAIL", month=month, detail=str(exc))

    operation_counts = _operation_counts(adjustments)
    if not usage:
        return _blocked_result(
            month=month,
            detail=f"missing finance monthly rows for {month}",
            close_status="blocked_missing_finance",
            operation_counts=operation_counts,
        )

    unresolved_reconciliation = _unresolved_reconciliation(reconciliation, resolutions)
    blocking_items = _blocking_items(adjusted_rows=adjusted_rows, unresolved_reconciliation=unresolved_reconciliation)
    close_status = "ready" if not blocking_items else str(blocking_items[0]["code"])
    status: MonthlyCloseStatus = "PASS" if close_status == "ready" else "BLOCKED"
    base_spend = sum((row.spend for row in usage), Decimal("0"))
    adjusted_spend = sum((row.spend for row in adjusted_rows), Decimal("0"))
    adjustment_delta = _ledger_delta(adjustments)
    currency = _result_currency(usage=usage, reconciliation=reconciliation, adjustments=adjustments)
    adjusted_output_rows = [_output_row(row) for row in _sort_rows(adjusted_rows)]
    reconciliation_resolutions = _resolution_output(resolutions=resolutions, reconciliation=reconciliation)
    summary = {
        "base_spend": format_money(base_spend),
        "adjustment_delta": format_money(adjustment_delta),
        "adjusted_spend": format_money(adjusted_spend),
        "currency": currency,
        "monthly_row_count": len(usage),
        "adjustment_count": len(adjustments),
    }
    markdown = _markdown(
        month=month,
        close_status=close_status,
        summary=summary,
        operation_counts=operation_counts,
        blocking_items=blocking_items,
        unresolved_reconciliation=unresolved_reconciliation,
    )
    return {
        "status": status,
        "month": month,
        "detail": "monthly close package generated" if status == "PASS" else "monthly close blocked",
        "close_status": close_status,
        "summary": summary,
        "operation_counts": operation_counts,
        "blocking_items": blocking_items,
        "unresolved_reconciliation": unresolved_reconciliation,
        "reconciliation_resolutions": reconciliation_resolutions,
        "adjusted_monthly_rows": adjusted_output_rows,
        "audit_trail": [_audit_entry(adjustment) for adjustment in adjustments],
        "markdown": markdown,
    }


def _apply_adjustments(
    usage: list[MonthlyCloseRow],
    reconciliation: list[dict[str, object]],
    adjustments: list[AdjustmentRow],
) -> tuple[list[MonthlyCloseRow], dict[str, AdjustmentRow]]:
    rows = list(usage)
    original_by_entry_id = {row.entry_id: row for row in rows if row.entry_id}
    attributed_targets = _projected_attribution_targets(rows, adjustments)
    resolutions: dict[str, AdjustmentRow] = {}

    for adjustment in adjustments:
        if adjustment.operation_type == "supplemental":
            rows.append(_supplemental_row(adjustment, fallback=usage[0] if usage else None))
        elif adjustment.operation_type == "reversal":
            if not _required_raw_text(adjustment, "reverses_entry_id"):
                raise ValueError(f"{adjustment.adjustment_id} reversal requires reverses_entry_id")
            if adjustment.amount >= 0:
                raise ValueError(f"{adjustment.adjustment_id} reversal amount must be negative")
            reverses_entry_id = _required_raw_text(adjustment, "reverses_entry_id")
            target = attributed_targets.get(reverses_entry_id) or original_by_entry_id.get(reverses_entry_id)
            if target is None:
                raise ValueError(f"{adjustment.adjustment_id} reverses unknown monthly entry")
            rows.append(_reversal_row(adjustment, target=target))
        elif adjustment.operation_type == "attribution_adjustment":
            source_entry_id = _required_raw_text(adjustment, "source_entry_id")
            target_index = next((index for index, row in enumerate(rows) if row.entry_id == source_entry_id), None)
            if target_index is None:
                raise ValueError(f"{adjustment.adjustment_id} attribution references unknown monthly entry")
            if adjustment.amount <= 0:
                raise ValueError(f"{adjustment.adjustment_id} attribution amount must be positive")
            rows[target_index] = _attributed_row(rows[target_index], adjustment)
        elif adjustment.operation_type == "difference_resolution":
            key = _required_raw_text(adjustment, "reconciliation_key")
            resolution_status = _required_raw_text(adjustment, "resolution_status")
            if resolution_status not in _RESOLUTION_STATUSES:
                raise ValueError(f"{adjustment.adjustment_id} has unsupported resolution_status: {resolution_status}")
            if key not in {_reconciliation_key(row) for row in reconciliation}:
                raise ValueError(f"{adjustment.adjustment_id} references unknown reconciliation_key")
            resolutions[key] = adjustment
        else:
            raise ValueError(f"unsupported operation_type: {adjustment.operation_type}")

    return _aggregate_rows(rows), resolutions


def _projected_attribution_targets(
    rows: list[MonthlyCloseRow], adjustments: list[AdjustmentRow]
) -> dict[str, MonthlyCloseRow]:
    original_by_entry_id = {row.entry_id: row for row in rows if row.entry_id}
    targets: dict[str, MonthlyCloseRow] = {}
    for adjustment in adjustments:
        if adjustment.operation_type != "attribution_adjustment":
            continue
        source_entry_id = _required_raw_text(adjustment, "source_entry_id")
        source = original_by_entry_id.get(source_entry_id)
        if source is None:
            continue
        targets[source_entry_id] = _attributed_row(source, adjustment)
    return targets


def _monthly_row(row: Mapping[str, object]) -> MonthlyCloseRow:
    monthly_row = MonthlyCloseRow(
        month=_required_text(row, "month"),
        department_id=_dimension(row, "department_id"),
        project_id=_dimension(row, "project_id"),
        cost_center_id=_dimension(row, "cost_center_id"),
        user_id=_dimension(row, "user_id"),
        key_alias=_dimension(row, "key_alias"),
        model=_dimension(row, "model"),
        endpoint=_dimension(row, "endpoint"),
        spend=_decimal(row.get("spend"), "spend"),
        currency=_required_text(row, "currency").upper(),
        pricing_version=_dimension(row, "pricing_version"),
        request_count=_non_negative_int(row.get("request_count"), "request_count"),
        close_status=_text(row.get("close_status"), default="ready"),
        source="monthly",
        adjustment_id="",
        entry_id=_text(row.get("entry_id"), default=""),
    )
    if monthly_row.entry_id:
        return monthly_row
    return replace(monthly_row, entry_id=_stable_monthly_row_key(monthly_row))


def _adjustment_row(row: Mapping[str, object]) -> AdjustmentRow:
    operation_type = _required_text(row, "operation_type").lower()
    if operation_type not in _OPERATIONS:
        raise ValueError(f"unsupported operation_type: {operation_type}")
    return AdjustmentRow(
        adjustment_id=_required_text(row, "adjustment_id"),
        month=_required_text(row, "month"),
        operation_type=operation_type,
        amount=_decimal(row.get("amount"), "amount"),
        currency=_required_text(row, "currency").upper(),
        effective_date=_required_text(row, "effective_date"),
        approver=_required_text(row, "approver"),
        reason=_required_text(row, "reason"),
        raw=row,
    )


def _reconciliation_row(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "month": _required_text(row, "month"),
        "model": _dimension(row, "model"),
        "endpoint": _dimension(row, "endpoint"),
        "aimanager_amount": _decimal(row.get("aimanager_amount"), "aimanager_amount"),
        "ycapi_amount": _decimal(row.get("ycapi_amount"), "ycapi_amount"),
        "difference": _decimal(row.get("difference"), "difference"),
        "currency": _required_text(row, "currency").upper(),
        "status": _text(row.get("status"), default="matched").lower(),
    }


def _supplemental_row(adjustment: AdjustmentRow, *, fallback: MonthlyCloseRow | None) -> MonthlyCloseRow:
    if adjustment.amount <= 0:
        raise ValueError(f"{adjustment.adjustment_id} supplemental amount must be positive")
    return MonthlyCloseRow(
        month=adjustment.month,
        department_id=_required_raw_text(adjustment, "department_id"),
        project_id=_required_raw_text(adjustment, "project_id"),
        cost_center_id=_required_raw_text(adjustment, "cost_center_id"),
        user_id=_required_raw_text(adjustment, "user_id"),
        key_alias=_required_raw_text(adjustment, "key_alias"),
        model=_required_raw_text(adjustment, "model"),
        endpoint=_required_raw_text(adjustment, "endpoint"),
        spend=adjustment.amount,
        currency=adjustment.currency,
        pricing_version=_raw_text(
            adjustment,
            "pricing_version",
            default=fallback.pricing_version if fallback is not None else _UNASSIGNED,
        ),
        request_count=_non_negative_int(adjustment.raw.get("request_count") or 1, "request_count"),
        close_status="ready",
        source="supplemental",
        adjustment_id=adjustment.adjustment_id,
        entry_id="",
    )


def _reversal_row(adjustment: AdjustmentRow, *, target: MonthlyCloseRow) -> MonthlyCloseRow:
    return MonthlyCloseRow(
        month=adjustment.month,
        department_id=_raw_text(adjustment, "department_id", default=target.department_id),
        project_id=_raw_text(adjustment, "project_id", default=target.project_id),
        cost_center_id=_raw_text(adjustment, "cost_center_id", default=target.cost_center_id),
        user_id=_raw_text(adjustment, "user_id", default=target.user_id),
        key_alias=_raw_text(adjustment, "key_alias", default=target.key_alias),
        model=_raw_text(adjustment, "model", default=target.model),
        endpoint=_raw_text(adjustment, "endpoint", default=target.endpoint),
        spend=adjustment.amount,
        currency=adjustment.currency,
        pricing_version=_raw_text(adjustment, "pricing_version", default=target.pricing_version),
        request_count=0,
        close_status="ready",
        source="reversal",
        adjustment_id=adjustment.adjustment_id,
        entry_id="",
    )


def _attributed_row(row: MonthlyCloseRow, adjustment: AdjustmentRow) -> MonthlyCloseRow:
    _validate_from_dimension(row, adjustment, "department_id")
    _validate_from_dimension(row, adjustment, "project_id")
    _validate_from_dimension(row, adjustment, "cost_center_id")
    _validate_from_dimension(row, adjustment, "key_alias")
    return replace(
        row,
        department_id=_raw_text(adjustment, "to_department_id", default=row.department_id),
        project_id=_raw_text(adjustment, "to_project_id", default=row.project_id),
        cost_center_id=_raw_text(adjustment, "to_cost_center_id", default=row.cost_center_id),
        key_alias=_raw_text(adjustment, "to_key_alias", default=row.key_alias),
        close_status="ready",
        source="attribution_adjustment",
        adjustment_id=adjustment.adjustment_id,
    )


def _aggregate_rows(rows: list[MonthlyCloseRow]) -> list[MonthlyCloseRow]:
    buckets: dict[tuple[str, ...], list[MonthlyCloseRow]] = {}
    for row in rows:
        key = (
            row.month,
            row.department_id,
            row.project_id,
            row.cost_center_id,
            row.user_id,
            row.key_alias,
            row.model,
            row.endpoint,
            row.currency,
            row.pricing_version,
        )
        buckets.setdefault(key, []).append(row)

    aggregated: list[MonthlyCloseRow] = []
    for key, items in buckets.items():
        first = items[0]
        source, adjustment_id = _aggregate_source(items)
        aggregated.append(
            MonthlyCloseRow(
                month=key[0],
                department_id=key[1],
                project_id=key[2],
                cost_center_id=key[3],
                user_id=key[4],
                key_alias=key[5],
                model=key[6],
                endpoint=key[7],
                currency=key[8],
                pricing_version=key[9],
                spend=sum((item.spend for item in items), Decimal("0")),
                request_count=sum(item.request_count for item in items),
                close_status=_row_close_status(items),
                source=source,
                adjustment_id=adjustment_id,
                entry_id=first.entry_id,
            )
        )
    return aggregated


def _aggregate_source(items: list[MonthlyCloseRow]) -> tuple[str, str]:
    sources = {item.source for item in items}
    if "monthly" in sources or "supplemental" in sources:
        return "monthly_close", ""
    attribution = next((item for item in items if item.source == "attribution_adjustment"), None)
    if attribution is not None:
        return "attribution_adjustment", attribution.adjustment_id
    first_adjusted = next((item for item in items if item.adjustment_id), None)
    if first_adjusted is not None:
        return first_adjusted.source, first_adjusted.adjustment_id
    return items[0].source, items[0].adjustment_id


def _row_close_status(items: list[MonthlyCloseRow]) -> str:
    if any(_has_unassigned_dimension(item) and item.spend != 0 for item in items):
        return "blocked_unassigned"
    return "ready"


def _blocking_items(
    *,
    adjusted_rows: list[MonthlyCloseRow],
    unresolved_reconciliation: list[dict[str, object]],
) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    unassigned_rows = [_output_row(row) for row in adjusted_rows if _has_unassigned_dimension(row) and row.spend != 0]
    if unassigned_rows:
        items.append(
            {
                "code": "blocked_unassigned",
                "detail": "monthly close contains unassigned spend",
                "rows": unassigned_rows,
            }
        )
    if unresolved_reconciliation:
        items.append(
            {
                "code": "blocked_unresolved_reconciliation",
                "detail": "monthly close contains unresolved ycapi reconciliation differences",
                "rows": unresolved_reconciliation,
            }
        )
    return items


def _unresolved_reconciliation(
    reconciliation: list[dict[str, object]], resolutions: Mapping[str, AdjustmentRow]
) -> list[dict[str, object]]:
    unresolved: list[dict[str, object]] = []
    for row in reconciliation:
        key = _reconciliation_key(row)
        if row["status"] != "needs_review" or key in resolutions:
            continue
        unresolved.append(
            {
                "reconciliation_key": key,
                "model": row["model"],
                "endpoint": row["endpoint"],
                "currency": row["currency"],
                "difference": format_money(row["difference"]),
                "status": row["status"],
            }
        )
    return unresolved


def _resolution_output(
    *, resolutions: Mapping[str, AdjustmentRow], reconciliation: list[dict[str, object]]
) -> list[dict[str, object]]:
    reconciliation_by_key = {_reconciliation_key(row): row for row in reconciliation}
    output = []
    for key in sorted(resolutions):
        adjustment = resolutions[key]
        row = reconciliation_by_key.get(key)
        output.append(
            {
                "reconciliation_key": key,
                "status": "resolved",
                "resolution_status": _required_raw_text(adjustment, "resolution_status"),
                "adjustment_id": adjustment.adjustment_id,
                "difference": format_money(row["difference"]) if row is not None else format_money(adjustment.amount),
                "approver": adjustment.approver,
            }
        )
    return output


def _output_row(row: MonthlyCloseRow) -> dict[str, object]:
    return {
        "month": row.month,
        "department_id": row.department_id,
        "project_id": row.project_id,
        "cost_center_id": row.cost_center_id,
        "user_id": row.user_id,
        "key_alias": row.key_alias,
        "model": row.model,
        "endpoint": row.endpoint,
        "spend": format_money(row.spend),
        "currency": row.currency,
        "pricing_version": row.pricing_version,
        "request_count": row.request_count,
        "close_status": row.close_status,
        "source": row.source,
        "adjustment_id": row.adjustment_id,
    }


def _base_result(*, status: MonthlyCloseStatus, month: str, detail: str) -> dict[str, object]:
    return {
        "status": status,
        "month": month,
        "detail": detail,
        "close_status": "failed" if status == "FAIL" else "blocked",
        "summary": {},
        "operation_counts": {operation: 0 for operation in _OPERATIONS},
        "blocking_items": [],
        "unresolved_reconciliation": [],
        "reconciliation_resolutions": [],
        "adjusted_monthly_rows": [],
        "audit_trail": [],
        "markdown": "",
    }


def _blocked_result(
    *,
    month: str,
    detail: str,
    close_status: str,
    operation_counts: dict[str, int],
) -> dict[str, object]:
    result = _base_result(status="BLOCKED", month=month, detail=detail)
    result["close_status"] = close_status
    result["operation_counts"] = operation_counts
    return result


def _audit_entry(adjustment: AdjustmentRow) -> dict[str, str]:
    return {
        "adjustment_id": adjustment.adjustment_id,
        "operation_type": adjustment.operation_type,
        "effective_date": adjustment.effective_date,
        "approver": adjustment.approver,
        "reason": adjustment.reason,
    }


def _markdown(
    *,
    month: str,
    close_status: str,
    summary: Mapping[str, object],
    operation_counts: Mapping[str, int],
    blocking_items: list[dict[str, object]],
    unresolved_reconciliation: list[dict[str, object]],
) -> str:
    lines = [
        f"# AiManager 月结包 - {month}",
        "",
        f"月结状态：{close_status}",
        f"调整后费用：{summary.get('adjusted_spend', '0')} {summary.get('currency', '')}".rstrip(),
        f"基础费用：{summary.get('base_spend', '0')} {summary.get('currency', '')}".rstrip(),
        f"调整净额：{summary.get('adjustment_delta', '0')} {summary.get('currency', '')}".rstrip(),
        "",
        "## 调整操作",
    ]
    for operation in _OPERATIONS:
        lines.append(f"- {_OPERATION_LABELS[operation]}：{operation_counts.get(operation, 0)}")
    if blocking_items:
        lines.extend(["", "## 阻断项"])
        for item in blocking_items:
            lines.append(f"- {item['code']}：{item['detail']}")
    if unresolved_reconciliation:
        lines.extend(["", "## 未处理对账差异"])
        for item in unresolved_reconciliation:
            lines.append(f"- {item['reconciliation_key']}：{item['difference']} {item['currency']}")
    return "\n".join(lines) + "\n"


def _operation_counts(adjustments: list[AdjustmentRow]) -> dict[str, int]:
    counter = Counter(adjustment.operation_type for adjustment in adjustments)
    return {operation: counter.get(operation, 0) for operation in _OPERATIONS}


def _ledger_delta(adjustments: list[AdjustmentRow]) -> Decimal:
    return sum(
        (
            adjustment.amount
            for adjustment in adjustments
            if adjustment.operation_type in {"supplemental", "reversal"}
        ),
        Decimal("0"),
    )


def _result_currency(
    *,
    usage: list[MonthlyCloseRow],
    reconciliation: list[dict[str, object]],
    adjustments: list[AdjustmentRow],
) -> str:
    for row in usage:
        return row.currency
    for row in reconciliation:
        return str(row["currency"])
    for adjustment in adjustments:
        return adjustment.currency
    return ""


def _validate_unique_adjustments(adjustments: list[AdjustmentRow]) -> None:
    seen: set[str] = set()
    for adjustment in adjustments:
        if adjustment.adjustment_id in seen:
            raise ValueError(f"duplicate adjustment_id: {adjustment.adjustment_id}")
        seen.add(adjustment.adjustment_id)


def _validate_single_currency(
    *,
    usage: list[MonthlyCloseRow],
    reconciliation: list[dict[str, object]],
    adjustments: list[AdjustmentRow],
) -> None:
    currencies = {row.currency for row in usage if row.currency}
    currencies.update(str(row["currency"]) for row in reconciliation if row.get("currency"))
    currencies.update(adjustment.currency for adjustment in adjustments if adjustment.currency)
    if len(currencies) > 1:
        raise ValueError(f"mixed currency in monthly close package: {sorted(currencies)}")


def _validate_from_dimension(row: MonthlyCloseRow, adjustment: AdjustmentRow, dimension: str) -> None:
    expected = _raw_text(adjustment, f"from_{dimension}", default=getattr(row, dimension))
    actual = getattr(row, dimension)
    if expected != actual:
        raise ValueError(
            f"{adjustment.adjustment_id} attribution from_{dimension}={expected} does not match source row {actual}"
        )


def _sort_rows(rows: list[MonthlyCloseRow]) -> list[MonthlyCloseRow]:
    return sorted(
        rows,
        key=lambda row: (
            row.model,
            row.endpoint,
            row.department_id,
            row.project_id,
            row.cost_center_id,
            row.key_alias,
        ),
    )


def _stable_monthly_row_key(row: MonthlyCloseRow) -> str:
    return "|".join(
        (
            row.month,
            row.department_id,
            row.project_id,
            row.cost_center_id,
            row.user_id,
            row.key_alias,
            row.model,
            row.endpoint,
            row.currency,
            row.pricing_version,
        )
    )


def _reconciliation_key(row: Mapping[str, object]) -> str:
    return f"{row['month']}|{row['model']}|{row['endpoint']}|{row['currency']}"


def _has_unassigned_dimension(row: MonthlyCloseRow) -> bool:
    return any(
        value == _UNASSIGNED
        for value in (
            row.department_id,
            row.project_id,
            row.cost_center_id,
            row.user_id,
            row.key_alias,
        )
    )


def _required_raw_text(adjustment: AdjustmentRow, field: str) -> str:
    return _required_text(adjustment.raw, field)


def _raw_text(adjustment: AdjustmentRow, field: str, *, default: str) -> str:
    return _text(adjustment.raw.get(field), default=default)


def _dimension(row: Mapping[str, object], field: str) -> str:
    return _text(row.get(field), default=_UNASSIGNED)


def _required_text(row: Mapping[str, object], field: str) -> str:
    value = _text(row.get(field), default="")
    if not value:
        raise ValueError(f"{field} is required")
    return value


def _text(value: object, *, default: str = _UNASSIGNED) -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _decimal(value: object, field: str) -> Decimal:
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a decimal number") from exc
    if not decimal.is_finite():
        raise ValueError(f"{field} must be finite")
    return decimal


def _non_negative_int(value: object, field: str) -> int:
    try:
        number = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if number < 0:
        raise ValueError(f"{field} must be non-negative")
    return number

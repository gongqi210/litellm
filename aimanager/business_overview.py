from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from aimanager.finance import format_money

BusinessOverviewStatus = Literal["PASS", "FAIL", "BLOCKED"]

_RATE_QUANT = Decimal("0.000001")
_UNASSIGNED = "unassigned"


@dataclass(frozen=True)
class MonthlyUsageRow:
    month: str
    department_id: str
    project_id: str
    cost_center_id: str
    user_id: str
    key_alias: str
    spend: Decimal
    currency: str
    request_count: int
    failed_requests: int


@dataclass(frozen=True)
class BudgetRow:
    month: str
    scope_type: str
    scope_id: str
    budget_amount: Decimal
    currency: str


def build_business_overview(
    *,
    monthly_rows: Iterable[Mapping[str, object]],
    budget_rows: Iterable[Mapping[str, object]],
    observability_report: Mapping[str, object] | None,
    month: str,
    top_n: int = 5,
) -> dict[str, object]:
    try:
        usage = tuple(_usage_row(row) for row in monthly_rows if _text(row.get("month")) == month)
        budgets = tuple(_budget_row(row) for row in budget_rows if _text(row.get("month")) == month)
    except ValueError as exc:
        return _result(status="FAIL", month=month, detail=str(exc))

    if not usage:
        return _result(status="BLOCKED", month=month, detail=f"missing finance rows for month {month}")
    if not budgets:
        return _result(status="BLOCKED", month=month, detail=f"missing budget rows for month {month}")
    if observability_report is None:
        return _result(status="BLOCKED", month=month, detail="missing observability report")

    usage_currencies = frozenset(row.currency for row in usage if row.currency)
    budget_currencies = frozenset(row.currency for row in budgets if row.currency)
    if len(usage_currencies) != 1:
        return _result(status="FAIL", month=month, detail="mixed currency in finance monthly rows")

    currency = next(iter(usage_currencies))
    if any(item != currency for item in budget_currencies):
        return _result(status="FAIL", month=month, detail="mixed currency between finance and budget rows")

    total_spend = sum((row.spend for row in usage), Decimal("0"))
    company_budget = _company_budget(budgets)
    budget_amount = company_budget if company_budget is not None else _fallback_total_budget(budgets)
    summary = {
        "total_spend": format_money(total_spend),
        "currency": currency,
        "budget_amount": format_money(budget_amount),
        "budget_utilization_rate": _rate(total_spend, budget_amount),
        "request_count": sum(row.request_count for row in usage),
        "failed_requests": sum(row.failed_requests for row in usage),
    }
    budget_utilization = _budget_utilization(usage=usage, budgets=budgets)
    top_departments = _rank_dimension(
        usage=usage,
        field_name="department_id",
        label="department_id",
        top_n=top_n,
        budget_lookup=_budget_lookup(budget_utilization, scope_type="department"),
    )
    top_projects = _rank_dimension(
        usage=usage,
        field_name="project_id",
        label="project_id",
        top_n=top_n,
        budget_lookup=_budget_lookup(budget_utilization, scope_type="project"),
    )
    top_keys = _rank_dimension(
        usage=usage,
        field_name="key_alias",
        label="key_alias",
        top_n=top_n,
        budget_lookup={},
    )
    try:
        anomalies = _anomalies(observability_report)
    except ValueError as exc:
        return _result(status="FAIL", month=month, detail=str(exc))
    markdown = _markdown(
        month=month,
        summary=summary,
        top_departments=top_departments,
        top_projects=top_projects,
        top_keys=top_keys,
        anomalies=anomalies,
    )
    return {
        "status": "PASS",
        "month": month,
        "detail": "business overview generated",
        "summary": summary,
        "budget_utilization": budget_utilization,
        "top_departments": top_departments,
        "top_projects": top_projects,
        "top_keys": top_keys,
        "anomalies": anomalies,
        "markdown": markdown,
    }


def _usage_row(row: Mapping[str, object]) -> MonthlyUsageRow:
    return MonthlyUsageRow(
        month=_required_text(row, "month"),
        department_id=_text(row.get("department_id")),
        project_id=_text(row.get("project_id")),
        cost_center_id=_text(row.get("cost_center_id")),
        user_id=_text(row.get("user_id")),
        key_alias=_text(row.get("key_alias")),
        spend=_decimal(row.get("spend"), "spend"),
        currency=_required_text(row, "currency"),
        request_count=_non_negative_int(row.get("request_count"), "request_count"),
        failed_requests=_non_negative_int(row.get("failed_requests"), "failed_requests"),
    )


def _budget_row(row: Mapping[str, object]) -> BudgetRow:
    scope_type = _required_text(row, "scope_type").lower()
    if scope_type not in {"company", "department", "project", "cost_center", "key"}:
        raise ValueError(f"unsupported budget scope_type: {scope_type}")
    return BudgetRow(
        month=_required_text(row, "month"),
        scope_type=scope_type,
        scope_id=_required_text(row, "scope_id"),
        budget_amount=_decimal(row.get("budget_amount"), "budget_amount"),
        currency=_required_text(row, "currency"),
    )


def _result(*, status: BusinessOverviewStatus, month: str, detail: str) -> dict[str, object]:
    return {
        "status": status,
        "month": month,
        "detail": detail,
        "summary": {},
        "budget_utilization": [],
        "top_departments": [],
        "top_projects": [],
        "top_keys": [],
        "anomalies": [],
        "markdown": "",
    }


def _company_budget(budgets: tuple[BudgetRow, ...]) -> Decimal | None:
    company_rows = tuple(row for row in budgets if row.scope_type == "company")
    if not company_rows:
        return None
    return sum((row.budget_amount for row in company_rows), Decimal("0"))


def _fallback_total_budget(budgets: tuple[BudgetRow, ...]) -> Decimal:
    department_rows = tuple(row for row in budgets if row.scope_type == "department")
    if department_rows:
        return sum((row.budget_amount for row in department_rows), Decimal("0"))
    for scope_type in ("project", "cost_center", "key"):
        scope_rows = tuple(row for row in budgets if row.scope_type == scope_type)
        if scope_rows:
            return sum((row.budget_amount for row in scope_rows), Decimal("0"))
    return Decimal("0")


def _budget_utilization(*, usage: tuple[MonthlyUsageRow, ...], budgets: tuple[BudgetRow, ...]) -> list[dict[str, object]]:
    entries = [
        {
            "scope_type": budget.scope_type,
            "scope_id": budget.scope_id,
            "budget_amount": format_money(budget.budget_amount),
            "actual_spend": format_money(_actual_spend(usage=usage, scope_type=budget.scope_type, scope_id=budget.scope_id)),
            "budget_utilization_rate": _rate(
                _actual_spend(usage=usage, scope_type=budget.scope_type, scope_id=budget.scope_id),
                budget.budget_amount,
            ),
            "status": _budget_status(
                _actual_spend(usage=usage, scope_type=budget.scope_type, scope_id=budget.scope_id),
                budget.budget_amount,
            ),
        }
        for budget in budgets
    ]
    return sorted(entries, key=lambda item: (str(item["scope_type"]), str(item["scope_id"])))


def _actual_spend(*, usage: tuple[MonthlyUsageRow, ...], scope_type: str, scope_id: str) -> Decimal:
    if scope_type == "company":
        return sum((row.spend for row in usage), Decimal("0"))
    if scope_type == "department":
        return sum((row.spend for row in usage if row.department_id == scope_id), Decimal("0"))
    if scope_type == "project":
        return sum((row.spend for row in usage if row.project_id == scope_id), Decimal("0"))
    if scope_type == "cost_center":
        return sum((row.spend for row in usage if row.cost_center_id == scope_id), Decimal("0"))
    if scope_type == "key":
        return sum((row.spend for row in usage if row.key_alias == scope_id), Decimal("0"))
    return Decimal("0")


def _budget_status(actual_spend: Decimal, budget_amount: Decimal) -> str:
    if budget_amount <= 0:
        return "exceeded" if actual_spend > 0 else "ok"
    utilization = actual_spend / budget_amount
    if utilization > 1:
        return "exceeded"
    if utilization >= Decimal("0.8"):
        return "warning"
    return "ok"


def _budget_lookup(budget_utilization: list[dict[str, object]], *, scope_type: str) -> dict[str, dict[str, object]]:
    return {
        str(entry["scope_id"]): entry
        for entry in budget_utilization
        if entry.get("scope_type") == scope_type
    }


def _rank_dimension(
    *,
    usage: tuple[MonthlyUsageRow, ...],
    field_name: str,
    label: str,
    top_n: int,
    budget_lookup: Mapping[str, Mapping[str, object]],
) -> list[dict[str, object]]:
    buckets = {
        value: {
            label: value,
            "spend": format_money(sum((row.spend for row in usage if getattr(row, field_name) == value), Decimal("0"))),
            "request_count": sum(row.request_count for row in usage if getattr(row, field_name) == value),
        }
        for value in frozenset(getattr(row, field_name) for row in usage)
    }
    enriched = tuple(_with_budget(item, budget_lookup.get(str(item[label]))) for item in buckets.values())
    return sorted(enriched, key=lambda item: (-Decimal(str(item["spend"])), str(item[label])))[: max(top_n, 0)]


def _with_budget(item: dict[str, object], budget: Mapping[str, object] | None) -> dict[str, object]:
    if budget is None:
        return item
    return {
        **item,
        "budget_amount": budget["budget_amount"],
        "budget_utilization_rate": budget["budget_utilization_rate"],
    }


def _anomalies(report: Mapping[str, object]) -> list[dict[str, object]]:
    alert_anomalies = tuple(_alert_anomaly(alert) for alert in _alert_rows(report))
    if alert_anomalies:
        return _unique_anomalies(alert_anomalies)

    metric_anomalies = tuple(
        item
        for item in (
            _metric_anomaly(report.get("metrics", {}), "http_429_count", "aimanager_http_429_seen", "warning"),
            _metric_anomaly(report.get("metrics", {}), "http_5xx_count", "aimanager_http_5xx_seen", "high"),
            _metric_anomaly(report.get("metrics", {}), "budget_blocked_count", "aimanager_budget_blocked_seen", "high"),
            _metric_anomaly(
                report.get("metrics", {}),
                "passthrough_blocked_count",
                "aimanager_passthrough_blocked_seen",
                "warning",
            ),
            _metric_anomaly(
                report.get("metrics", {}),
                "missing_request_id_count",
                "aimanager_request_id_missing",
                "high",
            ),
        )
        if item is not None
    )
    return _unique_anomalies(metric_anomalies)


def _alert_rows(report: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    alerts = report.get("alerts", [])
    if not isinstance(alerts, list):
        return ()
    return tuple(alert for alert in alerts if isinstance(alert, Mapping))


def _alert_anomaly(alert: Mapping[str, object]) -> dict[str, object]:
    return {
        "source": "observability_alert",
        "code": _text(alert.get("code")),
        "severity": _text(alert.get("severity")),
        **_optional_fields(alert, ("count", "value", "threshold")),
    }


def _unique_anomalies(rows: tuple[dict[str, object], ...]) -> list[dict[str, object]]:
    codes = tuple(str(row["code"]) for row in rows)
    return [row for index, row in enumerate(rows) if str(row["code"]) not in codes[:index]]


def _metric_anomaly(metrics: object, field_name: str, code: str, severity: str) -> dict[str, object] | None:
    if not isinstance(metrics, Mapping):
        return None
    count = _non_negative_int(metrics.get(field_name), field_name)
    if count == 0:
        return None
    return {
        "source": "observability_metric",
        "code": code,
        "severity": severity,
        "count": count,
    }


def _optional_fields(row: Mapping[str, object], field_names: tuple[str, ...]) -> dict[str, object]:
    return {field: row[field] for field in field_names if row.get(field) not in (None, "")}


def _markdown(
    *,
    month: str,
    summary: Mapping[str, object],
    top_departments: list[dict[str, object]],
    top_projects: list[dict[str, object]],
    top_keys: list[dict[str, object]],
    anomalies: list[dict[str, object]],
) -> str:
    total_spend = summary["total_spend"]
    budget_amount = summary["budget_amount"]
    currency = summary["currency"]
    utilization = _utilization_label(str(summary["budget_utilization_rate"]))
    lines = [
        f"# AiManager 经营总览 - {month}",
        "",
        f"- 月费用：{total_spend} {currency}",
        f"- 预算消耗：{total_spend} / {budget_amount} {currency} ({utilization})",
        f"- 请求量：{summary['request_count']}，失败请求：{summary['failed_requests']}",
        "",
        "## TOP 部门",
        *_rank_lines(top_departments, "department_id"),
        "",
        "## TOP 项目",
        *_rank_lines(top_projects, "project_id"),
        "",
        "## TOP Key",
        *_rank_lines(top_keys, "key_alias"),
        "",
        "## 异常事件",
        *(_anomaly_lines(anomalies) if anomalies else ["- 无"]),
    ]
    return "\n".join(lines) + "\n"


def _rank_lines(rows: list[dict[str, object]], label: str) -> list[str]:
    if not rows:
        return ["- 无"]
    return [
        f"- {row[label]}：{row['spend']}，请求 {row['request_count']}"
        for row in rows
    ]


def _anomaly_lines(rows: list[dict[str, object]]) -> list[str]:
    return [
        f"- [{row['severity']}] {row['code']}"
        for row in rows
    ]


def _utilization_label(rate: str) -> str:
    if rate == "over_budget":
        return "over budget"
    return f"{format((Decimal(rate) * Decimal('100')).quantize(_RATE_QUANT), 'f')}%"


def _rate(numerator: Decimal, denominator: Decimal) -> str:
    if denominator <= 0:
        if numerator > 0:
            return "over_budget"
        return "0.000000"
    return format((numerator / denominator).quantize(_RATE_QUANT), "f")


def _required_text(row: Mapping[str, object], field_name: str) -> str:
    value = _text(row.get(field_name))
    if value == _UNASSIGNED:
        raise ValueError(f"{field_name} is required")
    return value


def _text(value: object) -> str:
    if value is None:
        return _UNASSIGNED
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else _UNASSIGNED
    return str(value)


def _decimal(value: object, field_name: str) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a decimal number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a decimal number") from exc
    if not number.is_finite():
        raise ValueError(f"{field_name} must be a finite decimal number")
    if number < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return number


def _non_negative_int(value: object, field_name: str) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a non-negative integer")
    try:
        number = int(str(value))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a non-negative integer") from exc
    if number < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return number

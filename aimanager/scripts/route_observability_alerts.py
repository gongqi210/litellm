from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from aimanager.observability import (
    DEFAULT_FAILURE_RATE_ALERT_THRESHOLD,
    derive_observability_alerts,
)


RouteStatus = Literal["PASS", "FAIL", "BLOCKED"]
Severity = Literal["info", "warning", "high", "critical"]
SEVERITY_RANK: dict[str, int] = {
    "info": 0,
    "warning": 1,
    "high": 2,
    "critical": 3,
}
_SIX_PLACES = Decimal("0.000001")
METRIC_KEYS = (
    "request_count",
    "failed_requests",
    "failure_rate",
    "http_429_count",
    "http_5xx_count",
    "budget_blocked_count",
    "passthrough_blocked_count",
    "enforced_params_blocked_count",
    "missing_request_id_count",
)


@dataclass(frozen=True)
class WebhookResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True)
class AlertRouteResult:
    status: RouteStatus
    alert_count: int
    delivered_count: int
    detail: str
    payload: dict[str, Any] | None = None


PostWebhook = Callable[[str, dict[str, Any]], WebhookResponse]


def route_observability_alerts(
    *,
    report: dict[str, Any],
    webhook_url: str,
    dry_run: bool = False,
    min_severity: str = "warning",
    title: str = "AiManager observability alerts",
    post: PostWebhook | None = None,
    timeout_seconds: float = 10,
) -> AlertRouteResult:
    validation_detail = validate_observability_report_alerts(report)
    if validation_detail:
        return AlertRouteResult(
            status="FAIL",
            alert_count=_safe_alert_count(report),
            delivered_count=0,
            detail=validation_detail,
        )

    alerts = _selected_alerts(report, min_severity=min_severity)
    if not alerts:
        return AlertRouteResult(
            status="PASS",
            alert_count=0,
            delivered_count=0,
            detail=f"no alerts at or above severity {min_severity}",
        )

    routed_report = {**report, "alerts": alerts}
    payload = _wecom_markdown_payload(routed_report, title=title)
    if dry_run:
        return AlertRouteResult(
            status="PASS",
            alert_count=len(alerts),
            delivered_count=0,
            detail=f"dry-run rendered {len(alerts)} alert(s)",
            payload=payload,
        )

    if not webhook_url.strip():
        return AlertRouteResult(
            status="BLOCKED",
            alert_count=len(alerts),
            delivered_count=0,
            detail="missing AIMANAGER_WECOM_WEBHOOK_URL or --webhook-url for WeCom alert routing",
            payload=payload,
        )

    sender = post or _post_with_urllib(timeout_seconds=timeout_seconds)
    try:
        response = sender(webhook_url, payload)
    except (OSError, ValueError, http.client.HTTPException) as exc:
        return AlertRouteResult(
            status="FAIL",
            alert_count=len(alerts),
            delivered_count=0,
            detail=f"WeCom webhook request failed: {type(exc).__name__}",
            payload=payload,
        )
    evaluation = _evaluate_wecom_response(response)
    if evaluation:
        return AlertRouteResult(
            status="FAIL",
            alert_count=len(alerts),
            delivered_count=0,
            detail=evaluation,
            payload=payload,
        )
    return AlertRouteResult(
        status="PASS",
        alert_count=len(alerts),
        delivered_count=len(alerts),
        detail=f"sent {len(alerts)} alert(s) to WeCom webhook",
        payload=payload,
    )


def build_wecom_markdown(report: dict[str, Any], *, title: str = "AiManager observability alerts") -> str:
    alerts = _alerts(report)
    metrics = _metrics(report)
    lines = [f"### {title}", ""]
    if not alerts:
        lines.append("No active alerts.")
    else:
        lines.append(f"Active alerts: {len(alerts)}")
        lines.append("")
        for alert in alerts:
            code = _text(alert.get("code"), default="unknown_alert")
            severity = _text(alert.get("severity"), default="warning")
            detail = _alert_detail(alert)
            lines.append(f"- [{severity}] {code}{detail}")

    metric_lines = [f"{key}: {_text(metrics.get(key), default='0')}" for key in METRIC_KEYS if key in metrics]
    if metric_lines:
        lines.extend(["", "Metrics:", *[f"- {line}" for line in metric_lines]])
    return "\n".join(lines)


def _wecom_markdown_payload(report: dict[str, Any], *, title: str) -> dict[str, Any]:
    return {
        "msgtype": "markdown",
        "markdown": {
            "content": build_wecom_markdown(report, title=title),
        },
    }


def _selected_alerts(report: dict[str, Any], *, min_severity: str) -> list[dict[str, Any]]:
    min_rank = _severity_rank(min_severity)
    return [
        alert
        for alert in _alerts(report)
        if _severity_rank(_text(alert.get("severity"), default="warning")) >= min_rank
    ]


def _alerts(report: dict[str, Any]) -> list[dict[str, Any]]:
    alerts = report.get("alerts", [])
    if not isinstance(alerts, list):
        raise ValueError("observability report alerts must be a list")
    normalized: list[dict[str, Any]] = []
    for alert in alerts:
        if not isinstance(alert, dict):
            raise ValueError("observability report alerts must contain objects")
        normalized.append(alert)
    return normalized


def _metrics(report: dict[str, Any]) -> dict[str, Any]:
    metrics = report.get("metrics", {})
    if not isinstance(metrics, dict):
        raise ValueError("observability report metrics must be an object")
    return metrics


def validate_observability_report_alerts(report: dict[str, Any]) -> str:
    try:
        provided_alerts = _alerts(report)
        expected_alerts = derive_observability_alerts(
            _metrics(report),
            failure_rate_alert_threshold=_validation_failure_rate_threshold(report),
        )
        alerts_match = _canonical_alerts(provided_alerts) == _canonical_alerts(expected_alerts)
    except ValueError as exc:
        detail = str(exc).strip()
        suffix = f": {detail}" if detail else ""
        return f"observability report alerts could not be validated: {type(exc).__name__}{suffix}"
    if alerts_match:
        return ""
    return (
        "observability report alerts do not match metrics-derived alerts; "
        f"provided_codes={_alert_codes(provided_alerts)} expected_codes={_alert_codes(expected_alerts)}"
    )


def _validation_failure_rate_threshold(report: dict[str, Any]) -> Decimal:
    policy = report.get("alert_policy")
    if isinstance(policy, dict):
        declared = policy.get("failure_rate_alert_threshold")
        if declared not in (None, ""):
            try:
                parsed = Decimal(str(declared))
            except (InvalidOperation, ValueError, TypeError):
                return DEFAULT_FAILURE_RATE_ALERT_THRESHOLD
            if parsed.is_finite() and Decimal(0) <= parsed <= DEFAULT_FAILURE_RATE_ALERT_THRESHOLD:
                return parsed
    return DEFAULT_FAILURE_RATE_ALERT_THRESHOLD


def _canonical_alerts(alerts: list[dict[str, Any]]) -> list[dict[str, str]]:
    return sorted(
        (_canonical_alert(alert) for alert in alerts),
        key=lambda alert: alert.get("code", ""),
    )


def _canonical_alert(alert: dict[str, Any]) -> dict[str, str]:
    canonical = {
        "code": _text(alert.get("code"), default=""),
        "severity": _text(alert.get("severity"), default="warning").lower(),
    }
    if "count" in alert:
        canonical["count"] = _canonical_count(alert["count"])
    for field_name in ("value", "threshold"):
        if field_name in alert:
            canonical[field_name] = _canonical_decimal(alert[field_name], field_name)
    return canonical


def _canonical_count(value: Any) -> str:
    if isinstance(value, bool):
        raise ValueError("alert count must be an integer")
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("alert count must be an integer") from exc
    if count < 0:
        raise ValueError("alert count must be a non-negative integer")
    return str(count)


def _canonical_decimal(value: Any, field_name: str) -> str:
    if value in (None, "") or isinstance(value, bool):
        raise ValueError(f"alert {field_name} must be a decimal number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"alert {field_name} must be a decimal number") from exc
    if not number.is_finite():
        raise ValueError(f"alert {field_name} must be a finite decimal number")
    return format(number.quantize(_SIX_PLACES), "f")


def _alert_codes(alerts: list[dict[str, Any]]) -> list[str]:
    return sorted(_text(alert.get("code"), default="unknown_alert") for alert in alerts)


def _safe_alert_count(report: dict[str, Any]) -> int:
    alerts = report.get("alerts", [])
    return len(alerts) if isinstance(alerts, list) else 0


def _alert_detail(alert: dict[str, Any]) -> str:
    details: list[str] = []
    for key in ("count", "value", "threshold"):
        if key in alert:
            details.append(f"{key}={_text(alert.get(key), default='')}")
    return f" ({', '.join(details)})" if details else ""


def _severity_rank(severity: str) -> int:
    return SEVERITY_RANK.get(severity.strip().lower(), SEVERITY_RANK["warning"])


def _evaluate_wecom_response(response: WebhookResponse) -> str:
    if response.status_code != 200:
        return f"WeCom webhook returned HTTP {response.status_code}"
    try:
        payload = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "WeCom webhook returned non-JSON response"
    if not isinstance(payload, dict):
        return "WeCom webhook returned invalid JSON response"
    errcode = payload.get("errcode")
    if errcode != 0:
        return f"WeCom webhook returned errcode={errcode}"
    return ""


def _post_with_urllib(*, timeout_seconds: float) -> PostWebhook:
    def post(url: str, payload: dict[str, Any]) -> WebhookResponse:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                return WebhookResponse(
                    status_code=response.status,
                    headers=dict(response.headers.items()),
                    body=response.read(),
                )
        except urllib.error.HTTPError as exc:
            return WebhookResponse(
                status_code=exc.code,
                headers=dict(exc.headers.items()),
                body=exc.read(),
            )

    return post


def _load_report(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _write_payload(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _text(value: Any, *, default: str) -> str:
    if value is None:
        return default
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else default
    return str(value)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Route AiManager observability alerts to WeCom.")
    parser.add_argument("--report-file", required=True, help="JSON report created by export_observability.")
    parser.add_argument("--webhook-url", default=os.environ.get("AIMANAGER_WECOM_WEBHOOK_URL", ""))
    parser.add_argument("--dry-run", action="store_true", help="Render payload without sending to WeCom.")
    parser.add_argument("--output-payload-file", help="Optional path to write the rendered WeCom payload JSON.")
    parser.add_argument("--min-severity", choices=tuple(SEVERITY_RANK), default="warning")
    parser.add_argument("--title", default="AiManager observability alerts")
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args(argv)

    try:
        report = _load_report(Path(args.report_file))
        result = route_observability_alerts(
            report=report,
            webhook_url=args.webhook_url,
            dry_run=args.dry_run,
            min_severity=args.min_severity,
            title=args.title,
            timeout_seconds=args.timeout,
        )
        if args.output_payload_file and result.payload is not None:
            _write_payload(Path(args.output_payload_file), result.payload)
    except Exception as exc:
        print(f"FAIL observability alert routing: {type(exc).__name__}", file=sys.stderr)
        return 1

    print(
        f"{result.status} observability alert routing: "
        f"alerts={result.alert_count} delivered={result.delivered_count} {result.detail}"
    )
    if result.status == "FAIL":
        return 1
    if result.status == "BLOCKED":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

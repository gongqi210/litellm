from __future__ import annotations

from decimal import Decimal

from aimanager.observability import build_observability_report, parse_audit_events_from_log_lines


def test_observability_report_counts_request_failures_and_audit_events() -> None:
    audit_lines = [
        'INFO aimanager_audit_event={"event_type":"passthrough_blocked","request_id":"req_passthrough_1","reason":"aimanager_passthrough_blocked","severity":"warning"}',
        'INFO aimanager_audit_event={"event_type":"enforced_params_blocked","request_id":"req_shared_1","reason":"aimanager_enforced_params_missing","severity":"warning"}',
        'INFO aimanager_audit_event={"event_type":"budget_blocked","request_id":"req_budget_1","reason":"budget_exceeded","severity":"high"}',
        'INFO aimanager_audit_event={"event_type":"key_revoked","request_id":"req_revoke_1","reason":"offboarding","severity":"critical"}',
    ]
    request_rows = [
        {
            "request_id": "req_chat_ok",
            "status_code": "200",
            "status": "success",
            "model": "openai/gemini-2.5-flash",
            "team_id": "team_market",
            "key_alias": "market-key",
            "latency_ms": "120",
            "total_tokens": "150",
            "spend": "0.002",
        },
        {
            "request_id": "req_rate_limited",
            "status_code": "429",
            "status": "failed",
            "model": "openai/gemini-2.5-flash",
            "team_id": "team_market",
            "key_alias": "market-key",
            "latency_ms": "30",
            "total_tokens": "0",
            "spend": "0",
        },
        {
            "request_id": "req_ycapi_5xx",
            "status_code": "503",
            "status": "failed",
            "model": "openai/deepseek-chat",
            "team_id": "team_dev",
            "key_alias": "dev-key",
            "latency_ms": "950",
            "total_tokens": "0",
            "spend": "0",
        },
    ]

    report = build_observability_report(audit_log_lines=audit_lines, request_rows=request_rows)

    metrics = report["metrics"]
    assert metrics["request_count"] == 3
    assert metrics["successful_requests"] == 1
    assert metrics["failed_requests"] == 2
    assert metrics["failure_rate"] == Decimal("0.666667")
    assert metrics["http_429_count"] == 1
    assert metrics["http_5xx_count"] == 1
    assert metrics["average_latency_ms"] == Decimal("366.666667")
    assert metrics["total_tokens"] == 150
    assert metrics["spend"] == Decimal("0.002")
    assert metrics["audit_events"]["passthrough_blocked"] == 1
    assert metrics["audit_events"]["enforced_params_blocked"] == 1
    assert metrics["audit_events"]["budget_blocked"] == 1
    assert metrics["audit_events"]["key_revoked"] == 1
    assert metrics["enforced_params_blocked_count"] == 1
    assert metrics["budget_blocked_count"] == 1
    assert metrics["passthrough_blocked_count"] == 1
    assert metrics["observed_request_ids"] == [
        "req_budget_1",
        "req_chat_ok",
        "req_passthrough_1",
        "req_rate_limited",
        "req_revoke_1",
        "req_shared_1",
        "req_ycapi_5xx",
    ]

    market_bucket = next(row for row in report["by_key_model"] if row["key_alias"] == "market-key")
    assert market_bucket["team_id"] == "team_market"
    assert market_bucket["model"] == "gemini-2.5-flash"
    assert market_bucket["request_count"] == 2
    assert market_bucket["failed_requests"] == 1


def test_observability_report_emits_machine_readable_alerts() -> None:
    report = build_observability_report(
        audit_log_lines=[
            'aimanager_audit_event={"event_type":"passthrough_blocked","request_id":"req_block","reason":"provider bypass"}',
            'aimanager_audit_event={"event_type":"budget_blocked","request_id":"req_budget","reason":"budget_exceeded"}',
        ],
        request_rows=[
            {"request_id": "req_ok", "status_code": 200, "status": "success"},
            {"request_id": "req_429", "status_code": 429, "status": "failed"},
            {"request_id": "req_500", "status_code": 500, "status": "failed"},
            {"request_id": "", "status_code": 200, "status": "success"},
        ],
        failure_rate_alert_threshold=Decimal("0.10"),
    )

    alerts = {alert["code"]: alert for alert in report["alerts"]}
    assert alerts["aimanager_failure_rate_high"]["severity"] == "high"
    assert alerts["aimanager_failure_rate_high"]["value"] == Decimal("0.500000")
    assert alerts["aimanager_http_429_seen"]["count"] == 1
    assert alerts["aimanager_http_5xx_seen"]["severity"] == "high"
    assert alerts["aimanager_budget_blocked_seen"]["severity"] == "high"
    assert alerts["aimanager_passthrough_blocked_seen"]["severity"] == "warning"
    assert alerts["aimanager_request_id_missing"]["count"] == 1


def test_parse_audit_events_from_log_lines_ignores_malformed_and_non_audit_lines() -> None:
    events = parse_audit_events_from_log_lines(
        [
            "Authorization: Bearer must-not-be-captured",
            'aimanager_audit_event={"event_type":"policy_blocked","request_id":"req_policy","reason":"config update"} trailing text',
            "aimanager_audit_event={not-json}",
            'aimanager_audit_event=["not", "an", "object"]',
        ]
    )

    assert events == [
        {
            "event_type": "policy_blocked",
            "request_id": "req_policy",
            "reason": "config update",
        }
    ]

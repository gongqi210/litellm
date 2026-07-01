from __future__ import annotations

import json
from typing import Any

from aimanager.scripts.route_observability_alerts import (
    WebhookResponse,
    build_wecom_markdown,
    main,
    route_observability_alerts,
)


def test_build_wecom_markdown_summarizes_alert_codes_and_metrics() -> None:
    content = build_wecom_markdown(_report_with_alerts(), title="AiManager production alerts")

    assert "AiManager production alerts" in content
    assert "aimanager_failure_rate_high" in content
    assert "aimanager_budget_blocked_seen" in content
    assert "failure_rate: 0.500000" in content
    assert "request_count: 4" in content
    assert "webhook" not in content.lower()


def test_route_observability_alerts_noops_when_no_alerts() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    result = route_observability_alerts(
        report={"metrics": {"request_count": 0}, "alerts": []},
        webhook_url="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret",
        post=lambda url, payload: calls.append((url, payload)) or WebhookResponse(200, {}, b'{"errcode":0}'),
    )

    assert result.status == "PASS"
    assert result.alert_count == 0
    assert result.delivered_count == 0
    assert calls == []


def test_route_observability_alerts_dry_run_renders_payload_without_webhook() -> None:
    result = route_observability_alerts(
        report=_report_with_alerts(),
        webhook_url="",
        dry_run=True,
    )

    assert result.status == "PASS"
    assert result.alert_count == 3
    assert result.delivered_count == 0
    assert result.payload is not None
    assert result.payload["msgtype"] == "markdown"
    assert "aimanager_http_429_seen" in result.payload["markdown"]["content"]


def test_route_observability_alerts_fails_when_alerts_do_not_match_metrics() -> None:
    result = route_observability_alerts(
        report={
            "metrics": {
                "request_count": 1,
                "failed_requests": 0,
                "failure_rate": "0.000000",
                "http_429_count": 0,
                "http_5xx_count": 0,
                "budget_blocked_count": 0,
                "passthrough_blocked_count": 0,
                "enforced_params_blocked_count": 0,
                "missing_request_id_count": 0,
            },
            "alerts": [{"code": "aimanager_failure_rate_high", "severity": "high"}],
        },
        webhook_url="",
        dry_run=True,
    )

    assert result.status == "FAIL"
    assert result.alert_count == 1
    assert result.delivered_count == 0
    assert "metrics-derived alerts" in result.detail


def test_route_observability_alerts_blocks_when_webhook_missing() -> None:
    result = route_observability_alerts(
        report=_report_with_alerts(),
        webhook_url="",
        dry_run=False,
    )

    assert result.status == "BLOCKED"
    assert result.alert_count == 3
    assert "missing AIMANAGER_WECOM_WEBHOOK_URL" in result.detail


def test_route_observability_alerts_posts_wecom_markdown_payload() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def post(url: str, payload: dict[str, Any]) -> WebhookResponse:
        calls.append((url, payload))
        return WebhookResponse(
            status_code=200,
            headers={"Content-Type": "application/json"},
            body=b'{"errcode":0,"errmsg":"ok"}',
        )

    result = route_observability_alerts(
        report=_report_with_alerts(),
        webhook_url="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret",
        post=post,
    )

    assert result.status == "PASS"
    assert result.delivered_count == 3
    assert len(calls) == 1
    assert calls[0][1]["msgtype"] == "markdown"
    assert "secret" not in result.detail


def test_route_observability_alerts_fails_on_wecom_error_code() -> None:
    result = route_observability_alerts(
        report=_report_with_alerts(),
        webhook_url="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret",
        post=lambda url, payload: WebhookResponse(200, {}, b'{"errcode":93000,"errmsg":"invalid webhook"}'),
    )

    assert result.status == "FAIL"
    assert result.delivered_count == 0
    assert "errcode=93000" in result.detail


def test_route_observability_alerts_fails_on_non_200_response() -> None:
    result = route_observability_alerts(
        report=_report_with_alerts(),
        webhook_url="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret",
        post=lambda url, payload: WebhookResponse(500, {}, b"server error"),
    )

    assert result.status == "FAIL"
    assert "HTTP 500" in result.detail


def test_route_observability_alerts_masks_malformed_webhook_url() -> None:
    for webhook_url in (
        "not-a-url?key=secret-webhook-token",
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/se nd?key=secret-webhook-token",
    ):
        result = route_observability_alerts(
            report=_report_with_alerts(),
            webhook_url=webhook_url,
        )

        assert result.status == "FAIL"
        assert "secret-webhook-token" not in result.detail
        assert webhook_url not in result.detail


def test_route_observability_alerts_cli_masks_malformed_webhook_url(tmp_path, capsys) -> None:
    report_file = tmp_path / "observability.json"
    report_file.write_text(json.dumps(_report_with_alerts()), encoding="utf-8")

    for webhook_url in (
        "not-a-url?key=secret-webhook-token",
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/se nd?key=secret-webhook-token",
    ):
        exit_code = main(
            [
                "--report-file",
                str(report_file),
                "--webhook-url",
                webhook_url,
            ]
        )

        captured = capsys.readouterr()
        output = captured.out + captured.err
        assert exit_code == 1
        assert "secret-webhook-token" not in output
        assert webhook_url not in output


def test_route_observability_alerts_filters_by_min_severity() -> None:
    result = route_observability_alerts(
        report=_report_with_alerts(),
        webhook_url="",
        dry_run=True,
        min_severity="high",
    )

    assert result.status == "PASS"
    assert result.alert_count == 2
    assert result.payload is not None
    content = result.payload["markdown"]["content"]
    assert "aimanager_failure_rate_high" in content
    assert "aimanager_budget_blocked_seen" in content
    assert "aimanager_http_429_seen" not in content


def test_route_observability_alerts_cli_writes_dry_run_payload(tmp_path) -> None:
    report_file = tmp_path / "observability.json"
    payload_file = tmp_path / "wecom_payload.json"
    report_file.write_text(json.dumps(_report_with_alerts()), encoding="utf-8")

    exit_code = main(
        [
            "--report-file",
            str(report_file),
            "--dry-run",
            "--output-payload-file",
            str(payload_file),
            "--min-severity",
            "high",
        ]
    )

    assert exit_code == 0
    payload = json.loads(payload_file.read_text(encoding="utf-8"))
    assert payload["msgtype"] == "markdown"
    assert "aimanager_budget_blocked_seen" in payload["markdown"]["content"]
    assert "aimanager_http_429_seen" not in payload["markdown"]["content"]


def _report_with_alerts() -> dict[str, Any]:
    return {
        "metrics": {
            "request_count": 4,
            "failed_requests": 2,
            "failure_rate": "0.500000",
            "http_429_count": 1,
            "http_5xx_count": 0,
            "budget_blocked_count": 1,
            "passthrough_blocked_count": 0,
            "missing_request_id_count": 0,
        },
        "alerts": [
            {
                "code": "aimanager_failure_rate_high",
                "severity": "high",
                "value": "0.500000",
                "threshold": "0.100000",
            },
            {
                "code": "aimanager_http_429_seen",
                "severity": "warning",
                "count": 1,
            },
            {
                "code": "aimanager_budget_blocked_seen",
                "severity": "high",
                "count": 1,
            },
        ],
    }

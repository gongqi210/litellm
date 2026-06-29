from __future__ import annotations

import csv
import json

from aimanager.scripts.export_observability import main


def test_observability_export_script_writes_json_report(tmp_path) -> None:
    audit_file = tmp_path / "aimanager.log"
    request_file = tmp_path / "request_status.csv"
    output_file = tmp_path / "aimanager_observability_report.json"

    audit_file.write_text(
        "\n".join(
            [
                'INFO aimanager_audit_event={"event_type":"passthrough_blocked","request_id":"req_passthrough","reason":"aimanager_passthrough_blocked"}',
                'INFO aimanager_audit_event={"event_type":"budget_blocked","request_id":"req_budget","reason":"budget_exceeded"}',
            ]
        ),
        encoding="utf-8",
    )
    with request_file.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "request_id",
                "status_code",
                "status",
                "model",
                "team_id",
                "key_alias",
                "latency_ms",
                "total_tokens",
                "spend",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "request_id": "req_ok",
                "status_code": "200",
                "status": "success",
                "model": "openai/gemini-2.5-flash",
                "team_id": "team_market",
                "key_alias": "market-key",
                "latency_ms": "100",
                "total_tokens": "42",
                "spend": "0.001",
            }
        )
        writer.writerow(
            {
                "request_id": "req_503",
                "status_code": "503",
                "status": "failed",
                "model": "openai/gemini-2.5-flash",
                "team_id": "team_market",
                "key_alias": "market-key",
                "latency_ms": "900",
                "total_tokens": "0",
                "spend": "0",
            }
        )

    exit_code = main(
        [
            "--audit-log-file",
            str(audit_file),
            "--request-status-file",
            str(request_file),
            "--output-file",
            str(output_file),
            "--failure-rate-alert-threshold",
            "0.10",
        ]
    )

    assert exit_code == 0
    payload = json.loads(output_file.read_text(encoding="utf-8"))
    assert payload["metrics"]["request_count"] == 2
    assert payload["metrics"]["failed_requests"] == 1
    assert payload["metrics"]["failure_rate"] == "0.500000"
    assert payload["metrics"]["http_5xx_count"] == 1
    assert payload["metrics"]["audit_events"]["passthrough_blocked"] == 1
    assert payload["metrics"]["budget_blocked_count"] == 1
    assert {alert["code"] for alert in payload["alerts"]} >= {
        "aimanager_failure_rate_high",
        "aimanager_http_5xx_seen",
        "aimanager_budget_blocked_seen",
        "aimanager_passthrough_blocked_seen",
    }

from __future__ import annotations

import json

from aimanager.scripts.smoke_blocked_routes import HttpResponse
from aimanager.scripts.smoke_key_lifecycle import (
    AuditLogEvent,
    parse_audit_events_from_logs,
    run_key_lifecycle_smoke,
)


def test_key_lifecycle_smoke_passes_when_freeze_and_revoke_reject_inference_and_emit_audits() -> None:
    requests: list[tuple[str, str, dict[str, str], dict[str, object] | None]] = []

    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        payload = json.loads(body.decode("utf-8")) if body else None
        requests.append((method, url, headers, payload))
        if url.endswith("/key/generate"):
            assert headers["x-aimanager-role"] == "proxy_admin"
            assert headers["x-aimanager-actor"] == "aimanager-ci"
            assert payload is not None
            if payload["key_alias"] == "aimanager-lifecycle-freeze-lifecycle-123":
                return _json_response({"key": "sk-freeze-smoke"})
            if payload["key_alias"] == "aimanager-lifecycle-revoke-lifecycle-123":
                return _json_response({"key": "sk-revoke-smoke"})
            raise AssertionError(f"unexpected key alias {payload['key_alias']}")
        if url.endswith("/v1/chat/completions"):
            token = headers["Authorization"].removeprefix("Bearer ")
            if token == "sk-freeze-smoke" and headers["x-request-id"].startswith("lifecycle-freeze-reject"):
                return _json_response(
                    {"error": {"type": "authentication_error", "message": "Key is blocked."}},
                    status_code=401,
                )
            if token == "sk-revoke-smoke" and headers["x-request-id"].startswith("lifecycle-revoke-reject"):
                return _json_response(
                    {"error": {"type": "authentication_error", "message": "Invalid proxy server token"}},
                    status_code=401,
                )
            return _json_response({"id": "chatcmpl-lifecycle-smoke"})
        if url.endswith("/key/block"):
            assert headers["x-aimanager-role"] == "proxy_admin"
            assert headers["x-aimanager-actor"] == "aimanager-ci"
            assert headers["x-aimanager-reason"] == "AC-11 freeze lifecycle smoke"
            assert headers["x-aimanager-key-alias"] == "aimanager-lifecycle-freeze-lifecycle-123"
            assert payload == {"key": "sk-freeze-smoke"}
            return _json_response({"key_alias": "aimanager-lifecycle-freeze-lifecycle-123", "blocked": True})
        if url.endswith("/key/delete"):
            assert headers["x-aimanager-role"] == "proxy_admin"
            assert headers["x-aimanager-actor"] == "aimanager-ci"
            assert headers["x-aimanager-reason"] in {
                "AC-11 revoke lifecycle smoke",
                "AiManager smoke cleanup",
            }
            return _json_response({"deleted_keys": payload["keys"]})  # type: ignore[index]
        raise AssertionError(f"unexpected URL {url}")

    def poll_audit_events(request_marker: str) -> list[AuditLogEvent]:
        assert request_marker == "lifecycle-123"
        return [
            AuditLogEvent(
                event_type="key_frozen",
                request_id="lifecycle-freeze-lifecycle-123",
                reason="AC-11 freeze lifecycle smoke",
            ),
            AuditLogEvent(
                event_type="key_revoked",
                request_id="lifecycle-revoke-lifecycle-123",
                reason="AC-11 revoke lifecycle smoke",
            ),
        ]

    result = run_key_lifecycle_smoke(
        business_base_url="http://localhost:4000",
        admin_base_url="http://localhost:4001",
        master_key="local-master-key",
        request_marker="lifecycle-123",
        fetch=fetch,
        poll_audit_events=poll_audit_events,
        sleep=lambda _seconds: None,
        poll_attempts=1,
    )

    assert result.passed is True
    assert result.detail == "key freeze and revoke are enforced and audited"
    assert result.freeze_reject_status_code == 401
    assert result.revoke_reject_status_code == 401
    assert result.freeze_error_message == "Key is blocked."
    assert result.revoke_error_message == "Invalid proxy server token"
    assert result.audit_event_types == ("key_frozen", "key_revoked")
    assert any(request[1].endswith("/key/block") for request in requests)
    assert any(
        request[1].endswith("/key/delete") and request[2]["x-aimanager-reason"] == "AC-11 revoke lifecycle smoke"
        for request in requests
    )


def test_key_lifecycle_smoke_fails_when_frozen_key_still_allows_inference() -> None:
    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        payload = json.loads(body.decode("utf-8")) if body else None
        if url.endswith("/key/generate"):
            assert headers["x-aimanager-role"] == "proxy_admin"
            return _json_response({"key": "sk-freeze-smoke"})
        if url.endswith("/key/block") or url.endswith("/key/delete"):
            assert headers["x-aimanager-role"] == "proxy_admin"
            return _json_response({"ok": True})
        if url.endswith("/v1/chat/completions"):
            return _json_response({"id": "chatcmpl-allowed"})
        raise AssertionError(f"unexpected URL {url}; payload={payload}")

    result = run_key_lifecycle_smoke(
        business_base_url="http://localhost:4000",
        admin_base_url="http://localhost:4001",
        master_key="local-master-key",
        request_marker="lifecycle-123",
        fetch=fetch,
        poll_audit_events=lambda _request_marker: [],
        sleep=lambda _seconds: None,
        poll_attempts=1,
    )

    assert result.passed is False
    assert "frozen key was not rejected" in result.detail


def test_parse_audit_events_from_logs_extracts_aimanager_events() -> None:
    logs = """
INFO ignored line
aimanager_audit_event={"event_type":"key_frozen","request_id":"lifecycle-freeze-1","reason":"AC-11 freeze"}
aimanager_audit_event={"event_type":"key_revoked","request_id":"lifecycle-revoke-1","reason":"AC-11 revoke"}
aimanager_audit_event={not-json}
"""

    events = parse_audit_events_from_logs(logs)

    assert events == [
        AuditLogEvent(event_type="key_frozen", request_id="lifecycle-freeze-1", reason="AC-11 freeze"),
        AuditLogEvent(event_type="key_revoked", request_id="lifecycle-revoke-1", reason="AC-11 revoke"),
    ]


def _json_response(payload: dict[str, object], status_code: int = 200) -> HttpResponse:
    return HttpResponse(
        status_code=status_code,
        headers={"content-type": "application/json"},
        body=json.dumps(payload).encode("utf-8"),
    )

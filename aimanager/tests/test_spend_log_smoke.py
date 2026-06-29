from __future__ import annotations

import json

from aimanager.scripts.smoke_blocked_routes import HttpResponse
from aimanager.scripts.smoke_spend_logs import (
    SpendLogRow,
    _delete_virtual_key,
    build_governed_key_payload,
    parse_spend_log_rows,
    run_spend_log_smoke,
)


def test_build_governed_key_payload_covers_aimanager_metadata_and_shared_key_enforcement() -> None:
    payload = build_governed_key_payload(
        request_marker="smoke-123",
        models=["gemini-2.5-flash", "ycapi-image-1"],
    )

    assert payload["user_id"] == "aimanager-smoke-user"
    assert payload["team_id"] == "team_aimanager_smoke"
    assert payload["models"] == ["gemini-2.5-flash", "ycapi-image-1"]
    assert payload["max_budget"] > 0
    assert payload["rpm_limit"] > 0
    assert payload["tpm_limit"] > 0
    assert payload["duration"] == "1h"
    assert payload["metadata"]["owner"] == "aimanager-smoke"
    assert payload["metadata"]["department_id"] == "dept_smoke"
    assert payload["metadata"]["project_id"] == "proj_aimanager_runtime_smoke"
    assert payload["metadata"]["cost_center_id"] == "cc_smoke"
    assert payload["metadata"]["scenario_l1"] == "engineering"
    assert payload["metadata"]["scenario_l2"] == "runtime-spend-smoke"
    assert payload["metadata"]["approver"] == "aimanager-ci"
    assert payload["metadata"]["internal_or_external"] == "internal"
    assert payload["metadata"]["shared_key"] is True
    assert payload["metadata"]["end_user_principal"] == "employee-smoke-001"
    assert payload["metadata"]["aimanager_smoke_id"] == "smoke-123"


def test_build_governed_key_payload_can_create_employee_key_without_shared_key_enforcement() -> None:
    payload = build_governed_key_payload(
        request_marker="smoke-123",
        models=["gemini-2.5-flash"],
        shared_key=False,
    )

    assert "shared_key" not in payload["metadata"]
    assert payload["metadata"]["end_user_principal"] == "employee-smoke-001"


def test_build_governed_key_payload_accepts_budget_alias_and_scenario_overrides() -> None:
    payload = build_governed_key_payload(
        request_marker="budget-123",
        models=["gemini-2.5-flash"],
        shared_key=False,
        max_budget=0.005,
        key_alias_prefix="aimanager-budget-smoke",
        scenario_l2="runtime-budget-block-smoke",
    )

    assert payload["key_alias"] == "aimanager-budget-smoke-budget-123"
    assert payload["max_budget"] == 0.005
    assert payload["metadata"]["scenario_l2"] == "runtime-budget-block-smoke"


def test_spend_log_smoke_passes_when_chat_and_image_spend_rows_are_nonzero() -> None:
    requests: list[tuple[str, str, dict[str, str], dict[str, object] | None]] = []
    poll_count = 0

    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        nonlocal poll_count
        payload = json.loads(body.decode("utf-8")) if body else None
        requests.append((method, url, headers, payload))
        if url.endswith("/key/generate"):
            assert headers["x-aimanager-role"] == "proxy_admin"
            assert headers["x-aimanager-actor"] == "aimanager-ci"
            return _json_response({"key": "sk-virtual-smoke"})
        if url.endswith("/v1/chat/completions"):
            assert headers["Authorization"] == "Bearer sk-virtual-smoke"
            assert payload is not None
            assert payload["user"] == "employee-smoke-001"
            assert payload["metadata"]["scenario_l1"] == "engineering"  # type: ignore[index]
            assert payload["metadata"]["department_id"] == "dept_smoke"  # type: ignore[index]
            assert payload["metadata"]["project_id"] == "proj_aimanager_runtime_smoke"  # type: ignore[index]
            assert payload["metadata"]["cost_center_id"] == "cc_smoke"  # type: ignore[index]
            assert payload["metadata"]["currency"] == "CNY"  # type: ignore[index]
            assert payload["metadata"]["pricing_version"] == "m1-runtime-smoke"  # type: ignore[index]
            assert payload["metadata"]["image_count"] == 0  # type: ignore[index]
            return _json_response({"id": "chatcmpl-smoke", "object": "chat.completion"})
        if url.endswith("/v1/images/generations"):
            assert headers["Authorization"] == "Bearer sk-virtual-smoke"
            assert payload is not None
            assert payload["user"] == "employee-smoke-001"
            assert payload["metadata"]["scenario_l1"] == "engineering"  # type: ignore[index]
            assert payload["metadata"]["department_id"] == "dept_smoke"  # type: ignore[index]
            assert payload["metadata"]["project_id"] == "proj_aimanager_runtime_smoke"  # type: ignore[index]
            assert payload["metadata"]["cost_center_id"] == "cc_smoke"  # type: ignore[index]
            assert payload["metadata"]["currency"] == "CNY"  # type: ignore[index]
            assert payload["metadata"]["pricing_version"] == "m1-runtime-smoke"  # type: ignore[index]
            assert payload["metadata"]["image_count"] == 1  # type: ignore[index]
            return _json_response({"created": 1, "data": [{"url": "https://example.invalid/smoke.png"}]})
        if url.endswith("/key/delete"):
            assert headers["x-aimanager-role"] == "proxy_admin"
            assert headers["x-aimanager-actor"] == "aimanager-ci"
            assert headers["x-aimanager-reason"] == "AiManager smoke cleanup"
            return _json_response({"deleted": True})
        raise AssertionError(f"unexpected URL {url}")

    def poll_logs(marker: str) -> list[SpendLogRow]:
        nonlocal poll_count
        poll_count += 1
        if poll_count == 1:
            return []
        assert marker == "smoke-123"
        return [
            SpendLogRow(
                request_id="chat-req-smoke",
                call_type="completion",
                model="openai/gemini-2.5-flash",
                spend=0.000003,
            ),
            SpendLogRow(
                request_id="image-req-smoke",
                call_type="image_generation",
                model="openai/ycapi-image-1",
                spend=0.01,
            ),
        ]

    result = run_spend_log_smoke(
        business_base_url="http://localhost:4000",
        admin_base_url="http://localhost:4001",
        master_key="local-master-key",
        request_marker="smoke-123",
        fetch=fetch,
        poll_spend_logs=poll_logs,
        sleep=lambda _seconds: None,
        poll_attempts=2,
    )

    assert result.passed is True
    assert result.chat_spend == 0.000003
    assert result.image_spend == 0.01
    assert any(request[1].endswith("/key/delete") for request in requests)
    key_generate_request = next(request for request in requests if request[1].endswith("/key/generate"))
    assert "shared_key" not in key_generate_request[3]["metadata"]  # type: ignore[index]


def test_spend_log_smoke_fails_when_image_spend_is_zero() -> None:
    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        if url.endswith("/key/generate"):
            assert headers["x-aimanager-role"] == "proxy_admin"
            return _json_response({"key": "sk-virtual-smoke"})
        if url.endswith("/v1/chat/completions") or url.endswith("/v1/images/generations"):
            return _json_response({"ok": True})
        if url.endswith("/key/delete"):
            assert headers["x-aimanager-role"] == "proxy_admin"
            assert headers["x-aimanager-actor"] == "aimanager-ci"
            assert headers["x-aimanager-reason"] == "AiManager smoke cleanup"
            return _json_response({"deleted": True})
        raise AssertionError(f"unexpected URL {url}")

    def poll_logs(marker: str) -> list[SpendLogRow]:
        assert marker == "smoke-123"
        return [
            SpendLogRow(
                request_id="chat-req-smoke",
                call_type="completion",
                model="openai/gemini-2.5-flash",
                spend=0.000003,
            ),
            SpendLogRow(
                request_id="image-req-smoke",
                call_type="image_generation",
                model="openai/ycapi-image-1",
                spend=0.0,
            ),
        ]

    result = run_spend_log_smoke(
        business_base_url="http://localhost:4000",
        admin_base_url="http://localhost:4001",
        master_key="local-master-key",
        request_marker="smoke-123",
        fetch=fetch,
        poll_spend_logs=poll_logs,
        sleep=lambda _seconds: None,
        poll_attempts=1,
    )

    assert result.passed is False
    assert result.chat_spend == 0.000003
    assert result.image_spend == 0.0
    assert "image spend log missing or zero" in result.detail


def test_delete_virtual_key_sends_disposition_headers_for_lifecycle_audit() -> None:
    observed_headers: dict[str, str] = {}

    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        nonlocal observed_headers
        assert method == "POST"
        assert url == "http://localhost:4001/key/delete"
        observed_headers = headers
        assert body is not None
        assert json.loads(body.decode("utf-8")) == {"keys": ["sk-cleanup"]}
        return _json_response({"deleted_keys": ["sk-cleanup"]})

    _delete_virtual_key(
        fetch,
        "http://localhost:4001",
        "local-master-key",
        "sk-cleanup",
        "cleanup-123",
    )

    assert observed_headers["x-request-id"] == "delete-key-cleanup-123"
    assert observed_headers["x-aimanager-role"] == "proxy_admin"
    assert observed_headers["x-aimanager-actor"] == "aimanager-ci"
    assert observed_headers["x-aimanager-reason"] == "AiManager smoke cleanup"


def test_parse_spend_log_rows_accepts_psql_json_output() -> None:
    rows = parse_spend_log_rows(
        json.dumps(
            [
                {
                    "request_id": "chat-req",
                    "call_type": "completion",
                    "model": "gemini-2.5-flash",
                    "spend": "0.000003",
                },
                {
                    "request_id": "image-req",
                    "call_type": "image_generation",
                    "model": "ycapi-image-1",
                    "spend": 0.01,
                },
            ]
        )
    )

    assert rows == [
        SpendLogRow(
            request_id="chat-req",
            call_type="completion",
            model="gemini-2.5-flash",
            spend=0.000003,
        ),
        SpendLogRow(
            request_id="image-req",
            call_type="image_generation",
            model="ycapi-image-1",
            spend=0.01,
        ),
    ]


def _json_response(payload: dict[str, object], status_code: int = 200) -> HttpResponse:
    return HttpResponse(
        status_code=status_code,
        headers={"content-type": "application/json"},
        body=json.dumps(payload).encode("utf-8"),
    )

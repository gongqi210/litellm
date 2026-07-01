from __future__ import annotations

import json

from aimanager.asgi import _business_work_context_error
from aimanager.scripts.smoke_blocked_routes import HttpResponse
from aimanager.scripts.smoke_work_context_enforcement import (
    WORK_CONTEXT_POLICY_CODE,
    WorkContextSmokeCase,
    _request_metadata,
    run_work_context_enforcement_smoke,
)
from aimanager.work_context import validate_work_context


def test_work_context_enforcement_smoke_passes_when_chat_and_image_fail_closed() -> None:
    calls: list[tuple[str, str, dict[str, str], dict[str, object]]] = []

    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        assert body is not None
        payload = json.loads(body.decode("utf-8"))
        calls.append((method, url, headers, payload))
        return HttpResponse(
            status_code=400,
            headers={
                "Content-Type": "application/json",
                "x-aimanager-policy-code": WORK_CONTEXT_POLICY_CODE,
            },
            body=json.dumps(
                {
                    "error": {
                        "code": WORK_CONTEXT_POLICY_CODE,
                        "message": "AiManager work context rejects request: missing metadata work context object",
                    }
                }
            ).encode("utf-8"),
        )

    results = run_work_context_enforcement_smoke(
        base_url="http://localhost:4000",
        employee_key="employee-virtual-key",
        request_marker="wc-smoke-123",
        fetch=fetch,
    )

    assert results
    assert all(result.passed for result in results)
    assert {(call[0], call[1]) for call in calls} == {
        ("POST", "http://localhost:4000/v1/chat/completions"),
        ("POST", "http://localhost:4000/v1/images/generations"),
    }
    assert all(call[2]["Authorization"] == "Bearer employee-virtual-key" for call in calls)
    assert all(call[2]["x-request-id"].startswith("work-context-wc-smoke-123-") for call in calls)
    assert any(call[3]["model"] == "gemini-2.5-flash" for call in calls)
    assert any(call[3]["model"] == "ycapi-image-1" for call in calls)
    assert all("metadata" not in call[3] for call in calls)


def test_valid_context_roundtrip_metadata_matches_real_work_context_validator() -> None:
    metadata = _request_metadata("wc-smoke-123", image_count=0)

    validation = validate_work_context(metadata, mode="preflight")
    payload = {
        "model": "gemini-2.5-flash",
        "messages": [{"role": "user", "content": "safe smoke"}],
        "user": "employee-smoke-001",
        "metadata": metadata,
    }

    assert validation.status == "PASS"
    assert _business_work_context_error(payload) == ""


def test_work_context_enforcement_smoke_can_run_valid_context_roundtrip_after_fail_closed_checks() -> None:
    calls: list[tuple[str, str, dict[str, str], dict[str, object]]] = []

    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        assert body is not None
        payload = json.loads(body.decode("utf-8"))
        calls.append((method, url, headers, payload))
        if "valid" not in headers["x-request-id"]:
            return HttpResponse(
                status_code=400,
                headers={
                    "Content-Type": "application/json",
                    "x-aimanager-policy-code": WORK_CONTEXT_POLICY_CODE,
                },
                body=json.dumps({"error": {"code": WORK_CONTEXT_POLICY_CODE}}).encode("utf-8"),
            )
        if url.endswith("/v1/chat/completions"):
            assert payload["user"] == "employee-smoke-001"
            assert payload["metadata"]["scenario_l1"] == "engineering"  # type: ignore[index]
            assert payload["metadata"]["scenario_l2"] == "code_assist"  # type: ignore[index]
            assert payload["metadata"]["end_user_principal"] == "employee-smoke-001"  # type: ignore[index]
            assert payload["metadata"]["work_item_id"] == "wc-smoke-123"  # type: ignore[index]
            assert payload["metadata"]["approval_required"] is False  # type: ignore[index]
            return HttpResponse(
                status_code=200,
                headers={"Content-Type": "application/json", "x-litellm-call-id": "chat-call-id"},
                body=json.dumps(
                    {
                        "id": "chatcmpl-valid-context",
                        "choices": [{"message": {"content": "do not store this assistant text"}}],
                        "usage": {"prompt_tokens": 6, "completion_tokens": 2, "total_tokens": 8},
                    }
                ).encode("utf-8"),
            )
        if url.endswith("/v1/images/generations"):
            assert payload["user"] == "employee-smoke-001"
            assert payload["metadata"]["image_count"] == 1  # type: ignore[index]
            return HttpResponse(
                status_code=200,
                headers={"Content-Type": "application/json", "x-litellm-call-id": "image-call-id"},
                body=json.dumps({"created": 1, "data": [{"b64_json": "raw-image-content-not-for-output"}]}).encode(
                    "utf-8"
                ),
            )
        raise AssertionError(url)

    results = run_work_context_enforcement_smoke(
        base_url="http://localhost:4000",
        employee_key="employee-virtual-key",
        request_marker="wc-smoke-123",
        run_valid_context_roundtrip=True,
        fetch=fetch,
    )

    assert len(results) == 4
    assert all(result.passed for result in results)
    valid_results = [result for result in results if result.case.check_type == "valid_context_roundtrip"]
    assert [result.case.path for result in valid_results] == ["/v1/chat/completions", "/v1/images/generations"]
    assert valid_results[0].request_id == "chat-call-id"
    assert valid_results[0].usage_present is True
    assert valid_results[0].work_context_present is True
    assert valid_results[1].request_id == "image-call-id"
    assert valid_results[1].image_result_count == 1
    assert valid_results[1].work_context_present is True
    serialized = json.dumps([result.__dict__ for result in results], default=str, ensure_ascii=False)
    assert "employee-virtual-key" not in serialized
    assert "do not store this assistant text" not in serialized
    assert "raw-image-content-not-for-output" not in serialized
    assert [call[2]["x-request-id"] for call in calls] == [
        "work-context-wc-smoke-123-v1-chat-completions",
        "work-context-wc-smoke-123-v1-images-generations",
        "work-context-wc-smoke-123-valid-v1-chat-completions",
        "work-context-wc-smoke-123-valid-v1-images-generations",
    ]


def test_work_context_enforcement_smoke_skips_valid_roundtrip_when_fail_closed_checks_fail() -> None:
    calls: list[tuple[str, str]] = []

    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:  # noqa: ARG001
        calls.append((method, url))
        return HttpResponse(
            status_code=200,
            headers={"Content-Type": "application/json"},
            body=json.dumps({"ok": True}).encode("utf-8"),
        )

    results = run_work_context_enforcement_smoke(
        base_url="http://localhost:4000",
        employee_key="employee-virtual-key",
        request_marker="wc-smoke-123",
        run_valid_context_roundtrip=True,
        fetch=fetch,
    )

    assert len(results) == 2
    assert all(result.passed is False for result in results)
    assert calls == [
        ("POST", "http://localhost:4000/v1/chat/completions"),
        ("POST", "http://localhost:4000/v1/images/generations"),
    ]


def test_work_context_enforcement_smoke_fails_if_prompt_is_echoed() -> None:
    prompt_text = "DO_NOT_ECHO_WORK_CONTEXT_SMOKE_wc-smoke-123"

    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        return HttpResponse(
            status_code=400,
            headers={"x-aimanager-policy-code": WORK_CONTEXT_POLICY_CODE},
            body=json.dumps(
                {
                    "error": {
                        "code": WORK_CONTEXT_POLICY_CODE,
                        "message": f"bad echo {prompt_text}",
                    }
                }
            ).encode("utf-8"),
        )

    results = run_work_context_enforcement_smoke(
        base_url="http://localhost:4000",
        employee_key="employee-virtual-key",
        request_marker="wc-smoke-123",
        cases=(WorkContextSmokeCase("POST", "/v1/chat/completions", "gemini-2.5-flash", prompt_text),),
        fetch=fetch,
    )

    assert len(results) == 1
    assert results[0].passed is False
    assert "response echoed smoke prompt" in results[0].detail


def test_work_context_enforcement_smoke_fails_on_wrong_policy_code_or_status() -> None:
    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        return HttpResponse(
            status_code=403,
            headers={"x-aimanager-policy-code": "aimanager_route_not_allowed"},
            body=json.dumps({"error": {"code": "aimanager_route_not_allowed"}}).encode("utf-8"),
        )

    results = run_work_context_enforcement_smoke(
        base_url="http://localhost:4000",
        employee_key="employee-virtual-key",
        request_marker="wc-smoke-123",
        fetch=fetch,
    )

    assert results
    assert all(result.passed is False for result in results)
    assert all("expected 400" in result.detail for result in results)


def test_work_context_enforcement_smoke_fails_on_wrong_policy_code_with_400_status() -> None:
    cases = (
        WorkContextSmokeCase("POST", "/v1/chat/completions", "gemini-2.5-flash", "header-mismatch"),
        WorkContextSmokeCase("POST", "/v1/images/generations", "ycapi-image-1", "body-mismatch"),
    )
    responses = iter(
        (
            HttpResponse(
                status_code=400,
                headers={"x-aimanager-policy-code": "aimanager_request_body_invalid"},
                body=json.dumps({"error": {"code": WORK_CONTEXT_POLICY_CODE}}).encode("utf-8"),
            ),
            HttpResponse(
                status_code=400,
                headers={"x-aimanager-policy-code": WORK_CONTEXT_POLICY_CODE},
                body=json.dumps({"error": {"code": "aimanager_request_body_invalid"}}).encode("utf-8"),
            ),
        )
    )

    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        return next(responses)

    results = run_work_context_enforcement_smoke(
        base_url="http://localhost:4000",
        employee_key="employee-virtual-key",
        request_marker="wc-smoke-123",
        cases=cases,
        fetch=fetch,
    )

    assert len(results) == 2
    assert results[0].passed is False
    assert f"expected policy header {WORK_CONTEXT_POLICY_CODE}" in results[0].detail
    assert results[1].passed is False
    assert f"expected body error.code {WORK_CONTEXT_POLICY_CODE}" in results[1].detail

from __future__ import annotations

import json

from aimanager.scripts.smoke_blocked_routes import HttpResponse
from aimanager.scripts.smoke_work_context_enforcement import (
    WORK_CONTEXT_POLICY_CODE,
    WorkContextSmokeCase,
    run_work_context_enforcement_smoke,
)


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

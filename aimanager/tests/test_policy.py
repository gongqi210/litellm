import asyncio
import json

from aimanager.asgi import YcapiOnlyAllowlistMiddleware
from aimanager.policy import build_policy_error_body, evaluate_route


def test_business_routes_are_allowed() -> None:
    assert evaluate_route("GET", "/v1/models").allowed is True
    assert evaluate_route("POST", "/v1/chat/completions").allowed is True
    assert evaluate_route("POST", "/v1/images/generations").allowed is True


def test_method_mismatch_is_blocked() -> None:
    decision = evaluate_route("GET", "/v1/chat/completions")

    assert decision.allowed is False
    assert decision.code == "aimanager_route_not_allowed"
    assert "not allowed" in decision.message


def test_uncommitted_business_routes_are_default_denied() -> None:
    blocked_routes = [
        ("POST", "/v1/embeddings"),
        ("POST", "/v1/completions"),
        ("POST", "/chat/completions"),
        ("POST", "/images/generations"),
        ("POST", "/v1/models"),
        ("GET", "/anything/random"),
    ]

    for method, path in blocked_routes:
        decision = evaluate_route(method, path)
        assert decision.allowed is False, f"{method} {path}"
        assert decision.code == "aimanager_route_not_allowed", f"{method} {path}"


def test_google_native_routes_are_blocked() -> None:
    generate = evaluate_route("POST", "/v1beta/models/gemini-2.5-flash:generateContent")
    stream = evaluate_route("POST", "/models/gemini-2.5-flash:streamGenerateContent")

    assert generate.allowed is False
    assert generate.code == "aimanager_google_native_blocked"
    assert stream.allowed is False
    assert stream.code == "aimanager_google_native_blocked"


def test_provider_passthrough_routes_are_blocked() -> None:
    blocked_paths = [
        "/anthropic/messages",
        "/gemini/v1beta/models",
        "/bedrock/model",
        "/openai/deployments/test/chat/completions",
        "/openai_passthrough/v1/chat/completions",
        "/cohere/chat",
        "/vllm/v1/chat/completions",
        "/mistral/v1/chat/completions",
        "/azure/openai/deployments/test",
        "/azure_ai/models/test",
        "/watsonx/ml/v1/text/generation",
        "/cursor/v1/chat/completions",
        "/vertex_ai/discovery/test",
        "/vertex-ai/v1/projects/test",
    ]

    for path in blocked_paths:
        decision = evaluate_route("POST", path)
        assert decision.allowed is False, path
        assert decision.code == "aimanager_passthrough_blocked", path


def test_dynamic_passthrough_config_update_and_model_write_are_blocked() -> None:
    cases = [
        ("GET", "/pass-through-endpoints"),
        ("POST", "/pass-through-endpoints"),
        ("POST", "/config/update"),
        ("POST", "/model/new"),
        ("PATCH", "/model/update"),
        ("DELETE", "/model/gemini-2.5-flash"),
    ]

    for method, path in cases:
        decision = evaluate_route(method, path)
        assert decision.allowed is False, f"{method} {path}"
        assert decision.code == "aimanager_config_immutable", f"{method} {path}"


def test_health_routes_are_allowed() -> None:
    assert evaluate_route("GET", "/health").allowed is True
    assert evaluate_route("GET", "/health/liveliness").allowed is True
    assert evaluate_route("GET", "/health/readiness").allowed is True


def test_policy_error_body_is_openai_compatible_and_has_request_id() -> None:
    decision = evaluate_route("POST", "/config/update")

    body = build_policy_error_body(decision, request_id="req-test-123")

    assert body == {
        "error": {
            "message": "AiManager policy blocks runtime configuration and model writes",
            "type": "permission_error",
            "param": None,
            "code": "aimanager_config_immutable",
        },
        "request_id": "req-test-123",
    }


def test_allowlist_middleware_forwards_allowed_requests() -> None:
    calls: list[dict[str, object]] = []

    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        calls.append(scope)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = YcapiOnlyAllowlistMiddleware(downstream)

    messages = asyncio.run(_call_asgi(app, "GET", "/v1/models"))

    assert len(calls) == 1
    assert messages[0]["status"] == 204


def test_allowlist_middleware_blocks_before_downstream() -> None:
    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        raise AssertionError("blocked requests must not reach downstream LiteLLM")

    app = YcapiOnlyAllowlistMiddleware(downstream)

    messages = asyncio.run(
        _call_asgi(
            app,
            "POST",
            "/config/update",
            headers=[(b"x-request-id", b"req-from-client")],
        )
    )

    assert messages[0]["status"] == 403
    response_headers = dict(messages[0]["headers"])
    assert response_headers[b"content-type"] == b"application/json"
    assert response_headers[b"x-litellm-call-id"] == b"req-from-client"

    body = json.loads(messages[1]["body"])
    assert body["request_id"] == "req-from-client"
    assert body["error"]["code"] == "aimanager_config_immutable"


def test_allowlist_middleware_ignores_non_http_scopes() -> None:
    calls: list[dict[str, object]] = []

    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        calls.append(scope)

    app = YcapiOnlyAllowlistMiddleware(downstream)

    asyncio.run(app({"type": "lifespan"}, _empty_receive, _collecting_send([])))

    assert calls == [{"type": "lifespan"}]


async def _call_asgi(
    app: YcapiOnlyAllowlistMiddleware,
    method: str,
    path: str,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = []
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers or [],
    }
    await app(scope, _empty_receive, _collecting_send(messages))
    return messages


async def _empty_receive() -> dict[str, object]:
    return {"type": "http.request", "body": b"", "more_body": False}


def _collecting_send(messages: list[dict[str, object]]):
    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    return send

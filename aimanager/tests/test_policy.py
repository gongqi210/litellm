import asyncio
import json

from aimanager.asgi import YcapiOnlyAllowlistMiddleware
from aimanager.governance import SHARED_KEY_ENFORCED_PARAMS
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


def test_business_surface_blocks_litellm_management_routes() -> None:
    blocked_routes = [
        ("GET", "/ui"),
        ("GET", "/ui/"),
        ("POST", "/login"),
        ("POST", "/key/generate"),
        ("GET", "/key/info"),
        ("POST", "/team/new"),
        ("POST", "/user/new"),
        ("GET", "/spend/logs"),
        ("GET", "/global/spend"),
    ]

    for method, path in blocked_routes:
        decision = evaluate_route(method, path, surface="business")
        assert decision.allowed is False, f"{method} {path}"
        assert decision.code == "aimanager_route_not_allowed", f"{method} {path}"


def test_management_surface_allows_litellm_management_routes() -> None:
    allowed_routes = [
        ("GET", "/ui"),
        ("GET", "/ui/"),
        ("GET", "/ui/assets/logo.png"),
        ("POST", "/login"),
        ("POST", "/v2/login"),
        ("POST", "/key/generate"),
        ("GET", "/key/info"),
        ("POST", "/key/block"),
        ("POST", "/team/new"),
        ("GET", "/team/list"),
        ("POST", "/user/new"),
        ("GET", "/user/list"),
        ("GET", "/spend/logs"),
        ("GET", "/global/spend"),
        ("GET", "/global/spend/report"),
        ("GET", "/global/activity"),
        ("POST", "/budget/new"),
        ("GET", "/model/info"),
        ("GET", "/config/yaml"),
    ]

    for method, path in allowed_routes:
        decision = evaluate_route(method, path, surface="management")
        assert decision.allowed is True, f"{method} {path}"


def test_management_surface_still_blocks_provider_config_and_model_write_routes() -> None:
    blocked_routes = [
        ("POST", "/anthropic/messages", "aimanager_passthrough_blocked"),
        ("POST", "/v1beta/models/gemini-2.5-flash:generateContent", "aimanager_google_native_blocked"),
        ("POST", "/config/update", "aimanager_config_immutable"),
        ("POST", "/pass-through-endpoints", "aimanager_config_immutable"),
        ("POST", "/model/new", "aimanager_config_immutable"),
        ("PATCH", "/model/update", "aimanager_config_immutable"),
        ("DELETE", "/model/gemini-2.5-flash", "aimanager_config_immutable"),
    ]

    for method, path, code in blocked_routes:
        decision = evaluate_route(method, path, surface="management")
        assert decision.allowed is False, f"{method} {path}"
        assert decision.code == code, f"{method} {path}"


def test_high_risk_runtime_config_routes_are_immutable_on_all_surfaces() -> None:
    blocked_routes = [
        ("POST", "/config/field/update"),
        ("DELETE", "/config/field/delete"),
        ("DELETE", "/config/callback/delete"),
        ("PATCH", "/config/cost_margin_config"),
        ("PATCH", "/config/cost_discount_config"),
        ("POST", "/config_overrides/hashicorp_vault"),
        ("POST", "/config_overrides/hashicorp_vault/test_connection"),
        ("POST", "/cache/settings"),
        ("POST", "/reload/model_cost_map"),
        ("POST", "/reload/anthropic_beta_headers"),
    ]

    for surface in ["business", "management"]:
        for method, path in blocked_routes:
            decision = evaluate_route(method, path, surface=surface)
            assert decision.allowed is False, f"{surface} {method} {path}"
            assert decision.code == "aimanager_config_immutable", f"{surface} {method} {path}"


def test_management_surface_does_not_unlock_uncommitted_inference_routes() -> None:
    blocked_routes = [
        ("POST", "/v1/embeddings"),
        ("POST", "/v1/completions"),
        ("POST", "/v1/responses"),
        ("POST", "/responses"),
        ("POST", "/v1/messages"),
    ]

    for method, path in blocked_routes:
        decision = evaluate_route(method, path, surface="management")
        assert decision.allowed is False, f"{method} {path}"
        assert decision.code == "aimanager_route_not_allowed", f"{method} {path}"


def test_management_surface_does_not_unlock_unknown_sso_or_global_routes() -> None:
    blocked_routes = [
        ("POST", "/sso/provider/update"),
        ("POST", "/global/config/update"),
        ("DELETE", "/global/internal-state"),
    ]

    for method, path in blocked_routes:
        decision = evaluate_route(method, path, surface="management")
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


def test_management_surface_middleware_forwards_management_requests() -> None:
    calls: list[dict[str, object]] = []

    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        calls.append(scope)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = YcapiOnlyAllowlistMiddleware(downstream, surface="management")

    messages = asyncio.run(_call_asgi(app, "GET", "/key/list"))

    assert len(calls) == 1
    assert messages[0]["status"] == 204


def test_management_surface_rejects_key_generate_without_governance_metadata() -> None:
    audit_events: list[dict[str, object]] = []

    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        raise AssertionError("invalid key creation must not reach downstream LiteLLM")

    app = YcapiOnlyAllowlistMiddleware(
        downstream,
        audit_sink=audit_events.append,
        surface="management",
    )

    messages = asyncio.run(
        _call_asgi(
            app,
            "POST",
            "/key/generate",
            headers=[(b"x-request-id", b"req-key-governance")],
            body=json.dumps(
                {
                    "user_id": "u_001",
                    "team_id": "team_market",
                    "models": ["gemini-2.5-flash"],
                    "max_budget": 100,
                    "rpm_limit": 60,
                    "tpm_limit": 120000,
                    "duration": "30d",
                }
            ).encode("utf-8"),
        )
    )

    assert messages[0]["status"] == 400
    body = json.loads(messages[1]["body"])
    assert body["request_id"] == "req-key-governance"
    assert body["error"]["code"] == "aimanager_key_governance_invalid"
    assert "metadata" in body["error"]["message"]
    assert len(audit_events) == 1
    assert audit_events[0]["event_type"] == "policy_blocked"
    assert audit_events[0]["reason"] == "aimanager_key_governance_invalid"


def test_management_surface_normalizes_key_generate_before_forwarding() -> None:
    forwarded_bodies: list[dict[str, object]] = []

    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        message = await receive()
        forwarded_bodies.append(json.loads(message["body"]))
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = YcapiOnlyAllowlistMiddleware(downstream, surface="management")

    messages = asyncio.run(
        _call_asgi(
            app,
            "POST",
            "/key/generate",
            body=json.dumps(_valid_key_generate_payload()).encode("utf-8"),
        )
    )

    assert messages[0]["status"] == 204
    assert forwarded_bodies[0]["metadata"]["cost_center_id"] == "cc-market"
    assert "enforced_params" not in forwarded_bodies[0]["metadata"]


def test_management_surface_adds_enforced_params_for_shared_key_generate() -> None:
    forwarded_bodies: list[dict[str, object]] = []
    payload = _valid_key_generate_payload()
    payload["metadata"]["shared_key"] = True

    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        message = await receive()
        forwarded_bodies.append(json.loads(message["body"]))
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = YcapiOnlyAllowlistMiddleware(downstream, surface="management")

    messages = asyncio.run(
        _call_asgi(
            app,
            "POST",
            "/key/generate",
            body=json.dumps(payload).encode("utf-8"),
        )
    )

    assert messages[0]["status"] == 204
    assert forwarded_bodies[0]["metadata"]["enforced_params"] == SHARED_KEY_ENFORCED_PARAMS


def test_management_surface_rejects_malformed_key_generate_json() -> None:
    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        raise AssertionError("malformed key creation must not reach downstream LiteLLM")

    app = YcapiOnlyAllowlistMiddleware(downstream, surface="management")

    messages = asyncio.run(
        _call_asgi(
            app,
            "POST",
            "/key/generate",
            headers=[(b"x-request-id", b"req-bad-json")],
            body=b"{not-json",
        )
    )

    assert messages[0]["status"] == 400
    body = json.loads(messages[1]["body"])
    assert body["request_id"] == "req-bad-json"
    assert body["error"]["code"] == "aimanager_key_governance_invalid"
    assert "JSON object body" in body["error"]["message"]


def test_management_surface_replays_chunked_key_generate_with_fixed_headers() -> None:
    forwarded_bodies: list[dict[str, object]] = []
    forwarded_headers: list[dict[bytes, bytes]] = []
    payload = _valid_key_generate_payload()
    payload["metadata"]["shared_key"] = True
    raw_body = json.dumps(payload).encode("utf-8")

    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        message = await receive()
        forwarded_bodies.append(json.loads(message["body"]))
        forwarded_headers.append(dict(scope["headers"]))
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = YcapiOnlyAllowlistMiddleware(downstream, surface="management")

    messages = asyncio.run(
        _call_asgi_with_receive(
            app,
            "POST",
            "/key/generate",
            _receive_chunks([raw_body[:17], raw_body[17:]]),
            headers=[(b"content-length", b"1"), (b"transfer-encoding", b"chunked")],
        )
    )

    assert messages[0]["status"] == 204
    assert forwarded_bodies[0]["metadata"]["enforced_params"] == SHARED_KEY_ENFORCED_PARAMS
    normalized_length = len(json.dumps(forwarded_bodies[0], separators=(",", ":")).encode("utf-8"))
    assert forwarded_headers[0][b"content-length"] == str(normalized_length).encode("ascii")
    assert b"transfer-encoding" not in forwarded_headers[0]
    assert forwarded_headers[0][b"content-type"] == b"application/json"


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


def test_allowlist_middleware_emits_audit_event_for_blocked_requests() -> None:
    audit_events: list[dict[str, object]] = []

    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        raise AssertionError("blocked requests must not reach downstream LiteLLM")

    app = YcapiOnlyAllowlistMiddleware(downstream, audit_sink=audit_events.append)

    asyncio.run(
        _call_asgi(
            app,
            "POST",
            "/anthropic/messages",
            headers=[
                (b"x-request-id", b"req-audit-1"),
                (b"authorization", b"Bearer must-not-be-logged"),
                (b"x-aimanager-actor", b"employee-123"),
                (b"x-aimanager-key-alias", b"market-shared-key"),
                (b"x-aimanager-team-id", b"team_market"),
                (b"x-aimanager-department-id", b"dept_market"),
                (b"x-aimanager-project-id", b"proj_launch"),
                (b"x-aimanager-cost-center-id", b"cc_growth"),
            ],
        )
    )

    assert len(audit_events) == 1
    event = audit_events[0]
    assert event["event_type"] == "passthrough_blocked"
    assert event["actor"] == "employee-123"
    assert event["subject_key_alias"] == "market-shared-key"
    assert event["team_id"] == "team_market"
    assert event["department_id"] == "dept_market"
    assert event["project_id"] == "proj_launch"
    assert event["cost_center_id"] == "cc_growth"
    assert event["request_id"] == "req-audit-1"
    assert event["reason"] == "aimanager_passthrough_blocked"
    assert event["metadata"]["method"] == "POST"
    assert event["metadata"]["path"] == "/anthropic/messages"
    assert "authorization" not in json.dumps(event).lower()
    assert "must-not-be-logged" not in json.dumps(event)


def test_allowlist_middleware_emits_policy_blocked_audit_for_config_updates() -> None:
    audit_events: list[dict[str, object]] = []

    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        raise AssertionError("blocked requests must not reach downstream LiteLLM")

    app = YcapiOnlyAllowlistMiddleware(downstream, audit_sink=audit_events.append)

    asyncio.run(_call_asgi(app, "POST", "/config/update"))

    assert len(audit_events) == 1
    event = audit_events[0]
    assert event["event_type"] == "policy_blocked"
    assert event["reason"] == "aimanager_config_immutable"
    assert event["actor"] == "aimanager-policy"
    assert event["subject_key_alias"] == "unassigned"


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
    body: bytes = b"",
) -> list[dict[str, object]]:
    return await _call_asgi_with_receive(app, method, path, _receive_once(body), headers=headers)


async def _call_asgi_with_receive(
    app: YcapiOnlyAllowlistMiddleware,
    method: str,
    path: str,
    receive,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = []
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers or [],
    }
    await app(scope, receive, _collecting_send(messages))
    return messages


async def _empty_receive() -> dict[str, object]:
    return {"type": "http.request", "body": b"", "more_body": False}


def _receive_once(body: bytes):
    sent = False

    async def receive() -> dict[str, object]:
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


def _receive_chunks(chunks: list[bytes]):
    index = 0

    async def receive() -> dict[str, object]:
        nonlocal index
        if index >= len(chunks):
            return {"type": "http.request", "body": b"", "more_body": False}
        chunk = chunks[index]
        index += 1
        return {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(chunks),
        }

    return receive


def _collecting_send(messages: list[dict[str, object]]):
    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    return send


def _valid_key_generate_payload() -> dict[str, object]:
    return {
        "user_id": "u_001",
        "team_id": "team_market",
        "models": ["gemini-2.5-flash"],
        "max_budget": 100.0,
        "rpm_limit": 60,
        "tpm_limit": 120000,
        "duration": "30d",
        "metadata": {
            "owner": "alice",
            "department_id": "market",
            "project_id": "campaign-2026-q3",
            "cost_center_id": "cc-market",
            "scenario_l1": "marketing",
            "scenario_l2": "copywriting",
            "approver": "cfo",
            "internal_or_external": "internal",
        },
    }

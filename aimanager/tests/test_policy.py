import asyncio
import json

from aimanager.asgi import YcapiOnlyAllowlistMiddleware
from aimanager.governance import SHARED_KEY_ENFORCED_PARAMS
from aimanager.policy import build_policy_error_body, evaluate_route
from aimanager.runtime_metrics import AiManagerMetrics


def test_business_routes_are_allowed() -> None:
    assert evaluate_route("GET", "/v1/models").allowed is True
    assert evaluate_route("POST", "/v1/chat/completions").allowed is True
    assert evaluate_route("POST", "/v1/images/generations").allowed is True


def test_business_video_lifecycle_routes_are_allowed() -> None:
    assert evaluate_route("POST", "/v1/videos").allowed is True
    assert evaluate_route("GET", "/v1/videos/video_abc123").allowed is True
    assert evaluate_route("GET", "/v1/videos/video_abc123/content").allowed is True


def test_business_video_extras_and_writes_are_blocked() -> None:
    assert evaluate_route("GET", "/v1/videos").allowed is False
    assert evaluate_route("GET", "/v1/videos/characters").allowed is False
    assert evaluate_route("GET", "/v1/videos/edits").allowed is False
    assert evaluate_route("GET", "/v1/videos/extensions").allowed is False
    assert evaluate_route("GET", "/v1/videos/video_abc123/extra").allowed is False
    assert evaluate_route("POST", "/v1/videos/video_abc123/remix").allowed is False
    assert evaluate_route("DELETE", "/v1/videos/video_abc123").allowed is False


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
        ("GET", "/"),
        ("HEAD", "/"),
        ("POST", "/login"),
        ("POST", "/key/generate"),
        ("GET", "/key/info"),
        ("POST", "/v2/key/info"),
        ("POST", "/team/new"),
        ("GET", "/v2/team/list"),
        ("POST", "/user/new"),
        ("GET", "/spend/logs"),
        ("GET", "/global/spend"),
        ("GET", "/litellm-asset-prefix/_next/static/chunks/app.js"),
        ("GET", "/__next._tree.txt"),
        ("GET", "/get/ui_settings"),
        ("GET", "/get/ui_theme_settings"),
        ("GET", "/sso/get/ui_settings"),
        ("GET", "/litellm/.well-known/litellm-ui-config"),
        ("GET", "/public/litellm_blog_posts"),
        ("GET", "/health/readiness/details"),
        ("GET", "/v2/user/info"),
        ("GET", "/tag/list"),
        ("GET", "/project/list"),
        ("GET", "/api/plugins"),
    ]

    for method, path in blocked_routes:
        decision = evaluate_route(method, path, surface="business")
        assert decision.allowed is False, f"{method} {path}"
        assert decision.code == "aimanager_route_not_allowed", f"{method} {path}"


def test_management_surface_allows_litellm_management_routes() -> None:
    allowed_routes = [
        ("GET", "/"),
        ("HEAD", "/"),
        ("GET", "/ui"),
        ("GET", "/ui/"),
        ("GET", "/ui/assets/logo.png"),
        ("GET", "/litellm-asset-prefix/_next/static/chunks/app.js"),
        ("GET", "/litellm-asset-prefix/_next/static/css/app.css"),
        ("GET", "/litellm-asset-prefix/_next/static/media/font.woff2"),
        ("GET", "/__next._tree.txt"),
        ("GET", "/litellm/.well-known/litellm-ui-config"),
        ("GET", "/public/litellm_blog_posts"),
        ("GET", "/health/license"),
        ("GET", "/health/readiness/details"),
        ("GET", "/get/ui_settings"),
        ("GET", "/get/ui_theme_settings"),
        ("GET", "/sso/get/ui_settings"),
        ("POST", "/login"),
        ("POST", "/v2/login"),
        ("POST", "/key/generate"),
        ("GET", "/key/info"),
        ("POST", "/v2/key/info"),
        ("POST", "/key/block"),
        ("POST", "/team/new"),
        ("GET", "/team/list"),
        ("GET", "/v2/team/list"),
        ("POST", "/user/new"),
        ("GET", "/user/list"),
        ("GET", "/v2/user/info"),
        ("GET", "/spend/logs"),
        ("GET", "/global/spend"),
        ("GET", "/global/spend/report"),
        ("GET", "/global/activity"),
        ("POST", "/budget/new"),
        ("GET", "/tag/list"),
        ("GET", "/project/list"),
        ("GET", "/v2/guardrails/list"),
        ("GET", "/guardrails/list"),
        ("GET", "/v1/agents"),
        ("GET", "/policies/list"),
        ("GET", "/prompts/list"),
        ("GET", "/api/plugins"),
        ("GET", "/model/info"),
        ("GET", "/config/yaml"),
    ]

    for method, path in allowed_routes:
        decision = evaluate_route(method, path, surface="management")
        assert decision.allowed is True, f"{method} {path}"


def test_management_surface_allows_metrics_but_business_surface_blocks_it() -> None:
    assert evaluate_route("GET", "/metrics", surface="management").allowed is True
    business_decision = evaluate_route("GET", "/metrics", surface="business")

    assert business_decision.allowed is False
    assert business_decision.code == "aimanager_route_not_allowed"


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
        ("POST", "/v1/agents"),
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


def test_allowlist_middleware_wraps_downstream_json_error_with_request_id() -> None:
    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"x-litellm-call-id", b"litellm-downstream-429"),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": json.dumps(
                    {
                        "error": {
                            "message": "ycapi rate limit exceeded",
                            "type": "rate_limit_error",
                            "param": "model",
                            "code": "rate_limit_exceeded",
                        }
                    }
                ).encode("utf-8"),
            }
        )

    app = YcapiOnlyAllowlistMiddleware(downstream)

    messages = asyncio.run(
        _call_asgi(
            app,
            "POST",
            "/v1/chat/completions",
            headers=[(b"x-request-id", b"client-request-1")],
        )
    )

    assert messages[0]["status"] == 429
    response_headers = dict(messages[0]["headers"])
    assert response_headers[b"content-type"] == b"application/json"
    assert response_headers[b"x-litellm-call-id"] == b"litellm-downstream-429"
    body = json.loads(messages[1]["body"])
    assert body == {
        "error": {
            "message": "ycapi rate limit exceeded",
            "type": "rate_limit_error",
            "param": "model",
            "code": "rate_limit_exceeded",
        },
        "request_id": "litellm-downstream-429",
    }


def test_allowlist_middleware_wraps_downstream_text_error_without_secret_leak() -> None:
    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b"upstream failed with Bearer ycapi-token-secret and sk-test-secret",
            }
        )

    app = YcapiOnlyAllowlistMiddleware(downstream)

    messages = asyncio.run(
        _call_asgi(
            app,
            "POST",
            "/v1/chat/completions",
            headers=[(b"x-request-id", b"client-request-503")],
        )
    )

    assert messages[0]["status"] == 503
    response_headers = dict(messages[0]["headers"])
    assert response_headers[b"content-type"] == b"application/json"
    assert response_headers[b"x-litellm-call-id"] == b"client-request-503"
    body = json.loads(messages[1]["body"])
    assert body == {
        "error": {
            "message": "Upstream request failed with HTTP 503",
            "type": "server_error",
            "param": None,
            "code": "upstream_error",
        },
        "request_id": "client-request-503",
    }
    assert "ycapi-token-secret" not in messages[1]["body"].decode("utf-8")
    assert "sk-test-secret" not in messages[1]["body"].decode("utf-8")


def test_allowlist_middleware_falls_back_to_status_error_types() -> None:
    status_to_type = {
        401: "authentication_error",
        403: "permission_error",
        404: "not_found_error",
    }

    for status, expected_type in status_to_type.items():

        async def downstream(scope, receive, send, status=status) -> None:  # type: ignore[no-untyped-def]
            await send({"type": "http.response.start", "status": status, "headers": []})
            await send(
                {
                    "type": "http.response.body",
                    "body": json.dumps({"error": {"message": f"downstream {status}", "param": None}}).encode(
                        "utf-8"
                    ),
                }
            )

        app = YcapiOnlyAllowlistMiddleware(downstream)

        messages = asyncio.run(
            _call_asgi(
                app,
                "POST",
                "/v1/chat/completions",
                headers=[(b"x-request-id", f"client-request-{status}".encode("ascii"))],
            )
        )

        body = json.loads(messages[1]["body"])
        assert messages[0]["status"] == status
        assert body["request_id"] == f"client-request-{status}"
        assert body["error"]["type"] == expected_type
        assert body["error"]["code"] == "upstream_error"


def test_allowlist_middleware_passes_success_streaming_chunks_through() -> None:
    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        await send({"type": "http.response.body", "body": b"data: first\n\n", "more_body": True})
        await send({"type": "http.response.body", "body": b"data: second\n\n", "more_body": False})

    app = YcapiOnlyAllowlistMiddleware(downstream)

    messages = asyncio.run(_call_asgi(app, "POST", "/v1/chat/completions"))

    assert messages == [
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/event-stream")],
        },
        {"type": "http.response.body", "body": b"data: first\n\n", "more_body": True},
        {"type": "http.response.body", "body": b"data: second\n\n", "more_body": False},
    ]


def test_business_chat_stream_requests_force_usage_in_stream_options() -> None:
    captured: dict[str, object] = {}

    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        message = await receive()
        captured["scope"] = scope
        captured["body"] = message["body"]
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = YcapiOnlyAllowlistMiddleware(downstream)
    raw_body = json.dumps(
        {
            "model": "gemini-2.5-flash",
            "stream": True,
            "stream_options": {"include_usage": False},
            "messages": [{"role": "user", "content": "hello"}],
        }
    ).encode("utf-8")

    messages = asyncio.run(
        _call_asgi(
            app,
            "POST",
            "/v1/chat/completions",
            headers=[(b"content-type", b"application/json"), (b"content-length", str(len(raw_body)).encode())],
            body=raw_body,
        )
    )

    forwarded = json.loads(captured["body"])
    assert messages[0]["status"] == 204
    assert forwarded["stream_options"]["include_usage"] is True
    forwarded_headers = dict(captured["scope"]["headers"])
    assert forwarded_headers[b"content-length"] == str(len(captured["body"])).encode("ascii")


def test_allowlist_middleware_redacts_json_error_message_secrets() -> None:
    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        await send(
            {
                "type": "http.response.start",
                "status": 500,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": json.dumps(
                    {
                        "error": {
                            "message": (
                                "upstream failed with Bearer ycapi-token-secret, "
                                "sk-test-secret, and postgresql://dbuser:dbpassword@postgres/aimanager"
                            ),
                            "type": "server_error",
                            "param": None,
                            "code": "upstream_failed",
                        }
                    }
                ).encode("utf-8"),
            }
        )

    app = YcapiOnlyAllowlistMiddleware(downstream)

    messages = asyncio.run(
        _call_asgi(
            app,
            "POST",
            "/v1/chat/completions",
            headers=[(b"x-request-id", b"client-request-redact")],
        )
    )

    body = json.loads(messages[1]["body"])
    message = body["error"]["message"]
    assert body["request_id"] == "client-request-redact"
    assert "ycapi-token-secret" not in message
    assert "sk-test-secret" not in message
    assert "dbpassword" not in message
    assert "Bearer [REDACTED]" in message
    assert "sk-[REDACTED]" in message
    assert "postgresql://[REDACTED]@postgres/aimanager" in message


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


def test_management_rbac_blocks_readonly_roles_from_high_risk_writes() -> None:
    for role, path in [
        ("finance", "/key/generate"),
        ("ceo", "/key/block"),
        ("audit", "/team/new"),
        ("proxy_admin_viewer", "/budget/new"),
    ]:
        audit_events: list[dict[str, object]] = []
        calls: list[dict[str, object]] = []

        async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
            calls.append(scope)
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        app = YcapiOnlyAllowlistMiddleware(
            downstream,
            audit_sink=audit_events.append,
            surface="management",
            rbac_enabled=True,
        )

        messages = asyncio.run(
            _call_asgi(
                app,
                "POST",
                path,
                headers=[
                    (b"x-request-id", f"req-rbac-{role}".encode("ascii")),
                    (b"x-aimanager-actor", role.encode("ascii")),
                    (b"x-aimanager-role", role.encode("ascii")),
                ],
                body=json.dumps(_valid_key_generate_payload()).encode("utf-8"),
            )
        )

        assert calls == []
        assert messages[0]["status"] == 403
        response_headers = dict(messages[0]["headers"])
        assert response_headers[b"x-aimanager-policy-code"] == b"aimanager_rbac_denied"
        body = json.loads(messages[1]["body"])
        assert body["request_id"] == f"req-rbac-{role}"
        assert body["error"]["code"] == "aimanager_rbac_denied"
        assert role in body["error"]["message"]
        assert len(audit_events) == 1
        event = audit_events[0]
        assert event["event_type"] == "policy_blocked"
        assert event["reason"] == "aimanager_rbac_denied"
        assert event["actor"] == role
        assert event["metadata"]["role"] == role
        assert event["metadata"]["path"] == path
        assert event["metadata"]["status_code"] == 403


def test_management_rbac_fails_closed_for_missing_or_unknown_roles() -> None:
    for role in ["", "contractor"]:
        calls: list[dict[str, object]] = []

        async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
            calls.append(scope)
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        headers = [(b"x-request-id", b"req-rbac-unknown")]
        if role:
            headers.append((b"x-aimanager-role", role.encode("ascii")))
        app = YcapiOnlyAllowlistMiddleware(
            downstream,
            surface="management",
            rbac_enabled=True,
        )

        messages = asyncio.run(_call_asgi(app, "POST", "/user/update", headers=headers, body=b"{}"))

        assert calls == []
        assert messages[0]["status"] == 403
        body = json.loads(messages[1]["body"])
        assert body["error"]["code"] == "aimanager_rbac_denied"
        assert (role or "missing") in body["error"]["message"]


def test_management_rbac_header_does_not_unlock_business_surface() -> None:
    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        raise AssertionError("business surface must not forward management routes")

    app = YcapiOnlyAllowlistMiddleware(
        downstream,
        surface="business",
        rbac_enabled=True,
    )

    messages = asyncio.run(
        _call_asgi(
            app,
            "POST",
            "/key/generate",
            headers=[
                (b"x-request-id", b"req-rbac-business"),
                (b"x-aimanager-role", b"proxy_admin"),
            ],
            body=json.dumps(_valid_key_generate_payload()).encode("utf-8"),
        )
    )

    body = json.loads(messages[1]["body"])
    assert messages[0]["status"] == 403
    assert body["error"]["code"] == "aimanager_route_not_allowed"


def test_management_rbac_allows_admin_roles_to_forward_high_risk_writes() -> None:
    forwarded_bodies: list[dict[str, object]] = []

    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        message = await receive()
        forwarded_bodies.append(json.loads(message["body"]))
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = YcapiOnlyAllowlistMiddleware(
        downstream,
        surface="management",
        rbac_enabled=True,
    )

    messages = asyncio.run(
        _call_asgi(
            app,
            "POST",
            "/key/generate",
            headers=[
                (b"x-request-id", b"req-rbac-admin"),
                (b"x-aimanager-role", b"proxy_admin"),
            ],
            body=json.dumps(_valid_key_generate_payload()).encode("utf-8"),
        )
    )

    assert messages[0]["status"] == 204
    assert forwarded_bodies[0]["metadata"]["cost_center_id"] == "cc-market"


def test_management_rbac_allows_readonly_roles_to_fetch_reports() -> None:
    calls: list[tuple[str, str, str]] = []

    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        headers = dict(scope["headers"])
        calls.append(
            (
                scope["method"],
                scope["path"],
                headers[b"x-aimanager-role"].decode("ascii"),
            )
        )
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    app = YcapiOnlyAllowlistMiddleware(
        downstream,
        surface="management",
        rbac_enabled=True,
    )

    for role, path in [
        ("finance", "/spend/logs"),
        ("ceo", "/global/spend"),
        ("audit", "/global/activity"),
    ]:
        messages = asyncio.run(
            _call_asgi(
                app,
                "GET",
                path,
                headers=[(b"x-aimanager-role", role.encode("ascii"))],
            )
        )
        assert messages[0]["status"] == 200

    assert calls == [
        ("GET", "/spend/logs", "finance"),
        ("GET", "/global/spend", "ceo"),
        ("GET", "/global/activity", "audit"),
    ]


def test_database_ready_guard_blocks_allowed_business_routes_before_downstream() -> None:
    audit_events: list[dict[str, object]] = []
    calls: list[dict[str, object]] = []

    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        calls.append(scope)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    app = YcapiOnlyAllowlistMiddleware(
        downstream,
        audit_sink=audit_events.append,
        database_ready_check_enabled=True,
        database_ready_checker=lambda: False,
    )

    messages = asyncio.run(
        _call_asgi(
            app,
            "POST",
            "/v1/chat/completions",
            headers=[(b"x-request-id", b"req-db-down")],
            body=json.dumps(
                {"model": "gemini-2.5-flash", "messages": [{"role": "user", "content": "hello"}]}
            ).encode("utf-8"),
        )
    )

    assert calls == []
    assert messages[0]["status"] == 503
    response_headers = dict(messages[0]["headers"])
    assert response_headers[b"x-aimanager-policy-code"] == b"aimanager_database_unavailable"
    body = json.loads(messages[1]["body"])
    assert body["request_id"] == "req-db-down"
    assert body["error"]["code"] == "aimanager_database_unavailable"
    assert len(audit_events) == 1
    assert audit_events[0]["event_type"] == "policy_blocked"
    assert audit_events[0]["reason"] == "aimanager_database_unavailable"


def test_database_ready_guard_does_not_mask_provider_passthrough_policy() -> None:
    def fail_if_called() -> bool:
        raise AssertionError("database readiness must not run for blocked provider passthrough")

    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        raise AssertionError("blocked provider passthrough must not reach downstream LiteLLM")

    app = YcapiOnlyAllowlistMiddleware(
        downstream,
        database_ready_check_enabled=True,
        database_ready_checker=fail_if_called,
    )

    messages = asyncio.run(_call_asgi(app, "POST", "/anthropic/messages", body=b"{}"))

    assert messages[0]["status"] == 403
    body = json.loads(messages[1]["body"])
    assert body["error"]["code"] == "aimanager_passthrough_blocked"


def test_database_ready_guard_serves_readiness_503_before_downstream() -> None:
    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        raise AssertionError("readiness should fail fast before downstream LiteLLM when DB is down")

    app = YcapiOnlyAllowlistMiddleware(
        downstream,
        database_ready_check_enabled=True,
        database_ready_checker=lambda: False,
    )

    messages = asyncio.run(_call_asgi(app, "GET", "/health/readiness"))

    assert messages[0]["status"] == 503
    body = json.loads(messages[1]["body"])
    assert body["error"]["code"] == "aimanager_database_unavailable"


def test_database_ready_guard_leaves_liveliness_to_downstream() -> None:
    calls: list[str] = []

    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        calls.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"alive"})

    app = YcapiOnlyAllowlistMiddleware(
        downstream,
        database_ready_check_enabled=True,
        database_ready_checker=lambda: False,
    )

    messages = asyncio.run(_call_asgi(app, "GET", "/health/liveliness"))

    assert calls == ["/health/liveliness"]
    assert messages[0]["status"] == 200


def test_database_ready_guard_allows_business_routes_when_database_is_ready() -> None:
    calls: list[dict[str, object]] = []

    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        calls.append(scope)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = YcapiOnlyAllowlistMiddleware(
        downstream,
        database_ready_check_enabled=True,
        database_ready_checker=lambda: True,
    )

    messages = asyncio.run(_call_asgi(app, "GET", "/v1/models"))

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


def test_management_surface_coerces_admin_ui_numeric_strings_before_forwarding() -> None:
    forwarded_bodies: list[dict[str, object]] = []
    payload = _valid_key_generate_payload()
    payload["max_budget"] = "100.5"
    payload["rpm_limit"] = "60"
    payload["tpm_limit"] = "120000"

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
    assert forwarded_bodies[0]["max_budget"] == 100.5
    assert isinstance(forwarded_bodies[0]["max_budget"], float)
    assert forwarded_bodies[0]["rpm_limit"] == 60
    assert isinstance(forwarded_bodies[0]["rpm_limit"], int)
    assert forwarded_bodies[0]["tpm_limit"] == 120000
    assert isinstance(forwarded_bodies[0]["tpm_limit"], int)


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


def test_allowlist_middleware_records_bounded_audit_metrics_for_blocked_requests() -> None:
    metrics = AiManagerMetrics()

    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        raise AssertionError("blocked requests must not reach downstream LiteLLM")

    app = YcapiOnlyAllowlistMiddleware(downstream, metrics_sink=metrics)

    asyncio.run(
        _call_asgi(
            app,
            "POST",
            "/anthropic/messages",
            headers=[
                (b"x-request-id", b"req-metrics-must-not-be-a-label"),
                (b"x-aimanager-key-alias", b"market-key-must-not-be-a-label"),
            ],
        )
    )

    text = metrics.render_prometheus_text()
    assert 'aimanager_audit_events_total{event_type="passthrough_blocked",severity="warning"} 1' in text
    assert 'aimanager_http_responses_total{method="POST",status_class="4xx",status_code="403"} 1' in text
    assert "req-metrics-must-not-be-a-label" not in text
    assert "market-key-must-not-be-a-label" not in text


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


def test_allowlist_middleware_emits_budget_blocked_audit_for_downstream_budget_errors() -> None:
    audit_events: list[dict[str, object]] = []

    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [(b"x-litellm-call-id", b"req-budget-1")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": json.dumps(
                    {
                        "error": {
                            "message": "Budget has been exceeded! Current cost: 0.01, Max budget: 0.005",
                            "type": "budget_exceeded",
                            "param": None,
                            "code": "429",
                        }
                    }
                ).encode("utf-8"),
                }
            )

    async def no_key_disposition(token: str):
        return None

    app = YcapiOnlyAllowlistMiddleware(
        downstream,
        audit_sink=audit_events.append,
        key_disposition_checker=no_key_disposition,
    )

    messages = asyncio.run(
        _call_asgi(
            app,
            "POST",
            "/v1/chat/completions",
            headers=[
                (b"x-request-id", b"req-budget-1"),
                (b"authorization", b"Bearer must-not-be-logged"),
                (b"x-aimanager-actor", b"employee-123"),
                (b"x-aimanager-key-alias", b"market-key"),
                (b"x-aimanager-team-id", b"team_market"),
                (b"x-aimanager-department-id", b"dept_market"),
                (b"x-aimanager-project-id", b"proj_launch"),
                (b"x-aimanager-cost-center-id", b"cc_growth"),
            ],
        )
    )

    assert messages[0]["status"] == 429
    response_body = json.loads(messages[1]["body"])
    assert response_body["request_id"] == "req-budget-1"
    assert response_body["error"]["type"] == "budget_exceeded"
    assert len(audit_events) == 1
    event = audit_events[0]
    assert event["event_type"] == "budget_blocked"
    assert event["severity"] == "high"
    assert event["actor"] == "employee-123"
    assert event["subject_key_alias"] == "market-key"
    assert event["team_id"] == "team_market"
    assert event["department_id"] == "dept_market"
    assert event["project_id"] == "proj_launch"
    assert event["cost_center_id"] == "cc_growth"
    assert event["request_id"] == "req-budget-1"
    assert event["reason"] == "budget_exceeded"
    assert event["metadata"]["method"] == "POST"
    assert event["metadata"]["path"] == "/v1/chat/completions"
    assert event["metadata"]["status_code"] == 429
    assert event["metadata"]["downstream_error_type"] == "budget_exceeded"
    assert "authorization" not in json.dumps(event).lower()
    assert "must-not-be-logged" not in json.dumps(event)


def test_allowlist_middleware_records_downstream_429_and_5xx_status_metrics() -> None:
    metrics = AiManagerMetrics()
    statuses = [200, 429, 503]

    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        status = statuses.pop(0)
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = YcapiOnlyAllowlistMiddleware(downstream, metrics_sink=metrics)

    for request_id in ("req-ok", "req-429", "req-503"):
        asyncio.run(
            _call_asgi(
                app,
                "POST",
                "/v1/chat/completions",
                headers=[(b"x-request-id", request_id.encode("utf-8"))],
            )
        )

    text = metrics.render_prometheus_text()
    assert 'aimanager_http_responses_total{method="POST",status_class="2xx",status_code="200"} 1' in text
    assert 'aimanager_http_responses_total{method="POST",status_class="4xx",status_code="429"} 1' in text
    assert 'aimanager_http_responses_total{method="POST",status_class="5xx",status_code="503"} 1' in text
    assert "req-503" not in text


def test_management_surface_serves_aimanager_metrics_without_downstream_litellm() -> None:
    calls: list[dict[str, object]] = []
    metrics = AiManagerMetrics()
    metrics.record_audit_event({"event_type": "budget_blocked", "severity": "high"})

    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        calls.append(scope)
        raise AssertionError("AiManager should serve /metrics before downstream LiteLLM")

    app = YcapiOnlyAllowlistMiddleware(downstream, surface="management", metrics_sink=metrics)

    messages = asyncio.run(_call_asgi(app, "GET", "/metrics"))

    assert calls == []
    assert messages[0]["status"] == 200
    headers = dict(messages[0]["headers"])
    assert headers[b"content-type"].startswith(b"text/plain")
    body = messages[1]["body"].decode("utf-8")
    assert 'aimanager_audit_events_total{event_type="budget_blocked",severity="high"} 1' in body


def test_management_surface_emits_key_frozen_audit_after_successful_block() -> None:
    audit_events: list[dict[str, object]] = []
    forwarded_bodies: list[dict[str, object]] = []

    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        message = await receive()
        forwarded_bodies.append(json.loads(message["body"]))
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"x-litellm-call-id", b"req-freeze-1")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": json.dumps({"key_alias": "market-key", "blocked": True}).encode("utf-8"),
            }
        )

    app = YcapiOnlyAllowlistMiddleware(
        downstream,
        audit_sink=audit_events.append,
        surface="management",
    )

    messages = asyncio.run(
        _call_asgi(
            app,
            "POST",
            "/key/block",
            headers=[
                (b"x-request-id", b"req-freeze-1"),
                (b"x-aimanager-actor", b"security-admin"),
                (b"x-aimanager-reason", b"suspected token exposure"),
                (b"x-aimanager-key-alias", b"market-key"),
                (b"x-aimanager-team-id", b"team_market"),
                (b"x-aimanager-department-id", b"dept_market"),
                (b"x-aimanager-project-id", b"proj_launch"),
                (b"x-aimanager-cost-center-id", b"cc_growth"),
            ],
            body=json.dumps({"key": "sk-test-virtual"}).encode("utf-8"),
        )
    )

    assert messages[0]["status"] == 200
    assert forwarded_bodies == [{"key": "sk-test-virtual"}]
    assert len(audit_events) == 1
    event = audit_events[0]
    assert event["event_type"] == "key_frozen"
    assert event["severity"] == "high"
    assert event["actor"] == "security-admin"
    assert event["reason"] == "suspected token exposure"
    assert event["subject_key_alias"] == "market-key"
    assert event["team_id"] == "team_market"
    assert event["department_id"] == "dept_market"
    assert event["project_id"] == "proj_launch"
    assert event["cost_center_id"] == "cc_growth"
    assert event["request_id"] == "req-freeze-1"
    assert event["metadata"]["method"] == "POST"
    assert event["metadata"]["path"] == "/key/block"
    assert event["metadata"]["status_code"] == 200
    assert event["metadata"]["operation"] == "freeze"


def test_management_surface_emits_key_revoked_audit_after_successful_delete() -> None:
    audit_events: list[dict[str, object]] = []
    forwarded_bodies: list[dict[str, object]] = []

    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        message = await receive()
        forwarded_bodies.append(json.loads(message["body"]))
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"x-litellm-call-id", b"req-revoke-1")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": json.dumps({"deleted_keys": ["sk-test-virtual"]}).encode("utf-8"),
            }
        )

    app = YcapiOnlyAllowlistMiddleware(
        downstream,
        audit_sink=audit_events.append,
        surface="management",
    )

    messages = asyncio.run(
        _call_asgi(
            app,
            "POST",
            "/key/delete",
            headers=[
                (b"x-request-id", b"req-revoke-1"),
                (b"x-aimanager-actor", b"security-admin"),
                (b"x-aimanager-reason", b"credential rotation completed"),
                (b"x-aimanager-key-alias", b"market-key"),
                (b"x-aimanager-team-id", b"team_market"),
                (b"x-aimanager-department-id", b"dept_market"),
                (b"x-aimanager-project-id", b"proj_launch"),
                (b"x-aimanager-cost-center-id", b"cc_growth"),
            ],
            body=json.dumps({"keys": ["sk-test-virtual"]}).encode("utf-8"),
        )
    )

    assert messages[0]["status"] == 200
    assert forwarded_bodies == [{"keys": ["sk-test-virtual"]}]
    assert len(audit_events) == 1
    event = audit_events[0]
    assert event["event_type"] == "key_revoked"
    assert event["severity"] == "critical"
    assert event["actor"] == "security-admin"
    assert event["reason"] == "credential rotation completed"
    assert event["subject_key_alias"] == "market-key"
    assert event["team_id"] == "team_market"
    assert event["department_id"] == "dept_market"
    assert event["project_id"] == "proj_launch"
    assert event["cost_center_id"] == "cc_growth"
    assert event["request_id"] == "req-revoke-1"
    assert event["metadata"]["method"] == "POST"
    assert event["metadata"]["path"] == "/key/delete"
    assert event["metadata"]["status_code"] == 200
    assert event["metadata"]["operation"] == "revoke"


def test_management_surface_rejects_key_lifecycle_without_disposition_headers() -> None:
    audit_events: list[dict[str, object]] = []

    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        raise AssertionError("key lifecycle without disposition headers must not reach LiteLLM")

    app = YcapiOnlyAllowlistMiddleware(
        downstream,
        audit_sink=audit_events.append,
        surface="management",
    )

    messages = asyncio.run(
        _call_asgi(
            app,
            "POST",
            "/key/block",
            headers=[(b"x-request-id", b"req-missing-disposition")],
            body=json.dumps({"key": "sk-test-virtual"}).encode("utf-8"),
        )
    )

    assert messages[0]["status"] == 400
    body = json.loads(messages[1]["body"])
    assert body["request_id"] == "req-missing-disposition"
    assert body["error"]["code"] == "aimanager_key_lifecycle_invalid"
    assert "x-aimanager-actor" in body["error"]["message"]
    assert "x-aimanager-reason" in body["error"]["message"]
    assert len(audit_events) == 1
    event = audit_events[0]
    assert event["event_type"] == "policy_blocked"
    assert event["reason"] == "aimanager_key_lifecycle_invalid"
    assert event["metadata"]["path"] == "/key/block"
    assert event["metadata"]["status_code"] == 400


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

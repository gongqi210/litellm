import asyncio
import json

from aimanager import asgi as asgi_module
from aimanager.asgi import YcapiOnlyAllowlistMiddleware


def test_business_chat_non_stream_requests_preserve_body_bytes() -> None:
    captured: dict[str, object] = {}
    raw_body = b'{\n  "model": "gemini-2.5-flash",\n  "messages": []\n}'

    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        message = await receive()
        captured["scope"] = scope
        captured["body"] = message["body"]
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    messages = asyncio.run(
        _call_asgi(
            YcapiOnlyAllowlistMiddleware(downstream),
            _receive_once(raw_body),
            headers=[(b"content-type", b"application/json"), (b"content-length", str(len(raw_body)).encode())],
        )
    )

    assert messages[0]["status"] == 204
    assert captured["body"] == raw_body
    assert captured["scope"]["headers"] == [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(raw_body)).encode()),
    ]


def test_business_chat_body_too_large_fails_closed(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        calls.append(scope)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    monkeypatch.setattr(asgi_module, "MAX_CHAT_COMPLETION_BODY_BYTES", 16)

    messages = asyncio.run(
        _call_asgi(
            YcapiOnlyAllowlistMiddleware(downstream),
            _receive_once(b'{"stream":true,"messages":[]}'),
        )
    )

    assert calls == []
    _assert_body_error(messages, "too large")


def test_business_chat_invalid_asgi_body_chunk_fails_closed() -> None:
    calls: list[dict[str, object]] = []

    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        calls.append(scope)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def invalid_receive() -> dict[str, object]:
        return {"type": "http.request", "body": object(), "more_body": False}

    messages = asyncio.run(_call_asgi(YcapiOnlyAllowlistMiddleware(downstream), invalid_receive))

    assert calls == []
    _assert_body_error(messages, "request body must be bytes")


def test_business_chat_requires_work_context_when_enforcement_enabled() -> None:
    calls: list[dict[str, object]] = []
    audit_events: list[dict[str, object]] = []

    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        calls.append(scope)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    raw_body = json.dumps(
        {
            "model": "gemini-2.5-flash",
            "messages": [{"role": "user", "content": "internal prompt must not leak"}],
        }
    ).encode("utf-8")

    messages = asyncio.run(
        _call_asgi(
            YcapiOnlyAllowlistMiddleware(
                downstream,
                audit_sink=audit_events.append,
                work_context_enforcement_enabled=True,
            ),
            _receive_once(raw_body),
            headers=[(b"x-request-id", b"req-work-context-missing")],
        )
    )

    assert calls == []
    _assert_work_context_error(messages, "metadata")
    body_text = messages[1]["body"].decode("utf-8")
    assert "internal prompt must not leak" not in body_text
    assert len(audit_events) == 1
    assert audit_events[0]["event_type"] == "policy_blocked"
    assert audit_events[0]["reason"] == "aimanager_work_context_invalid"


def test_business_chat_forwards_valid_work_context_when_enforcement_enabled() -> None:
    captured: dict[str, object] = {}
    raw_body = json.dumps(
        {
            "model": "gemini-2.5-flash",
            "stream": True,
            "stream_options": {"include_usage": False},
            "messages": [{"role": "user", "content": "hello"}],
            "user": "employee-smoke-001",
            "metadata": _valid_work_context_metadata(image_count=0),
        }
    ).encode("utf-8")

    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        message = await receive()
        captured["scope"] = scope
        captured["body"] = message["body"]
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    messages = asyncio.run(
        _call_asgi(
            YcapiOnlyAllowlistMiddleware(downstream, work_context_enforcement_enabled=True),
            _receive_once(raw_body),
            headers=[(b"content-type", b"application/json"), (b"content-length", str(len(raw_body)).encode())],
        )
    )

    assert messages[0]["status"] == 204
    forwarded = json.loads(captured["body"])
    assert forwarded["metadata"]["scenario_l2"] == "code_assist"
    assert forwarded["stream_options"]["include_usage"] is True
    forwarded_headers = dict(captured["scope"]["headers"])
    assert forwarded_headers[b"content-length"] == str(len(captured["body"])).encode("ascii")


def test_business_image_requires_work_context_when_enforcement_enabled() -> None:
    calls: list[dict[str, object]] = []

    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        calls.append(scope)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    messages = asyncio.run(
        _call_asgi(
            YcapiOnlyAllowlistMiddleware(downstream, work_context_enforcement_enabled=True),
            _receive_once(
                json.dumps(
                    {
                        "model": "ycapi-image-1",
                        "prompt": "internal prompt must not leak",
                        "n": 1,
                    }
                ).encode("utf-8")
            ),
            path="/v1/images/generations",
            headers=[(b"x-request-id", b"req-image-context-missing")],
        )
    )

    assert calls == []
    _assert_work_context_error(messages, "metadata")
    assert "internal prompt must not leak" not in messages[1]["body"].decode("utf-8")


def test_business_image_forwards_valid_work_context_when_enforcement_enabled() -> None:
    captured: dict[str, object] = {}
    raw_body = json.dumps(
        {
            "model": "ycapi-image-1",
            "prompt": "image asset for internal docs",
            "n": 1,
            "user": "employee-smoke-001",
            "metadata": _valid_work_context_metadata(image_count=1),
        }
    ).encode("utf-8")

    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        message = await receive()
        captured["scope"] = scope
        captured["body"] = message["body"]
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    messages = asyncio.run(
        _call_asgi(
            YcapiOnlyAllowlistMiddleware(downstream, work_context_enforcement_enabled=True),
            _receive_once(raw_body),
            path="/v1/images/generations",
        )
    )

    assert messages[0]["status"] == 204
    assert captured["body"] == raw_body
    assert captured["scope"]["path"] == "/v1/images/generations"


def test_business_work_context_rejects_user_principal_mismatch() -> None:
    calls: list[dict[str, object]] = []
    metadata = _valid_work_context_metadata(image_count=0)
    metadata["end_user_principal"] = "employee-other-002"

    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        calls.append(scope)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    messages = asyncio.run(
        _call_asgi(
            YcapiOnlyAllowlistMiddleware(downstream, work_context_enforcement_enabled=True),
            _receive_once(
                json.dumps(
                    {
                        "model": "gemini-2.5-flash",
                        "messages": [{"role": "user", "content": "hello"}],
                        "user": "employee-smoke-001",
                        "metadata": metadata,
                    }
                ).encode("utf-8")
            ),
        )
    )

    assert calls == []
    _assert_work_context_error(messages, "user")


async def _call_asgi(
    app: YcapiOnlyAllowlistMiddleware,
    receive,
    headers: list[tuple[bytes, bytes]] | None = None,
    path: str = "/v1/chat/completions",
) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = []
    await app(
        {"type": "http", "method": "POST", "path": path, "headers": headers or []},
        receive,
        _collecting_send(messages),
    )
    return messages


def _receive_once(body: bytes):
    sent = False

    async def receive() -> dict[str, object]:
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


def _collecting_send(messages: list[dict[str, object]]):
    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    return send


def _assert_body_error(messages: list[dict[str, object]], expected_detail: str) -> None:
    assert messages[0]["status"] == 400
    body = json.loads(messages[1]["body"])
    assert body["error"]["code"] == "aimanager_request_body_invalid"
    assert expected_detail in body["error"]["message"]


def _assert_work_context_error(messages: list[dict[str, object]], expected_detail: str) -> None:
    assert messages[0]["status"] == 400
    response_headers = dict(messages[0]["headers"])
    assert response_headers[b"x-aimanager-policy-code"] == b"aimanager_work_context_invalid"
    body = json.loads(messages[1]["body"])
    assert body["error"]["code"] == "aimanager_work_context_invalid"
    assert expected_detail in body["error"]["message"]


def _valid_work_context_metadata(*, image_count: int) -> dict[str, object]:
    return {
        "work_item_id": "work-smoke-001",
        "employee_id": "employee-smoke-001",
        "department_id": "dept_smoke",
        "end_user_principal": "employee-smoke-001",
        "scenario_l1": "engineering",
        "scenario_l2": "code_assist",
        "internal_or_external": "internal",
        "channel": "sdk",
        "project_id": "proj_aimanager_runtime_smoke",
        "sensitivity_level": "internal",
        "approval_required": False,
        "cost_center_id": "cc_smoke",
        "currency": "CNY",
        "pricing_version": "m1-runtime-smoke",
        "image_count": image_count,
    }

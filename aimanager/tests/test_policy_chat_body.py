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


async def _call_asgi(
    app: YcapiOnlyAllowlistMiddleware,
    receive,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = []
    await app(
        {"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": headers or []},
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

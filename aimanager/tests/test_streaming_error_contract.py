from __future__ import annotations

import asyncio
import json

from aimanager.asgi import YcapiOnlyAllowlistMiddleware


def test_allowlist_middleware_redacts_and_normalizes_streaming_error_events() -> None:
    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"text/event-stream"),
                    (b"x-litellm-call-id", b"litellm-stream-error"),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": (
                    b'data: {"error":{"message":"stream failed with Bearer ycapi-stream-token and '
                    b'sk-stream-secret","type":"server_error","param":null,"code":"upstream_failed"}}\n\n'
                ),
                "more_body": False,
            }
        )

    app = YcapiOnlyAllowlistMiddleware(downstream)

    messages = asyncio.run(_call_asgi(app, "POST", "/v1/chat/completions"))

    assert messages[0]["status"] == 200
    stream_text = messages[1]["body"].decode("utf-8")
    assert "ycapi-stream-token" not in stream_text
    assert "sk-stream-secret" not in stream_text
    assert "Bearer [REDACTED]" in stream_text
    assert "sk-[REDACTED]" in stream_text
    assert stream_text.startswith("data: ")
    event = json.loads(stream_text.removeprefix("data: ").strip())
    assert event["request_id"] == "litellm-stream-error"
    assert event["error"]["message"] == "stream failed with Bearer [REDACTED] and sk-[REDACTED]"
    assert event["error"]["type"] == "server_error"
    assert event["error"]["code"] == "upstream_failed"


def test_allowlist_middleware_redacts_streaming_error_secret_split_across_chunks() -> None:
    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b'data: {"error":{"message":"split secret Bearer ycapi-',
                "more_body": True,
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b'stream-token and sk-stream-secret","param":null}}\n\n',
                "more_body": False,
            }
        )

    app = YcapiOnlyAllowlistMiddleware(downstream)

    messages = asyncio.run(
        _call_asgi(
            app,
            "POST",
            "/v1/chat/completions",
            headers=[(b"x-request-id", b"client-stream-split")],
        )
    )

    stream_text = b"".join(message.get("body", b"") for message in messages[1:]).decode("utf-8")
    assert "ycapi-stream-token" not in stream_text
    assert "sk-stream-secret" not in stream_text
    event = json.loads(stream_text.removeprefix("data: ").strip())
    assert event["request_id"] == "client-stream-split"
    assert event["error"]["message"] == "split secret Bearer [REDACTED] and sk-[REDACTED]"


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

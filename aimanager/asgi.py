from __future__ import annotations

import json
from typing import Any, Awaitable, Callable
from uuid import uuid4

from aimanager.policy import build_policy_error_body, evaluate_route

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class YcapiOnlyAllowlistMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", ""))
        path = str(scope.get("path", ""))
        decision = evaluate_route(method, path)
        if decision.allowed:
            await self.app(scope, receive, send)
            return

        request_id = _request_id_from_scope(scope)
        body = json.dumps(build_policy_error_body(decision, request_id)).encode("utf-8")
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"x-litellm-call-id", request_id.encode("utf-8")),
            (b"x-aimanager-policy-code", decision.code.encode("utf-8")),
        ]
        await send(
            {
                "type": "http.response.start",
                "status": decision.status_code,
                "headers": headers,
            }
        )
        await send({"type": "http.response.body", "body": body})


class _LazyLiteLLMProxyApp:
    def __init__(self) -> None:
        self._app: ASGIApp | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self._app is None:
            from litellm.proxy.proxy_server import app as litellm_proxy_app

            self._app = litellm_proxy_app
        await self._app(scope, receive, send)


def _request_id_from_scope(scope: Scope) -> str:
    headers = scope.get("headers") or []
    for key, value in headers:
        if key.lower() in {b"x-litellm-call-id", b"x-request-id"} and value:
            return value.decode("utf-8", errors="replace")
    return f"req_{uuid4().hex}"


app = YcapiOnlyAllowlistMiddleware(_LazyLiteLLMProxyApp())

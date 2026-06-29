from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable
from uuid import uuid4

from aimanager.audit import build_audit_event
from aimanager.policy import build_policy_error_body, evaluate_route

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]
AuditSink = Callable[[dict[str, Any]], None]

LOGGER = logging.getLogger("aimanager.audit")


class YcapiOnlyAllowlistMiddleware:
    def __init__(self, app: ASGIApp, audit_sink: AuditSink | None = None) -> None:
        self.app = app
        self.audit_sink = audit_sink or _log_audit_event

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
        _emit_policy_audit_event(self.audit_sink, scope, decision, request_id)
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


def _emit_policy_audit_event(
    audit_sink: AuditSink,
    scope: Scope,
    decision: Any,
    request_id: str,
) -> None:
    headers = _headers_from_scope(scope)
    method = str(scope.get("method", ""))
    path = str(scope.get("path", ""))
    event_type = _audit_event_type_for_policy_code(decision.code)
    event = build_audit_event(
        event_type,
        actor=_header(headers, "x-aimanager-actor", default="aimanager-policy"),
        subject_key_alias=_header(headers, "x-aimanager-key-alias", "x-litellm-key-alias"),
        team_id=_header(headers, "x-aimanager-team-id"),
        department_id=_header(headers, "x-aimanager-department-id"),
        project_id=_header(headers, "x-aimanager-project-id"),
        cost_center_id=_header(headers, "x-aimanager-cost-center-id"),
        reason=decision.code,
        request_id=request_id,
        metadata={
            "method": method,
            "path": path,
            "policy_code": decision.code,
            "status_code": decision.status_code,
        },
    )
    try:
        audit_sink(event)
    except Exception:
        LOGGER.exception("failed_to_emit_aimanager_audit_event request_id=%s", request_id)


def _audit_event_type_for_policy_code(policy_code: str) -> str:
    if policy_code in {"aimanager_passthrough_blocked", "aimanager_google_native_blocked"}:
        return "passthrough_blocked"
    return "policy_blocked"


def _headers_from_scope(scope: Scope) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in scope.get("headers") or []:
        header_name = key.decode("latin1", errors="replace").lower()
        if header_name not in headers:
            headers[header_name] = value.decode("utf-8", errors="replace")
    return headers


def _header(headers: dict[str, str], *names: str, default: str = "") -> str:
    for name in names:
        value = headers.get(name.lower(), "").strip()
        if value:
            return value
    return default


def _log_audit_event(event: dict[str, Any]) -> None:
    LOGGER.warning(
        "aimanager_audit_event=%s",
        json.dumps(event, sort_keys=True, separators=(",", ":")),
    )


app = YcapiOnlyAllowlistMiddleware(_LazyLiteLLMProxyApp())

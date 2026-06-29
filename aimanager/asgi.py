from __future__ import annotations

import json
import logging
import os
import re
import socket
import struct
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse
from uuid import uuid4

from aimanager.audit import build_audit_event
from aimanager.governance import KeyGovernanceError, normalize_key_request
from aimanager.policy import RouteDecision, build_policy_error_body, evaluate_route
from aimanager.runtime_metrics import DEFAULT_AIMANAGER_METRICS, AiManagerMetrics

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]
AuditSink = Callable[[dict[str, Any]], None]
MetricsSink = AiManagerMetrics
DatabaseReadyChecker = Callable[[], bool]

LOGGER = logging.getLogger("aimanager.audit")
KEY_GOVERNANCE_POLICY_CODE = "aimanager_key_governance_invalid"
KEY_LIFECYCLE_POLICY_CODE = "aimanager_key_lifecycle_invalid"
RBAC_POLICY_CODE = "aimanager_rbac_denied"
DATABASE_UNAVAILABLE_POLICY_CODE = "aimanager_database_unavailable"
MAX_KEY_GENERATE_BODY_BYTES = 64 * 1024
MAX_DOWNSTREAM_AUDIT_BODY_BYTES = 16 * 1024
MAX_DOWNSTREAM_ERROR_BODY_BYTES = 64 * 1024
MANAGEMENT_RBAC_ADMIN_ROLES = frozenset(
    {
        "admin",
        "aimanager_admin",
        "super_admin",
        "system_admin",
        "proxy_admin",
    }
)
MANAGEMENT_HIGH_RISK_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
MANAGEMENT_HIGH_RISK_WRITE_PREFIXES = (
    "/key",
    "/team",
    "/user",
    "/customer",
    "/organization",
    "/budget",
    "/spend",
    "/global/spend",
)
ROLE_ALIASES = {
    "superadmin": "super_admin",
    "systemadmin": "system_admin",
    "proxyadmin": "proxy_admin",
    "proxyadminviewer": "proxy_admin_viewer",
    "admin_viewer": "proxy_admin_viewer",
    "readonly": "read_only",
    "read-only": "read_only",
    "viewer": "read_only",
    "gm": "ceo",
    "cfo": "finance",
}
SENSITIVE_TEXT_PATTERNS = (
    (re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE), "Bearer [REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9._-]*"), "sk-[REDACTED]"),
    (re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@"), r"\1[REDACTED]@"),
    (
        re.compile(r"\b(YCAPI_API_TOKEN|api[_-]?key|token)\s*[:=]\s*[^,\s\"']+", re.IGNORECASE),
        r"\1=[REDACTED]",
    ),
)


class YcapiOnlyAllowlistMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        audit_sink: AuditSink | None = None,
        metrics_sink: MetricsSink | None = None,
        surface: str = "business",
        rbac_enabled: bool | None = None,
        database_ready_check_enabled: bool | None = None,
        database_ready_checker: DatabaseReadyChecker | None = None,
    ) -> None:
        self.app = app
        self.metrics_sink = metrics_sink or DEFAULT_AIMANAGER_METRICS
        self.audit_sink = _audit_sink_with_metrics(audit_sink or _log_audit_event, self.metrics_sink)
        self.surface = "management" if surface == "management" else "business"
        self.rbac_enabled = (
            _env_flag("AIMANAGER_RBAC_ENABLED", default=False) if rbac_enabled is None else rbac_enabled
        )
        self.database_ready_check_enabled = (
            _env_flag("AIMANAGER_DATABASE_READY_CHECK_ENABLED", default=False)
            if database_ready_check_enabled is None
            else database_ready_check_enabled
        )
        self.database_ready_checker = database_ready_checker or _database_ready_from_env

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", ""))
        path = str(scope.get("path", ""))
        decision = evaluate_route(method, path, surface=self.surface)
        metrics_send = _http_status_metrics_send_wrapper(send, self.metrics_sink, scope)
        if decision.allowed:
            if self.surface == "management" and _is_metrics_route(method, path):
                await _send_metrics_response(metrics_send, self.metrics_sink)
                return

            request_id = _request_id_from_scope(scope)
            if self.database_ready_check_enabled and _requires_database_ready(method, path):
                database_decision = _database_unavailable_decision(method, path)
                if not self.database_ready_checker():
                    _emit_policy_audit_event(self.audit_sink, scope, database_decision, request_id)
                    await _send_policy_error(metrics_send, database_decision, request_id)
                    return
            contract_send = _downstream_error_contract_send_wrapper(
                metrics_send,
                request_id,
            )
            audit_send = _downstream_budget_audit_send_wrapper(
                contract_send,
                self.audit_sink,
                scope,
                request_id,
            )
            if self.surface == "management" and self.rbac_enabled:
                rbac_decision = _management_rbac_decision(scope, method, path)
                if rbac_decision is not None:
                    _emit_management_rbac_audit_event(
                        self.audit_sink,
                        scope,
                        rbac_decision,
                        request_id,
                    )
                    await _send_policy_error(metrics_send, rbac_decision, request_id)
                    return
            if self.surface == "management" and _is_key_lifecycle_route(method, path):
                disposition_error = _key_lifecycle_disposition_error(scope)
                if disposition_error:
                    _emit_key_lifecycle_policy_audit_event(
                        self.audit_sink,
                        scope,
                        request_id,
                        disposition_error,
                    )
                    await _send_key_lifecycle_error(metrics_send, disposition_error, request_id)
                    return
                audit_send = _downstream_key_lifecycle_audit_send_wrapper(
                    audit_send,
                    self.audit_sink,
                    scope,
                    request_id,
                )
            if self.surface == "management" and _is_key_generate_route(method, path):
                payload: dict[str, Any] | None = None
                try:
                    raw_body = await _read_request_body(receive)
                    payload = _parse_json_object_body(raw_body)
                    normalized_body = _normalized_key_generate_body(payload)
                except KeyGovernanceError as exc:
                    _emit_key_governance_audit_event(
                        self.audit_sink,
                        scope,
                        request_id,
                        str(exc),
                        payload,
                    )
                    await _send_key_governance_error(metrics_send, str(exc), request_id)
                    return

                await self.app(
                    _scope_with_json_body_headers(scope, normalized_body),
                    _receive_once(normalized_body),
                    audit_send,
                )
                return
            await self.app(scope, receive, audit_send)
            return

        request_id = _request_id_from_scope(scope)
        _emit_policy_audit_event(self.audit_sink, scope, decision, request_id)
        await _send_policy_error(metrics_send, decision, request_id)


class _LazyLiteLLMProxyApp:
    def __init__(self) -> None:
        self._app: ASGIApp | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self._app is None:
            from litellm.proxy.proxy_server import app as litellm_proxy_app

            from aimanager.litellm_entrypoint import register_aimanager_image_model_costs

            register_aimanager_image_model_costs()
            self._app = litellm_proxy_app
        await self._app(scope, receive, send)


def _request_id_from_scope(scope: Scope) -> str:
    headers = scope.get("headers") or []
    for key, value in headers:
        if key.lower() in {b"x-litellm-call-id", b"x-request-id"} and value:
            return value.decode("utf-8", errors="replace")
    return f"req_{uuid4().hex}"


def _is_key_generate_route(method: str, path: str) -> bool:
    normalized_path = _normalized_request_path(path)
    return method.upper() == "POST" and normalized_path == "/key/generate"


def _is_key_lifecycle_route(method: str, path: str) -> bool:
    if method.upper() != "POST":
        return False
    return _key_lifecycle_event(path) is not None


def _is_metrics_route(method: str, path: str) -> bool:
    return method.upper() == "GET" and _normalized_request_path(path) == "/metrics"


def _requires_database_ready(method: str, path: str) -> bool:
    normalized_path = _normalized_request_path(path)
    if normalized_path in {"/health", "/health/liveliness"}:
        return False
    if _is_metrics_route(method, normalized_path):
        return False
    return True


def _database_unavailable_decision(method: str, path: str) -> RouteDecision:
    normalized_method = method.upper()
    normalized_path = _normalized_request_path(path)
    return RouteDecision(
        allowed=False,
        code=DATABASE_UNAVAILABLE_POLICY_CODE,
        message=(
            "AiManager database is unavailable; "
            f"failing closed before downstream LiteLLM for {normalized_method} {normalized_path}"
        ),
        status_code=503,
    )


def _management_rbac_decision(scope: Scope, method: str, path: str) -> RouteDecision | None:
    if not _is_management_high_risk_write(method, path):
        return None
    role = _management_role_from_scope(scope)
    if role in MANAGEMENT_RBAC_ADMIN_ROLES:
        return None

    normalized_method = method.upper()
    normalized_path = _normalized_request_path(path)
    display_role = role or "missing"
    return RouteDecision(
        allowed=False,
        code=RBAC_POLICY_CODE,
        message=(
            "AiManager RBAC blocks role "
            f"{display_role} from high-risk management operation {normalized_method} {normalized_path}"
        ),
        status_code=403,
    )


def _is_management_high_risk_write(method: str, path: str) -> bool:
    if method.upper() not in MANAGEMENT_HIGH_RISK_WRITE_METHODS:
        return False
    normalized_path = _normalized_request_path(path)
    return any(
        normalized_path == prefix or normalized_path.startswith(f"{prefix}/")
        for prefix in MANAGEMENT_HIGH_RISK_WRITE_PREFIXES
    )


def _management_role_from_scope(scope: Scope) -> str:
    headers = _headers_from_scope(scope)
    return _normalize_management_role(
        _header(
            headers,
            "x-aimanager-role",
            "x-litellm-user-role",
            "x-user-role",
            "x-authenticated-role",
        )
    )


def _normalize_management_role(role: str) -> str:
    normalized = role.strip().lower().replace(" ", "_").replace("-", "_")
    return ROLE_ALIASES.get(normalized, normalized)


def _key_lifecycle_event(path: str) -> tuple[str, str] | None:
    normalized_path = _normalized_request_path(path)
    if normalized_path == "/key/block":
        return "key_frozen", "freeze"
    if normalized_path == "/key/delete":
        return "key_revoked", "revoke"
    return None


def _normalized_request_path(path: str) -> str:
    normalized_path = path.split("?", 1)[0].strip() or "/"
    if not normalized_path.startswith("/"):
        normalized_path = f"/{normalized_path}"
    if len(normalized_path) > 1:
        normalized_path = normalized_path.rstrip("/")
    return normalized_path


async def _read_request_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    total_size = 0
    while True:
        message = await receive()
        message_type = message.get("type")
        if message_type == "http.disconnect":
            break
        if message_type != "http.request":
            continue

        chunk = message.get("body", b"")
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        if not isinstance(chunk, bytes):
            raise KeyGovernanceError("request body must be bytes for AiManager key creation")

        total_size += len(chunk)
        if total_size > MAX_KEY_GENERATE_BODY_BYTES:
            raise KeyGovernanceError("request body is too large for AiManager key creation")
        chunks.append(chunk)

        if not bool(message.get("more_body", False)):
            break
    return b"".join(chunks)


def _parse_json_object_body(raw_body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KeyGovernanceError("JSON object body is required for AiManager key creation") from exc
    if not isinstance(payload, dict):
        raise KeyGovernanceError("JSON object body is required for AiManager key creation")
    return payload


def _normalized_key_generate_body(payload: dict[str, Any]) -> bytes:
    normalized = normalize_key_request(payload, shared_key=_is_shared_key_request(payload))
    return json.dumps(normalized, separators=(",", ":")).encode("utf-8")


def _is_shared_key_request(payload: dict[str, Any]) -> bool:
    metadata = payload.get("metadata")
    return isinstance(metadata, dict) and metadata.get("shared_key") is True


def _scope_with_json_body_headers(scope: Scope, body: bytes) -> Scope:
    headers: list[tuple[bytes, bytes]] = []
    has_content_type = False
    for key, value in scope.get("headers") or []:
        lowered = key.lower()
        if lowered in {b"content-length", b"transfer-encoding"}:
            continue
        if lowered == b"content-type":
            has_content_type = True
        headers.append((key, value))
    if not has_content_type:
        headers.append((b"content-type", b"application/json"))
    headers.append((b"content-length", str(len(body)).encode("ascii")))

    updated_scope = dict(scope)
    updated_scope["headers"] = headers
    return updated_scope


def _receive_once(body: bytes) -> Receive:
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


async def _send_key_governance_error(send: Send, message: str, request_id: str) -> None:
    response = {
        "error": {
            "message": f"AiManager key governance rejects key creation: {message}",
            "type": "invalid_request_error",
            "param": None,
            "code": KEY_GOVERNANCE_POLICY_CODE,
        },
        "request_id": request_id,
    }
    body = json.dumps(response).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"x-litellm-call-id", request_id.encode("utf-8")),
        (b"x-aimanager-policy-code", KEY_GOVERNANCE_POLICY_CODE.encode("utf-8")),
    ]
    await send({"type": "http.response.start", "status": 400, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def _send_key_lifecycle_error(send: Send, message: str, request_id: str) -> None:
    response = {
        "error": {
            "message": f"AiManager key lifecycle rejects key disposition: {message}",
            "type": "invalid_request_error",
            "param": None,
            "code": KEY_LIFECYCLE_POLICY_CODE,
        },
        "request_id": request_id,
    }
    body = json.dumps(response).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"x-litellm-call-id", request_id.encode("utf-8")),
        (b"x-aimanager-policy-code", KEY_LIFECYCLE_POLICY_CODE.encode("utf-8")),
    ]
    await send({"type": "http.response.start", "status": 400, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def _send_policy_error(send: Send, decision: RouteDecision, request_id: str) -> None:
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


async def _send_metrics_response(send: Send, metrics_sink: MetricsSink) -> None:
    body = metrics_sink.render_prometheus_text().encode("utf-8")
    headers = [
        (b"content-type", b"text/plain; version=0.0.4; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    await send({"type": "http.response.start", "status": 200, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def _http_status_metrics_send_wrapper(
    send: Send,
    metrics_sink: MetricsSink,
    scope: Scope,
) -> Send:
    method = str(scope.get("method", ""))
    recorded = False

    async def wrapped_send(message: Message) -> None:
        nonlocal recorded
        await send(message)
        if recorded or message.get("type") != "http.response.start":
            return
        raw_status = message.get("status")
        if isinstance(raw_status, int):
            metrics_sink.record_http_response(method=method, status_code=raw_status)
            recorded = True

    return wrapped_send


def _downstream_error_contract_send_wrapper(
    send: Send,
    request_id: str,
) -> Send:
    status_code: int | None = None
    response_headers: dict[str, str] = {}
    raw_response_headers: list[tuple[Any, Any]] = []
    body_chunks: list[bytes] = []
    body_size = 0
    passthrough = False

    async def wrapped_send(message: Message) -> None:
        nonlocal body_size, passthrough, raw_response_headers, response_headers, status_code

        message_type = message.get("type")
        if message_type == "http.response.start":
            raw_status = message.get("status")
            status_code = raw_status if isinstance(raw_status, int) else None
            response_headers = _headers_from_message(message)
            raw_response_headers = list(message.get("headers") or [])
            passthrough = status_code is None or status_code < 400
            if passthrough:
                await send(message)
            return

        if message_type != "http.response.body" or passthrough:
            await send(message)
            return

        chunk = message.get("body", b"")
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        if isinstance(chunk, bytes) and body_size < MAX_DOWNSTREAM_ERROR_BODY_BYTES:
            remaining = MAX_DOWNSTREAM_ERROR_BODY_BYTES - body_size
            body_chunks.append(chunk[:remaining])
            body_size += min(len(chunk), remaining)

        if bool(message.get("more_body", False)):
            return

        final_status = status_code or 500
        response_request_id = _header(response_headers, "x-litellm-call-id", "x-request-id", default=request_id)
        response_body = json.dumps(
            _build_downstream_error_body(
                status_code=final_status,
                response_body=b"".join(body_chunks),
                request_id=response_request_id,
            ),
            separators=(",", ":"),
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": final_status,
                "headers": _downstream_error_response_headers(
                    raw_response_headers,
                    request_id=response_request_id,
                    body=response_body,
                ),
            }
        )
        await send({"type": "http.response.body", "body": response_body})

    return wrapped_send


def _build_downstream_error_body(
    *,
    status_code: int,
    response_body: bytes,
    request_id: str,
) -> dict[str, object]:
    fallback_message = f"Upstream request failed with HTTP {status_code}"
    fallback_type = _downstream_error_type(status_code)
    error: dict[str, object] = {
        "message": fallback_message,
        "type": fallback_type,
        "param": None,
        "code": "upstream_error",
    }

    parsed_error = _parsed_openai_error(response_body)
    if parsed_error is not None:
        message = _safe_error_text(parsed_error.get("message"), fallback=fallback_message)
        error_type = _safe_error_text(parsed_error.get("type"), fallback=fallback_type)
        code = _safe_error_text(parsed_error.get("code"), fallback="upstream_error")
        param = parsed_error.get("param")
        error = {
            "message": message,
            "type": error_type,
            "param": _safe_error_text(param, fallback="") if isinstance(param, str) else None,
            "code": code,
        }

    return {"error": error, "request_id": request_id}


def _parsed_openai_error(response_body: bytes) -> dict[str, Any] | None:
    try:
        payload: Any = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    return error if isinstance(error, dict) else None


def _downstream_error_type(status_code: int) -> str:
    if status_code == 401:
        return "authentication_error"
    if status_code == 403:
        return "permission_error"
    if status_code == 404:
        return "not_found_error"
    if status_code == 429:
        return "rate_limit_error"
    if status_code >= 500:
        return "server_error"
    return "upstream_error"


def _safe_error_text(value: Any, *, fallback: str) -> str:
    text = _text_from_value(value)
    if not text:
        return fallback
    return _redact_sensitive_text(text)


def _redact_sensitive_text(text: str) -> str:
    redacted = text
    for pattern, replacement in SENSITIVE_TEXT_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def _downstream_error_response_headers(
    raw_headers: list[tuple[Any, Any]],
    *,
    request_id: str,
    body: bytes,
) -> list[tuple[bytes, bytes]]:
    replaced_headers = {b"content-length", b"content-type", b"x-litellm-call-id"}
    headers: list[tuple[bytes, bytes]] = []
    for key, value in raw_headers:
        key_bytes = key if isinstance(key, bytes) else str(key).encode("latin1", errors="replace")
        if key_bytes.lower() in replaced_headers:
            continue
        value_bytes = value if isinstance(value, bytes) else str(value).encode("latin1", errors="replace")
        headers.append((key_bytes, value_bytes))
    headers.extend(
        [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"x-litellm-call-id", request_id.encode("utf-8")),
        ]
    )
    return headers


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


def _emit_key_governance_audit_event(
    audit_sink: AuditSink,
    scope: Scope,
    request_id: str,
    governance_error: str,
    payload: dict[str, Any] | None,
) -> None:
    headers = _headers_from_scope(scope)
    method = str(scope.get("method", ""))
    path = str(scope.get("path", ""))
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    metadata = metadata if isinstance(metadata, dict) else {}
    event = build_audit_event(
        "policy_blocked",
        actor=_header(headers, "x-aimanager-actor", default="aimanager-policy"),
        subject_key_alias=_header(
            headers,
            "x-aimanager-key-alias",
            "x-litellm-key-alias",
            default=_payload_text(payload, "key_alias"),
        ),
        team_id=_header(headers, "x-aimanager-team-id", default=_payload_text(payload, "team_id")),
        department_id=_header(
            headers,
            "x-aimanager-department-id",
            default=_payload_text(metadata, "department_id"),
        ),
        project_id=_header(
            headers,
            "x-aimanager-project-id",
            default=_payload_text(metadata, "project_id"),
        ),
        cost_center_id=_header(
            headers,
            "x-aimanager-cost-center-id",
            default=_payload_text(metadata, "cost_center_id"),
        ),
        reason=KEY_GOVERNANCE_POLICY_CODE,
        request_id=request_id,
        metadata={
            "method": method,
            "path": path,
            "policy_code": KEY_GOVERNANCE_POLICY_CODE,
            "status_code": 400,
            "governance_error": governance_error,
        },
    )
    try:
        audit_sink(event)
    except Exception:
        LOGGER.exception("failed_to_emit_aimanager_audit_event request_id=%s", request_id)


def _emit_key_lifecycle_policy_audit_event(
    audit_sink: AuditSink,
    scope: Scope,
    request_id: str,
    disposition_error: str,
) -> None:
    headers = _headers_from_scope(scope)
    method = str(scope.get("method", ""))
    path = str(scope.get("path", ""))
    event = build_audit_event(
        "policy_blocked",
        actor=_header(headers, "x-aimanager-actor", default="aimanager-policy"),
        subject_key_alias=_header(headers, "x-aimanager-key-alias", "x-litellm-key-alias"),
        team_id=_header(headers, "x-aimanager-team-id"),
        department_id=_header(headers, "x-aimanager-department-id"),
        project_id=_header(headers, "x-aimanager-project-id"),
        cost_center_id=_header(headers, "x-aimanager-cost-center-id"),
        reason=KEY_LIFECYCLE_POLICY_CODE,
        request_id=request_id,
        metadata={
            "method": method,
            "path": path,
            "policy_code": KEY_LIFECYCLE_POLICY_CODE,
            "status_code": 400,
            "disposition_error": disposition_error,
        },
    )
    try:
        audit_sink(event)
    except Exception:
        LOGGER.exception("failed_to_emit_aimanager_audit_event request_id=%s", request_id)


def _emit_management_rbac_audit_event(
    audit_sink: AuditSink,
    scope: Scope,
    decision: RouteDecision,
    request_id: str,
) -> None:
    headers = _headers_from_scope(scope)
    role = _management_role_from_scope(scope) or "missing"
    method = str(scope.get("method", ""))
    path = str(scope.get("path", ""))
    event = build_audit_event(
        "policy_blocked",
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
            "role": role,
            "policy_code": decision.code,
            "status_code": decision.status_code,
        },
    )
    try:
        audit_sink(event)
    except Exception:
        LOGGER.exception("failed_to_emit_aimanager_audit_event request_id=%s", request_id)


def _downstream_budget_audit_send_wrapper(
    send: Send,
    audit_sink: AuditSink,
    scope: Scope,
    request_id: str,
) -> Send:
    status_code: int | None = None
    response_headers: dict[str, str] = {}
    body_chunks: list[bytes] = []
    body_size = 0
    emitted = False

    async def wrapped_send(message: Message) -> None:
        nonlocal body_size, emitted, response_headers, status_code

        await send(message)

        message_type = message.get("type")
        if message_type == "http.response.start":
            raw_status = message.get("status")
            status_code = raw_status if isinstance(raw_status, int) else None
            response_headers = _headers_from_message(message)
            return

        if message_type != "http.response.body" or status_code != 429 or emitted:
            return

        chunk = message.get("body", b"")
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        if isinstance(chunk, bytes) and body_size < MAX_DOWNSTREAM_AUDIT_BODY_BYTES:
            remaining = MAX_DOWNSTREAM_AUDIT_BODY_BYTES - body_size
            body_chunks.append(chunk[:remaining])
            body_size += min(len(chunk), remaining)

        if bool(message.get("more_body", False)):
            return

        emitted = _emit_downstream_budget_audit_event(
            audit_sink,
            scope,
            response_headers,
            status_code,
            b"".join(body_chunks),
            request_id,
        )

    return wrapped_send


def _downstream_key_lifecycle_audit_send_wrapper(
    send: Send,
    audit_sink: AuditSink,
    scope: Scope,
    request_id: str,
) -> Send:
    status_code: int | None = None
    response_headers: dict[str, str] = {}
    emitted = False

    async def wrapped_send(message: Message) -> None:
        nonlocal emitted, response_headers, status_code

        await send(message)

        message_type = message.get("type")
        if message_type == "http.response.start":
            raw_status = message.get("status")
            status_code = raw_status if isinstance(raw_status, int) else None
            response_headers = _headers_from_message(message)
            return

        if message_type != "http.response.body" or emitted or status_code is None:
            return
        if status_code < 200 or status_code >= 300:
            return
        if bool(message.get("more_body", False)):
            return

        emitted = _emit_downstream_key_lifecycle_audit_event(
            audit_sink,
            scope,
            response_headers,
            status_code,
            request_id,
        )

    return wrapped_send


def _emit_downstream_key_lifecycle_audit_event(
    audit_sink: AuditSink,
    scope: Scope,
    response_headers: dict[str, str],
    status_code: int,
    request_id: str,
) -> bool:
    method = str(scope.get("method", ""))
    path = str(scope.get("path", ""))
    lifecycle_event = _key_lifecycle_event(path)
    if method.upper() != "POST" or lifecycle_event is None:
        return False

    event_type, operation = lifecycle_event
    headers = _headers_from_scope(scope)
    response_request_id = _header(response_headers, "x-litellm-call-id", "x-request-id")
    event = build_audit_event(
        event_type,
        actor=_header(headers, "x-aimanager-actor", default="aimanager-policy"),
        subject_key_alias=_header(headers, "x-aimanager-key-alias", "x-litellm-key-alias"),
        team_id=_header(headers, "x-aimanager-team-id"),
        department_id=_header(headers, "x-aimanager-department-id"),
        project_id=_header(headers, "x-aimanager-project-id"),
        cost_center_id=_header(headers, "x-aimanager-cost-center-id"),
        reason=_header(headers, "x-aimanager-reason", "x-aimanager-disposition-reason"),
        request_id=response_request_id or request_id,
        metadata={
            "method": method,
            "path": path,
            "status_code": status_code,
            "operation": operation,
        },
    )
    try:
        audit_sink(event)
    except Exception:
        LOGGER.exception("failed_to_emit_aimanager_audit_event request_id=%s", request_id)
    return True


def _emit_downstream_budget_audit_event(
    audit_sink: AuditSink,
    scope: Scope,
    response_headers: dict[str, str],
    status_code: int,
    response_body: bytes,
    request_id: str,
) -> bool:
    budget_error = _budget_error_from_response_body(response_body)
    if budget_error is None:
        return False

    downstream_error_type, downstream_error_message = budget_error
    headers = _headers_from_scope(scope)
    method = str(scope.get("method", ""))
    path = str(scope.get("path", ""))
    response_request_id = _header(response_headers, "x-litellm-call-id", "x-request-id")
    event = build_audit_event(
        "budget_blocked",
        actor=_header(headers, "x-aimanager-actor", default="aimanager-policy"),
        subject_key_alias=_header(headers, "x-aimanager-key-alias", "x-litellm-key-alias"),
        team_id=_header(headers, "x-aimanager-team-id"),
        department_id=_header(headers, "x-aimanager-department-id"),
        project_id=_header(headers, "x-aimanager-project-id"),
        cost_center_id=_header(headers, "x-aimanager-cost-center-id"),
        reason="budget_exceeded",
        request_id=response_request_id or request_id,
        metadata={
            "method": method,
            "path": path,
            "status_code": status_code,
            "downstream_error_type": downstream_error_type,
            "downstream_error_message": downstream_error_message,
        },
    )
    try:
        audit_sink(event)
    except Exception:
        LOGGER.exception("failed_to_emit_aimanager_audit_event request_id=%s", request_id)
    return True


def _budget_error_from_response_body(body: bytes) -> tuple[str, str] | None:
    try:
        payload: Any = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    error_type = _text_from_value(error.get("type") or error.get("code"))
    message = _text_from_value(error.get("message"))
    if error_type.lower() == "budget_exceeded":
        return "budget_exceeded", message
    normalized_message = message.lower()
    if "budget has been exceeded" in normalized_message or (
        "current cost" in normalized_message and "max budget" in normalized_message
    ):
        return error_type or "budget_exceeded", message
    return None


def _headers_from_message(message: Message) -> dict[str, str]:
    return _headers_from_pairs(message.get("headers") or [])


def _audit_event_type_for_policy_code(policy_code: str) -> str:
    if policy_code in {"aimanager_passthrough_blocked", "aimanager_google_native_blocked"}:
        return "passthrough_blocked"
    return "policy_blocked"


def _headers_from_scope(scope: Scope) -> dict[str, str]:
    return _headers_from_pairs(scope.get("headers") or [])


def _key_lifecycle_disposition_error(scope: Scope) -> str:
    headers = _headers_from_scope(scope)
    missing: list[str] = []
    if not _header(headers, "x-aimanager-actor"):
        missing.append("x-aimanager-actor")
    if not _header(headers, "x-aimanager-reason", "x-aimanager-disposition-reason"):
        missing.append("x-aimanager-reason")
    if not missing:
        return ""
    return f"missing required disposition header(s): {', '.join(missing)}"


def _headers_from_pairs(pairs: Any) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in pairs:
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


def _payload_text(payload: dict[str, Any] | None, field_name: str) -> str:
    if not isinstance(payload, dict):
        return ""
    value = payload.get(field_name)
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value)


def _text_from_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value)


def _audit_sink_with_metrics(audit_sink: AuditSink, metrics_sink: MetricsSink) -> AuditSink:
    def wrapped(event: dict[str, Any]) -> None:
        try:
            metrics_sink.record_audit_event(event)
        except Exception:
            LOGGER.exception("failed_to_record_aimanager_audit_metric")
        audit_sink(event)

    return wrapped


def _env_flag(name: str, *, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _database_ready_from_env() -> bool:
    database_url = os.environ.get("DATABASE_URL", "")
    parsed = urlparse(database_url)
    host = parsed.hostname
    if not host:
        return False
    port = parsed.port or 5432
    timeout = _env_float("AIMANAGER_DATABASE_READY_CHECK_TIMEOUT_SECONDS", default=0.25)
    return _postgres_server_responds(host, port, timeout)


def _postgres_server_responds(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(struct.pack("!II", 8, 80877103))
            return sock.recv(1) in {b"S", b"N"}
    except OSError:
        return False


def _env_float(name: str, *, default: float) -> float:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _log_audit_event(event: dict[str, Any]) -> None:
    LOGGER.warning(
        "aimanager_audit_event=%s",
        json.dumps(event, sort_keys=True, separators=(",", ":")),
    )


app = YcapiOnlyAllowlistMiddleware(
    _LazyLiteLLMProxyApp(),
    surface=os.environ.get("AIMANAGER_ROUTE_SURFACE", "business"),
)

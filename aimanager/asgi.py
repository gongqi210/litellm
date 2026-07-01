from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import socket
import struct
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import urlparse
from uuid import uuid4

from aimanager.audit import build_audit_event
from aimanager.governance import KeyGovernanceError, normalize_key_request
from aimanager.policy import ALLOWED_BUSINESS_ROUTES, RouteDecision, build_policy_error_body, evaluate_route
from aimanager.runtime_metrics import DEFAULT_AIMANAGER_METRICS, AiManagerMetrics
from aimanager.work_context import validate_work_context

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]
AuditSink = Callable[[dict[str, Any]], None]
MetricsSink = AiManagerMetrics
DatabaseReadyChecker = Callable[[], bool]
KeyDispositionChecker = Callable[[str], Awaitable[RouteDecision | None]]

LOGGER = logging.getLogger("aimanager.audit")
KEY_GOVERNANCE_POLICY_CODE = "aimanager_key_governance_invalid"
REQUEST_BODY_POLICY_CODE = "aimanager_request_body_invalid"
KEY_LIFECYCLE_POLICY_CODE = "aimanager_key_lifecycle_invalid"
KEY_BLOCKED_POLICY_CODE = "aimanager_key_blocked"
KEY_REVOKED_POLICY_CODE = "aimanager_key_revoked"
BUSINESS_TOKEN_POLICY_CODE = "aimanager_business_token_forbidden"
WORK_CONTEXT_POLICY_CODE = "aimanager_work_context_invalid"
RBAC_POLICY_CODE = "aimanager_rbac_denied"
DATABASE_UNAVAILABLE_POLICY_CODE = "aimanager_database_unavailable"
MAX_KEY_GENERATE_BODY_BYTES = 64 * 1024
MAX_CHAT_COMPLETION_BODY_BYTES = 32 * 1024 * 1024
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
LITELLM_CREDENTIAL_HEADER_PRECEDENCE = (
    ("x-litellm-api-key", "bearer_or_raw"),
    ("authorization", "bearer"),
    ("api-key", "raw"),
    ("x-api-key", "raw"),
    ("x-goog-api-key", "raw"),
    ("ocp-apim-subscription-key", "raw"),
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
SSE_EVENT_DELIMITER_PATTERN = re.compile(br"\r\n\r\n|\n\n|\r\r")


class YcapiOnlyAllowlistMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        audit_sink: AuditSink | None = None,
        metrics_sink: MetricsSink | None = None,
        surface: str = "business",
        rbac_enabled: bool | None = None,
        work_context_enforcement_enabled: bool | None = None,
        database_ready_check_enabled: bool | None = None,
        database_ready_checker: DatabaseReadyChecker | None = None,
        key_disposition_checker: KeyDispositionChecker | None = None,
    ) -> None:
        self.app = app
        self.metrics_sink = metrics_sink or DEFAULT_AIMANAGER_METRICS
        self.audit_sink = _audit_sink_with_metrics(audit_sink or _log_audit_event, self.metrics_sink)
        self.surface = "management" if surface == "management" else "business"
        self.rbac_enabled = (
            _env_flag("AIMANAGER_RBAC_ENABLED", default=False) if rbac_enabled is None else rbac_enabled
        )
        self.work_context_enforcement_enabled = (
            _env_flag("AIMANAGER_WORK_CONTEXT_ENFORCEMENT_ENABLED", default=False)
            if work_context_enforcement_enabled is None
            else work_context_enforcement_enabled
        )
        self.database_ready_check_enabled = (
            _env_flag("AIMANAGER_DATABASE_READY_CHECK_ENABLED", default=False)
            if database_ready_check_enabled is None
            else database_ready_check_enabled
        )
        self.database_ready_checker = database_ready_checker or _database_ready_from_env
        self.key_disposition_checker = key_disposition_checker or _litellm_key_disposition_from_db

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
            if _requires_key_disposition_check(method, path):
                token = _litellm_credential_token_from_scope(scope)
                if token:
                    if self.surface == "business":
                        business_token_decision = _business_forbidden_credential_decision(token)
                        if business_token_decision is not None:
                            _emit_policy_audit_event(self.audit_sink, scope, business_token_decision, request_id)
                            await _send_policy_error(metrics_send, business_token_decision, request_id)
                            return
                    try:
                        key_disposition_decision = await self.key_disposition_checker(token)
                    except Exception:
                        LOGGER.exception("failed_to_check_aimanager_key_disposition request_id=%s", request_id)
                        key_disposition_decision = _database_unavailable_decision(method, path)
                    if key_disposition_decision is not None and not key_disposition_decision.allowed:
                        _emit_policy_audit_event(self.audit_sink, scope, key_disposition_decision, request_id)
                        await _send_policy_error(metrics_send, key_disposition_decision, request_id)
                        return
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
            if (
                self.surface == "business"
                and self.work_context_enforcement_enabled
                and _requires_business_work_context(method, path)
            ):
                try:
                    raw_body = await _read_request_body(receive, max_body_bytes=MAX_CHAT_COMPLETION_BODY_BYTES)
                    payload = _parse_json_object_body(raw_body, purpose="work context enforcement")
                except KeyGovernanceError as exc:
                    await _send_request_body_error(metrics_send, str(exc), request_id)
                    return

                work_context_error = _business_work_context_error(payload)
                if work_context_error:
                    decision = _work_context_invalid_decision(method, path, work_context_error)
                    _emit_policy_audit_event(self.audit_sink, scope, decision, request_id)
                    await _send_work_context_error(metrics_send, work_context_error, request_id)
                    return

                downstream_body = _stream_usage_enforced_body(raw_body) if _is_chat_completion_route(method, path) else raw_body
                downstream_scope = (
                    _scope_with_json_body_headers(scope, downstream_body)
                    if downstream_body != raw_body
                    else scope
                )
                await self.app(downstream_scope, _receive_once(downstream_body), audit_send)
                return
            if _is_chat_completion_route(method, path):
                try:
                    raw_body = await _read_request_body(receive, max_body_bytes=MAX_CHAT_COMPLETION_BODY_BYTES)
                except KeyGovernanceError as exc:
                    await _send_request_body_error(metrics_send, str(exc), request_id)
                    return
                stream_usage_body = _stream_usage_enforced_body(raw_body)
                downstream_scope = (
                    _scope_with_json_body_headers(scope, stream_usage_body)
                    if stream_usage_body != raw_body
                    else scope
                )
                await self.app(downstream_scope, _receive_once(stream_usage_body), audit_send)
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

            from aimanager.litellm_entrypoint import (
                register_aimanager_enforced_params_guard,
                register_aimanager_image_model_costs,
            )

            register_aimanager_image_model_costs()
            register_aimanager_enforced_params_guard()
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


def _is_chat_completion_route(method: str, path: str) -> bool:
    normalized_path = _normalized_request_path(path)
    return method.upper() == "POST" and normalized_path == "/v1/chat/completions"


def _is_image_generation_route(method: str, path: str) -> bool:
    normalized_path = _normalized_request_path(path)
    return method.upper() == "POST" and normalized_path == "/v1/images/generations"


def _requires_business_work_context(method: str, path: str) -> bool:
    return _is_chat_completion_route(method, path) or _is_image_generation_route(method, path)


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


def _requires_key_disposition_check(method: str, path: str) -> bool:
    return (method.upper(), _normalized_request_path(path)) in ALLOWED_BUSINESS_ROUTES


def _litellm_credential_token_from_scope(scope: Scope) -> str:
    headers = _headers_from_scope(scope)
    configured_header_name = _configured_litellm_key_header_name()
    if configured_header_name:
        token = _credential_token_from_header(_header(headers, configured_header_name), mode="bearer")
        if token:
            return token
    for header_name, mode in LITELLM_CREDENTIAL_HEADER_PRECEDENCE:
        token = _credential_token_from_header(_header(headers, header_name), mode=mode)
        if token:
            return token
    return ""


def _configured_litellm_key_header_name() -> str:
    try:
        from litellm.proxy.proxy_server import general_settings
    except Exception:
        return ""
    if not isinstance(general_settings, dict):
        return ""
    value = general_settings.get("litellm_key_header_name")
    return value.strip().lower() if isinstance(value, str) else ""


def _credential_token_from_header(value: str, *, mode: str) -> str:
    credential = value.strip()
    if not credential:
        return ""
    if mode == "raw":
        return credential
    if credential.startswith("Bearer "):
        return credential[7:].strip()
    if credential.startswith("bearer "):
        return credential[7:].strip()
    if credential.startswith("Basic "):
        return credential[6:].strip()
    if credential.startswith("AWS4-HMAC-SHA256"):
        match = re.search(r"Credential=Bearer\s+([^/\s,]+)", credential)
        if match:
            return match.group(1).strip()
        match = re.search(r"Credential=([^/\s,]+)", credential)
        return match.group(1).strip() if match else ""
    if mode == "bearer_or_raw":
        return credential
    return ""


def _business_forbidden_credential_decision(token: str) -> RouteDecision | None:
    forbidden_tokens = {
        secret
        for secret in (
            os.environ.get("LITELLM_MASTER_KEY", "").strip(),
            os.environ.get("YCAPI_API_TOKEN", "").strip(),
        )
        if secret
    }
    if token not in forbidden_tokens:
        return None
    return RouteDecision(
        allowed=False,
        code=BUSINESS_TOKEN_POLICY_CODE,
        message=(
            "Admin or upstream tokens are not allowed on the business API; "
            "use a governed LiteLLM virtual key."
        ),
        status_code=403,
    )


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


async def _litellm_key_disposition_from_db(token: str) -> RouteDecision | None:
    hashed_token = _hash_litellm_token_for_lookup(token)
    try:
        from litellm.proxy.proxy_server import prisma_client
    except Exception as exc:
        raise RuntimeError("LiteLLM Prisma client is not importable") from exc

    db = getattr(prisma_client, "db", None) if prisma_client is not None else None
    if db is None:
        raise RuntimeError("LiteLLM Prisma client is not initialized")

    active_key = await db.litellm_verificationtoken.find_unique(where={"token": hashed_token})
    if active_key is not None:
        if getattr(active_key, "blocked", None) is True:
            return RouteDecision(
                allowed=False,
                code=KEY_BLOCKED_POLICY_CODE,
                message="Key is blocked. Update via `/key/unblock` if you're an admin.",
                status_code=401,
            )
        return None

    deleted_key = await db.litellm_deletedverificationtoken.find_first(where={"token": hashed_token})
    if deleted_key is not None:
        return RouteDecision(
            allowed=False,
            code=KEY_REVOKED_POLICY_CODE,
            message="Authentication Error, Invalid proxy server token passed. Key was revoked.",
            status_code=401,
        )
    return None


def _hash_litellm_token_for_lookup(token: str) -> str:
    if not token.startswith("sk-"):
        return token
    return hashlib.sha256(token.encode()).hexdigest()


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


async def _read_request_body(receive: Receive, *, max_body_bytes: int | None = MAX_KEY_GENERATE_BODY_BYTES) -> bytes:
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
            raise KeyGovernanceError("request body must be bytes for AiManager request processing")

        total_size += len(chunk)
        if max_body_bytes is not None and total_size > max_body_bytes:
            raise KeyGovernanceError("request body is too large for AiManager request processing")
        chunks.append(chunk)

        if not bool(message.get("more_body", False)):
            break
    return b"".join(chunks)


def _parse_json_object_body(raw_body: bytes, *, purpose: str = "key creation") -> dict[str, Any]:
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KeyGovernanceError(f"JSON object body is required for AiManager {purpose}") from exc
    if not isinstance(payload, dict):
        raise KeyGovernanceError(f"JSON object body is required for AiManager {purpose}")
    return payload


def _normalized_key_generate_body(payload: dict[str, Any]) -> bytes:
    normalized = normalize_key_request(payload, shared_key=_is_shared_key_request(payload))
    return json.dumps(normalized, separators=(",", ":")).encode("utf-8")


def _stream_usage_enforced_body(raw_body: bytes) -> bytes:
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw_body
    if not isinstance(payload, dict) or payload.get("stream") is not True:
        return raw_body
    stream_options = payload.get("stream_options")
    normalized_stream_options = dict(stream_options) if isinstance(stream_options, dict) else {}
    if normalized_stream_options.get("include_usage") is True:
        return raw_body
    normalized = dict(payload)
    normalized_stream_options["include_usage"] = True
    normalized["stream_options"] = normalized_stream_options
    return json.dumps(normalized, separators=(",", ":")).encode("utf-8")


def _is_shared_key_request(payload: dict[str, Any]) -> bool:
    metadata = payload.get("metadata")
    return isinstance(metadata, dict) and metadata.get("shared_key") is True


def _business_work_context_error(payload: dict[str, Any]) -> str:
    metadata = payload.get("metadata")
    if not isinstance(metadata, Mapping):
        return "missing metadata work context object"

    user = _payload_text(payload, "user")
    context = _work_context_from_metadata(metadata)
    validation = validate_work_context(context, mode="preflight")
    errors = set(validation.errors)
    if not user:
        errors.add("user")
    end_user_principal = _text_from_value(metadata.get("end_user_principal"))
    if user and end_user_principal and user != end_user_principal:
        errors.add("user")

    if not errors:
        return ""
    return f"missing or invalid work context fields: {', '.join(sorted(errors))}"


def _work_context_from_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    context_fields = (
        "work_item_id",
        "employee_id",
        "department_id",
        "end_user_principal",
        "scenario_l1",
        "scenario_l2",
        "internal_or_external",
        "channel",
        "project_id",
        "customer_id",
        "sensitivity_level",
        "approval_required",
    )
    context = {field_name: metadata.get(field_name) for field_name in context_fields if field_name in metadata}
    workflow = metadata.get("workflow")
    if isinstance(workflow, Mapping):
        context["workflow"] = dict(workflow)
    brand_safety = metadata.get("brand_safety")
    if isinstance(brand_safety, Mapping):
        context["brand_safety"] = dict(brand_safety)
    return context


def _work_context_invalid_decision(method: str, path: str, message: str) -> RouteDecision:
    normalized_method = method.upper()
    normalized_path = _normalized_request_path(path)
    return RouteDecision(
        allowed=False,
        code=WORK_CONTEXT_POLICY_CODE,
        message=f"AiManager work context rejects {normalized_method} {normalized_path}: {message}",
        status_code=400,
    )


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


async def _send_request_body_error(send: Send, message: str, request_id: str) -> None:
    response = {
        "error": {
            "message": f"AiManager request processing rejects request body: {message}",
            "type": "invalid_request_error",
            "param": None,
            "code": REQUEST_BODY_POLICY_CODE,
        },
        "request_id": request_id,
    }
    body = json.dumps(response).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"x-litellm-call-id", request_id.encode("utf-8")),
        (b"x-aimanager-policy-code", REQUEST_BODY_POLICY_CODE.encode("ascii")),
    ]
    await send({"type": "http.response.start", "status": 400, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def _send_work_context_error(send: Send, message: str, request_id: str) -> None:
    response = {
        "error": {
            "message": f"AiManager work context rejects request: {message}",
            "type": "invalid_request_error",
            "param": None,
            "code": WORK_CONTEXT_POLICY_CODE,
        },
        "request_id": request_id,
    }
    body = json.dumps(response).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"x-litellm-call-id", request_id.encode("utf-8")),
        (b"x-aimanager-policy-code", WORK_CONTEXT_POLICY_CODE.encode("ascii")),
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
    event_stream_passthrough = False
    event_stream_buffer = b""

    async def wrapped_send(message: Message) -> None:
        nonlocal body_size, event_stream_buffer, event_stream_passthrough, passthrough
        nonlocal raw_response_headers, response_headers, status_code

        message_type = message.get("type")
        if message_type == "http.response.start":
            raw_status = message.get("status")
            status_code = raw_status if isinstance(raw_status, int) else None
            response_headers = _headers_from_message(message)
            raw_response_headers = list(message.get("headers") or [])
            passthrough = status_code is None or status_code < 400
            event_stream_passthrough = passthrough and _is_event_stream_response(response_headers)
            if passthrough:
                start_message = dict(message)
                if event_stream_passthrough:
                    start_message["headers"] = _headers_without_content_length(raw_response_headers)
                await send(start_message)
            return

        if message_type != "http.response.body":
            await send(message)
            return

        if passthrough:
            if event_stream_passthrough:
                chunk = message.get("body", b"")
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")
                if isinstance(chunk, bytes):
                    event_stream_buffer += chunk
                events, event_stream_buffer = _split_complete_sse_events(event_stream_buffer)
                body = b"".join(_normalize_sse_event(event, request_id=_stream_request_id(response_headers, request_id)) for event in events)
                if not bool(message.get("more_body", False)) and event_stream_buffer:
                    body += _normalize_sse_event(
                        event_stream_buffer,
                        request_id=_stream_request_id(response_headers, request_id),
                    )
                    event_stream_buffer = b""
                if body or not bool(message.get("more_body", False)):
                    output_message = dict(message)
                    output_message["body"] = body
                    await send(output_message)
                return
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


def _is_event_stream_response(headers: Mapping[str, str]) -> bool:
    return _header(headers, "content-type").lower().startswith("text/event-stream")


def _stream_request_id(headers: Mapping[str, str], fallback: str) -> str:
    return _header(headers, "x-litellm-call-id", "x-request-id", default=fallback)


def _headers_without_content_length(raw_headers: list[tuple[Any, Any]]) -> list[tuple[bytes, bytes]]:
    headers: list[tuple[bytes, bytes]] = []
    for key, value in raw_headers:
        key_bytes = key if isinstance(key, bytes) else str(key).encode("latin1", errors="replace")
        if key_bytes.lower() == b"content-length":
            continue
        value_bytes = value if isinstance(value, bytes) else str(value).encode("latin1", errors="replace")
        headers.append((key_bytes, value_bytes))
    return headers


def _split_complete_sse_events(buffer: bytes) -> tuple[list[bytes], bytes]:
    events: list[bytes] = []
    start = 0
    for match in SSE_EVENT_DELIMITER_PATTERN.finditer(buffer):
        events.append(buffer[start : match.end()])
        start = match.end()
    return events, buffer[start:]


def _normalize_sse_event(event: bytes, *, request_id: str) -> bytes:
    text = event.decode("utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    normalized = [
        _normalize_sse_data_line(line, request_id=request_id)
        if line.startswith("data:")
        else _redact_sensitive_text(line)
        for line in lines
    ]
    return "".join(normalized).encode("utf-8")


def _normalize_sse_data_line(line: str, *, request_id: str) -> str:
    line_ending = _line_ending(line)
    body = line[: -len(line_ending)] if line_ending else line
    prefix, raw_data = body.split(":", 1)
    leading_space = " " if raw_data.startswith(" ") else ""
    data = raw_data[1:] if leading_space else raw_data
    if not data or data == "[DONE]":
        return _redact_sensitive_text(line)
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return _redact_sensitive_text(line)
    if not isinstance(payload, dict) or not isinstance(payload.get("error"), dict):
        return _redact_sensitive_text(line)
    normalized_payload = _build_downstream_error_body(
        status_code=500,
        response_body=json.dumps(payload).encode("utf-8"),
        request_id=request_id,
    )
    return f"{prefix}:{leading_space}{json.dumps(normalized_payload, separators=(',', ':'))}{line_ending}"


def _line_ending(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    if line.endswith("\r"):
        return "\r"
    return ""


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

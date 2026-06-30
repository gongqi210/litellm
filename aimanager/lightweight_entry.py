from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from aimanager.work_context import (
    CLOSURE_WORKFLOW_FIELDS,
    PREFLIGHT_WORKFLOW_FIELDS,
    REQUIRED_BRAND_SAFETY_CHECKS,
    WorkContextMode,
    validate_work_context,
)


LightweightEntryStatus = Literal["PASS", "FAIL", "BLOCKED"]
DEFAULT_EMPLOYEE_KEY_ENV = "AIMANAGER_EMPLOYEE_VIRTUAL_KEY"
YCAPI_TOKEN_ENV = "YCAPI_API_TOKEN"
DEFAULT_BASE_URL = "http://localhost:4000"
DEFAULT_CHAT_MODEL = "gemini-2.5-flash"
ALLOWED_CHAT_MODELS = ("gemini-2.5-flash", "deepseek-chat")
MAX_TOKENS_DEFAULT = 512
MAX_TOKENS_LIMIT = 4096
REQUIRED_FINANCE_METADATA_FIELDS = ("cost_center_id", "currency", "pricing_version")
FORBIDDEN_FORM_FIELDS = frozenset(
    {
        "authorization",
        "api_key",
        "apikey",
        "bearer_token",
        "employee_key",
        "litellm_master_key",
        "master_key",
        "virtual_key",
        "ycapi_api_key",
        "ycapi_api_token",
        "ycapi_token",
    }
)
SECRET_VALUE_PATTERNS = (
    re.compile(r"\bBearer\s+[^\s]{8,}", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9._-]{12,}"),
)


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True)
class LightweightEntryResult:
    status: LightweightEntryStatus
    detail: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    work_context: dict[str, Any] = field(default_factory=dict)
    request: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LightweightEntrySubmissionResult:
    status: LightweightEntryStatus
    detail: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    entry: LightweightEntryResult | None = None
    checked_endpoint: str | None = None
    status_code: int | None = None
    assistant_text: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.entry is not None:
            payload["entry"] = self.entry.to_dict()
        return payload


Fetch = Callable[[str, str, dict[str, str], bytes | None], HttpResponse]


def prepare_lightweight_entry(
    form_payload: Mapping[str, Any],
    *,
    mode: WorkContextMode = "preflight",
) -> LightweightEntryResult:
    errors = _forbidden_form_field_errors(form_payload)
    errors.extend(_forbidden_form_value_errors(form_payload))
    prompt = form_payload.get("prompt")
    if not _is_non_empty_text(prompt):
        errors.append("prompt")
    for field_name in REQUIRED_FINANCE_METADATA_FIELDS:
        if not _is_non_empty_text(form_payload.get(field_name)):
            errors.append(field_name)

    model = _model_from_form(form_payload.get("model"))
    if model not in ALLOWED_CHAT_MODELS:
        errors.append("model")

    max_tokens = _max_tokens_from_form(form_payload.get("max_tokens"))
    if max_tokens is None:
        errors.append("max_tokens")

    work_context_validation = validate_work_context(_work_context_from_form(form_payload), mode=mode)
    if work_context_validation.status != "PASS":
        errors.extend(work_context_validation.errors)

    if errors:
        work_context = work_context_validation.to_dict()
        if any(error.startswith("forbidden_secret_") for error in errors):
            work_context["normalized_context"] = {}
        return LightweightEntryResult(
            status="FAIL",
            detail=f"lightweight entry failed validation with {len(set(errors))} error(s)",
            errors=sorted(set(errors)),
            work_context=work_context,
        )

    normalized_context = work_context_validation.normalized_context
    return LightweightEntryResult(
        status="PASS",
        detail="lightweight entry is ready for AiManager governed chat submission",
        work_context=work_context_validation.to_dict(),
        request={
            "method": "POST",
            "path": "/v1/chat/completions",
            "body": {
                "model": model,
                "messages": [{"role": "user", "content": str(prompt).strip()}],
                "max_tokens": max_tokens,
                "user": _original_text(form_payload, "end_user_principal"),
                "metadata": _request_metadata(form_payload, normalized_context),
            },
        },
    )


def submit_lightweight_entry(
    form_payload: Mapping[str, Any],
    *,
    base_url: str = DEFAULT_BASE_URL,
    employee_key: str,
    employee_key_env_name: str = DEFAULT_EMPLOYEE_KEY_ENV,
    mode: WorkContextMode = "preflight",
    fetch: Fetch | None = None,
    timeout_seconds: float = 10,
) -> LightweightEntrySubmissionResult:
    if employee_key_env_name == YCAPI_TOKEN_ENV:
        return LightweightEntrySubmissionResult(
            status="FAIL",
            detail=f"employee key env cannot be {YCAPI_TOKEN_ENV}; use a LiteLLM virtual key env",
        )

    entry = prepare_lightweight_entry(form_payload, mode=mode)
    if entry.status != "PASS":
        return LightweightEntrySubmissionResult(
            status=entry.status,
            detail=entry.detail,
            errors=entry.errors,
            warnings=entry.warnings,
            entry=entry,
        )
    if not employee_key.strip():
        return LightweightEntrySubmissionResult(
            status="BLOCKED",
            detail=f"missing {employee_key_env_name}; cannot submit lightweight entry through AiManager",
            entry=entry,
        )

    request = entry.request or {}
    body = request.get("body")
    if not isinstance(body, dict):
        return LightweightEntrySubmissionResult(
            status="FAIL",
            detail="lightweight entry request body is missing",
            errors=["request.body"],
            entry=entry,
        )

    fetcher = fetch or _fetch_with_urllib(timeout_seconds=timeout_seconds)
    endpoint = "/v1/chat/completions"
    try:
        response = fetcher(
            "POST",
            _join_url(base_url, endpoint),
            {
                "Authorization": f"Bearer {employee_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "x-request-id": f"lightweight-{body['metadata']['work_item_id']}",
            },
            json.dumps(body).encode("utf-8"),
        )
    except (OSError, ValueError, http.client.HTTPException):
        return LightweightEntrySubmissionResult(
            status="FAIL",
            detail="lightweight entry request failed before receiving an HTTP response",
            entry=entry,
            checked_endpoint=endpoint,
        )

    if response.status_code != 200:
        return LightweightEntrySubmissionResult(
            status="FAIL",
            detail=f"business gateway returned HTTP {response.status_code}",
            entry=entry,
            checked_endpoint=endpoint,
            status_code=response.status_code,
        )

    assistant_text, shape_error = _chat_message_content_or_error(response.body)
    if assistant_text is None:
        return LightweightEntrySubmissionResult(
            status="FAIL",
            detail=f"business gateway response {shape_error}",
            entry=entry,
            checked_endpoint=endpoint,
            status_code=response.status_code,
        )

    return LightweightEntrySubmissionResult(
        status="PASS",
        detail="lightweight entry request succeeded through AiManager business gateway",
        entry=entry,
        checked_endpoint=endpoint,
        status_code=response.status_code,
        assistant_text=assistant_text,
    )


def _work_context_from_form(form_payload: Mapping[str, Any]) -> dict[str, Any]:
    context = {
        field_name: form_payload.get(field_name)
        for field_name in (
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
        if field_name in form_payload
    }
    workflow = _workflow_from_form(form_payload)
    if workflow:
        context["workflow"] = workflow
    brand_safety = _brand_safety_from_form(form_payload)
    if brand_safety:
        context["brand_safety"] = brand_safety
    return context


def _workflow_from_form(form_payload: Mapping[str, Any]) -> dict[str, Any]:
    nested = form_payload.get("workflow")
    nested_fields = dict(nested) if isinstance(nested, Mapping) else {}
    flat_fields = {
        field_name: form_payload[field_name]
        for field_name in (*PREFLIGHT_WORKFLOW_FIELDS, *CLOSURE_WORKFLOW_FIELDS)
        if field_name in form_payload
    }
    return {**nested_fields, **flat_fields}


def _brand_safety_from_form(form_payload: Mapping[str, Any]) -> dict[str, Any]:
    nested = form_payload.get("brand_safety")
    nested_fields = dict(nested) if isinstance(nested, Mapping) else {}
    flat_fields = {
        field_name: form_payload[field_name]
        for field_name in REQUIRED_BRAND_SAFETY_CHECKS
        if field_name in form_payload
    }
    policy_ref = form_payload.get("brand_safety_policy_ref")
    policy_field = {"policy_ref": policy_ref} if policy_ref is not None else {}
    return {**nested_fields, **policy_field, **flat_fields}


def _request_metadata(form_payload: Mapping[str, Any], normalized_context: Mapping[str, Any]) -> dict[str, Any]:
    optional_fields = {
        field_name: _original_text(form_payload, field_name)
        for field_name in ("project_id", "customer_id")
        if _is_non_empty_text(form_payload.get(field_name))
    }
    return {
        "entry_type": "lightweight_non_sdk",
        "work_item_id": _original_text(form_payload, "work_item_id"),
        "employee_id": _original_text(form_payload, "employee_id"),
        "department_id": _original_text(form_payload, "department_id"),
        "end_user_principal": _original_text(form_payload, "end_user_principal"),
        "cost_center_id": _original_text(form_payload, "cost_center_id"),
        "currency": _original_text(form_payload, "currency"),
        "pricing_version": _original_text(form_payload, "pricing_version"),
        "scenario_l1": normalized_context["scenario_l1"],
        "scenario_l2": normalized_context["scenario_l2"],
        "internal_or_external": normalized_context["internal_or_external"],
        "channel": normalized_context["channel"],
        "sensitivity_level": normalized_context["sensitivity_level"],
        "approval_required": normalized_context["approval_required"],
        "requires_external_approval": normalized_context["requires_external_approval"],
        "workflow_mode": normalized_context["workflow_mode"],
        **optional_fields,
    }


def _forbidden_form_field_errors(form_payload: Mapping[str, Any]) -> list[str]:
    return [
        f"forbidden_secret_field:{field_name}"
        for field_name in sorted({field_name.lower() for field_name in form_payload} & FORBIDDEN_FORM_FIELDS)
    ]


def _forbidden_form_value_errors(value: Any, *, path: str = "") -> list[str]:
    if isinstance(value, str):
        if any(pattern.search(value) for pattern in SECRET_VALUE_PATTERNS):
            return [f"forbidden_secret_value:{path}" if path else "forbidden_secret_value"]
        return []
    if isinstance(value, Mapping):
        return [
            error
            for field_name, field_value in value.items()
            for error in _forbidden_form_value_errors(
                field_value,
                path=f"{path}.{field_name}" if path else str(field_name),
            )
        ]
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray)):
        return [
            error
            for index, item in enumerate(value)
            for error in _forbidden_form_value_errors(item, path=f"{path}[{index}]" if path else f"[{index}]")
        ]
    return []


def _original_text(form_payload: Mapping[str, Any], field_name: str) -> str:
    value = form_payload.get(field_name)
    return value.strip() if isinstance(value, str) else ""


def _model_from_form(value: Any) -> str:
    if value is None:
        return DEFAULT_CHAT_MODEL
    if not isinstance(value, str):
        return ""
    return value.strip()


def _max_tokens_from_form(value: Any) -> int | None:
    if value is None:
        return MAX_TOKENS_DEFAULT
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    if value < 1 or value > MAX_TOKENS_LIMIT:
        return None
    return value


def _is_non_empty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _fetch_with_urllib(*, timeout_seconds: float) -> Fetch:
    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        request = urllib.request.Request(url=url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                return HttpResponse(
                    status_code=response.status,
                    headers=dict(response.headers.items()),
                    body=response.read(),
                )
        except urllib.error.HTTPError as exc:
            return HttpResponse(
                status_code=exc.code,
                headers=dict(exc.headers.items()),
                body=exc.read(),
            )

    return fetch


def _chat_shape_error(body: bytes) -> str:
    return _chat_message_content_or_error(body)[1]


def _chat_message_content(body: bytes) -> str | None:
    return _chat_message_content_or_error(body)[0]


def _chat_message_content_or_error(body: bytes) -> tuple[str | None, str]:
    try:
        payload: Any = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "returned non-JSON body"
    if not isinstance(payload, dict):
        return None, "returned non-object body"
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None, "missing choices message content"
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return None, "missing choices message content"
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return None, "missing choices message content"
    return content, ""

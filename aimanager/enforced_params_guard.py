from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger

from aimanager.audit import build_audit_event
from aimanager.governance import _is_missing
from aimanager.policy import RouteDecision, build_policy_error_body


LOGGER = logging.getLogger("aimanager.policy")
ERROR_CODE = "aimanager_enforced_params_missing"
ACTOR = "aimanager-enforced-params-guard"

AuditSink = Callable[[dict[str, Any]], None]


class AiManagerEnforcedParamsGuard(CustomLogger):
    """LiteLLM OSS runtime guard for shared-key work-context metadata."""

    guard_name = "aimanager_enforced_params_guard"

    def __init__(self, audit_sink: AuditSink | None = None) -> None:
        super().__init__()
        self.audit_sink = audit_sink or _log_audit_event

    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: dict[str, Any],
        call_type: str,
    ) -> dict[str, Any]:
        enforced_params = _enforced_params_from_key(user_api_key_dict)
        if not enforced_params:
            return data

        missing = missing_enforced_params(data, enforced_params)
        if not missing:
            return data

        request_id = _request_id(data)
        self._emit_audit_event(
            user_api_key_dict=user_api_key_dict,
            request_id=request_id,
            missing=missing,
            call_type=call_type,
        )
        raise HTTPException(
            status_code=400,
            detail=build_enforced_params_error_body(missing=missing, request_id=request_id),
        )

    def _emit_audit_event(
        self,
        *,
        user_api_key_dict: Any,
        request_id: str,
        missing: Sequence[str],
        call_type: str,
    ) -> None:
        try:
            event = build_audit_event(
                "enforced_params_blocked",
                actor=ACTOR,
                subject_key_alias=_key_attr(user_api_key_dict, "key_alias"),
                team_id=_key_attr(user_api_key_dict, "team_id"),
                department_id=_key_attr(user_api_key_dict, "department_id"),
                project_id=_key_attr(user_api_key_dict, "project_id"),
                cost_center_id=_key_attr(user_api_key_dict, "cost_center_id"),
                reason=ERROR_CODE,
                request_id=request_id,
                metadata={
                    "missing_params": list(missing),
                    "call_type": str(call_type or ""),
                },
            )
            self.audit_sink(event)
        except Exception:
            LOGGER.exception("failed_to_emit_aimanager_audit_event request_id=%s", request_id)


def missing_enforced_params(data: Mapping[str, Any], enforced_params: Sequence[str]) -> list[str]:
    missing: list[str] = []
    for path in enforced_params:
        normalized_path = str(path or "").strip()
        if not normalized_path:
            continue
        if _is_missing(_get_path(data, normalized_path)):
            missing.append(normalized_path)
    return missing


def build_enforced_params_error_body(*, missing: Sequence[str], request_id: str) -> dict[str, object]:
    primary = list(missing)[0] if missing else "unknown"
    decision = RouteDecision(
        allowed=False,
        code=ERROR_CODE,
        message=(
            "AiManager shared key request is missing required work-context field(s): "
            + ", ".join(str(item) for item in missing)
        ),
        status_code=400,
    )
    body = build_policy_error_body(decision, request_id)
    error = body.get("error")
    if isinstance(error, dict):
        error["param"] = primary
    return body


def _enforced_params_from_key(user_api_key_dict: Any) -> list[str]:
    metadata = _key_metadata(user_api_key_dict)
    raw = metadata.get("enforced_params")
    if isinstance(raw, str):
        return [item.strip() for item in raw.split(",") if item.strip()]
    if isinstance(raw, Sequence) and not isinstance(raw, (bytes, bytearray, str)):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []


def _key_metadata(user_api_key_dict: Any) -> dict[str, Any]:
    raw = _key_attr(user_api_key_dict, "metadata")
    if isinstance(raw, Mapping):
        return dict(raw)
    return {}


def _key_attr(user_api_key_dict: Any, name: str) -> Any:
    if user_api_key_dict is None:
        return ""
    if isinstance(user_api_key_dict, Mapping):
        return user_api_key_dict.get(name, "")
    return getattr(user_api_key_dict, name, "")


def _get_path(data: Mapping[str, Any], path: str) -> Any:
    current: Any = data
    for part in path.split("."):
        if isinstance(current, Mapping):
            current = current.get(part)
        else:
            return None
    return current


def _request_id(data: Mapping[str, Any]) -> str:
    metadata = data.get("metadata")
    if isinstance(metadata, Mapping):
        request_id = metadata.get("request_id") or metadata.get("litellm_call_id")
        if isinstance(request_id, str) and request_id.strip():
            return request_id.strip()
    request_id = data.get("request_id") or data.get("litellm_call_id")
    if isinstance(request_id, str) and request_id.strip():
        return request_id.strip()
    return "unknown"


def _log_audit_event(event: dict[str, Any]) -> None:
    LOGGER.info(
        "aimanager_audit_event=%s",
        json.dumps(event, sort_keys=True, separators=(",", ":")),
    )

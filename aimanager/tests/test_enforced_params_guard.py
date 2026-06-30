from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from aimanager.enforced_params_guard import (
    AiManagerEnforcedParamsGuard,
    missing_enforced_params,
)
from aimanager.governance import SHARED_KEY_ENFORCED_PARAMS


_MISSING = object()


def test_missing_end_user_principal_is_blocked_and_audited() -> None:
    audit_events: list[dict[str, Any]] = []
    guard = AiManagerEnforcedParamsGuard(audit_sink=audit_events.append)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            guard.async_pre_call_hook(
                user_api_key_dict=_key(),
                cache=None,
                data=_request(metadata={"scenario_l1": "sales", "request_id": "req-shared-1"}),
                call_type="completion",
            )
        )

    assert exc_info.value.status_code == 400
    body = exc_info.value.detail
    assert body["request_id"] == "req-shared-1"
    assert body["error"]["code"] == "aimanager_enforced_params_missing"
    assert body["error"]["param"] == "metadata.end_user_principal"
    assert "metadata.end_user_principal" in body["error"]["message"]
    assert len(audit_events) == 1
    event = audit_events[0]
    assert event["event_type"] == "enforced_params_blocked"
    assert event["reason"] == "aimanager_enforced_params_missing"
    assert event["actor"] == "aimanager-enforced-params-guard"
    assert event["subject_key_alias"] == "market-shared-key"
    assert event["team_id"] == "team_market"
    assert event["department_id"] == "dept_market"
    assert event["project_id"] == "proj_launch"
    assert event["cost_center_id"] == "cc_growth"
    assert event["request_id"] == "req-shared-1"
    assert event["metadata"]["missing_params"] == ["metadata.end_user_principal"]
    assert event["metadata"]["call_type"] == "completion"


def test_missing_top_level_user_is_blocked() -> None:
    guard = AiManagerEnforcedParamsGuard()

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            guard.async_pre_call_hook(
                user_api_key_dict=_key(),
                cache=None,
                data=_request(user=_MISSING),
                call_type="completion",
            )
        )

    assert exc_info.value.detail["error"]["param"] == "user"


def test_missing_nested_scenario_is_blocked_when_metadata_is_absent() -> None:
    guard = AiManagerEnforcedParamsGuard()

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            guard.async_pre_call_hook(
                user_api_key_dict=_key(),
                cache=None,
                data=_request(metadata=None),
                call_type="completion",
            )
        )

    assert exc_info.value.detail["error"]["param"] == "metadata.scenario_l1"


def test_blank_and_empty_values_are_missing() -> None:
    assert missing_enforced_params(
        {
            "user": " ",
            "metadata": {
                "scenario_l1": [],
                "end_user_principal": None,
            },
        },
        SHARED_KEY_ENFORCED_PARAMS,
    ) == [
        "user",
        "metadata.scenario_l1",
        "metadata.end_user_principal",
    ]


def test_complete_shared_key_request_passes_unchanged() -> None:
    guard = AiManagerEnforcedParamsGuard()
    request = _request()

    returned = asyncio.run(
        guard.async_pre_call_hook(
            user_api_key_dict=_key(),
            cache=None,
            data=request,
            call_type="completion",
        )
    )

    assert returned is request


def test_key_without_enforced_params_passes_unchanged() -> None:
    guard = AiManagerEnforcedParamsGuard()
    request = {"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}]}

    returned = asyncio.run(
        guard.async_pre_call_hook(
            user_api_key_dict=SimpleNamespace(metadata={}, key_alias="personal-key"),
            cache=None,
            data=request,
            call_type="completion",
        )
    )

    assert returned is request


def test_error_body_does_not_echo_secret_like_request_values() -> None:
    guard = AiManagerEnforcedParamsGuard()
    request = _request(
        metadata={
            "scenario_l1": "sales",
            "request_id": "req-secret",
            "internal_note": "Bearer sk-live-secret-value",
        }
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            guard.async_pre_call_hook(
                user_api_key_dict=_key(metadata={"enforced_params": ["metadata.end_user_principal"]}),
                cache=None,
                data=request,
                call_type="completion",
            )
        )

    serialized = json.dumps(exc_info.value.detail)
    assert "sk-live-secret-value" not in serialized
    assert "Bearer" not in serialized


def _key(metadata: dict[str, Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        metadata={"enforced_params": list(SHARED_KEY_ENFORCED_PARAMS)} if metadata is None else metadata,
        key_alias="market-shared-key",
        team_id="team_market",
        department_id="dept_market",
        project_id="proj_launch",
        cost_center_id="cc_growth",
    )


def _request(
    *,
    user: object = "employee-123",
    metadata: dict[str, Any] | None = {
        "scenario_l1": "sales",
        "end_user_principal": "employee-123",
        "request_id": "req-ok",
    },
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": "draft a reply"}],
    }
    if user is not _MISSING:
        request["user"] = user
    if metadata is not None:
        request["metadata"] = metadata
    return request

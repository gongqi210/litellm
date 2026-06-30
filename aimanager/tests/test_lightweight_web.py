from __future__ import annotations

import asyncio
from typing import Any

from aimanager.lightweight_entry import LightweightEntrySubmissionResult, prepare_lightweight_entry
from aimanager.lightweight_web import (
    INTERNAL_TRIAL_SCENARIOS,
    MAX_FORM_BODY_BYTES,
    LightweightTrialDefaults,
    build_trial_form,
    create_app,
)


def test_build_trial_form_merges_defaults_and_forces_internal_web_context() -> None:
    result = build_trial_form(
        {
            "prompt": "Summarize the meeting notes",
            "scenario_l1": "collaboration",
            "scenario_l2": "summary",
            "channel": "wechat",
            "internal_or_external": "external",
            "approval_required": True,
            "cost_center_id": "OVERRIDE",
        },
        defaults=_defaults(),
        work_item_id="trial-001",
    )

    assert result.status == "PASS"
    assert result.errors == ()
    assert result.form["work_item_id"] == "trial-001"
    assert result.form["channel"] == "lightweight_web"
    assert result.form["internal_or_external"] == "internal"
    assert result.form["approval_required"] is False
    assert result.form["cost_center_id"] == "CC-MKT"
    assert result.form["currency"] == "CNY"
    assert result.form["pricing_version"] == "2026-06"
    assert prepare_lightweight_entry(result.form).status == "PASS"


def test_build_trial_form_rejects_non_internal_fast_trial_scenarios() -> None:
    result = build_trial_form(
        {
            "prompt": "Write a customer article",
            "scenario_l1": "marketing",
            "scenario_l2": "wechat_article",
        },
        defaults=_defaults(),
        work_item_id="trial-002",
    )

    assert result.status == "FAIL"
    assert result.errors == ("scenario",)
    assert result.form == {}


def test_all_offered_internal_scenarios_build_governed_requests() -> None:
    for scenario_l1, scenario_l2_values in INTERNAL_TRIAL_SCENARIOS.items():
        for scenario_l2 in scenario_l2_values:
            result = build_trial_form(
                {
                    "prompt": "Summarize this internal note",
                    "scenario_l1": scenario_l1,
                    "scenario_l2": scenario_l2,
                },
                defaults=_defaults(),
                work_item_id=f"trial-{scenario_l1}-{scenario_l2}",
            )

            assert result.status == "PASS", f"{scenario_l1}/{scenario_l2}"
            assert prepare_lightweight_entry(result.form).status == "PASS", f"{scenario_l1}/{scenario_l2}"


def test_lightweight_web_homepage_renders_form_without_keys_or_ycapi_terms() -> None:
    app = create_app(
        defaults=_defaults(),
        employee_key_provider=lambda: ("AIMANAGER_EMPLOYEE_VIRTUAL_KEY", "employee-trial-key"),
    )

    response = asyncio.run(_call_app(app, "GET", "/"))

    assert response["status"] == 200
    text = response["body"].decode("utf-8")
    assert 'data-verify-unit="aimanager-lightweight-web"' in text
    assert "employee-trial-key" not in text
    assert "YCAPI_API_TOKEN" not in text
    assert "Authorization" not in text
    assert "virtual_key" not in text


def test_lightweight_web_entry_submits_with_server_side_key_and_escapes_answer() -> None:
    calls: list[dict[str, Any]] = []

    def submitter(**kwargs: Any) -> LightweightEntrySubmissionResult:
        calls.append(kwargs)
        return LightweightEntrySubmissionResult(
            status="PASS",
            detail="lightweight entry request succeeded through AiManager business gateway",
            checked_endpoint="/v1/chat/completions",
            status_code=200,
            assistant_text="<script>alert(1)</script>",
        )

    app = create_app(
        defaults=_defaults(),
        employee_key_provider=lambda: ("AIMANAGER_EMPLOYEE_VIRTUAL_KEY", "employee-trial-key"),
        submitter=submitter,
        business_base_url="http://localhost:4000",
    )

    response = asyncio.run(
        _call_app(
            app,
            "POST",
            "/entry",
            form={
                "prompt": "Summarize this internal note",
                "scenario_l1": "collaboration",
                "scenario_l2": "summary",
            },
        )
    )

    assert response["status"] == 200
    text = response["body"].decode("utf-8")
    assert len(calls) == 1
    assert calls[0]["employee_key"] == "employee-trial-key"
    assert calls[0]["employee_key_env_name"] == "AIMANAGER_EMPLOYEE_VIRTUAL_KEY"
    assert calls[0]["base_url"] == "http://localhost:4000"
    assert calls[0]["form_payload"]["channel"] == "lightweight_web"
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text
    assert "<script>alert(1)</script>" not in text
    assert "employee-trial-key" not in text
    assert "Authorization" not in text


def test_lightweight_web_entry_blocks_missing_employee_key_without_submit() -> None:
    calls: list[dict[str, Any]] = []

    app = create_app(
        defaults=_defaults(),
        employee_key_provider=lambda: ("AIMANAGER_EMPLOYEE_VIRTUAL_KEY", " "),
        submitter=lambda **kwargs: calls.append(kwargs) or _submission_pass(),
    )

    response = asyncio.run(
        _call_app(
            app,
            "POST",
            "/entry",
            form={
                "prompt": "Summarize this internal note",
                "scenario_l1": "collaboration",
                "scenario_l2": "summary",
            },
        )
    )

    assert response["status"] == 200
    assert "BLOCKED" in response["body"].decode("utf-8")
    assert calls == []


def test_lightweight_web_entry_rejects_secret_prompt_without_echo_or_submit() -> None:
    calls: list[dict[str, Any]] = []
    secret_prompt = "Summarize this Bearer secret-token-value"
    app = create_app(
        defaults=_defaults(),
        employee_key_provider=lambda: ("AIMANAGER_EMPLOYEE_VIRTUAL_KEY", "employee-trial-key"),
        submitter=lambda **kwargs: calls.append(kwargs) or _submission_pass(),
    )

    response = asyncio.run(
        _call_app(
            app,
            "POST",
            "/entry",
            form={
                "prompt": secret_prompt,
                "scenario_l1": "collaboration",
                "scenario_l2": "summary",
            },
        )
    )

    text = response["body"].decode("utf-8")
    assert response["status"] == 200
    assert "FAIL" in text
    assert "secret-token-value" not in text
    assert calls == []


def test_lightweight_web_entry_rejects_ycapi_token_key_without_submit() -> None:
    for key_env, key_value, ycapi_value in (
        ("YCAPI_API_TOKEN", "employee-trial-key", "ycapi-token"),
        ("AIMANAGER_EMPLOYEE_VIRTUAL_KEY", "ycapi-token", "ycapi-token"),
    ):
        calls: list[dict[str, Any]] = []
        app = create_app(
            defaults=_defaults(),
            employee_key_provider=lambda key_env=key_env, key_value=key_value: (key_env, key_value),
            ycapi_token_provider=lambda ycapi_value=ycapi_value: ycapi_value,
            submitter=lambda **kwargs: calls.append(kwargs) or _submission_pass(),
        )

        response = asyncio.run(
            _call_app(
                app,
                "POST",
                "/entry",
                form={
                    "prompt": "Summarize this internal note",
                    "scenario_l1": "collaboration",
                    "scenario_l2": "summary",
                },
            )
        )

        text = response["body"].decode("utf-8")
        assert response["status"] == 200
        assert "FAIL" in text
        assert "ycapi-token" not in text
        assert calls == []


def test_lightweight_web_entry_rejects_oversized_body_without_submit() -> None:
    calls: list[dict[str, Any]] = []
    app = create_app(
        defaults=_defaults(),
        employee_key_provider=lambda: ("AIMANAGER_EMPLOYEE_VIRTUAL_KEY", "employee-trial-key"),
        submitter=lambda **kwargs: calls.append(kwargs) or _submission_pass(),
    )

    response = asyncio.run(
        _call_app(
            app,
            "POST",
            "/entry",
            raw_body=b"prompt=" + (b"x" * (MAX_FORM_BODY_BYTES + 1)),
        )
    )

    text = response["body"].decode("utf-8")
    assert response["status"] == 413
    assert "FAIL" in text
    assert "request_body_too_large" in text
    assert calls == []


async def _call_app(
    app: Any,
    method: str,
    path: str,
    *,
    form: dict[str, str] | None = None,
    raw_body: bytes | None = None,
) -> dict[str, Any]:
    from urllib.parse import urlencode

    body = raw_body if raw_body is not None else urlencode(form or {}).encode("utf-8")
    messages: list[dict[str, Any]] = []
    headers = [(b"content-type", b"application/x-www-form-urlencoded")] if body else []
    scope = {"type": "http", "method": method, "path": path, "headers": headers}
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(scope, receive, send)
    return {
        "status": messages[0]["status"],
        "headers": dict(messages[0].get("headers") or []),
        "body": b"".join(message.get("body", b"") for message in messages[1:]),
    }


def _defaults() -> LightweightTrialDefaults:
    return LightweightTrialDefaults(
        employee_id="E-1001",
        department_id="D-MKT",
        end_user_principal="user-1001",
        project_id="P-INTERNAL",
        cost_center_id="CC-MKT",
        currency="CNY",
        pricing_version="2026-06",
    )


def _submission_pass() -> LightweightEntrySubmissionResult:
    return LightweightEntrySubmissionResult(
        status="PASS",
        detail="lightweight entry request succeeded through AiManager business gateway",
        checked_endpoint="/v1/chat/completions",
        status_code=200,
        assistant_text="draft",
    )

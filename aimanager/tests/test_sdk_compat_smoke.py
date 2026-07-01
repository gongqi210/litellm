from __future__ import annotations

import json

from aimanager.scripts.smoke_sdk_compat import HttpResponse, main, run_sdk_compat_smoke


def test_sdk_compat_smoke_blocks_without_employee_key() -> None:
    calls: list[str] = []

    result = run_sdk_compat_smoke(
        base_url="http://localhost:4000",
        employee_key="",
        employee_key_env_name="AIMANAGER_EMPLOYEE_VIRTUAL_KEY",
        request_marker="sdk-smoke-123",
        fetch=lambda method, url, headers, body: calls.append(url) or _json_response({"ok": True}),
    )

    assert result.status == "BLOCKED"
    assert "AIMANAGER_EMPLOYEE_VIRTUAL_KEY" in result.detail
    assert calls == []


def test_sdk_compat_smoke_rejects_ycapi_token_env_name_without_fetch() -> None:
    calls: list[str] = []

    result = run_sdk_compat_smoke(
        base_url="http://localhost:4000",
        employee_key="ycapi-token-secret",
        employee_key_env_name="YCAPI_API_TOKEN",
        request_marker="sdk-smoke-123",
        fetch=lambda method, url, headers, body: calls.append(url) or _json_response({"ok": True}),
    )

    assert result.status == "FAIL"
    assert result.detail == "employee key env cannot be YCAPI_API_TOKEN; use a LiteLLM virtual key env"
    assert calls == []
    assert "ycapi-token-secret" not in result.detail


def test_sdk_compat_smoke_calls_models_chat_and_image_with_work_metadata() -> None:
    employee_key = "evk-secret"
    calls: list[tuple[str, str, dict[str, str], dict[str, object] | None]] = []

    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        payload = json.loads(body.decode("utf-8")) if body else None
        calls.append((method, url, headers, payload))
        assert headers["Authorization"] == f"Bearer {employee_key}"
        assert "YCAPI_API_TOKEN" not in headers
        if url.endswith("/v1/models"):
            assert method == "GET"
            assert payload is None
            return _models_response(["gemini-2.5-flash", "deepseek-chat", "ycapi-image-1"])
        if url.endswith("/v1/chat/completions"):
            assert method == "POST"
            assert payload is not None
            assert payload["model"] in {"gemini-2.5-flash", "deepseek-chat"}
            assert payload["user"] == "employee-smoke-001"
            metadata = payload["metadata"]  # type: ignore[index]
            assert metadata["scenario_l1"] == "engineering"  # type: ignore[index]
            assert metadata["scenario_l2"] == "code_assist"  # type: ignore[index]
            assert metadata["work_item_id"] == "sdk-smoke-123"  # type: ignore[index]
            assert metadata["employee_id"] == "employee-smoke-001"  # type: ignore[index]
            assert metadata["department_id"] == "dept_smoke"  # type: ignore[index]
            assert metadata["project_id"] == "proj_aimanager_runtime_smoke"  # type: ignore[index]
            assert metadata["cost_center_id"] == "cc_smoke"  # type: ignore[index]
            assert metadata["end_user_principal"] == "employee-smoke-001"  # type: ignore[index]
            assert metadata["internal_or_external"] == "internal"  # type: ignore[index]
            assert metadata["channel"] == "sdk"  # type: ignore[index]
            assert metadata["sensitivity_level"] == "internal"  # type: ignore[index]
            assert metadata["approval_required"] is False  # type: ignore[index]
            assert metadata["aimanager_smoke_id"] == "sdk-smoke-123"  # type: ignore[index]
            messages = payload["messages"]  # type: ignore[index]
            first_content = messages[0]["content"]  # type: ignore[index]
            if isinstance(first_content, list):
                assert payload["model"] == "gemini-2.5-flash"
                assert any(
                    part.get("type") == "image_url"
                    and part.get("image_url", {}).get("url", "").startswith("data:image/png;base64,")
                    for part in first_content
                    if isinstance(part, dict)
                )
                assert metadata["image_count"] == 1  # type: ignore[index]
            else:
                assert metadata["image_count"] == 0  # type: ignore[index]
            return _chat_response(str(payload["model"]))
        if url.endswith("/v1/images/generations"):
            assert method == "POST"
            assert payload is not None
            assert payload["model"] == "ycapi-image-1"
            assert payload["response_format"] == "b64_json"
            assert payload["user"] == "employee-smoke-001"
            metadata = payload["metadata"]  # type: ignore[index]
            assert metadata["scenario_l2"] == "code_assist"  # type: ignore[index]
            assert metadata["work_item_id"] == "sdk-smoke-123"  # type: ignore[index]
            assert metadata["employee_id"] == "employee-smoke-001"  # type: ignore[index]
            assert metadata["channel"] == "sdk"  # type: ignore[index]
            assert metadata["image_count"] == 1  # type: ignore[index]
            return _json_response({"created": 1, "data": [{"b64_json": "aW1hZ2U="}]})
        raise AssertionError(f"unexpected URL {url}")

    result = run_sdk_compat_smoke(
        base_url="http://localhost:4000",
        employee_key=employee_key,
        employee_key_env_name="AIMANAGER_EMPLOYEE_VIRTUAL_KEY",
        request_marker="sdk-smoke-123",
        fetch=fetch,
    )

    assert result.status == "PASS"
    assert result.checked_endpoints == (
        "models",
        "chat:gemini-2.5-flash",
        "chat:deepseek-chat",
        "image",
        "vision:gemini-2.5-flash",
    )
    assert len(calls) == 5
    assert "evk-secret" not in result.detail


def test_sdk_compat_smoke_fails_when_expected_model_is_missing() -> None:
    calls: list[str] = []

    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        calls.append(url)
        return _models_response(["gemini-2.5-flash", "ycapi-image-1"])

    result = run_sdk_compat_smoke(
        base_url="http://localhost:4000",
        employee_key="evk-secret",
        employee_key_env_name="AIMANAGER_EMPLOYEE_VIRTUAL_KEY",
        request_marker="sdk-smoke-123",
        fetch=fetch,
    )

    assert result.status == "FAIL"
    assert "missing model(s): deepseek-chat" in result.detail
    assert len(calls) == 1
    assert "evk-secret" not in result.detail


def test_sdk_compat_smoke_masks_upstream_error_body() -> None:
    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        if url.endswith("/v1/models"):
            return _models_response(["gemini-2.5-flash", "deepseek-chat", "ycapi-image-1"])
        return HttpResponse(
            status_code=500,
            headers={"Content-Type": "text/plain"},
            body=b"failed with Bearer evk-secret and ycapi-token-secret",
        )

    result = run_sdk_compat_smoke(
        base_url="http://localhost:4000",
        employee_key="evk-secret",
        employee_key_env_name="AIMANAGER_EMPLOYEE_VIRTUAL_KEY",
        request_marker="sdk-smoke-123",
        fetch=fetch,
    )

    assert result.status == "FAIL"
    assert result.detail == "chat gemini-2.5-flash request returned HTTP 500"
    assert "evk-secret" not in result.detail
    assert "ycapi-token-secret" not in result.detail


def test_sdk_compat_smoke_masks_transport_exception_message() -> None:
    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        raise OSError("failed http://localhost:4000 with Bearer evk-secret and ycapi-token-secret")

    result = run_sdk_compat_smoke(
        base_url="http://localhost:4000",
        employee_key="evk-secret",
        employee_key_env_name="AIMANAGER_EMPLOYEE_VIRTUAL_KEY",
        request_marker="sdk-smoke-123",
        fetch=fetch,
    )

    assert result.status == "FAIL"
    assert result.detail == "models request failed: OSError"
    assert "http://localhost:4000" not in result.detail
    assert "evk-secret" not in result.detail
    assert "ycapi-token-secret" not in result.detail


def test_sdk_compat_smoke_fails_on_non_openai_chat_shape() -> None:
    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        if url.endswith("/v1/models"):
            return _models_response(["gemini-2.5-flash", "deepseek-chat", "ycapi-image-1"])
        return _json_response({"ok": True})

    result = run_sdk_compat_smoke(
        base_url="http://localhost:4000",
        employee_key="evk-secret",
        employee_key_env_name="AIMANAGER_EMPLOYEE_VIRTUAL_KEY",
        request_marker="sdk-smoke-123",
        fetch=fetch,
    )

    assert result.status == "FAIL"
    assert result.detail == "chat gemini-2.5-flash response missing choices message content"


def test_sdk_compat_smoke_fails_on_non_openai_image_shape() -> None:
    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        if url.endswith("/v1/models"):
            return _models_response(["gemini-2.5-flash", "deepseek-chat", "ycapi-image-1"])
        if url.endswith("/v1/chat/completions"):
            return _chat_response("gemini-2.5-flash")
        if url.endswith("/v1/images/generations"):
            return _json_response({"created": 1, "data": [{"revised_prompt": "ok"}]})
        raise AssertionError(f"unexpected URL {url}")

    result = run_sdk_compat_smoke(
        base_url="http://localhost:4000",
        employee_key="evk-secret",
        employee_key_env_name="AIMANAGER_EMPLOYEE_VIRTUAL_KEY",
        request_marker="sdk-smoke-123",
        fetch=fetch,
    )

    assert result.status == "FAIL"
    assert result.detail == "image response missing url or b64_json"


def test_sdk_compat_smoke_skip_image_checks_chat_only_models() -> None:
    calls: list[str] = []

    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        calls.append(url)
        if url.endswith("/v1/models"):
            return _models_response(["gemini-2.5-flash", "deepseek-chat"])
        return _chat_response("gemini-2.5-flash")

    result = run_sdk_compat_smoke(
        base_url="http://localhost:4000",
        employee_key="evk-secret",
        employee_key_env_name="AIMANAGER_EMPLOYEE_VIRTUAL_KEY",
        request_marker="sdk-smoke-123",
        fetch=fetch,
        include_image=False,
        include_vision=False,
    )

    assert result.status == "PASS"
    assert result.checked_endpoints == ("models", "chat:gemini-2.5-flash", "chat:deepseek-chat")
    assert not any(url.endswith("/v1/images/generations") for url in calls)


def test_sdk_compat_smoke_cli_blocks_without_employee_key_and_ignores_ycapi_token(monkeypatch, capsys) -> None:
    monkeypatch.delenv("AIMANAGER_EMPLOYEE_VIRTUAL_KEY", raising=False)
    monkeypatch.setenv("YCAPI_API_TOKEN", "ycapi-token-secret")

    exit_code = main(["--base-url", "http://localhost:4000"])

    output = capsys.readouterr().out
    assert exit_code == 2
    assert "BLOCKED SDK compatibility smoke" in output
    assert "AIMANAGER_EMPLOYEE_VIRTUAL_KEY" in output
    assert "ycapi-token-secret" not in output


def test_sdk_compat_smoke_cli_rejects_ycapi_token_env_as_employee_key(monkeypatch, capsys) -> None:
    monkeypatch.setenv("YCAPI_API_TOKEN", "ycapi-token-secret")

    exit_code = main(["--base-url", "http://localhost:4000", "--employee-key-env", "YCAPI_API_TOKEN"])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL SDK compatibility smoke" in output
    assert "employee key env cannot be YCAPI_API_TOKEN" in output
    assert "ycapi-token-secret" not in output


def test_sdk_compat_smoke_cli_rejects_employee_key_equal_to_ycapi_token(monkeypatch, capsys) -> None:
    monkeypatch.setenv("AIMANAGER_EMPLOYEE_VIRTUAL_KEY", "ycapi-token-secret")
    monkeypatch.setenv("YCAPI_API_TOKEN", "ycapi-token-secret")

    exit_code = main(["--base-url", "http://localhost:4000"])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL SDK compatibility smoke" in output
    assert "employee key matches YCAPI_API_TOKEN" in output
    assert "ycapi-token-secret" not in output


def test_sdk_compat_smoke_cli_rejects_employee_key_equal_to_ycapi_token_after_strip(
    monkeypatch, capsys
) -> None:
    monkeypatch.setenv("AIMANAGER_EMPLOYEE_VIRTUAL_KEY", "  ycapi-token-secret ")
    monkeypatch.setenv("YCAPI_API_TOKEN", "ycapi-token-secret")

    exit_code = main(["--base-url", "http://localhost:4000"])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "employee key matches YCAPI_API_TOKEN" in output
    assert "ycapi-token-secret" not in output


def _models_response(model_ids: list[str]) -> HttpResponse:
    return _json_response({"object": "list", "data": [{"id": model_id} for model_id in model_ids]})


def _chat_response(model: str) -> HttpResponse:
    return _json_response(
        {
            "id": "chatcmpl-sdk-smoke",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
        }
    )


def _json_response(payload: dict[str, object]) -> HttpResponse:
    return HttpResponse(
        status_code=200,
        headers={"Content-Type": "application/json"},
        body=json.dumps(payload).encode("utf-8"),
    )

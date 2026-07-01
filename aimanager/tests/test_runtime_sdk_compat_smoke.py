from __future__ import annotations

import json

from aimanager.scripts.smoke_sdk_compat import HttpResponse, SdkCompatSmokeResult
from aimanager.scripts.smoke_runtime_sdk_compat import main, run_runtime_sdk_compat_smoke


def test_runtime_sdk_compat_smoke_creates_key_runs_sdk_and_deletes_without_ycapi_token() -> None:
    admin_calls: list[tuple[str, str, dict[str, str], dict[str, object] | None]] = []
    sdk_calls: list[dict[str, object]] = []

    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        payload = json.loads(body.decode("utf-8")) if body else None
        admin_calls.append((method, url, headers, payload))
        assert headers["Authorization"] == "Bearer master-secret"
        assert "YCAPI_API_TOKEN" not in headers
        if url.endswith("/team/info?team_id=team_aimanager_sdk_smoke"):
            return _json_response({"team_id": "team_aimanager_sdk_smoke"})
        if url.endswith("/key/generate"):
            assert method == "POST"
            assert payload is not None
            assert payload["key_alias"] == "aimanager-sdk-smoke-sdk-runtime-123"
            assert payload["user_id"] == "aimanager-sdk-smoke-user"
            assert payload["team_id"] == "team_aimanager_sdk_smoke"
            assert payload["models"] == ["gemini-2.5-flash", "deepseek-chat", "ycapi-image-1"]
            metadata = payload["metadata"]  # type: ignore[index]
            assert metadata["scenario_l2"] == "code_assist"  # type: ignore[index]
            assert metadata["aimanager_smoke_id"] == "sdk-runtime-123"  # type: ignore[index]
            assert "shared_key" not in metadata  # type: ignore[operator]
            return _json_response({"key": "evk-secret"})
        if url.endswith("/key/delete"):
            assert method == "POST"
            assert payload == {"keys": ["evk-secret"]}
            assert headers["x-aimanager-actor"] == "aimanager-ci"
            assert headers["x-aimanager-reason"] == "AiManager runtime SDK compatibility smoke cleanup"
            return _json_response({"deleted_keys": ["evk-secret"]})
        raise AssertionError(f"unexpected admin URL {url}")

    def sdk_runner(**kwargs: object) -> SdkCompatSmokeResult:
        sdk_calls.append(kwargs)
        assert kwargs["base_url"] == "http://business.local"
        assert kwargs["employee_key"] == "evk-secret"
        assert kwargs["employee_key_env_name"] == "AIMANAGER_EMPLOYEE_VIRTUAL_KEY"
        assert kwargs["request_marker"] == "sdk-runtime-123"
        assert kwargs["chat_models"] == ("gemini-2.5-flash", "deepseek-chat")
        assert kwargs["image_model"] == "ycapi-image-1"
        assert kwargs["vision_model"] == "gemini-2.5-flash"
        return SdkCompatSmokeResult(
            status="PASS",
            detail="employee OpenAI-compatible SDK flow succeeded through AiManager",
            checked_endpoints=("models", "chat:gemini-2.5-flash", "chat:deepseek-chat", "image"),
        )

    result = run_runtime_sdk_compat_smoke(
        business_base_url="http://business.local",
        admin_base_url="http://admin.local",
        master_key="master-secret",
        request_marker="sdk-runtime-123",
        fetch=fetch,
        sdk_smoke_runner=sdk_runner,
    )

    assert result.status == "PASS"
    assert result.checked_endpoints == ("models", "chat:gemini-2.5-flash", "chat:deepseek-chat", "image")
    assert result.key_alias == "aimanager-sdk-smoke-sdk-runtime-123"
    assert result.cleanup_status == "PASS"
    assert len(sdk_calls) == 1
    assert [call[1].rsplit("/", 1)[-1] for call in admin_calls] == [
        "info?team_id=team_aimanager_sdk_smoke",
        "generate",
        "delete",
    ]
    assert "evk-secret" not in result.detail
    assert "master-secret" not in result.detail


def test_runtime_sdk_compat_smoke_creates_team_when_missing() -> None:
    team_new_payloads: list[dict[str, object]] = []

    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        payload = json.loads(body.decode("utf-8")) if body else None
        if url.endswith("/team/info?team_id=team_aimanager_sdk_smoke"):
            return HttpResponse(status_code=404, headers={}, body=b"not found")
        if url.endswith("/team/new"):
            assert payload is not None
            team_new_payloads.append(payload)
            return _json_response({"team_id": payload["team_id"]})
        if url.endswith("/key/generate"):
            return _json_response({"key": "evk-secret"})
        if url.endswith("/key/delete"):
            return _json_response({"deleted_keys": ["evk-secret"]})
        raise AssertionError(f"unexpected URL {url}")

    result = run_runtime_sdk_compat_smoke(
        business_base_url="http://business.local",
        admin_base_url="http://admin.local",
        master_key="master-secret",
        request_marker="sdk-runtime-123",
        fetch=fetch,
        sdk_smoke_runner=lambda **kwargs: SdkCompatSmokeResult(status="PASS", detail="ok"),
    )

    assert result.status == "PASS"
    assert team_new_payloads == [
        {
            "team_id": "team_aimanager_sdk_smoke",
            "team_alias": "AiManager SDK Smoke Team",
            "models": ["gemini-2.5-flash", "deepseek-chat", "ycapi-image-1"],
            "max_budget": 100,
            "rpm_limit": 600,
            "tpm_limit": 120000,
        }
    ]


def test_runtime_sdk_compat_smoke_deletes_key_when_sdk_fails() -> None:
    deleted: list[dict[str, object]] = []

    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        payload = json.loads(body.decode("utf-8")) if body else None
        if url.endswith("/team/info?team_id=team_aimanager_sdk_smoke"):
            return _json_response({"team_id": "team_aimanager_sdk_smoke"})
        if url.endswith("/key/generate"):
            return _json_response({"key": "evk-secret"})
        if url.endswith("/key/delete"):
            assert payload is not None
            deleted.append(payload)
            return _json_response({"deleted_keys": ["evk-secret"]})
        raise AssertionError(f"unexpected URL {url}")

    result = run_runtime_sdk_compat_smoke(
        business_base_url="http://business.local",
        admin_base_url="http://admin.local",
        master_key="master-secret",
        request_marker="sdk-runtime-123",
        fetch=fetch,
        sdk_smoke_runner=lambda **kwargs: SdkCompatSmokeResult(
            status="FAIL",
            detail="chat gemini-2.5-flash request returned HTTP 500",
            status_code=500,
        ),
    )

    assert result.status == "FAIL"
    assert result.status_code == 500
    assert result.cleanup_status == "PASS"
    assert deleted == [{"keys": ["evk-secret"]}]
    assert "chat gemini-2.5-flash request returned HTTP 500" in result.detail
    assert "evk-secret" not in result.detail


def test_runtime_sdk_compat_smoke_reports_cleanup_when_sdk_runner_raises_after_key_creation() -> None:
    deleted: list[dict[str, object]] = []

    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        payload = json.loads(body.decode("utf-8")) if body else None
        if url.endswith("/team/info?team_id=team_aimanager_sdk_smoke"):
            return _json_response({"team_id": "team_aimanager_sdk_smoke"})
        if url.endswith("/key/generate"):
            return _json_response({"key": "evk-secret"})
        if url.endswith("/key/delete"):
            assert payload is not None
            deleted.append(payload)
            return _json_response({"deleted_keys": ["evk-secret"]})
        raise AssertionError(f"unexpected URL {url}")

    result = run_runtime_sdk_compat_smoke(
        business_base_url="http://business.local",
        admin_base_url="http://admin.local",
        master_key="master-secret",
        request_marker="sdk-runtime-123",
        fetch=fetch,
        sdk_smoke_runner=lambda **kwargs: (_ for _ in ()).throw(OSError("sdk runner transport")),
    )

    assert result.status == "BLOCKED"
    assert result.detail == "admin surface unavailable: OSError"
    assert result.cleanup_status == "PASS"
    assert deleted == [{"keys": ["evk-secret"]}]


def test_runtime_sdk_compat_smoke_fails_if_cleanup_fails_after_pass() -> None:
    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        if url.endswith("/team/info?team_id=team_aimanager_sdk_smoke"):
            return _json_response({"team_id": "team_aimanager_sdk_smoke"})
        if url.endswith("/key/generate"):
            return _json_response({"key": "evk-secret"})
        if url.endswith("/key/delete"):
            return HttpResponse(status_code=500, headers={}, body=b"delete failed with evk-secret")
        raise AssertionError(f"unexpected URL {url}")

    result = run_runtime_sdk_compat_smoke(
        business_base_url="http://business.local",
        admin_base_url="http://admin.local",
        master_key="master-secret",
        request_marker="sdk-runtime-123",
        fetch=fetch,
        sdk_smoke_runner=lambda **kwargs: SdkCompatSmokeResult(status="PASS", detail="ok"),
    )

    assert result.status == "FAIL"
    assert result.cleanup_status == "FAIL"
    assert result.detail == "SDK compatibility passed but disposable key cleanup failed: HTTP 500"
    assert "evk-secret" not in result.detail


def test_runtime_sdk_compat_smoke_fails_if_cleanup_transport_raises_after_pass() -> None:
    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        if url.endswith("/team/info?team_id=team_aimanager_sdk_smoke"):
            return _json_response({"team_id": "team_aimanager_sdk_smoke"})
        if url.endswith("/key/generate"):
            return _json_response({"key": "evk-secret"})
        if url.endswith("/key/delete"):
            raise OSError("delete failed for evk-secret")
        raise AssertionError(f"unexpected URL {url}")

    result = run_runtime_sdk_compat_smoke(
        business_base_url="http://business.local",
        admin_base_url="http://admin.local",
        master_key="master-secret",
        request_marker="sdk-runtime-123",
        fetch=fetch,
        sdk_smoke_runner=lambda **kwargs: SdkCompatSmokeResult(status="PASS", detail="ok"),
    )

    assert result.status == "FAIL"
    assert result.cleanup_status == "FAIL"
    assert result.detail == "SDK compatibility passed but disposable key cleanup failed: OSError"
    assert "evk-secret" not in result.detail


def test_runtime_sdk_compat_smoke_blocks_without_master_key_without_fetch() -> None:
    result = run_runtime_sdk_compat_smoke(
        business_base_url="http://business.local",
        admin_base_url="http://admin.local",
        master_key=" ",
        request_marker="sdk-runtime-123",
        fetch=lambda method, url, headers, body: (_ for _ in ()).throw(AssertionError("no fetch")),
        sdk_smoke_runner=lambda **kwargs: (_ for _ in ()).throw(AssertionError("no sdk")),
    )

    assert result.status == "BLOCKED"
    assert result.detail == "missing LITELLM_MASTER_KEY or --master-key"
    assert result.cleanup_status == "SKIP"


def test_runtime_sdk_compat_smoke_blocks_when_admin_surface_is_unreachable() -> None:
    result = run_runtime_sdk_compat_smoke(
        business_base_url="http://business.local",
        admin_base_url="http://admin.local",
        master_key="master-secret",
        request_marker="sdk-runtime-123",
        fetch=lambda method, url, headers, body: (_ for _ in ()).throw(OSError("connection refused")),
        sdk_smoke_runner=lambda **kwargs: (_ for _ in ()).throw(AssertionError("no sdk")),
    )

    assert result.status == "BLOCKED"
    assert result.detail == "admin surface unavailable: OSError"
    assert result.cleanup_status == "SKIP"


def test_runtime_sdk_compat_smoke_blocks_when_admin_surface_returns_gateway_error() -> None:
    result = run_runtime_sdk_compat_smoke(
        business_base_url="http://business.local",
        admin_base_url="http://admin.local",
        master_key="master-secret",
        request_marker="sdk-runtime-123",
        fetch=lambda method, url, headers, body: HttpResponse(status_code=502, headers={}, body=b"bad gateway"),
        sdk_smoke_runner=lambda **kwargs: (_ for _ in ()).throw(AssertionError("no sdk")),
    )

    assert result.status == "BLOCKED"
    assert result.detail == "admin surface unavailable: team info returned HTTP 502"
    assert result.status_code == 502
    assert result.cleanup_status == "SKIP"


def test_runtime_sdk_compat_smoke_blocks_when_business_surface_is_unreachable_and_cleans_up() -> None:
    deleted: list[dict[str, object]] = []

    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        payload = json.loads(body.decode("utf-8")) if body else None
        if url.endswith("/team/info?team_id=team_aimanager_sdk_smoke"):
            return _json_response({"team_id": "team_aimanager_sdk_smoke"})
        if url.endswith("/key/generate"):
            return _json_response({"key": "evk-secret"})
        if url.endswith("/key/delete"):
            assert payload is not None
            deleted.append(payload)
            return _json_response({"deleted_keys": ["evk-secret"]})
        raise AssertionError(f"unexpected URL {url}")

    result = run_runtime_sdk_compat_smoke(
        business_base_url="http://business.local",
        admin_base_url="http://admin.local",
        master_key="master-secret",
        request_marker="sdk-runtime-123",
        fetch=fetch,
        sdk_smoke_runner=lambda **kwargs: SdkCompatSmokeResult(
            status="FAIL",
            detail="models request failed: URLError",
        ),
    )

    assert result.status == "BLOCKED"
    assert result.detail == "business surface unavailable: models request failed: URLError"
    assert result.cleanup_status == "PASS"
    assert deleted == [{"keys": ["evk-secret"]}]


def test_runtime_sdk_compat_smoke_cli_blocks_without_master_key(capsys) -> None:
    exit_code = main(
        [
            "--business-base-url",
            "http://business.local",
            "--admin-base-url",
            "http://admin.local",
            "--master-key",
            "",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 2
    assert "BLOCKED runtime SDK compatibility smoke" in output
    assert "missing LITELLM_MASTER_KEY or --master-key" in output


def _json_response(payload: dict[str, object]) -> HttpResponse:
    return HttpResponse(
        status_code=200,
        headers={"Content-Type": "application/json"},
        body=json.dumps(payload).encode("utf-8"),
    )

from __future__ import annotations

import json

from aimanager.scripts import smoke_live_ycapi
from aimanager.scripts.smoke_live_ycapi import HttpResponse, main, run_live_ycapi_smoke


def test_live_ycapi_smoke_blocks_without_token() -> None:
    calls: list[str] = []

    result = run_live_ycapi_smoke(
        base_url="https://ycapi.ycaicloud.com/v1",
        api_token="",
        token_env_name="YCAPI_API_TOKEN",
        expected_models=("gemini-2.5-flash",),
        fetch=lambda method, url, headers, body: calls.append(url) or _models_response([]),
    )

    assert result.status == "BLOCKED"
    assert "YCAPI_API_TOKEN" in result.detail
    assert calls == []


def test_live_ycapi_smoke_gets_models_without_printing_token() -> None:
    token = "ycapi-token-secret"
    calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        calls.append((method, url, headers, body))
        assert method == "GET"
        assert url == "https://ycapi.ycaicloud.com/v1/models"
        assert headers["Authorization"] == f"Bearer {token}"
        assert headers["Accept"] == "application/json"
        assert body is None
        return _models_response(["gemini-2.5-flash", "deepseek-chat", "ycapi-image-1"])

    result = run_live_ycapi_smoke(
        base_url="https://ycapi.ycaicloud.com/v1",
        api_token=token,
        token_env_name="YCAPI_API_TOKEN",
        expected_models=("gemini-2.5-flash", "ycapi-image-1"),
        fetch=fetch,
    )

    assert result.status == "PASS"
    assert result.model_count == 3
    assert "ycapi-token-secret" not in result.detail
    assert len(calls) == 1


def test_live_ycapi_smoke_fails_when_expected_model_is_missing() -> None:
    result = run_live_ycapi_smoke(
        base_url="https://ycapi.ycaicloud.com/v1",
        api_token="ycapi-token-secret",
        token_env_name="YCAPI_API_TOKEN",
        expected_models=("gemini-2.5-flash", "ycapi-image-1"),
        fetch=lambda method, url, headers, body: _models_response(["gemini-2.5-flash"]),
    )

    assert result.status == "FAIL"
    assert "missing expected model(s): ycapi-image-1" in result.detail
    assert "ycapi-token-secret" not in result.detail


def test_live_ycapi_smoke_does_not_leak_response_body_on_http_error() -> None:
    result = run_live_ycapi_smoke(
        base_url="https://ycapi.ycaicloud.com/v1",
        api_token="ycapi-token-secret",
        token_env_name="YCAPI_API_TOKEN",
        expected_models=(),
        fetch=lambda method, url, headers, body: HttpResponse(
            status_code=403,
            headers={"Content-Type": "application/json"},
            body=b'{"error":"Bearer ycapi-token-secret and sk-secret-leak"}',
        ),
    )

    assert result.status == "FAIL"
    assert result.detail == "ycapi /models returned HTTP 403"
    assert "ycapi-token-secret" not in result.detail
    assert "sk-secret-leak" not in result.detail


def test_live_ycapi_smoke_masks_transport_errors() -> None:
    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        raise OSError("failed with ycapi-token-secret")

    result = run_live_ycapi_smoke(
        base_url="https://ycapi.ycaicloud.com/v1",
        api_token="ycapi-token-secret",
        token_env_name="YCAPI_API_TOKEN",
        expected_models=(),
        fetch=fetch,
    )

    assert result.status == "FAIL"
    assert result.detail == "ycapi /models request failed: OSError"
    assert "ycapi-token-secret" not in result.detail


def test_live_ycapi_smoke_cli_returns_blocked_when_token_missing(monkeypatch, capsys) -> None:
    monkeypatch.delenv("YCAPI_API_TOKEN", raising=False)

    exit_code = main(["--base-url", "https://ycapi.ycaicloud.com/v1"])

    output = capsys.readouterr().out
    assert exit_code == 2
    assert "BLOCKED live ycapi smoke" in output
    assert "YCAPI_API_TOKEN" in output


def test_live_ycapi_smoke_cli_masks_malformed_url_and_token(monkeypatch, capsys) -> None:
    monkeypatch.setenv("YCAPI_API_TOKEN", "ycapi-token-secret")

    exit_code = main(["--base-url", "https://ycapi.ycaicloud.com/v 1"])

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert exit_code == 1
    assert "FAIL live ycapi smoke" in output
    assert "ycapi-token-secret" not in output
    assert "https://ycapi.ycaicloud.com/v 1" not in output


def test_live_ycapi_smoke_cli_preserves_blocked_exit_when_result_file_write_fails(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    monkeypatch.delenv("YCAPI_API_TOKEN", raising=False)
    not_a_directory = tmp_path / "not-a-directory"
    not_a_directory.write_text("already a file", encoding="utf-8")

    exit_code = main(
        [
            "--base-url",
            "https://ycapi.ycaicloud.com/v1",
            "--output-json-file",
            str(not_a_directory / "result.json"),
        ]
    )

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert exit_code == 2
    assert "BLOCKED live ycapi smoke" in output
    assert "result file not written" in output


def test_live_ycapi_smoke_cli_returns_failure_when_pass_result_file_write_fails(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    monkeypatch.setenv("YCAPI_API_TOKEN", "ycapi-token-secret")

    def fake_fetch_factory(*, timeout_seconds: float):  # noqa: ARG001
        return lambda method, url, headers, body: _models_response(["gemini-2.5-flash"])

    monkeypatch.setattr(smoke_live_ycapi, "_fetch_with_urllib", fake_fetch_factory)
    not_a_directory = tmp_path / "not-a-directory"
    not_a_directory.write_text("already a file", encoding="utf-8")

    exit_code = main(
        [
            "--base-url",
            "https://ycapi.ycaicloud.com/v1",
            "--expect-model",
            "gemini-2.5-flash",
            "--output-json-file",
            str(not_a_directory / "result.json"),
        ]
    )

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert exit_code == 1
    assert "PASS live ycapi smoke" in output
    assert "result file not written" in output
    assert "ycapi-token-secret" not in output


def _models_response(model_ids: list[str]) -> HttpResponse:
    return HttpResponse(
        status_code=200,
        headers={"Content-Type": "application/json"},
        body=json.dumps({"object": "list", "data": [{"id": model_id} for model_id in model_ids]}).encode("utf-8"),
    )

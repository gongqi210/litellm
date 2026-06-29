from __future__ import annotations

from types import SimpleNamespace

import pytest

from aimanager.litellm_entrypoint import (
    AIMANAGER_ASGI_APP,
    RuntimeEnvironmentError,
    install_aimanager_app_override,
    validate_required_runtime_env,
)


def test_entrypoint_overrides_litellm_uvicorn_app_without_losing_other_args() -> None:
    class FakeHelpers:
        @staticmethod
        def _get_default_unvicorn_init_args(**kwargs: object) -> dict[str, object]:
            return {
                "app": "litellm.proxy.proxy_server:app",
                "host": kwargs["host"],
                "port": kwargs["port"],
                "timeout_keep_alive": kwargs["keepalive_timeout"],
            }

    fake_proxy_cli = SimpleNamespace(ProxyInitializationHelpers=FakeHelpers)

    install_aimanager_app_override(fake_proxy_cli)

    args = FakeHelpers._get_default_unvicorn_init_args(
        host="0.0.0.0",
        port=4000,
        keepalive_timeout=15,
    )

    assert args == {
        "app": AIMANAGER_ASGI_APP,
        "host": "0.0.0.0",
        "port": 4000,
        "timeout_keep_alive": 15,
    }


def test_runtime_env_preflight_rejects_missing_required_secrets() -> None:
    with pytest.raises(RuntimeEnvironmentError, match="LITELLM_MASTER_KEY, YCAPI_API_TOKEN"):
        validate_required_runtime_env(
            {
                "YCAPI_BASE_URL": "https://ycapi.ycaicloud.com/v1",
                "LITELLM_MASTER_KEY": "",
            }
        )


def test_runtime_env_preflight_accepts_required_values() -> None:
    validate_required_runtime_env(
        {
            "LITELLM_MASTER_KEY": "test-master-key",
            "YCAPI_BASE_URL": "https://ycapi.ycaicloud.com/v1",
            "YCAPI_API_TOKEN": "test-ycapi-token",
        }
    )

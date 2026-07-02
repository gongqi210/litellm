from __future__ import annotations

from types import SimpleNamespace

import pytest

from aimanager.litellm_entrypoint import (
    AIMANAGER_ASGI_APP,
    CONFIG_FILE_PATH_ENV,
    LOCAL_MODEL_COST_MAP_ENV,
    RuntimeEnvironmentError,
    configure_litellm_startup_environment,
    install_aimanager_app_override,
    register_aimanager_enforced_params_guard,
    register_aimanager_image_model_costs,
    register_aimanager_video_model_costs,
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


def test_entrypoint_forces_local_model_cost_map_before_litellm_import() -> None:
    env = {LOCAL_MODEL_COST_MAP_ENV: "False"}

    configure_litellm_startup_environment(env)

    assert env[LOCAL_MODEL_COST_MAP_ENV] == "True"


def test_entrypoint_captures_config_arg_for_late_startup_hooks() -> None:
    env: dict[str, str] = {}

    configure_litellm_startup_environment(env, ["--config=/tmp/aimanager-config.yaml"])

    assert env[CONFIG_FILE_PATH_ENV] == "/tmp/aimanager-config.yaml"


def test_register_aimanager_image_model_costs_uses_provider_prefixed_model(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
model_list:
  - model_name: deepseek-chat
    litellm_params:
      model: openai/deepseek-chat
      input_cost_per_token: 0.0000001
      output_cost_per_token: 0.0000004
  - model_name: ycapi-image-1
    litellm_params:
      model: openai/ycapi-image-1
      input_cost_per_image: 0.01
    model_info:
      mode: image_generation
      input_cost_per_image: 0.01
""",
        encoding="utf-8",
    )
    registered: list[dict[str, dict[str, object]]] = []

    returned = register_aimanager_image_model_costs(
        config_path,
        register_model=lambda *, model_cost: registered.append(model_cost),
    )

    assert returned == {
        "openai/ycapi-image-1": {
            "mode": "image_generation",
            "input_cost_per_image": 0.01,
        }
    }
    assert registered == [returned]


def test_register_aimanager_video_model_costs_uses_provider_prefixed_model(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
model_list:
  - model_name: ycapi-image-1
    litellm_params:
      model: openai/ycapi-image-1
      input_cost_per_image: 0.01
    model_info:
      mode: image_generation
      input_cost_per_image: 0.01
  - model_name: ycapi-video-1
    litellm_params:
      model: openai/ycapi-video-1
      output_cost_per_video_per_second: 0.5
    model_info:
      mode: video_generation
      output_cost_per_video_per_second: 0.5
""",
        encoding="utf-8",
    )
    registered: list[dict[str, dict[str, object]]] = []

    returned = register_aimanager_video_model_costs(
        config_path,
        register_model=lambda *, model_cost: registered.append(model_cost),
    )

    assert returned == {
        "openai/ycapi-video-1": {
            "mode": "video_generation",
            "output_cost_per_video_per_second": 0.5,
        }
    }
    assert registered == [returned]


def test_register_aimanager_video_model_costs_noop_without_video_model(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
model_list:
  - model_name: ycapi-image-1
    litellm_params:
      model: openai/ycapi-image-1
      input_cost_per_image: 0.01
    model_info:
      mode: image_generation
      input_cost_per_image: 0.01
""",
        encoding="utf-8",
    )
    calls: list[object] = []

    returned = register_aimanager_video_model_costs(
        config_path,
        register_model=lambda *, model_cost: calls.append(model_cost),
    )

    assert returned == {}
    assert calls == []


def test_register_aimanager_enforced_params_guard_registers_proxy_callback_once() -> None:
    registered: list[object] = []

    class FakeCallbackManager:
        def add_litellm_callback(self, callback: object) -> None:
            registered.append(callback)
            fake_litellm.callbacks.append(callback)

    fake_litellm = SimpleNamespace(
        callbacks=[],
        logging_callback_manager=FakeCallbackManager(),
    )

    first = register_aimanager_enforced_params_guard(litellm_module=fake_litellm)
    second = register_aimanager_enforced_params_guard(litellm_module=fake_litellm)

    assert second is first
    assert registered == [first]
    assert fake_litellm.callbacks == [first]

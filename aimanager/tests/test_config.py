from pathlib import Path

import pytest
import yaml

from aimanager.scripts.validate_config import (
    ConfigValidationError,
    validate_aimanager_config,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "aimanager" / "config.yaml"
COMPOSE_PATH = PROJECT_ROOT / "aimanager" / "docker-compose.yml"
DOCKERFILE_PATH = PROJECT_ROOT / "Dockerfile"
ENV_EXAMPLE_PATH = PROJECT_ROOT / "aimanager" / ".env.example"


def test_aimanager_config_is_locked_to_ycapi() -> None:
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))

    validate_aimanager_config(CONFIG_PATH)

    assert config["general_settings"]["master_key"] == "os.environ/LITELLM_MASTER_KEY"
    assert config["general_settings"]["store_model_in_db"] is False
    assert config["general_settings"]["always_include_stream_usage"] is True

    model_list = config["model_list"]
    assert model_list

    for model in model_list:
        params = model["litellm_params"]
        assert params["model"].startswith("openai/")
        assert params["api_base"] == "os.environ/YCAPI_BASE_URL"
        assert params["api_key"] == "os.environ/YCAPI_API_TOKEN"
        if model["model_name"] == "ycapi-image-1":
            model_info = model["model_info"]
            assert model_info["mode"] == "image_generation"
            assert "output_cost_per_image" not in params
            assert params["input_cost_per_image"] > 0
            assert model_info["input_cost_per_image"] == params["input_cost_per_image"]
        else:
            assert params["input_cost_per_token"] > 0
            assert params["output_cost_per_token"] > 0


def test_compose_uses_aimanager_litellm_entrypoint() -> None:
    compose = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    service = compose["services"]["aimanager"]

    assert service["build"]["target"] == "runtime"
    assert "target" not in service["build"].get("args", {})
    assert service["entrypoint"] == ["python", "-m", "aimanager.litellm_entrypoint"]
    assert service["command"] == [
        "--config=/app/config.yaml",
        "--host=0.0.0.0",
        "--port=4000",
        "--enforce_prisma_migration_check",
    ]
    assert service["environment"]["CONFIG_FILE_PATH"] == "/app/config.yaml"
    assert "LITELLM_MASTER_KEY" in service["environment"]
    assert service["environment"]["LITELLM_MASTER_KEY"] is None
    assert service["environment"]["YCAPI_BASE_URL"] == (
        "${YCAPI_BASE_URL:-https://ycapi.ycaicloud.com/v1}"
    )
    assert "YCAPI_API_TOKEN" in service["environment"]
    assert service["environment"]["YCAPI_API_TOKEN"] is None
    assert service["environment"].get("AIMANAGER_ROUTE_SURFACE", "business") == "business"
    assert service["environment"]["AIMANAGER_DATABASE_READY_CHECK_ENABLED"] == "True"
    assert service["environment"]["AIMANAGER_WORK_CONTEXT_ENFORCEMENT_ENABLED"] == "True"
    assert service["environment"]["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"


def test_compose_postgres_host_port_is_localhost_and_non_default() -> None:
    compose = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    service = compose["services"]["db"]

    assert service["ports"] == ["127.0.0.1:${AIMANAGER_POSTGRES_PORT:-15440}:5432"]


def test_compose_exposes_management_surface_only_on_localhost_profile() -> None:
    compose = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    service = compose["services"]["aimanager-admin"]

    assert service["profiles"] == ["admin"]
    assert service["image"] == "aimanager-litellm:local"
    assert service["environment"]["AIMANAGER_ROUTE_SURFACE"] == "management"
    assert service["environment"]["AIMANAGER_RBAC_ENABLED"] == "True"
    assert service["environment"]["AIMANAGER_DATABASE_READY_CHECK_ENABLED"] == "True"
    assert "AIMANAGER_WORK_CONTEXT_ENFORCEMENT_ENABLED" not in service["environment"]
    assert service["environment"]["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
    assert service["ports"] == ["127.0.0.1:4001:4000"]
    assert service["entrypoint"] == ["python", "-m", "aimanager.litellm_entrypoint"]
    assert service["command"] == [
        "--config=/app/config.yaml",
        "--host=0.0.0.0",
        "--port=4000",
        "--enforce_prisma_migration_check",
    ]


def test_runtime_dockerfile_copies_aimanager_package() -> None:
    dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")

    assert "COPY --from=builder /app/aimanager /app/aimanager" in dockerfile


def test_env_example_uses_non_secret_placeholders() -> None:
    env_example = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")

    assert "AIMANAGER_WORK_CONTEXT_ENFORCEMENT_ENABLED=True" in env_example
    assert "sk-" not in env_example
    assert "AKIA" not in env_example
    assert "AIza" not in env_example
    assert "BEGIN PRIVATE KEY" not in env_example


def test_validator_rejects_direct_vendor_upstream(tmp_path: Path) -> None:
    bad_config = tmp_path / "bad-config.yaml"
    bad_config.write_text(
        """
model_list:
  - model_name: gpt-4o
    litellm_params:
      model: openai/gpt-4o
      api_base: https://api.openai.com/v1
      api_key: os.environ/OPENAI_API_KEY
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
  store_model_in_db: false
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigValidationError):
        validate_aimanager_config(bad_config)


@pytest.mark.parametrize("value", [False, None])
def test_validator_rejects_missing_or_disabled_stream_usage(tmp_path: Path, value: object) -> None:
    general_settings = {
        "master_key": "os.environ/LITELLM_MASTER_KEY",
        "store_model_in_db": False,
    }
    if value is not None:
        general_settings["always_include_stream_usage"] = value
    bad_config = tmp_path / "bad-stream-usage.yaml"
    bad_config.write_text(
        yaml.safe_dump(
            {
                "model_list": [
                    {
                        "model_name": "gemini-2.5-flash",
                        "litellm_params": {
                            "model": "openai/gemini-2.5-flash",
                            "api_base": "os.environ/YCAPI_BASE_URL",
                            "api_key": "os.environ/YCAPI_API_TOKEN",
                            "input_cost_per_token": 0.000001,
                            "output_cost_per_token": 0.000001,
                        },
                    }
                ],
                "general_settings": general_settings,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigValidationError, match="always_include_stream_usage"):
        validate_aimanager_config(bad_config)


def test_validator_rejects_zero_chat_pricing(tmp_path: Path) -> None:
    bad_config = tmp_path / "bad-chat-price.yaml"
    bad_config.write_text(
        """
model_list:
  - model_name: gemini-2.5-flash
    litellm_params:
      model: openai/gemini-2.5-flash
      api_base: os.environ/YCAPI_BASE_URL
      api_key: os.environ/YCAPI_API_TOKEN
      input_cost_per_token: 0.0
      output_cost_per_token: 0.000001
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
  store_model_in_db: false
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigValidationError, match="input_cost_per_token"):
        validate_aimanager_config(bad_config)


@pytest.mark.parametrize(
    "bad_value",
    [False, True, "0.01", -0.1, float("nan"), float("inf")],
)
def test_validator_rejects_non_finite_or_non_numeric_chat_pricing(
    tmp_path: Path, bad_value: object
) -> None:
    bad_config = tmp_path / "bad-chat-price-type.yaml"
    bad_config.write_text(
        yaml.safe_dump(
            {
                "model_list": [
                    {
                        "model_name": "gemini-2.5-flash",
                        "litellm_params": {
                            "model": "openai/gemini-2.5-flash",
                            "api_base": "os.environ/YCAPI_BASE_URL",
                            "api_key": "os.environ/YCAPI_API_TOKEN",
                            "input_cost_per_token": bad_value,
                            "output_cost_per_token": 0.000001,
                        },
                    }
                ],
                "general_settings": {
                    "master_key": "os.environ/LITELLM_MASTER_KEY",
                    "store_model_in_db": False,
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigValidationError, match="input_cost_per_token"):
        validate_aimanager_config(bad_config)


def test_validator_rejects_missing_chat_output_pricing(tmp_path: Path) -> None:
    bad_config = tmp_path / "bad-chat-missing-output-price.yaml"
    bad_config.write_text(
        """
model_list:
  - model_name: deepseek-chat
    litellm_params:
      model: openai/deepseek-chat
      api_base: os.environ/YCAPI_BASE_URL
      api_key: os.environ/YCAPI_API_TOKEN
      input_cost_per_token: 0.000001
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
  store_model_in_db: false
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigValidationError, match="output_cost_per_token"):
        validate_aimanager_config(bad_config)


def test_validator_rejects_image_output_cost_per_image(tmp_path: Path) -> None:
    bad_config = tmp_path / "bad-image-price-key.yaml"
    bad_config.write_text(
        """
model_list:
  - model_name: ycapi-image-1
    litellm_params:
      model: openai/ycapi-image-1
      api_base: os.environ/YCAPI_BASE_URL
      api_key: os.environ/YCAPI_API_TOKEN
      output_cost_per_image: 0.01
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
  store_model_in_db: false
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigValidationError, match="input_cost_per_image"):
        validate_aimanager_config(bad_config)


def test_validator_rejects_zero_image_pricing(tmp_path: Path) -> None:
    bad_config = tmp_path / "bad-image-zero-price.yaml"
    bad_config.write_text(
        """
model_list:
  - model_name: ycapi-image-1
    litellm_params:
      model: openai/ycapi-image-1
      api_base: os.environ/YCAPI_BASE_URL
      api_key: os.environ/YCAPI_API_TOKEN
      input_cost_per_image: 0.0
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
  store_model_in_db: false
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigValidationError, match="input_cost_per_image"):
        validate_aimanager_config(bad_config)


def test_validator_rejects_image_model_without_image_generation_model_info(tmp_path: Path) -> None:
    bad_config = tmp_path / "bad-image-model-info.yaml"
    bad_config.write_text(
        """
model_list:
  - model_name: ycapi-image-1
    litellm_params:
      model: openai/ycapi-image-1
      api_base: os.environ/YCAPI_BASE_URL
      api_key: os.environ/YCAPI_API_TOKEN
      input_cost_per_image: 0.01
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
  store_model_in_db: false
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigValidationError, match="model_info.mode"):
        validate_aimanager_config(bad_config)


def test_validator_rejects_image_model_info_price_mismatch(tmp_path: Path) -> None:
    bad_config = tmp_path / "bad-image-model-info-price.yaml"
    bad_config.write_text(
        """
model_list:
  - model_name: ycapi-image-1
    litellm_params:
      model: openai/ycapi-image-1
      api_base: os.environ/YCAPI_BASE_URL
      api_key: os.environ/YCAPI_API_TOKEN
      input_cost_per_image: 0.01
    model_info:
      mode: image_generation
      input_cost_per_image: 0.02
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
  store_model_in_db: false
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigValidationError, match="model_info.input_cost_per_image"):
        validate_aimanager_config(bad_config)


def test_validator_rejects_pass_through_endpoints(tmp_path: Path) -> None:
    bad_config = tmp_path / "bad-passthrough.yaml"
    bad_config.write_text(
        """
model_list:
  - model_name: gemini-2.5-flash
    litellm_params:
      model: openai/gemini-2.5-flash
      api_base: os.environ/YCAPI_BASE_URL
      api_key: os.environ/YCAPI_API_TOKEN
      input_cost_per_token: 0.000001
      output_cost_per_token: 0.000001
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
  store_model_in_db: false
  pass_through_endpoints:
    - path: /anything
      target: https://example.com
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigValidationError, match="pass_through_endpoints"):
        validate_aimanager_config(bad_config)

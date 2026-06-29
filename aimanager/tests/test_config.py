from pathlib import Path

import pytest
import yaml

from aimanager.scripts.validate_config import (
    ConfigValidationError,
    validate_aimanager_config,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "aimanager" / "config.yaml"


def test_aimanager_config_is_locked_to_ycapi() -> None:
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))

    validate_aimanager_config(CONFIG_PATH)

    assert config["general_settings"]["master_key"] == "os.environ/LITELLM_MASTER_KEY"
    assert config["general_settings"]["store_model_in_db"] is False

    model_list = config["model_list"]
    assert model_list

    for model in model_list:
        params = model["litellm_params"]
        assert params["model"].startswith("openai/")
        assert params["api_base"] == "os.environ/YCAPI_BASE_URL"
        assert params["api_key"] == "os.environ/YCAPI_API_TOKEN"


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

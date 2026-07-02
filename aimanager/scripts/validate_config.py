from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Any

import yaml


ALLOWED_ENV_REFS = {
    "os.environ/YCAPI_BASE_URL",
    "os.environ/YCAPI_API_TOKEN",
    "os.environ/LITELLM_MASTER_KEY",
}

FORBIDDEN_CONFIG_MARKERS = (
    "api.openai.com",
    "api.anthropic.com",
    "generativelanguage.googleapis.com",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "AZURE_API_KEY",
    "AZURE_API_BASE",
    "BEDROCK",
    "VERTEX",
)

IMAGE_MODEL_NAME = "ycapi-image-1"
VIDEO_MODEL_NAME = "ycapi-video-1"
MAX_USER_API_KEY_CACHE_TTL_SECONDS = 5


class ConfigValidationError(ValueError):
    """Raised when the AiManager LiteLLM config violates the ycapi boundary."""


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigValidationError(f"config file does not exist: {path}") from exc
    if not isinstance(loaded, dict):
        raise ConfigValidationError("config root must be a YAML mapping")
    return loaded


def _walk_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        results: list[str] = []
        for child in value.values():
            results.extend(_walk_strings(child))
        return results
    if isinstance(value, list):
        results = []
        for child in value:
            results.extend(_walk_strings(child))
        return results
    return []


def _validate_env_refs(config: dict[str, Any]) -> None:
    for value in _walk_strings(config):
        if "os.environ/" not in value:
            continue
        _, env_ref = value.split("os.environ/", 1)
        env_name = env_ref.split()[0].strip("}'\"]")
        normalized = f"os.environ/{env_name}"
        if normalized not in ALLOWED_ENV_REFS:
            raise ConfigValidationError(f"unsupported environment reference: {normalized}")


def _validate_model_list(config: dict[str, Any]) -> None:
    model_list = config.get("model_list")
    if not isinstance(model_list, list) or not model_list:
        raise ConfigValidationError("model_list must be a non-empty list")

    for idx, item in enumerate(model_list):
        if not isinstance(item, dict):
            raise ConfigValidationError(f"model_list[{idx}] must be a mapping")

        model_name = item.get("model_name")
        params = item.get("litellm_params")
        if not isinstance(model_name, str) or not model_name:
            raise ConfigValidationError(f"model_list[{idx}].model_name is required")
        if not isinstance(params, dict):
            raise ConfigValidationError(f"model_list[{idx}].litellm_params must be a mapping")

        upstream_model = params.get("model")
        if not isinstance(upstream_model, str) or not upstream_model.startswith("openai/"):
            raise ConfigValidationError(f"{model_name}: upstream model must use openai/<ycapi-model>")
        if upstream_model == "openai/*":
            raise ConfigValidationError(f"{model_name}: openai/* wildcard is not allowed")

        if params.get("api_base") != "os.environ/YCAPI_BASE_URL":
            raise ConfigValidationError(f"{model_name}: api_base must be os.environ/YCAPI_BASE_URL")
        if params.get("api_key") != "os.environ/YCAPI_API_TOKEN":
            raise ConfigValidationError(f"{model_name}: api_key must be os.environ/YCAPI_API_TOKEN")

        if model_name == VIDEO_MODEL_NAME:
            _validate_positive_number(params, model_name, "output_cost_per_video_per_second")
            _validate_video_model_info(item, model_name, params["output_cost_per_video_per_second"])
        elif model_name == IMAGE_MODEL_NAME:
            if "output_cost_per_image" in params:
                raise ConfigValidationError(
                    f"{model_name}: output_cost_per_image is not used by LiteLLM image pricing; "
                    "set input_cost_per_image instead"
                )
            _validate_positive_number(params, model_name, "input_cost_per_image")
            _validate_image_model_info(item, model_name, params["input_cost_per_image"])
        else:
            _validate_positive_number(params, model_name, "input_cost_per_token")
            _validate_positive_number(params, model_name, "output_cost_per_token")


def _validate_image_model_info(item: dict[str, Any], model_name: str, expected_price: float) -> None:
    model_info = item.get("model_info")
    if not isinstance(model_info, dict):
        raise ConfigValidationError(f"{model_name}: model_info.mode must be image_generation")
    if model_info.get("mode") != "image_generation":
        raise ConfigValidationError(f"{model_name}: model_info.mode must be image_generation")
    _validate_positive_number({"model_info": model_info}, model_name, "model_info.input_cost_per_image")
    if model_info["input_cost_per_image"] != expected_price:
        raise ConfigValidationError(
            f"{model_name}: model_info.input_cost_per_image must match litellm_params.input_cost_per_image"
        )


def _validate_video_model_info(item: dict[str, Any], model_name: str, expected_price: float) -> None:
    model_info = item.get("model_info")
    if not isinstance(model_info, dict):
        raise ConfigValidationError(f"{model_name}: model_info.mode must be video_generation")
    if model_info.get("mode") != "video_generation":
        raise ConfigValidationError(f"{model_name}: model_info.mode must be video_generation")
    _validate_positive_number(
        {"model_info": model_info}, model_name, "model_info.output_cost_per_video_per_second"
    )
    if model_info["output_cost_per_video_per_second"] != expected_price:
        raise ConfigValidationError(
            f"{model_name}: model_info.output_cost_per_video_per_second must match "
            "litellm_params.output_cost_per_video_per_second"
        )


def _validate_general_settings(config: dict[str, Any]) -> None:
    general_settings = config.get("general_settings")
    if not isinstance(general_settings, dict):
        raise ConfigValidationError("general_settings must be a mapping")
    if general_settings.get("master_key") != "os.environ/LITELLM_MASTER_KEY":
        raise ConfigValidationError("general_settings.master_key must use os.environ/LITELLM_MASTER_KEY")
    if general_settings.get("store_model_in_db") is not False:
        raise ConfigValidationError("general_settings.store_model_in_db must be false for ycapi-only mode")
    if general_settings.get("pass_through_endpoints"):
        raise ConfigValidationError("general_settings.pass_through_endpoints must be empty for ycapi-only mode")
    if general_settings.get("always_include_stream_usage") is not True:
        raise ConfigValidationError("general_settings.always_include_stream_usage must be true for billable streams")
    _validate_user_api_key_cache_ttl(general_settings)


def _validate_user_api_key_cache_ttl(general_settings: dict[str, Any]) -> None:
    ttl = general_settings.get("user_api_key_cache_ttl")
    if (
        not isinstance(ttl, (int, float))
        or isinstance(ttl, bool)
        or not math.isfinite(ttl)
        or ttl <= 0
        or ttl > MAX_USER_API_KEY_CACHE_TTL_SECONDS
    ):
        raise ConfigValidationError(
            "general_settings.user_api_key_cache_ttl must be a positive number no greater than "
            f"{MAX_USER_API_KEY_CACHE_TTL_SECONDS} seconds"
        )


def _validate_positive_number(params: dict[str, Any], model_name: str, field_name: str) -> None:
    value: Any = params
    for part in field_name.split("."):
        if not isinstance(value, dict):
            value = None
            break
        value = value.get(part)
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ConfigValidationError(f"{model_name}: {field_name} must be a positive number")


def _validate_forbidden_markers(config_path: Path) -> None:
    raw = config_path.read_text(encoding="utf-8")
    for marker in FORBIDDEN_CONFIG_MARKERS:
        if marker in raw:
            raise ConfigValidationError(f"forbidden direct-provider marker in config: {marker}")


def validate_aimanager_config(config_path: Path | str) -> None:
    path = Path(config_path)
    config = _load_yaml(path)
    _validate_forbidden_markers(path)
    _validate_env_refs(config)
    _validate_model_list(config)
    _validate_general_settings(config)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate AiManager LiteLLM config invariants.")
    parser.add_argument("config", type=Path, help="Path to aimanager/config.yaml")
    args = parser.parse_args(argv)

    try:
        validate_aimanager_config(args.config)
    except ConfigValidationError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    print(f"PASS: {args.config} is locked to ycapi")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

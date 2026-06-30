from __future__ import annotations

import os
import sys
from os import PathLike
from pathlib import Path
from math import isfinite
from typing import Any, Callable, Mapping, MutableMapping, Sequence


AIMANAGER_ASGI_APP = "aimanager.asgi:app"
CONFIG_FILE_PATH_ENV = "CONFIG_FILE_PATH"
LOCAL_MODEL_COST_MAP_ENV = "LITELLM_LOCAL_MODEL_COST_MAP"
REQUIRED_RUNTIME_ENV = (
    "LITELLM_MASTER_KEY",
    "YCAPI_BASE_URL",
    "YCAPI_API_TOKEN",
)


class RuntimeEnvironmentError(RuntimeError):
    """Raised when AiManager cannot safely start with the current environment."""


def install_aimanager_app_override(proxy_cli_module: Any) -> None:
    helpers = proxy_cli_module.ProxyInitializationHelpers
    original_get_uvicorn_args = helpers._get_default_unvicorn_init_args

    def _get_default_unvicorn_init_args(*args: Any, **kwargs: Any) -> dict[str, Any]:
        uvicorn_args = dict(original_get_uvicorn_args(*args, **kwargs))
        uvicorn_args["app"] = AIMANAGER_ASGI_APP
        return uvicorn_args

    helpers._get_default_unvicorn_init_args = staticmethod(_get_default_unvicorn_init_args)


def validate_required_runtime_env(env: Mapping[str, str | None]) -> None:
    missing = [name for name in REQUIRED_RUNTIME_ENV if not (env.get(name) or "").strip()]
    if missing:
        raise RuntimeEnvironmentError(
            "AiManager runtime requires non-empty environment variable(s): " + ", ".join(missing)
        )


def configure_litellm_startup_environment(
    env: MutableMapping[str, str],
    argv: Sequence[str] | None = None,
) -> None:
    env[LOCAL_MODEL_COST_MAP_ENV] = "True"
    config_path = _config_path_from_argv(argv or ())
    if config_path and not (env.get(CONFIG_FILE_PATH_ENV) or "").strip():
        env[CONFIG_FILE_PATH_ENV] = config_path


def register_aimanager_image_model_costs(
    config_path: str | PathLike[str] | None = None,
    *,
    register_model: Callable[..., None] | None = None,
) -> dict[str, dict[str, Any]]:
    model_cost = _load_image_generation_model_costs(config_path)
    if not model_cost:
        return {}

    if register_model is None:
        from litellm import register_model as register_model

    register_model(model_cost=model_cost)
    return model_cost


def register_aimanager_enforced_params_guard(*, litellm_module: Any | None = None) -> object:
    if litellm_module is None:
        import litellm as litellm_module

    from aimanager.enforced_params_guard import AiManagerEnforcedParamsGuard

    callbacks = getattr(litellm_module, "callbacks", None)
    if isinstance(callbacks, list):
        for callback in callbacks:
            if isinstance(callback, AiManagerEnforcedParamsGuard):
                return callback

    guard = AiManagerEnforcedParamsGuard()
    manager = getattr(litellm_module, "logging_callback_manager", None)
    if manager is not None and hasattr(manager, "add_litellm_callback"):
        manager.add_litellm_callback(guard)
    elif isinstance(callbacks, list):
        callbacks.append(guard)
    else:
        raise RuntimeEnvironmentError("LiteLLM callback registry is not available for AiManager startup")
    return guard


def main(argv: Sequence[str] | None = None) -> Any:
    cli_args = list(sys.argv[1:] if argv is None else argv)
    configure_litellm_startup_environment(os.environ, cli_args)
    if not _is_metadata_command(cli_args):
        try:
            validate_required_runtime_env(os.environ)
        except RuntimeEnvironmentError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(2) from exc

    from litellm.proxy import proxy_cli

    install_aimanager_app_override(proxy_cli)
    return proxy_cli.run_server.main(
        args=cli_args,
        prog_name="aimanager-litellm",
    )


def _is_metadata_command(argv: Sequence[str]) -> bool:
    return any(arg in {"--help", "-h", "--version", "-v"} for arg in argv)


def _config_path_from_argv(argv: Sequence[str]) -> str:
    for index, arg in enumerate(argv):
        if arg.startswith("--config="):
            return arg.split("=", 1)[1].strip()
        if arg == "--config" and index + 1 < len(argv):
            return argv[index + 1].strip()
    return ""


def _load_image_generation_model_costs(
    config_path: str | PathLike[str] | None = None,
) -> dict[str, dict[str, Any]]:
    resolved_path = Path(config_path or os.environ.get(CONFIG_FILE_PATH_ENV, ""))
    if not str(resolved_path):
        return {}
    if not resolved_path.exists():
        raise RuntimeEnvironmentError(
            f"AiManager image cost registration requires readable {CONFIG_FILE_PATH_ENV}: {resolved_path}"
        )

    import yaml

    config = yaml.safe_load(resolved_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        return {}

    model_cost: dict[str, dict[str, Any]] = {}
    for item in config.get("model_list") or []:
        if not isinstance(item, dict):
            continue
        params = item.get("litellm_params")
        model_info = item.get("model_info")
        if not isinstance(params, dict) or not isinstance(model_info, dict):
            continue
        if model_info.get("mode") != "image_generation":
            continue
        model = params.get("model")
        cost = model_info.get("input_cost_per_image")
        if (
            isinstance(model, str)
            and model.strip()
            and isinstance(cost, int | float)
            and not isinstance(cost, bool)
            and isfinite(cost)
            and cost > 0
        ):
            model_cost[model.strip()] = {
                "mode": "image_generation",
                "input_cost_per_image": float(cost),
            }
    return model_cost


if __name__ == "__main__":
    main()

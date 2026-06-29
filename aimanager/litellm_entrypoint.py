from __future__ import annotations

import os
import sys
from typing import Any, Mapping, MutableMapping, Sequence


AIMANAGER_ASGI_APP = "aimanager.asgi:app"
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


def configure_litellm_startup_environment(env: MutableMapping[str, str]) -> None:
    env[LOCAL_MODEL_COST_MAP_ENV] = "True"


def main(argv: Sequence[str] | None = None) -> Any:
    cli_args = list(sys.argv[1:] if argv is None else argv)
    configure_litellm_startup_environment(os.environ)
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


if __name__ == "__main__":
    main()

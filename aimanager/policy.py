from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


RouteSurface = Literal["business", "management"]


ALLOWED_BUSINESS_ROUTES = {
    ("GET", "/v1/models"),
    ("POST", "/v1/chat/completions"),
    ("POST", "/v1/images/generations"),
    ("POST", "/v1/videos"),
}

VIDEO_READ_RESERVED_SEGMENTS = frozenset({"characters", "edits", "extensions"})

ALLOWED_HEALTH_ROUTES = {
    ("GET", "/health"),
    ("GET", "/health/liveliness"),
    ("GET", "/health/readiness"),
}

PROVIDER_PASSTHROUGH_PREFIXES = (
    "/anthropic",
    "/gemini",
    "/bedrock",
    "/openai",
    "/openai_passthrough",
    "/cohere",
    "/vllm",
    "/mistral",
    "/azure",
    "/azure_ai",
    "/watsonx",
    "/cursor",
    "/vertex_ai",
    "/vertex-ai",
)

CONFIG_IMMUTABLE_PREFIXES = (
    "/pass-through-endpoints",
    "/config/update",
    "/config/field/update",
    "/config/field/delete",
    "/config/callback/delete",
    "/config/cost_margin_config",
    "/config/cost_discount_config",
    "/config_overrides",
    "/cache/settings",
    "/reload",
)

MANAGEMENT_ROUTE_PREFIXES = (
    "/ui",
    "/login",
    "/v2/login",
    "/v3/login",
    "/fallback/login",
    "/onboarding",
    "/get_logo_url",
    "/get_image",
    "/get_favicon",
    "/key",
    "/team",
    "/user",
    "/customer",
    "/organization",
    "/budget",
    "/spend",
    "/global/spend",
    "/global/activity",
)

MANAGEMENT_STATIC_PREFIXES = (
    "/litellm-asset-prefix/_next/static",
    "/_next/static",
)

MANAGEMENT_EXACT_ROUTES = {
    ("GET", "/"),
    ("HEAD", "/"),
    ("GET", "/__next._tree.txt"),
    ("GET", "/metrics"),
    ("GET", "/config/yaml"),
    ("GET", "/config/list"),
    ("GET", "/config/field/info"),
    ("GET", "/get/ui_settings"),
    ("GET", "/get/ui_theme_settings"),
    ("GET", "/health/license"),
    ("GET", "/health/readiness/details"),
    ("GET", "/litellm/.well-known/litellm-ui-config"),
    ("GET", "/model/info"),
    ("GET", "/model_group/info"),
    ("GET", "/model_group/list"),
    ("GET", "/models"),
    ("GET", "/public/litellm_blog_posts"),
    ("GET", "/sso/get/ui_settings"),
    ("GET", "/tag/list"),
    ("GET", "/project/list"),
    ("GET", "/v2/user/info"),
    ("GET", "/v2/team/list"),
    ("GET", "/v2/guardrails/list"),
    ("GET", "/guardrails/list"),
    ("GET", "/v1/agents"),
    ("GET", "/policies/list"),
    ("GET", "/prompts/list"),
    ("GET", "/api/plugins"),
    ("POST", "/v2/key/info"),
}

MODEL_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@dataclass(frozen=True)
class RouteDecision:
    allowed: bool
    code: str
    message: str
    status_code: int = 403


def evaluate_route(
    method: str,
    path: str,
    *,
    surface: str = "business",
) -> RouteDecision:
    normalized_method = method.upper()
    normalized_path = _normalize_path(path)
    normalized_surface = _normalize_surface(surface)

    if (normalized_method, normalized_path) in ALLOWED_BUSINESS_ROUTES:
        return _allowed()
    if (normalized_method, normalized_path) in ALLOWED_HEALTH_ROUTES:
        return _allowed()
    if is_business_video_read_route(normalized_method, normalized_path):
        return _allowed()

    if _is_google_native_route(normalized_path):
        return RouteDecision(
            allowed=False,
            code="aimanager_google_native_blocked",
            message="AiManager blocks Google native model endpoints; use /v1/chat/completions",
        )

    if _starts_with_any(normalized_path, PROVIDER_PASSTHROUGH_PREFIXES):
        return RouteDecision(
            allowed=False,
            code="aimanager_passthrough_blocked",
            message="AiManager blocks provider passthrough routes; use ycapi-backed /v1 endpoints",
        )

    if _starts_with_any(normalized_path, CONFIG_IMMUTABLE_PREFIXES):
        return _config_immutable()

    if _is_model_write(normalized_method, normalized_path):
        return _config_immutable()

    if normalized_surface == "management" and _is_management_route(
        normalized_method,
        normalized_path,
    ):
        return _allowed()

    return RouteDecision(
        allowed=False,
        code="aimanager_route_not_allowed",
        message=f"Route {normalized_method} {normalized_path} is not allowed by AiManager M1 policy",
    )


def build_policy_error_body(decision: RouteDecision, request_id: str) -> dict[str, object]:
    return {
        "error": {
            "message": decision.message,
            "type": "permission_error",
            "param": None,
            "code": decision.code,
        },
        "request_id": request_id,
    }


def _allowed() -> RouteDecision:
    return RouteDecision(allowed=True, code="ok", message="allowed", status_code=200)


def _config_immutable() -> RouteDecision:
    return RouteDecision(
        allowed=False,
        code="aimanager_config_immutable",
        message="AiManager policy blocks runtime configuration and model writes",
    )


def _normalize_path(path: str) -> str:
    normalized = path.split("?", 1)[0].strip() or "/"
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    if len(normalized) > 1:
        normalized = normalized.rstrip("/")
    return normalized


def _normalize_surface(surface: str) -> RouteSurface:
    if surface == "management":
        return "management"
    return "business"


def is_business_video_read_route(method: str, path: str) -> bool:
    if method.upper() != "GET":
        return False
    normalized = _normalize_path(path)
    if not normalized.startswith("/v1/videos/"):
        return False
    segments = normalized[len("/v1/videos/") :].split("/")
    head = segments[0]
    if not head or head in VIDEO_READ_RESERVED_SEGMENTS:
        return False
    if len(segments) == 1:
        return True
    return len(segments) == 2 and segments[1] == "content"


def _is_google_native_route(path: str) -> bool:
    if ":generateContent" not in path and ":streamGenerateContent" not in path:
        return False
    return path.startswith("/v1beta/models/") or path.startswith("/models/")


def _starts_with_any(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in prefixes)


def _is_model_write(method: str, path: str) -> bool:
    if method not in MODEL_WRITE_METHODS:
        return False
    return path == "/model" or path.startswith("/model/")


def _is_management_route(method: str, path: str) -> bool:
    if method == "GET" and _starts_with_any(path, MANAGEMENT_STATIC_PREFIXES):
        return True
    if _starts_with_any(path, MANAGEMENT_ROUTE_PREFIXES):
        return True
    return (method, path) in MANAGEMENT_EXACT_ROUTES

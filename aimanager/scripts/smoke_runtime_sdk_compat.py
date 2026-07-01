from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

from aimanager.scripts.smoke_sdk_compat import (
    DEFAULT_CHAT_MODELS,
    DEFAULT_EMPLOYEE_KEY_ENV,
    DEFAULT_IMAGE_MODEL,
    DEFAULT_VISION_MODEL,
    HttpResponse,
    SdkCompatSmokeResult,
    SdkCompatStatus,
    run_sdk_compat_smoke,
)


DEFAULT_BUSINESS_BASE_URL = "http://localhost:4000"
DEFAULT_ADMIN_BASE_URL = "http://localhost:4001"
DEFAULT_TEAM_ID = "team_aimanager_sdk_smoke"
DEFAULT_TEAM_ALIAS = "AiManager SDK Smoke Team"
DEFAULT_USER_ID = "aimanager-sdk-smoke-user"

_MARKER_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]+$")
_ADMIN_RBAC_ROLE = "proxy_admin"
_ADMIN_ACTOR = "aimanager-ci"
_CLEANUP_REASON = "AiManager runtime SDK compatibility smoke cleanup"


CleanupStatus = Literal["PASS", "FAIL", "SKIP"]
Fetch = Callable[[str, str, dict[str, str], bytes | None], HttpResponse]
SdkSmokeRunner = Callable[..., SdkCompatSmokeResult]


@dataclass(frozen=True)
class RuntimeSdkCompatSmokeResult:
    status: SdkCompatStatus
    detail: str
    key_alias: str
    cleanup_status: CleanupStatus
    checked_endpoints: tuple[str, ...] = ()
    status_code: int | None = None


class RuntimeSdkCompatSmokeError(RuntimeError):
    def __init__(self, detail: str, *, status_code: int | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def run_runtime_sdk_compat_smoke(
    *,
    business_base_url: str,
    admin_base_url: str,
    master_key: str,
    request_marker: str,
    chat_models: Sequence[str] = DEFAULT_CHAT_MODELS,
    image_model: str = DEFAULT_IMAGE_MODEL,
    vision_model: str = DEFAULT_VISION_MODEL,
    include_image: bool = True,
    include_vision: bool = True,
    team_id: str = DEFAULT_TEAM_ID,
    team_alias: str = DEFAULT_TEAM_ALIAS,
    timeout_seconds: float = 30,
    fetch: Fetch | None = None,
    sdk_smoke_runner: SdkSmokeRunner | None = None,
) -> RuntimeSdkCompatSmokeResult:
    key_alias = f"aimanager-sdk-smoke-{request_marker}"
    if not master_key.strip():
        return RuntimeSdkCompatSmokeResult(
            status="BLOCKED",
            detail="missing LITELLM_MASTER_KEY or --master-key",
            key_alias=key_alias,
            cleanup_status="SKIP",
        )
    _validate_marker(request_marker)

    fetcher = fetch or _fetch_with_urllib(timeout_seconds=timeout_seconds)
    sdk_runner = sdk_smoke_runner or run_sdk_compat_smoke
    virtual_key: str | None = None
    cleanup_status: CleanupStatus = "SKIP"
    cleanup_detail = ""
    checked_endpoints: tuple[str, ...] = ()
    sdk_result: SdkCompatSmokeResult | None = None
    early_result: RuntimeSdkCompatSmokeResult | None = None

    try:
        key_models = _models_for_key(
            chat_models=chat_models,
            image_model=image_model,
            vision_model=vision_model,
            include_image=include_image,
            include_vision=include_vision,
        )
        _ensure_runtime_smoke_team(
            fetcher=fetcher,
            admin_base_url=admin_base_url,
            master_key=master_key,
            team_id=team_id,
            team_alias=team_alias,
            models=key_models,
            request_marker=request_marker,
        )
        key_payload = build_runtime_sdk_key_payload(
            request_marker=request_marker,
            key_alias=key_alias,
            team_id=team_id,
            models=key_models,
        )
        key_response = _request_json(
            fetcher=fetcher,
            method="POST",
            url=_join_url(admin_base_url, "/key/generate"),
            headers=_admin_headers(master_key, request_id=f"runtime-sdk-key-{request_marker}", has_body=True),
            payload=key_payload,
        )
        virtual_key = _extract_virtual_key(key_response)

        sdk_result = sdk_runner(
            base_url=business_base_url,
            employee_key=virtual_key,
            employee_key_env_name=DEFAULT_EMPLOYEE_KEY_ENV,
            request_marker=request_marker,
            chat_models=tuple(model for model in chat_models if model.strip()),
            image_model=image_model,
            vision_model=vision_model,
            include_image=include_image,
            include_vision=include_vision,
            timeout_seconds=timeout_seconds,
        )
        checked_endpoints = sdk_result.checked_endpoints
    except RuntimeSdkCompatSmokeError as exc:
        if exc.status_code in {502, 503, 504}:
            early_result = RuntimeSdkCompatSmokeResult(
                status="BLOCKED",
                detail=f"admin surface unavailable: {exc.detail}",
                key_alias=key_alias,
                cleanup_status=cleanup_status,
                checked_endpoints=checked_endpoints,
                status_code=exc.status_code,
            )
        else:
            early_result = RuntimeSdkCompatSmokeResult(
                status="FAIL",
                detail=exc.detail,
                key_alias=key_alias,
                cleanup_status=cleanup_status,
                checked_endpoints=checked_endpoints,
                status_code=exc.status_code,
            )
    except (OSError, ValueError) as exc:
        early_result = RuntimeSdkCompatSmokeResult(
            status="BLOCKED",
            detail=f"admin surface unavailable: {type(exc).__name__}",
            key_alias=key_alias,
            cleanup_status=cleanup_status,
            checked_endpoints=checked_endpoints,
        )
    finally:
        if virtual_key:
            cleanup_status, cleanup_detail = _delete_disposable_key(
                fetcher=fetcher,
                admin_base_url=admin_base_url,
                master_key=master_key,
                virtual_key=virtual_key,
                request_marker=request_marker,
            )

    if early_result is not None:
        if cleanup_status == "FAIL":
            return RuntimeSdkCompatSmokeResult(
                status="FAIL",
                detail=f"{early_result.detail}; disposable key cleanup failed: {cleanup_detail}",
                key_alias=key_alias,
                cleanup_status=cleanup_status,
                checked_endpoints=early_result.checked_endpoints,
                status_code=early_result.status_code,
            )
        return RuntimeSdkCompatSmokeResult(
            status=early_result.status,
            detail=early_result.detail,
            key_alias=key_alias,
            cleanup_status=cleanup_status,
            checked_endpoints=early_result.checked_endpoints,
            status_code=early_result.status_code,
        )

    if sdk_result is None:
        return RuntimeSdkCompatSmokeResult(
            status="FAIL",
            detail="SDK compatibility smoke did not run",
            key_alias=key_alias,
            cleanup_status=cleanup_status,
        )

    result_status = sdk_result.status
    result_detail = sdk_result.detail
    if _is_business_surface_unavailable(sdk_result):
        result_status = "BLOCKED"
        result_detail = f"business surface unavailable: {sdk_result.detail}"

    if cleanup_status == "FAIL":
        if result_status == "PASS":
            return RuntimeSdkCompatSmokeResult(
                status="FAIL",
                detail=f"SDK compatibility passed but disposable key cleanup failed: {cleanup_detail}",
                key_alias=key_alias,
                cleanup_status=cleanup_status,
                checked_endpoints=checked_endpoints,
                status_code=sdk_result.status_code,
            )
        return RuntimeSdkCompatSmokeResult(
            status="FAIL",
            detail=f"{result_detail}; disposable key cleanup failed: {cleanup_detail}",
            key_alias=key_alias,
            cleanup_status=cleanup_status,
            checked_endpoints=checked_endpoints,
            status_code=sdk_result.status_code,
        )

    return RuntimeSdkCompatSmokeResult(
        status=result_status,
        detail=result_detail,
        key_alias=key_alias,
        cleanup_status=cleanup_status,
        checked_endpoints=checked_endpoints,
        status_code=sdk_result.status_code,
    )


def build_runtime_sdk_key_payload(
    *,
    request_marker: str,
    key_alias: str,
    team_id: str,
    models: Sequence[str],
) -> dict[str, Any]:
    _validate_marker(request_marker)
    return {
        "key_alias": key_alias,
        "user_id": DEFAULT_USER_ID,
        "team_id": team_id,
        "models": list(models),
        "max_budget": 2.0,
        "rpm_limit": 60,
        "tpm_limit": 120000,
        "duration": "1h",
        "metadata": {
            "owner": "aimanager-sdk-smoke",
            "department_id": "dept_smoke",
            "project_id": "proj_aimanager_runtime_smoke",
            "cost_center_id": "cc_smoke",
            "scenario_l1": "engineering",
            "scenario_l2": "code_assist",
            "approver": "aimanager-ci",
            "internal_or_external": "internal",
            "end_user_principal": "employee-smoke-001",
            "aimanager_smoke_id": request_marker,
        },
    }


def _ensure_runtime_smoke_team(
    *,
    fetcher: Fetch,
    admin_base_url: str,
    master_key: str,
    team_id: str,
    team_alias: str,
    models: Sequence[str],
    request_marker: str,
) -> None:
    query = urllib.parse.urlencode({"team_id": team_id})
    response = fetcher(
        "GET",
        f"{_join_url(admin_base_url, '/team/info')}?{query}",
        _admin_headers(master_key, request_id=f"runtime-sdk-team-info-{request_marker}", has_body=False),
        None,
    )
    if 200 <= response.status_code < 300:
        return
    if response.status_code not in {400, 404}:
        raise RuntimeSdkCompatSmokeError(
            f"team info returned HTTP {response.status_code}",
            status_code=response.status_code,
        )
    _request_json(
        fetcher=fetcher,
        method="POST",
        url=_join_url(admin_base_url, "/team/new"),
        headers=_admin_headers(master_key, request_id=f"runtime-sdk-team-new-{request_marker}", has_body=True),
        payload={
            "team_id": team_id,
            "team_alias": team_alias,
            "models": list(models),
            "max_budget": 100,
            "rpm_limit": 600,
            "tpm_limit": 120000,
        },
    )


def _delete_disposable_key(
    *,
    fetcher: Fetch,
    admin_base_url: str,
    master_key: str,
    virtual_key: str,
    request_marker: str,
) -> tuple[CleanupStatus, str]:
    try:
        response = fetcher(
            "POST",
            _join_url(admin_base_url, "/key/delete"),
            _admin_headers(
                master_key,
                request_id=f"runtime-sdk-key-delete-{request_marker}",
                has_body=True,
                cleanup=True,
            ),
            json.dumps({"keys": [virtual_key]}).encode("utf-8"),
        )
    except (OSError, ValueError, http.client.HTTPException) as exc:
        return "FAIL", type(exc).__name__
    if 200 <= response.status_code < 300:
        return "PASS", ""
    return "FAIL", f"HTTP {response.status_code}"


def _request_json(
    *,
    fetcher: Fetch,
    method: str,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> dict[str, Any]:
    response = fetcher(method, url, headers, json.dumps(payload).encode("utf-8"))
    if response.status_code < 200 or response.status_code >= 300:
        raise RuntimeSdkCompatSmokeError(
            f"{method} {url} returned HTTP {response.status_code}",
            status_code=response.status_code,
        )
    try:
        parsed = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeSdkCompatSmokeError(f"{method} {url} did not return JSON") from exc
    if not isinstance(parsed, dict):
        raise RuntimeSdkCompatSmokeError(f"{method} {url} returned non-object JSON")
    return parsed


def _extract_virtual_key(payload: dict[str, Any]) -> str:
    value = payload.get("key") or payload.get("token")
    if not isinstance(value, str) or not value.strip():
        raise RuntimeSdkCompatSmokeError("key generation response did not include a virtual key")
    return value


def _models_for_key(
    *,
    chat_models: Sequence[str],
    image_model: str,
    vision_model: str,
    include_image: bool,
    include_vision: bool,
) -> tuple[str, ...]:
    models = [model for model in chat_models if model.strip()]
    if include_image and image_model.strip():
        models.append(image_model)
    if include_vision and vision_model.strip():
        models.append(vision_model)
    return tuple(dict.fromkeys(models))


def _is_business_surface_unavailable(result: SdkCompatSmokeResult) -> bool:
    return (
        result.status == "FAIL"
        and not result.checked_endpoints
        and result.status_code is None
        and result.detail.startswith("models request failed:")
    )


def _admin_headers(
    master_key: str,
    *,
    request_id: str,
    has_body: bool,
    cleanup: bool = False,
) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {master_key}",
        "Accept": "application/json",
        "x-request-id": request_id,
        "x-aimanager-role": _ADMIN_RBAC_ROLE,
        "x-aimanager-actor": _ADMIN_ACTOR,
    }
    if has_body:
        headers["Content-Type"] = "application/json"
    if cleanup:
        headers.update(
            {
                "litellm-changed-by": _ADMIN_ACTOR,
                "x-aimanager-reason": _CLEANUP_REASON,
                "x-aimanager-disposition-reason": _CLEANUP_REASON,
            }
        )
    return headers


def _fetch_with_urllib(*, timeout_seconds: float) -> Fetch:
    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                return HttpResponse(
                    status_code=response.status,
                    headers=dict(response.headers.items()),
                    body=response.read(),
                )
        except urllib.error.HTTPError as exc:
            return HttpResponse(
                status_code=exc.code,
                headers=dict(exc.headers.items()),
                body=exc.read(),
            )

    return fetch


def _join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _validate_marker(marker: str) -> None:
    if not marker or not _MARKER_PATTERN.fullmatch(marker):
        raise ValueError("request_marker must contain only letters, digits, underscore, dot, colon, or hyphen")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create a disposable employee key and smoke test SDK compatibility through AiManager."
    )
    parser.add_argument(
        "--business-base-url",
        default=os.environ.get("AIMANAGER_BASE_URL", DEFAULT_BUSINESS_BASE_URL),
    )
    parser.add_argument(
        "--admin-base-url",
        default=os.environ.get("AIMANAGER_ADMIN_BASE_URL", DEFAULT_ADMIN_BASE_URL),
    )
    parser.add_argument("--master-key", default=os.environ.get("LITELLM_MASTER_KEY", ""))
    parser.add_argument("--request-marker", default=f"aimanager-runtime-sdk-{uuid4().hex[:12]}")
    parser.add_argument(
        "--chat-model",
        action="append",
        default=[],
        help="Chat model to verify. Repeat for multiple models. Defaults to M1 chat models.",
    )
    parser.add_argument("--image-model", default=DEFAULT_IMAGE_MODEL)
    parser.add_argument("--vision-model", default=DEFAULT_VISION_MODEL)
    parser.add_argument("--skip-image", action="store_true")
    parser.add_argument("--skip-vision", action="store_true")
    parser.add_argument("--team-id", default=DEFAULT_TEAM_ID)
    parser.add_argument("--team-alias", default=DEFAULT_TEAM_ALIAS)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args(argv)

    chat_models = tuple(args.chat_model) if args.chat_model else DEFAULT_CHAT_MODELS
    try:
        result = run_runtime_sdk_compat_smoke(
            business_base_url=args.business_base_url,
            admin_base_url=args.admin_base_url,
            master_key=args.master_key,
            request_marker=args.request_marker,
            chat_models=chat_models,
            image_model=args.image_model,
            vision_model=args.vision_model,
            include_image=not args.skip_image,
            include_vision=not args.skip_vision,
            team_id=args.team_id,
            team_alias=args.team_alias,
            timeout_seconds=args.timeout,
        )
    except Exception as exc:
        print(f"FAIL runtime SDK compatibility smoke: {type(exc).__name__}", file=sys.stderr)
        return 1

    endpoints = ",".join(result.checked_endpoints) if result.checked_endpoints else "-"
    print(
        f"{result.status} runtime SDK compatibility smoke: "
        f"key_alias={result.key_alias} cleanup={result.cleanup_status} "
        f"endpoints={endpoints} status_code={result.status_code or '-'} {result.detail}"
    )
    if result.status == "FAIL":
        return 1
    if result.status == "BLOCKED":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal


SdkCompatStatus = Literal["PASS", "FAIL", "BLOCKED"]
DEFAULT_BASE_URL = "http://localhost:4000"
DEFAULT_EMPLOYEE_KEY_ENV = "AIMANAGER_EMPLOYEE_VIRTUAL_KEY"
DEFAULT_CHAT_MODEL = "gemini-2.5-flash"
DEFAULT_CHAT_MODELS = (DEFAULT_CHAT_MODEL, "deepseek-chat")
DEFAULT_IMAGE_MODEL = "ycapi-image-1"
DEFAULT_VISION_MODEL = DEFAULT_CHAT_MODEL
_MARKER_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]+$")
_SMOKE_DATA_IMAGE_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True)
class SdkCompatSmokeResult:
    status: SdkCompatStatus
    detail: str
    checked_endpoints: tuple[str, ...] = ()
    status_code: int | None = None


Fetch = Callable[[str, str, dict[str, str], bytes | None], HttpResponse]


def run_sdk_compat_smoke(
    *,
    base_url: str,
    employee_key: str,
    employee_key_env_name: str = DEFAULT_EMPLOYEE_KEY_ENV,
    request_marker: str = "sdk-smoke",
    chat_models: Sequence[str] = DEFAULT_CHAT_MODELS,
    image_model: str = DEFAULT_IMAGE_MODEL,
    vision_model: str = DEFAULT_VISION_MODEL,
    include_image: bool = True,
    include_vision: bool = True,
    fetch: Fetch | None = None,
    timeout_seconds: float = 10,
) -> SdkCompatSmokeResult:
    if not employee_key.strip():
        return SdkCompatSmokeResult(
            status="BLOCKED",
            detail=f"missing {employee_key_env_name}; cannot verify employee SDK compatibility",
        )
    _validate_marker(request_marker)

    fetcher = fetch or _fetch_with_urllib(timeout_seconds=timeout_seconds)
    checked: list[str] = []

    models_response = _request(
        fetcher,
        method="GET",
        url=_join_url(base_url, "/v1/models"),
        employee_key=employee_key,
        request_id=f"models-{request_marker}",
        body=None,
        endpoint="models",
    )
    if isinstance(models_response, SdkCompatSmokeResult):
        return models_response
    checked.append("models")
    if models_response.status_code != 200:
        return SdkCompatSmokeResult(
            status="FAIL",
            detail=f"models request returned HTTP {models_response.status_code}",
            checked_endpoints=tuple(checked),
            status_code=models_response.status_code,
        )

    observed_models = _extract_model_ids(models_response.body)
    normalized_chat_models = tuple(model for model in chat_models if model.strip())
    expected_models = list(dict.fromkeys([*normalized_chat_models]))
    if include_image:
        expected_models.append(image_model)
    if include_vision and vision_model not in expected_models:
        expected_models.append(vision_model)
    missing_models = [model for model in expected_models if model not in observed_models]
    if missing_models:
        return SdkCompatSmokeResult(
            status="FAIL",
            detail=f"missing model(s): {', '.join(missing_models)}",
            checked_endpoints=tuple(checked),
            status_code=models_response.status_code,
        )

    for chat_model in normalized_chat_models:
        endpoint = f"chat:{chat_model}"
        chat_response = _request(
            fetcher,
            method="POST",
            url=_join_url(base_url, "/v1/chat/completions"),
            employee_key=employee_key,
            request_id=f"chat-{chat_model}-{request_marker}",
            body={
                "model": chat_model,
                "messages": [{"role": "user", "content": "AiManager SDK compatibility smoke"}],
                "max_tokens": 8,
                "user": "employee-smoke-001",
                "metadata": _request_metadata(request_marker, image_count=0),
            },
            endpoint=endpoint,
        )
        if isinstance(chat_response, SdkCompatSmokeResult):
            return chat_response
        checked.append(endpoint)
        if chat_response.status_code != 200:
            return SdkCompatSmokeResult(
                status="FAIL",
                detail=f"chat {chat_model} request returned HTTP {chat_response.status_code}",
                checked_endpoints=tuple(checked),
                status_code=chat_response.status_code,
            )
        chat_shape_error = _chat_shape_error(chat_response.body)
        if chat_shape_error:
            return SdkCompatSmokeResult(
                status="FAIL",
                detail=f"chat {chat_model} response {chat_shape_error}",
                checked_endpoints=tuple(checked),
                status_code=chat_response.status_code,
            )

    if include_image:
        image_response = _request(
            fetcher,
            method="POST",
            url=_join_url(base_url, "/v1/images/generations"),
            employee_key=employee_key,
            request_id=f"image-{request_marker}",
            body={
                "model": image_model,
                "prompt": "AiManager SDK compatibility smoke image",
                "n": 1,
                "size": "1024x1024",
                "response_format": "b64_json",
                "user": "employee-smoke-001",
                "metadata": _request_metadata(request_marker, image_count=1),
            },
            endpoint="image",
        )
        if isinstance(image_response, SdkCompatSmokeResult):
            return image_response
        checked.append("image")
        if image_response.status_code != 200:
            return SdkCompatSmokeResult(
                status="FAIL",
                detail=f"image request returned HTTP {image_response.status_code}",
                checked_endpoints=tuple(checked),
                status_code=image_response.status_code,
            )
        image_shape_error = _image_shape_error(image_response.body)
        if image_shape_error:
            return SdkCompatSmokeResult(
                status="FAIL",
                detail=f"image response {image_shape_error}",
                checked_endpoints=tuple(checked),
                status_code=image_response.status_code,
            )

    if include_vision:
        endpoint = f"vision:{vision_model}"
        vision_response = _request(
            fetcher,
            method="POST",
            url=_join_url(base_url, "/v1/chat/completions"),
            employee_key=employee_key,
            request_id=f"vision-{request_marker}",
            body={
                "model": vision_model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Describe this one-pixel image briefly."},
                            {"type": "image_url", "image_url": {"url": _SMOKE_DATA_IMAGE_URL}},
                        ],
                    }
                ],
                "max_tokens": 8,
                "user": "employee-smoke-001",
                "metadata": _request_metadata(request_marker, image_count=1),
            },
            endpoint=endpoint,
        )
        if isinstance(vision_response, SdkCompatSmokeResult):
            return vision_response
        checked.append(endpoint)
        if vision_response.status_code != 200:
            return SdkCompatSmokeResult(
                status="FAIL",
                detail=f"vision {vision_model} request returned HTTP {vision_response.status_code}",
                checked_endpoints=tuple(checked),
                status_code=vision_response.status_code,
            )
        vision_shape_error = _chat_shape_error(vision_response.body)
        if vision_shape_error:
            return SdkCompatSmokeResult(
                status="FAIL",
                detail=f"vision {vision_model} response {vision_shape_error}",
                checked_endpoints=tuple(checked),
                status_code=vision_response.status_code,
            )

    return SdkCompatSmokeResult(
        status="PASS",
        detail="employee OpenAI-compatible SDK flow succeeded through AiManager",
        checked_endpoints=tuple(checked),
    )


def _request(
    fetcher: Fetch,
    *,
    method: str,
    url: str,
    employee_key: str,
    request_id: str,
    body: dict[str, Any] | None,
    endpoint: str,
) -> HttpResponse | SdkCompatSmokeResult:
    encoded_body = json.dumps(body).encode("utf-8") if body is not None else None
    headers = _request_headers(employee_key, request_id=request_id, has_body=body is not None)
    try:
        return fetcher(method, url, headers, encoded_body)
    except (OSError, ValueError, http.client.HTTPException) as exc:
        return SdkCompatSmokeResult(
            status="FAIL",
            detail=f"{endpoint} request failed: {type(exc).__name__}",
        )


def _request_headers(employee_key: str, *, request_id: str, has_body: bool) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {employee_key}",
        "Accept": "application/json",
        "x-request-id": request_id,
    }
    if has_body:
        headers["Content-Type"] = "application/json"
    return headers


def _request_metadata(marker: str, *, image_count: int) -> dict[str, str | int]:
    return {
        "department_id": "dept_smoke",
        "project_id": "proj_aimanager_runtime_smoke",
        "cost_center_id": "cc_smoke",
        "pricing_version": "m1-runtime-smoke",
        "currency": "CNY",
        "scenario_l1": "engineering",
        "scenario_l2": "sdk-compat-smoke",
        "end_user_principal": "employee-smoke-001",
        "aimanager_smoke_id": marker,
        "image_count": image_count,
    }


def _extract_model_ids(body: bytes) -> tuple[str, ...]:
    try:
        payload: Any = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ()
    if not isinstance(payload, dict):
        return ()
    data = payload.get("data")
    if not isinstance(data, list):
        return ()
    model_ids: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if isinstance(model_id, str) and model_id.strip():
            model_ids.append(model_id)
    return tuple(model_ids)


def _chat_shape_error(body: bytes) -> str:
    try:
        payload: Any = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "returned non-JSON body"
    if not isinstance(payload, dict):
        return "returned non-object body"
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return "missing choices message content"
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return "missing choices message content"
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return "missing choices message content"
    return ""


def _image_shape_error(body: bytes) -> str:
    try:
        payload: Any = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "returned non-JSON body"
    if not isinstance(payload, dict):
        return "returned non-object body"
    data = payload.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return "missing data item"
    item = data[0]
    url = item.get("url")
    b64_json = item.get("b64_json")
    if isinstance(url, str) and url.strip():
        return ""
    if isinstance(b64_json, str) and b64_json.strip():
        return ""
    return "missing url or b64_json"


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
        raise ValueError("request marker must contain only letters, numbers, '.', ':', '_' or '-'")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke test employee SDK compatibility through AiManager.")
    parser.add_argument("--base-url", default=os.environ.get("AIMANAGER_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument(
        "--employee-key-env",
        default=DEFAULT_EMPLOYEE_KEY_ENV,
        help="Environment variable containing an employee LiteLLM virtual key.",
    )
    parser.add_argument("--request-marker", default="sdk-smoke")
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
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args(argv)
    chat_models = tuple(args.chat_model) if args.chat_model else DEFAULT_CHAT_MODELS

    try:
        result = run_sdk_compat_smoke(
            base_url=args.base_url,
            employee_key=os.environ.get(args.employee_key_env, ""),
            employee_key_env_name=args.employee_key_env,
            request_marker=args.request_marker,
            chat_models=chat_models,
            image_model=args.image_model,
            vision_model=args.vision_model,
            include_image=not args.skip_image,
            include_vision=not args.skip_vision,
            timeout_seconds=args.timeout,
        )
    except Exception as exc:
        print(f"FAIL SDK compatibility smoke: {type(exc).__name__}", file=sys.stderr)
        return 1

    endpoints = ",".join(result.checked_endpoints) if result.checked_endpoints else "-"
    print(
        f"{result.status} SDK compatibility smoke: "
        f"endpoints={endpoints} status_code={result.status_code or '-'} {result.detail}"
    )
    if result.status == "FAIL":
        return 1
    if result.status == "BLOCKED":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

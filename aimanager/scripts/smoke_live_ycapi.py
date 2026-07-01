from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal


LiveYcapiStatus = Literal["PASS", "FAIL", "BLOCKED"]
DEFAULT_YCAPI_BASE_URL = "https://ycapi.ycaicloud.com/v1"
DEFAULT_CHAT_MODEL = "gemini-2.5-flash"
DEFAULT_IMAGE_MODEL = "ycapi-image-1"
_CHAT_REQUEST_ID = "aimanager-live-ycapi-smoke-chat"
_IMAGE_REQUEST_ID = "aimanager-live-ycapi-smoke-image"


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True)
class LiveYcapiSmokeResult:
    status: LiveYcapiStatus
    detail: str
    status_code: int | None = None
    model_count: int = 0
    observed_models: tuple[str, ...] = ()
    inference_checked: bool = False
    chat_status_code: int | None = None
    image_status_code: int | None = None
    chat_usage_present: bool = False
    image_result_count: int = 0
    roundtrip_request_ids: tuple[str, ...] = ()


Fetch = Callable[[str, str, dict[str, str], bytes | None], HttpResponse]


def run_live_ycapi_smoke(
    *,
    base_url: str,
    api_token: str,
    token_env_name: str = "YCAPI_API_TOKEN",
    expected_models: Sequence[str] = (),
    run_inference_roundtrip: bool = False,
    chat_model: str = DEFAULT_CHAT_MODEL,
    image_model: str = DEFAULT_IMAGE_MODEL,
    fetch: Fetch | None = None,
    timeout_seconds: float = 10,
) -> LiveYcapiSmokeResult:
    if not api_token.strip():
        return LiveYcapiSmokeResult(
            status="BLOCKED",
            detail=f"missing {token_env_name}; cannot produce live ycapi evidence",
        )

    fetcher = fetch or _fetch_with_urllib(timeout_seconds=timeout_seconds)
    try:
        response = fetcher(
            "GET",
            _join_url(base_url, "/models"),
            {
                "Authorization": f"Bearer {api_token}",
                "Accept": "application/json",
                "x-request-id": "aimanager-live-ycapi-smoke-models",
            },
            None,
        )
    except (OSError, ValueError, http.client.HTTPException) as exc:
        return LiveYcapiSmokeResult(
            status="FAIL",
            detail=f"ycapi /models request failed: {type(exc).__name__}",
        )

    if response.status_code != 200:
        return LiveYcapiSmokeResult(
            status="FAIL",
            detail=f"ycapi /models returned HTTP {response.status_code}",
            status_code=response.status_code,
        )

    try:
        payload = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return LiveYcapiSmokeResult(
            status="FAIL",
            detail="ycapi /models returned non-JSON response",
            status_code=response.status_code,
        )

    observed_models = _extract_model_ids(payload)
    if not observed_models:
        return LiveYcapiSmokeResult(
            status="FAIL",
            detail="ycapi /models returned no model ids",
            status_code=response.status_code,
        )

    expected = tuple(model for model in expected_models if model.strip())
    missing = tuple(model for model in expected if model not in observed_models)
    if missing:
        return LiveYcapiSmokeResult(
            status="FAIL",
            detail=f"missing expected model(s): {', '.join(missing)}",
            status_code=response.status_code,
            model_count=len(observed_models),
            observed_models=observed_models,
        )

    if run_inference_roundtrip:
        roundtrip_result = _run_inference_roundtrip(
            base_url=base_url,
            api_token=api_token,
            fetcher=fetcher,
            model_status_code=response.status_code,
            model_count=len(observed_models),
            observed_models=observed_models,
            chat_model=chat_model,
            image_model=image_model,
        )
        if roundtrip_result is not None:
            return roundtrip_result

    return LiveYcapiSmokeResult(
        status="PASS",
        detail=f"ycapi /models returned {len(observed_models)} model(s)",
        status_code=response.status_code,
        model_count=len(observed_models),
        observed_models=observed_models,
    )


def _run_inference_roundtrip(
    *,
    base_url: str,
    api_token: str,
    fetcher: Fetch,
    model_status_code: int,
    model_count: int,
    observed_models: tuple[str, ...],
    chat_model: str,
    image_model: str,
) -> LiveYcapiSmokeResult | None:
    request_ids: list[str] = []
    chat_status_code: int | None = None
    image_status_code: int | None = None
    chat_usage_present = False
    image_result_count = 0
    if chat_model not in observed_models:
        return _roundtrip_result(
            status="FAIL",
            detail=f"ycapi chat roundtrip model is not listed by /models: {chat_model}",
            model_status_code=model_status_code,
            model_count=model_count,
            observed_models=observed_models,
            chat_status_code=chat_status_code,
            image_status_code=image_status_code,
            chat_usage_present=chat_usage_present,
            image_result_count=image_result_count,
            request_ids=(),
        )
    if image_model not in observed_models:
        return _roundtrip_result(
            status="FAIL",
            detail=f"ycapi image roundtrip model is not listed by /models: {image_model}",
            model_status_code=model_status_code,
            model_count=model_count,
            observed_models=observed_models,
            chat_status_code=chat_status_code,
            image_status_code=image_status_code,
            chat_usage_present=chat_usage_present,
            image_result_count=image_result_count,
            request_ids=(),
        )
    try:
        request_ids.append(_CHAT_REQUEST_ID)
        chat_response = fetcher(
            "POST",
            _join_url(base_url, "/chat/completions"),
            {
                "Authorization": f"Bearer {api_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "x-request-id": _CHAT_REQUEST_ID,
            },
            _json_bytes(
                {
                    "model": chat_model,
                    "messages": [
                        {
                            "role": "user",
                            "content": "AiManager live ycapi smoke. Reply with a short acknowledgement.",
                        }
                    ],
                    "max_tokens": 16,
                    "temperature": 0,
                    "stream": False,
                }
            ),
        )
    except (OSError, ValueError, http.client.HTTPException) as exc:
        return _roundtrip_result(
            status="FAIL",
            detail=f"ycapi chat roundtrip request failed: {type(exc).__name__}",
            model_status_code=model_status_code,
            model_count=model_count,
            observed_models=observed_models,
            chat_status_code=chat_status_code,
            image_status_code=image_status_code,
            chat_usage_present=chat_usage_present,
            image_result_count=image_result_count,
            request_ids=tuple(request_ids),
        )
    chat_status_code = chat_response.status_code
    if chat_response.status_code != 200:
        return _roundtrip_result(
            status="FAIL",
            detail=f"ycapi chat roundtrip returned HTTP {chat_response.status_code}",
            model_status_code=model_status_code,
            model_count=model_count,
            observed_models=observed_models,
            chat_status_code=chat_status_code,
            image_status_code=image_status_code,
            chat_usage_present=chat_usage_present,
            image_result_count=image_result_count,
            request_ids=tuple(request_ids),
        )
    chat_payload = _decode_json(chat_response.body)
    if chat_payload is None:
        return _roundtrip_result(
            status="FAIL",
            detail="ycapi chat roundtrip returned non-JSON response",
            model_status_code=model_status_code,
            model_count=model_count,
            observed_models=observed_models,
            chat_status_code=chat_status_code,
            image_status_code=image_status_code,
            chat_usage_present=chat_usage_present,
            image_result_count=image_result_count,
            request_ids=tuple(request_ids),
        )
    chat_usage_present = _has_positive_usage(chat_payload)
    if not chat_usage_present:
        return _roundtrip_result(
            status="FAIL",
            detail="ycapi chat roundtrip did not return usage",
            model_status_code=model_status_code,
            model_count=model_count,
            observed_models=observed_models,
            chat_status_code=chat_status_code,
            image_status_code=image_status_code,
            chat_usage_present=chat_usage_present,
            image_result_count=image_result_count,
            request_ids=tuple(request_ids),
        )

    try:
        request_ids.append(_IMAGE_REQUEST_ID)
        image_response = fetcher(
            "POST",
            _join_url(base_url, "/images/generations"),
            {
                "Authorization": f"Bearer {api_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "x-request-id": _IMAGE_REQUEST_ID,
            },
            _json_bytes(
                {
                    "model": image_model,
                    "prompt": "AiManager live ycapi smoke image.",
                    "n": 1,
                    "size": "512x512",
                    "response_format": "b64_json",
                }
            ),
        )
    except (OSError, ValueError, http.client.HTTPException) as exc:
        return _roundtrip_result(
            status="FAIL",
            detail=f"ycapi image roundtrip request failed: {type(exc).__name__}",
            model_status_code=model_status_code,
            model_count=model_count,
            observed_models=observed_models,
            chat_status_code=chat_status_code,
            image_status_code=image_status_code,
            chat_usage_present=chat_usage_present,
            image_result_count=image_result_count,
            request_ids=tuple(request_ids),
        )
    image_status_code = image_response.status_code
    if image_response.status_code != 200:
        return _roundtrip_result(
            status="FAIL",
            detail=f"ycapi image roundtrip returned HTTP {image_response.status_code}",
            model_status_code=model_status_code,
            model_count=model_count,
            observed_models=observed_models,
            chat_status_code=chat_status_code,
            image_status_code=image_status_code,
            chat_usage_present=chat_usage_present,
            image_result_count=image_result_count,
            request_ids=tuple(request_ids),
        )
    image_payload = _decode_json(image_response.body)
    if image_payload is None:
        return _roundtrip_result(
            status="FAIL",
            detail="ycapi image roundtrip returned non-JSON response",
            model_status_code=model_status_code,
            model_count=model_count,
            observed_models=observed_models,
            chat_status_code=chat_status_code,
            image_status_code=image_status_code,
            chat_usage_present=chat_usage_present,
            image_result_count=image_result_count,
            request_ids=tuple(request_ids),
        )
    image_result_count = _image_result_count(image_payload)
    if image_result_count <= 0:
        return _roundtrip_result(
            status="FAIL",
            detail="ycapi image roundtrip did not return image data",
            model_status_code=model_status_code,
            model_count=model_count,
            observed_models=observed_models,
            chat_status_code=chat_status_code,
            image_status_code=image_status_code,
            chat_usage_present=chat_usage_present,
            image_result_count=image_result_count,
            request_ids=tuple(request_ids),
        )
    return _roundtrip_result(
        status="PASS",
        detail=f"ycapi /models returned {model_count} model(s); chat/image roundtrip passed",
        model_status_code=model_status_code,
        model_count=model_count,
        observed_models=observed_models,
        chat_status_code=chat_status_code,
        image_status_code=image_status_code,
        chat_usage_present=chat_usage_present,
        image_result_count=image_result_count,
        request_ids=tuple(request_ids),
    )


def _roundtrip_result(
    *,
    status: LiveYcapiStatus,
    detail: str,
    model_status_code: int,
    model_count: int,
    observed_models: tuple[str, ...],
    chat_status_code: int | None,
    image_status_code: int | None,
    chat_usage_present: bool,
    image_result_count: int,
    request_ids: tuple[str, ...],
) -> LiveYcapiSmokeResult:
    return LiveYcapiSmokeResult(
        status=status,
        detail=detail,
        status_code=model_status_code,
        model_count=model_count,
        observed_models=observed_models,
        inference_checked=True,
        chat_status_code=chat_status_code,
        image_status_code=image_status_code,
        chat_usage_present=chat_usage_present,
        image_result_count=image_result_count,
        roundtrip_request_ids=request_ids,
    )


def _extract_model_ids(payload: Any) -> tuple[str, ...]:
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


def _decode_json(body: bytes) -> dict[str, Any] | None:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _has_positive_usage(payload: dict[str, Any]) -> bool:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return False
    total_tokens = usage.get("total_tokens")
    return isinstance(total_tokens, int) and total_tokens > 0


def _image_result_count(payload: dict[str, Any]) -> int:
    data = payload.get("data")
    if not isinstance(data, list):
        return 0
    result_count = 0
    for item in data:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("b64_json"), str) and item["b64_json"].strip():
            result_count += 1
        elif isinstance(item.get("url"), str) and item["url"].strip():
            result_count += 1
    return result_count


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live preflight for ycapi token reachability.")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("YCAPI_BASE_URL", DEFAULT_YCAPI_BASE_URL),
        help="ycapi OpenAI-compatible base URL.",
    )
    parser.add_argument(
        "--api-token-env",
        default="YCAPI_API_TOKEN",
        help="Environment variable containing the ycapi API token.",
    )
    parser.add_argument(
        "--expect-model",
        action="append",
        default=[],
        help="Model id expected in ycapi /models. Repeat for multiple models.",
    )
    parser.add_argument(
        "--run-inference-roundtrip",
        action="store_true",
        help="Also run one live chat and one live image generation request after /models passes.",
    )
    parser.add_argument("--chat-model", default=DEFAULT_CHAT_MODEL)
    parser.add_argument("--image-model", default=DEFAULT_IMAGE_MODEL)
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--output-json-file", help="Optional path to write the smoke result JSON.")
    args = parser.parse_args(argv)

    try:
        result = run_live_ycapi_smoke(
            base_url=args.base_url,
            api_token=os.environ.get(args.api_token_env, ""),
            token_env_name=args.api_token_env,
            expected_models=args.expect_model,
            run_inference_roundtrip=args.run_inference_roundtrip,
            chat_model=args.chat_model,
            image_model=args.image_model,
            timeout_seconds=args.timeout,
        )
    except Exception as exc:
        print(f"FAIL live ycapi smoke: {type(exc).__name__}", file=sys.stderr)
        return 1

    write_error = ""
    if args.output_json_file:
        try:
            _write_result(Path(args.output_json_file), result)
        except Exception as exc:
            write_error = type(exc).__name__

    print(
        f"{result.status} live ycapi smoke: "
        f"status_code={result.status_code or '-'} model_count={result.model_count} "
        f"inference_checked={str(result.inference_checked).lower()} {result.detail}"
    )
    if write_error:
        print(f"WARN live ycapi smoke result file not written: {write_error}", file=sys.stderr)
    if result.status == "FAIL":
        return 1
    if result.status == "BLOCKED":
        return 2
    if write_error:
        return 1
    return 0


def _write_result(path: Path, result: LiveYcapiSmokeResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())

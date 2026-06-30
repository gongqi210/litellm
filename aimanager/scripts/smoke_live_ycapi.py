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


Fetch = Callable[[str, str, dict[str, str], bytes | None], HttpResponse]


def run_live_ycapi_smoke(
    *,
    base_url: str,
    api_token: str,
    token_env_name: str = "YCAPI_API_TOKEN",
    expected_models: Sequence[str] = (),
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

    return LiveYcapiSmokeResult(
        status="PASS",
        detail=f"ycapi /models returned {len(observed_models)} model(s)",
        status_code=response.status_code,
        model_count=len(observed_models),
        observed_models=observed_models,
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
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--output-json-file", help="Optional path to write the smoke result JSON.")
    args = parser.parse_args(argv)

    try:
        result = run_live_ycapi_smoke(
            base_url=args.base_url,
            api_token=os.environ.get(args.api_token_env, ""),
            token_env_name=args.api_token_env,
            expected_models=args.expect_model,
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
        f"status_code={result.status_code or '-'} model_count={result.model_count} {result.detail}"
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

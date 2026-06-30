from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BlockedRouteCase:
    method: str
    path: str
    expected_policy_code: str


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True)
class SmokeResult:
    case: BlockedRouteCase
    passed: bool
    detail: str
    status_code: int | None = None
    policy_code: str | None = None


Fetch = Callable[[str, str, dict[str, str], bytes | None], HttpResponse]


BLOCKED_ROUTE_CASES = (
    BlockedRouteCase("POST", "/anthropic/messages", "aimanager_passthrough_blocked"),
    BlockedRouteCase("POST", "/gemini/v1beta/models", "aimanager_passthrough_blocked"),
    BlockedRouteCase("POST", "/bedrock/model", "aimanager_passthrough_blocked"),
    BlockedRouteCase(
        "POST",
        "/openai/deployments/test/chat/completions",
        "aimanager_passthrough_blocked",
    ),
    BlockedRouteCase(
        "POST",
        "/openai_passthrough/v1/chat/completions",
        "aimanager_passthrough_blocked",
    ),
    BlockedRouteCase("POST", "/cohere/chat", "aimanager_passthrough_blocked"),
    BlockedRouteCase("POST", "/vllm/v1/chat/completions", "aimanager_passthrough_blocked"),
    BlockedRouteCase("POST", "/mistral/v1/chat/completions", "aimanager_passthrough_blocked"),
    BlockedRouteCase("POST", "/azure/openai/deployments/test", "aimanager_passthrough_blocked"),
    BlockedRouteCase("POST", "/azure_ai/models/test", "aimanager_passthrough_blocked"),
    BlockedRouteCase("POST", "/watsonx/ml/v1/text/generation", "aimanager_passthrough_blocked"),
    BlockedRouteCase("POST", "/cursor/v1/chat/completions", "aimanager_passthrough_blocked"),
    BlockedRouteCase("POST", "/vertex_ai/discovery/test", "aimanager_passthrough_blocked"),
    BlockedRouteCase("POST", "/vertex-ai/v1/projects/test", "aimanager_passthrough_blocked"),
    BlockedRouteCase(
        "POST",
        "/v1beta/models/gemini-2.5-flash:generateContent",
        "aimanager_google_native_blocked",
    ),
    BlockedRouteCase(
        "POST",
        "/models/gemini-2.5-flash:streamGenerateContent",
        "aimanager_google_native_blocked",
    ),
    BlockedRouteCase("POST", "/pass-through-endpoints", "aimanager_config_immutable"),
    BlockedRouteCase("POST", "/config/update", "aimanager_config_immutable"),
    BlockedRouteCase("POST", "/config/field/update", "aimanager_config_immutable"),
    BlockedRouteCase("DELETE", "/config/field/delete", "aimanager_config_immutable"),
    BlockedRouteCase("DELETE", "/config/callback/delete", "aimanager_config_immutable"),
    BlockedRouteCase("PATCH", "/config/cost_margin_config", "aimanager_config_immutable"),
    BlockedRouteCase("PATCH", "/config/cost_discount_config", "aimanager_config_immutable"),
    BlockedRouteCase("POST", "/config_overrides/hashicorp_vault", "aimanager_config_immutable"),
    BlockedRouteCase(
        "POST",
        "/config_overrides/hashicorp_vault/test_connection",
        "aimanager_config_immutable",
    ),
    BlockedRouteCase("POST", "/cache/settings", "aimanager_config_immutable"),
    BlockedRouteCase("POST", "/reload/model_cost_map", "aimanager_config_immutable"),
    BlockedRouteCase("POST", "/reload/anthropic_beta_headers", "aimanager_config_immutable"),
    BlockedRouteCase("POST", "/model/new", "aimanager_config_immutable"),
    BlockedRouteCase("PATCH", "/model/update", "aimanager_config_immutable"),
    BlockedRouteCase("DELETE", "/model/gemini-2.5-flash", "aimanager_config_immutable"),
    BlockedRouteCase("POST", "/v1/embeddings", "aimanager_route_not_allowed"),
    BlockedRouteCase("POST", "/v1/completions", "aimanager_route_not_allowed"),
    BlockedRouteCase("GET", "/v1/models", "aimanager_business_token_forbidden"),
    BlockedRouteCase("POST", "/v1/chat/completions", "aimanager_business_token_forbidden"),
)


def run_blocked_route_smoke(
    *,
    base_url: str,
    master_key: str,
    cases: Sequence[BlockedRouteCase] = BLOCKED_ROUTE_CASES,
    fetch: Fetch | None = None,
    timeout_seconds: float = 10,
) -> list[SmokeResult]:
    fetcher = fetch or _fetch_with_urllib(timeout_seconds=timeout_seconds)
    results: list[SmokeResult] = []
    for case in cases:
        url = _join_url(base_url, case.path)
        headers = {
            "Authorization": f"Bearer {master_key}",
            "Content-Type": "application/json",
            "x-request-id": f"smoke_{_slug(case.path)}",
        }
        try:
            response = fetcher(case.method, url, headers, _request_body(case.method))
        except OSError as exc:
            results.append(SmokeResult(case, passed=False, detail=f"request failed: {exc}"))
            continue
        results.append(_evaluate_response(case, response))
    return results


def _evaluate_response(case: BlockedRouteCase, response: HttpResponse) -> SmokeResult:
    policy_code = _header(response.headers, "x-aimanager-policy-code")
    body_policy_code = _body_policy_code(response.body)
    if response.status_code != 403:
        return SmokeResult(
            case,
            passed=False,
            detail=f"expected 403, got {response.status_code}",
            status_code=response.status_code,
            policy_code=policy_code or body_policy_code,
        )
    if policy_code != case.expected_policy_code:
        return SmokeResult(
            case,
            passed=False,
            detail=f"expected policy header {case.expected_policy_code}, got {policy_code or 'missing'}",
            status_code=response.status_code,
            policy_code=policy_code,
        )
    if body_policy_code != case.expected_policy_code:
        return SmokeResult(
            case,
            passed=False,
            detail=f"expected body error.code {case.expected_policy_code}, got {body_policy_code or 'missing'}",
            status_code=response.status_code,
            policy_code=policy_code,
        )
    return SmokeResult(
        case,
        passed=True,
        detail="blocked by AiManager policy",
        status_code=response.status_code,
        policy_code=policy_code,
    )


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


def _request_body(method: str) -> bytes | None:
    if method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
        return b"{}"
    return None


def _join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _header(headers: dict[str, str], name: str) -> str:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return ""


def _body_policy_code(body: bytes) -> str:
    try:
        payload: Any = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return ""
    code = error.get("code")
    return code if isinstance(code, str) else ""


def _slug(value: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in value).strip("_")[:64] or "route"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke test AiManager blocked route policy.")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("AIMANAGER_BASE_URL", "http://localhost:4000"),
    )
    parser.add_argument("--master-key", default=os.environ.get("LITELLM_MASTER_KEY", ""))
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args(argv)

    if not args.master_key.strip():
        print("FAIL missing LITELLM_MASTER_KEY or --master-key", file=sys.stderr)
        return 2

    results = run_blocked_route_smoke(
        base_url=args.base_url,
        master_key=args.master_key,
        timeout_seconds=args.timeout,
    )
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(
            f"{status}\t{result.case.method}\t{result.case.path}\t"
            f"{result.status_code or '-'}\t{result.policy_code or '-'}\t{result.detail}"
        )
    return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

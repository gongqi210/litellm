from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str
    status_code: int | None = None


Fetch = Callable[[str, str, dict[str, str], bytes | None], HttpResponse]

_PROVIDER_BLOCK_PATH = "/anthropic/messages"
_BUSINESS_CHAT_PATH = "/v1/chat/completions"
_READINESS_PATH = "/health/readiness"
_SAFE_FAILURE_STATUSES = {500, 502, 503, 504}
_SENSITIVE_PATTERNS = (
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9._-]*"),
    re.compile(r"postgres(?:ql)?://[^@\s]+:[^@\s]+@", re.IGNORECASE),
    re.compile(r"ycapi[-_ ]?(?:api[-_ ]?)?token", re.IGNORECASE),
)


def run_postgres_down_smoke(
    *,
    base_url: str,
    master_key: str,
    business_key: str | None = None,
    fetch: Fetch | None = None,
    timeout_seconds: float = 10,
    sensitive_values: Sequence[str] = (),
) -> list[CheckResult]:
    fetcher = fetch or _fetch_with_urllib(timeout_seconds=timeout_seconds)
    employee_key = business_key or master_key
    secrets = tuple(value for value in (master_key, employee_key, *sensitive_values) if _is_sensitive_value(value))
    return [
        _run_check(
            "readiness_fails",
            lambda: _check_readiness(fetcher, base_url, secrets),
        ),
        _run_check(
            "provider_passthrough_still_blocked",
            lambda: _check_provider_passthrough(fetcher, base_url, master_key, secrets),
        ),
        _run_check(
            "business_call_fails_safely",
            lambda: _check_business_call(fetcher, base_url, employee_key, secrets),
        ),
    ]


def _run_check(name: str, check: Callable[[], CheckResult]) -> CheckResult:
    try:
        return check()
    except OSError as exc:
        return CheckResult(name, False, f"request failed: {exc}")


def _check_readiness(fetch: Fetch, base_url: str, secrets: Sequence[str]) -> CheckResult:
    response = fetch("GET", _join_url(base_url, _READINESS_PATH), {}, None)
    if _contains_sensitive_text(response.body, secrets):
        return CheckResult("readiness_fails", False, "readiness response leaked secret-like content", response.status_code)
    if response.status_code in _SAFE_FAILURE_STATUSES:
        return CheckResult("readiness_fails", True, "readiness failed while Postgres was down", response.status_code)
    return CheckResult(
        "readiness_fails",
        False,
        f"expected readiness 5xx while Postgres is down, got {response.status_code}",
        response.status_code,
    )


def _check_provider_passthrough(
    fetch: Fetch,
    base_url: str,
    master_key: str,
    secrets: Sequence[str],
) -> CheckResult:
    response = fetch(
        "POST",
        _join_url(base_url, _PROVIDER_BLOCK_PATH),
        _auth_headers(master_key, "postgres-down-smoke-provider-block"),
        b"{}",
    )
    if _contains_sensitive_text(response.body, secrets):
        return CheckResult(
            "provider_passthrough_still_blocked",
            False,
            "provider block response leaked secret-like content",
            response.status_code,
        )
    policy_code = _header(response.headers, "x-aimanager-policy-code") or _body_policy_code(response.body)
    if response.status_code == 403 and policy_code == "aimanager_passthrough_blocked":
        return CheckResult(
            "provider_passthrough_still_blocked",
            True,
            "provider passthrough remained blocked before downstream LiteLLM",
            response.status_code,
        )
    return CheckResult(
        "provider_passthrough_still_blocked",
        False,
        f"expected AiManager passthrough 403, got status={response.status_code} policy={policy_code or 'missing'}",
        response.status_code,
    )


def _check_business_call(
    fetch: Fetch,
    base_url: str,
    master_key: str,
    secrets: Sequence[str],
) -> CheckResult:
    response = fetch(
        "POST",
        _join_url(base_url, _BUSINESS_CHAT_PATH),
        _auth_headers(master_key, "postgres-down-smoke-chat"),
        json.dumps(
            {
                "model": "gemini-2.5-flash",
                "messages": [{"role": "user", "content": "postgres-down-smoke"}],
            }
        ).encode("utf-8"),
    )
    if _contains_sensitive_text(response.body, secrets):
        return CheckResult(
            "business_call_fails_safely",
            False,
            "business response leaked secret-like content",
            response.status_code,
        )
    if response.status_code in _SAFE_FAILURE_STATUSES:
        return CheckResult(
            "business_call_fails_safely",
            True,
            "business call failed closed with sanitized 5xx",
            response.status_code,
        )
    return CheckResult(
        "business_call_fails_safely",
        False,
        f"expected 5xx failure while Postgres is down, got {response.status_code}",
        response.status_code,
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


def _auth_headers(master_key: str, request_id: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {master_key}",
        "Content-Type": "application/json",
        "x-request-id": request_id,
    }


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


def _contains_sensitive_text(body: bytes, secrets: Sequence[str]) -> bool:
    text = body.decode("utf-8", errors="replace")
    if any(pattern.search(text) for pattern in _SENSITIVE_PATTERNS):
        return True
    return any(secret and secret in text for secret in secrets)


def _is_sensitive_value(value: str | None) -> bool:
    if value is None:
        return False
    stripped = value.strip()
    if len(stripped) < 10:
        return False
    return stripped.lower() not in {"changeme", "change-me", "placeholder"}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Smoke test AiManager behavior while Postgres is unavailable."
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("AIMANAGER_BASE_URL", "http://localhost:4000"),
    )
    parser.add_argument("--master-key", default=os.environ.get("LITELLM_MASTER_KEY", ""))
    parser.add_argument(
        "--business-key",
        default=os.environ.get("AIMANAGER_POSTGRES_DOWN_BUSINESS_KEY", ""),
        help="Employee virtual key created before Postgres is stopped. Defaults to --master-key for backwards compatibility.",
    )
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args(argv)

    if not args.master_key.strip():
        print("FAIL missing LITELLM_MASTER_KEY or --master-key", file=sys.stderr)
        return 2

    results = run_postgres_down_smoke(
        base_url=args.base_url,
        master_key=args.master_key,
        business_key=args.business_key or None,
        timeout_seconds=args.timeout,
        sensitive_values=(os.environ.get("YCAPI_API_TOKEN", ""), os.environ.get("POSTGRES_PASSWORD", "")),
    )
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(f"{status}\t{result.name}\t{result.status_code or '-'}\t{result.detail}")
    return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

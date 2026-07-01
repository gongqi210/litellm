from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from aimanager.scripts.smoke_blocked_routes import HttpResponse, _fetch_with_urllib


WORK_CONTEXT_POLICY_CODE = "aimanager_work_context_invalid"

Fetch = Callable[[str, str, dict[str, str], bytes | None], HttpResponse]

_MARKER_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]+$")


@dataclass(frozen=True)
class WorkContextSmokeCase:
    method: str
    path: str
    model: str
    prompt: str


@dataclass(frozen=True)
class WorkContextSmokeResult:
    case: WorkContextSmokeCase
    passed: bool
    detail: str
    status_code: int | None = None
    policy_code: str | None = None


def run_work_context_enforcement_smoke(
    *,
    base_url: str,
    employee_key: str,
    request_marker: str,
    cases: Sequence[WorkContextSmokeCase] | None = None,
    fetch: Fetch | None = None,
    timeout_seconds: float = 10,
) -> list[WorkContextSmokeResult]:
    _validate_marker(request_marker)
    fetcher = fetch or _fetch_with_urllib(timeout_seconds=timeout_seconds)
    smoke_cases = tuple(cases) if cases is not None else _default_cases(request_marker)
    results: list[WorkContextSmokeResult] = []

    for case in smoke_cases:
        url = _join_url(base_url, case.path)
        headers = {
            "Authorization": f"Bearer {employee_key}",
            "Content-Type": "application/json",
            "x-request-id": f"work-context-{request_marker}-{_slug(case.path)}",
        }
        try:
            response = fetcher(case.method, url, headers, _request_body(case))
        except OSError as exc:
            results.append(WorkContextSmokeResult(case, passed=False, detail=f"request failed: {exc}"))
            continue
        results.append(_evaluate_response(case, response))

    return results


def _default_cases(request_marker: str) -> tuple[WorkContextSmokeCase, ...]:
    prompt = f"DO_NOT_ECHO_WORK_CONTEXT_SMOKE_{request_marker}"
    return (
        WorkContextSmokeCase("POST", "/v1/chat/completions", "gemini-2.5-flash", prompt),
        WorkContextSmokeCase("POST", "/v1/images/generations", "ycapi-image-1", prompt),
    )


def _request_body(case: WorkContextSmokeCase) -> bytes:
    if case.path == "/v1/chat/completions":
        payload: dict[str, Any] = {
            "model": case.model,
            "messages": [{"role": "user", "content": case.prompt}],
            "max_tokens": 8,
            "user": "employee-smoke-001",
        }
    elif case.path == "/v1/images/generations":
        payload = {
            "model": case.model,
            "prompt": case.prompt,
            "n": 1,
            "size": "1024x1024",
            "user": "employee-smoke-001",
        }
    else:
        payload = {
            "model": case.model,
            "prompt": case.prompt,
            "user": "employee-smoke-001",
        }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _evaluate_response(case: WorkContextSmokeCase, response: HttpResponse) -> WorkContextSmokeResult:
    policy_code = _header(response.headers, "x-aimanager-policy-code")
    body_policy_code = _body_policy_code(response.body)
    body_text = _body_text(response.body)
    if case.prompt in body_text:
        return WorkContextSmokeResult(
            case,
            passed=False,
            detail="response echoed smoke prompt",
            status_code=response.status_code,
            policy_code=policy_code or body_policy_code,
        )
    if response.status_code != 400:
        return WorkContextSmokeResult(
            case,
            passed=False,
            detail=f"expected 400, got {response.status_code}",
            status_code=response.status_code,
            policy_code=policy_code or body_policy_code,
        )
    if policy_code != WORK_CONTEXT_POLICY_CODE:
        return WorkContextSmokeResult(
            case,
            passed=False,
            detail=f"expected policy header {WORK_CONTEXT_POLICY_CODE}, got {policy_code or 'missing'}",
            status_code=response.status_code,
            policy_code=policy_code,
        )
    if body_policy_code != WORK_CONTEXT_POLICY_CODE:
        return WorkContextSmokeResult(
            case,
            passed=False,
            detail=f"expected body error.code {WORK_CONTEXT_POLICY_CODE}, got {body_policy_code or 'missing'}",
            status_code=response.status_code,
            policy_code=policy_code,
        )
    return WorkContextSmokeResult(
        case,
        passed=True,
        detail="missing work context rejected before provider dispatch",
        status_code=response.status_code,
        policy_code=policy_code,
    )


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


def _body_text(body: bytes) -> str:
    return body.decode("utf-8", errors="replace")


def _slug(value: str) -> str:
    return "".join(char if char.isalnum() else "-" for char in value).strip("-")[:64] or "route"


def _validate_marker(request_marker: str) -> None:
    if not request_marker or not _MARKER_PATTERN.fullmatch(request_marker):
        raise ValueError("request_marker may only contain letters, digits, underscore, dot, colon, or dash")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke test AiManager work-context fail-closed policy.")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("AIMANAGER_BUSINESS_BASE_URL")
        or os.environ.get("AIMANAGER_BASE_URL")
        or "http://localhost:4000",
    )
    parser.add_argument("--employee-key", default=os.environ.get("AIMANAGER_EMPLOYEE_VIRTUAL_KEY", ""))
    parser.add_argument("--request-marker", default=f"wc-{uuid4().hex[:12]}")
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args(argv)

    if not args.employee_key.strip():
        print("BLOCKED missing AIMANAGER_EMPLOYEE_VIRTUAL_KEY or --employee-key", file=sys.stderr)
        return 2

    try:
        results = run_work_context_enforcement_smoke(
            base_url=args.base_url,
            employee_key=args.employee_key,
            request_marker=args.request_marker,
            timeout_seconds=args.timeout,
        )
    except ValueError as exc:
        print(f"FAIL invalid input: {exc}", file=sys.stderr)
        return 2

    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(
            f"{status}\t{result.case.method}\t{result.case.path}\t"
            f"{result.status_code or '-'}\t{result.policy_code or '-'}\t{result.detail}"
        )
    return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

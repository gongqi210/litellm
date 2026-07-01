from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

from aimanager.scripts.smoke_blocked_routes import HttpResponse, _fetch_with_urllib


WORK_CONTEXT_POLICY_CODE = "aimanager_work_context_invalid"

Fetch = Callable[[str, str, dict[str, str], bytes | None], HttpResponse]
WorkContextCheckType = Literal["missing_context_block", "valid_context_roundtrip"]

_MARKER_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]+$")


@dataclass(frozen=True)
class WorkContextSmokeCase:
    method: str
    path: str
    model: str
    prompt: str
    check_type: WorkContextCheckType = "missing_context_block"
    work_item_id: str = ""


@dataclass(frozen=True)
class WorkContextSmokeResult:
    case: WorkContextSmokeCase
    passed: bool
    detail: str
    status_code: int | None = None
    policy_code: str | None = None
    request_id: str | None = None
    usage_present: bool = False
    image_result_count: int = 0
    work_context_present: bool = False


def run_work_context_enforcement_smoke(
    *,
    base_url: str,
    employee_key: str,
    request_marker: str,
    cases: Sequence[WorkContextSmokeCase] | None = None,
    run_valid_context_roundtrip: bool = False,
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
            results.append(WorkContextSmokeResult(case, passed=False, detail=f"request failed: {type(exc).__name__}"))
            continue
        results.append(_evaluate_response(case, response))

    if run_valid_context_roundtrip and results and all(result.passed for result in results):
        for case in _valid_context_cases(request_marker):
            url = _join_url(base_url, case.path)
            headers = {
                "Authorization": f"Bearer {employee_key}",
                "Content-Type": "application/json",
                "x-request-id": f"work-context-{request_marker}-valid-{_slug(case.path)}",
            }
            try:
                response = fetcher(case.method, url, headers, _request_body(case))
            except OSError as exc:
                results.append(WorkContextSmokeResult(case, passed=False, detail=f"request failed: {type(exc).__name__}"))
                continue
            results.append(_evaluate_valid_context_response(case, response))

    return results


def _default_cases(request_marker: str) -> tuple[WorkContextSmokeCase, ...]:
    prompt = f"DO_NOT_ECHO_WORK_CONTEXT_SMOKE_{request_marker}"
    return (
        WorkContextSmokeCase("POST", "/v1/chat/completions", "gemini-2.5-flash", prompt),
        WorkContextSmokeCase("POST", "/v1/images/generations", "ycapi-image-1", prompt),
    )


def _valid_context_cases(request_marker: str) -> tuple[WorkContextSmokeCase, ...]:
    prompt = f"DO_NOT_ECHO_WORK_CONTEXT_VALID_SMOKE_{request_marker}"
    return (
        WorkContextSmokeCase(
            "POST",
            "/v1/chat/completions",
            "gemini-2.5-flash",
            prompt,
            check_type="valid_context_roundtrip",
            work_item_id=request_marker,
        ),
        WorkContextSmokeCase(
            "POST",
            "/v1/images/generations",
            "ycapi-image-1",
            prompt,
            check_type="valid_context_roundtrip",
            work_item_id=request_marker,
        ),
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
    if case.check_type == "valid_context_roundtrip":
        image_count = 1 if case.path == "/v1/images/generations" else 0
        payload["metadata"] = _request_metadata(case.work_item_id or "work-context-smoke", image_count=image_count)
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _request_metadata(work_item_id: str, *, image_count: int) -> dict[str, str | int | bool]:
    return {
        "work_item_id": work_item_id,
        "employee_id": "employee-smoke-001",
        "department_id": "dept_smoke",
        "project_id": "proj_aimanager_runtime_smoke",
        "cost_center_id": "cc_smoke",
        "scenario_l1": "engineering",
        "scenario_l2": "code_assist",
        "internal_or_external": "internal",
        "channel": "sdk",
        "sensitivity_level": "internal",
        "approval_required": False,
        "end_user_principal": "employee-smoke-001",
        "currency": "CNY",
        "pricing_version": "m1-production-readiness",
        "image_count": image_count,
        "aimanager_smoke_id": work_item_id,
    }


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


def _evaluate_valid_context_response(case: WorkContextSmokeCase, response: HttpResponse) -> WorkContextSmokeResult:
    body_text = _body_text(response.body)
    if case.prompt in body_text:
        return WorkContextSmokeResult(
            case,
            passed=False,
            detail="response echoed smoke prompt",
            status_code=response.status_code,
            work_context_present=True,
        )
    if response.status_code != 200:
        return WorkContextSmokeResult(
            case,
            passed=False,
            detail=f"expected 200, got {response.status_code}",
            status_code=response.status_code,
            work_context_present=True,
        )
    payload = _json_object(response.body)
    if payload is None:
        return WorkContextSmokeResult(
            case,
            passed=False,
            detail="valid work context roundtrip returned non-JSON response",
            status_code=response.status_code,
            work_context_present=True,
        )
    request_id = _response_request_id(response, payload)
    if not request_id:
        return WorkContextSmokeResult(
            case,
            passed=False,
            detail="valid work context roundtrip did not return request id",
            status_code=response.status_code,
            work_context_present=True,
        )
    if case.path == "/v1/chat/completions":
        usage_present = _has_positive_usage(payload)
        if not usage_present:
            return WorkContextSmokeResult(
                case,
                passed=False,
                detail="valid work context chat roundtrip did not return usage",
                status_code=response.status_code,
                request_id=request_id,
                usage_present=False,
                work_context_present=True,
            )
        return WorkContextSmokeResult(
            case,
            passed=True,
            detail="valid work context chat roundtrip passed",
            status_code=response.status_code,
            request_id=request_id,
            usage_present=True,
            work_context_present=True,
        )
    image_result_count = _image_result_count(payload)
    if image_result_count <= 0:
        return WorkContextSmokeResult(
            case,
            passed=False,
            detail="valid work context image roundtrip did not return image data",
            status_code=response.status_code,
            request_id=request_id,
            image_result_count=image_result_count,
            work_context_present=True,
        )
    return WorkContextSmokeResult(
        case,
        passed=True,
        detail="valid work context image roundtrip passed",
        status_code=response.status_code,
        request_id=request_id,
        image_result_count=image_result_count,
        work_context_present=True,
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
    payload = _json_object(body)
    if payload is None:
        return ""
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return ""
    code = error.get("code")
    return code if isinstance(code, str) else ""


def _json_object(body: bytes) -> dict[str, Any] | None:
    try:
        payload: Any = json.loads(body.decode("utf-8"))
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


def _response_request_id(response: HttpResponse, payload: dict[str, Any]) -> str:
    for name in ("x-litellm-call-id", "x-request-id", "request-id"):
        value = _header(response.headers, name)
        if value:
            return value
    body_request_id = payload.get("request_id")
    return body_request_id if isinstance(body_request_id, str) and body_request_id.strip() else ""


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
    parser.add_argument(
        "--run-valid-context-roundtrip",
        action="store_true",
        help="After fail-closed checks pass, run governed chat/image requests with valid work-context metadata.",
    )
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
            run_valid_context_roundtrip=args.run_valid_context_roundtrip,
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

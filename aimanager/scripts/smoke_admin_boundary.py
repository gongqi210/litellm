from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal


BoundaryStatus = Literal["PASS", "FAIL", "BLOCKED"]


@dataclass(frozen=True)
class AdminBoundaryCase:
    name: str
    surface: str
    method: str
    path: str
    expected_policy_code: str | None = None


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True)
class AdminBoundaryResult:
    name: str
    surface: str
    method: str
    path: str
    status: BoundaryStatus
    detail: str
    status_code: int | None = None
    policy_code: str | None = None


Fetch = Callable[[str, str, dict[str, str], bytes | None], HttpResponse]


BUSINESS_BOUNDARY_CASES = (
    AdminBoundaryCase("business_admin_ui", "business", "GET", "/ui", "aimanager_route_not_allowed"),
    AdminBoundaryCase(
        "business_admin_static",
        "business",
        "GET",
        "/litellm-asset-prefix/_next/static/chunks/app.js",
        "aimanager_route_not_allowed",
    ),
    AdminBoundaryCase(
        "business_next_tree",
        "business",
        "GET",
        "/__next._tree.txt",
        "aimanager_route_not_allowed",
    ),
    AdminBoundaryCase("business_metrics", "business", "GET", "/metrics", "aimanager_route_not_allowed"),
    AdminBoundaryCase("business_key_info", "business", "GET", "/key/info", "aimanager_route_not_allowed"),
    AdminBoundaryCase("business_key_generate", "business", "POST", "/key/generate", "aimanager_route_not_allowed"),
    AdminBoundaryCase("business_v2_key_info", "business", "POST", "/v2/key/info", "aimanager_route_not_allowed"),
    AdminBoundaryCase(
        "business_config_update",
        "business",
        "POST",
        "/config/field/update",
        "aimanager_config_immutable",
    ),
    AdminBoundaryCase(
        "business_pass_through_endpoint_write",
        "business",
        "POST",
        "/pass-through-endpoints",
        "aimanager_config_immutable",
    ),
)

PUBLIC_ADMIN_CASES = (
    AdminBoundaryCase("public_admin_ui", "public_admin", "GET", "/ui"),
    AdminBoundaryCase("public_admin_key_generate", "public_admin", "POST", "/key/generate"),
)

EDGE_BLOCK_STATUSES = {401, 403, 404}
ROLE_REACHED_GOVERNANCE_CODE = "aimanager_key_governance_invalid"


def run_admin_boundary_smoke(
    *,
    business_base_url: str | None,
    public_admin_url: str | None = None,
    business_cases: Sequence[AdminBoundaryCase] = BUSINESS_BOUNDARY_CASES,
    public_admin_cases: Sequence[AdminBoundaryCase] = PUBLIC_ADMIN_CASES,
    allowed_sso_redirect_hosts: Sequence[str] = (),
    require_business_base_url: bool = False,
    require_public_admin_url: bool = False,
    master_key: str = "",
    fetch: Fetch | None = None,
    timeout_seconds: float = 10,
) -> list[AdminBoundaryResult]:
    fetcher = fetch or _fetch_with_urllib(timeout_seconds=timeout_seconds)
    results: list[AdminBoundaryResult] = []

    if business_base_url:
        for case in business_cases:
            results.append(
                _run_case(
                    case,
                    base_url=business_base_url,
                    fetch=fetcher,
                    evaluator=_evaluate_business_response,
                )
            )
    elif require_business_base_url:
        results.append(
            AdminBoundaryResult(
                name="business_base_url_required",
                surface="business",
                method="-",
                path="-",
                status="BLOCKED",
                detail="missing business base URL; run with --business-base-url to verify employee API exposure",
            )
        )

    if public_admin_url:
        for case in public_admin_cases:
            results.append(
                _run_case(
                    case,
                    base_url=public_admin_url,
                    fetch=fetcher,
                    evaluator=lambda current_case, response: _evaluate_public_admin_response(
                        current_case,
                        response,
                        allowed_sso_redirect_hosts=allowed_sso_redirect_hosts,
                    ),
                )
            )
    elif require_public_admin_url:
        results.append(
            AdminBoundaryResult(
                name="public_admin_url_required",
                surface="public_admin",
                method="-",
                path="-",
                status="BLOCKED",
                detail="missing public admin URL; run with --public-admin-url to verify SSO/reverse-proxy exposure",
            )
        )

    return results


def _run_case(
    case: AdminBoundaryCase,
    *,
    base_url: str,
    fetch: Fetch,
    evaluator: Callable[[AdminBoundaryCase, HttpResponse], AdminBoundaryResult],
) -> AdminBoundaryResult:
    url = _join_url(base_url, case.path)
    headers = _request_headers(case)
    try:
        response = fetch(case.method, url, headers, _request_body(case))
    except OSError as exc:
        if case.surface == "public_admin":
            return AdminBoundaryResult(
                name=case.name,
                surface=case.surface,
                method=case.method,
                path=case.path,
                status="PASS",
                detail=f"public admin URL unreachable from this client: {exc}",
            )
        return AdminBoundaryResult(
            name=case.name,
            surface=case.surface,
            method=case.method,
            path=case.path,
            status="FAIL",
            detail=f"business boundary request failed: {exc}",
        )
    return evaluator(case, response)


def _evaluate_business_response(case: AdminBoundaryCase, response: HttpResponse) -> AdminBoundaryResult:
    observed_policy_code = _observed_policy_code(response)
    if observed_policy_code:
        if observed_policy_code != case.expected_policy_code:
            return _result(
                case,
                "FAIL",
                f"expected AiManager policy {case.expected_policy_code}, got {observed_policy_code}",
                response,
                observed_policy_code,
            )
        if response.status_code != 403:
            return _result(
                case,
                "FAIL",
                f"expected AiManager 403 policy block, got {response.status_code}",
                response,
                observed_policy_code,
            )
        return _result(case, "PASS", "blocked by AiManager business-surface policy", response, observed_policy_code)

    if response.status_code in EDGE_BLOCK_STATUSES:
        return _result(case, "PASS", "blocked before the management surface by edge/auth policy", response, None)

    return _result(
        case,
        "FAIL",
        f"business management route unexpectedly reachable with HTTP {response.status_code}",
        response,
        None,
    )


def _evaluate_public_admin_response(
    case: AdminBoundaryCase,
    response: HttpResponse,
    *,
    allowed_sso_redirect_hosts: Sequence[str],
) -> AdminBoundaryResult:
    observed_policy_code = _observed_policy_code(response)
    if observed_policy_code == ROLE_REACHED_GOVERNANCE_CODE:
        return _result(
            case,
            "FAIL",
            "client-supplied role reached AiManager management governance; strip trusted headers at the edge",
            response,
            observed_policy_code,
        )

    if response.status_code in EDGE_BLOCK_STATUSES:
        detail = (
            "blocked by AiManager policy before admin access"
            if observed_policy_code
            else "blocked by public edge/auth policy"
        )
        return _result(case, "PASS", detail, response, observed_policy_code)

    if 300 <= response.status_code <= 399:
        location = _header(response.headers, "Location")
        if _redirect_host_allowed(location, allowed_sso_redirect_hosts):
            return _result(case, "PASS", f"allowed SSO redirect to {location}", response, observed_policy_code)
        return _result(
            case,
            "FAIL",
            f"redirect is not on an allowed SSO host: {location or 'missing Location'}",
            response,
            observed_policy_code,
        )

    return _result(
        case,
        "FAIL",
        f"public admin route unexpectedly reachable with HTTP {response.status_code}",
        response,
        observed_policy_code,
    )


def _fetch_with_urllib(*, timeout_seconds: float) -> Fetch:
    opener = urllib.request.build_opener(_NoRedirectHandler)

    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with opener.open(request, timeout=timeout_seconds) as response:
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


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def _request_headers(case: AdminBoundaryCase) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "x-request-id": f"admin_boundary_{_slug(case.name)}",
        "x-aimanager-role": "proxy_admin",
        "x-aimanager-actor": "spoofed-public-client",
        "x-aimanager-reason": "admin-boundary-smoke",
    }


def _request_body(case: AdminBoundaryCase) -> bytes | None:
    if case.method.upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    if case.path == "/key/generate":
        return b'{"key_alias":"admin-boundary-smoke"}'
    return b"{}"


def _result(
    case: AdminBoundaryCase,
    status: BoundaryStatus,
    detail: str,
    response: HttpResponse,
    policy_code: str | None,
) -> AdminBoundaryResult:
    return AdminBoundaryResult(
        name=case.name,
        surface=case.surface,
        method=case.method,
        path=case.path,
        status=status,
        detail=detail,
        status_code=response.status_code,
        policy_code=policy_code,
    )


def _observed_policy_code(response: HttpResponse) -> str:
    return _header(response.headers, "x-aimanager-policy-code") or _body_policy_code(response.body)


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


def _header(headers: dict[str, str], name: str) -> str:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return ""


def _redirect_host_allowed(location: str, allowed_hosts: Sequence[str]) -> bool:
    if not location:
        return False
    host = urllib.parse.urlparse(location).hostname or ""
    allowed = {value.strip().lower() for value in allowed_hosts if value.strip()}
    return bool(host) and host.lower() in allowed


def _join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _slug(value: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in value).strip("_")[:64] or "case"


def _env_list(name: str) -> list[str]:
    return [value.strip() for value in os.environ.get(name, "").split(",") if value.strip()]


def _exit_code(results: Sequence[AdminBoundaryResult]) -> int:
    if any(result.status == "FAIL" for result in results):
        return 1
    if any(result.status == "BLOCKED" for result in results):
        return 2
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke test AiManager admin exposure boundaries.")
    parser.add_argument(
        "--business-base-url",
        default=os.environ.get("AIMANAGER_BUSINESS_BASE_URL", ""),
        help="Employee/business API base URL. Use an empty value to skip business checks.",
    )
    parser.add_argument(
        "--public-admin-url",
        default=os.environ.get("AIMANAGER_PUBLIC_ADMIN_URL", ""),
        help="Externally reachable admin URL to probe for SSO/reverse-proxy protection.",
    )
    parser.add_argument(
        "--allowed-sso-redirect-host",
        action="append",
        default=[],
        help="Allowed SSO redirect host. May be repeated. Env AIMANAGER_ALLOWED_SSO_REDIRECT_HOSTS also supports comma-separated hosts.",
    )
    parser.add_argument(
        "--require-business-base-url",
        action="store_true",
        help="Return BLOCKED if --business-base-url is missing.",
    )
    parser.add_argument(
        "--require-public-admin-url",
        action="store_true",
        help="Return BLOCKED if --public-admin-url is missing.",
    )
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args(argv)

    allowed_sso_hosts = [*args.allowed_sso_redirect_host, *_env_list("AIMANAGER_ALLOWED_SSO_REDIRECT_HOSTS")]
    results = run_admin_boundary_smoke(
        business_base_url=args.business_base_url.strip() or None,
        public_admin_url=args.public_admin_url.strip() or None,
        allowed_sso_redirect_hosts=allowed_sso_hosts,
        require_business_base_url=args.require_business_base_url,
        require_public_admin_url=args.require_public_admin_url,
        timeout_seconds=args.timeout,
    )

    for result in results:
        print(
            f"{result.status}\t{result.name}\t{result.surface}\t{result.method}\t{result.path}\t"
            f"{result.status_code or '-'}\t{result.policy_code or '-'}\t{result.detail}"
        )
    if not results:
        print("BLOCKED\tno_checks_selected\t-\t-\t-\t-\t-\tno admin boundary checks were selected")
        return 2
    return _exit_code(results)


if __name__ == "__main__":
    raise SystemExit(main())

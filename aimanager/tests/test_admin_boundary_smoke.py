from __future__ import annotations

import json

from aimanager.scripts.smoke_admin_boundary import (
    BUSINESS_BOUNDARY_CASES,
    AdminBoundaryCase,
    HttpResponse,
    run_admin_boundary_smoke,
)


def test_business_boundary_accepts_policy_blocks_even_with_spoofed_role_header() -> None:
    requests: list[tuple[str, str, dict[str, str]]] = []

    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        requests.append((method, url, headers))
        case = _case_for_url(url)
        return HttpResponse(
            status_code=403,
            headers={"x-aimanager-policy-code": case.expected_policy_code or ""},
            body=json.dumps({"error": {"code": case.expected_policy_code}}).encode("utf-8"),
        )

    results = run_admin_boundary_smoke(
        business_base_url="https://api.example.com",
        business_cases=BUSINESS_BOUNDARY_CASES,
        public_admin_url=None,
        master_key="must-not-be-sent",
        fetch=fetch,
    )

    assert results
    assert all(result.status == "PASS" for result in results)
    assert all(request[2]["x-aimanager-role"] == "proxy_admin" for request in requests)
    assert all(request[2]["x-aimanager-actor"] == "spoofed-public-client" for request in requests)
    assert all("Authorization" not in request[2] for request in requests)


def test_business_boundary_fails_when_management_route_is_reachable() -> None:
    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        return HttpResponse(status_code=200, headers={}, body=b"<html>LiteLLM UI</html>")

    results = run_admin_boundary_smoke(
        business_base_url="https://api.example.com",
        business_cases=[AdminBoundaryCase("business_ui", "business", "GET", "/ui", "aimanager_route_not_allowed")],
        public_admin_url=None,
        fetch=fetch,
    )

    assert len(results) == 1
    assert results[0].status == "FAIL"
    assert "unexpectedly reachable" in results[0].detail


def test_business_boundary_fails_on_wrong_aimanager_policy_code() -> None:
    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        return HttpResponse(
            status_code=403,
            headers={"x-aimanager-policy-code": "aimanager_rbac_denied"},
            body=b'{"error":{"code":"aimanager_rbac_denied"}}',
        )

    results = run_admin_boundary_smoke(
        business_base_url="https://api.example.com",
        business_cases=[AdminBoundaryCase("business_ui", "business", "GET", "/ui", "aimanager_route_not_allowed")],
        public_admin_url=None,
        fetch=fetch,
    )

    assert len(results) == 1
    assert results[0].status == "FAIL"
    assert "expected AiManager policy aimanager_route_not_allowed" in results[0].detail


def test_business_boundary_fails_when_policy_block_is_not_403() -> None:
    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        return HttpResponse(
            status_code=401,
            headers={"x-aimanager-policy-code": "aimanager_route_not_allowed"},
            body=b'{"error":{"code":"aimanager_route_not_allowed"}}',
        )

    results = run_admin_boundary_smoke(
        business_base_url="https://api.example.com",
        business_cases=[AdminBoundaryCase("business_ui", "business", "GET", "/ui", "aimanager_route_not_allowed")],
        public_admin_url=None,
        fetch=fetch,
    )

    assert len(results) == 1
    assert results[0].status == "FAIL"
    assert "expected AiManager 403 policy block" in results[0].detail


def test_public_admin_boundary_passes_when_url_is_unreachable() -> None:
    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        raise OSError("connection refused")

    results = run_admin_boundary_smoke(
        business_base_url=None,
        public_admin_url="https://admin.example.com",
        public_admin_cases=[AdminBoundaryCase("public_ui", "public_admin", "GET", "/ui")],
        fetch=fetch,
    )

    assert len(results) == 1
    assert results[0].status == "PASS"
    assert "unreachable" in results[0].detail


def test_public_admin_boundary_fails_when_route_is_reachable() -> None:
    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        return HttpResponse(status_code=200, headers={}, body=b"<html>LiteLLM Admin</html>")

    results = run_admin_boundary_smoke(
        business_base_url=None,
        public_admin_url="https://admin.example.com",
        public_admin_cases=[AdminBoundaryCase("public_ui", "public_admin", "GET", "/ui")],
        fetch=fetch,
    )

    assert len(results) == 1
    assert results[0].status == "FAIL"
    assert "public admin route unexpectedly reachable" in results[0].detail


def test_public_admin_boundary_fails_when_spoofed_role_reaches_key_governance() -> None:
    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        return HttpResponse(
            status_code=400,
            headers={"x-aimanager-policy-code": "aimanager_key_governance_invalid"},
            body=b'{"error":{"code":"aimanager_key_governance_invalid"}}',
        )

    results = run_admin_boundary_smoke(
        business_base_url=None,
        public_admin_url="https://admin.example.com",
        public_admin_cases=[AdminBoundaryCase("public_key_generate", "public_admin", "POST", "/key/generate")],
        fetch=fetch,
    )

    assert len(results) == 1
    assert results[0].status == "FAIL"
    assert "client-supplied role reached AiManager management governance" in results[0].detail


def test_public_admin_boundary_fails_on_unlisted_redirect_host() -> None:
    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        return HttpResponse(
            status_code=302,
            headers={"Location": "https://admin.example.com/ui"},
            body=b"",
        )

    results = run_admin_boundary_smoke(
        business_base_url=None,
        public_admin_url="https://admin.example.com",
        public_admin_cases=[AdminBoundaryCase("public_ui", "public_admin", "GET", "/ui")],
        allowed_sso_redirect_hosts=["sso.example.com"],
        fetch=fetch,
    )

    assert len(results) == 1
    assert results[0].status == "FAIL"
    assert "redirect is not on an allowed SSO host" in results[0].detail


def test_public_admin_boundary_accepts_allowed_sso_redirect() -> None:
    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        return HttpResponse(
            status_code=302,
            headers={"Location": "https://sso.example.com/login?next=/ui"},
            body=b"",
        )

    results = run_admin_boundary_smoke(
        business_base_url=None,
        public_admin_url="https://admin.example.com",
        public_admin_cases=[AdminBoundaryCase("public_ui", "public_admin", "GET", "/ui")],
        allowed_sso_redirect_hosts=["sso.example.com"],
        fetch=fetch,
    )

    assert len(results) == 1
    assert results[0].status == "PASS"
    assert "allowed SSO redirect" in results[0].detail


def test_public_admin_boundary_marks_missing_required_url_blocked() -> None:
    results = run_admin_boundary_smoke(
        business_base_url=None,
        public_admin_url=None,
        require_public_admin_url=True,
        fetch=lambda method, url, headers, body: HttpResponse(200, {}, b""),
    )

    assert len(results) == 1
    assert results[0].name == "public_admin_url_required"
    assert results[0].status == "BLOCKED"


def test_business_boundary_marks_missing_required_url_blocked() -> None:
    results = run_admin_boundary_smoke(
        business_base_url=None,
        require_business_base_url=True,
        public_admin_url=None,
        fetch=lambda method, url, headers, body: HttpResponse(200, {}, b""),
    )

    assert len(results) == 1
    assert results[0].name == "business_base_url_required"
    assert results[0].status == "BLOCKED"


def _case_for_url(url: str) -> AdminBoundaryCase:
    path = url.removeprefix("https://api.example.com")
    for case in BUSINESS_BOUNDARY_CASES:
        if case.path == path:
            return case
    raise AssertionError(f"unexpected URL {url}")

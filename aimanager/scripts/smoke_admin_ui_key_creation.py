from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from aimanager.governance import KeyGovernanceError, normalize_key_request


DEFAULT_ADMIN_BASE_URL = "http://127.0.0.1:4001"
DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_TEAM_ID = "team_aimanager_smoke"
DEFAULT_TEAM_ALIAS = "AiManager Smoke Team"
DEFAULT_USER_ID = "aimanager-ui-smoke-user"
DEFAULT_END_USER = "employee-ui-smoke-001"

_MARKER_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]+$")
_ADMIN_RBAC_ROLE = "proxy_admin"
_ADMIN_ACTOR = "aimanager-ui-smoke"


class AdminUiKeyCreationError(AssertionError):
    """Raised when the Admin UI key-creation smoke observes an invalid payload."""


@dataclass(frozen=True)
class AdminUiKeyCreationSmokeResult:
    passed: bool
    detail: str
    request_marker: str
    key_alias: str
    reject_status: int | None = None
    accept_status: int | None = None
    policy_code: str | None = None


def build_governance_metadata(request_marker: str) -> dict[str, str]:
    _validate_marker(request_marker)
    return {
        "owner": "aimanager-ui-smoke",
        "department_id": "dept_smoke",
        "project_id": "proj_aimanager_admin_ui_smoke",
        "cost_center_id": "cc_smoke",
        "scenario_l1": "engineering",
        "scenario_l2": "admin-ui-key-creation-smoke",
        "approver": "aimanager-ci",
        "internal_or_external": "internal",
        "end_user_principal": DEFAULT_END_USER,
        "aimanager_smoke_id": request_marker,
    }


def validate_key_generate_payload(
    payload: dict[str, Any],
    *,
    request_marker: str,
    expected_model: str,
) -> None:
    _validate_marker(request_marker)
    if not isinstance(payload, dict):
        raise AdminUiKeyCreationError("key/generate payload must be a JSON object")
    try:
        normalized = normalize_key_request(payload, shared_key=False)
    except KeyGovernanceError as exc:
        raise AdminUiKeyCreationError(str(exc)) from exc

    models = normalized.get("models")
    if expected_model not in models:
        raise AdminUiKeyCreationError(f"models must include {expected_model}")
    metadata = normalized.get("metadata")
    if metadata.get("aimanager_smoke_id") != request_marker:
        raise AdminUiKeyCreationError("metadata.aimanager_smoke_id does not match request marker")


async def run_admin_ui_key_creation_smoke(
    *,
    admin_base_url: str,
    username: str,
    password: str,
    master_key: str,
    request_marker: str,
    model: str = DEFAULT_MODEL,
    team_id: str = DEFAULT_TEAM_ID,
    team_alias: str = DEFAULT_TEAM_ALIAS,
    headless: bool = True,
    timeout_seconds: float = 30.0,
    bootstrap_team: bool = True,
    browser_executable: str | None = None,
) -> AdminUiKeyCreationSmokeResult:
    _validate_marker(request_marker)
    admin_base_url = admin_base_url.rstrip("/")
    key_alias = f"aimanager-ui-smoke-{request_marker}"

    if bootstrap_team:
        ensure_smoke_team(
            admin_base_url=admin_base_url,
            master_key=master_key,
            team_id=team_id,
            team_alias=team_alias,
            model=model,
            timeout_seconds=timeout_seconds,
        )

    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - exercised only without optional dependency.
        raise RuntimeError("Playwright is required: uv run --with playwright python -m ...") from exc

    virtual_key: str | None = None
    reject_status: int | None = None
    accept_status: int | None = None
    policy_code: str | None = None
    timeout_ms = int(timeout_seconds * 1000)

    async with async_playwright() as playwright:
        launch_options: dict[str, Any] = {"headless": headless}
        if browser_executable:
            launch_options["executable_path"] = browser_executable
        browser = await playwright.chromium.launch(**launch_options)
        context = await browser.new_context(
            extra_http_headers={
                "x-request-id": f"admin-ui-smoke-{request_marker}",
                "x-aimanager-role": _ADMIN_RBAC_ROLE,
                "x-aimanager-actor": _ADMIN_ACTOR,
            }
        )
        page = await context.new_page()
        try:
            await _login(page, admin_base_url, username, password, timeout_ms)
            await _open_create_key_form(page, admin_base_url, key_alias, team_id, model, timeout_ms)
            await _fill_required_limits_and_duration(page, timeout_ms)

            reject_response = await _submit_create_key(page, timeout_ms)
            reject_status = reject_response.status
            policy_code = reject_response.headers.get("x-aimanager-policy-code")
            if reject_status != 400 or policy_code != "aimanager_key_governance_invalid":
                return AdminUiKeyCreationSmokeResult(
                    passed=False,
                    detail=(
                        "missing-metadata UI request did not fail closed with "
                        "aimanager_key_governance_invalid"
                    ),
                    request_marker=request_marker,
                    key_alias=key_alias,
                    reject_status=reject_status,
                    policy_code=policy_code,
                )

            metadata = build_governance_metadata(request_marker)
            await _fill_metadata(page, metadata, timeout_ms)

            accept_response = await _submit_create_key(page, timeout_ms)
            accept_status = accept_response.status
            accept_payload = _request_post_json(accept_response.request)
            validate_key_generate_payload(
                accept_payload,
                request_marker=request_marker,
                expected_model=model,
            )
            if accept_status < 200 or accept_status >= 300:
                return AdminUiKeyCreationSmokeResult(
                    passed=False,
                    detail=f"governed UI key creation returned HTTP {accept_status}",
                    request_marker=request_marker,
                    key_alias=key_alias,
                    reject_status=reject_status,
                    accept_status=accept_status,
                    policy_code=policy_code,
                )
            response_body = await accept_response.json()
            virtual_key = _extract_virtual_key(response_body)
            if not virtual_key:
                return AdminUiKeyCreationSmokeResult(
                    passed=False,
                    detail="governed UI key creation response did not include a virtual key",
                    request_marker=request_marker,
                    key_alias=key_alias,
                    reject_status=reject_status,
                    accept_status=accept_status,
                    policy_code=policy_code,
                )
            return AdminUiKeyCreationSmokeResult(
                passed=True,
                detail="Admin UI key creation failed closed without metadata and succeeded with governed fields",
                request_marker=request_marker,
                key_alias=key_alias,
                reject_status=reject_status,
                accept_status=accept_status,
                policy_code=policy_code,
            )
        finally:
            await context.close()
            await browser.close()
            if virtual_key:
                delete_virtual_key(
                    admin_base_url=admin_base_url,
                    master_key=master_key,
                    virtual_key=virtual_key,
                    request_marker=request_marker,
                    timeout_seconds=timeout_seconds,
                )


def ensure_smoke_team(
    *,
    admin_base_url: str,
    master_key: str,
    team_id: str,
    team_alias: str,
    model: str,
    timeout_seconds: float,
) -> None:
    query = urllib.parse.urlencode({"team_id": team_id})
    try:
        _request_json(
            "GET",
            f"{admin_base_url.rstrip('/')}/team/info?{query}",
            master_key=master_key,
            request_id=f"team-info-{team_id}",
            timeout_seconds=timeout_seconds,
        )
        return
    except urllib.error.HTTPError as exc:
        if exc.code not in {400, 404}:
            raise

    _request_json(
        "POST",
        f"{admin_base_url.rstrip('/')}/team/new",
        master_key=master_key,
        request_id=f"team-new-{team_id}",
        timeout_seconds=timeout_seconds,
        payload={
            "team_id": team_id,
            "team_alias": team_alias,
            "models": [model],
            "max_budget": 100,
            "rpm_limit": 600,
            "tpm_limit": 120000,
        },
    )


def delete_virtual_key(
    *,
    admin_base_url: str,
    master_key: str,
    virtual_key: str,
    request_marker: str,
    timeout_seconds: float,
) -> None:
    _request_json(
        "POST",
        f"{admin_base_url.rstrip('/')}/key/delete",
        master_key=master_key,
        request_id=f"delete-ui-key-{request_marker}",
        timeout_seconds=timeout_seconds,
        payload={"keys": [virtual_key]},
        extra_headers={
            "x-aimanager-reason": "AiManager Admin UI smoke cleanup",
            "x-aimanager-disposition-reason": "AiManager Admin UI smoke cleanup",
        },
    )


async def _login(page: Any, admin_base_url: str, username: str, password: str, timeout_ms: int) -> None:
    await page.goto(f"{admin_base_url}/ui/login/", wait_until="domcontentloaded")
    await page.get_by_placeholder("Enter your username").fill(username)
    await page.get_by_placeholder("Enter your password").fill(password)
    await page.get_by_role("button", name="Login", exact=True).click()
    await page.wait_for_url(
        lambda url: _url_path(str(url)).startswith("/ui") and "/login" not in _url_path(str(url)),
        timeout=timeout_ms,
    )


async def _open_create_key_form(
    page: Any,
    admin_base_url: str,
    key_alias: str,
    team_id: str,
    model: str,
    timeout_ms: int,
) -> None:
    params = urllib.parse.urlencode(
        {
            "create": "true",
            "owned_by": "you",
            "team_id": team_id,
            "key_alias": key_alias,
            "models": model,
            "key_type": "llm_api",
        }
    )
    target_url = f"{admin_base_url}/ui/?{params}"
    for _ in range(3):
        await page.goto(target_url, wait_until="domcontentloaded")
        if await _wait_for_visible_text(page, "Key Ownership", exact=True, timeout_ms=min(timeout_ms, 15_000)):
            break
    else:
        raise AdminUiKeyCreationError("Admin UI create-key modal did not open from deep link")

    if not await _wait_for_visible_text(page, team_id, exact=False, timeout_ms=timeout_ms):
        raise AdminUiKeyCreationError(f"Admin UI create-key modal did not preselect team {team_id}")
    if not await _wait_for_visible_text(page, model, exact=True, timeout_ms=timeout_ms):
        raise AdminUiKeyCreationError(f"Admin UI create-key modal did not preselect model {model}")


async def _fill_required_limits_and_duration(page: Any, timeout_ms: int) -> None:
    modal = _create_key_modal(page)
    await modal.get_by_text("Optional Settings", exact=True).click(timeout=timeout_ms)
    await _fill_numerical_input(modal, "max_budget", "1", timeout_ms)
    await _fill_numerical_input(modal, "tpm_limit", "120000", timeout_ms)
    await _fill_numerical_input(modal, "rpm_limit", "60", timeout_ms)
    await modal.get_by_text("Key Lifecycle", exact=True).click(timeout=timeout_ms)
    duration_input = modal.locator('input[name="duration"]').last
    await duration_input.wait_for(state="visible", timeout=timeout_ms)
    await duration_input.fill("1h", timeout=timeout_ms)


async def _fill_metadata(page: Any, metadata: dict[str, str], timeout_ms: int) -> None:
    modal = _create_key_modal(page)
    metadata_text = json.dumps(metadata, sort_keys=True)
    await modal.get_by_placeholder("Enter metadata as JSON").fill(metadata_text, timeout=timeout_ms)


async def _submit_create_key(page: Any, timeout_ms: int) -> Any:
    modal = _create_key_modal(page)
    async with page.expect_response(lambda response: response.url.endswith("/key/generate"), timeout=timeout_ms) as info:
        await modal.get_by_role("button", name="Create Key", exact=True).click(timeout=timeout_ms)
    return await info.value


async def _fill_numerical_input(modal: Any, field_name: str, value: str, timeout_ms: int) -> None:
    field = modal.locator(f"#{field_name}").last
    if await field.count() == 0:
        field = modal.locator(f'input[name="{field_name}"]').last
    await field.wait_for(state="visible", timeout=timeout_ms)
    await field.fill(value)


def _create_key_modal(page: Any) -> Any:
    return page.locator(".ant-modal").filter(has_text="Key Ownership").last


async def _wait_for_visible_text(page: Any, text: str, *, exact: bool, timeout_ms: int) -> bool:
    deadline = time.monotonic() + (timeout_ms / 1000)
    locator = page.get_by_text(text, exact=exact).first
    while time.monotonic() < deadline:
        try:
            if await locator.count() > 0 and await locator.is_visible():
                return True
        except Exception:
            pass
        await page.wait_for_timeout(250)
    return False


def _request_post_json(request: Any) -> dict[str, Any]:
    post_data = request.post_data
    if not post_data:
        raise AdminUiKeyCreationError("captured /key/generate request had no JSON body")
    try:
        payload = json.loads(post_data)
    except json.JSONDecodeError as exc:
        raise AdminUiKeyCreationError("captured /key/generate request body was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise AdminUiKeyCreationError("captured /key/generate request body was not an object")
    return payload


def _url_path(url: str) -> str:
    return urllib.parse.urlparse(url).path


def _extract_virtual_key(response_body: Any) -> str | None:
    if not isinstance(response_body, dict):
        return None
    key = response_body.get("key")
    return key if isinstance(key, str) and key else None


def _request_json(
    method: str,
    url: str,
    *,
    master_key: str,
    request_id: str,
    timeout_seconds: float,
    payload: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> Any:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Authorization": f"Bearer {master_key}",
        "Content-Type": "application/json",
        "x-request-id": request_id,
        "x-aimanager-role": _ADMIN_RBAC_ROLE,
        "x-aimanager-actor": _ADMIN_ACTOR,
    }
    if extra_headers:
        headers.update(extra_headers)
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
        raw = response.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def _validate_marker(request_marker: str) -> None:
    if not request_marker or not _MARKER_PATTERN.fullmatch(request_marker):
        raise ValueError("request_marker must contain only letters, numbers, dots, colons, underscores, or dashes")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Smoke test AiManager Admin UI governed key creation.")
    parser.add_argument("--admin-base-url", default=os.getenv("AIMANAGER_ADMIN_BASE_URL", DEFAULT_ADMIN_BASE_URL))
    parser.add_argument("--username", default=os.getenv("AIMANAGER_ADMIN_UI_USERNAME", "admin"))
    parser.add_argument("--password", default=os.getenv("AIMANAGER_ADMIN_UI_PASSWORD"))
    parser.add_argument("--master-key", default=os.getenv("LITELLM_MASTER_KEY"))
    parser.add_argument("--request-marker", default=f"{int(time.time())}-{uuid4().hex[:8]}")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--team-id", default=DEFAULT_TEAM_ID)
    parser.add_argument("--team-alias", default=DEFAULT_TEAM_ALIAS)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--headed", action="store_true", help="Run browser headed instead of headless.")
    parser.add_argument("--skip-team-bootstrap", action="store_true")
    parser.add_argument("--browser-executable", default=os.getenv("AIMANAGER_PLAYWRIGHT_BROWSER_EXECUTABLE"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    master_key = args.master_key
    password = args.password or master_key
    if not master_key:
        print("FAIL missing LITELLM_MASTER_KEY or --master-key", file=sys.stderr)
        return 2
    if not password:
        print("FAIL missing Admin UI password or --password", file=sys.stderr)
        return 2

    try:
        result = asyncio.run(
            run_admin_ui_key_creation_smoke(
                admin_base_url=args.admin_base_url,
                username=args.username,
                password=password,
                master_key=master_key,
                request_marker=args.request_marker,
                model=args.model,
                team_id=args.team_id,
                team_alias=args.team_alias,
                headless=not args.headed,
                timeout_seconds=args.timeout,
                bootstrap_team=not args.skip_team_bootstrap,
                browser_executable=args.browser_executable,
            )
        )
    except Exception as exc:
        print(f"FAIL admin_ui_key_creation error={type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    status = "PASS" if result.passed else "FAIL"
    print(
        " ".join(
            [
                status,
                "admin_ui_key_creation",
                f"marker={result.request_marker}",
                f"alias={result.key_alias}",
                f"reject_status={result.reject_status}",
                f"accept_status={result.accept_status}",
                f"policy_code={result.policy_code}",
                f"detail={result.detail}",
            ]
        )
    )
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import json

from aimanager.scripts.smoke_blocked_routes import (
    BLOCKED_ROUTE_CASES,
    BlockedRouteCase,
    HttpResponse,
    run_blocked_route_smoke,
)


def test_blocked_route_inventory_covers_m1_provider_and_config_risks() -> None:
    inventory = {(case.method, case.path, case.expected_policy_code) for case in BLOCKED_ROUTE_CASES}

    assert ("POST", "/anthropic/messages", "aimanager_passthrough_blocked") in inventory
    assert ("POST", "/gemini/v1beta/models", "aimanager_passthrough_blocked") in inventory
    assert ("POST", "/bedrock/model", "aimanager_passthrough_blocked") in inventory
    assert ("POST", "/vertex_ai/discovery/test", "aimanager_passthrough_blocked") in inventory
    assert ("POST", "/openai/deployments/test/chat/completions", "aimanager_passthrough_blocked") in inventory
    assert (
        "POST",
        "/v1beta/models/gemini-2.5-flash:generateContent",
        "aimanager_google_native_blocked",
    ) in inventory
    assert ("POST", "/config/update", "aimanager_config_immutable") in inventory
    assert ("POST", "/config/field/update", "aimanager_config_immutable") in inventory
    assert ("PATCH", "/config/cost_margin_config", "aimanager_config_immutable") in inventory
    assert ("POST", "/config_overrides/hashicorp_vault", "aimanager_config_immutable") in inventory
    assert ("POST", "/cache/settings", "aimanager_config_immutable") in inventory
    assert ("POST", "/reload/model_cost_map", "aimanager_config_immutable") in inventory
    assert ("POST", "/model/new", "aimanager_config_immutable") in inventory
    assert ("POST", "/v1/embeddings", "aimanager_route_not_allowed") in inventory
    assert ("GET", "/v1/models", "aimanager_business_token_forbidden") in inventory
    assert ("POST", "/v1/chat/completions", "aimanager_business_token_forbidden") in inventory


def test_blocked_route_smoke_accepts_aimanager_policy_403_responses() -> None:
    requests: list[tuple[str, str, dict[str, str]]] = []

    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        requests.append((method, url, headers))
        policy_code = _case_for_url(url).expected_policy_code
        return HttpResponse(
            status_code=403,
            headers={"x-aimanager-policy-code": policy_code},
            body=json.dumps({"error": {"code": policy_code}}).encode("utf-8"),
        )

    results = run_blocked_route_smoke(
        base_url="http://localhost:4000",
        master_key="local-master-key",
        fetch=fetch,
    )

    assert results
    assert all(result.passed for result in results)
    assert {"POST", "PATCH", "DELETE"}.issubset({request[0] for request in requests})
    assert all(request[2]["Authorization"] == "Bearer local-master-key" for request in requests)


def test_blocked_route_smoke_rejects_unblocked_or_wrong_policy_responses() -> None:
    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        return HttpResponse(
            status_code=200,
            headers={},
            body=b'{"object":"unexpected"}',
        )

    results = run_blocked_route_smoke(
        base_url="http://localhost:4000",
        master_key="local-master-key",
        cases=[BlockedRouteCase("POST", "/anthropic/messages", "aimanager_passthrough_blocked")],
        fetch=fetch,
    )

    assert len(results) == 1
    assert results[0].passed is False
    assert "expected 403" in results[0].detail


def _case_for_url(url: str) -> BlockedRouteCase:
    path = url.removeprefix("http://localhost:4000")
    for case in BLOCKED_ROUTE_CASES:
        if case.path == path:
            return case
    raise AssertionError(f"unexpected URL {url}")

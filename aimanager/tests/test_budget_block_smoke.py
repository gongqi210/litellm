from __future__ import annotations

import json

from aimanager.scripts.smoke_blocked_routes import HttpResponse
from aimanager.scripts.smoke_budget_block import (
    KeyBudgetRow,
    parse_key_budget_rows,
    run_budget_block_smoke,
)


def test_budget_block_smoke_passes_when_second_request_is_rejected() -> None:
    requests: list[tuple[str, str, dict[str, str], dict[str, object] | None]] = []

    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        payload = json.loads(body.decode("utf-8")) if body else None
        requests.append((method, url, headers, payload))
        if url.endswith("/key/generate"):
            assert headers["x-aimanager-role"] == "proxy_admin"
            assert headers["x-aimanager-actor"] == "aimanager-ci"
            assert payload is not None
            assert payload["key_alias"] == "aimanager-budget-smoke-budget-123"
            assert payload["max_budget"] == 0.005
            assert "shared_key" not in payload["metadata"]  # type: ignore[operator]
            assert payload["metadata"]["scenario_l2"] == "runtime-budget-block-smoke"  # type: ignore[index]
            return _json_response({"key": "sk-budget-smoke"})
        if url.endswith("/v1/images/generations"):
            assert headers["Authorization"] == "Bearer sk-budget-smoke"
            assert payload is not None
            assert payload["metadata"]["scenario_l2"] == "runtime-budget-block-smoke"  # type: ignore[index]
            return _json_response({"created": 1, "data": [{"url": "https://example.invalid/smoke.png"}]})
        if url.endswith("/v1/chat/completions"):
            assert headers["Authorization"] == "Bearer sk-budget-smoke"
            return _json_response(
                {
                    "error": {
                        "message": (
                            "Budget has been exceeded! Key=aimanager-budget-smoke-budget-123 "
                            "Current cost: 0.01, Max budget: 0.005"
                        ),
                        "type": "budget_exceeded",
                        "param": None,
                        "code": "429",
                    },
                    "request_id": "budget-block-budget-123",
                },
                status_code=429,
            )
        if url.endswith("/key/delete"):
            assert headers["x-aimanager-role"] == "proxy_admin"
            return _json_response({"deleted": True})
        raise AssertionError(f"unexpected URL {url}")

    def poll_key_budget(key_alias: str) -> list[KeyBudgetRow]:
        assert key_alias == "aimanager-budget-smoke-budget-123"
        return [
            KeyBudgetRow(
                key_alias="aimanager-budget-smoke-budget-123",
                spend=0.01,
                max_budget=0.005,
            )
        ]

    result = run_budget_block_smoke(
        business_base_url="http://localhost:4000",
        admin_base_url="http://localhost:4001",
        master_key="local-master-key",
        request_marker="budget-123",
        budget=0.005,
        fetch=fetch,
        poll_key_budget=poll_key_budget,
        sleep=lambda _seconds: None,
        poll_attempts=1,
    )

    assert result.passed is True
    assert result.detail == "request blocked by LiteLLM key budget"
    assert result.prime_status_code == 200
    assert result.blocked_status_code == 429
    assert result.budget_error_type == "budget_exceeded"
    assert result.key_alias == "aimanager-budget-smoke-budget-123"
    assert result.key_spend == 0.01
    assert result.max_budget == 0.005
    assert any(request[1].endswith("/key/delete") for request in requests)


def test_budget_block_smoke_fails_when_followup_request_is_allowed() -> None:
    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        if url.endswith("/key/generate"):
            assert headers["x-aimanager-role"] == "proxy_admin"
            return _json_response({"key": "sk-budget-smoke"})
        if url.endswith("/v1/images/generations") or url.endswith("/v1/chat/completions"):
            return _json_response({"ok": True})
        if url.endswith("/key/delete"):
            assert headers["x-aimanager-role"] == "proxy_admin"
            return _json_response({"deleted": True})
        raise AssertionError(f"unexpected URL {url}")

    result = run_budget_block_smoke(
        business_base_url="http://localhost:4000",
        admin_base_url="http://localhost:4001",
        master_key="local-master-key",
        request_marker="budget-123",
        budget=0.005,
        fetch=fetch,
        poll_key_budget=lambda _key_alias: [],
        sleep=lambda _seconds: None,
        poll_attempts=1,
    )

    assert result.passed is False
    assert "budget request was not blocked" in result.detail
    assert result.blocked_status_code == 200


def test_parse_key_budget_rows_accepts_psql_json_output() -> None:
    rows = parse_key_budget_rows(
        json.dumps(
            [
                {
                    "key_alias": "aimanager-budget-smoke-budget-123",
                    "spend": "0.01",
                    "max_budget": "0.005",
                },
                {
                    "key_alias": "ignore-bad-budget",
                    "spend": "not-a-number",
                    "max_budget": 1,
                },
            ]
        )
    )

    assert rows == [
        KeyBudgetRow(
            key_alias="aimanager-budget-smoke-budget-123",
            spend=0.01,
            max_budget=0.005,
        )
    ]


def _json_response(payload: dict[str, object], status_code: int = 200) -> HttpResponse:
    return HttpResponse(
        status_code=status_code,
        headers={"content-type": "application/json"},
        body=json.dumps(payload).encode("utf-8"),
    )

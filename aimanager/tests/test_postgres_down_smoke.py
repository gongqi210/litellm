import json

from aimanager.scripts.smoke_postgres_down import HttpResponse, run_postgres_down_smoke


def test_postgres_down_smoke_accepts_failed_readiness_blocked_passthrough_and_safe_5xx() -> None:
    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        if url.endswith("/health/readiness"):
            return HttpResponse(503, {}, b'{"status":"unhealthy"}')
        if url.endswith("/anthropic/messages"):
            return HttpResponse(
                403,
                {"x-aimanager-policy-code": "aimanager_passthrough_blocked"},
                json.dumps({"error": {"code": "aimanager_passthrough_blocked"}}).encode("utf-8"),
            )
        if url.endswith("/v1/chat/completions"):
            assert method == "POST"
            assert headers["Authorization"] == "Bearer employee-virtual-key-secret"
            assert body is not None
            return HttpResponse(
                503,
                {"content-type": "application/json", "x-litellm-call-id": "req-db-down"},
                json.dumps(
                    {
                        "error": {
                            "message": "database unavailable",
                            "type": "server_error",
                            "code": "upstream_error",
                        },
                        "request_id": "req-db-down",
                    }
                ).encode("utf-8"),
            )
        raise AssertionError(url)

    results = run_postgres_down_smoke(
        base_url="http://localhost:4000",
        master_key="local-master",
        business_key="employee-virtual-key-secret",
        fetch=fetch,
        sensitive_values=("aimanager",),
    )

    assert [result.name for result in results] == [
        "readiness_fails",
        "provider_passthrough_still_blocked",
        "business_call_fails_safely",
    ]
    assert all(result.passed for result in results)


def test_postgres_down_smoke_fails_if_business_call_succeeds() -> None:
    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        if url.endswith("/health/readiness"):
            return HttpResponse(503, {}, b"")
        if url.endswith("/anthropic/messages"):
            return HttpResponse(403, {"x-aimanager-policy-code": "aimanager_passthrough_blocked"}, b"{}")
        if url.endswith("/v1/chat/completions"):
            return HttpResponse(200, {}, b'{"choices":[]}')
        raise AssertionError(url)

    results = run_postgres_down_smoke(
        base_url="http://localhost:4000",
        master_key="local-master",
        fetch=fetch,
    )

    business_result = results[-1]
    assert business_result.name == "business_call_fails_safely"
    assert not business_result.passed
    assert "expected 5xx" in business_result.detail


def test_postgres_down_smoke_fails_on_secret_like_response_body() -> None:
    def fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        if url.endswith("/health/readiness"):
            return HttpResponse(503, {}, b"")
        if url.endswith("/anthropic/messages"):
            return HttpResponse(403, {"x-aimanager-policy-code": "aimanager_passthrough_blocked"}, b"{}")
        if url.endswith("/v1/chat/completions"):
            return HttpResponse(
                500,
                {},
                b"postgresql://dbuser:dbpassword@db:5432/aimanager failed with Bearer secret",
            )
        raise AssertionError(url)

    results = run_postgres_down_smoke(
        base_url="http://localhost:4000",
        master_key="local-master",
        fetch=fetch,
    )

    business_result = results[-1]
    assert not business_result.passed
    assert "secret-like content" in business_result.detail

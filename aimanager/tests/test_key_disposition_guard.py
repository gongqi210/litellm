import asyncio
import hashlib
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from aimanager.asgi import YcapiOnlyAllowlistMiddleware, _litellm_key_disposition_from_db
from aimanager.policy import RouteDecision


def test_business_surface_rejects_blocked_key_before_downstream() -> None:
    audit_events: list[dict[str, object]] = []
    checked_tokens: list[str] = []

    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        raise AssertionError("blocked business keys must not reach downstream LiteLLM")

    async def key_disposition_checker(token: str):
        checked_tokens.append(token)
        return RouteDecision(
            allowed=False,
            code="aimanager_key_blocked",
            message="Key is blocked. Update via `/key/unblock` if you're an admin.",
            status_code=401,
        )

    app = YcapiOnlyAllowlistMiddleware(
        downstream,
        audit_sink=audit_events.append,
        surface="business",
        key_disposition_checker=key_disposition_checker,
    )

    messages = asyncio.run(
        _call_asgi(
            app,
            "POST",
            "/v1/chat/completions",
            headers=[
                (b"authorization", b"Bearer sk-frozen-business-key"),
                (b"x-request-id", b"req-blocked-key"),
                (b"x-aimanager-key-alias", b"market-key"),
            ],
            body=json.dumps({"model": "gemini-2.5-flash", "messages": []}).encode("utf-8"),
        )
    )

    assert checked_tokens == ["sk-frozen-business-key"]
    assert messages[0]["status"] == 401
    response_headers = dict(messages[0]["headers"])
    assert response_headers[b"x-aimanager-policy-code"] == b"aimanager_key_blocked"
    body = json.loads(messages[1]["body"])
    assert body["request_id"] == "req-blocked-key"
    assert body["error"]["code"] == "aimanager_key_blocked"
    assert "key is blocked" in body["error"]["message"].lower()
    assert len(audit_events) == 1
    event = audit_events[0]
    assert event["event_type"] == "policy_blocked"
    assert event["reason"] == "aimanager_key_blocked"
    assert event["subject_key_alias"] == "market-key"
    assert event["metadata"]["path"] == "/v1/chat/completions"
    assert event["metadata"]["status_code"] == 401


def test_management_surface_rejects_blocked_virtual_key_on_inference_routes() -> None:
    checked_tokens: list[str] = []

    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        raise AssertionError("blocked virtual keys must not reach management-surface inference routes")

    async def key_disposition_checker(token: str):
        checked_tokens.append(token)
        return RouteDecision(
            allowed=False,
            code="aimanager_key_blocked",
            message="Key is blocked. Update via `/key/unblock` if you're an admin.",
            status_code=401,
        )

    app = YcapiOnlyAllowlistMiddleware(
        downstream,
        surface="management",
        key_disposition_checker=key_disposition_checker,
    )

    messages = asyncio.run(
        _call_asgi(
            app,
            "POST",
            "/v1/chat/completions",
            headers=[(b"x-api-key", b"sk-frozen-management-key")],
            body=json.dumps({"model": "gemini-2.5-flash", "messages": []}).encode("utf-8"),
        )
    )

    assert checked_tokens == ["sk-frozen-management-key"]
    assert messages[0]["status"] == 401
    response_headers = dict(messages[0]["headers"])
    assert response_headers[b"x-aimanager-policy-code"] == b"aimanager_key_blocked"


@pytest.mark.parametrize(
    ("header_name", "header_value"),
    (
        (b"API-Key", b"sk-frozen-business-key"),
        (b"x-api-key", b"sk-frozen-business-key"),
        (b"x-goog-api-key", b"sk-frozen-business-key"),
        (b"Ocp-Apim-Subscription-Key", b"sk-frozen-business-key"),
        (b"x-litellm-api-key", b"sk-frozen-business-key"),
        (b"x-litellm-api-key", b"Bearer sk-frozen-business-key"),
    ),
)
def test_business_surface_rejects_blocked_key_from_litellm_credential_headers(
    header_name: bytes,
    header_value: bytes,
) -> None:
    checked_tokens: list[str] = []

    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        raise AssertionError("blocked business keys must not reach downstream LiteLLM")

    async def key_disposition_checker(token: str):
        checked_tokens.append(token)
        return RouteDecision(
            allowed=False,
            code="aimanager_key_blocked",
            message="Key is blocked. Update via `/key/unblock` if you're an admin.",
            status_code=401,
        )

    app = YcapiOnlyAllowlistMiddleware(
        downstream,
        surface="business",
        key_disposition_checker=key_disposition_checker,
    )

    messages = asyncio.run(
        _call_asgi(
            app,
            "POST",
            "/v1/chat/completions",
            headers=[
                (header_name, header_value),
                (b"x-request-id", b"req-blocked-alt-header"),
            ],
            body=json.dumps({"model": "gemini-2.5-flash", "messages": []}).encode("utf-8"),
        )
    )

    assert checked_tokens == ["sk-frozen-business-key"]
    assert messages[0]["status"] == 401
    response_headers = dict(messages[0]["headers"])
    assert response_headers[b"x-aimanager-policy-code"] == b"aimanager_key_blocked"
    body = json.loads(messages[1]["body"])
    assert body["request_id"] == "req-blocked-alt-header"
    assert body["error"]["code"] == "aimanager_key_blocked"


def test_business_surface_prefers_x_litellm_api_key_over_authorization_like_litellm() -> None:
    checked_tokens: list[str] = []

    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        raise AssertionError("blocked business keys must not reach downstream LiteLLM")

    async def key_disposition_checker(token: str):
        checked_tokens.append(token)
        return RouteDecision(
            allowed=False,
            code="aimanager_key_blocked",
            message="Key is blocked. Update via `/key/unblock` if you're an admin.",
            status_code=401,
        )

    app = YcapiOnlyAllowlistMiddleware(
        downstream,
        surface="business",
        key_disposition_checker=key_disposition_checker,
    )

    messages = asyncio.run(
        _call_asgi(
            app,
            "POST",
            "/v1/chat/completions",
            headers=[
                (b"authorization", b"Bearer sk-active-business-key"),
                (b"x-litellm-api-key", b"sk-frozen-business-key"),
            ],
            body=json.dumps({"model": "gemini-2.5-flash", "messages": []}).encode("utf-8"),
        )
    )

    assert checked_tokens == ["sk-frozen-business-key"]
    assert messages[0]["status"] == 401


def test_business_surface_does_not_run_key_disposition_without_litellm_credential() -> None:
    calls: list[dict[str, object]] = []
    checked_tokens: list[str] = []

    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        calls.append(scope)
        await send({"type": "http.response.start", "status": 401, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def key_disposition_checker(token: str):
        checked_tokens.append(token)
        return None

    app = YcapiOnlyAllowlistMiddleware(
        downstream,
        surface="business",
        key_disposition_checker=key_disposition_checker,
    )

    messages = asyncio.run(_call_asgi(app, "POST", "/v1/chat/completions"))

    assert checked_tokens == []
    assert len(calls) == 1
    assert messages[0]["status"] == 401


def test_business_surface_rejects_master_key_before_downstream(monkeypatch) -> None:
    audit_events: list[dict[str, object]] = []

    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        raise AssertionError("business surface must not accept the LiteLLM master key")

    async def key_disposition_checker(token: str):
        raise AssertionError("master key should be rejected before key disposition lookup")

    monkeypatch.setenv("LITELLM_MASTER_KEY", "master-secret")
    app = YcapiOnlyAllowlistMiddleware(
        downstream,
        audit_sink=audit_events.append,
        surface="business",
        key_disposition_checker=key_disposition_checker,
    )

    messages = asyncio.run(
        _call_asgi(
            app,
            "POST",
            "/v1/chat/completions",
            headers=[
                (b"authorization", b"Bearer master-secret"),
                (b"x-request-id", b"req-master-business"),
            ],
            body=json.dumps({"model": "gemini-2.5-flash", "messages": []}).encode("utf-8"),
        )
    )

    assert messages[0]["status"] == 403
    response_headers = dict(messages[0]["headers"])
    assert response_headers[b"x-aimanager-policy-code"] == b"aimanager_business_token_forbidden"
    body = json.loads(messages[1]["body"])
    assert body["request_id"] == "req-master-business"
    assert body["error"]["code"] == "aimanager_business_token_forbidden"
    assert "virtual key" in body["error"]["message"].lower()
    assert audit_events[0]["reason"] == "aimanager_business_token_forbidden"


def test_business_surface_rejects_ycapi_token_before_downstream(monkeypatch) -> None:
    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        raise AssertionError("business surface must not accept the upstream ycapi token")

    async def key_disposition_checker(token: str):
        raise AssertionError("upstream token should be rejected before key disposition lookup")

    monkeypatch.setenv("YCAPI_API_TOKEN", "ycapi-upstream-secret")
    app = YcapiOnlyAllowlistMiddleware(
        downstream,
        surface="business",
        key_disposition_checker=key_disposition_checker,
    )

    messages = asyncio.run(
        _call_asgi(
            app,
            "POST",
            "/v1/chat/completions",
            headers=[(b"authorization", b"Bearer ycapi-upstream-secret")],
            body=json.dumps({"model": "gemini-2.5-flash", "messages": []}).encode("utf-8"),
        )
    )

    assert messages[0]["status"] == 403
    response_headers = dict(messages[0]["headers"])
    assert response_headers[b"x-aimanager-policy-code"] == b"aimanager_business_token_forbidden"


@pytest.mark.parametrize(
    ("env_name", "secret", "header_name"),
    (
        ("LITELLM_MASTER_KEY", "master-secret", b"x-api-key"),
        ("YCAPI_API_TOKEN", "ycapi-upstream-secret", b"x-goog-api-key"),
    ),
)
def test_business_surface_rejects_admin_or_upstream_tokens_from_litellm_credential_headers(
    monkeypatch,
    env_name: str,
    secret: str,
    header_name: bytes,
) -> None:
    async def downstream(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        raise AssertionError("business surface must not accept admin or upstream tokens")

    async def key_disposition_checker(token: str):
        raise AssertionError("forbidden business tokens should be rejected before key disposition lookup")

    monkeypatch.setenv(env_name, secret)
    app = YcapiOnlyAllowlistMiddleware(
        downstream,
        surface="business",
        key_disposition_checker=key_disposition_checker,
    )

    messages = asyncio.run(
        _call_asgi(
            app,
            "POST",
            "/v1/chat/completions",
            headers=[(header_name, secret.encode("utf-8"))],
            body=json.dumps({"model": "gemini-2.5-flash", "messages": []}).encode("utf-8"),
        )
    )

    assert messages[0]["status"] == 403
    response_headers = dict(messages[0]["headers"])
    assert response_headers[b"x-aimanager-policy-code"] == b"aimanager_business_token_forbidden"


def test_default_key_disposition_checker_rejects_active_blocked_key(monkeypatch) -> None:
    observed_where: list[dict[str, object]] = []

    class VerificationTokenTable:
        async def find_unique(self, *, where):  # type: ignore[no-untyped-def]
            observed_where.append(where)
            return SimpleNamespace(blocked=True)

    class DeletedVerificationTokenTable:
        async def find_first(self, *, where):  # type: ignore[no-untyped-def]
            raise AssertionError("deleted key table should not be queried for active blocked keys")

    fake_proxy_server = ModuleType("litellm.proxy.proxy_server")
    fake_proxy_server.prisma_client = SimpleNamespace(
        db=SimpleNamespace(
            litellm_verificationtoken=VerificationTokenTable(),
            litellm_deletedverificationtoken=DeletedVerificationTokenTable(),
        )
    )
    monkeypatch.setitem(sys.modules, "litellm.proxy.proxy_server", fake_proxy_server)

    decision = asyncio.run(_litellm_key_disposition_from_db("sk-active-blocked"))

    assert decision is not None
    assert decision.status_code == 401
    assert decision.code == "aimanager_key_blocked"
    assert "key is blocked" in decision.message.lower()
    assert observed_where
    assert observed_where[0]["token"] != "sk-active-blocked"


def test_default_key_disposition_checker_allows_active_unblocked_key(monkeypatch) -> None:
    observed_active_where: list[dict[str, object]] = []
    raw_token = "sk-active-unblocked"
    expected_hashed_token = hashlib.sha256(raw_token.encode()).hexdigest()

    class VerificationTokenTable:
        async def find_unique(self, *, where):  # type: ignore[no-untyped-def]
            observed_active_where.append(where)
            return SimpleNamespace(blocked=False)

    class DeletedVerificationTokenTable:
        async def find_first(self, *, where):  # type: ignore[no-untyped-def]
            raise AssertionError("deleted key table should not be queried for active unblocked keys")

    fake_proxy_server = ModuleType("litellm.proxy.proxy_server")
    fake_proxy_server.prisma_client = SimpleNamespace(
        db=SimpleNamespace(
            litellm_verificationtoken=VerificationTokenTable(),
            litellm_deletedverificationtoken=DeletedVerificationTokenTable(),
        )
    )
    monkeypatch.setitem(sys.modules, "litellm.proxy.proxy_server", fake_proxy_server)

    decision = asyncio.run(_litellm_key_disposition_from_db(raw_token))

    assert decision is None
    assert observed_active_where == [{"token": expected_hashed_token}]


def test_default_key_disposition_checker_rejects_deleted_key(monkeypatch) -> None:
    class VerificationTokenTable:
        async def find_unique(self, *, where):  # type: ignore[no-untyped-def]
            return None

    class DeletedVerificationTokenTable:
        async def find_first(self, *, where):  # type: ignore[no-untyped-def]
            return SimpleNamespace(token=where["token"])

    fake_proxy_server = ModuleType("litellm.proxy.proxy_server")
    fake_proxy_server.prisma_client = SimpleNamespace(
        db=SimpleNamespace(
            litellm_verificationtoken=VerificationTokenTable(),
            litellm_deletedverificationtoken=DeletedVerificationTokenTable(),
        )
    )
    monkeypatch.setitem(sys.modules, "litellm.proxy.proxy_server", fake_proxy_server)

    decision = asyncio.run(_litellm_key_disposition_from_db("sk-deleted"))

    assert decision is not None
    assert decision.status_code == 401
    assert decision.code == "aimanager_key_revoked"
    assert "revoked" in decision.message.lower()


def test_default_key_disposition_checker_defers_unknown_key_to_litellm_auth(monkeypatch) -> None:
    observed_active_where: list[dict[str, object]] = []
    observed_deleted_where: list[dict[str, object]] = []
    raw_token = "sk-unknown-to-disposition-guard"
    expected_hashed_token = hashlib.sha256(raw_token.encode()).hexdigest()

    class VerificationTokenTable:
        async def find_unique(self, *, where):  # type: ignore[no-untyped-def]
            observed_active_where.append(where)
            return None

    class DeletedVerificationTokenTable:
        async def find_first(self, *, where):  # type: ignore[no-untyped-def]
            observed_deleted_where.append(where)
            return None

    fake_proxy_server = ModuleType("litellm.proxy.proxy_server")
    fake_proxy_server.prisma_client = SimpleNamespace(
        db=SimpleNamespace(
            litellm_verificationtoken=VerificationTokenTable(),
            litellm_deletedverificationtoken=DeletedVerificationTokenTable(),
        )
    )
    monkeypatch.setitem(sys.modules, "litellm.proxy.proxy_server", fake_proxy_server)

    decision = asyncio.run(_litellm_key_disposition_from_db(raw_token))

    assert decision is None
    assert observed_active_where == [{"token": expected_hashed_token}]
    assert observed_deleted_where == [{"token": expected_hashed_token}]


def test_default_key_disposition_checker_fails_closed_when_prisma_uninitialized(monkeypatch) -> None:
    fake_proxy_server = ModuleType("litellm.proxy.proxy_server")
    fake_proxy_server.prisma_client = SimpleNamespace(db=None)
    monkeypatch.setitem(sys.modules, "litellm.proxy.proxy_server", fake_proxy_server)

    with pytest.raises(RuntimeError, match="Prisma client is not initialized"):
        asyncio.run(_litellm_key_disposition_from_db("sk-db-unavailable"))


async def _call_asgi(
    app: YcapiOnlyAllowlistMiddleware,
    method: str,
    path: str,
    headers: list[tuple[bytes, bytes]] | None = None,
    body: bytes = b"",
) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = []
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers or [],
    }
    await app(scope, _receive_once(body), _collecting_send(messages))
    return messages


def _receive_once(body: bytes):
    sent = False

    async def receive() -> dict[str, object]:
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


def _collecting_send(messages: list[dict[str, object]]):
    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    return send

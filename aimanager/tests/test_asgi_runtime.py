from __future__ import annotations

import asyncio
import sys
from types import ModuleType

from aimanager import litellm_entrypoint
from aimanager.asgi import _LazyLiteLLMProxyApp


def test_lazy_proxy_registers_aimanager_hooks_after_litellm_proxy_import(monkeypatch) -> None:
    async def downstream(scope, receive, send) -> None:
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    fake_litellm = ModuleType("litellm")
    fake_proxy = ModuleType("litellm.proxy")
    fake_proxy_server = ModuleType("litellm.proxy.proxy_server")
    fake_proxy_server.app = downstream
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm)
    monkeypatch.setitem(sys.modules, "litellm.proxy", fake_proxy)
    monkeypatch.setitem(sys.modules, "litellm.proxy.proxy_server", fake_proxy_server)

    registrations: list[str] = []
    monkeypatch.setattr(
        litellm_entrypoint,
        "register_aimanager_image_model_costs",
        lambda: registrations.append("image-costs"),
    )
    monkeypatch.setattr(
        litellm_entrypoint,
        "register_aimanager_enforced_params_guard",
        lambda: registrations.append("enforced-params-guard"),
    )

    app = _LazyLiteLLMProxyApp()
    messages = asyncio.run(_call_asgi(app))
    asyncio.run(_call_asgi(app))

    assert messages[0]["status"] == 204
    assert registrations == ["image-costs", "enforced-params-guard"]


async def _call_asgi(app):
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app({"type": "http", "method": "GET", "path": "/health/liveliness"}, receive, send)
    return messages

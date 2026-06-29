# AiManager Research Report

Date: 2026-06-29

## Finding

LiteLLM already supports OpenAI-compatible upstreams through `model: openai/<model>`, `api_base`, and `api_key` in `model_list`. ycapi is OpenAI-compatible for chat and image generation, so AiManager can be an additive deployment overlay without changing LiteLLM SDK internals.

## ycapi Boundary

- Canonical API base: `https://ycapi.ycaicloud.com/v1`
- Auth: `Authorization: Bearer <ycapi token>`
- AiManager must use only `YCAPI_BASE_URL` and `YCAPI_API_TOKEN` for upstream model calls.

## LiteLLM Capabilities Retained

- Admin UI
- Virtual keys
- Teams and users
- Budgets and rate limits
- Usage and spend logs
- Model access control at the LiteLLM key/team layer

## Compatibility Notes

| ycapi alias | AiManager v1 status | Reason |
| --- | --- | --- |
| `gemini-2.5-flash` | Enabled | OpenAI-compatible chat/vision |
| `deepseek-chat` | Enabled | OpenAI-compatible chat |
| `ycapi-image-1` | Enabled | OpenAI-compatible image path |
| `ycapi-video-1` | Deferred | ycapi video has asynchronous creation/polling semantics that do not match LiteLLM's built-in OpenAI video schema |

## Decision

Use a config-only LiteLLM overlay for v1. Keep `STORE_MODEL_IN_DB=false` so operators cannot accidentally add direct upstream providers from the UI. Future model-management UI changes should preserve the same ycapi-only validation.

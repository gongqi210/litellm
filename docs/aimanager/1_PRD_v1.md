# AiManager PRD v1

## Objective

Provide a company-managed AI API gateway based on LiteLLM where employees and internal systems call AiManager, and AiManager forwards upstream model calls only through ycapi.

## Users

- Admins manage keys, teams, budgets, model access, and usage review in LiteLLM.
- Employees and internal applications call OpenAI-compatible endpoints exposed by AiManager.
- Platform operators maintain ycapi tokens and deployment configuration.

## Scope

- Keep the full LiteLLM proxy/admin deployment.
- Configure ycapi-backed chat and image models.
- Store secrets only in local `.env` or deployment secret managers.
- Provide a validation command that fails if direct provider keys or direct provider API bases are introduced into the AiManager config.

## Out Of Scope For v1

- Direct vendor model access.
- `ycapi-video-1` exposure through LiteLLM's built-in video model list.
- A custom model CRUD flow that safely writes ycapi-only model rows to DB.
- Production secret manager integration.

## Success Criteria

- `/v1/models` advertises only AiManager-approved ycapi-backed models.
- `/v1/chat/completions` works for `gemini-2.5-flash` and `deepseek-chat` after valid secrets are configured.
- `/v1/images/generations` works for `ycapi-image-1` after valid secrets are configured.
- LiteLLM admin UI remains available at `/ui`.
- Config validation fails on direct provider upstreams.

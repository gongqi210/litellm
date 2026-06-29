# AiManager

AiManager is the company-facing LiteLLM management layer for ycapi. It keeps LiteLLM's admin UI, virtual keys, teams, budgets, rate limits, and spend logs, while locking all configured upstream model calls to ycapi.

## Boundary

- Upstream API base: `YCAPI_BASE_URL`, default `https://ycapi.ycaicloud.com/v1`.
- Upstream credential: `YCAPI_API_TOKEN`.
- Do not add direct provider keys such as `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, Azure, Bedrock, or Vertex credentials to this deployment.
- `STORE_MODEL_IN_DB` is disabled by default so the YAML model list stays the source of truth for the ycapi-only boundary.
- `ycapi-video-1` is not exposed in this first config because LiteLLM's built-in video route expects vendor-specific schemas. Add a ycapi video adapter or audited authenticated passthrough route before enabling it.

## Enabled Models

| Client model | LiteLLM upstream | ycapi endpoint family |
| --- | --- | --- |
| `gemini-2.5-flash` | `openai/gemini-2.5-flash` | chat / vision |
| `deepseek-chat` | `openai/deepseek-chat` | chat |
| `ycapi-image-1` | `openai/ycapi-image-1` | image generation |

Pricing fields are explicit and nonzero so LiteLLM cannot silently inherit same-named public model prices or record zero image spend. Current values are M1 technical guardrail prices; finance-approved transfer prices still need a recorded `pricing_version`, approver, currency, tax mode, and effective date before production chargeback.

## Runtime Boundary

The local deployment starts `aimanager.asgi:app` with `CONFIG_FILE_PATH=/app/config.yaml`. The AiManager ASGI wrapper is the primary runtime boundary in front of the LiteLLM proxy app.

M1 business API allowlist:

- `GET /v1/models`
- `POST /v1/chat/completions`
- `POST /v1/images/generations`

M1 blocks provider passthrough, Google native `:generateContent` routes, `/pass-through-endpoints`, `/config/update`, model write routes, and uncommitted business APIs such as `/v1/embeddings` and `/v1/completions`.

## Key Governance

Before creating LiteLLM virtual keys through API or UI automation, normalize the request with `aimanager.governance.normalize_key_request`.

Required top-level fields:

- `user_id`
- `team_id`
- `models`
- `max_budget`
- `rpm_limit`
- `tpm_limit`
- `duration`

Required metadata:

- `owner`
- `department_id`
- `project_id`
- `cost_center_id`
- `scenario_l1`
- `scenario_l2`
- `approver`
- `internal_or_external`

Shared keys must call `normalize_key_request(payload, shared_key=True)`, which adds LiteLLM `enforced_params`:

```json
[
  "user",
  "metadata.scenario_l1",
  "metadata.end_user_principal"
]
```

This pure helper is tested locally. It still needs to be wired into the actual LiteLLM key creation flow before AC-08 can be marked fully PASS.

## Finance Reporting

`aimanager.finance` provides pure helpers for the finance export layer:

- `normalize_spend_record(row)` converts LiteLLM spend-log-like rows into AiManager finance dimensions.
- `aggregate_daily_usage(rows)` groups usage by date, department, project, cost center, employee, key, model, endpoint, currency, and pricing version.
- `aggregate_monthly_usage(rows)` produces the same governance dimensions at month grain for finance close.
- `reconcile_monthly_usage(aimanager_rows, ycapi_rows)` compares AiManager monthly totals with ycapi bill rows and marks differences as `matched` or `needs_review`.

Money values use `Decimal`. Missing ownership dimensions are kept visible as `unassigned` instead of being dropped. These helpers are tested locally but are not yet wired to LiteLLM `SpendLogs`, scheduled exports, or real ycapi bill ingestion.

## Audit Events

`aimanager.audit.build_audit_event` standardizes M1 risk events:

- `passthrough_blocked`
- `budget_blocked`
- `key_frozen`
- `key_revoked`

Each event carries `event_id`, `event_type`, `severity`, `occurred_at`, `actor`, `subject_key_alias`, team/department/project/cost-center dimensions, `reason`, `request_id`, and `metadata`. This is the local event contract for logs, alerts, and executive/finance dashboards. Runtime emitters still need to be attached to policy blocking, budget enforcement, and key lifecycle operations.

## Run

```bash
cd /Volumes/AI-projects/01-yca-AiManager/aimanager
cp .env.example .env
# Fill LITELLM_MASTER_KEY, YCAPI_API_TOKEN, and POSTGRES_PASSWORD in .env.
docker compose up --build
```

Admin UI: <http://localhost:4000/ui>

## Validate

```bash
cd /Volumes/AI-projects/01-yca-AiManager
uv run --no-project --with pyyaml python -m aimanager.scripts.validate_config aimanager/config.yaml
PYTHONPATH="$PWD" uv run --no-project --with pytest --with pyyaml pytest aimanager/tests -q
docker compose -f aimanager/docker-compose.yml config
```

Smoke test after `.env` is populated:

```bash
curl -s http://localhost:4000/v1/models \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY"

curl -s http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-2.5-flash","messages":[{"role":"user","content":"Reply with exactly: ok"}]}'

curl -i http://localhost:4000/v1beta/models/gemini-2.5-flash:generateContent \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{}'
```

The last command must return an AiManager 403 policy error and must not reach ycapi. A full runtime boot, model-list check, nonzero spend-log check, and live ycapi chat/image smoke are still required before M1 can enter business trial.

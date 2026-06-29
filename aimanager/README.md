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

The local deployment starts `python -m aimanager.litellm_entrypoint --config=/app/config.yaml`. This keeps LiteLLM's CLI initialization path for config loading, database URL handling, migration checks, and server options, while overriding the final uvicorn app to `aimanager.asgi:app`.

`aimanager.asgi` is the primary runtime boundary in front of the LiteLLM proxy app. It forwards lifespan events to the wrapped LiteLLM app so startup and shutdown hooks still run.

The entrypoint fails fast unless these runtime environment variables are non-empty:

- `LITELLM_MASTER_KEY`
- `YCAPI_BASE_URL`
- `YCAPI_API_TOKEN`

The entrypoint also forces `LITELLM_LOCAL_MODEL_COST_MAP=True` before importing LiteLLM. This keeps startup offline and prevents LiteLLM's import-time remote model cost map fetch from blocking the gateway when external network/TLS handshakes are slow or unavailable.

After the LiteLLM proxy app is loaded, `aimanager.asgi` registers image-generation pricing from `CONFIG_FILE_PATH` back into LiteLLM's runtime model cost map. This preserves nonzero `ycapi-image-1` spend logging even when LiteLLM stores `SpendLogs.model` as the provider-prefixed `openai/ycapi-image-1` alias.

`docker-compose.yml` explicitly passes these variables into the container from either the shell or `aimanager/.env`; this keeps local `docker compose -f aimanager/docker-compose.yml up` checks aligned with production startup behavior.

M1 business API allowlist:

- `GET /v1/models`
- `POST /v1/chat/completions`
- `POST /v1/images/generations`

M1 blocks provider passthrough, Google native `:generateContent` routes, `/pass-through-endpoints`, `/config/update`, model write routes, and uncommitted business APIs such as `/v1/embeddings` and `/v1/completions`.

Route surfaces:

- `AIMANAGER_ROUTE_SURFACE=business` is the default data-plane surface. It allows only the M1 business API allowlist plus health checks. LiteLLM Admin UI, key, team, user, budget, and spend routes are blocked on this surface.
- `AIMANAGER_ROUTE_SURFACE=management` is the controlled admin surface. It allows LiteLLM UI/key/team/user/budget/spend management routes, while still blocking provider passthrough, Google native routes, runtime config mutation, pass-through endpoint mutation, cache/reload mutation, and model writes.
- The local compose admin surface is behind the `admin` profile and binds only `127.0.0.1:4001`.

## Key Governance

The management surface intercepts `POST /key/generate` before the request reaches LiteLLM and normalizes it with `aimanager.governance.normalize_key_request`. Invalid key-creation payloads return HTTP 400 with `error.code=aimanager_key_governance_invalid`, preserve `request_id`, emit a structured `policy_blocked` audit event, and never reach downstream LiteLLM.

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

Shared keys must set `metadata.shared_key=true`; AiManager then calls `normalize_key_request(payload, shared_key=True)` and injects LiteLLM `enforced_params`:

```json
[
  "user",
  "metadata.scenario_l1",
  "metadata.end_user_principal"
]
```

The helper, management-surface ASGI wiring, and running API key-creation path are tested locally. UI automation for the same flow remains useful but is not required for AC-08.

Runtime inference enforcement for LiteLLM `metadata.enforced_params` is an Enterprise feature in this fork. AiManager can create shared keys with the required metadata, but spend-log smoke defaults to a non-shared employee key; exercising `--shared-key` against inference requires a LiteLLM Enterprise license or an AiManager-owned OSS enforcement layer.

## Finance Reporting

`aimanager.finance` provides pure helpers for the finance export layer:

- `normalize_spend_record(row)` converts LiteLLM `SpendLogs`-shaped rows into AiManager finance dimensions, including camelCase `startTime`, `call_type` endpoint inference, provider-prefixed `openai/<model>` normalization, and nested `metadata.user_api_key_metadata` / `metadata.spend_logs_metadata`.
- `normalize_ycapi_bill_record(row)` converts ycapi monthly bill rows from JSON/CSV-style fields into the reconciliation contract.
- `aggregate_daily_usage(rows)` groups usage by date, department, project, cost center, employee, key, model, endpoint, currency, and pricing version.
- `aggregate_monthly_usage(rows)` produces the same governance dimensions at month grain for finance close.
- `reconcile_monthly_usage(aimanager_rows, ycapi_rows)` compares AiManager monthly totals with ycapi bill rows and marks differences as `matched` or `needs_review`.
- `build_finance_export_bundle(spend_rows=..., ycapi_bill_rows=...)` returns the three M1 finance exports: `aimanager_usage_daily.csv`, `aimanager_finance_monthly.csv`, and `aimanager_reconciliation.csv`.

Money values use `Decimal`. Missing ownership dimensions are kept visible as `unassigned` instead of being dropped; monthly close rows with missing department/project/cost center are marked `close_status=blocked_unassigned`.

Local export command:

```bash
PYTHONPATH="$PWD" uv run --no-project python -m aimanager.scripts.export_finance \
  --spend-file /path/to/litellm-spendlogs.json \
  --ycapi-bill-file /path/to/ycapi-monthly-bill.csv \
  --output-dir /tmp/aimanager-finance-export
```

`--spend-file` and `--ycapi-bill-file` accept JSON arrays or CSV files. Real production ycapi bill files or API output still need to be supplied during monthly close; the local importer and export schema are covered by tests.

## Audit Events

`aimanager.audit.build_audit_event` standardizes M1 risk events:

- `passthrough_blocked`
- `policy_blocked`
- `budget_blocked`
- `key_frozen`
- `key_revoked`

Each event carries `event_id`, `event_type`, `severity`, `occurred_at`, `actor`, `subject_key_alias`, team/department/project/cost-center dimensions, `reason`, `request_id`, and `metadata`. This is the local event contract for logs, alerts, and executive/finance dashboards.

`aimanager.asgi` emits structured `aimanager_audit_event=...` logs before returning policy 403 responses. Provider/native bypass attempts emit `passthrough_blocked`; config/model writes and default-denied routes emit `policy_blocked`. Only allowlisted governance headers are copied into audit dimensions; Authorization and Cookie values are not logged.

On the management surface, key lifecycle operations are also governed before they reach LiteLLM. `POST /key/block` and `POST /key/delete` require `x-aimanager-actor` plus either `x-aimanager-reason` or `x-aimanager-disposition-reason`; missing disposition headers return HTTP 400 with `error.code=aimanager_key_lifecycle_invalid`. Successful `/key/block` responses emit `key_frozen`; successful `/key/delete` responses emit `key_revoked`. The downstream LiteLLM response body is preserved.

## Run

```bash
cd /Volumes/AI-projects/01-yca-AiManager/aimanager
cp .env.example .env
# Fill LITELLM_MASTER_KEY, YCAPI_API_TOKEN, and POSTGRES_PASSWORD in .env.
docker compose up --build
```

Business API: <http://localhost:4000/v1/models>

To run the local management surface:

```bash
docker compose --profile admin up --build
```

Admin UI: <http://127.0.0.1:4001/ui>

The default business port `4000` intentionally blocks `/ui`, `/key/*`, `/team/*`, `/user/*`, `/budget/*`, and `/spend/*`.

## Validate

```bash
cd /Volumes/AI-projects/01-yca-AiManager
uv run --no-project --with pyyaml python -m aimanager.scripts.validate_config aimanager/config.yaml
PYTHONPATH="$PWD" uv run --no-project --with pytest --with pyyaml pytest aimanager/tests -q
docker compose -f aimanager/docker-compose.yml config
docker compose -f aimanager/docker-compose.yml --profile admin config
```

With a running local proxy:

```bash
LITELLM_MASTER_KEY=aimanager-local-master-key \
PYTHONPATH="$PWD" uv run --no-project python -m aimanager.scripts.smoke_blocked_routes \
  --base-url http://localhost:4000
```

Mock ycapi runtime spend smoke:

```bash
PYTHONPATH="$PWD" uv run --no-project python -m aimanager.scripts.mock_ycapi \
  --host 0.0.0.0 --port 18080

LITELLM_MASTER_KEY=aimanager-local-master-key \
YCAPI_API_TOKEN=mock-ycapi-token \
YCAPI_BASE_URL=http://host.docker.internal:18080/v1 \
docker compose -f aimanager/docker-compose.yml --profile admin up -d --build db aimanager aimanager-admin

LITELLM_MASTER_KEY=aimanager-local-master-key \
PYTHONPATH="$PWD" uv run --no-project --with pyyaml python -m aimanager.scripts.smoke_spend_logs \
  --master-key aimanager-local-master-key \
  --business-base-url http://localhost:4000 \
  --admin-base-url http://localhost:4001 \
  --project-directory "$PWD" \
  --poll-attempts 60 \
  --poll-interval 1
```

Expected result: `PASS`, `chat_spend > 0`, and `image_spend > 0`. The smoke creates a governed disposable employee key, sends chat and image calls through the business surface, reads `LiteLLM_SpendLogs` from Postgres, and deletes the key. It never prints the virtual key.

Mock ycapi runtime budget-block smoke:

```bash
LITELLM_MASTER_KEY=aimanager-local-master-key \
PYTHONPATH="$PWD" uv run --no-project --with pyyaml python -m aimanager.scripts.smoke_budget_block \
  --master-key aimanager-local-master-key \
  --business-base-url http://localhost:4000 \
  --admin-base-url http://localhost:4001 \
  --project-directory "$PWD" \
  --budget 0.005 \
  --poll-attempts 60 \
  --poll-interval 1
```

Expected result: `PASS`, `prime_status=200`, `blocked_status=429`, `budget_error_type=budget_exceeded`, and a `REASON` line containing `Budget has been exceeded`. The smoke creates a governed disposable employee key with `max_budget` below one image call, primes spend through the business image endpoint, verifies the next business request is blocked by LiteLLM key budget enforcement, and deletes the key. `LiteLLM_VerificationToken.spend` is batch-written and can still show `0.0` when the real-time spend counter has already blocked the request, so the 429 response is the primary proof.

Mock ycapi runtime key lifecycle smoke:

```bash
LITELLM_MASTER_KEY=aimanager-local-master-key \
PYTHONPATH="$PWD" uv run --no-project --with pyyaml python -m aimanager.scripts.smoke_key_lifecycle \
  --master-key aimanager-local-master-key \
  --business-base-url http://localhost:4000 \
  --admin-base-url http://localhost:4001 \
  --project-directory "$PWD" \
  --poll-attempts 60 \
  --poll-interval 1
```

Expected result: `PASS`, `freeze_status=200`, `freeze_reject_status=401`, `revoke_status=200`, `revoke_reject_status=401`, and `audit_events=key_frozen,key_revoked`. The smoke creates two disposable governed employee keys, proves each key can call the business surface before disposition, freezes one key through the admin surface and verifies subsequent business inference is rejected with `Key is blocked`, revokes the other key and verifies subsequent business inference is rejected as an invalid token, then polls the admin container logs for structured `key_frozen` and `key_revoked` audit events with actor, reason, request id, key alias, and governance dimensions. It never prints the virtual key.

The rendered default compose config must show `build.target: runtime`, `entrypoint: ["python", "-m", "aimanager.litellm_entrypoint"]`, `--config=/app/config.yaml`, `--enforce_prisma_migration_check`, `AIMANAGER_ROUTE_SURFACE=business`, and `LITELLM_LOCAL_MODEL_COST_MAP=True`. The rendered `--profile admin` config must also show `aimanager-admin`, `AIMANAGER_ROUTE_SURFACE=management`, `LITELLM_LOCAL_MODEL_COST_MAP=True`, and `127.0.0.1:4001:4000`.

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

The last command must return an AiManager 403 policy error and must not reach ycapi.

Latest local runtime smoke evidence:

- `docker compose -f aimanager/docker-compose.yml up -d --build aimanager` rebuilt the runtime image, then started Postgres and AiManager with local placeholder credentials.
- LiteLLM Prisma migrations and post-migration sanity check completed; application startup reached `Application startup complete`.
- AiManager forces LiteLLM's local model cost map before import; `docker run ... -e LITELLM_LOCAL_MODEL_COST_MAP=True ... import litellm` completed in about 3.1s after reproducing the remote cost-map TLS startup hang without that setting.
- `GET /health/liveliness` returned 200.
- `GET /v1/models` returned only `gemini-2.5-flash`, `deepseek-chat`, and `ycapi-image-1`.
- `POST /anthropic/messages` returned 403 with `x-aimanager-policy-code: aimanager_passthrough_blocked`.
- `POST /config/update` returned 403 with `x-aimanager-policy-code: aimanager_config_immutable`.
- `python -m aimanager.scripts.smoke_blocked_routes` verified 33 provider/native/config/cache/reload/model-write/uncommitted routes as AiManager 403 policy blocks.
- Container logs emitted 34 structured `aimanager_audit_event` entries for those blocked requests plus the business-surface `/ui` block, with no Authorization, Bearer token, or local placeholder token content in the audit log tail.
- Runtime surface split was verified locally: business port `4000` blocks `/ui` with AiManager 403, admin port `127.0.0.1:4001` returns LiteLLM UI redirect for `/ui`, and admin port still blocks `/config/field/update` with `aimanager_config_immutable`.
- Local tests split surfaces: business surface blocks LiteLLM management routes, while management surface allows UI/key/team/user/budget/spend routes and still blocks provider/config/cache/reload/model-write routes. Compose renders `aimanager-admin` only under the `admin` profile on `127.0.0.1:4001`.
- Local tests also prove management `POST /key/generate` rejects missing governance metadata with `aimanager_key_governance_invalid`, forwards normalized valid payloads, and injects `enforced_params` for `metadata.shared_key=true`.
- Runtime admin-surface smoke with rebuilt `aimanager-litellm:local` proved `POST /key/generate` without governance metadata returns HTTP 400, `x-aimanager-policy-code: aimanager_key_governance_invalid`, preserves `x-litellm-call-id`, and emits a matching `policy_blocked` audit event without Authorization/Bearer leakage.
- Runtime valid shared-key smoke proved `POST /key/generate` returns the required employee/team/model/budget/rate-limit/expiry fields plus department/project/cost-center/scenario/approver metadata and `metadata.enforced_params`; the local smoke key was deleted immediately after verification.
- Mock ycapi runtime spend smoke proved `POST /v1/chat/completions` and `POST /v1/images/generations` through AiManager write nonzero `LiteLLM_SpendLogs.spend`: chat `3.3e-06`, image `0.01`. Rows were recorded as `openai/gemini-2.5-flash` and `openai/ycapi-image-1`.
- Mock ycapi runtime budget-block smoke proved a disposable governed key with `max_budget=0.005` can spend `0.01` on a successful image request, then receive HTTP 429 `budget_exceeded` on the next business request. The response and LiteLLM container log both recorded `Budget has been exceeded! ... Current cost: 0.01, Max budget: 0.005`; AiManager also emitted a structured `aimanager_audit_event` with `event_type=budget_blocked`, `severity=high`, and `reason=budget_exceeded`.
- Mock ycapi runtime key-lifecycle smoke proved admin `POST /key/block` freezes a governed key, subsequent business chat returns HTTP 401 with `Key is blocked`, admin `POST /key/delete` revokes a second governed key, subsequent business chat returns HTTP 401 invalid-token/not-found, and the admin container logs contain `key_frozen` and `key_revoked` `aimanager_audit_event` records with actor `aimanager-ci`, disposition reasons, request ids, key aliases, and governance dimensions.

Remaining before business trial: metrics/alerting, UI automation for the key creation flow, production ycapi bill evidence, RBAC verification, broader failure-mode checks, and live ycapi smoke with the production token policy.

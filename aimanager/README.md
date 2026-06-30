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
- The local compose admin surface is behind the `admin` profile, binds only `127.0.0.1:4001`, and enables `AIMANAGER_RBAC_ENABLED=True`.

## Management RBAC

When `AIMANAGER_RBAC_ENABLED=True`, the management surface applies an AiManager pre-auth RBAC guard to high-risk management writes under `/key`, `/team`, `/user`, `/customer`, `/organization`, `/budget`, `/spend`, and `/global/spend`.

- Admin aliases such as `proxy_admin`, `system_admin`, `super_admin`, `aimanager_admin`, and `admin` can continue to those write paths, where LiteLLM's own authenticated management authorization still applies.
- Read-only and business roles such as `finance`, `ceo`, `audit`, `proxy_admin_viewer`, and unknown/missing roles receive HTTP 403 with `error.code=aimanager_rbac_denied`.
- Read-only roles can still reach read routes such as `/spend/logs`, `/global/spend`, and `/global/activity`.
- A role header never unlocks the business surface; business port `4000` keeps blocking all LiteLLM management routes.

The M1 implementation treats `x-aimanager-role` as a trusted internal header for mock/edge-gateway enforcement. In production, the admin surface must remain behind LiteLLM authentication plus a trusted reverse proxy or SSO layer that strips any client-supplied `x-aimanager-role` and injects the authenticated role. Do not treat this header as a standalone public authorization credential.

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

The helper, management-surface ASGI wiring, running API key-creation path, and LiteLLM Admin UI key-creation flow are tested locally. The UI sends numeric form values as strings; AiManager normalizes those strings to numbers before forwarding the governed payload to LiteLLM.

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

## Observability

`aimanager.runtime_metrics.AiManagerMetrics` records bounded runtime counters without using high-cardinality labels such as request id, key alias, path, or reason:

- `aimanager_audit_events_total{event_type,severity}` for `passthrough_blocked`, `policy_blocked`, `budget_blocked`, `key_frozen`, and `key_revoked`.
- `aimanager_http_responses_total{method,status_class,status_code}` for allowed traffic and AiManager policy responses, including 429 and 5xx.

`GET /metrics` is served by AiManager on the management surface only. The business surface still blocks `/metrics` with the standard AiManager policy response, so operational counters are not exposed on the employee API port by default.

Local log/report export:

```bash
PYTHONPATH="$PWD" uv run --no-project python -m aimanager.scripts.export_observability \
  --audit-log-file /path/to/aimanager.log \
  --request-status-file /path/to/request-status.csv \
  --output-file /tmp/aimanager-observability.json
```

The report contains request count, failed requests, failure rate, 429/5xx counts, latency/token/spend totals, observed request ids, audit event counts, bounded key/model buckets, and machine-readable alerts for high failure rate, 429, 5xx, missing request id, budget blocks, and passthrough blocks. Request ids stay in the JSON report for correlation, not in Prometheus labels.

WeCom alert routing from an exported report:

```bash
PYTHONPATH="$PWD" uv run --no-project python -m aimanager.scripts.route_observability_alerts \
  --report-file /tmp/aimanager-observability.json \
  --dry-run \
  --output-payload-file /tmp/aimanager-wecom-alert-payload.json

PYTHONPATH="$PWD" uv run --no-project python -m aimanager.scripts.route_observability_alerts \
  --report-file /tmp/aimanager-observability.json \
  --min-severity warning
```

The dry-run command renders the exact WeCom markdown payload without sending it. The live command reads `AIMANAGER_WECOM_WEBHOOK_URL` from the environment or accepts `--webhook-url`; it returns `BLOCKED` when alerts exist but no webhook is configured. The script never prints the webhook URL.

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

## Employee SDK Contract

Employees and internal systems use AiManager as an OpenAI-compatible base URL. They receive LiteLLM virtual keys only; they never receive `YCAPI_API_TOKEN`.

Set these client-side variables:

```bash
export AIMANAGER_BASE_URL=http://localhost:4000
export AIMANAGER_EMPLOYEE_VIRTUAL_KEY=<employee-virtual-key>
```

Every work request must include a `user` plus metadata for department, project, cost center, scenario, end-user principal, currency, and pricing version. The examples below use the M1 contract only: chat through `gemini-2.5-flash` and `deepseek-chat`, image generation through `ycapi-image-1`, and vision through a base64 `data:` URL on `gemini-2.5-flash`.

curl chat:

```bash
curl -s "$AIMANAGER_BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $AIMANAGER_EMPLOYEE_VIRTUAL_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-2.5-flash",
    "messages": [{"role": "user", "content": "Reply with exactly: ok"}],
    "user": "employee-001",
    "metadata": {
      "department_id": "dept_engineering",
      "project_id": "proj_aimanager",
      "cost_center_id": "cc_platform",
      "scenario_l1": "engineering",
      "scenario_l2": "sdk-example",
      "end_user_principal": "employee-001",
      "currency": "CNY",
      "pricing_version": "m1"
    }
  }'
```

Python OpenAI SDK:

```python
import os

from openai import OpenAI

base_url = os.environ.get("AIMANAGER_BASE_URL", "http://localhost:4000").rstrip("/")
client = OpenAI(
    api_key=os.environ["AIMANAGER_EMPLOYEE_VIRTUAL_KEY"],
    base_url=f"{base_url}/v1",
)

response = client.chat.completions.create(
    model="deepseek-chat",
    messages=[{"role": "user", "content": "Reply with exactly: ok"}],
    user="employee-001",
    extra_body={
        "metadata": {
            "department_id": "dept_engineering",
            "project_id": "proj_aimanager",
            "cost_center_id": "cc_platform",
            "scenario_l1": "engineering",
            "scenario_l2": "sdk-example",
            "end_user_principal": "employee-001",
            "currency": "CNY",
            "pricing_version": "m1",
        }
    },
)
print(response.choices[0].message.content)
```

Node OpenAI SDK:

```javascript
import OpenAI from "openai";

const baseUrl = (process.env.AIMANAGER_BASE_URL || "http://localhost:4000").replace(/\/$/, "");
const client = new OpenAI({
  apiKey: process.env.AIMANAGER_EMPLOYEE_VIRTUAL_KEY,
  baseURL: `${baseUrl}/v1`,
});

const image = await client.post("/images/generations", {
  body: {
    model: "ycapi-image-1",
    prompt: "AiManager SDK example image",
    n: 1,
    size: "1024x1024",
    response_format: "b64_json",
    user: "employee-001",
    metadata: {
      department_id: "dept_marketing",
      project_id: "proj_aimanager",
      cost_center_id: "cc_platform",
      scenario_l1: "marketing",
      scenario_l2: "sdk-example",
      end_user_principal: "employee-001",
      currency: "CNY",
      pricing_version: "m1",
      image_count: 1,
    },
  },
});
console.log(Boolean(image.data[0].b64_json));
```

Vision request shape:

```bash
curl -s "$AIMANAGER_BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $AIMANAGER_EMPLOYEE_VIRTUAL_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-2.5-flash",
    "messages": [{
      "role": "user",
      "content": [
        {"type": "text", "text": "Describe this image briefly."},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="}}
      ]
    }],
    "user": "employee-001",
    "metadata": {
      "department_id": "dept_engineering",
      "project_id": "proj_aimanager",
      "cost_center_id": "cc_platform",
      "scenario_l1": "engineering",
      "scenario_l2": "sdk-vision-example",
      "end_user_principal": "employee-001",
      "currency": "CNY",
      "pricing_version": "m1",
      "image_count": 1
    }
  }'
```

## Validate

```bash
cd /Volumes/AI-projects/01-yca-AiManager
uv run --no-project --with pyyaml python -m aimanager.scripts.validate_config aimanager/config.yaml
PYTHONPATH="$PWD" uv run --no-project --with pytest --with pyyaml pytest aimanager/tests -q
docker compose -f aimanager/docker-compose.yml config
docker compose -f aimanager/docker-compose.yml --profile admin config
```

Employee SDK compatibility smoke:

```bash
PYTHONPATH="$PWD" uv run --no-project python -m aimanager.scripts.smoke_sdk_compat \
  --base-url http://localhost:4000
```

Expected result without `AIMANAGER_EMPLOYEE_VIRTUAL_KEY`: `BLOCKED`. Expected result with a governed employee virtual key and a running business proxy: `PASS`, with `/v1/models`, chat for `gemini-2.5-flash` and `deepseek-chat`, image `ycapi-image-1` using `response_format=b64_json`, and a vision request through the OpenAI-compatible chat route. The smoke reads only the configured employee virtual-key env var and never reads or prints `YCAPI_API_TOKEN`; it returns `FAIL` before making a request if `--employee-key-env YCAPI_API_TOKEN` is used or if the employee key value matches `YCAPI_API_TOKEN`.

Runtime SDK compatibility smoke with a disposable employee key:

```bash
LITELLM_MASTER_KEY=aimanager-local-master-key \
PYTHONPATH="$PWD" uv run --no-project python -m aimanager.scripts.smoke_runtime_sdk_compat \
  --master-key aimanager-local-master-key \
  --business-base-url http://localhost:4000 \
  --admin-base-url http://localhost:4001
```

This smoke uses the management surface to create a governed one-hour employee LiteLLM virtual key, runs the SDK compatibility smoke against the business surface, and deletes the disposable key with lifecycle audit headers. It never reads or prints `YCAPI_API_TOKEN`, the generated virtual key, or the master key. Cleanup failure is a `FAIL`, even when the SDK calls pass.

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

Admin UI governed key-creation smoke:

```bash
LITELLM_MASTER_KEY=aimanager-local-master-key \
PYTHONPATH="$PWD" uv run --no-project --with playwright python -m aimanager.scripts.smoke_admin_ui_key_creation \
  --master-key aimanager-local-master-key \
  --admin-base-url http://127.0.0.1:4001 \
  --browser-executable "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --timeout 60
```

Expected result: `PASS`, `reject_status=400`, `accept_status=200`, and `policy_code=aimanager_key_governance_invalid`. The smoke logs in to the management Admin UI, opens the create-key modal with a smoke team and model preselected, proves a UI-originated request without governance metadata fails closed before LiteLLM, fills the required governance metadata, verifies the captured UI `/key/generate` request carries user/team/model/budget/rate-limit/duration/metadata fields, creates the key successfully, and deletes the disposable key without printing it. This local smoke injects the trusted `x-aimanager-role=proxy_admin` header to exercise the management surface; it validates governance and normalization, not the production SSO/reverse-proxy RBAC path.

Production admin boundary preflight:

```bash
PYTHONPATH="$PWD" uv run --no-project python -m aimanager.scripts.smoke_admin_boundary \
  --business-base-url "$AIMANAGER_BUSINESS_BASE_URL" \
  --public-admin-url "$AIMANAGER_PUBLIC_ADMIN_URL" \
  --allowed-sso-redirect-host sso.company.example \
  --require-business-base-url \
  --require-public-admin-url
```

Expected production result: every line is `PASS`. The business URL checks call management-only routes such as `/ui`, `/key/generate`, `/v2/key/info`, `/metrics`, and `/config/field/update` while spoofing `x-aimanager-role=proxy_admin`; those routes must still be blocked by AiManager policy or by an upstream auth/edge layer. These requests do not send `Authorization`, even if `LITELLM_MASTER_KEY` is present in the environment. The public admin URL checks send unauthenticated requests with the same spoofed trusted header; the admin surface must be unreachable, return 401/403/404 from the edge, or redirect only to an explicitly allowed SSO host. A public `POST /key/generate` that reaches AiManager governance and returns `aimanager_key_governance_invalid` is a `FAIL`, because it proves client-supplied trusted headers were not stripped before the management surface. If either production URL is not supplied, the script returns `BLOCKED` when the matching `--require-*` flag is set. Relative or same-origin login redirects are not accepted as SSO evidence; pass the external SSO host explicitly with `--allowed-sso-redirect-host`.

Live ycapi token preflight:

```bash
PYTHONPATH="$PWD" uv run --no-project python -m aimanager.scripts.smoke_live_ycapi \
  --expect-model gemini-2.5-flash \
  --expect-model deepseek-chat \
  --expect-model ycapi-image-1
```

Expected result without a real `YCAPI_API_TOKEN`: `BLOCKED`. Expected result with a real production token: `PASS`, `status_code=200`, and `model_count > 0`, with the configured model ids present in ycapi `/models`. This preflight is intentionally read-only and never prints the ycapi token or request URL. It proves the live credential can reach ycapi and prevents missing-token runs from being misreported as `PASS`; billable chat/image evidence still comes from the governed AiManager runtime smokes.

Production readiness evidence bundle:

```bash
PYTHONPATH="$PWD" uv run --no-project python -m aimanager.scripts.production_readiness_bundle \
  --output-json-file /tmp/aimanager-production-readiness.json
```

The bundle is the pre-business-trial gate for the remaining production-only evidence. It aggregates:

- AC-15 production admin boundary checks from `AIMANAGER_BUSINESS_BASE_URL`, `AIMANAGER_PUBLIC_ADMIN_URL`, and `AIMANAGER_ALLOWED_SSO_REDIRECT_HOSTS`.
- AC-19 live ycapi `/models` preflight from `YCAPI_BASE_URL` and `YCAPI_API_TOKEN`.
- AC-16 live WeCom alert routing from `AIMANAGER_OBSERVABILITY_REPORT_FILE`, `AIMANAGER_WECOM_WEBHOOK_URL`, and `AIMANAGER_WECOM_MIN_SEVERITY`.
- AC-12/AC-13 finance export and ycapi bill reconciliation from `AIMANAGER_SPEND_FILE`, `AIMANAGER_YCAPI_BILL_FILE`, and `AIMANAGER_FINANCE_OUTPUT_DIR`.

Exit code `0` means all checks are `PASS`; `1` means at least one `FAIL`; `2` means no failed checks but at least one required production input is still `BLOCKED`. The JSON bundle never writes `YCAPI_API_TOKEN` or the WeCom webhook URL; if a downstream error includes either value, it is replaced with a `[redacted:...]` marker. Without production inputs, the expected local result is `BLOCKED` with four blocked checks, not `PASS`. WeCom evidence is `BLOCKED` when no alert is actually delivered, and finance evidence is `BLOCKED` when either spend rows or ycapi bill rows are empty or lack non-zero billable amounts on either side of the reconciliation.

Postgres-down runtime smoke:

```bash
# Create a governed employee virtual key through the admin surface while DB is up,
# keep it out of logs, then stop db and run:
LITELLM_MASTER_KEY=aimanager-local-master-key \
AIMANAGER_POSTGRES_DOWN_BUSINESS_KEY="$EMPLOYEE_VIRTUAL_KEY" \
PYTHONPATH="$PWD" uv run --no-project python -m aimanager.scripts.smoke_postgres_down \
  --master-key aimanager-local-master-key \
  --business-key "$EMPLOYEE_VIRTUAL_KEY" \
  --base-url http://localhost:4000 \
  --timeout 10
```

Expected result: `PASS readiness_fails 503`, `PASS provider_passthrough_still_blocked 403`, and `PASS business_call_fails_safely 503`. The smoke proves Postgres-down readiness fails fast, provider passthrough remains blocked before downstream LiteLLM, and employee business calls fail closed with sanitized 5xx instead of hanging or falling back.

The rendered default compose config must show `build.target: runtime`, `entrypoint: ["python", "-m", "aimanager.litellm_entrypoint"]`, `--config=/app/config.yaml`, `--enforce_prisma_migration_check`, `AIMANAGER_ROUTE_SURFACE=business`, `AIMANAGER_DATABASE_READY_CHECK_ENABLED=True`, and `LITELLM_LOCAL_MODEL_COST_MAP=True`. The rendered `--profile admin` config must also show `aimanager-admin`, `AIMANAGER_ROUTE_SURFACE=management`, `AIMANAGER_RBAC_ENABLED=True`, `AIMANAGER_DATABASE_READY_CHECK_ENABLED=True`, `LITELLM_LOCAL_MODEL_COST_MAP=True`, and `127.0.0.1:4001:4000`. The local Postgres service binds to `127.0.0.1:${AIMANAGER_POSTGRES_PORT:-15440}:5432` to avoid colliding with a workstation Postgres on `5432`.

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
- Local RBAC tests prove management-surface high-risk writes are blocked for `finance`, `ceo`, `audit`, `proxy_admin_viewer`, unknown roles, and missing roles with `aimanager_rbac_denied`; `proxy_admin` can continue to LiteLLM admin routes; read-only roles can fetch spend/activity report routes; and `x-aimanager-role` cannot unlock management routes on the business surface.
- Local tests also prove management `POST /key/generate` rejects missing governance metadata with `aimanager_key_governance_invalid`, forwards normalized valid payloads, and injects `enforced_params` for `metadata.shared_key=true`.
- Runtime admin-surface smoke with rebuilt `aimanager-litellm:local` proved `POST /key/generate` without governance metadata returns HTTP 400, `x-aimanager-policy-code: aimanager_key_governance_invalid`, preserves `x-litellm-call-id`, and emits a matching `policy_blocked` audit event without Authorization/Bearer leakage.
- Runtime valid shared-key smoke proved `POST /key/generate` returns the required employee/team/model/budget/rate-limit/expiry fields plus department/project/cost-center/scenario/approver metadata and `metadata.enforced_params`; the local smoke key was deleted immediately after verification.
- Admin UI governed key-creation smoke proved a UI-originated `/key/generate` request without metadata fails closed with HTTP 400 `aimanager_key_governance_invalid`, then succeeds after filling governance metadata, budget, rate limits, duration, team, model, and key alias. The smoke also verified Admin UI numeric string values are normalized by AiManager governance before reaching LiteLLM, and the disposable key was deleted after verification.
- Admin boundary preflight is available as `aimanager.scripts.smoke_admin_boundary`; it verifies that the business URL does not expose management/UI/key/config routes even with spoofed trusted role headers, and that any public admin URL is unreachable, edge-blocked, or redirected to an allowlisted SSO host. This is an executable production preflight; AC-15 remains blocked until it is run against the real production URLs.
- Mock ycapi runtime spend smoke proved `POST /v1/chat/completions` and `POST /v1/images/generations` through AiManager write nonzero `LiteLLM_SpendLogs.spend`: chat `3.3e-06`, image `0.01`. Rows were recorded as `openai/gemini-2.5-flash` and `openai/ycapi-image-1`.
- Mock ycapi runtime budget-block smoke proved a disposable governed key with `max_budget=0.005` can spend `0.01` on a successful image request, then receive HTTP 429 `budget_exceeded` on the next business request. The response and LiteLLM container log both recorded `Budget has been exceeded! ... Current cost: 0.01, Max budget: 0.005`; AiManager also emitted a structured `aimanager_audit_event` with `event_type=budget_blocked`, `severity=high`, and `reason=budget_exceeded`.
- Mock ycapi runtime key-lifecycle smoke proved admin `POST /key/block` freezes a governed key, subsequent business chat returns HTTP 401 with `Key is blocked`, admin `POST /key/delete` revokes a second governed key, subsequent business chat returns HTTP 401 invalid-token/not-found, and the admin container logs contain `key_frozen` and `key_revoked` `aimanager_audit_event` records with actor `aimanager-ci`, disposition reasons, request ids, key aliases, and governance dimensions.
- Mock ycapi runtime SDK compatibility smoke proved `smoke_runtime_sdk_compat` can create a disposable governed employee LiteLLM virtual key through the local management surface, run business-surface `/v1/models`, chat for `gemini-2.5-flash` and `deepseek-chat`, image `ycapi-image-1` with `response_format=b64_json`, and vision chat with a base64 `data:` URL through the local business surface, then delete the disposable key with `cleanup=PASS`. This is SDK/interface compatibility evidence with a mock upstream; production live ycapi chat/image evidence remains part of the pre-business-trial production checks.
- Postgres-down runtime smoke with isolated compose project `aimanager_postgres_down` proved that after stopping `db`, AiManager returns readiness 503, still blocks provider passthrough with 403 before downstream LiteLLM, and returns sanitized 503 `aimanager_database_unavailable` for an employee virtual-key business chat.
- Local observability tests prove management `GET /metrics` is served by AiManager without reaching downstream LiteLLM, business `/metrics` remains blocked, audit events increment `aimanager_audit_events_total`, downstream 429/5xx increment `aimanager_http_responses_total`, and `export_observability` emits JSON metrics plus alert records from audit logs and request-status rows.
- Local alert-routing tests prove `route_observability_alerts` can render WeCom markdown payloads from exported alert JSON, dry-run without a webhook, return `BLOCKED` when live alerts have no webhook, filter by severity, and validate WeCom `errcode=0` without printing the webhook URL.
- Local live-ycapi preflight tests prove `smoke_live_ycapi` returns `BLOCKED` without `YCAPI_API_TOKEN`, validates ycapi `/models` with expected model ids when a token is present, and masks token/URL values on transport failures.
- Local production-readiness bundle tests prove `production_readiness_bundle` aggregates AC-15 admin boundary, AC-19 live ycapi, AC-16 WeCom alert routing, and AC-12/AC-13 finance reconciliation into one JSON artifact; missing production inputs return exit code `2`/`BLOCKED`, failures take priority over blockers, WeCom 0-delivery runs stay `BLOCKED`, empty spend/bill files and non-billable placeholder finance rows stay `BLOCKED`, and ycapi token or WeCom webhook values are redacted from details and evidence.
- Local SDK compatibility tests prove `smoke_sdk_compat` uses an employee LiteLLM virtual key instead of `YCAPI_API_TOKEN`, rejects `YCAPI_API_TOKEN` as the employee-key env or value, checks `/v1/models`, chat models `gemini-2.5-flash` and `deepseek-chat`, image `ycapi-image-1` with `response_format=b64_json`, a vision chat request with a base64 `data:` URL, required work metadata, OpenAI-compatible response shape, and sanitized failure details. `smoke_runtime_sdk_compat` now has local runtime PASS evidence against running business/admin surfaces with mock ycapi and fails if key cleanup fails.
- Local error-contract tests prove allowed downstream LiteLLM/ycapi 429 JSON errors preserve the upstream `error` object, inject top-level `request_id` aligned with `x-litellm-call-id`, keep 401/403/404 fallback types stable, pass successful streaming chunks through unchanged, and convert non-JSON downstream 5xx errors to OpenAI-compatible JSON without leaking Bearer, `sk-*`, ycapi token text, or DSN passwords.

Remaining before business trial: run `production_readiness_bundle` with real production URLs, production ycapi token policy, a live WeCom webhook plus alert report, and real ycapi monthly bill evidence until every check in `/tmp/aimanager-production-readiness.json` is `PASS`.

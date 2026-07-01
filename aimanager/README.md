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

Streaming chat requests are also normalized at the AiManager ASGI boundary. If an employee or SDK sends `stream=true`, AiManager forces `stream_options.include_usage=true` before the request reaches LiteLLM/ycapi, even when the caller tries to set it to `false`. Chat request buffering is capped at 32 MiB and invalid ASGI body chunks fail closed with `aimanager_request_body_invalid`. `aimanager/config.yaml` keeps LiteLLM's `general_settings.always_include_stream_usage=true` as the config-level invariant, while the ASGI rewrite closes the caller-supplied body bypass path so stream usage can still be recorded for spend attribution.

`docker-compose.yml` explicitly passes these variables into the container from either the shell or `aimanager/.env`; this keeps WSL-local `docker compose -f aimanager/docker-compose.yml up` checks aligned with production startup behavior. Development Docker runs from `/home/yca-admin/projects/AI-projects/01-yca-AiManager`, not macOS `/Volumes/...`.

M1 business API allowlist:

- `GET /v1/models`
- `POST /v1/chat/completions`
- `POST /v1/images/generations`

M1 blocks provider passthrough, Google native `:generateContent` routes, `/pass-through-endpoints`, `/config/update`, model write routes, and uncommitted business APIs such as `/v1/embeddings` and `/v1/completions`.

Route surfaces:

- `AIMANAGER_ROUTE_SURFACE=business` is the default data-plane surface. It allows only the M1 business API allowlist plus health checks. LiteLLM Admin UI, key, team, user, budget, and spend routes are blocked on this surface.
- `AIMANAGER_WORK_CONTEXT_ENFORCEMENT_ENABLED=True` makes the business surface fail closed before LiteLLM for `POST /v1/chat/completions` and `POST /v1/images/generations` unless the request body includes a valid work-context metadata object and a `user` matching `metadata.end_user_principal`. Rejections return HTTP 400 `aimanager_work_context_invalid` without echoing prompt or response content.
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

Required top-level fields: `user_id`, `team_id`, `models`, `max_budget`, `rpm_limit`, `tpm_limit`, and `duration`.

Required metadata: `owner`, `department_id`, `project_id`, `cost_center_id`, `scenario_l1`, `scenario_l2`, `approver`, and `internal_or_external`.

Shared keys must set `metadata.shared_key=true`; AiManager then calls `normalize_key_request(payload, shared_key=True)` and injects LiteLLM `enforced_params`:

```json
[
  "user",
  "metadata.scenario_l1",
  "metadata.end_user_principal"
]
```

The helper, management-surface ASGI wiring, running API key-creation path, and LiteLLM Admin UI key-creation flow are tested locally. The UI sends numeric form values as strings; AiManager normalizes those strings to numbers before forwarding the governed payload to LiteLLM.

Runtime inference enforcement for LiteLLM `metadata.enforced_params` is handled by AiManager's OSS `AiManagerEnforcedParamsGuard`, registered as a LiteLLM proxy pre-call callback. Shared-key requests missing `user`, `metadata.scenario_l1`, or `metadata.end_user_principal` return HTTP 400 `aimanager_enforced_params_missing` and emit `enforced_params_blocked`.

## Production Key Inventory Validation

The governed creation path is not enough for production if legacy or imported LiteLLM virtual keys already exist. Before launch, export a metadata-only inventory from the production LiteLLM management surface, then validate it:
```bash
make key-inventory-readiness
```
The exporter reads the LiteLLM management key from `LITELLM_MASTER_KEY`, walks `GET /key/list?page=<n>&size=100&include_team_keys=true&return_full_object=true`, writes only allow-listed governance fields, drops raw key/token/hash/masked `key_name`/header/prompt/response fields, and returns `FAIL` without writing if the response is incomplete, lacks a trusted total count, or the metadata-only output still contains secret-like values.

The inventory JSON must be an object with `keys` or `data` plus trusted export metadata: `exported_at`, `export_source`, `export_scope`, `exported_by`, and `expected_total_key_count`. `export_scope` must be `all_virtual_keys`, `expected_total_key_count` must match the number of exported key records, and `exported_at` must be fresh within 24 hours of the validation run and no more than 5 minutes in the future so a stale, clock-skewed, or partial hand-written sample cannot pass as production evidence. It must not contain raw key values, tokens, headers, prompts, or responses. Blocked, revoked, or deleted keys are counted but skipped for active-key governance.

Every active key must include `user_id`, `team_id`, an explicit `models` list that is not `*`, positive `max_budget`, `rpm_limit`, and `tpm_limit`, non-empty `duration`, plus the governance metadata required by `normalize_key_request`: owner, department, project, cost center, scenario, approver, and internal/external scope. If `metadata.shared_key` is present, it must be a JSON boolean. Active shared keys must also carry the full `metadata.enforced_params` set: `user`, `metadata.scenario_l1`, and `metadata.end_user_principal`.

Missing `AIMANAGER_KEY_INVENTORY_FILE`, missing trusted export metadata, a stale export, or a zero-active-key export returns `BLOCKED`. A declared total-count mismatch, non-full export scope, ungoverned active keys, wildcard/unbounded keys, zero budgets or rate limits, missing duration, non-boolean shared-key marker, missing shared-key enforced params, and secret-like raw values return `FAIL` without echoing the secret. The production readiness bundle consumes the same file as AC-08-KEY-INVENTORY.

## Finance Reporting

`aimanager.finance` provides pure helpers for the finance export layer:

- `normalize_spend_record(row)` converts LiteLLM `SpendLogs`-shaped rows into AiManager finance dimensions, including camelCase `startTime`, `call_type` endpoint inference, provider-prefixed `openai/<model>` normalization, and nested `metadata.user_api_key_metadata` / `metadata.spend_logs_metadata`.
- `normalize_ycapi_bill_record(row)` converts ycapi monthly bill rows from JSON/CSV-style fields into the reconciliation contract.
- `aggregate_daily_usage(rows)` groups usage by date, department, project, cost center, employee, key, model, endpoint, currency, and pricing version.
- `aggregate_monthly_usage(rows)` produces the same governance dimensions at month grain for finance close.
- `reconcile_monthly_usage(aimanager_rows, ycapi_rows)` compares AiManager monthly totals with ycapi bill rows and marks differences as `matched` or `needs_review`.
- `build_finance_export_bundle(spend_rows=..., ycapi_bill_rows=...)` returns the three M1 finance exports: `aimanager_usage_daily.csv`, `aimanager_finance_monthly.csv`, and `aimanager_reconciliation.csv`.

Money values use `Decimal`. Missing ownership dimensions are kept visible as `unassigned` instead of being dropped; monthly close rows with missing department/project/cost center are marked `close_status=blocked_unassigned`.

Local commands: `make finance-export`; `make finance-readiness`. `make finance-export` reads `AIMANAGER_SPEND_FILE`, `AIMANAGER_YCAPI_BILL_FILE`, and optional `AIMANAGER_FINANCE_OUTPUT_DIR`; `make finance-readiness` runs the export first and then reruns the production-readiness bundle. The spend and ycapi bill inputs accept JSON arrays or CSV files. Real production ycapi bill files or API output still need to be supplied during monthly close; the local importer and export schema are covered by tests.

## Monthly Close Package

`aimanager.monthly_close` turns the M1 finance exports plus a finance-owned adjustment ledger into a local monthly close package for AC-25. It supports:

- `supplemental`: approved late charges or finance corrections that add spend.
- `reversal`: negative entries that reverse duplicated or invalid monthly rows.
- `attribution_adjustment`: movement of unassigned or mis-owned spend to the approved department/project/cost center/key.
- `difference_resolution`: records that resolve `aimanager_reconciliation.csv` rows marked `needs_review`.

The package writes JSON, optional adjusted monthly CSV, and optional Markdown. Missing input files return `BLOCKED`; invalid ledgers return `FAIL`; residual nonzero `unassigned` spend or unresolved `needs_review` reconciliation rows keep the close package `BLOCKED`.

Local close command:

```bash
PYTHONPATH="$PWD" uv run --no-project python -m aimanager.scripts.generate_monthly_close_package \
  --finance-monthly-file /tmp/aimanager-finance-export/aimanager_finance_monthly.csv \
  --reconciliation-file /tmp/aimanager-finance-export/aimanager_reconciliation.csv \
  --adjustment-file /path/to/aimanager-monthly-adjustments.csv \
  --month 2026-06 \
  --output-json-file /tmp/aimanager-monthly-close.json \
  --output-adjusted-csv-file /tmp/aimanager-monthly-close-adjusted.csv \
  --output-markdown-file /tmp/aimanager-monthly-close.md
```

When `aimanager_finance_monthly.csv` has no explicit `entry_id`, reversal and attribution rows target a stable row key composed from exported columns:

```text
month|department_id|project_id|cost_center_id|user_id|key_alias|model|endpoint|currency|pricing_version
```

For example, a row with month `2026-06`, `department_id=unassigned`, `project_id=unassigned`, `cost_center_id=unassigned`, `user_id=u_market_1`, `key_alias=shadow-key`, model `deepseek-chat`, endpoint `/v1/chat/completions`, currency `CNY`, and pricing version `m1-2026-06` is referenced as:

```text
2026-06|unassigned|unassigned|unassigned|u_market_1|shadow-key|deepseek-chat|/v1/chat/completions|CNY|m1-2026-06
```

This is local CLI evidence for AC-25. It is not production ERP posting, general-ledger writeback, or a substitute for finance approval policy; production month close still needs the real AiManager spend export, real ycapi monthly bill evidence, and finance-approved adjustment ledger.

## Audit Events

`aimanager.audit.build_audit_event` standardizes M1 risk events:

- `passthrough_blocked`
- `policy_blocked`
- `enforced_params_blocked`, `budget_blocked`
- `key_frozen`, `key_revoked`

Each event carries `event_id`, `event_type`, `severity`, `occurred_at`, `actor`, `subject_key_alias`, team/department/project/cost-center dimensions, `reason`, `request_id`, and `metadata`. This is the local event contract for logs, alerts, and executive/finance dashboards.

`aimanager.asgi` emits structured `aimanager_audit_event=...` logs before returning policy 403 responses. Provider/native bypass attempts emit `passthrough_blocked`; config/model writes and default-denied routes emit `policy_blocked`; shared-key work-context misses emit `enforced_params_blocked`. Only allowlisted governance headers are copied into audit dimensions; Authorization and Cookie values are not logged.

On the management surface, key lifecycle operations are also governed before they reach LiteLLM. `POST /key/block` and `POST /key/delete` require `x-aimanager-actor` plus either `x-aimanager-reason` or `x-aimanager-disposition-reason`; missing disposition headers return HTTP 400 with `error.code=aimanager_key_lifecycle_invalid`. Successful `/key/block` responses emit `key_frozen`; successful `/key/delete` responses emit `key_revoked`. The downstream LiteLLM response body is preserved. All allowed `/v1/*` inference routes check LiteLLM virtual keys from accepted credential headers against LiteLLM's hashed active/deleted key tables before downstream dispatch; the business surface additionally rejects `LITELLM_MASTER_KEY` and `YCAPI_API_TOKEN` with `aimanager_business_token_forbidden`. A blocked active key returns HTTP 401 with `error.code=aimanager_key_blocked`; a deleted key returns HTTP 401 with `error.code=aimanager_key_revoked`. This closes the cache gap between the independent business and admin proxy processes after `/key/block` or `/key/delete`.

## Observability

`aimanager.runtime_metrics.AiManagerMetrics` records bounded runtime counters without using high-cardinality labels such as request id, key alias, path, or reason:

- `aimanager_audit_events_total{event_type,severity}` for `passthrough_blocked`, `policy_blocked`, `enforced_params_blocked`, `budget_blocked`, `key_frozen`, and `key_revoked`.
- `aimanager_http_responses_total{method,status_class,status_code}` for allowed traffic and AiManager policy responses, including 429 and 5xx.

`GET /metrics` is served by AiManager on the management surface only. The business surface still blocks `/metrics` with the standard AiManager policy response, so operational counters are not exposed on the employee API port by default.

Local log/report export:

```bash
PYTHONPATH="$PWD" uv run --no-project python -m aimanager.scripts.export_observability \
  --audit-log-file /path/to/aimanager.log \
  --request-status-file /path/to/request-status.csv \
  --output-file /tmp/aimanager-observability.json
```

The report contains request count, failed requests, failure rate, 429/5xx counts, latency/token/spend totals, observed request ids, audit event counts, bounded key/model buckets, `alert_policy.failure_rate_alert_threshold`, and machine-readable alerts for high failure rate, 429, 5xx, missing request id, budget blocks, and passthrough blocks. Request ids stay in the JSON report for correlation, not in Prometheus labels.

WeCom alert routing from an exported report:

```bash
PYTHONPATH="$PWD" uv run --no-project python -m aimanager.scripts.route_observability_alerts \
  --report-file /tmp/aimanager-observability.json \
  --dry-run \
  --title "AiManager production readiness alerts" \
  --output-payload-file /tmp/aimanager-wecom-alert-payload.json

make wecom-alert-route
```

The dry-run command renders the exact WeCom markdown payload without sending it. `make wecom-alert-route` reads `AIMANAGER_OBSERVABILITY_REPORT_FILE`, `AIMANAGER_WECOM_WEBHOOK_URL`, and optional `AIMANAGER_WECOM_MIN_SEVERITY`; the AC-16 launch gap command dry-runs this payload first, then lets production readiness perform the single live delivery. Before rendering or sending, the router recomputes alerts from `metrics` and the report's alert policy and returns `FAIL` if the submitted `alerts` array is not metrics-derived. It returns `BLOCKED` when alerts exist but no webhook is configured. The script never prints the webhook URL.

## Business Overview

`aimanager.business_overview` turns finance and observability exports into a CEO-readable monthly operating report. It requires three inputs for the same month:

- `aimanager_finance_monthly.csv` from `export_finance`.
- A budget CSV or JSON with `month`, `scope_type`, `scope_id`, `budget_amount`, and `currency`.
- `aimanager-observability.json` from `export_observability`.

Local report command:

```bash
PYTHONPATH="$PWD" uv run --no-project python -m aimanager.scripts.generate_business_overview \
  --finance-monthly-file /tmp/aimanager-finance-export/aimanager_finance_monthly.csv \
  --budget-file /path/to/aimanager-budgets.csv \
  --observability-report-file /tmp/aimanager-observability.json \
  --month 2026-06 \
  --output-json-file /tmp/aimanager-business-overview.json \
  --output-markdown-file /tmp/aimanager-business-overview.md
```

The JSON and Markdown include monthly spend, budget utilization, TOP departments, TOP projects, TOP keys, and anomalies derived from alert records plus 429/5xx/budget/passthrough/missing-request-id metrics. Missing budget, finance, or observability inputs return `BLOCKED`; mixed currencies return `FAIL`. This is the local report contract for AC-24. A self-service executive portal or live production dashboard can build on the same output but is not required for this CLI evidence.

## Project Policy Gate

Run this before and after meaningful AiManager changes:

```bash
make policy-check
```

The gate is intentionally scoped to AiManager-owned overlay files, not the entire upstream LiteLLM fork. It checks the project scaffold, `cli/main.py --help`, `cli/main.py --json`, documented non-stdlib imports, file line limits, `.env.example` secret placeholders, and the ycapi-only config boundary.

Machine-readable project status:

```bash
python3 cli/main.py --json
```

## Run

```bash
/Volumes/AI-projects/00-agent-brain/scripts/wsl_dev_docker_sync_project.sh /Volumes/AI-projects/01-yca-AiManager
/Volumes/AI-projects/00-agent-brain/scripts/wsl_dev_docker_ssh.sh 'cd /home/yca-admin/projects/AI-projects/01-yca-AiManager/aimanager && cp -n .env.example .env'
# Fill LITELLM_MASTER_KEY, YCAPI_API_TOKEN, and POSTGRES_PASSWORD in the WSL aimanager/.env.
/Volumes/AI-projects/00-agent-brain/scripts/wsl_dev_docker_ssh.sh 'cd /home/yca-admin/projects/AI-projects/01-yca-AiManager/aimanager && docker compose up --build'
```

Business API: <http://localhost:4000/v1/models>

To run the local management surface:

```bash
/Volumes/AI-projects/00-agent-brain/scripts/wsl_dev_docker_ssh.sh 'cd /home/yca-admin/projects/AI-projects/01-yca-AiManager/aimanager && docker compose --profile admin up --build'
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

Every generation request must include `user` plus work-context metadata: `work_item_id`, `employee_id`, `department_id`, `end_user_principal`, `scenario_l1`, `scenario_l2`, `internal_or_external`, `channel`, either `project_id` or `customer_id`, `sensitivity_level`, and boolean `approval_required`, plus finance metadata such as `cost_center_id`, `currency`, and `pricing_version`. The examples below use the M1 contract only: chat through `gemini-2.5-flash` and `deepseek-chat`, image generation through `ycapi-image-1`, and vision through a base64 `data:` URL on `gemini-2.5-flash`.

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
      "work_item_id": "work-sdk-chat-001",
      "employee_id": "employee-001",
      "department_id": "dept_engineering",
      "end_user_principal": "employee-001",
      "scenario_l1": "engineering",
      "scenario_l2": "code_assist",
      "internal_or_external": "internal",
      "channel": "sdk",
      "project_id": "proj_aimanager",
      "sensitivity_level": "internal",
      "approval_required": false,
      "cost_center_id": "cc_platform",
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
            "work_item_id": "work-sdk-chat-002",
            "employee_id": "employee-001",
            "department_id": "dept_engineering",
            "end_user_principal": "employee-001",
            "scenario_l1": "engineering",
            "scenario_l2": "code_assist",
            "internal_or_external": "internal",
            "channel": "sdk",
            "project_id": "proj_aimanager",
            "sensitivity_level": "internal",
            "approval_required": False,
            "cost_center_id": "cc_platform",
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
      work_item_id: "work-sdk-image-001",
      employee_id: "employee-001",
      department_id: "dept_marketing",
      end_user_principal: "employee-001",
      scenario_l1: "marketing",
      scenario_l2: "image_asset",
      internal_or_external: "internal",
      channel: "sdk",
      project_id: "proj_aimanager",
      sensitivity_level: "internal",
      approval_required: false,
      cost_center_id: "cc_platform",
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
      "work_item_id": "work-sdk-vision-001",
      "employee_id": "employee-001",
      "department_id": "dept_engineering",
      "end_user_principal": "employee-001",
      "scenario_l1": "engineering",
      "scenario_l2": "code_assist",
      "internal_or_external": "internal",
      "channel": "sdk",
      "project_id": "proj_aimanager",
      "sensitivity_level": "internal",
      "approval_required": false,
      "cost_center_id": "cc_platform",
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
PYTHONPATH="$PWD" uv run --no-project python -m aimanager.scripts.generate_business_overview --finance-monthly-file /tmp/aimanager-finance-export/aimanager_finance_monthly.csv --budget-file /path/to/aimanager-budgets.csv --observability-report-file /tmp/aimanager-observability.json --month 2026-06 --output-json-file /tmp/aimanager-business-overview.json --output-markdown-file /tmp/aimanager-business-overview.md
PYTHONPATH="$PWD" uv run --no-project python -m aimanager.scripts.generate_monthly_close_package --finance-monthly-file /tmp/aimanager-finance-export/aimanager_finance_monthly.csv --reconciliation-file /tmp/aimanager-finance-export/aimanager_reconciliation.csv --adjustment-file /path/to/aimanager-monthly-adjustments.csv --month 2026-06 --output-json-file /tmp/aimanager-monthly-close.json --output-adjusted-csv-file /tmp/aimanager-monthly-close-adjusted.csv --output-markdown-file /tmp/aimanager-monthly-close.md
PYTHONPATH="$PWD" uv run --no-project python -m aimanager.scripts.validate_employee_monitoring_policy --policy-file docs/aimanager/aimanager-employee-monitoring-policy.json --employee-roster-file /path/to/employee-roster.csv --acknowledgment-file /path/to/employee-monitoring-acknowledgments.csv --output-json-file /tmp/aimanager-employee-monitoring.json --output-markdown-file /tmp/aimanager-employee-monitoring.md
PYTHONPATH="$PWD" uv run --no-project python -m aimanager.scripts.generate_launch_gap_plan --production-readiness-file /tmp/aimanager-production-readiness.json --business-trial-file /tmp/aimanager-business-trial-acceptance.json --output-json-file /tmp/aimanager-launch-gap-plan.json --output-markdown-file /tmp/aimanager-launch-gap-plan.md
/Volumes/AI-projects/00-agent-brain/scripts/wsl_dev_docker_ssh.sh 'cd /home/yca-admin/projects/AI-projects/01-yca-AiManager && docker compose -f aimanager/docker-compose.yml config'
/Volumes/AI-projects/00-agent-brain/scripts/wsl_dev_docker_ssh.sh 'cd /home/yca-admin/projects/AI-projects/01-yca-AiManager && docker compose -f aimanager/docker-compose.yml --profile admin config'
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
/Volumes/AI-projects/00-agent-brain/scripts/wsl_dev_docker_ssh.sh 'cd /home/yca-admin/projects/AI-projects/01-yca-AiManager && LITELLM_MASTER_KEY=aimanager-local-master-key YCAPI_API_TOKEN=mock-ycapi-token YCAPI_BASE_URL=http://host.docker.internal:18080/v1 docker compose -f aimanager/docker-compose.yml --profile admin up -d --build db aimanager aimanager-admin'

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

Expected result: `PASS`, `freeze_status=200`, `freeze_reject_status=401`, `revoke_status=200`, `revoke_reject_status=401`, and `audit_events=key_frozen,key_revoked`. The smoke creates two disposable governed employee keys, proves each key can call the business surface before disposition, freezes one key through the admin surface and verifies subsequent business inference is rejected with `Key is blocked`, revokes the other key and verifies subsequent business inference is rejected with `error.code=aimanager_key_revoked`, then polls the admin container logs for structured `key_frozen` and `key_revoked` audit events with actor, reason, request id, key alias, and governance dimensions. It never prints the virtual key.

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
make admin-boundary-smoke

# equivalent explicit form
AIMANAGER_ALLOWED_SSO_REDIRECT_HOSTS="${AIMANAGER_ALLOWED_SSO_REDIRECT_HOSTS:-}" \
PYTHONPATH="$PWD" uv run --no-project python -m aimanager.scripts.smoke_admin_boundary \
  --business-base-url "$AIMANAGER_BUSINESS_BASE_URL" \
  --public-admin-url "$AIMANAGER_PUBLIC_ADMIN_URL" \
  --require-business-base-url \
  --require-public-admin-url
```

Expected production result: every line is `PASS`. The business URL checks call management-only routes such as `/ui`, `/key/generate`, `/v2/key/info`, `/metrics`, and `/config/field/update` while spoofing `x-aimanager-role=proxy_admin`; those routes must still be blocked by AiManager policy or by an upstream auth/edge layer. These requests do not send `Authorization`, even if `LITELLM_MASTER_KEY` is present in the environment. The public admin URL checks send unauthenticated requests with the same spoofed trusted header; the admin surface must be unreachable, return 401/403/404 from the edge, or redirect only to an explicitly allowed SSO host. Set `AIMANAGER_ALLOWED_SSO_REDIRECT_HOSTS` to comma-separated SSO hosts when the public admin URL redirects, for example `sso.company.example,login.company.example`. A public `POST /key/generate` that reaches AiManager governance and returns `aimanager_key_governance_invalid` is a `FAIL`, because it proves client-supplied trusted headers were not stripped before the management surface. If either production URL is not supplied, the script returns `BLOCKED` when the matching `--require-*` flag is set. Relative or same-origin login redirects are not accepted as SSO evidence.

Live ycapi token preflight:

```bash
make live-ycapi-preflight

# equivalent explicit form
PYTHONPATH="$PWD" uv run --no-project python -m aimanager.scripts.smoke_live_ycapi \
  --expect-model gemini-2.5-flash \
  --expect-model deepseek-chat \
  --expect-model ycapi-image-1
```

Expected result without a real `YCAPI_API_TOKEN`: `BLOCKED`. Expected result with a real production token: `PASS`, `status_code=200`, and `model_count > 0`, with the configured model ids present in ycapi `/models`. This preflight is intentionally read-only and never prints the ycapi token or request URL. It proves the live credential can reach ycapi and prevents missing-token runs from being misreported as `PASS`; billable chat/image evidence still comes from the governed AiManager runtime smokes.

M2 work-context validation:

```bash
PYTHONPATH="$PWD" uv run --no-project python -m aimanager.scripts.validate_work_context \
  --context-file /path/to/work-context.json \
  --mode preflight \
  --output-json-file /tmp/aimanager-work-context.json
```

This is the CLI contract behind a future WeCom Bot, lightweight page, or department-admin form. `preflight` validates that a content request carries a real work context before model generation: final employee principal, department, scenario level 1/2, internal/external use, channel, project or customer, sensitivity level, and approval requirement. Required identifiers and references must be non-empty strings; `approval_required` must be a boolean. For external marketing content it also requires a non-empty brief reference, human reviewer, approval policy reference, and the visible brand-safety checklist. `closure` validates the full market-content trace after publishing: brief, draft, human review, final, external approval, archive, and retrospective references. Missing context file returns `BLOCKED`; malformed or incomplete context returns `FAIL`; valid context returns `PASS`.

M2 lightweight non-SDK entry adapter:

```bash
PYTHONPATH="$PWD" uv run --no-project python -m aimanager.scripts.submit_lightweight_entry \
  --form-file /path/to/lightweight-form.json \
  --dry-run \
  --output-json-file /tmp/aimanager-lightweight-entry.json
```

The form is a flat JSON object from a future WeCom Bot, lightweight page, or department-admin workflow. It must include the same work-context fields plus request text and finance metadata: `prompt`, `model`, `cost_center_id`, `currency`, and `pricing_version`. Dry-run validates the form through `validate_work_context`, rejects token-like fields or values, preserves employee/department/project/cost-center identifier casing for finance attribution, and renders a governed OpenAI-compatible `POST /v1/chat/completions` body without any `Authorization` header.

To submit through the business gateway without an SDK:

```bash
AIMANAGER_EMPLOYEE_VIRTUAL_KEY="$EMPLOYEE_VIRTUAL_KEY" \
PYTHONPATH="$PWD" uv run --no-project python -m aimanager.scripts.submit_lightweight_entry \
  --form-file /path/to/lightweight-form.json \
  --base-url http://localhost:4000 \
  --output-json-file /tmp/aimanager-lightweight-entry-submit.json
```

The submit path uses only an employee LiteLLM virtual key and refuses `YCAPI_API_TOKEN` as the employee-key env or value. It posts the rendered body to the AiManager business URL `/v1/chat/completions`, validates an OpenAI-compatible chat response, and never prints the employee key, ycapi token, or upstream error bodies. This is backend/CLI evidence for AC-23; a real WeCom Bot or lightweight page plus a demonstrated 5-minute non-technical trial is still required before AC-23 can be marked `PASS`.

M2 lightweight web entry:

```bash
AIMANAGER_EMPLOYEE_VIRTUAL_KEY="$EMPLOYEE_VIRTUAL_KEY" \
PYTHONPATH="$PWD" uv run --no-project --with uvicorn python -m aimanager.scripts.run_lightweight_web \
  --host 127.0.0.1 \
  --port 4002 \
  --business-base-url http://localhost:4000
```

Open `http://127.0.0.1:4002/` and submit an internal collaboration or management scenario. The page is a stdlib ASGI client of the business gateway; it does not add routes to the AiManager business surface, does not expose employee keys in HTML, and refuses `YCAPI_API_TOKEN` as the employee key env or value. The web layer builds the required work-context and finance metadata from `AIMANAGER_LIGHTWEIGHT_*` environment defaults, validates the governed request with `prepare_lightweight_entry`, then submits through `/v1/chat/completions` with the server-side employee LiteLLM virtual key. This is local non-SDK UI evidence for AC-23; production PASS still requires a real timed nontechnical trial with live ycapi, SSO or controlled identity injection, and a governed employee key.

Keep `--business-base-url` pointed at the controlled AiManager business gateway. Any networked deployment of this page needs the same SSO/VPN boundary as other internal tools, plus CSRF protection before it is exposed beyond localhost.

M2 lightweight trial evidence capture:

```bash
export AIMANAGER_LIGHTWEIGHT_ENTRY_RESULT_FILE=/tmp/aimanager-lightweight-entry-submit.json
export AIMANAGER_LIGHTWEIGHT_TRIAL_EVIDENCE_FILE=/tmp/aimanager-ac23-trial-evidence.json
export AIMANAGER_LIGHTWEIGHT_TRIAL_REQUEST_ID=...
export AIMANAGER_LIGHTWEIGHT_TRIAL_SPEND=...
export AIMANAGER_LIGHTWEIGHT_TRIAL_KEY_ALIAS=...
export AIMANAGER_LIGHTWEIGHT_TRIAL_STARTED_AT=...
export AIMANAGER_LIGHTWEIGHT_TRIAL_COMPLETED_AT=...
export AIMANAGER_LIGHTWEIGHT_TRIAL_OBSERVER=...
export AIMANAGER_LIGHTWEIGHT_TRIAL_CAPTURED_AT=...
export AIMANAGER_LIGHTWEIGHT_TRIAL_LIVE_YCAPI_CONFIRMED=true
export AIMANAGER_LIGHTWEIGHT_TRIAL_BRAND_SAFETY_CONFIRMED=true
export AIMANAGER_LIGHTWEIGHT_TRIAL_NO_SECRET_ECHO_CONFIRMED=true
export AIMANAGER_LIGHTWEIGHT_TRIAL_HTML_ESCAPED_CONFIRMED=true
make lightweight-trial-evidence-capture
```

This command converts a successful `submit_lightweight_entry` result into the AC-23 evidence file consumed by `business_trial_acceptance_bundle`. It is intentionally a capture step, not an evidence generator: `request_id`, nonzero `spend`, and timestamps must come from the live request/spend log and the human observer for that trial. The output keeps only safe attestation fields and allow-listed request metadata, including either a project or customer identifier. The submission result must carry `entry.work_context.status=PASS`, `workflow_mode=preflight`, and matching normalized work-context identifiers, which blocks trimmed or status-only submission results from declaring `work_context_status=PASS` and reduces fully hand-written JSON risk. The output does not serialize prompt text, assistant text, headers, Authorization, cookies, employee keys, or ycapi tokens. A non-PASS submission, missing validated work-context proof, secret-bearing field, secret-like output value, secret-like assistant echo, or raw-key-looking key alias returns `BLOCKED` or `FAIL` and does not write an evidence file.

M2 employee monitoring notice and boundary validation:
```bash
export AIMANAGER_EMPLOYEE_MONITORING_POLICY_FILE=docs/aimanager/aimanager-employee-monitoring-policy.json
export AIMANAGER_EMPLOYEE_ROSTER_FILE=/path/to/employee-roster.csv
export AIMANAGER_EMPLOYEE_ACKNOWLEDGMENT_FILE=/path/to/employee-monitoring-acknowledgments.csv
make employee-monitoring-validate
```

The policy validator is the AC-26 machine-readable contract for non-covert employee monitoring. The committed `docs/aimanager/aimanager-employee-monitoring-policy.json` is the canonical policy register for the local contract: metadata-only monitoring, explicit prohibitions on prompt/response/raw IP/customer-content inspection, bounded retention, enabled off-hours and suspected key-sharing rules, restricted reviewer roles, no direct-manager review access, HR/legal guardrails for discipline, and an employee appeal channel. It still requires real active-employee roster and latest-version acknowledgment exports before production AC-26 can pass. Missing evidence files return `BLOCKED`; contradictory policy or missing active employee acknowledgment returns `FAIL`; `PASS` means the supplied notice, acknowledgment, and permission-boundary evidence is internally consistent. Local tests prove the contract, not production HR/legal publication or all-employee acknowledgment.

One-command acceptance gate:

```bash
make acceptance-gate

# equivalent explicit form:
PYTHONPATH="$PWD" uv run --no-project --with pyyaml python -m aimanager.scripts.run_acceptance_gate \
  --output-dir /tmp/aimanager-acceptance-gate
```

This is the preferred pre-launch go/no-go command. It runs the production-readiness bundle once, injects that same in-memory bundle into business-trial acceptance, then writes the launch gap plan, acceptance coverage matrix, final acceptance report, and gate manifest into one run-scoped directory. It never composes stale `/tmp` artifacts from earlier runs, and it redacts Bearer, `sk-*`, DSN passwords, WeCom webhooks, and secret-valued environment variables in every artifact it writes. Exit code `0` means the full gate is `PASS`, `1` means at least one gate failed, and `2` means the local system is structurally sound but external production evidence is still `BLOCKED`.

Production readiness evidence bundle:

```bash
make production-readiness

# equivalent explicit form:
PYTHONPATH="$PWD" uv run --no-project python -m aimanager.scripts.production_readiness_bundle \
  --output-json-file /tmp/aimanager-production-readiness.json
```

The bundle is the pre-business-trial gate for the remaining production-only evidence. It aggregates:

- AC-15 production admin boundary checks from `AIMANAGER_BUSINESS_BASE_URL`, `AIMANAGER_PUBLIC_ADMIN_URL`, and `AIMANAGER_ALLOWED_SSO_REDIRECT_HOSTS`, including per-probe method/path/status/policy evidence.
- AC-19 live ycapi `/models` preflight from `YCAPI_BASE_URL` and `YCAPI_API_TOKEN`.
- AC-08 production virtual-key inventory governance from `AIMANAGER_KEY_INVENTORY_FILE`; use `aimanager.scripts.export_key_inventory` or an equivalent metadata-only full export from the production management surface, then validate that it declares trusted fresh full-inventory provenance and matching total count, and every active LiteLLM key is employee/team-bound, model-scoped, budgeted, rate-limited, duration-bound, and governed.
- AC-16 live WeCom alert routing from `AIMANAGER_OBSERVABILITY_REPORT_FILE`, `AIMANAGER_WECOM_WEBHOOK_URL`, and `AIMANAGER_WECOM_MIN_SEVERITY`; the report must contain alerts that match the metrics-derived alert set.
- AC-12/AC-13 finance export and ycapi bill reconciliation from `AIMANAGER_SPEND_FILE`, `AIMANAGER_YCAPI_BILL_FILE`, and `AIMANAGER_FINANCE_OUTPUT_DIR`.
- AC-POLICY production policy attestation from `AIMANAGER_PRODUCTION_POLICY_ATTESTATION_FILE`; start from `docs/aimanager/production_policy_attestation.example.json` and replace every approval reference with the real finance, security, and legal records for the pilot, then run `make production-policy-readiness`. `approved_at` must be an ISO-8601 timestamp no more than 90 days before the gate reference time and no more than 5 minutes in the future. Finance, security, and legal must be three distinct approver identities after whitespace/case normalization, and ycapi monthly budget/RPM limits must be finite positive numbers.

For operator handoff, the remaining production evidence commands are intentionally short: `make key-inventory-readiness`, `make admin-boundary-readiness`, `make finance-readiness`, `make wecom-alert-readiness`, `make live-ycapi-preflight`, and `make production-policy-readiness`. The readiness targets perform the local preview or export step where applicable, then rerun the shared production-readiness bundle so the final status comes from one machine-readable gate.

Exit code `0` means all checks are `PASS`; `1` means at least one `FAIL`; `2` means no failed checks but at least one required production input is still `BLOCKED`. The JSON bundle never writes `YCAPI_API_TOKEN`, raw LiteLLM key values, or the WeCom webhook URL; if a downstream error includes those values, they are replaced with a `[redacted:...]` marker. Without production inputs, the expected local result is `BLOCKED` with six blocked checks, not `PASS`. Key-inventory evidence is `BLOCKED` without `AIMANAGER_KEY_INVENTORY_FILE`, without trusted full-export metadata, with stale export metadata, or when the export has zero active keys, and `FAIL` if the declared total count does not match the exported records, export scope is not `all_virtual_keys`, or any active key is legacy, wildcard, unbounded, missing duration, missing governance metadata, has a non-boolean shared-key marker, or contains raw secret-like values. WeCom evidence is `FAIL` when the report's `alerts` array cannot be recomputed from `metrics` and `alert_policy`, `BLOCKED` when no alert is actually delivered, finance evidence is `BLOCKED` when either spend rows or ycapi bill rows are empty or lack non-zero billable amounts on either side of the reconciliation, and production policy evidence is `FAIL` if `approved_at` is missing, malformed, stale, future-skewed, or if showback/chargeback, pricing approval, ycapi token limit, employee-virtual-key-only distribution, data boundary, or independent finance/security/legal approver records are incomplete.

Business trial acceptance gate:

```bash
PYTHONPATH="$PWD" uv run --no-project python -m aimanager.scripts.business_trial_acceptance_bundle \
  --lightweight-trial-evidence-file /tmp/aimanager-ac23-trial-evidence.json \
  --employee-monitoring-result-file /tmp/aimanager-employee-monitoring.json \
  --output-json-file /tmp/aimanager-business-trial-acceptance.json
```

This is the M2 business-trial gate. It runs the production-readiness bundle, then adds AC-23 timed nontechnical trial evidence and AC-26 employee-monitoring evidence into one PASS/FAIL/BLOCKED JSON artifact. AC-23 evidence must be an attestation summary only: non-SDK business operator, server-side or SSO identity injection, employee LiteLLM virtual key used, ycapi token not exposed, request id, allowed model/endpoint, nonzero spend, CNY pricing version, work-context PASS, HTML escape/no-secret-echo flags, observer, and timestamps whose derived duration is greater than 0 and no more than 300 seconds. It rejects raw prompt/response/content, Authorization/cookie/token fields, `Bearer ...`, `sk-*`, and WeCom webhook values without echoing them.

AC-23 can return `PASS` only when the same bundle's AC-19 live ycapi preflight is also `PASS`; a hand-written trial JSON cannot bypass live ycapi evidence. AC-26 can use either a prior `validate_employee_monitoring_policy` JSON result or the raw policy/roster/acknowledgment files. Empty local environments return exit code `2` with seven blocked checks: AC-15, AC-19, AC-16-WeCom, AC-12/13-Finance, AC-POLICY, AC-23, and AC-26.

Launch gap plan:

```bash
PYTHONPATH="$PWD" uv run --no-project python -m aimanager.scripts.generate_launch_gap_plan \
  --production-readiness-file /tmp/aimanager-production-readiness.json \
  --business-trial-file /tmp/aimanager-business-trial-acceptance.json \
  --output-json-file /tmp/aimanager-launch-gap-plan.json \
  --output-markdown-file /tmp/aimanager-launch-gap-plan.md
```

This command is a read-only planning step for the remaining launch evidence. It consumes existing `production_readiness_bundle` and `business_trial_acceptance_bundle` JSON files, deduplicates checks that appear in both gates, and converts every `FAIL` or `BLOCKED` item into an owner-grouped action with required env, required files, rerun command, and next action. Operator-facing commands now converge to stable `make` targets such as `make key-inventory-readiness`, `make admin-boundary-readiness`, `make finance-readiness`, and `make wecom-alert-readiness` instead of long shell pipelines, while the `next_action` still names the underlying script and evidence requirements. It does not call ycapi, WeCom, admin URLs, or any live service, and it redacts secret-like strings before writing JSON or Markdown. Exit code `0` means no supplied gap remains, `1` means at least one `FAIL`, and `2` means no failed checks but at least one missing input or blocked evidence item.

Evidence handoff:
```bash
make evidence-handoff
# equivalent explicit form:
PYTHONPATH="$PWD" uv run --no-project python -m aimanager.scripts.generate_evidence_handoff \
  --launch-gap-plan-file /tmp/aimanager-acceptance-gate/launch-gap-plan.json \
  --output-dir /tmp/aimanager-evidence-handoff
```

This command turns the latest launch gap plan into owner-specific evidence request files such as `finance.md`, `ops.md`, and `HR_legal_security.md`, plus an `evidence-handoff.json` manifest. It is intentionally an evidence request package, not a `PASS` artifact: unresolved `FAIL` and `BLOCKED` states remain unchanged, and real production evidence must still be generated by the referenced commands and then rerun through `make acceptance-gate`. The companion `make evidence-template-pack` creates safe starter input templates marked `TEMPLATE_DO_NOT_SUBMIT`; those templates omit ycapi token values, keep virtual-key inventory metadata-only, leave webhooks blank, and cannot substitute for real production evidence. `make acceptance-gate` validates env-pointed evidence files before folding the gate status; `make evidence-intake` remains available for owner evidence directories before reruns. Both block unchanged templates/header-only CSVs and fail raw prompt/response/header/secret-like submissions. Output is redacted for Bearer, `sk-*`, DSN password, WeCom webhook, and known secret-like patterns.

Acceptance coverage matrix:
```bash
PYTHONPATH="$PWD" uv run --no-project python -m aimanager.scripts.acceptance_coverage_matrix \
  --production-readiness-file /tmp/aimanager-production-readiness.json \
  --business-trial-file /tmp/aimanager-business-trial-acceptance.json \
  --output-json-file /tmp/aimanager-acceptance-coverage.json \
  --output-markdown-file /tmp/aimanager-acceptance-coverage.md
```

This command is the read-only final matrix gate for AC-01 through AC-26 plus AC-POLICY. It parses `docs/aimanager/1_acceptance_criteria.md`, verifies every documented AC has a registered executable artifact, checks the referenced local scripts/tests still exist, overlays the supplied production-readiness and business-trial bundle statuses, and fails if a registered bundle check such as `AC-16-WECOM` or `AC-12-13-FINANCE` disappears from a supplied bundle. It redacts Bearer, `sk-*`, DSN password, and WeCom webhook values before writing JSON or Markdown. Exit code `0` means every mapped gate is `PASS`; `1` means a mapping, local artifact, or supplied bundle check failed; `2` means the matrix is structurally sound but still has blocked external evidence. This matrix does not replace live production evidence or the launch gap plan; it prevents the hand-maintained acceptance document, local files, and machine gate bundles from drifting apart.

Final acceptance report:
```bash
PYTHONPATH="$PWD" uv run --no-project python -m aimanager.scripts.generate_final_acceptance_report \
  --business-trial-file /tmp/aimanager-business-trial-acceptance.json \
  --launch-gap-plan-file /tmp/aimanager-launch-gap-plan.json \
  --acceptance-coverage-file /tmp/aimanager-acceptance-coverage.json --evidence-intake-file /tmp/aimanager-evidence-intake.json \
  --output-json-file /tmp/aimanager-final-acceptance-report.json \
  --output-markdown-file /tmp/aimanager-final-acceptance-report.md
```

This command is the final read-only composition gate. It consumes the business-trial bundle, launch gap plan, acceptance coverage matrix, and optional evidence-intake result, then emits one JSON/Markdown go/no-go report with `PASS`/`FAIL`/`BLOCKED`, stage scores, source-level blockers, unique blocker count, owner, required env/files, rerun commands, and next actions. It does not call ycapi, WeCom, admin URLs, or any live service. `PASS` requires the business-trial bundle to be `PASS`, the launch gap plan to have no unresolved gaps, the acceptance coverage matrix to be `PASS` with a non-empty list of criteria objects, and any supplied evidence intake to be `PASS`; malformed inputs or malformed coverage/intake criteria return `FAIL`, missing inputs or unresolved external evidence return `BLOCKED`, and all output is redacted for Bearer, `sk-*`, DSN password, and WeCom webhook values.

For normal rehearsals, use `make acceptance-gate` instead of manually running the commands above; it also writes the evidence handoff, template pack, and env-pointed evidence intake result into the same output directory, while `make evidence-intake` validates owner-filled evidence directories before reruns. If no `AIMANAGER_*_FILE` evidence env var is configured, the gate's evidence intake stage remains `BLOCKED` instead of passing on an empty file set. The individual commands remain useful for isolating a failed or blocked stage.

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

- Local compose rebuilt the runtime image, ran LiteLLM migrations, reached `Application startup complete`, and served health/model checks with only `gemini-2.5-flash`, `deepseek-chat`, and `ycapi-image-1` exposed.
- Surface and route smokes verify business port `4000` blocks UI, provider passthrough, config/cache/reload/model-write, business-token misuse, and uncommitted routes with AiManager policy errors before ycapi, while admin `127.0.0.1:4001` keeps LiteLLM management available under RBAC.
- Key-governance smokes verify `/key/generate` fails closed without metadata, normalizes valid UI/API payloads, injects shared-key `enforced_params`, and logs governance/audit events without leaking Authorization, Bearer, placeholder token, or key material.
- Mock ycapi runtime smokes verify nonzero spend logging for chat/image, budget-exceeded 429 after max-budget burn, freeze/revoke lifecycle blocks, SDK-compatible models/chat/image/vision through employee virtual keys, and sanitized Postgres-down behavior.
- Observability and alert tests verify management-only `/metrics`, exported JSON metrics/alerts, WeCom markdown rendering, severity filtering, webhook success handling, and `BLOCKED` status when live alert delivery has no webhook.
- Readiness gates verify production bundle, business-trial bundle, acceptance coverage matrix, final acceptance report, evidence intake, and golden fixtures; empty local production inputs still return `BLOCKED`, and secret-like env/header/DSN/WeCom values are redacted.
- Local streaming usage tests prove `aimanager/config.yaml` requires `always_include_stream_usage=true`, `validate_config` rejects missing or disabled stream usage accounting, the ASGI business chat boundary rewrites `stream_options.include_usage=false` to `true`, caps chat body buffering, fails closed on invalid body chunks, preserves non-stream request bytes, and mock ycapi can return OpenAI-compatible SSE chunks with a final `usage` event when usage is requested. This closes the local body-level bypass path for streaming spend attribution; production nonzero spend evidence still comes from the governed runtime smokes and live ycapi/finance bundles.
- Local lightweight trial evidence capture tests prove `capture_lightweight_trial_evidence` can safely convert a successful lightweight-entry submission result into AC-23 evidence for the business-trial gate while omitting prompt text, assistant text, request headers, Authorization, employee key values, ycapi token values, and WeCom webhook values; it supports project/customer context, blocks non-PASS submissions, requires `entry.work_context.status=PASS` with preflight normalized-context proof, rejects non-preflight or mismatched normalized proof, preserves the intended casefold match behavior, requires live-ycapi/brand-safety/no-secret-echo/html-escaped confirmations, rejects secret-like output values, secret-like assistant echo, and raw-key-looking aliases, and writes no output on `FAIL` or `BLOCKED`.
- Project policy gate evidence proves `make policy-check` returns PASS for `PROJECT_TYPE=software-cli`, required scaffold files, CLI help/json contract, AiManager-owned file length limits, documented overlay imports, ycapi-only config text, and `.env.example` secret-placeholder safety.
- Local work-context validation tests prove `validate_work_context` can gate an external marketing request before generation with non-empty string identifiers, scenario, channel, project/customer, sensitivity, required boolean approval, non-empty brief/reviewer/approval-policy references, and brand-safety checklist evidence; malformed JSON and invalid work contexts return `FAIL`, missing context files return `BLOCKED`, and closure mode requires draft, human-review, final, external-approval, archive, and retrospective references before the workflow can be treated as traceable. This is the machine-readable contract for a future non-SDK WeCom/lightweight entry, not the final business portal.
- Local lightweight-entry and lightweight-web tests prove `submit_lightweight_entry` can turn a flat WeCom/page-style form into a governed `/v1/chat/completions` body with work-context metadata plus `cost_center_id`, `currency`, and `pricing_version`; it preserves identifier casing for finance attribution, rejects token-like fields or values without echoing them, blocks missing employee virtual keys, refuses employee keys that equal `YCAPI_API_TOKEN`, and can submit through an injected raw HTTP transport without using an SDK. `lightweight_web` adds a clickable stdlib ASGI page for internal collaboration/management trials, keeps keys server-side, refuses ycapi token misuse, and HTML-escapes model output. This is local non-SDK UI evidence for AC-23, not production live ycapi, SSO identity injection, or timed human-trial evidence.
- Local business-overview tests prove `generate_business_overview` can combine monthly finance CSV, budget rows, and observability JSON into a CEO-readable JSON/Markdown report with monthly spend, budget utilization, anomaly events, TOP departments, TOP projects, and TOP keys; missing budget input returns `BLOCKED` and mixed currencies return `FAIL`. This is local report evidence for AC-24, not a production executive portal.
- Local monthly-close tests prove `generate_monthly_close_package` can combine `aimanager_finance_monthly.csv`, `aimanager_reconciliation.csv`, and a finance adjustment ledger into JSON/CSV/Markdown close evidence; the contract covers supplemental entries, reversals, attribution adjustments, reconciliation difference resolutions, residual `unassigned` blocking, duplicate adjustment failure, unsupported resolution failure, stable composite row keys when `entry_id` is absent, and no serialization of token-like extra fields. This is local close-package evidence for AC-25, not production ERP posting or general-ledger writeback.
- Local employee-monitoring tests prove `validate_employee_monitoring_policy` can validate AC-26 notice and permission-boundary evidence: metadata-only monitoring fields, prompt/response/raw IP/customer-content prohibitions, off-hours and suspected key-sharing rules, retention cap, reviewer role restrictions, HR/legal disciplinary guardrails, employee appeal channel, latest-version active-employee acknowledgment coverage, missing evidence `BLOCKED`, and token-like extras excluded from output. This is local policy/evidence contract, not proof that HR/legal has published the policy or that all real employees have acknowledged it.
- Local SDK compatibility tests prove `smoke_sdk_compat` uses an employee LiteLLM virtual key instead of `YCAPI_API_TOKEN`, rejects `YCAPI_API_TOKEN` as the employee-key env or value, checks `/v1/models`, chat models `gemini-2.5-flash` and `deepseek-chat`, image `ycapi-image-1` with `response_format=b64_json`, a vision chat request with a base64 `data:` URL, required work metadata, OpenAI-compatible response shape, and sanitized failure details. Business-surface work-context tests prove `AIMANAGER_WORK_CONTEXT_ENFORCEMENT_ENABLED=True` fails closed with 400 `aimanager_work_context_invalid` before LiteLLM for chat/image requests missing valid `user` + metadata, rejects `user`/`end_user_principal` mismatch, and does not echo prompt text in the error. `smoke_runtime_sdk_compat` now has local runtime PASS evidence against running business/admin surfaces with mock ycapi and fails if key cleanup fails.
- Local error-contract tests prove allowed downstream LiteLLM/ycapi 429 JSON errors preserve the upstream `error` object, inject top-level `request_id` aligned with `x-litellm-call-id`, keep 401/403/404 fallback types stable, pass successful streaming chunks through unchanged, normalize/redact SSE error events including split secrets, and convert non-JSON downstream 5xx errors to OpenAI-compatible JSON without leaking Bearer, `sk-*`, ycapi token text, or DSN passwords.

Remaining before business trial: run `make acceptance-gate` with real production URLs, a metadata-only production LiteLLM key inventory, production ycapi token policy, a live WeCom webhook plus alert report, real ycapi monthly bill evidence, AC-23 human trial evidence, and AC-26 HR/legal acknowledgment evidence until the final report in `/tmp/aimanager-acceptance-gate/final-acceptance-report.json` is `PASS`.

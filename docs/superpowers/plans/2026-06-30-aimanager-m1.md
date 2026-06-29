# AiManager M1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the AiManager M1 gateway so all supported LiteLLM API traffic is forced through ycapi, with testable route blocking, nonzero pricing, budget-ready spend tracking, key governance, audit, and acceptance evidence.

**Architecture:** AiManager keeps the full LiteLLM proxy for Admin UI, virtual keys, teams, budgets, rate limits, and spend logs. A project-owned ASGI wrapper provides the primary fail-closed runtime boundary before requests reach LiteLLM, while static config validation prevents ycapi bypass and zero-cost models from shipping.

**Tech Stack:** Python, FastAPI/Starlette ASGI middleware, LiteLLM proxy, PyYAML, pytest, Docker Compose, Postgres.

---

## File Structure

- Create `aimanager/asgi.py`: imports the LiteLLM proxy app and wraps it with AiManager policy middleware for runtime route control and error contract.
- Create `aimanager/policy.py`: pure policy helpers for method/path allowlist, blocked reasons, and OpenAI-compatible policy error bodies.
- Create `aimanager/tests/test_policy.py`: unit tests for route allowlist, blocked paths, method mismatch, and request id in policy errors.
- Modify `aimanager/scripts/validate_config.py`: enforce nonzero chat token prices, image `input_cost_per_image`, no `output_cost_per_image`, no passthrough endpoints.
- Modify `aimanager/tests/test_config.py`: failing tests for zero prices, wrong image pricing key, and pass-through config.
- Modify `aimanager/config.yaml`: replace placeholder 0.0 prices with nonzero M1 internal transfer prices and use `input_cost_per_image`.
- Modify `aimanager/docker-compose.yml`: start `aimanager.asgi:app` instead of directly starting LiteLLM proxy command, while still passing `/app/config.yaml`.
- Modify `aimanager/README.md`: document the new ASGI entrypoint, M1 route boundary, and validation commands.
- Modify `docs/aimanager/1_acceptance_criteria.md`: update AC statuses with current implemented evidence as work lands.
- Modify `项目知识图谱.md`: keep the project entry current after each meaningful implementation batch.

## Task 1: M1-A Static Pricing Guard

**Files:**
- Modify: `aimanager/scripts/validate_config.py`
- Modify: `aimanager/tests/test_config.py`
- Modify: `aimanager/config.yaml`

- [x] **Step 1: Write failing tests**

Add tests that:

```python
def test_validator_rejects_zero_chat_pricing(tmp_path: Path) -> None:
    bad_config = tmp_path / "bad-config.yaml"
    bad_config.write_text("""
model_list:
  - model_name: gemini-2.5-flash
    litellm_params:
      model: openai/gemini-2.5-flash
      api_base: os.environ/YCAPI_BASE_URL
      api_key: os.environ/YCAPI_API_TOKEN
      input_cost_per_token: 0.0
      output_cost_per_token: 0.000001
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
  store_model_in_db: false
""", encoding="utf-8")

    with pytest.raises(ConfigValidationError, match="input_cost_per_token"):
        validate_aimanager_config(bad_config)
```

Also add image pricing tests where `output_cost_per_image` is rejected and `input_cost_per_image <= 0` is rejected.

- [x] **Step 2: Verify red**

Run:

```bash
PYTHONPATH="$PWD" uv run --no-project --with pytest --with pyyaml pytest aimanager/tests/test_config.py -q
```

Expected: FAIL because the validator currently accepts zero chat prices and the wrong image price key.

- [x] **Step 3: Implement validator checks**

Update `validate_config.py` so chat models require positive numeric `input_cost_per_token` and `output_cost_per_token`; image models require positive numeric `input_cost_per_image` and reject `output_cost_per_image`.

- [x] **Step 4: Fix M1 config**

Set placeholder internal transfer prices in `aimanager/config.yaml`:

```yaml
input_cost_per_token: 0.0000001
output_cost_per_token: 0.0000004
input_cost_per_image: 0.01
```

These are nonzero M1 technical guardrail prices, not final finance-approved transfer prices.

- [x] **Step 5: Verify green**

Run:

```bash
uv run --no-project --with pyyaml python -m aimanager.scripts.validate_config aimanager/config.yaml
PYTHONPATH="$PWD" uv run --no-project --with pytest --with pyyaml pytest aimanager/tests/test_config.py -q
```

Expected: validator PASS, pytest PASS.

- [x] **Step 6: Commit**

```bash
git add aimanager/scripts/validate_config.py aimanager/tests/test_config.py aimanager/config.yaml
git commit -m "feat: enforce AiManager nonzero pricing"
```

## Task 2: M1-A Runtime Route Policy

**Files:**
- Create: `aimanager/policy.py`
- Create: `aimanager/asgi.py`
- Create: `aimanager/tests/test_policy.py`
- Modify: `aimanager/docker-compose.yml`

- [x] **Step 1: Write failing route policy tests**

Tests must assert:

- `GET /v1/models` is allowed.
- `POST /v1/chat/completions` is allowed.
- `POST /v1/images/generations` is allowed.
- Method mismatch is blocked.
- `/v1beta/models/gemini-2.5-flash:generateContent` is blocked.
- `/models/gemini-2.5-flash:streamGenerateContent` is blocked.
- `/vertex_ai/discovery/test` is blocked.
- `/pass-through-endpoints` is blocked.
- `/config/update` is blocked.
- `POST /model/new` is blocked.
- Error bodies include `error.code` and top-level `request_id`.

- [x] **Step 2: Verify red**

Run:

```bash
PYTHONPATH="$PWD" uv run --no-project --with pytest --with pyyaml pytest aimanager/tests/test_policy.py -q
```

Expected: FAIL because `aimanager.policy` does not exist.

- [x] **Step 3: Implement pure policy**

Implement method/path matching with exact business allowlist and explicit blocked prefixes/suffixes. Keep the module independent of LiteLLM imports so tests are fast.

- [x] **Step 4: Implement ASGI wrapper**

`aimanager/asgi.py` imports `litellm.proxy.proxy_server.app`, wraps it with middleware, and exports `app`. The middleware returns OpenAI-compatible 403 JSON for blocked paths and forwards allowed paths to LiteLLM.

- [x] **Step 5: Verify green**

Run:

```bash
PYTHONPATH="$PWD" uv run --no-project --with pytest --with pyyaml pytest aimanager/tests/test_policy.py -q
```

Expected: PASS.

- [x] **Step 6: Compose config check**

Update compose to launch the ASGI app and run:

```bash
docker compose -f aimanager/docker-compose.yml config
```

Expected: PASS and rendered command starts `aimanager.asgi:app`.

- [x] **Step 7: Commit**

```bash
git add aimanager/policy.py aimanager/asgi.py aimanager/tests/test_policy.py aimanager/docker-compose.yml
git commit -m "feat: add AiManager route allowlist"
```

Current note: policy, ASGI wrapper, Dockerfile runtime source copy, compose entrypoint, and local tests are complete. Compose now starts `aimanager.litellm_entrypoint`, which preserves LiteLLM CLI initialization while overriding the final uvicorn app to `aimanager.asgi:app`. A local runtime smoke proved LiteLLM migrations/lifespan, health, `/v1/models`, full blocked-route matrix, and structured policy audit logs. Before M1 trial, add spend-log proof for mock/live ycapi calls, runtime budget blocking, and key lifecycle emitters.

## Task 3: M1-B Key Governance and Enforced Params

**Files:**
- Create: `aimanager/governance.py`
- Create: `aimanager/tests/test_governance.py`
- Modify: `aimanager/README.md`

- [x] **Step 1: Write failing tests for key metadata**

Test that key creation metadata must include owner, department, project, cost center, scenario, budget, limits, expiry, and approver.

- [x] **Step 2: Verify red**

Run:

```bash
PYTHONPATH="$PWD" uv run --no-project --with pytest --with pyyaml pytest aimanager/tests/test_governance.py -q
```

Expected: FAIL because governance helpers do not exist.

- [x] **Step 3: Implement metadata validation and shared key defaults**

Produce helpers that normalize required key metadata and attach:

```json
{
  "enforced_params": ["user", "metadata.scenario_l1", "metadata.end_user_principal"]
}
```

- [x] **Step 4: Verify green and document runbook**

Run governance tests and update README with the key creation runbook.

- [x] **Step 5: Commit**

```bash
git add aimanager/governance.py aimanager/tests/test_governance.py aimanager/README.md
git commit -m "feat: define AiManager key governance"
```

## Task 4: M1-C Finance and Audit Foundations

**Files:**
- Create: `aimanager/finance.py`
- Create: `aimanager/audit.py`
- Create: `aimanager/tests/test_finance.py`
- Create: `aimanager/tests/test_audit.py`
- Modify: `aimanager/README.md`

- [x] **Step 1: Write failing finance tests**

Test daily usage aggregation, missing ownership dimensions as `unassigned`, monthly finance aggregation, AiManager-vs-ycapi reconciliation thresholds, and failed request spend retention.

- [x] **Step 2: Verify finance red**

Run:

```bash
PYTHONPATH="$PWD" uv run --no-project --with pytest --with pyyaml pytest aimanager/tests/test_finance.py -q
```

Expected: FAIL because `aimanager.finance` does not exist.

- [x] **Step 3: Implement finance helpers**

Implement spend record normalization, day/month aggregation, `Decimal` money handling, and monthly reconciliation with the materiality threshold `max(10 CNY, 1%)`.

- [x] **Step 4: Verify finance green**

Run:

```bash
PYTHONPATH="$PWD" uv run --no-project --with pytest --with pyyaml pytest aimanager/tests/test_finance.py -q
```

Expected: PASS.

- [x] **Step 5: Write failing audit tests**

Test standardized events for `passthrough_blocked`, `budget_blocked`, `key_frozen`, and `key_revoked`, including severity, reason requirements, request id, ownership dimensions, and metadata.

- [x] **Step 6: Verify audit red**

Run:

```bash
PYTHONPATH="$PWD" uv run --no-project --with pytest --with pyyaml pytest aimanager/tests/test_audit.py -q
```

Expected: FAIL because `aimanager.audit` does not exist.

- [x] **Step 7: Implement audit event contract**

Implement `build_audit_event` with stable required fields, default severities, key lifecycle reason validation, and `unassigned` dimension preservation.

- [x] **Step 8: Verify audit green and document runbook**

Run audit tests and update README with finance reporting and audit event usage notes.

## Task 5: M1-B Acceptance Evidence and Final Verification

**Files:**
- Modify: `docs/aimanager/1_acceptance_criteria.md`
- Modify: `aimanager/README.md`
- Modify: `项目知识图谱.md`

- [x] **Step 1: Run full local verification**

Run:

```bash
uv run --no-project --with pyyaml python -m aimanager.scripts.validate_config aimanager/config.yaml
PYTHONPATH="$PWD" uv run --no-project --with pytest --with pyyaml pytest aimanager/tests -q
docker compose -f aimanager/docker-compose.yml config
git diff --check
```

Expected: all PASS.

- [x] **Step 2: Update acceptance evidence**

Mark implemented local checks as PASS, live ycapi checks as BLOCKED if no real `YCAPI_API_TOKEN` is provided, and unimplemented M2 business workflows as SKIP or pending according to the acceptance file’s existing format.

- [x] **Step 3: Update knowledge graph**

Record the ASGI entrypoint, pricing validator, policy tests, and remaining M1-B/M2 work in `项目知识图谱.md`.

- [x] **Step 4: Commit**

```bash
git add docs/aimanager/1_acceptance_criteria.md aimanager/README.md 项目知识图谱.md
git commit -m "docs: record AiManager M1 evidence"
```

## Task 6: M1-A Runtime Entrypoint Hardening

**Files:**
- Create: `aimanager/litellm_entrypoint.py`
- Create: `aimanager/tests/test_litellm_entrypoint.py`
- Modify: `aimanager/docker-compose.yml`
- Modify: `aimanager/tests/test_config.py`
- Modify: `aimanager/README.md`
- Modify: `docs/aimanager/1_acceptance_criteria.md`
- Modify: `项目知识图谱.md`

- [x] **Step 1: Write failing entrypoint tests**

Test that the AiManager entrypoint preserves LiteLLM's uvicorn arguments while replacing only the app target with `aimanager.asgi:app`.

- [x] **Step 2: Verify red**

Run:

```bash
PYTHONPATH="$PWD" uv run --no-project --with pytest --with pyyaml pytest aimanager/tests/test_litellm_entrypoint.py -q
```

Expected: FAIL because `aimanager.litellm_entrypoint` does not exist.

- [x] **Step 3: Implement CLI wrapper**

Implement `aimanager.litellm_entrypoint` so runtime still goes through LiteLLM CLI config/DB/migration setup, then overrides uvicorn's app to `aimanager.asgi:app`.

- [x] **Step 4: Update compose contract**

Change compose to start `python -m aimanager.litellm_entrypoint --config=/app/config.yaml --host=0.0.0.0 --port=4000 --enforce_prisma_migration_check`.

- [x] **Step 5: Add runtime fail-fast and build target checks**

Ensure compose uses Docker `target: runtime` instead of a no-op build arg, and ensure `aimanager.litellm_entrypoint` refuses to start without non-empty `LITELLM_MASTER_KEY`, `YCAPI_BASE_URL`, and `YCAPI_API_TOKEN`.

- [x] **Step 6: Verify local contract**

Run:

```bash
PYTHONPATH="$PWD" uv run --no-project --with pytest --with pyyaml pytest aimanager/tests/test_config.py::test_compose_uses_aimanager_litellm_entrypoint aimanager/tests/test_litellm_entrypoint.py -q
docker compose -f aimanager/docker-compose.yml config
```

Expected: PASS.

- [x] **Step 7: Verify local runtime smoke**

Run with local placeholder credentials:

```bash
LITELLM_MASTER_KEY=aimanager-local-master-key \
YCAPI_BASE_URL=https://ycapi.ycaicloud.com/v1 \
YCAPI_API_TOKEN=aimanager-local-ycapi-token \
docker compose -f aimanager/docker-compose.yml up -d --force-recreate --no-build
```

Observed: Postgres and AiManager started healthy, LiteLLM Prisma migrations and post-migration sanity check completed, `GET /health/liveliness` returned 200, `GET /v1/models` returned only the three ycapi-backed models, and `POST /anthropic/messages` plus `POST /config/update` returned AiManager 403 policy errors.

## Task 7: M1-A Runtime Policy Audit and Blocked Route Matrix

**Files:**
- Modify: `aimanager/audit.py`
- Modify: `aimanager/asgi.py`
- Create: `aimanager/scripts/smoke_blocked_routes.py`
- Create: `aimanager/tests/test_blocked_route_smoke.py`
- Modify: `aimanager/tests/test_audit.py`
- Modify: `aimanager/tests/test_policy.py`
- Modify: `aimanager/README.md`
- Modify: `docs/aimanager/1_acceptance_criteria.md`
- Modify: `项目知识图谱.md`

- [x] **Step 1: Write failing audit emitter tests**

Test that policy-blocked requests emit audit events with request id, actor/key/dimension headers when present, and no Authorization leakage.

- [x] **Step 2: Implement runtime audit emitter**

`aimanager.asgi` now emits structured `aimanager_audit_event` logs before policy 403 responses. Provider/native routes use `passthrough_blocked`; config/model/default-denied routes use `policy_blocked`.

- [x] **Step 3: Write and implement blocked-route smoke script**

`aimanager.scripts.smoke_blocked_routes` checks 33 provider/native/config/cache/reload/model-write/uncommitted routes against a running proxy and requires 403 plus matching `x-aimanager-policy-code` and body `error.code`.

- [x] **Step 4: Verify runtime evidence**

With the current rebuilt image, `python -m aimanager.scripts.smoke_blocked_routes --base-url http://localhost:4000` returned PASS for all 33 cases. Container logs contained 34 `aimanager_audit_event` entries after the smoke plus a business-surface `/ui` block, and the audit log tail had no Authorization/Bearer/local placeholder token content.

## Task 8: M1-A Business and Management Surface Split

**Files:**
- Modify: `aimanager/policy.py`
- Modify: `aimanager/asgi.py`
- Modify: `aimanager/docker-compose.yml`
- Modify: `aimanager/tests/test_policy.py`
- Modify: `aimanager/tests/test_config.py`
- Modify: `aimanager/README.md`
- Modify: `docs/aimanager/1_acceptance_criteria.md`
- Modify: `项目知识图谱.md`

- [x] **Step 1: Write failing surface tests**

Tests assert that the default/business surface blocks LiteLLM management routes such as `/ui`, `/key/generate`, `/team/new`, `/user/new`, `/spend/logs`, and `/global/spend`; the management surface allows UI/key/team/user/budget/spend routes; and management still blocks provider passthrough, Google native routes, `/config/update`, `/pass-through-endpoints`, high-risk config/cache/reload mutation, and model writes.

- [x] **Step 2: Verify red**

Run:

```bash
PYTHONPATH="$PWD" uv run --no-project --with pytest --with pyyaml pytest \
  aimanager/tests/test_policy.py::test_business_surface_blocks_litellm_management_routes \
  aimanager/tests/test_policy.py::test_management_surface_allows_litellm_management_routes \
  aimanager/tests/test_policy.py::test_management_surface_still_blocks_provider_config_and_model_write_routes \
  aimanager/tests/test_policy.py::test_management_surface_middleware_forwards_management_requests \
  aimanager/tests/test_config.py::test_compose_exposes_management_surface_only_on_localhost_profile -q
```

Observed: FAIL because `evaluate_route(..., surface=...)`, `YcapiOnlyAllowlistMiddleware(..., surface=...)`, and `aimanager-admin` did not exist yet.

- [x] **Step 3: Implement surface-aware policy and ASGI wrapper**

`evaluate_route` now accepts `surface="business" | "management"` and normalizes unknown values to the fail-closed business surface. Provider/native/config/model-write blocks run before management allow rules.

- [x] **Step 4: Add local admin compose profile**

Default `aimanager` stays on `AIMANAGER_ROUTE_SURFACE=business` at `4000`. `aimanager-admin` is only rendered with `--profile admin`, uses `AIMANAGER_ROUTE_SURFACE=management`, and binds `127.0.0.1:4001:4000`.

- [x] **Step 5: Verify green**

Run:

```bash
PYTHONPATH="$PWD" uv run --no-project --with pytest --with pyyaml pytest aimanager/tests -q
docker compose -f aimanager/docker-compose.yml config
docker compose -f aimanager/docker-compose.yml --profile admin config
```

Observed: 76 tests passed; compose rendered business surface by default and localhost-only admin surface under the `admin` profile. Runtime checks showed business `4000` blocks `/ui`, admin `127.0.0.1:4001` reaches LiteLLM UI redirect, and admin `/config/field/update` is still `aimanager_config_immutable`.

## Task 9: M1-A Offline LiteLLM Startup Cost Map

**Files:**
- Modify: `aimanager/litellm_entrypoint.py`
- Modify: `aimanager/docker-compose.yml`
- Modify: `aimanager/tests/test_litellm_entrypoint.py`
- Modify: `aimanager/tests/test_config.py`
- Modify: `aimanager/README.md`
- Modify: `docs/aimanager/1_acceptance_criteria.md`
- Modify: `项目知识图谱.md`

- [x] **Step 1: Reproduce startup hang**

After Docker Desktop recovered from a local runtime interruption, a rebuilt AiManager container stayed alive but unhealthy with no listener on port 4000. `faulthandler.dump_traceback_later()` showed LiteLLM import blocking in `fetch_remote_model_cost_map()` during an external TLS handshake. A second stack showed the later Pydantic schema-generation phase, confirming the root startup blocker began before the proxy listener.

- [x] **Step 2: Force local model cost map before LiteLLM import**

`aimanager.litellm_entrypoint` now sets `LITELLM_LOCAL_MODEL_COST_MAP=True` before importing `litellm.proxy.proxy_cli`. Compose also sets the same environment variable on both business and admin surfaces.

- [x] **Step 3: Verify startup protection**

Run:

```bash
PYTHONPATH="$PWD" uv run --no-project --with pytest --with pyyaml pytest aimanager/tests -q
docker compose -f aimanager/docker-compose.yml config
docker compose -f aimanager/docker-compose.yml --profile admin config
```

Observed: 76 tests passed. With `LITELLM_LOCAL_MODEL_COST_MAP=True`, `docker run ... import litellm` completed in about 3.1s. A rebuilt runtime image started healthy, `GET /health/liveliness` returned 200, the 33-case blocked-route smoke passed, business logs contained 34 structured audit events without token leakage, and admin `127.0.0.1:4001` returned `/ui` 307 while still blocking `/config/field/update` with `aimanager_config_immutable`.

## Task 10: M1-B Management Key Generate Governance Wiring

**Files:**
- Modify: `aimanager/asgi.py`
- Modify: `aimanager/tests/test_policy.py`
- Modify: `aimanager/README.md`
- Modify: `docs/aimanager/1_acceptance_criteria.md`
- Modify: `项目知识图谱.md`

- [x] **Step 1: Write failing ASGI key-generate tests**

Tests assert that management `POST /key/generate` rejects payloads without required governance metadata, never calls downstream LiteLLM for invalid key creation, preserves `request_id`, emits a `policy_blocked` audit event with `reason=aimanager_key_governance_invalid`, forwards valid normalized JSON bodies, and injects `enforced_params` for `metadata.shared_key=true`.

- [x] **Step 2: Verify red**

Run:

```bash
PYTHONPATH="$PWD" uv run --no-project --with pytest --with pyyaml pytest aimanager/tests/test_policy.py -q
```

Observed: FAIL because management `/key/generate` was still forwarded to LiteLLM without calling `aimanager.governance.normalize_key_request`.

- [x] **Step 3: Implement narrow ASGI body governance**

`YcapiOnlyAllowlistMiddleware` now gates only `surface=management`, method `POST`, normalized path `/key/generate`. It drains and size-limits the ASGI request body, requires a JSON object, runs `normalize_key_request`, replays the normalized JSON body to downstream LiteLLM with corrected `content-length`, and returns HTTP 400 with `error.code=aimanager_key_governance_invalid` for invalid governance payloads.

- [x] **Step 4: Verify green**

Run:

```bash
PYTHONPATH="$PWD" uv run --no-project --with pytest --with pyyaml pytest aimanager/tests/test_policy.py -q
PYTHONPATH="$PWD" uv run --no-project --with pytest --with pyyaml pytest aimanager/tests -q
```

Observed: `test_policy.py` passed 25 tests; full AiManager suite passed 81 tests. After rebuilding the shared `aimanager-litellm:local` image and recreating `aimanager-admin`, runtime `POST /key/generate` without governance metadata returned HTTP 400 with `x-aimanager-policy-code: aimanager_key_governance_invalid` and preserved `x-litellm-call-id=runtime-key-governance-smoke`; admin logs emitted one matching `policy_blocked` audit event with no Authorization/Bearer leakage. A valid shared-key runtime smoke returned the required employee/team/model/budget/rate-limit/expiry fields plus department/project/cost-center/scenario/approver metadata and `metadata.enforced_params`; the smoke key was deleted immediately after verification. AC-08 is now PASS for API coverage; UI automation remains a follow-up.

## Self-Review

- Spec coverage: Tasks 1 and 2 cover the hard M1-A P0 items from the synthesis design. Task 3 covers key governance. Task 4 adds finance aggregation, reconciliation, and audit event foundations. Task 5 records acceptance evidence. Task 6 hardens startup so the ASGI boundary does not bypass LiteLLM CLI initialization and proves a local container boot. Task 7 proves the full blocked-route matrix and policy audit logs. Task 8 preserves LiteLLM management routes on a localhost-only management surface while keeping the business surface ycapi-only. Task 9 removes LiteLLM import-time remote cost-map dependency from AiManager startup. Task 10 wires key governance into management `/key/generate` before LiteLLM and proves API key creation metadata in runtime. Full spend-log, runtime budget blocking, key lifecycle emitters, UI key-flow automation, and live ycapi verification remain separate follow-up work.
- Placeholder scan: no `TODO`, `TBD`, or unspecified “handle edge cases” instructions are used.
- Type consistency: all planned Python modules live under `aimanager/`, tests use `pytest`, and commands match the existing project validation pattern.

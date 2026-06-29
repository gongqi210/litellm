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

- [ ] **Step 7: Commit**

```bash
git add aimanager/policy.py aimanager/asgi.py aimanager/tests/test_policy.py aimanager/docker-compose.yml
git commit -m "feat: add AiManager route allowlist"
```

Current note: policy, ASGI wrapper, Dockerfile runtime source copy, compose entrypoint, and local tests are complete. Before M1 trial, add a route inventory check with full proxy dependencies and run a real container boot to prove LiteLLM lifespan/migrations, `/v1/models`, blocked-route no-outbound, and spend logging.

## Task 3: M1-B Key Governance and Enforced Params

**Files:**
- Create: `aimanager/governance.py`
- Create: `aimanager/tests/test_governance.py`
- Modify: `aimanager/README.md`

- [ ] **Step 1: Write failing tests for key metadata**

Test that key creation metadata must include owner, department, project, cost center, scenario, budget, limits, expiry, and approver.

- [ ] **Step 2: Verify red**

Run:

```bash
PYTHONPATH="$PWD" uv run --no-project --with pytest --with pyyaml pytest aimanager/tests/test_governance.py -q
```

Expected: FAIL because governance helpers do not exist.

- [ ] **Step 3: Implement metadata validation and shared key defaults**

Produce helpers that normalize required key metadata and attach:

```json
{
  "enforced_params": ["user", "metadata.scenario_l1", "metadata.end_user_principal"]
}
```

- [ ] **Step 4: Verify green and document runbook**

Run governance tests and update README with the key creation runbook.

- [ ] **Step 5: Commit**

```bash
git add aimanager/governance.py aimanager/tests/test_governance.py aimanager/README.md
git commit -m "feat: define AiManager key governance"
```

## Task 4: M1-B Acceptance Evidence and Final Verification

**Files:**
- Modify: `docs/aimanager/1_acceptance_criteria.md`
- Modify: `aimanager/README.md`
- Modify: `项目知识图谱.md`

- [ ] **Step 1: Run full local verification**

Run:

```bash
uv run --no-project --with pyyaml python -m aimanager.scripts.validate_config aimanager/config.yaml
PYTHONPATH="$PWD" uv run --no-project --with pytest --with pyyaml pytest aimanager/tests -q
docker compose -f aimanager/docker-compose.yml config
git diff --check
```

Expected: all PASS.

- [ ] **Step 2: Update acceptance evidence**

Mark implemented local checks as PASS, live ycapi checks as BLOCKED if no real `YCAPI_API_TOKEN` is provided, and unimplemented M2 business workflows as SKIP or pending according to the acceptance file’s existing format.

- [ ] **Step 3: Update knowledge graph**

Record the ASGI entrypoint, pricing validator, policy tests, and remaining M1-B/M2 work in `项目知识图谱.md`.

- [ ] **Step 4: Commit**

```bash
git add docs/aimanager/1_acceptance_criteria.md aimanager/README.md 项目知识图谱.md
git commit -m "docs: record AiManager M1 evidence"
```

## Self-Review

- Spec coverage: Tasks 1 and 2 cover the hard M1-A P0 items from the synthesis design. Tasks 3 and 4 cover key governance and acceptance evidence. Full spend-log and live ycapi verification remain separate follow-up work because they need a running proxy and real or mock upstream integration.
- Placeholder scan: no `TODO`, `TBD`, or unspecified “handle edge cases” instructions are used.
- Type consistency: all planned Python modules live under `aimanager/`, tests use `pytest`, and commands match the existing project validation pattern.

# LiteLLM Makefile
# Simple Makefile for running tests and basic development tasks

.PHONY: help policy-check key-inventory-export key-inventory-readiness admin-boundary-smoke admin-boundary-readiness finance-export finance-readiness wecom-alert-route wecom-alert-readiness production-readiness live-ycapi-preflight live-ycapi-roundtrip work-context-enforcement-smoke work-context-enforcement-readiness production-policy-readiness employee-monitoring-validate lightweight-trial-evidence-capture acceptance-gate evidence-handoff evidence-template-pack evidence-intake test test-unit test-unit-llms test-unit-proxy-guardrails test-unit-proxy-core test-unit-proxy-misc \
	test-unit-integrations test-unit-core-utils test-unit-other test-unit-root \
	test-proxy-unit-a test-proxy-unit-b test-integration test-unit-helm \
	info lint lint-dev format \
	lint-basedpyright lint-basedpyright-budget-update \
	lint-ruff-budget lint-ruff-budget-update lint-budget-update lint-gate \
	install-dev install-proxy-dev install-test-deps install-hooks \
	install-helm-unittest check-circular-imports check-import-safety

# Default target
help:
	@echo "Available commands:"
	@echo "  make policy-check       - Run AiManager project policy gate"
	@echo "  make key-inventory-export - Export metadata-only LiteLLM virtual-key inventory for AC-08"
	@echo "  make key-inventory-readiness - Export, validate, and rerun readiness for AC-08"
	@echo "  make admin-boundary-smoke - Probe production business/admin exposure boundaries for AC-15"
	@echo "  make admin-boundary-readiness - Preview admin boundary and rerun readiness for AC-15"
	@echo "  make finance-export     - Export AiManager finance CSVs from LiteLLM spend and ycapi bill evidence"
	@echo "  make finance-readiness  - Export finance CSVs and rerun readiness for AC-12/13"
	@echo "  make wecom-alert-route  - Route AiManager observability alerts to WeCom for AC-16"
	@echo "  make wecom-alert-readiness - Dry-run WeCom payload and rerun live readiness for AC-16"
	@echo "  make production-readiness - Run AiManager production readiness bundle"
	@echo "  make live-ycapi-preflight - Run read-only live ycapi /models preflight for AC-19"
	@echo "  make live-ycapi-roundtrip - Run live ycapi /models + chat/image roundtrip smoke for AC-19"
	@echo "  make work-context-enforcement-smoke - Verify business chat/image reject missing work context"
	@echo "  make work-context-enforcement-readiness - Verify AC-20 fail-closed plus valid work-context roundtrip"
	@echo "  make production-policy-readiness - Rerun production readiness with AC-POLICY attestation"
	@echo "  make employee-monitoring-validate - Validate AC-26 employee monitoring notice and acknowledgment evidence"
	@echo "  make lightweight-trial-evidence-capture - Capture AC-23 non-SDK trial evidence from a successful entry result"
	@echo "  make acceptance-gate    - Run AiManager one-command acceptance gate into /tmp or AIMANAGER_ACCEPTANCE_GATE_OUTPUT_DIR"
	@echo "  make evidence-handoff   - Generate owner-specific evidence requests from the latest acceptance gate"
	@echo "  make evidence-template-pack - Generate safe external evidence input templates from the latest acceptance gate"
	@echo "  make evidence-intake    - Validate filled external evidence files before rerunning the acceptance gate"
	@echo "  make install-dev        - Install development dependencies"
	@echo "  make install-proxy-dev  - Install proxy development dependencies"
	@echo "  make install-dev-ci     - Install dev dependencies (CI-compatible, pins OpenAI)"
	@echo "  make install-proxy-dev-ci - Install proxy dev dependencies (CI-compatible)"
	@echo "  make install-test-deps  - Install the full local test environment"
	@echo "  make install-helm-unittest - Install helm unittest plugin"
	@echo "  make install-hooks      - Install git hooks (Conventional Commits + Branches)"
	@echo "  make format             - Apply ruff format code formatting"
	@echo "  make format-check       - Check ruff format code formatting (matches CI)"
	@echo "  make lint               - Run all linting (Ruff, basedpyright, format check, circular imports, import safety)"
	@echo "  make lint-ruff          - Run Ruff linting only"
	@echo "  make lint-basedpyright  - Run basedpyright strict, gated by per-rule error counts"
	@echo "  make lint-basedpyright-budget-update - Re-capture the basedpyright per-rule budget (ratchet)"
	@echo "  make lint-format        - Check ruff format formatting (matches CI)"
	@echo "  make lint-ruff-budget - Gate the codebase total of each strict ruff rule against its ceiling"
	@echo "  make lint-gate        - Strict ruff gate in CI-parity mode (fetches staging, simulates the merge)"
	@echo "  make lint-ruff-budget-update - Re-capture per-rule baselines in ruff-strict-budget.json (ratchet)"
	@echo "  make lint-budget-update - Re-capture all ratchet budgets (ruff + basedpyright)"
	@echo "  make check-circular-imports - Check for circular imports"
	@echo "  make check-import-safety - Check import safety"
	@echo "  make test               - Run all tests"
	@echo "  make test-unit          - Run unit tests (tests/test_litellm)"
	@echo "  make test-unit-llms     - Run LLM provider tests (~225 files)"
	@echo "  make test-unit-proxy-guardrails - Run proxy guardrails+mgmt tests (~51 files)"
	@echo "  make test-unit-proxy-core - Run proxy auth+client+db+hooks tests (~52 files)"
	@echo "  make test-unit-proxy-misc - Run proxy misc tests (~77 files)"
	@echo "  make test-unit-integrations - Run integration tests (~60 files)"
	@echo "  make test-unit-core-utils - Run core utils tests (~32 files)"
	@echo "  make test-unit-other    - Run other tests (caching, responses, etc., ~69 files)"
	@echo "  make test-unit-root     - Run root-level tests (~34 files)"
	@echo "  make test-proxy-unit-a  - Run proxy_unit_tests (a-o, ~20 files)"
	@echo "  make test-proxy-unit-b  - Run proxy_unit_tests (p-z, ~28 files)"
	@echo "  make test-integration   - Run integration tests"
	@echo "  make test-unit-helm     - Run helm unit tests"

UV := uv
UV_RUN := $(UV) run --no-sync

policy-check:
	python3 scripts/project_policy_check.py

key-inventory-export:
	PYTHONPATH="$$(pwd)" uv run --no-project python -m aimanager.scripts.export_key_inventory --admin-base-url "$${AIMANAGER_ADMIN_BASE_URL:-http://127.0.0.1:4001}" --output-inventory-file "$${AIMANAGER_KEY_INVENTORY_FILE:-/tmp/aimanager-key-inventory.json}" --output-json-file "$${AIMANAGER_KEY_INVENTORY_EXPORT_RESULT_FILE:-/tmp/aimanager-key-inventory-export.json}"

key-inventory-readiness:
	AIMANAGER_KEY_INVENTORY_FILE="$${AIMANAGER_KEY_INVENTORY_FILE:-/tmp/aimanager-key-inventory.json}" $(MAKE) key-inventory-export
	AIMANAGER_KEY_INVENTORY_FILE="$${AIMANAGER_KEY_INVENTORY_FILE:-/tmp/aimanager-key-inventory.json}" AIMANAGER_EMPLOYEE_MONITORING_POLICY_FILE="$${AIMANAGER_EMPLOYEE_MONITORING_POLICY_FILE:-docs/aimanager/aimanager-employee-monitoring-policy.json}" PYTHONPATH="$$(pwd)" uv run --no-project python -m aimanager.scripts.validate_key_inventory --inventory-file "$${AIMANAGER_KEY_INVENTORY_FILE:-/tmp/aimanager-key-inventory.json}" --require-acknowledged-employees --output-json-file "$${AIMANAGER_KEY_INVENTORY_VALIDATION_RESULT_FILE:-/tmp/aimanager-key-inventory-validation.json}"
	AIMANAGER_KEY_INVENTORY_FILE="$${AIMANAGER_KEY_INVENTORY_FILE:-/tmp/aimanager-key-inventory.json}" AIMANAGER_EMPLOYEE_MONITORING_POLICY_FILE="$${AIMANAGER_EMPLOYEE_MONITORING_POLICY_FILE:-docs/aimanager/aimanager-employee-monitoring-policy.json}" $(MAKE) production-readiness

admin-boundary-smoke:
	AIMANAGER_ALLOWED_SSO_REDIRECT_HOSTS="$${AIMANAGER_ALLOWED_SSO_REDIRECT_HOSTS:-}" PYTHONPATH="$$(pwd)" uv run --no-project python -m aimanager.scripts.smoke_admin_boundary --business-base-url "$${AIMANAGER_BUSINESS_BASE_URL}" --public-admin-url "$${AIMANAGER_PUBLIC_ADMIN_URL}" --require-business-base-url --require-public-admin-url

admin-boundary-readiness:
	$(MAKE) admin-boundary-smoke || true
	$(MAKE) production-readiness

finance-export:
	PYTHONPATH="$$(pwd)" uv run --no-project python -m aimanager.scripts.export_finance --spend-file "$${AIMANAGER_SPEND_FILE}" --ycapi-bill-file "$${AIMANAGER_YCAPI_BILL_FILE}" --output-dir "$${AIMANAGER_FINANCE_OUTPUT_DIR:-/tmp/aimanager-finance-export}"

finance-readiness:
	$(MAKE) finance-export
	$(MAKE) production-readiness

wecom-alert-route:
	@dry_run_arg=""; \
	if [ "$${AIMANAGER_WECOM_DRY_RUN}" = "true" ]; then dry_run_arg="--dry-run"; fi; \
	PYTHONPATH="$$(pwd)" uv run --no-project python -m aimanager.scripts.route_observability_alerts --report-file "$${AIMANAGER_OBSERVABILITY_REPORT_FILE}" --webhook-url "$${AIMANAGER_WECOM_WEBHOOK_URL}" $$dry_run_arg --min-severity "$${AIMANAGER_WECOM_MIN_SEVERITY:-warning}" --title "AiManager production readiness alerts" --output-payload-file /tmp/aimanager-wecom-alert-payload.json

wecom-alert-readiness:
	$(MAKE) wecom-alert-route AIMANAGER_WECOM_DRY_RUN=true || true
	$(MAKE) production-readiness

production-readiness:
	PYTHONPATH="$$(pwd)" uv run --no-project --with pyyaml python -m aimanager.scripts.production_readiness_bundle --output-json-file "$${AIMANAGER_PRODUCTION_READINESS_OUTPUT_FILE:-/tmp/aimanager-production-readiness.json}"

live-ycapi-preflight:
	PYTHONPATH="$$(pwd)" uv run --no-project python -m aimanager.scripts.smoke_live_ycapi --expect-model gemini-2.5-flash --expect-model deepseek-chat --expect-model ycapi-image-1

live-ycapi-roundtrip:
	PYTHONPATH="$$(pwd)" uv run --no-project python -m aimanager.scripts.smoke_live_ycapi --expect-model gemini-2.5-flash --expect-model deepseek-chat --expect-model ycapi-image-1 --run-inference-roundtrip

work-context-enforcement-smoke:
	PYTHONPATH="$$(pwd)" uv run --no-project python -m aimanager.scripts.smoke_work_context_enforcement --base-url "$${AIMANAGER_BUSINESS_BASE_URL:-http://localhost:4000}"

work-context-enforcement-readiness:
	$(MAKE) work-context-enforcement-smoke || true
	$(MAKE) production-readiness

production-policy-readiness:
	AIMANAGER_PRODUCTION_POLICY_ATTESTATION_FILE="$${AIMANAGER_PRODUCTION_POLICY_ATTESTATION_FILE}" $(MAKE) production-readiness

employee-monitoring-validate:
	PYTHONPATH="$$(pwd)" uv run --no-project --with pyyaml python -m aimanager.scripts.validate_employee_monitoring_policy --policy-file "$${AIMANAGER_EMPLOYEE_MONITORING_POLICY_FILE:-docs/aimanager/aimanager-employee-monitoring-policy.json}" --employee-roster-file "$${AIMANAGER_EMPLOYEE_ROSTER_FILE}" --acknowledgment-file "$${AIMANAGER_EMPLOYEE_ACKNOWLEDGMENT_FILE}" --output-json-file "$${AIMANAGER_EMPLOYEE_MONITORING_RESULT_FILE:-/tmp/aimanager-employee-monitoring.json}" --output-markdown-file "$${AIMANAGER_EMPLOYEE_MONITORING_MARKDOWN_FILE:-/tmp/aimanager-employee-monitoring.md}"

lightweight-trial-evidence-capture:
	@if [ "$${AIMANAGER_LIGHTWEIGHT_TRIAL_LIVE_YCAPI_CONFIRMED}" != "true" ]; then echo "BLOCKED AC-23: set AIMANAGER_LIGHTWEIGHT_TRIAL_LIVE_YCAPI_CONFIRMED=true after live ycapi evidence is captured." >&2; exit 2; fi
	@if [ "$${AIMANAGER_LIGHTWEIGHT_TRIAL_BRAND_SAFETY_CONFIRMED}" != "true" ]; then echo "BLOCKED AC-23: set AIMANAGER_LIGHTWEIGHT_TRIAL_BRAND_SAFETY_CONFIRMED=true after brand-safety evidence is captured." >&2; exit 2; fi
	@if [ "$${AIMANAGER_LIGHTWEIGHT_TRIAL_NO_SECRET_ECHO_CONFIRMED}" != "true" ]; then echo "BLOCKED AC-23: set AIMANAGER_LIGHTWEIGHT_TRIAL_NO_SECRET_ECHO_CONFIRMED=true after secret redaction evidence is captured." >&2; exit 2; fi
	@if [ "$${AIMANAGER_LIGHTWEIGHT_TRIAL_HTML_ESCAPED_CONFIRMED}" != "true" ]; then echo "BLOCKED AC-23: set AIMANAGER_LIGHTWEIGHT_TRIAL_HTML_ESCAPED_CONFIRMED=true after HTML escaping evidence is captured." >&2; exit 2; fi
	@confirm_args=""; \
	if [ "$${AIMANAGER_LIGHTWEIGHT_TRIAL_LIVE_YCAPI_CONFIRMED}" = "true" ]; then confirm_args="$$confirm_args --live-ycapi-confirmed"; fi; \
	if [ "$${AIMANAGER_LIGHTWEIGHT_TRIAL_BRAND_SAFETY_CONFIRMED}" = "true" ]; then confirm_args="$$confirm_args --brand-safety-confirmed"; fi; \
	if [ "$${AIMANAGER_LIGHTWEIGHT_TRIAL_NO_SECRET_ECHO_CONFIRMED}" = "true" ]; then confirm_args="$$confirm_args --no-secret-echo-confirmed"; fi; \
	if [ "$${AIMANAGER_LIGHTWEIGHT_TRIAL_HTML_ESCAPED_CONFIRMED}" = "true" ]; then confirm_args="$$confirm_args --html-escaped-confirmed"; fi; \
	PYTHONPATH="$$(pwd)" uv run --no-project --with pyyaml python -m aimanager.scripts.capture_lightweight_trial_evidence --lightweight-entry-result-file "$${AIMANAGER_LIGHTWEIGHT_ENTRY_RESULT_FILE:-/tmp/aimanager-lightweight-entry-submit.json}" --request-id "$${AIMANAGER_LIGHTWEIGHT_TRIAL_REQUEST_ID}" --spend "$${AIMANAGER_LIGHTWEIGHT_TRIAL_SPEND}" --operator-role "$${AIMANAGER_LIGHTWEIGHT_TRIAL_OPERATOR_ROLE:-marketing}" --identity-source "$${AIMANAGER_LIGHTWEIGHT_TRIAL_IDENTITY_SOURCE:-sso}" --employee-virtual-key-alias "$${AIMANAGER_LIGHTWEIGHT_TRIAL_KEY_ALIAS}" --started-at "$${AIMANAGER_LIGHTWEIGHT_TRIAL_STARTED_AT}" --completed-at "$${AIMANAGER_LIGHTWEIGHT_TRIAL_COMPLETED_AT}" --observer "$${AIMANAGER_LIGHTWEIGHT_TRIAL_OBSERVER}" --captured-at "$${AIMANAGER_LIGHTWEIGHT_TRIAL_CAPTURED_AT}" $$confirm_args --output-json-file "$${AIMANAGER_LIGHTWEIGHT_TRIAL_EVIDENCE_FILE:-/tmp/aimanager-ac23-trial-evidence.json}"

acceptance-gate:
	PYTHONPATH="$$(pwd)" uv run --no-project --with pyyaml python -m aimanager.scripts.run_acceptance_gate --output-dir "$${AIMANAGER_ACCEPTANCE_GATE_OUTPUT_DIR:-/tmp/aimanager-acceptance-gate}"

evidence-handoff:
	PYTHONPATH="$$(pwd)" uv run --no-project python -m aimanager.scripts.generate_evidence_handoff --launch-gap-plan-file "$${AIMANAGER_ACCEPTANCE_GATE_OUTPUT_DIR:-/tmp/aimanager-acceptance-gate}/launch-gap-plan.json" --output-dir "$${AIMANAGER_EVIDENCE_HANDOFF_OUTPUT_DIR:-/tmp/aimanager-evidence-handoff}"

evidence-template-pack:
	PYTHONPATH="$$(pwd)" uv run --no-project python -m aimanager.scripts.generate_evidence_template_pack --launch-gap-plan-file "$${AIMANAGER_ACCEPTANCE_GATE_OUTPUT_DIR:-/tmp/aimanager-acceptance-gate}/launch-gap-plan.json" --output-dir "$${AIMANAGER_EVIDENCE_TEMPLATE_PACK_OUTPUT_DIR:-/tmp/aimanager-evidence-template-pack}"

evidence-intake:
	PYTHONPATH="$$(pwd)" uv run --no-project python -m aimanager.scripts.validate_evidence_intake --input-dir "$${AIMANAGER_EVIDENCE_INTAKE_INPUT_DIR:-/tmp/aimanager-evidence-template-pack}" --output-json-file "$${AIMANAGER_EVIDENCE_INTAKE_OUTPUT_JSON:-/tmp/aimanager-evidence-intake.json}" --output-markdown-file "$${AIMANAGER_EVIDENCE_INTAKE_OUTPUT_MARKDOWN:-/tmp/aimanager-evidence-intake.md}"

# Show info
info:
	@echo "UV: $(UV)"

# Installation targets
install-dev:
	$(UV) sync --frozen

install-proxy-dev:
	$(UV) sync --frozen --group proxy-dev --extra proxy

# CI-compatible installations (matches GitHub workflows exactly)
install-dev-ci:
	$(UV) sync --frozen

install-proxy-dev-ci:
	$(UV) sync --frozen --group proxy-dev --extra proxy

install-test-deps: install-proxy-dev
	$(UV) sync --frozen --all-groups --all-extras
	$(UV_RUN) prisma generate --schema litellm/proxy/schema.prisma

install-helm-unittest:
	helm plugin install https://github.com/helm-unittest/helm-unittest --version v0.4.4 || echo "ignore error if plugin exists"

# Install git hooks that enforce Conventional Commits and Conventional Branches.
# Opt-in: not chained into install-dev.
install-hooks:
	./scripts/install_git_hooks.sh

# Formatting
# Wrap width is ruff.toml's single source of truth (line-length = 120), shared by the
# formatter, E501, and the import sorter so there's no 88-vs-120 split to reconcile.
format: install-dev
	cd litellm && $(UV_RUN) ruff format --exclude '/enterprise/' . && cd ..

format-check: install-dev
	cd litellm && $(UV_RUN) ruff format --check --exclude '/enterprise/' . && cd ..

# Linting targets
lint-ruff: install-dev
	cd litellm && $(UV_RUN) ruff check . && cd ..

# faster linter for developing ...
# inspiration from:
# https://github.com/astral-sh/ruff/discussions/10977
# https://github.com/astral-sh/ruff/discussions/4049
lint-format-changed: install-dev
	@git diff origin/main --unified=0 --no-color -- '*.py' | \
	perl -ne '\
		if (/^diff --git a\/(.*) b\//) { $$file = $$1; } \
		if (/^@@ .* \+(\d+)(?:,(\d+))? @@/) { \
			$$start = $$1; $$count = $$2 || 1; $$end = $$start + $$count - 1; \
			print "$$file:$$start:1-$$end:999\n"; \
		}' | \
		while read range; do \
			file="$${range%%:*}"; \
			lines="$${range#*:}"; \
			echo "Formatting $$file (lines $$lines)"; \
			$(UV_RUN) ruff format --range "$$lines" "$$file"; \
		done

lint-ruff-dev: install-dev
	@tmpfile=$$(mktemp /tmp/ruff-dev.XXXXXX) && \
	cd litellm && \
	($(UV_RUN) ruff check . --output-format=pylint || true) > "$$tmpfile" && \
	$(UV_RUN) diff-quality --violations=pylint "$$tmpfile" --compare-branch=origin/main && \
	cd .. ; \
	rm -f "$$tmpfile"

lint-ruff-FULL-dev: install-dev
	@files=$$(git diff --name-only origin/main -- '*.py'); \
	if [ -n "$$files" ]; then echo "$$files" | xargs $(UV_RUN) ruff check; \
	else echo "No changed .py files to check."; fi

lint-basedpyright: install-dev
	git fetch origin litellm_internal_staging
	($(UV_RUN) basedpyright --outputjson || true) | $(UV_RUN) python scripts/type_check_gate.py --base origin/litellm_internal_staging

lint-basedpyright-budget-update: install-dev
	($(UV_RUN) basedpyright --outputjson || true) | $(UV_RUN) python scripts/type_check_gate.py --update

lint-format: format-check

lint-ruff-budget: install-dev
	$(UV_RUN) python scripts/ruff_strict_gate.py

# Strict gate, invoked the same way CI does in test-linting.yml so a local pass
# means the CI check will pass too.
lint-gate: install-dev
	git fetch origin litellm_internal_staging
	$(UV_RUN) python scripts/ruff_strict_gate.py --base origin/litellm_internal_staging

lint-ruff-budget-update: install-dev
	$(UV_RUN) python scripts/ruff_strict_gate.py --update

# Ratchet all budgets in one shot (ruff strict + basedpyright)
lint-budget-update: lint-ruff-budget-update lint-basedpyright-budget-update

check-circular-imports: install-dev
	cd litellm && $(UV_RUN) python ../tests/documentation_tests/test_circular_imports.py && cd ..

check-import-safety: install-dev
	@$(UV_RUN) python -c "from litellm import *; print('[from litellm import *] OK! no issues!');" || (echo '🚨 import failed, this means you introduced unprotected imports! 🚨'; exit 1)

# Combined linting (matches test-linting.yml workflow)
lint: format-check lint-ruff lint-basedpyright check-circular-imports check-import-safety lint-ruff-budget

# Faster linting for local development (only checks changed code)
lint-dev: lint-format-changed check-circular-imports check-import-safety

# Testing targets
test: install-test-deps
	$(UV_RUN) pytest tests/

test-unit: install-test-deps
	$(UV_RUN) pytest tests/test_litellm -x -vv -n 4

# Matrix test targets (matching CI workflow groups)
test-unit-llms: install-test-deps
	$(UV_RUN) pytest tests/test_litellm/llms --tb=short -vv -n 4 --durations=20

test-unit-proxy-guardrails: install-test-deps
	$(UV_RUN) pytest tests/test_litellm/proxy/guardrails tests/test_litellm/proxy/management_endpoints tests/test_litellm/proxy/management_helpers --tb=short -vv -n 4 --durations=20

test-unit-proxy-core: install-test-deps
	$(UV_RUN) pytest tests/test_litellm/proxy/auth tests/test_litellm/proxy/client tests/test_litellm/proxy/db tests/test_litellm/proxy/hooks tests/test_litellm/proxy/policy_engine --tb=short -vv -n 4 --durations=20

test-unit-proxy-misc: install-test-deps
	$(UV_RUN) pytest tests/test_litellm/proxy/_experimental tests/test_litellm/proxy/agent_endpoints tests/test_litellm/proxy/anthropic_endpoints tests/test_litellm/proxy/common_utils tests/test_litellm/proxy/discovery_endpoints tests/test_litellm/proxy/experimental tests/test_litellm/proxy/google_endpoints tests/test_litellm/proxy/health_endpoints tests/test_litellm/proxy/image_endpoints tests/test_litellm/proxy/middleware tests/test_litellm/proxy/openai_files_endpoint tests/test_litellm/proxy/pass_through_endpoints tests/test_litellm/proxy/prompts tests/test_litellm/proxy/public_endpoints tests/test_litellm/proxy/response_api_endpoints tests/test_litellm/proxy/shutdown tests/test_litellm/proxy/spend_tracking tests/test_litellm/proxy/ui_crud_endpoints tests/test_litellm/proxy/vector_store_endpoints tests/test_litellm/proxy/test_*.py --tb=short -vv -n 4 --durations=20

test-unit-integrations: install-test-deps
	$(UV_RUN) pytest tests/test_litellm/integrations --tb=short -vv -n 4 --durations=20

test-unit-core-utils: install-test-deps
	$(UV_RUN) pytest tests/test_litellm/litellm_core_utils --tb=short -vv -n 2 --durations=20

test-unit-other: install-test-deps
	$(UV_RUN) pytest tests/test_litellm/caching tests/test_litellm/responses tests/test_litellm/secret_managers tests/test_litellm/vector_stores tests/test_litellm/a2a_protocol tests/test_litellm/anthropic_interface tests/test_litellm/completion_extras tests/test_litellm/containers tests/test_litellm/enterprise tests/test_litellm/experimental_mcp_client tests/test_litellm/google_genai tests/test_litellm/images tests/test_litellm/interactions tests/test_litellm/passthrough tests/test_litellm/router_strategy tests/test_litellm/router_utils tests/test_litellm/types --tb=short -vv -n 4 --durations=20

test-unit-root: install-test-deps
	$(UV_RUN) pytest tests/test_litellm/test_*.py --tb=short -vv -n 4 --durations=20

# Proxy unit tests (tests/proxy_unit_tests split alphabetically)
test-proxy-unit-a: install-test-deps
	$(UV_RUN) pytest tests/proxy_unit_tests/test_[a-o]*.py --tb=short -vv -n 2 --durations=20

test-proxy-unit-b: install-test-deps
	$(UV_RUN) pytest tests/proxy_unit_tests/test_[p-z]*.py --tb=short -vv -n 2 --durations=20

test-integration: install-test-deps
	$(UV_RUN) pytest tests/ -k "not test_litellm"

test-unit-helm: install-helm-unittest
	helm unittest -f 'tests/*.yaml' deploy/charts/litellm-helm

# LLM Translation testing targets
test-llm-translation: install-test-deps
	@echo "Running LLM translation tests..."
	@python .github/workflows/run_llm_translation_tests.py

test-llm-translation-single: install-test-deps
	@echo "Running single LLM translation test file..."
	@if [ -z "$(FILE)" ]; then echo "Usage: make test-llm-translation-single FILE=test_filename.py"; exit 1; fi
	@mkdir -p test-results
	$(UV_RUN) pytest tests/llm_translation/$(FILE) \
		--junitxml=test-results/junit.xml \
		-v --tb=short --maxfail=100 --timeout=300

test-llm-translation-flush-vcr-cache:
	$(UV_RUN) python tests/_flush_vcr_cache.py

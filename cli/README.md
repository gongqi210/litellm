# AiManager CLI Contract

AiManager is an overlay on a LiteLLM fork, so the project-level CLI contract is represented by:

- `python3 cli/main.py --help`
- `python3 cli/main.py --json`
- `make policy-check`

The detailed operational commands stay in `aimanager/scripts/` and are listed in `cli/aimanager_cli_manifest.json`. Production UI or non-technical entry work should not bypass these command contracts; it must reuse the same policy, readiness, finance, observability, and lightweight-entry checks.

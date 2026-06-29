# AiManager Acceptance Criteria

## PASS Criteria

- `aimanager/config.yaml` contains only ycapi-backed upstream model entries.
- `aimanager/scripts/validate_config.py aimanager/config.yaml` passes.
- `aimanager/tests/test_config.py` passes.
- `docker compose -f aimanager/docker-compose.yml config` renders a valid Compose file.
- No direct provider secret names are required by `aimanager/.env.example`.
- `ycapi-video-1` is documented as deferred instead of being falsely exposed.

## BLOCKED Criteria

- Live ycapi smoke tests are blocked until a real `YCAPI_API_TOKEN` and `LITELLM_MASTER_KEY` are populated locally.

## Required Manual Production Checks

- Set non-zero AiManager resale prices before using spend budgets as the source of truth.
- Size the ycapi token's rate/spend limits for aggregate AiManager traffic.
- Confirm employees receive only LiteLLM virtual keys, never the ycapi upstream token.

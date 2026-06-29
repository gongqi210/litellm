# AiManager Agent Instructions

- Read `CLAUDE.md` for upstream LiteLLM coding guidelines.
- Read `项目知识图谱.md` before project edits, and update it after meaningful project-file changes.
- This project keeps LiteLLM management capabilities while routing configured upstream model calls through ycapi.
- Do not add direct upstream provider credentials or bases by default. Use `YCAPI_BASE_URL` and `YCAPI_API_TOKEN`.
- Keep secrets out of git. Use `aimanager/.env` or deployment secret managers.
- Validate AiManager config with `aimanager/scripts/validate_config.py` before completion.

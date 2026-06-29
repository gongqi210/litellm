# AiManager Entry

AiManager is the ycapi-backed company management deployment for this LiteLLM fork.

- Deployment overlay: `aimanager/`
- Project knowledge graph: `项目知识图谱.md`
- Runtime invariant: all configured upstream model calls use `YCAPI_BASE_URL` and `YCAPI_API_TOKEN`
- Management plane: keep full LiteLLM proxy/admin UI for virtual keys, teams, budgets, limits, and usage logs

Start with `aimanager/README.md` for local deployment and validation commands.

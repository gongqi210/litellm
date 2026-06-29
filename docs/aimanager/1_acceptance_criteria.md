# AiManager Acceptance Criteria v3

日期：2026-06-30

## 状态口径

- `PASS`：已真实运行或检查通过。
- `FAIL`：已真实运行或检查失败。
- `BLOCKED`：由于环境、权限、依赖或密钥不足无法验证。
- `SKIP`：明确不适用，并说明理由。

## M1 必须验收项

| 编号 | 验收项 | 验证方式 | 通过标准 |
| --- | --- | --- | --- |
| AC-01 | 静态配置 ycapi-only | `validate_config.py` + pytest | 禁止直连 provider key/base、`openai/*`、误配 video |
| AC-02 | 运行时 passthrough 封堵 | curl `/anthropic/`、`/gemini/`、`/bedrock/`、`/vertex_ai/`、`/openai/` 等路径 | 全部返回 403/404，且不产生上游调用 |
| AC-03 | `/v1/models` 白名单 | live 或 mock proxy | 只展示 `gemini-2.5-flash`、`deepseek-chat`、`ycapi-image-1` |
| AC-04 | Chat 正向调用 | mock ycapi；有 token 时 live smoke | 请求经 AiManager 到 ycapi，生成 usage/spend/request id |
| AC-05 | Image 正向调用 | mock ycapi；有 token 时 live smoke | 支持 `ycapi-image-1`，记录图片费用和 request id |
| AC-06 | SDK 兼容 | curl、Python、Node 示例 | base URL、model、virtual key 可跑通承诺能力 |
| AC-07 | 错误契约 | mock 401/403/404/429/5xx | 返回 OpenAI-compatible error，不泄露密钥 |
| AC-08 | Key 创建元数据 | Admin/API 检查 | key 绑定员工、部门、项目、成本中心、场景、预算、限流、有效期 |
| AC-09 | 非零计价 | 配置/数据库/调用检查 | M1 财务验收不得使用全 0 价格 |
| AC-10 | 预算阻断 | 小预算 + mock 调用 | 达阈值后请求被拒绝或降级，记录原因 |
| AC-11 | key 冻结/撤销 | 创建后冻结/撤销再调用 | 请求被拒绝，审计记录包含处置人和原因 |
| AC-12 | usage/spend 报表 | 查询或导出 | 可按日期、部门、项目、员工、成本中心、key、模型聚合 |
| AC-13 | 月度对账字段 | 导出样例检查 | 包含模型、token/图片数、金额、币种、部门、项目、key、差异字段 |
| AC-14 | RBAC 基础 | 不同角色登录或 mock 权限 | 高危操作仅管理员可用，财务/总经理/审计只读范围正确 |
| AC-15 | 管理面访问控制 | 部署配置检查 + `smoke_admin_boundary` 生产 preflight | Admin UI 不暴露到不受控公网入口，业务 URL 无法因伪造 trusted header 解锁管理路由，公开管理 URL 必须不可达、边缘阻断或只重定向到允许的 SSO |
| AC-16 | 可观测性 | 日志/metrics 检查 | request id、失败率、429/5xx、预算阻断、passthrough 拦截可观测 |
| AC-17 | 故障场景 | mock ycapi 429/5xx、Postgres down、配置错误 | 返回可解释错误，不切直连供应商 |
| AC-18 | `.env.example` 安全 | secret scan | 仅占位变量，无真实密钥 |
| AC-19 | live smoke 口径 | 缺真实 `YCAPI_API_TOKEN` 时 | 必须标 `BLOCKED`，不得伪装 `PASS` |

## 当前 M1 本地证据

| 编号 | 当前状态 | 证据 | 说明 |
| --- | --- | --- | --- |
| AC-01 | PASS | `uv run --no-project --with pyyaml python -m aimanager.scripts.validate_config aimanager/config.yaml`；`pytest aimanager/tests/test_config.py` | 已拒绝供应商直连、passthrough 配置、零价格、NaN/inf/非数字价格、错误 image 计价键 |
| AC-02 | PASS | `pytest aimanager/tests/test_policy.py`；`python -m aimanager.scripts.smoke_blocked_routes --base-url http://localhost:4000` | policy 单测证明被封请求不会进入 downstream LiteLLM；运行中 proxy smoke 已覆盖 33 条 provider/native/config/cache/reload/model-write/uncommitted 路径，全部返回 AiManager 403 policy code，并产生审计事件；管理面 surface 也继续封堵 provider/native/config/cache/reload/model-write |
| AC-03 | PASS | 运行中 proxy：`GET /v1/models` | 已启动容器验证响应只包含 `gemini-2.5-flash`、`deepseek-chat`、`ycapi-image-1` |
| AC-04 | PASS | `python -m aimanager.scripts.mock_ycapi` + rebuilt `aimanager`/`aimanager-admin` + `python -m aimanager.scripts.smoke_spend_logs` | mock ycapi chat 经 business surface 调用成功，`LiteLLM_SpendLogs` 写入 `openai/gemini-2.5-flash`，`spend=3.3e-06` |
| AC-05 | PASS | `python -m aimanager.scripts.mock_ycapi` + rebuilt `aimanager`/`aimanager-admin` + `python -m aimanager.scripts.smoke_spend_logs` | `ycapi-image-1` 经 business surface 调用成功，`LiteLLM_SpendLogs` 写入 `openai/ycapi-image-1`，`spend=0.01`；entrypoint/ASGI 在 LiteLLM proxy 加载后注册 provider-prefixed image 单价，避免图片 spend 记 0 |
| AC-07 | PASS | `PYTHONPATH="$PWD" uv run --no-project --with pytest --with pyyaml pytest aimanager/tests/test_policy.py -q` | AiManager 自有 policy error 与下游 LiteLLM/ycapi 原生错误均返回 OpenAI-compatible `error` + 顶层 `request_id`；当下游带 `x-litellm-call-id` 时 body `request_id` 与 header 对齐；401/403/404/429/5xx 类型回退稳定；下游文本 5xx 被规范化为 JSON `server_error`/`upstream_error`，JSON error message 会脱敏 Bearer、`sk-*`、ycapi token 和 DSN 密码 |
| AC-08 | PASS | `pytest aimanager/tests/test_governance.py`；`pytest aimanager/tests/test_policy.py`；`pytest aimanager/tests/test_admin_ui_key_creation_smoke.py`；运行中 admin proxy：无 metadata 的 `POST /key/generate` 返回 400 `aimanager_key_governance_invalid`；有效 shared key 创建返回员工、团队、模型、预算、限流、有效期、部门、项目、成本中心、场景、审批人和 `metadata.enforced_params`，测试 key 已删除；真实 Chrome/Admin UI smoke：`python -m aimanager.scripts.smoke_admin_ui_key_creation` 返回 `PASS`，`reject_status=400`，`accept_status=200` | 已实现 key request 元数据、预算、限流、有效期、审批人和 shared key `enforced_params` 校验，并已接入 management surface `POST /key/generate` ASGI 前置校验；缺治理元数据不会进入 LiteLLM，UI-originated key 创建在缺 metadata 时 fail-closed，补齐治理 metadata、预算、限流、有效期、team/model/key alias 后可创建 disposable key 并自动删除；Admin UI 数字字符串会在 governance 层规范化为数值；`metadata.shared_key=true` 会写入 LiteLLM 实际 enforcement 读取的 `metadata.enforced_params`；当前 LiteLLM fork 的 inference-time `enforced_params` 强制执行需要 Enterprise license，OSS 路径下 spend smoke 使用非 shared employee key |
| AC-09 | PASS | `validate_config.py` + `pytest aimanager/tests/test_config.py` + `python -m aimanager.scripts.smoke_spend_logs` | 配置层禁止零计价和错误 image 键；运行态 chat/image `LiteLLM_SpendLogs.spend` 均为非零，当前 M1 技术转售价 chat `3.3e-06`、image `0.01` |
| AC-10 | PASS | `python -m aimanager.scripts.smoke_budget_block` + LiteLLM/AiManager container log | mock ycapi + rebuilt business/admin proxy 已验证：一次性治理 key `max_budget=0.005` 先经 image 成功消费 `0.01`，随后同 key 的 business chat 请求返回 HTTP 429，`error.type=budget_exceeded`，响应和容器日志均记录 `Budget has been exceeded! ... Current cost: 0.01, Max budget: 0.005`；AiManager 同时写出结构化 `aimanager_audit_event`，`event_type=budget_blocked`、`severity=high`、`reason=budget_exceeded`；`LiteLLM_VerificationToken.spend` 可因批量写延迟仍为 `0.0`，不作为阻断主证据 |
| AC-11 | PASS | `pytest aimanager/tests/test_audit.py`；`pytest aimanager/tests/test_policy.py`；`python -m aimanager.scripts.smoke_key_lifecycle` + admin/business container logs | 管理面 `POST /key/block` 和 `POST /key/delete` 必须携带 `x-aimanager-actor` 与 `x-aimanager-reason`/`x-aimanager-disposition-reason`，缺失时返回 400 `aimanager_key_lifecycle_invalid` 且不进入 LiteLLM；mock ycapi + rebuilt business/admin proxy 已验证：冻结 key 后 business chat 返回 HTTP 401，原因包含 `Key is blocked`；撤销 key 后 business chat 返回 HTTP 401，原因包含无效 token/not found；admin container 同时写出 `key_frozen` 和 `key_revoked` 结构化审计事件，包含处置人、原因、request id、key alias 和组织维度 |
| AC-12 | PASS | `pytest aimanager/tests/test_finance.py aimanager/tests/test_finance_export_script.py` | 已实现 LiteLLM `SpendLogs` 形态归一化，支持 `startTime`、`call_type`、`openai/` provider 前缀和嵌套 `metadata.user_api_key_metadata`/`metadata.spend_logs_metadata`；`aimanager.scripts.export_finance` 可从 JSON/CSV 输入导出 `aimanager_usage_daily.csv`、`aimanager_finance_monthly.csv`、`aimanager_reconciliation.csv`，按日期/月度、部门、项目、员工、成本中心、key、模型、endpoint、币种和价格版本聚合；`unassigned` 成本会在月度导出中标记 `close_status=blocked_unassigned` |
| AC-13 | PASS | `pytest aimanager/tests/test_finance.py aimanager/tests/test_finance_export_script.py` | 已实现 ycapi 月账单 JSON/CSV 样例导入和字段归一化，支持 `billing_month`/`model_name`/`api_path`/token/image/amount/currency 字段；对账导出包含模型、token/图片数、AiManager 金额、ycapi 金额、币种、差异金额、差异率和 `matched`/`needs_review` 状态，差异阈值为 `max(10 CNY, 1%)`；真实生产 ycapi 月账单文件或接口仍需在月结前补充为外部证据 |
| AC-14 | PASS | `PYTHONPATH="$PWD" uv run --no-project --with pytest --with pyyaml pytest aimanager/tests/test_policy.py -q -k 'management_rbac'`；`pytest aimanager/tests/test_config.py::test_compose_exposes_management_surface_only_on_localhost_profile` | management surface 在 `AIMANAGER_RBAC_ENABLED=True` 时对高危写路径执行 RBAC 预检：`finance`/`ceo`/`audit`/`proxy_admin_viewer`、未知角色和缺角色访问 `/key`、`/team`、`/user`、`/budget` 等写路径返回 HTTP 403 `aimanager_rbac_denied` 并记录 `policy_blocked` 审计；`proxy_admin` 可继续进入 LiteLLM 管理流；财务/总经理/审计可访问 spend/activity 只读报表；role header 不能解锁 business surface 管理路由。本项为 mock/trusted-header 层证据，生产仍必须依赖 LiteLLM 原生认证授权和受控 SSO/反代剥离伪造 role header，归入 AC-15 部署边界验收 |
| AC-15 | BLOCKED | `docker compose -f aimanager/docker-compose.yml config`；`docker compose -f aimanager/docker-compose.yml --profile admin config`；`pytest aimanager/tests/test_config.py::test_compose_exposes_management_surface_only_on_localhost_profile`；`pytest aimanager/tests/test_policy.py`；`pytest aimanager/tests/test_admin_boundary_smoke.py`；运行中 proxy smoke；`python -m aimanager.scripts.smoke_admin_boundary --business-base-url <business-url> --public-admin-url <public-admin-url> --allowed-sso-redirect-host <sso-host> --require-business-base-url --require-public-admin-url` | 默认 `aimanager` 服务为 `AIMANAGER_ROUTE_SURFACE=business`，业务端口 4000 封堵 LiteLLM 管理路由；`aimanager-admin` 仅在 `admin` profile 渲染，`AIMANAGER_ROUTE_SURFACE=management` 且只绑定 `127.0.0.1:4001`；真实容器启动、Prisma migration/lifespan、health、`/v1/models`、业务面 `/ui` 403、管理面 `/ui` 307 和管理面 `/config/field/update` 403 已验证；新增 `smoke_admin_boundary` 可在生产检查业务 URL 伪造 `x-aimanager-role=proxy_admin` 时仍无法访问 `/ui`、`/key/generate`、`/v2/key/info`、`/metrics`、`/config/field/update` 等管理/配置路由，且不会发送 `Authorization`；同时检查公开管理 URL 不可达、被 401/403/404 边缘阻断或只重定向到 allowlisted SSO；真实生产 VPN/反代/公网暴露边界仍需用生产 URL 跑出全 PASS 后解除 BLOCKED |
| AC-16 | PASS | `pytest aimanager/tests/test_audit.py aimanager/tests/test_policy.py aimanager/tests/test_observability.py aimanager/tests/test_observability_export_script.py`；运行中 business proxy log：policy block `aimanager_audit_event`；admin proxy log：`/config/field/update` policy event；运行中 key governance smoke 日志：`request_id=runtime-key-governance-smoke` 对应 `policy_blocked`，secret-like 检查无 Authorization/Bearer/本地占位 token；`python -m aimanager.scripts.smoke_budget_block` 触发 LiteLLM 429 budget log 和 AiManager `budget_blocked` 结构化审计事件；`python -m aimanager.scripts.smoke_key_lifecycle` 触发 `key_frozen`/`key_revoked` 结构化审计事件；`PYTHONPATH="$PWD" uv run --no-project python -m aimanager.scripts.export_observability --help`；`LITELLM_LOCAL_MODEL_COST_MAP=True` 入口保护已验证 | passthrough/config/cache/reload/model-write/default-denied policy block 已有运行时结构化审计日志；key 创建治理拒绝会记录 `policy_blocked` 且不记录请求密钥；预算阻断已有 429 响应、LiteLLM 日志和 AiManager `budget_blocked` 结构化审计事件；key 冻结/撤销已有管理面 lifecycle 审计 emitters；management-only `GET /metrics` 由 AiManager 直接输出低基数 `aimanager_audit_events_total` 与 `aimanager_http_responses_total`，business `/metrics` 仍按 policy 阻断；`export_observability` 可从审计日志和 request-status JSON/CSV 生成 failure rate、429/5xx、预算阻断、passthrough 拦截、缺 request id 等机器可读 alert 记录；生产 Alertmanager/WeCom 路由仍属于部署配置项 |
| AC-17 | PASS | `PYTHONPATH="$PWD" uv run --no-project --with pytest --with pyyaml pytest aimanager/tests/test_policy.py -q -k 'database_ready_guard'`；`pytest aimanager/tests/test_postgres_down_smoke.py`；隔离 compose project `aimanager_postgres_down` + `python -m aimanager.scripts.smoke_postgres_down` | AiManager 在 `AIMANAGER_DATABASE_READY_CHECK_ENABLED=True` 时对 readiness、业务调用和管理允许路由执行 Postgres 协议握手式 DB-ready 预检；真实容器运行中停止 Postgres 后，`/health/readiness` 返回 503，provider passthrough 仍由 AiManager 返回 403 `aimanager_passthrough_blocked`，员工 virtual key 的 business chat 返回 503 `aimanager_database_unavailable`，不进入 downstream LiteLLM/ycapi，不回显 Bearer、`sk-*`、ycapi token 或 DSN 密码；启动期外网/TLS 异常仍由本地 model cost map 保护 |
| AC-18 | PASS | `pytest aimanager/tests/test_config.py::test_env_example_uses_non_secret_placeholders`；`rg` secret-like 扫描 | `.env.example` 只保留非密钥占位符，未命中 `sk-`、`AKIA`、`AIza`、private key 等模式 |

上表中的 `BLOCKED` 项不是失败，而是完整 AC 还缺生产管理面暴露边界或真实 live ycapi 证据。M1 试点前必须解除这些 `BLOCKED` 项；生产告警路由需在部署侧把 management metrics 或 observability JSON report 接入既定告警渠道。

## M2 业务试点验收项

| 编号 | 验收项 | 通过标准 |
| --- | --- | --- |
| AC-20 | 市场场景标签 | 内容生成必须记录一级/二级场景、内用/外发、渠道、项目/客户、敏感级别 |
| AC-21 | 市场内容流程 | brief、生成草稿、人工审核、定稿、外发、归档、复盘闭环可追踪 |
| AC-22 | 品牌安全 | 品牌语气、禁用承诺、事实核查、版权和人物肖像规则可见并留痕 |
| AC-23 | 非技术入口 | 市场或业务人员不依赖 SDK，能在 5 分钟内完成一次合规试用 |
| AC-24 | 经营总览 | 总经理可查看月费用、预算消耗、异常事件、TOP 部门/项目/key |
| AC-25 | 月结与调整 | 财务可完成关账、补记、冲销、归属调整和差异处理记录 |
| AC-26 | 员工监控制度 | 非工作时间异常、key 共享检测等规则完成内部告知和权限边界确认 |

## Required Manual Production Checks

- 明确 showback 或 chargeback 政策。
- 设置非零 AiManager 转售价并记录审批人、生效时间和价格版本。
- Size ycapi token 的 rate/spend limits，避免单 token 限额影响全部员工。
- 确认员工只接收 LiteLLM virtual key，永不接触 ycapi token。
- 确认涉及个人信息、客户数据、商业秘密和跨境模型的数据使用边界。

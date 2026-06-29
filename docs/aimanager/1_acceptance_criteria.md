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
| AC-15 | 管理面访问控制 | 部署配置检查 | Admin UI 不暴露到不受控公网入口 |
| AC-16 | 可观测性 | 日志/metrics 检查 | request id、失败率、429/5xx、预算阻断、passthrough 拦截可观测 |
| AC-17 | 故障场景 | mock ycapi 429/5xx、Postgres down、配置错误 | 返回可解释错误，不切直连供应商 |
| AC-18 | `.env.example` 安全 | secret scan | 仅占位变量，无真实密钥 |
| AC-19 | live smoke 口径 | 缺真实 `YCAPI_API_TOKEN` 时 | 必须标 `BLOCKED`，不得伪装 `PASS` |

## 当前 M1 本地证据

| 编号 | 当前状态 | 证据 | 说明 |
| --- | --- | --- | --- |
| AC-01 | PASS | `uv run --no-project --with pyyaml python -m aimanager.scripts.validate_config aimanager/config.yaml`；`pytest aimanager/tests/test_config.py` | 已拒绝供应商直连、passthrough 配置、零价格、NaN/inf/非数字价格、错误 image 计价键 |
| AC-02 | PASS | `pytest aimanager/tests/test_policy.py`；`python -m aimanager.scripts.smoke_blocked_routes --base-url http://localhost:4000` | policy 单测证明被封请求不会进入 downstream LiteLLM；运行中 proxy smoke 已覆盖 23 条 provider/native/config/model-write/uncommitted 路径，全部返回 AiManager 403 policy code，并产生审计事件 |
| AC-03 | PASS | 运行中 proxy：`GET /v1/models` | 已启动容器验证响应只包含 `gemini-2.5-flash`、`deepseek-chat`、`ycapi-image-1` |
| AC-05 | BLOCKED | `validate_config.py` + `pytest aimanager/tests/test_config.py` | `ycapi-image-1` 已改用 `input_cost_per_image > 0`；仍需 mock/live 调用证明 spend log 非零 |
| AC-07 | BLOCKED | `pytest aimanager/tests/test_policy.py` | AiManager 自有 policy error 已返回 OpenAI-compatible body + `request_id`；LiteLLM 原生错误包装仍需集成验证 |
| AC-08 | BLOCKED | `pytest aimanager/tests/test_governance.py` | 已实现 key request 元数据、预算、限流、有效期、审批人和 shared key `enforced_params` 纯校验；仍需接入 LiteLLM key 创建 API/UI |
| AC-09 | BLOCKED | `validate_config.py` + `pytest aimanager/tests/test_config.py` | 配置层已禁止零计价和错误 image 键；财务审批价与真实 spend 非零仍需后续验证 |
| AC-10 | BLOCKED | `pytest aimanager/tests/test_audit.py` | 已定义 `budget_blocked` 审计事件结构；仍需真实预算阈值触发和请求阻断验证 |
| AC-11 | BLOCKED | `pytest aimanager/tests/test_audit.py` | 已定义 `key_frozen`、`key_revoked` 审计事件结构；仍需接入 LiteLLM key 冻结/撤销操作并调用验证 |
| AC-12 | BLOCKED | `pytest aimanager/tests/test_finance.py` | 已实现 usage/spend 日/月聚合基础，可按日期、部门、项目、员工、成本中心、key、模型、endpoint、币种和价格版本聚合；仍需接入 LiteLLM `SpendLogs` 和导出任务 |
| AC-13 | BLOCKED | `pytest aimanager/tests/test_finance.py` | 已实现月度金额字段和 AiManager-vs-ycapi 差异对账基础，采用 `max(10 CNY, 1%)` 物料差异阈值；仍需真实 ycapi 账单导入 |
| AC-15 | BLOCKED | `docker compose -f aimanager/docker-compose.yml config`；`pytest aimanager/tests/test_litellm_entrypoint.py`；运行中 proxy smoke | compose 已使用 `build.target: runtime` 和 `python -m aimanager.litellm_entrypoint --config=/app/config.yaml`，保留 LiteLLM CLI 初始化并将最终 uvicorn app 替换为 `aimanager.asgi:app`；入口已要求 `LITELLM_MASTER_KEY`、`YCAPI_BASE_URL`、`YCAPI_API_TOKEN` 非空且 compose 已显式透传；真实容器启动、Prisma migration/lifespan、health 和 `/v1/models` 已验证；生产 Admin UI 网络暴露边界仍未验收 |
| AC-16 | BLOCKED | `pytest aimanager/tests/test_audit.py`；运行中 proxy log：23 条 `aimanager_audit_event`，secret-like 检查无 Authorization/Bearer | passthrough/config/model-write/default-denied policy block 已有运行时结构化审计日志；预算阻断、key 冻结/撤销、metrics/告警仍需接入 |
| AC-17 | BLOCKED | `pytest aimanager/tests/test_policy.py` | policy 层证明不会切 provider passthrough；ycapi 429/5xx、DB down 还未跑 |
| AC-18 | PASS | `pytest aimanager/tests/test_config.py::test_env_example_uses_non_secret_placeholders`；`rg` secret-like 扫描 | `.env.example` 只保留非密钥占位符，未命中 `sk-`、`AKIA`、`AIza`、private key 等模式 |

上表中的 `BLOCKED` 项不是失败，而是完整 AC 还缺运行中 proxy、mock/live ycapi、spend log 数据源、预算阻断、RBAC、审计 emitters 或真实 ycapi 账单证据。M1 试点前必须解除这些 `BLOCKED` 项。

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

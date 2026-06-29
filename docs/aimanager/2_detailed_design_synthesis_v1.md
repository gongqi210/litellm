# AiManager 详细设计汇总 v1

日期：2026-06-29
阶段：阶段二前设计深化
当前 Agent：Codex 总指挥 / 架构终审
输入文档：

- `docs/aimanager/1_PRD_v3.md`
- `docs/aimanager/1_acceptance_criteria.md`
- `docs/aimanager/2_detailed_design_codex_v1.md`
- `docs/aimanager/2_detailed_design_claude_v1.md`
- `docs/aimanager/2_review_codex_on_claude_v1.md`
- `docs/aimanager/2_review_claude_on_codex_v1.md`

## 1. 最终结论

两份设计不是二选一。最终 M1 实施基线采用：

- Claude 版的源码锚点、`aimanager/asgi.py` 进程内 fail-closed 白名单中间件、Google 原生路由封堵、`input_cost_per_image` 图片计价修正、`enforced_params` 主体透传机制。
- Codex 版的治理结构、财务导出与月度对账、key 生命周期、RBAC/UI 边界、M1/M2/M3 分期和 AC 映射。

本汇总设计修正双方评审中发现的全部 P0/P1。M1 不能按任一单份设计直接开发，必须以本文为基线。

M1 的不可破坏约束：

1. 员工和内部系统只调用 AiManager 暴露的 OpenAI-compatible 入口。
2. AiManager 只通过 `YCAPI_BASE_URL` + `YCAPI_API_TOKEN` 出站调用 ycapi。
3. LiteLLM 的 Admin UI、virtual keys、teams、budgets、rate limits、usage/spend logs 继续保留。
4. provider passthrough、Google 原生 Gemini 路由、模型写入、运行时 config 更新在 M1 默认不可达。
5. chat/image 都必须有非零且可追溯的 ycapi 转售价；验收看真实 spend 落库，不能只看配置字段。

## 2. 系统形态

### 2.1 推荐形态

M1 使用完整 LiteLLM proxy 作为管理与调用底座，在进程内包一层 AiManager ASGI 应用：

```text
OpenAI SDK / curl / internal systems
        |
        v
Edge Guard / ingress allowlist
        |
        v
aimanager.asgi:YcapiOnlyAllowlistMiddleware
        |
        v
litellm.proxy.proxy_server.app
        |
        v
ycapi OpenAI-compatible endpoint
```

原因：

- 完整 proxy 保留 LiteLLM 管理面、key、team、budget、spend log 能力。
- `gateway/` data-plane-only 不满足管理诉求。
- fork 深删 passthrough 会增加升级成本，且需要持续追 LiteLLM 路由变化。
- fail-closed 白名单的失效方向是“漏配即拒绝”，符合 ycapi-only 的安全目标。

### 2.2 四层控制

| 层 | 控制点 | 目标 | 验收口径 |
| --- | --- | --- | --- |
| L0 静态配置 | `aimanager/config.yaml` + `validate_config.py` | 只配置 ycapi 模型、非零计价、安全 general settings | CI 与 pytest 失败即阻断 |
| L1 边缘入口 | Nginx/Traefik/网关规则 | 不让公网或旁路入口暴露危险路径 | curl 路径矩阵 |
| L2 进程内白名单 | `aimanager/asgi.py` | 任何 LiteLLM 新增路由默认拒绝 | 单测 + 集成 + 无 outbound 断言 |
| L3 LiteLLM 原生治理 | key/team/budget/RBAC/spend | 保留管理能力和预算阻断 | Admin/API smoke + spend 落库 |

L2 是主安全边界。L1 是加固，不是主判断。禁止把 provider denylist 作为唯一运行时边界。

## 3. M1 路由基线

### 3.1 业务 API 白名单

M1 只承诺以下业务 API：

| Method | Path | 说明 |
| --- | --- | --- |
| `GET` | `/v1/models` | 只返回 `gemini-2.5-flash`、`deepseek-chat`、`ycapi-image-1` |
| `POST` | `/v1/chat/completions` | chat/vision，通过 ycapi OpenAI-compatible |
| `POST` | `/v1/images/generations` | 文生图，通过 ycapi OpenAI-compatible |

不放行 `/v1/completions`、`/v1/embeddings`、无 `/v1` 前缀的兼容路径、img2img、video、ycapi 专有 `/v1/usage`、provider-specific API。后续要开放任一新 endpoint，必须补 PRD、模型清单、计价、SDK 示例和 AC。

### 3.2 管理与健康路由

M1 管理面保留但受控：

- 健康检查：只放行部署探针需要的 health/readiness/liveness 路径，并用路由清单测试锁定。
- Admin UI：只在内网/VPN/受控入口开放。
- key/team/budget/spend/user 管理：按角色放行，且不得包含模型写入或 runtime config 写入。
- spend/report 导出：只读和导出能力对财务、总经理、审计只读可见，但必须脱敏个人明细。

实现时需要生成 LiteLLM 路由清单，白名单按 method + path 精确匹配，不使用宽泛前缀放行管理 API。

### 3.3 必须显式拒绝的路径

以下路径或行为在 M1 统一返回 403，error code 为 `aimanager_policy_blocked` 或更具体的稳定 code：

| 类别 | 示例 | 原因 |
| --- | --- | --- |
| provider passthrough | `/anthropic/*`、`/gemini/*`、`/bedrock/*`、`/openai/*`、`/cohere/*`、`/vllm/*`、`/mistral/*`、`/azure/*`、`/azure_ai/*`、`/watsonx/*`、`/cursor/*` | 防止绕过 ycapi |
| Google 原生路由 | `/v1beta/models/*:generateContent`、`/models/*:generateContent`、`:streamGenerateContent` | `google_router` 在 LiteLLM proxy 中独立挂载 |
| Vertex discovery | `/vertex_ai/discovery/*` | 存在无鉴权 passthrough 风险 |
| 动态 passthrough | `/pass-through-endpoints` 与配置/DB 中的 `pass_through_endpoints` | 防止后门上游 |
| runtime config | `/config/update` | 防止运行期翻转 `store_model_in_db` 或新增危险 general settings |
| 模型写入 | `POST /model/new`、`PATCH/POST /model/update`、`DELETE /model/*` | 模型清单只能走 Git 配置变更 |
| 未承诺业务 API | `/v1/embeddings`、`/v1/completions`、非 `/v1` 兼容路径 | 没有计价、验收和 SDK 契约 |

验收不仅看 403/404，还要证明被拒请求没有出站到 ycapi 或任何供应商 mock。

## 4. ycapi-only 配置与漂移控制

### 4.1 配置清单

`aimanager/config.yaml` 必须满足：

- `model_list` 只包含 `gemini-2.5-flash`、`deepseek-chat`、`ycapi-image-1`。
- 每个 deployment 的 `api_base` 只允许 `os.environ/YCAPI_BASE_URL` 或默认 `https://ycapi.ycaicloud.com/v1`。
- 每个 deployment 的 `api_key` 只允许 `os.environ/YCAPI_API_TOKEN`。
- 禁止 OpenAI、Anthropic、Gemini、Azure、Bedrock、Vertex 等供应商直连 key/env/base URL。
- 禁止 `ycapi-video-1` 进入 LiteLLM `model_list`，除非先实现异步 video adapter 与独立验收。
- `general_settings.store_model_in_db=false`。
- `general_settings.pass_through_endpoints` 不存在或为空。
- 默认不保存 prompt/response 正文。

### 4.2 运行时漂移

M1 必须在启动和健康检查中验证：

- `store_model_in_db` 运行时最终值仍为 false。
- DB 中没有已启用的非 ycapi 模型。
- DB/config 中没有 passthrough endpoint。
- 管理 API 没有暴露 `/config/update` 和模型写路径。
- 运行环境没有供应商直连 key。

任一漂移命中，health 返回 FAIL，阻止上线或摘流。

模型变更流程只允许：

1. 修改 Git 中的 `aimanager/config.yaml`。
2. 更新价格版本和验收用例。
3. 运行静态校验、单测、route allowlist 回归、mock/live smoke。
4. 通过评审后发布。

## 5. 计价、预算与 spend

### 5.1 价格字段决议

chat 模型必须显式配置：

- `input_cost_per_token > 0`
- `output_cost_per_token > 0`

`gemini-2.5-flash` 与 LiteLLM 内置价格表同名，缺少自定义价或字段写错时可能静默继承内置供应商价格。M1 validator 必须强制 AiManager 模型都有 ycapi 转售价，不能依赖内置价格表兜底。

image 模型必须显式配置：

- `input_cost_per_image > 0`

LiteLLM 的 image cost calculator 读取 `input_cost_per_image` 或 `input_cost_per_pixel`，不读取 `output_cost_per_image`。因此 `ycapi-image-1` 禁止使用 `output_cost_per_image` 作为有效计价字段，也不能保留 0.0 图片价格。

### 5.2 价格版本

每个价格版本至少包含：

| 字段 | 说明 |
| --- | --- |
| `pricing_version` | 例如 `aimanager-2026-06-m1` |
| `model_name` | 对外模型名 |
| `endpoint_type` | chat / image |
| `unit` | input token / output token / image |
| `unit_price` | ycapi 转售价，非零 |
| `currency` | 默认 CNY |
| `tax_mode` | 含税/未税 |
| `effective_at` | 生效时间 |
| `approved_by` | 财务或授权审批人 |
| `rounding` | 金额四舍五入规则 |
| `failed_request_billing` | 失败请求已消耗资源时是否计费 |

技术 validator 只证明字段存在、键名正确、数值非零、模型覆盖完整。价格是否等于财务审批转售价，由价格版本登记和财务验收证明。

### 5.3 预算阻断

M1 使用 LiteLLM 原生预算机制：

- key 硬预算与多窗口预算。
- team/project 预算。
- global proxy 预算。
- RPM/TPM 限流。
- soft budget 只告警，不阻断。

预算检查必须发生在上游调用前。超额返回 429，并记录 key、team、预算类型、阈值、当前 spend、request_id。

验收方式：

- 用极小预算 key 触发 429。
- 证明被拒请求没有出站到 ycapi。
- 证明预算阻断事件可在日志或报表中检索。

### 5.4 spend 与对账

成功调用必须写入 `LiteLLM_SpendLogs.spend`，并包含：

- request id
- key hash / key alias
- user/team/project/department/cost center
- model、endpoint、call type
- token 数或 image count
- spend、currency、pricing_version
- status code、error type、latency
- scenario_l1/scenario_l2

AC-05 和 AC-09 的关键断言是“chat/image 调用真实写入非零 spend”，不是“配置里写了一个非零数字”。

M1 导出文件：

| 文件 | 粒度 | 用途 |
| --- | --- | --- |
| `aimanager_usage_daily.csv` | 日期 + 部门 + 项目 + key + 模型 | 日常运营 |
| `aimanager_finance_monthly.csv` | 月份 + 成本中心 + 项目 + 模型 | 财务归集 |
| `aimanager_reconciliation.csv` | AiManager vs ycapi 账单 | 差异处理 |
| `aimanager_risk_events.csv` | 预算阻断、passthrough 拦截、异常 key | 管理复盘 |

`unassigned` 成本不得关账，必须补齐部门、项目、成本中心或记录财务处理结论。

## 6. Key 生命周期、主体透传与审计

### 6.1 Key 创建约束

创建 key 必填：

- 所有人 / 申请人
- 部门、项目、成本中心
- 业务场景一级/二级标签
- 允许模型范围
- 预算、RPM、TPM
- 有效期
- 审批人
- 内部/外部使用属性

AiManager 在 key 创建入口前置 Pydantic 校验。裸 LiteLLM key 创建能力不能绕过这些字段。

### 6.2 状态机

| 状态 | LiteLLM 载体 | 行为 |
| --- | --- | --- |
| `active` | 默认有效 key | 可调用授权模型 |
| `rotating` | 新旧 key 并存窗口 | 旧 key 到期自动失效 |
| `frozen` | `blocked=true` | 调用被拒，可恢复 |
| `revoked` | 删除 key | 调用 401，不可恢复 |
| `expired` | `expires < now` | 调用 401/403 |
| `budget_limited` | 预算命中 | 调用 429 |

冻结、撤销、解冻、降额、提额、轮换都必须审计。

### 6.3 共享 key 主体透传

共享 key 必须强制调用方传最终主体。实现采用 LiteLLM 原生 `enforced_params`，不自研重复校验。

共享 key metadata 示例：

```json
{
  "enforced_params": [
    "user",
    "metadata.scenario_l1",
    "metadata.end_user_principal"
  ]
}
```

缺少最终主体、业务场景或 request metadata 的请求直接拒绝。无法稳定透传最终主体的内部系统，不进入公司级推广。

### 6.4 审计落点

M1 审计落点：

- who / when / action：复用 LiteLLM 对象审计日志能力。
- 处置原因、备注、审批来源：写入 key metadata，例如 `disposition_reason`、`approval_ref`。
- 高风险事件：M1 可先导出 `aimanager_risk_events.csv`，M2 再建专用扩展表。

AC-11 需要证明冻结/撤销后再次调用被拒，且审计记录能看到处置人和原因。

## 7. API、SDK 与错误契约

### 7.1 SDK 兼容

M1 支持：

- curl。
- Python OpenAI SDK，`base_url="${AIMANAGER_BASE_URL}/v1"`。
- Node OpenAI SDK，`base_url="${AIMANAGER_BASE_URL}/v1"`。
- chat：`gemini-2.5-flash`、`deepseek-chat`。
- image：`ycapi-image-1`，优先 `response_format=b64_json`。
- vision：只承诺 base64 `data:` URL。

streaming、tool calls、JSON mode、复杂 `response_format` 只有在 mock/live smoke 均通过后才标记支持。

### 7.2 错误响应决议

M1 采用方案 B：AiManager 中间件或响应包装层给自有拦截错误和 LiteLLM 错误响应补顶层 `request_id`，同时保留 `x-litellm-call-id` 响应头。

统一错误体：

```json
{
  "error": {
    "message": "sanitized message",
    "type": "authentication_error | permission_error | invalid_request_error | rate_limit_error | upstream_error",
    "param": null,
    "code": "stable_error_code"
  },
  "request_id": "req_or_litellm_call_id"
}
```

实现要求：

- 自有白名单拦截错误生成 request id 并写日志。
- LiteLLM 已产生 `x-litellm-call-id` 时，body 中 `request_id` 与该 header 对齐。
- 下游返回 JSON error 时保留 `message`、`type`、`param`、`code`；下游返回非 JSON 4xx/5xx 时规范化为 OpenAI-compatible JSON。
- 不泄露 token、数据库密码、供应商 key、DSN 密码、完整 prompt；文本错误默认使用通用 upstream message，JSON 字段做敏感串脱敏。
- streaming 中的预调用错误也必须符合该错误体；streaming 已开始后的上游中断按 SDK 可处理错误记录日志和 spend/failure。

AC-07 断言 body `request_id` 与 header 关联一致，且 401/403/404/429/5xx 类型稳定。

## 8. RBAC 与 UI 边界

路径白名单解决“系统暴露哪些能力”，RBAC 解决“谁能做什么”。两者都必须存在。

| 能力/页面 | 超级管理员 | 系统管理员 | 总经理 | 财务 | 部门管理员 | 开发者 | 普通员工 | 审计只读 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| key 创建/冻结/撤销/轮换 | 全部 | 全部，受审批约束 | 只读 | 只读 | 本部门申请/冻结建议 | 自己项目申请 | 只看自己状态 | 只读 |
| team/project/budget 管理 | 全部 | 全部，受审批约束 | 只读 | 预算只读/导出 | 本部门只读/申请 | 无 | 无 | 只读 |
| spend 明细 | 全部 | 全部 | 汇总和必要下钻 | 财务维度全量 | 本部门 | 自己项目 | 自己 | 脱敏只读 |
| 财务导出/对账 | 全部 | 技术导出 | 汇总查看 | 全部 | 本部门查看 | 无 | 无 | 只读 |
| 模型清单写入 | 禁止 UI 写入，走 Git 流程 | 禁止 UI 写入，走 Git 流程 | 无 | 无 | 无 | 无 | 无 | 无 |
| runtime config 更新 | 禁止 | 禁止 | 无 | 无 | 无 | 无 | 无 | 无 |
| prompt 保存开关 | 高危审批后才可改 | 高危审批后才可改 | 只读 | 只读 | 无 | 无 | 无 | 只读 |
| 风险事件 | 全部 | 全部 | 汇总查看 | 费用相关 | 本部门 | 自己项目 | 无 | 全部只读 |

M1 Admin UI 不是业务门户。总经理、市场、财务、部门管理员的业务可读性通过轻量经营总览和导出报表满足。M2 再做 WeCom Bot 或业务门户。

## 9. 测试与验收基线

### 9.1 P0 阻断项

以下任一失败，M1 不得进入试点：

| 项 | 必须通过的证据 |
| --- | --- |
| ycapi-only 静态配置 | `validate_config.py` 和 pytest 拒绝供应商 key/base、video、passthrough、危险 settings |
| fail-closed 运行时边界 | 未白名单 route 全部 403/404，且无 outbound |
| Google 原生路由封堵 | `:generateContent` 与 `:streamGenerateContent` 路径被拒 |
| `/config/update` 封堵 | 无法运行期翻转 `store_model_in_db` |
| 模型写路径封堵 | UI/API 不能新增直连模型 |
| chat 非零 spend | mock/live 调用写入非零 spend |
| image 非零 spend | `input_cost_per_image > 0` 且调用写入非零 spend |
| 预算阻断 | 小预算 key 超额前置 429 且无 outbound |
| key 元数据 | 缺部门/项目/成本中心/场景/预算/有效期/审批人时创建失败 |
| shared key 主体 | 缺 `user` 或 `metadata.end_user_principal` 时调用失败 |
| 错误契约 | body `request_id` + OpenAI-compatible error 稳定 |

### 9.2 回归用例

至少覆盖：

- route inventory test：新增 LiteLLM route 默认不进入 allowlist。
- deny path matrix：provider passthrough、Google native、Vertex discovery、pass-through endpoints。
- method mismatch：`GET /v1/chat/completions`、`POST /v1/models` 被拒。
- pricing mutant：移除 chat price、把 image price 写成 `output_cost_per_image`、设成 0.0 均失败。
- config drift：尝试 `/config/update`、模型写入、DB passthrough 后 health FAIL 或请求被拒。
- RBAC：财务无 key 修改、部门管理员不能看跨部门明细、审计只读不能写。
- secret scan：`.env.example` 只有占位符，日志和错误不回显密钥。

### 9.3 live 口径

没有真实 `YCAPI_API_TOKEN` 时，live chat/image 项标记为 `BLOCKED`，不能写成 `PASS`。有 token 时必须真实跑：

- chat 一条。
- image 一条。
- 401/403/429/5xx mock 错误矩阵。
- spend log 查询。
- request id 关联。

## 10. 分期实施

### M1-A：硬边界与计价修复

交付：

1. `aimanager/asgi.py` 与 `YcapiOnlyAllowlistMiddleware`。
2. compose/启动命令改为启动 AiManager ASGI app。
3. `validate_config.py` 增加 route/config/pricing/image key 校验。
4. `ycapi-image-1` 改为 `input_cost_per_image > 0`。
5. chat 模型显式写非零 input/output token 价格。
6. `/config/update`、模型写路径、provider passthrough、Google native route 测试。

退出条件：AC-01、AC-02、AC-03、AC-05、AC-07、AC-09、AC-15、AC-17、AC-18 无 FAIL。

### M1-B：治理与报表闭环

交付：

1. key 创建前置校验。
2. shared key `enforced_params`。
3. 冻结/撤销/轮换流程与审计落点。
4. 小预算阻断 smoke。
5. 日报/月报/对账 CSV。
6. 轻量经营总览。
7. RBAC 测试。

退出条件：AC-04、AC-06、AC-08、AC-10、AC-11、AC-12、AC-13、AC-14、AC-16、AC-19 无 FAIL；live 缺 token 只能标 BLOCKED 并写明解除条件。

### M2：业务试点

交付：

- 业务场景字典。
- 市场内容流程闭环。
- 品牌安全留痕。
- 非技术入口 5 分钟合规试用。
- 月结调整和差异处理流程。
- 员工监控制度告知。

### M3：公司推广

交付：

- 多部门推广。
- 异常行为检测。
- 管理层周期报告。
- WeCom 协作入口或通知。
- 运维与财务月度例行制度。

## 11. 互评问题处理结果

| 来源 | 问题 | 裁决 |
| --- | --- | --- |
| Claude 评 Codex P0-1 | image 计价键错误，`output_cost_per_image` 无效 | 采纳。M1 使用 `input_cost_per_image > 0`，验收真实 spend |
| Claude 评 Codex P0-2 | denylist 非 fail-closed，会漏 Google native 等路由 | 采纳。L2 改为 ASGI allowlist 主边界 |
| Claude 评 Codex P1-1 | `/config/update` 可翻转 `store_model_in_db` | 采纳。M1 显式拒绝 `/config/update` |
| Claude 评 Codex P1-2 | 审计落点不明确 | 采纳。who/when/action 走 LiteLLM audit，原因写 metadata |
| Claude 评 Codex P1-3 | shared key 未绑定 `enforced_params` | 采纳。共享 key metadata 固化 `enforced_params` |
| Claude 评 Codex P2-1 | body `request_id` 非 LiteLLM 原生 | 采纳并决策。M1 用 wrapper 注入 body request_id |
| Claude 评 Codex P2-2 | `gemini-2.5-flash` 内置价格静默继承风险 | 采纳。validator 强制显式 ycapi 转售价 |
| Codex 评 Claude P1 | 白名单路由过宽 | 采纳。M1 业务 API 收窄到三个 PRD 承诺路径 |
| Codex 评 Claude P1 | `/model/` 写路径依赖内部 500 | 采纳。模型写入由中间件显式 403 |
| Codex 评 Claude P1 | `request_id` A/B 未定 | 采纳。本文选择方案 B |
| Codex 评 Claude P2 | RBAC 与路径白名单职责不够细 | 采纳。本文补 M1 RBAC 表 |
| Codex 评 Claude P2 | 技术非零价与财务审批价需区分 | 采纳。validator 与财务价格版本分层 |

## 12. 实施前检查清单

进入代码实现前，开发人员按本文逐项确认：

- [ ] M1 route allowlist 已转成 method + path 精确清单。
- [ ] 路由清单测试能发现 LiteLLM 新增 route。
- [ ] `validate_config.py` 覆盖 image `input_cost_per_image`、chat input/output token price、禁供应商 key/base、禁 passthrough、禁 video。
- [ ] `aimanager/config.yaml` 不再含无效的 `output_cost_per_image` 作为图片计价字段。
- [ ] `/config/update` 和模型写路径被显式拒绝。
- [ ] shared key 创建时写入 `enforced_params`。
- [x] 错误响应体含 `request_id`，header 保留 `x-litellm-call-id`。
- [ ] AC-01 到 AC-19 的测试能因真实缺陷失败。
- [ ] 没有真实 ycapi token 的 live 项标记 `BLOCKED`，不伪装通过。

本文是 M1 实施基线。若后续 PRD 或验收标准变化，必须同步修订本文、`1_acceptance_criteria.md` 和 `项目知识图谱.md`。

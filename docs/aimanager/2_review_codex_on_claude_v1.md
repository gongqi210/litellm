# Codex 对 Claude 详细设计的评审 v1

日期：2026-06-29

评审对象：`docs/aimanager/2_detailed_design_claude_v1.md`

## 1. 结论

Claude 版整体可作为 M1 实现主设计之一，优点是源码锚点扎实，尤其补到了 Codex 版没有充分展开的三点：`google_router` 原生 Gemini 路由、`/config/update` 漂移、image 计价键使用 `input_cost_per_image` 而不是 `output_cost_per_image`。这些发现应进入最终汇总设计。

但 Claude 版的白名单中间件方案存在过宽放行和部分未决策项未闭环的问题。若直接按文档实现，M1 可能暴露 PRD 未承诺的 endpoint，或把模型写路由依赖 LiteLLM 内部 500 行为兜底，验收上不够硬。

## 2. Findings

### P1 - M1 允许路由范围过宽，会暴露未承诺能力

位置：Claude 版 §3.2。

问题：设计把 `/v1/completions`、`/v1/embeddings`、`/chat/completions`、`/images/generations`、`/models` 等都放进业务 LLM 白名单。PRD v3 的 M1 明确承诺的是 `GET /v1/models`、`POST /v1/chat/completions`、`POST /v1/images/generations`，而 `embeddings` 未进入模型白名单、计价、SDK 示例、mock/live 验收矩阵。无 `/v1` 前缀的兼容路径也没有被 PRD 承诺。

为什么重要：AC-03/04/05/06/07 的验收边界只覆盖 chat/image/models。白名单放行超出承诺范围的 endpoint，会产生未计价、未对账、未错误契约验证的行为面；这和 ycapi-only 治理目标冲突。

建议修复：M1 白名单只放行 PRD 承诺的三个路径及健康检查、受控管理面。`/v1/completions`、`/v1/embeddings`、无 `/v1` 前缀路径必须进入“需单独 PRD+计价+验收后开放”清单。若确需保留 OpenAI legacy compatibility，也必须先补模型、计价和 AC 映射。

### P1 - `/model/` 管理路由放行依赖 `store_model_in_db=false` 的内部失败行为，边界不够硬

位置：Claude 版 §3.2、§3.3。

问题：设计允许 `/model/` 管理路由通过中间件，然后依赖 `store_model_in_db=false` 让模型写操作在 LiteLLM 内部报 500。这个做法把安全边界建立在上游内部实现细节上，而且 500 语义也不适合作为“被治理策略拒绝”的正常结果。

为什么重要：FR-01 要求管理员尝试新增直连 provider、修改 model config 或启用 DB 模型存储时，系统必须阻止、回滚或触发漂移告警。AC-15 也要求管理面访问控制清晰。依赖内部 500 不利于审计、告警、用户提示和回归测试；LiteLLM 上游行为一改，边界可能变软。

建议修复：中间件层对模型写路径做显式拒绝，只放行只读模型信息接口，例如 `GET /v1/models`、`GET /model/info`。`POST /model/new`、`PATCH/POST /model/update`、`DELETE /model/*`、`/config/update` 这类配置写入口默认 403，并返回 `aimanager_config_immutable`。`store_model_in_db=false` 保留为第二道兜底，而不是主控制面。

### P1 - 错误契约中的 `request_id` 没有做出实现决策

位置：Claude 版 §7.2。

问题：PRD v3 的错误响应示例要求顶层 `request_id`；Claude 版提出方案 A（只从响应头取）和方案 B（body 注入），但没有在 M1 设计中定案。

为什么重要：AC-07 需要验证错误契约。若设计不定案，测试无法写成确定断言，开发者 SDK 示例也不知道从 body 还是 header 读取 request id。PRD 目前已经写了 body 字段，默认应按 PRD 实现，除非后续正式修 PRD。

建议修复：M1 选方案 B：AiManager 中间件统一给自有拦截错误和 LiteLLM 错误响应补顶层 `request_id`，同时保留 `x-litellm-call-id` 响应头。若为降低侵入选择方案 A，则必须同步修订 PRD/AC，将 `request_id` 口径改为 header，并在 SDK 示例中统一展示。

### P2 - 白名单中间件与 RBAC 的职责边界需要更细

位置：Claude 版 §3.2、§6.2。

问题：设计说 M1 默认放行“全部管理读端点 + key/team/budget 写端点”，但没有把这些管理路由按角色拆成超级管理员、系统管理员、财务、审计只读、部门管理员的可见和可写范围。当前表述容易让中间件变成“只按路径不按角色”的粗粒度入口。

为什么重要：RBAC 是 PRD v3 和 AC-14 的 P0 验收项。路径白名单只解决“能不能到 LiteLLM”，不解决“谁能做什么”。如果设计文档不明确角色与管理 API 的关系，实现时容易依赖 Admin UI 默认行为，无法证明财务/总经理/审计只读范围正确。

建议修复：在汇总设计中补一张“管理 API/页面 × 角色”的 M1 权限表，并要求 AC-14 至少用 mock session 或 API token 覆盖高危写操作拒绝、费用下钻可见范围、个人明细脱敏。

### P2 - `validate_config.py` 扩展项很好，但要区分“转售价正确性”和“非零存在性”

位置：Claude 版 §3.1、§5.1。

问题：Claude 版正确指出要校验非零价格，且发现 image 计价键问题。但静态校验最多证明价格存在且非零，不能证明价格就是财务审批价。

为什么重要：AC-09 是技术检查，PRD 的财务要求还包括审批人、生效时间、价格版本、币种和税务口径。若最终实现只做非零数值检查，财务仍无法通过。

建议修复：把 validator 分两层：配置技术校验检查“存在、数值、键名、模型覆盖”；财务验收检查 `pricing_version`、`approved_by`、`effective_at`、`currency`、`tax_mode` 和价格登记表/配置是否匹配。

## 3. Strengths

- Claude 版对源码锚点更细，尤其是 `google_router`、`route_checks.allowed_passthrough_routes`、`enforced_params`、`SpendLogsPayload` 和 image 计价键发现，应该纳入最终设计。
- 选择进程内 ASGI 白名单中间件而不是删路由，工程上更稳，升级 LiteLLM 时风险更低。
- 明确指出当前 overlay 只覆盖 L0 静态校验，没有 L2 运行时封堵，这与知识图谱中的进入 M1 前置条件一致。
- 预算阻断部分锚定了 LiteLLM 的具体 budget check，比 Codex 版更适合后续实现和测试。

## 4. 与 Codex 版的主要差异

- Codex 版使用“Edge Guard + 应用层 deny middleware”的双层封堵，但没有把应用层方案具体到 `aimanager/asgi.py`；Claude 版的实现路径更具体。
- Claude 版倾向默认拒绝白名单；Codex 版倾向 provider denylist。最终建议采用 Claude 的 fail-closed 白名单思想，但白名单必须缩窄到 PRD 已承诺 endpoint。
- Codex 版的财务导出和经营治理叙述更完整；Claude 版的 LiteLLM 源码与测试锚点更完整。

## 5. 合并建议

建议合并进入最终设计，合并原则如下：

1. 采用 Claude 版的 `aimanager/asgi.py` 进程内白名单中间件，但按 Codex/PRD 收窄业务路由。
2. 采用 Claude 版的 image 计价修正、预算源码锚点、Google 原生路由封堵和 `enforced_params` 设计。
3. 采用 Codex 版的财务导出、月度对账、RBAC/UI 四态和 M1/M2/M3 经营闭环结构。
4. 在汇总设计中关闭 `request_id` 未决项，形成可测试的 AC-07 断言。

最终状态：有条件通过。修复上述 P1 后可作为 M1 实施设计基线。

# Codex 详细设计评审（Claude 评审 Codex v1）

日期：2026-06-29
评审对象：`docs/aimanager/2_detailed_design_codex_v1.md`
对照基线：`docs/aimanager/1_PRD_v3.md`、`docs/aimanager/1_acceptance_criteria.md`、当前仓库 `aimanager/` overlay 与 LiteLLM 源码
评审性质：设计评审，按严重度排序，先给问题再给结论

## 0. 评审口径

我按"会不会导致 M1 退出条件被错误判 PASS"来定严重度。P0 是会直接让 AC-02、AC-05、AC-09 这类 M1 阻断项形式上通过、实质上失效的硬伤；P1 是会留下可被绕过的边界或漂移面；P2 是契约/可维护性层面的欠账。所有源码结论都已在本仓库核对过行号与行为，下文标注的事实均经过验证，不是推测。

## 1. 按严重度排序的问题

### P0-1　image 计价键错误未被发现，AC-05/AC-09 会假性通过

影响章节：Codex §7.1（计价字段表把 `unit` 写成 `image`）、§5.1（pricing 校验只说"缺少非零价格版本"）、§11.2 AC-05/AC-09 映射。

为什么重要：当前 `aimanager/config.yaml` 给 `ycapi-image-1` 写的是 `output_cost_per_image: 0.0`。我核对了 `litellm/cost_calculator.py:2007-2013` 的 `default_image_cost_calculator`：它只读 `input_cost_per_image`（Priority 1），否则回退 `input_cost_per_pixel`（Priority 2），两者都没有就直接 `raise Exception`。它从不读取 `output_cost_per_image`。也就是说现配置的键名是错的、值也是零；即便把它改成非零的 `output_cost_per_image`，图片成本仍然要么算成 0、要么抛异常。Codex 文档全篇没有点出这个键错配，§5.1 的 pricing 校验只笼统要求"非零价格版本"，§7.1 把计价单位写成 `image` 但没绑定到 LiteLLM 实际读取的键。按 Codex 文档原样实现，image 调用的 spend 会是 0 或直接报错，而 AC-05（记录图片费用）和 AC-09（非零计价）却会因为"配置里确实写了一个非零数字"而被判 PASS。这正是 PRD §11 和 AC-09 明令禁止的"价格为 0.0 标 PASS"。

具体修复：把 `ycapi-image-1` 改用 `input_cost_per_image`（每张成功返回图片的 ycapi 转售价，非零），删除无效的 `output_cost_per_image` 和无意义的 `input_cost_per_token`；`validate_config.py` 增加"image 模型必须存在且 `input_cost_per_image > 0`"的校验；AC-05 断言必须落到 `LiteLLM_SpendLogs.spend` 真实写入了非零金额，而不仅是配置里有个数字。这点 Claude 设计 §5.3 已经定位并给了修复方向，Codex 应直接采纳。

### P0-2　passthrough 封堵用 denylist，不是 fail-closed，会漏 google 原生端点与无鉴权路由

影响章节：Codex §5.2（Edge Guard denylist 固定路径表 + 应用层 `AIMANAGER_BLOCKED_PROVIDER_PATHS` 前缀匹配）、§4（关键实现锚点只列了 `llm_passthrough_router`）。

为什么重要：完整 proxy 在 `litellm/proxy/proxy_server.py` 无条件挂载了三个相关 router，我已核对：`llm_passthrough_router`（`:15444`）、`pass_through_router`（`:15445`）、`google_router`（`:15478`，来自 `litellm/proxy/google_endpoints/endpoints.py`，挂 Gemini 原生 `:generateContent`/`:streamGenerateContent`）。Codex 的封堵清单（§5.2）只枚举了 provider-specific 前缀（`/anthropic/*`、`/gemini/*` 等）和隐含的 `/pass-through-endpoints`，没有覆盖 google 原生 `:generateContent` 这种"路径里没有 provider 前缀、靠后缀触发"的端点，也没列 `/watsonx`、`/milvus`、`/vertex_ai/discovery/`。`/vertex_ai/discovery/{endpoint:path}` 在 `llm_passthrough_endpoints.py` 是无鉴权挂载，按 key 限权这层也兜不住。更根本的问题是方向：denylist 要求穷举，上游 LiteLLM 一旦新增任何 provider passthrough 路由，Codex 的封堵就静默放行，AC-02"所有可用上游路径均被证明只走 ycapi"和 PRD FR-01 立刻被破。这不是配置疏漏，是机制选型问题。

具体修复：改成 fail-closed 的白名单（allowlist）中间件——只放行明确需要的前缀（`/v1/chat/completions`、`/v1/images/generations`、`/v1/models`、收敛后的管理与 `/ui` 静态资源），其余一律默认拒绝。这样上游新增的任何路径默认 404，`:generateContent`、`/vertex_ai/discovery/` 这类也被同一条规则兜住，无需逐个枚举。这正是 Claude 设计 §2.2/§3.2 的方案，技术上严格优于 denylist：denylist 的失效模式是"漏列就泄漏"，allowlist 的失效模式是"漏列就 404"，对一个把 ycapi-only 当作不可变约束的系统，后者是唯一可接受的失败方向。Codex 在这一点上是错的，应改用白名单。

需要说明的是，Codex §5.2 的验收里确实写了"mock ycapi 和任何直连供应商 mock 都无请求记录"，这个"无 outbound"断言是对的、也是必须的，这一点不用改；要改的是被验收的封堵机制本身。

### P1-1　运行时 `/config/update` 可翻转 `store_model_in_db`，Codex 漂移控制没堵这条

影响章节：Codex §5.3 管理漂移控制表。

为什么重要：Codex 用 `STORE_MODEL_IN_DB=false` + 启动检查来兜模型写操作，方向对。但我核对 `proxy_server.py:5491-5501`：运行时通过 `/config/update` 传入 `general_settings.store_model_in_db` 可以把这个全局值在进程内翻成 `True`，启动期检查管不到运行期翻转。Codex 的 denylist 既不在拒绝清单里列 `/config/update`，§5.3 也只写了"启动检查 `store_model_in_db` 最终值"，没有覆盖运行时翻转这条路径。一旦 `store_model_in_db` 被运行时置真，`/model/new` 就能把直连 provider 模型写进运行态，绕过整个 ycapi-only 边界。

具体修复：M1 不暴露 `/config/update`（白名单默认不放行即可，这又是 fail-closed 比 denylist 强的一个实例），并在漂移自检里把"`store_model_in_db` 运行期最终值必须为 false"纳入健康检查，非 false 即 FAIL health。

### P1-2　管理操作审计落点没有源码锚点，AC-11 留痕只停在要求层

影响章节：Codex §6.3 key 生命周期"管理操作必须写入：操作人、时间、动作、对象 key hash、原因、备注、审批来源"。

为什么重要：Codex 把审计字段列全了，但没说这些字段落到哪、由谁写。LiteLLM 有原生对象审计日志机制（`create_object_audit_log`，记录 who/when/action），处置原因这类业务字段则需要写进 key metadata（如 `disposition_reason`）。Codex 文档停在"必须写入"，没给落地载体，实现者容易自造一张表或干脆漏掉，AC-11"审计记录包含处置人和原因"就会只在 mock 里假装满足。

具体修复：明确"who/when/action 复用 LiteLLM 原生对象审计日志，处置原因/备注写 key metadata，M2 再建风险事件扩展表"，给出 schema 落点。Claude 设计 §6.1 已这样落地，可对齐。

### P1-3　共享 key 强制透传最终主体，没绑定到原生 `enforced_params`

影响章节：Codex §6.2 metadata 合并规则"共享 key 必须在 request metadata 提供 `end_user_id` 或 `system_actor_id`；缺失时 400/403"。

为什么重要：这是 PRD FR-03 的硬约束（无法透传主体的系统不得进入公司级推广）。Codex 把要求写对了，但没给实现机制，听起来像要自己在中间件里手写一段校验。其实 LiteLLM 原生就有 `enforced_params`（我核对 `litellm/proxy/litellm_pre_call_utils.py:2031-2044`，可从 `general_settings` 或单把 key 的 `metadata['enforced_params']` 读取，缺字段直接拒），共享 key 创建时在 metadata 写 `enforced_params: ["user", "metadata.scenario_l1", "metadata.end_user_principal"]` 即可，无需自研。Codex 漏掉这个原生能力，既增加实现量又偏离"standard over hand-rolled"。

具体修复：把共享 key 的主体透传绑定到 `enforced_params`，在创建 runbook 里固化这批字段。

### P2-1　错误体顶层 `request_id` 是 LiteLLM 原生交付不了的契约

影响章节：Codex §8.3 错误响应 JSON 示例里直接写了 `"request_id": "req_..."`。

为什么重要：PRD FR-07 的错误 schema 确实要求顶层 `request_id`，但 LiteLLM 的 `ProxyException.to_dict()` 默认只返回 `{message, type, param, code}`，不含顶层 `request_id`。Codex 把它当成原生就有的字段写进契约，没说明这需要约定客户端从响应头 `x-litellm-call-id` 读，或在中间件里往错误体注入。实现者照抄会发现错误体里没有 `request_id`，AC-07 的契约断言对不上。

具体修复：明确二选一——方案 A 约定从响应头 `x-litellm-call-id` 取并统一文档/SDK 示例（最小改动、跟随上游）；方案 B 在中间件包一层往 body 注入 `request_id`。Claude 设计 §7.2 已把这个差距点出并给了 A/B 取舍与 owner，Codex 应补上同样的决策。

### P2-2　`gemini-2.5-flash` 与内置价格表同名导致静默继承，未提示

影响章节：Codex §5.1 pricing 校验。

为什么重要：`gemini-2.5-flash` 在 LiteLLM 内置价格表里有同名条目，自定义单价缺失或写错时不会报错，而是静默继承供应商原价（不是 ycapi 转售价）。Codex 的校验只说"缺少非零价格版本"判 FAIL，能挡住键缺失，但挡不住"键在、值非零、但值错"的情况，文档也没提醒这个静默继承陷阱。严重度低于 image 那条，因为非零校验至少能挡住最坏的零值，真实转售价的正确性本就要靠价格版本评审兜。

具体修复：在 §5.1 加一句风险提示，并要求 chat 模型显式写 `input_cost_per_token`/`output_cost_per_token`，由价格版本评审确认数值是 ycapi 转售价。

## 2. Codex 设计的优点

这些是 Codex 做得扎实、Claude 设计可以反过来借鉴的地方，不是客套：

- §7.4 财务导出给了具体文件清单（`aimanager_usage_daily.csv`、`aimanager_finance_monthly.csv`、`aimanager_reconciliation.csv`、`aimanager_risk_events.csv`）和粒度，比 Claude 设计的散文式描述更可直接落地；`unassigned` 成本不得关账这条写进了对账规则，和 PRD FR-05 完全对齐。
- §13 把三个形态（完整 proxy+Edge Guard、fork 删 passthrough、改用 `gateway/`）的取舍显式列了优缺点，决策可追溯，这点比 Claude 设计只在 §2.1 一笔带过更清楚。
- §5.2 验收 curl 的 `for` 循环写法符合项目对"证据要可复现的 bash"的偏好，且包含了"无 outbound 到上游"这条关键断言。
- §10.3、§12 的失败模式表和分期退出条件（M1-A/M1-B/M2/M3）边界清楚，AC 映射表（§11.2）覆盖到 AC-26，完整度高。
- §6.3 key 状态机把六个状态对应到 LiteLLM 字段/策略，并明确列出高风险动作清单，治理意识到位。

## 3. 与我自己（Claude）设计的分歧与自查

为公平起见，列出我设计里也存在、不应只苛责 Codex 的点：

- 财务导出文件清单：Codex 比我具体。我的 §5.6 偏机制（指到日聚合表和端点），落地交付物清单不如 Codex 直接，应补齐文件名与字段对齐样例。
- 形态取舍的显式对比：Codex §13 的三方案优缺点表值得我补进 §2。
- 我和 Codex 在"保留完整 proxy 而非 gateway data-plane"、"模型清单 Git-managed 而非 UI-managed"、"非零计价是预算验收前提"、"live 缺 token 必须标 BLOCKED"这些大方向上完全一致，没有分歧。核心分歧只在 passthrough 封堵的机制选型（denylist vs allowlist）和 image 计价键这两点，而这两点恰好都是 P0。

## 4. 最终合入建议

结论：**Codex v1 方向正确、结构完整，但不能按现状进入 M1 实现，必须先修两个 P0。**

理由很直接：Codex 的总体架构判断（保留完整 proxy、Git-managed 模型清单、双层封堵、非零计价验收、分期退出）都站得住，文档完整度甚至在财务交付物和形态取舍上强于我的版本。但 P0-1（image 计价键错配）和 P0-2（denylist 非 fail-closed）会让 AC-05、AC-09、AC-02 这三个 M1 阻断项形式上 PASS、实质失效——而 PRD §11 和 AC 明确规定这三项任一 FAIL 即整体 FAIL、不得进入试点。这两个问题都已用源码核实（`cost_calculator.py:2007-2013` 只读 `input_cost_per_image`；`proxy_server.py:15444/15445/15478` 三个 router 无条件挂载），不是观点之争。

建议的合入路径：

1. 先合 Codex 的文档骨架（§7.4 财务清单、§13 取舍、§6.3 状态机、AC 映射），这些直接可用
2. 用 Claude 设计的两点替换 Codex 对应章节：passthrough 封堵改 fail-closed allowlist 中间件（替换 §5.2 的 denylist 主机制，Edge Guard 降为边缘加固而非主防线）；image 计价改 `input_cost_per_image` 并相应扩 `validate_config.py`（替换 §7.1/§5.1 的相关口径）
3. 补三个 P1（运行时 `store_model_in_db` 翻转检查、审计落点锚点、共享 key 绑 `enforced_params`）和两个 P2（错误体 request_id 方案、gemini 静默继承提示）

两份设计不是二选一，而是 Codex 的交付物结构 + Claude 的两处机制修正合并成一份实现级文档最优。修完 P0 与上述 P1/P2 后，可进入 M1 实现。

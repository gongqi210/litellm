# AiManager 详细设计 v1（Claude）

日期：2026-06-29
作者：Claude（与 Codex 协作）
状态：实现级设计，覆盖 PRD v3、验收矩阵 v3、多角色评审 v2 的 P0 返工项
基线代码：`gongqi210/litellm`（分支 `codex/bootstrap-aimanager`），完整 LiteLLM proxy + Admin UI 形态

本文是落地文档，不是产品综述。所有实现触点都给出仓库内的 `文件:行` 锚点，便于 Codex 直接对照源码动手。需求口径以 `docs/aimanager/1_PRD_v3.md`、`docs/aimanager/1_acceptance_criteria.md`、`docs/aimanager/1_multi_role_review_v2.md` 为准；本文只做技术决策、接口契约、数据落地、强制收敛方案和验收映射。

## 0. 术语与单一事实源

| 术语 | 含义 | 单一事实源 |
| --- | --- | --- |
| ycapi | OpenAI 兼容聚合网关，`https://ycapi.ycaicloud.com/v1` | `YCAPI_BASE_URL` |
| 上游 token | ycapi 下游凭证，全公司仅一枚，由控制台签发 | `YCAPI_API_TOKEN`，不下发给员工 |
| virtual key | LiteLLM 给员工/系统签发的访问凭证，业务文案称"访问凭证" | `LiteLLM_VerificationToken` 表 |
| 模型清单 | M1 对外只开放 `gemini-2.5-flash`、`deepseek-chat`、`ycapi-image-1` | `aimanager/config.yaml` 的 `model_list` |
| request_id | 贯穿日志、spend、错误关联的请求标识 | `litellm_call_id`，响应头 `x-litellm-call-id` |

核心不变量：模型清单的唯一事实源是 `aimanager/config.yaml`，不是数据库；上游出口的唯一事实源是 `YCAPI_BASE_URL` + `YCAPI_API_TOKEN`，不存在第二条直连供应商出口。本文所有强制收敛设计都围绕"让这两个事实源在配置期、启动期、运行时和管理面都不可被绕过"展开。

## 1. 设计目标与非目标

### 1.1 设计目标（按重要性排序）

1. 正确：所有可用上游路径（`model_list` 路由、provider passthrough、配置型 passthrough、Google 原生端点、反代直透）都只能到 ycapi，且这一点可被运行时验证，而不仅是静态校验通过。
2. 安全：上游 token、数据库密码、master key 不出现在响应、日志、文档；Admin UI 不暴露在不受控公网；密钥可轮换、可冻结、可撤销并留痕。
3. 可计费：预算、报表、超预算阻断建立在已审批的非零计价之上；`0.0` 价格不得进入 M1 财务验收。
4. 可读可维护：v1 以配置 overlay + 一个最小的进程内强制收敛模块交付，不分叉 LiteLLM 内部实现，便于跟随上游升级。
5. 现代：复用 LiteLLM 既有的 key/team/budget/spend/usage 能力和 OpenAI 兼容契约，而不是另造一套。

### 1.2 非目标

1. 不接入任何直连供应商凭证（OpenAI、Anthropic、Gemini、Azure、Bedrock、Vertex 等）。
2. 不承诺 API 网关能自动判定"每一次调用是否绝对属于工作用途"；治理目标是可追踪、可限制、可发现、可处置、可复盘。
3. M1 不把 LiteLLM Admin UI 当作最终业务门户；总经理/财务/市场视角的业务门户属于 M2 及以后。
4. M1 不映射 ycapi 专有路径：`ycapi-video-1`（异步创建+轮询语义）、img2img、`/v1/multimodal/generate`、`/v1/usage`。
5. 不默认存储完整 prompt/response 正文。

## 2. 架构选型与理由

### 2.1 形态：完整 proxy + 进程内强制收敛中间件

LiteLLM 有两种部署形态：完整 `proxy`（含 Admin UI、管理端点、provider passthrough）和 `gateway/` 的 data-plane-only。M1 选完整 proxy，因为需求要保留 key/team/budget/spend/Admin UI（PRD §4.1、§12）。代价是完整 proxy 会无条件挂载一批 provider passthrough 路由，这些路由绕过 `model_list`，是 ycapi-only 保证的最大泄漏面（多角色评审硬伤 #1）。

关键源码事实：在 `litellm/proxy/proxy_server.py` 中，passthrough 路由是**无条件**挂载的，没有任何环境变量或开关能整体关闭它们：

- `app.include_router(llm_passthrough_router)`（`proxy_server.py:15444`），来自 `litellm/proxy/pass_through_endpoints/llm_passthrough_endpoints.py`
- `app.include_router(pass_through_router)`（`proxy_server.py:15445`），来自 `litellm/proxy/pass_through_endpoints/pass_through_endpoints.py`，承载 `general_settings.pass_through_endpoints` 配置型透传与 `/pass-through-endpoints/` 运行时 CRUD
- `app.include_router(google_router)`（`proxy_server.py:15478`），来自 `litellm/proxy/google_endpoints/endpoints.py`，挂载 Gemini 原生 `:generateContent`/`:streamGenerateContent`

因此"只靠静态校验 `config.yaml`"无法满足 AC-02。必须在运行时把这些路径封死。

### 2.2 决策：进程内 ASGI 白名单中间件为主，边缘反代和按 key 限权为辅

`app = FastAPI(..., lifespan=proxy_startup_event)`（`proxy_server.py:1160-1168`），且配置可由环境变量 `CONFIG_FILE_PATH` 注入（`proxy_server.py:830`，`get_secret_str("CONFIG_FILE_PATH")`）。LiteLLM 自身就用 `app.add_middleware(...)` 叠加中间件（`proxy_server.py:1741-1752`、`15481`）。这给了我们一个不分叉内部实现的干净切入点：AiManager 提供一个 ASGI 包装模块，导入官方 `app` 并叠加一个**默认拒绝**的白名单中间件，再用 uvicorn 启动这个包装后的 app。lifespan 仍由官方 `app` 持有，配置仍从 `CONFIG_FILE_PATH` 加载，升级 LiteLLM 不受影响。

选白名单而非黑名单的理由：黑名单要穷举 `/anthropic`、`/gemini`、`/bedrock`、`/vertex_ai`、`/vertex-ai`、`/vertex_ai/discovery`、`/openai`、`/openai_passthrough`、`/cohere`、`/vllm`、`/mistral`、`/azure`、`/azure_ai`、`/watsonx`、`/cursor`、`/assemblyai`、`/eu.assemblyai`、`/milvus` 以及 Google 原生 `:generateContent`，上游一旦新增 provider passthrough 就会静默泄漏。白名单（fail-closed）只放行明确需要的前缀，新增的任何路径默认 404，符合 never-nester 与 fail-closed 原则。

四层防线（纵深防御）：

| 层 | 时机 | 机制 | 主要源码锚点 |
| --- | --- | --- | --- |
| L0 配置期 | CI / 启动前 | `validate_config.py` 拒绝供应商标记、`openai/*`、误配 video、`pass_through_endpoints`、`0.0` 计价 | `aimanager/scripts/validate_config.py` |
| L1 边缘 | 入口反代 | 反代只放行允许前缀，其余 404，且 Admin UI 仅内网/VPN | 部署层（nginx/Caddy/Envoy） |
| L2 进程内 | 每个请求 | ASGI 白名单中间件，默认拒绝，捕获未认证的 `/vertex_ai/discovery/` 等 | AiManager 新增 `aimanager/asgi.py` |
| L3 按 key | 鉴权期 | key/team 不授予 `allowed_passthrough_routes`，`allowed_routes` 收敛到 LLM+管理路由 | `litellm/proxy/auth/route_checks.py:623-675` |

L3 已经部分 fail-closed：对 `auth=true` 的 passthrough 路由，`route_checks.py:666-674` 要求 key 或 team 的 metadata 显式配置 `allowed_passthrough_routes` 才放行，未配置即拒绝。但 `/vertex_ai/discovery/{endpoint:path}` 在 `llm_passthrough_endpoints.py:1785-1787` 是**无鉴权**挂载，会绕过 L3；配置型 passthrough 也可能声明 `auth=false`。所以 L2 中间件是不可省的兜底，L3 只是加固。

## 3. ycapi-only 强制收敛

### 3.1 L0 配置期静态校验

现状 `aimanager/scripts/validate_config.py` 已校验：环境引用白名单（`ALLOWED_ENV_REFS`）、`model_list` 必须 `openai/<ycapi-model>` 且禁止 `openai/*`、禁止 `ycapi-video-1` 进 `model_list`、`api_base`/`api_key` 必须指向 ycapi 环境变量、`store_model_in_db` 必须为 `false`、禁止直连供应商文本标记（`FORBIDDEN_CONFIG_MARKERS`）。

必须补齐的校验项（否则 AC-01/AC-09 不可靠）：

1. 非零计价：现 `_validate_model_list`（`validate_config.py:73-107`）只检查 chat 模型存在 `input_cost_per_token`/`output_cost_per_token` 键，不检查数值，且对 `ycapi-image-1` 完全跳过定价检查（`validate_config.py:105`）。需改为：chat 模型 `input_cost_per_token` 与 `output_cost_per_token` 必须 `> 0`；image 模型必须存在且 `> 0` 的 `input_cost_per_image`（见 §5.3 的关键发现）。
2. 禁止 `general_settings.pass_through_endpoints`：一旦出现，直接判 FAIL，杜绝配置型透传出口。
3. 禁止 `router_settings.fallbacks` / `default_fallbacks` 指向任何非 ycapi 模型（M1 模型清单内只有 ycapi 模型，fallback 必须也在清单内）。

该脚本同时作为 CI gate 与容器启动前置（启动脚本先跑校验，FAIL 即拒绝进入运行状态，对应 PRD §11"配置错误"）。

### 3.2 L2 运行时 passthrough 封堵（实现核心）

新增文件 `aimanager/asgi.py`（v1 唯一需要写的应用层代码），形态：

```text
from litellm.proxy.proxy_server import app   # 复用官方 app + lifespan
app.add_middleware(YcapiOnlyAllowlistMiddleware)   # 默认拒绝
# 启动：uvicorn aimanager.asgi:app（CONFIG_FILE_PATH 指向 aimanager/config.yaml）
```

`YcapiOnlyAllowlistMiddleware` 是一个 `BaseHTTPMiddleware`/纯 ASGI 中间件，逻辑（早返回，无深嵌套）：

1. 取 `request.url.path`，与允许前缀集合做最长前缀匹配；命中则放行，未命中返回 OpenAI 兼容的 403 错误体（见 §7.2 错误契约），并自增 `aimanager_passthrough_blocked_total{path}` 指标。
2. 允许前缀是一个 `frozenset`，覆盖三类：
   - 业务 LLM 路由：`/v1/chat/completions`、`/v1/completions`、`/v1/embeddings`、`/v1/images/generations`、`/v1/models`、`/chat/completions`、`/images/generations`、`/models`
   - 管理与可观测：`/key/`、`/team/`、`/user/`、`/budget/`、`/model/info`、`/model/`（见 §3.3 说明，写操作另由 `store_model_in_db=false` 兜死）、`/spend/`、`/global/spend`、`/health`、`/health/liveliness`、`/health/readiness`、`/metrics`
   - 控制台静态资源：`/ui`、`/sso`、`/login`、`/_next`、`/static`、`/`（登录页）
3. 显式拒绝清单（即便误进白名单也兜底）：所有 §2.1 列出的 passthrough 前缀，外加 `/pass-through-endpoints`、Google 原生 `:generateContent`/`:streamGenerateContent`、`/vertex_ai`、`/vertex-ai`。

为什么用中间件而不是删路由：启动后遍历 `app.routes` 删除目标路由依赖路由对象的内部结构，跨版本脆弱；中间件只看路径字符串，稳定且可单测（构造 `/anthropic/v1/messages`、`/vertex_ai/discovery/x` 等请求断言 404/403，且断言没有 outbound 到上游）。

边界判定准则（未决，见 §11）：白名单"管理路由"具体放行到哪一级，取决于业务门户是否最终复用 LiteLLM 管理端点。M1 默认放行全部管理读端点 + key/team/budget 写端点（供管理员用），但 `/pass-through-endpoints` 的 CRUD 一律拒绝。

### 3.3 管理面漂移控制

需求：管理员通过 Admin UI/API 不能新增直连 provider、不能改 model config、不能开 DB 模型存储（FR-01 第三条）。源码层面 `store_model_in_db=false` 已经把模型写操作兜死：

- `store_model_in_db` 默认 `False`（`proxy_server.py:1916`），由 `general_settings.store_model_in_db` 读入（`proxy_server.py:4372-4375`）。
- `/model/new` 处理函数 `add_new_model`（`litellm/proxy/management_endpoints/model_management_endpoints.py:1206`）在 `store_model_in_db` 非真时直接抛 HTTP 500：`{"error": "Set 'STORE_MODEL_IN_DB='True'' in your env to enable this feature."}`（`model_management_endpoints.py:1305-1309`）。即 UI/API 无法把新模型写进运行态。
- 运行时通过 `/config/update` 改 `store_model_in_db` 也要防：`proxy_server.py:5491-5501` 允许运行时改这个值。L2 中间件对 `/config/update` 默认不放行（不在白名单），M1 不暴露该端点；若未来需要，必须再加一道"禁止把 store_model_in_db 置 true"的校验。

漂移检测（M1 轻量，M2 强化）：

1. 启动自检：lifespan 之后，AiManager 自检 `llm_router` 暴露的模型名集合是否恰好等于 `config.yaml` 的 `model_list`，不一致则告警并标记 unhealthy。
2. Admin UI 收敛：M1 用 `DISABLE_ADMIN_UI`（`litellm/proxy/discovery_endpoints/ui_discovery_endpoints.py:25`、`litellm/proxy/management_endpoints/ui_sso.py:850` 读取 `os.getenv("DISABLE_ADMIN_UI")`）这一开关决定是否对外开 UI。决策：M1 保留 UI 但仅限内网/VPN（PRD §10、AC-15）；不需要把 UI 整体关掉，因为模型写操作已被 `store_model_in_db=false` 封死，UI 退化为 key/team/budget/usage 管理面。

### 3.4 DB / config 交互

模型清单走 YAML（不进 DB）；key/team/budget/user/spend 走 Postgres。两者职责不重叠：YAML 决定"能调哪些模型、出口在哪"，DB 决定"谁能调、配额多少、花了多少"。这条边界保证即使数据库被改，也无法新增一个直连供应商的模型出口。

`/v1/models`（`proxy_server.py:8054`，依赖 `user_api_key_auth`）按 key/team 的 `models` 白名单过滤并隐藏被 block 的模型名（`proxy_server.py:8121`）。配合 key 级 `models` 限制（见 §4），普通员工 key 的 `/v1/models` 只会看到被授权的子集。AC-03 的"只展示三个模型"由 config 的 model_list + master/admin 视角共同保证。

## 4. 保留的 LiteLLM 能力与元数据映射

### 4.1 保留能力清单

| 能力 | LiteLLM 载体 | AiManager 用法 |
| --- | --- | --- |
| 访问凭证 | `LiteLLM_VerificationToken`（`schema.prisma:390-449`） | 每员工/系统一把 virtual key，绑定组织与成本维度 |
| 团队/部门 | `LiteLLM_TeamTable`（`schema.prisma:118-157`） | 一部门一 team，承载 `department_id`/`cost_center_id` |
| 用户/员工 | `LiteLLM_UserTable`（`schema.prisma:234-268`） | 实名员工主体 |
| 预算 | key/team/user 自带 `max_budget`/`soft_budget` + `LiteLLM_BudgetTable`（`schema.prisma:12-34`） | 财务上限与预警 |
| 限流 | `tpm_limit`/`rpm_limit`/`max_parallel_requests` | 技术限流，与预算解耦 |
| 用量/费用 | `LiteLLM_SpendLogs`（`schema.prisma:577-614`）+ 日表 | 审计与财务归集 |
| 管理 UI | `_experimental/out` 静态站 + 管理端点 | M1 技术管理面 |

### 4.2 维度元数据落地口径

PRD §9 要求把员工/部门/项目/成本中心/场景标签映射进 LiteLLM。落地分三处，优先级"请求 > key > team"，越具体越优先：

1. team metadata（`LiteLLM_TeamTable.metadata`，`schema.prisma:126`）：`department_id`、`cost_center_id`、`owner_id`、`default_project_id`。一部门建一个 team。
2. key metadata（`LiteLLM_VerificationToken.metadata`，`schema.prisma:407`）：`employee_id`、`employee_name`、`department_id`、`project_id`、`project_name`、`cost_center_id`、`scenario_l1`、`scenario_l2`、`internal_or_external`。创建时通过 `GenerateKeyRequest.metadata`（`litellm/proxy/_types.py:1025`）写入，处理函数 `generate_key_fn`（`litellm/proxy/management_endpoints/key_management_endpoints.py:1320`）经 `_common_key_generation_helper`（`key_management_endpoints.py:646`）落库。
3. request metadata：单次调用可在 body 的 `metadata` 或请求头 `x-litellm-spend-logs-metadata`（`litellm/proxy/litellm_pre_call_utils.py:680`，由 `_get_spend_logs_metadata_from_request_headers` 解析）覆盖项目/场景。请求级与 key 级 metadata 的合并发生在 `litellm_pre_call_utils.py:318` 起的 `_metadata_variable_name`（`litellm_pre_call_utils.py:339-351`）逻辑里。

成本中心维度的归集字典与 `LiteLLM_DailyTagSpend`（`schema.prisma:857`，按 `tag` 聚合）打通：key metadata 里的 `tags`（如 `scenario:研发/代码辅助`、`cost_center:cc-123`）在 `litellm_pre_call_utils.py:1017-1020` 被合并进请求 tags，进而落进 `request_tags`（`LiteLLM_SpendLogs.request_tags`，`schema.prisma:598`）和日 tag 表。这样财务可直接按场景/成本中心出报表，无需自建归集逻辑。

### 4.3 共享 key 的最终主体透传（FR-03 关键）

共享 key（系统级、部门级）必须强制透传最终使用人或系统主体，否则审计无法落到自然人。LiteLLM 原生支持 `enforced_params`（`litellm/proxy/litellm_pre_call_utils.py:2031-2044`，`_get_enforced_params`），可在 `general_settings.enforced_params` 全局设置，或在某把 key 的 metadata 里设 `enforced_params`（`user_api_key_dict.metadata['enforced_params']`，`litellm_pre_call_utils.py:2044`），示例值 `['user', 'metadata.scenario_l1', 'metadata.end_user_principal']`（注释见 `litellm_pre_call_utils.py:1705`）。缺字段的请求会被拒绝。

落地：共享 key 创建时在 metadata 写 `enforced_params: ["user", "metadata.scenario_l1", "metadata.end_user_principal"]`。这样任何用共享 key 的调用都必须带最终主体，否则报错。无法透传主体的内部系统不得进入公司级推广（PRD §3 FR-03），由制度+这条技术约束共同保证。

`KeyRequestBase`（`litellm/proxy/_types.py:1060`）已提供 `enforced_params`（`_types.py:1065`）、`tags`（`_types.py:1063`）、`allowed_routes`（`_types.py:1066`）、`allowed_passthrough_routes`（`_types.py:1067`）字段，无需扩展 schema 即可落地上述全部约束。

## 5. 计价、非零成本、预算阻断、spend 日志、财务导出、对账

### 5.1 计价模型

LiteLLM 成本计算入口 `completion_cost`（`litellm/cost_calculator.py:1098-1680`）-> `cost_per_token`（`cost_calculator.py:292-689`）。自定义单价通过 `_cost_per_token_custom_pricing_helper`（`cost_calculator.py:176-224`）读取，并在 `cost_per_token` 内（`cost_calculator.py:402-413`）优先于内置价格表 `model_prices_and_context_window.json` 生效。

`gemini-2.5-flash` 与内置价格表同名，会静默继承内置价，因此 chat 模型**必须**显式写 `input_cost_per_token`/`output_cost_per_token`（且非零），这是 ycapi 转售价而非供应商原价。计价口径（PRD §FR-05）需在配置注释或 §10 价格版本表固化：币种、含税口径、单价版本、生效时间、最小计费单位、四舍五入、失败请求是否计费、streaming 按最终 usage 计费、image 按成功返回图片数计费。

### 5.2 价格版本

LiteLLM 没有内建"价格版本+历史快照"。M1 用配置承载当期价 + 一张轻量价格版本登记（`pricing_version`、`effective_at`、`currency`、`unit_price`，PRD §9），月结金额按调用时点的价格快照固化（spend 落库时已是当时算出的金额，天然是快照）。M2 若要价格回溯重算，再建扩展表。M1 不做重算，只保证"已写入 spend 的金额不被改价影响"。

### 5.3 image 计价的关键发现（AC-05/AC-09 阻断项）

`aimanager/config.yaml` 现在给 `ycapi-image-1` 写的是 `output_cost_per_image: 0.0`。但 LiteLLM 的图片成本计算 `default_image_cost_calculator`（`cost_calculator.py:1941-2014`）只读 `input_cost_per_image`（`cost_calculator.py:2007-2008`，`return cost_info["input_cost_per_image"] * n`）或回退 `input_cost_per_pixel`（`cost_calculator.py:2010-2011`），两者都没有时直接抛异常（`cost_calculator.py:2012-2013`）。它从不读取 `output_cost_per_image`。

结论：当前配置的 `output_cost_per_image` 既是错的键、又是零值。即使改成非零的 `output_cost_per_image` 也不会被采纳，图片成本会算成 0 或直接报错。修复：`ycapi-image-1` 必须改用非零 `input_cost_per_image`（每张成功返回图片的转售价），并由 §9 的 mock/live 冒烟断言 image 调用确实在 `LiteLLM_SpendLogs.spend` 写入了非零金额。`validate_config.py` 需相应改为校验 image 模型的 `input_cost_per_image > 0`（§3.1）。

### 5.4 预算阻断（请求前拦截）

所有预算检查在鉴权阶段、调用上游之前完成，超额抛 `litellm.BudgetExceededError`，proxy 映射为 HTTP 429：

| 维度 | 函数 | 锚点 | 触发条件 |
| --- | --- | --- | --- |
| key 硬预算 | `_virtual_key_max_budget_check` | `litellm/proxy/auth/auth_checks.py:3434-3502` | `spend >= valid_token.max_budget` |
| key 多窗口预算 | `_virtual_key_multi_budget_check` | `auth_checks.py:3505-3543` | 窗口内 `window_spend >= max_budget` |
| team 硬预算 | `_team_max_budget_check` | `auth_checks.py:3828-3873` | `spend > team.max_budget` |
| 全局预算 | `_global_proxy_budget_check` | `auth_checks.py:347-358` | `global_spend > litellm.max_budget` |
| 软预算（仅告警） | `_virtual_key_soft_budget_check` / `_team_soft_budget_check` | `auth_checks.py:3546-3582`、`3913-3983` | `spend >= soft_budget`，不阻断 |

预算阻断用 Redis 计数器（`spend:key:{token}`、`spend:team:{team_id}`）做实时预估，月结金额按已确认调用与价格快照固化（PRD §FR-05 区分实时预估与月结）。失败请求是否计费见 §5.5。AC-10 用"小预算 + mock 调用"验证：把某 key `max_budget` 设到极小值，调用到阈值后请求被 429 拒绝并记录原因。

### 5.5 spend 日志（含失败计费口径）

成功调用经回调 `_ProxyDBLogger._PROXY_track_cost_callback`（`litellm/proxy/hooks/proxy_track_cost_callback.py:171-307`）写库；失败/中断经 `async_post_call_failure_hook`（`proxy_track_cost_callback.py:41-168`），会从已产生的 usage 恢复成本（`proxy_track_cost_callback.py:155`，`recovered_response_cost`）并以 `status="failure"` 落库。即失败请求若已消耗 token 也会计费并计入预算，这点必须在计价口径里写清楚。

落库编排 `DBSpendUpdateWriter.update_database`（`litellm/proxy/db/db_spend_update_writer.py:121-219`），payload 由 `get_logging_payload`（`litellm/proxy/spend_tracking/spend_tracking_utils.py:227-448`）构造，类型 `SpendLogsPayload`（`litellm/proxy/_types.py:3095-3127`）。落进 `LiteLLM_SpendLogs` 的关键字段：`request_id`（`schema.prisma:578`）、`api_key`（哈希后，`580`）、`spend`（`581`）、`prompt_tokens`/`completion_tokens`/`total_tokens`（`582-584`）、`model`（`589`）、`user`（`594`）、`team_id`（`599`）、`metadata`（`595`，含 `_get_spend_logs_metadata` 抽取的 `user_api_key_team_id`/`user_api_key_project_id` 等，`spend_tracking_utils.py:68`）、`request_tags`（`598`）、`startTime`/`endTime`。

默认不存完整 prompt/response 正文（PRD §FR-06）：确保 `general_settings` 不开 `store_prompts_in_spend_logs` 一类选项，spend 日志只留计量与维度字段。这点纳入 §9 配置检查。

### 5.6 财务导出与日聚合

LiteLLM 已内建按维度的日聚合表，财务无需扫描明细：`LiteLLM_DailyUserSpend`（`schema.prisma:704`）、`LiteLLM_DailyTeamSpend`（`826`）、`LiteLLM_DailyTagSpend`（`857`）、`LiteLLM_DailyEndUserSpend`（`766`）、`LiteLLM_DailyOrganizationSpend`（`735`）。每行含 `date`、`api_key`、`model`、`endpoint`、`prompt_tokens`、`completion_tokens`、`spend`、`api_requests`、`successful_requests`、`failed_requests`。

导出端点：`/spend/logs`（`litellm/proxy/spend_tracking/spend_management_endpoints.py:2261`，`view_spend_logs`，支持按 api_key/user/request_id/日期过滤）、`/global/spend/report`（`spend_management_endpoints.py:954`，按日期/team/model 聚合）、`/spend/keys`（`:48`）、`/spend/users`（`:110`）、`/global/spend`（`:2730`）。AC-12/AC-13 要求按"日期、部门、项目、员工、成本中心、key、模型、endpoint"聚合并导出 CSV/XLSX：部门/项目/成本中心维度通过 team metadata + key metadata + DailyTagSpend 的 tag 维度拼出。M1 可先用上述端点 + 一段 SQL/脚本生成对账样例，业务化报表留给 M2 门户。

### 5.7 月度对账

AiManager 的 spend 导出与 ycapi 账单/发票/充值消耗按月对账（PRD §FR-05）。差异字段：月份、模型、token/图片数、金额、币种、税费、key、部门、项目。差异容忍阈值默认 1% 或 10 元人民币取大者，超阈由财务与系统管理员共同确认。M1 交付对账字段对齐的导出样例（AC-13），月结闭环与冲销留 M2（AC-25）。注意 ycapi 侧 `/v1/usage` 是专有路径不映射进 LiteLLM（见 §1.2），对账以 ycapi 控制台账单/发票为准，不依赖在 LiteLLM 内拉 ycapi usage。

## 6. Key 生命周期、RBAC、UI 边界

### 6.1 Key 状态机

PRD §FR-02 定义状态：`active`、`rotating`、`frozen`、`revoked`、`expired`、`budget_limited`。LiteLLM 原生字段到状态的映射：

| 状态 | LiteLLM 载体 | 鉴权期判定锚点 |
| --- | --- | --- |
| active | 默认 | - |
| frozen | `LiteLLM_VerificationToken.blocked=true`（`schema.prisma:408`） | `litellm/proxy/auth/user_api_key_auth.py:1610`，blocked 抛"Key is blocked" |
| revoked | 删除 key（`/key/delete`） | 查无此 key -> 401 |
| expired | `expires < now`（`schema.prisma:396`） | `user_api_key_auth.py:1716-1734`，过期抛"Expired Key" |
| budget_limited | 预算检查命中 | §5.4 各 budget check -> 429 |
| rotating | `auto_rotate`/`rotation_interval`（`_types.py:1097-1098`）或手动轮换期 | 轮换窗口内新旧 key 并存 |

冻结/撤销/降额必须留痕（处置人、时间、原因、备注，AC-11）。LiteLLM 的对象审计日志（`create_object_audit_log`，见 `model_management_endpoints.py:1318-1331` 同款机制）记录 who/when/action；处置原因 M1 写进 key metadata 的 `disposition_reason`，M2 建风险事件扩展表（PRD §9）。

创建 key 必填字段（FR-02）：所有者、部门、项目、成本中心、业务场景、模型范围、预算、RPM/TPM、有效期、审批人。映射到 `GenerateKeyRequest`：`user_id`、`team_id`、`metadata.{department_id,project_id,cost_center_id,scenario_l1,scenario_l2,approver}`、`models`、`max_budget`/`soft_budget`、`rpm_limit`/`tpm_limit`、`duration`。这些字段 `GenerateRequestBase`/`KeyRequestBase`/`GenerateKeyRequest`（`_types.py:1011/1060/1090`）均已具备。AiManager 在创建端点前置一层 Pydantic 校验，强制这批字段非空（避免裸用未约束的 dict），缺失即 422，满足 AC-08。

### 6.2 RBAC

PRD §7 的八类角色（超级管理员、系统管理员、总经理、财务、部门管理员、开发者、普通员工、审计只读）映射到 LiteLLM 角色体系（`LitellmUserRoles`）：proxy_admin / proxy_admin_viewer / internal_user / internal_user_viewer / team 维度角色。M1 用这套原生角色覆盖"高危操作仅管理员、财务/总经理/审计只读、部门管理员限本部门"，team 级可见范围由 team 归属天然隔离。细到"总经理可批注、要求整改"这类业务动作 M1 不在 UI 实现，走制度+导出报表；M2 业务门户再做。AC-14 用不同角色登录/mock 权限验证高危按钮可见性与费用/个人明细可见范围。

### 6.3 Admin UI 与业务门户边界

M1：Admin UI 仅限内网/VPN（AC-15），定位为技术管理面（key/team/budget/usage），模型写操作已被 `store_model_in_db=false` 封死。业务可读价值通过"一页轻量经营总览 + 导出报表"交付（PRD §4.1 第 9 条），数据来自 §5.6 的日聚合表与导出端点。业务 UI 文案转译（virtual key=访问凭证、RPM=每分钟请求上限、TPM=每分钟用量上限、token=文本用量单位、P95=95% 请求响应耗时）M1 先落到导出报表表头与总览页文案，自研门户留 M2（开放问题 §11）。

## 7. OpenAI SDK 兼容、请求/错误契约、可观测性

### 7.1 兼容矩阵（M1）

承诺能力：Python/Node OpenAI SDK + curl 的 chat（`gemini-2.5-flash`、`deepseek-chat`）与 image（`ycapi-image-1`，首选 `response_format: b64_json`）。`base_url` 必须含 AiManager 的 `/v1`，用 virtual key。需 live 冒烟确认后才承诺：streaming、tool calls、JSON mode、response_format。受限：vision 只接受 base64 `data:` URL（ycapi 不抓远程图片 URL，见集成备忘），`n`/`size`/`quality` 以 ycapi 实际支持为准。不支持：img2img、`/v1/multimodal/generate`、ycapi `/v1/usage`、video（PRD §FR-07）。

vision 的 `drop_params: true`（已在 `config.yaml`）确保 SDK 传入 ycapi 不支持的参数时被丢弃而非报错。image 调用建议带 `Idempotency-Key`（ycapi 对 image/video 要求幂等键，见集成备忘），M1 文档在 curl/SDK 示例里给出。

### 7.2 错误契约

LiteLLM 的 `ProxyException`（`litellm/proxy/_types.py:3199`）经 `openai_exception_handler`（`litellm/proxy/proxy_server.py:1321-1332`）返回 `JSONResponse(status_code, content={"error": error_dict}, headers=...)`，`error_dict` 来自 `to_dict()`（`_types.py:3235-3245`），字段为 `{message, type, param, code}`。状态码语义与 PRD §FR-07 一致：401 无效 key、403 无权限/被封路径、404 模型或路径不存在、429 预算/频率/上游限流、5xx 系统/上游故障。"No healthy deployment" 自动映射 429（`_types.py:3230`）。

差距与决策：PRD 期望的错误体含顶层 `request_id`，但默认 `to_dict()` 不含 `request_id`。request_id 即 `litellm_call_id`，成功响应经响应头 `x-litellm-call-id` 暴露（`litellm/proxy/common_request_processing.py:831`，请求可自带或由 uuid4 生成，`common_request_processing.py:1117`）。两个可选方案：

- 方案 A（推荐，最小改动）：约定客户端从响应头 `x-litellm-call-id` 取 request_id，文档与 SDK 示例统一从 header 读，错误体保持 LiteLLM 原生 `{"error": {...}}`。
- 方案 B：在 §3.2 的中间件里包一层错误响应，往错误体注入 `request_id`（从 `request.state` 或生成的 call id 取）。

决策准则：若内部系统普遍依赖 body 内 request_id 做日志关联，选 B；否则选 A（不动 LiteLLM 错误契约，跟随上游最稳）。owner：架构师 + 开发代表，M1 冒烟前定。L2 中间件返回的 403（passthrough 封堵）必须用与 §7.2 一致的 OpenAI 兼容错误体，`type` 用 `permission_error`，并保证不泄露上游 token/DB 密码/供应商密钥（PRD §FR-07 末句）。

### 7.3 可观测性

request_id 贯穿 AiManager 中间件日志、LiteLLM usage/spend（`LiteLLM_SpendLogs.request_id`）、ycapi 响应关联。指标（PRD §FR-06）：请求数、失败率、延迟、token/图片数、费用、按 key/team/model 维度、429/5xx、预算阻断次数、撤销 key 拦截次数、passthrough 拦截次数。前几项 LiteLLM Prometheus 集成已出；后三项中"passthrough 拦截次数"由 §3.2 中间件自增 `aimanager_passthrough_blocked_total`，"预算阻断次数"可从 BudgetExceededError 计数或 429 标签拆分，"撤销 key 拦截"从 401/blocked 日志统计。日志保留（PRD §FR-06）：usage 24 月、财务报表 36 月、风险事件 24 月、管理操作 36 月、脱敏采样 ≤90 天，由 Postgres 分区表保留策略与 `LiteLLM_SpendLogs` 分区清理（`litellm/proxy/db/db_transaction_queue/spend_logs_partition_manager.py`）落地。

## 8. 部署拓扑、安全控制、密钥、故障模式

### 8.1 拓扑

现状 `aimanager/docker-compose.yml`：`aimanager`（基于本仓库构建，端口 4000，`STORE_MODEL_IN_DB=False`，`config.yaml` 只读挂载，healthcheck 打 `/health/liveliness`）+ `db`（postgres:16）。v1 在此基础上：

1. 启动命令从直接跑 LiteLLM CLI 改为跑 `uvicorn aimanager.asgi:app`（§3.2），并设 `CONFIG_FILE_PATH=/app/config.yaml`，使官方 app 的 lifespan 仍加载 ycapi-only 配置。
2. 入口前置反代（L1），只放行白名单前缀，Admin UI 与管理端点限内网/VPN 来源（AC-15）。
3. 启动前置跑 `validate_config.py`（L0），FAIL 即不启动。

### 8.2 密钥与安全

`LITELLM_MASTER_KEY`、`YCAPI_API_TOKEN`、`POSTGRES_PASSWORD` 只进 `.env`（已在 `.gitignore`），生产用 secret manager，不在日志/文档/响应回显（PRD §10、AC-18）。`.env.example` 只放占位（AC-18，secret scan）。master key 需轮换机制；ycapi token 泄漏时立即轮换、冻结旧 token、审计最近调用、通知受影响管理员（PRD §10）。ycapi token 全公司一枚且不下发员工，其 rate/spend 上限需在 ycapi 控制台配置得足够大，避免单 token 限额拖垮全员（验收手工项）。

### 8.3 故障模式（PRD §11）

| 场景 | 行为 | 锚点/机制 |
| --- | --- | --- |
| ycapi 429 | 透传 429，记 key/model/request_id，不切直连 | LiteLLM 上游错误映射；M1 `num_retries:1`（`config.yaml`） |
| ycapi 5xx/超时 | 返回可解释 `upstream_error`，记 retryable，告警 | `request_timeout:600` / `router_settings.timeout:600` |
| Postgres 不可用 | 管理面不可用，健康检查失败，不静默放行未授权请求 | `/health/readiness`（`litellm/proxy/health_endpoints/_health_endpoints.py:1547`）探测 DB |
| 配置错误 | 启动/CI 校验失败，不进运行态 | L0 `validate_config.py` |
| passthrough 未封堵 | M1 FAIL，不得上线 | §3.2 L2 中间件 + AC-02 |
| 价格为 0.0 | M1 财务验收 FAIL | §5.3 + L0 非零校验 |
| master key 泄漏 | 轮换 master key，审计高危操作 | 运维流程 |
| 预算池耗尽 | 阻断或降级，通知管理员/财务，不调直连兜底 | §5.4 budget check -> 429 |

关键不变量：任何故障路径都不得 fallback 到直连供应商。M1 模型清单内的 fallback 必须仍是 ycapi 模型，且 L2 中间件保证即使配置出错也没有第二条出口。

## 9. 测试与验收计划（AC-01..AC-26）

测试遵循"会因功能缺失/被 mutate 而失败"的原则，不写凑覆盖率的空测试。单测放 `tests/test_litellm/` 镜像路径或 `aimanager/tests/`（沿用现有 `aimanager/tests/test_config.py` 约定）。

### 9.1 M1（AC-01..AC-19）

| 编号 | 验证方式 | 关键断言 | 主要锚点 |
| --- | --- | --- | --- |
| AC-01 静态配置 | `validate_config.py` + pytest | 拒绝供应商 key/base、`openai/*`、误配 video、`pass_through_endpoints`、`0.0`/缺失计价 | `aimanager/scripts/validate_config.py`（需按 §3.1 扩展） |
| AC-02 运行时封堵 | 对 `/anthropic/` `/gemini/` `/bedrock/` `/vertex_ai/` `/vertex_ai/discovery/` `/openai/` `/openai_passthrough/` `/cohere/` `/vllm/` `/mistral/` `/azure/` `/azure_ai/` `/watsonx/` `/cursor/` `/v1beta/models/x:generateContent` `/pass-through-endpoints` curl | 全 403/404 且无 outbound 到上游 | §3.2 中间件单测 + 集成 |
| AC-03 模型白名单 | live/mock `/v1/models` | 只见三模型 | `proxy_server.py:8054` |
| AC-04 chat 正向 | mock ycapi；有 token 时 live | 经 AiManager 到 ycapi，生成 usage/spend/request_id | §5.5 |
| AC-05 image 正向 | mock ycapi；有 token 时 live | 记录非零图片费用与 request_id | §5.3（必须先修 `input_cost_per_image`） |
| AC-06 SDK 兼容 | curl/Python/Node 示例 | 承诺能力跑通 | §7.1 |
| AC-07 错误契约 | mock 401/403/404/429/5xx | OpenAI 兼容错误，不泄密钥 | `proxy_server.py:1321-1332` |
| AC-08 key 元数据 | Admin/API | 绑定员工/部门/项目/成本中心/场景/预算/限流/有效期 | §6.1 + 前置 Pydantic 校验 |
| AC-09 非零计价 | 配置/库/调用 | 无全 0 价格 | §5.1/§5.3 |
| AC-10 预算阻断 | 小预算 + mock | 达阈被拒并记原因 | §5.4 |
| AC-11 冻结/撤销 | 创建后冻结/撤销再调 | 被拒，审计含处置人/原因 | §6.1 |
| AC-12 报表 | 查询/导出 | 多维聚合 | §5.6 |
| AC-13 对账字段 | 导出样例 | 含模型/数量/金额/币种/部门/项目/key/差异字段 | §5.7 |
| AC-14 RBAC | 多角色登录/mock | 高危仅管理员，只读范围正确 | §6.2 |
| AC-15 管理面访问控制 | 部署配置检查 | UI 不暴露不受控公网 | §8.1 L1 + `DISABLE_ADMIN_UI` |
| AC-16 可观测 | 日志/metrics | request_id、失败率、429/5xx、预算阻断、passthrough 拦截可观测 | §7.3 |
| AC-17 故障场景 | mock 429/5xx、DB down、配置错误 | 可解释错误，不切直连 | §8.3 |
| AC-18 `.env.example` 安全 | secret scan | 仅占位 | `aimanager/.env.example` |
| AC-19 live 口径 | 缺真实 token 时 | 标 BLOCKED，不伪装 PASS | 流程约束 |

AC-02 的"无 outbound 到上游"断言要落到：mock 一个会记录任何 outbound 的 fake 上游，断言被封路径不产生任何到 ycapi/供应商的请求。这是与"返回 403/404"互补的关键断言，否则只验了状态码没验真封堵。

### 9.2 M2（AC-20..AC-26）

市场场景标签（AC-20）、内容流程闭环（AC-21）、品牌安全留痕（AC-22）、非技术入口 5 分钟合规试用（AC-23）、经营总览（AC-24）、月结与调整（AC-25）、员工监控制度告知（AC-26）。这些以业务流程+扩展表+门户为主，M1 只把场景字典与品牌安全规则写进制度与 key/request metadata，技术上预留 tag/metadata 字段（§4.2）。

### 9.3 live 冒烟口径

无真实 `YCAPI_API_TOKEN` 时 live 项一律标 BLOCKED（AC-19），不得伪装 PASS。有 token 时按项目规范用真实 ycapi 跑 chat 与 image 各一条，证明 request_id 贯穿、spend 非零、错误契约正确，证据用 curl 命令 + 输出，不用一次性 python 脚本。

## 10. 分阶段实施计划

### M1 本地技术验证（P0，无 FAIL 才退出）

1. 修 `config.yaml`：chat 模型非零 `input_cost_per_token`/`output_cost_per_token`；`ycapi-image-1` 改用非零 `input_cost_per_image`（删除无效的 `output_cost_per_image`，§5.3）。
2. 扩展 `validate_config.py`：非零计价、image 计价键、禁 `pass_through_endpoints`、fallback 仅限 ycapi 模型（§3.1）。
3. 新增 `aimanager/asgi.py` + `YcapiOnlyAllowlistMiddleware`（§3.2），改 compose 启动命令与 `CONFIG_FILE_PATH`（§8.1）。
4. 创建 key 的前置 Pydantic 校验，强制 §6.1 必填字段；共享 key 写 `enforced_params`（§4.3）。
5. 价格版本登记 + 对账字段对齐的导出样例（§5.2/§5.7）。
6. 一页经营总览（读日聚合表，§5.6）。
7. 测试：AC-01..AC-19 全部覆盖，passthrough 封堵单测含"无 outbound"断言（§9.1）。

### M2 业务试点（先补经营/财务/合规/市场/UI 前置）

市场内容流程与品牌安全可执行入口、非技术入口（WeCom Bot/轻量网页/部门管理员代操作，开放问题 §11）、财务月结闭环与冲销（扩展表）、业务化报表门户、员工监控制度告知。

### M3 公司推广

统一大模型 API 使用规范，员工只领 virtual key，影子 API 有制度约束与发现机制，月度经营复盘输出异常用量与节省建议。

## 11. 风险、权衡与未决策

### 11.1 技术风险

1. image 计价键错配（§5.3）：现配置 `output_cost_per_image` 不被采纳，image 成本会算 0 或报错。已定修复方向（改 `input_cost_per_image`），但需 live 冒烟确认 ycapi-image-1 的 deployment 自定义价确实进了 `litellm.model_cost` 并落到 spend。owner：开发；准则：mock+live 都能看到非零 image spend 才算 AC-05/AC-09 PASS。
2. passthrough 跟随上游漂移：LiteLLM 升级可能新增 provider passthrough 前缀，白名单中间件天然 fail-closed（新前缀默认拒），但要在升级 checklist 里复跑 AC-02 全量路径断言。owner：架构师。
3. `gemini-2.5-flash` 内置价覆盖：忘记写显式价就会静默用错价。L0 非零校验拦截，但需注意校验只看键值，真实转售价的正确性靠价格版本评审。
4. 中间件白名单粒度：放行到 `/model/`（含写）依赖 `store_model_in_db=false` 兜死写操作。若未来开 DB 模型存储，这条假设失效，必须同步收紧白名单并恢复 model 写操作的显式拒绝。

### 11.2 权衡

1. 中间件 vs 反代 vs 删路由：选进程内白名单中间件为主（可单测、随 app 走、不分叉），反代为边缘加固，不选启动后删路由（跨版本脆弱）。
2. 错误体 request_id（§7.2）：方案 A（header）最小改动、跟随上游；方案 B（注入 body）满足强契约但要维护一层包装。

### 11.3 未决策（带 owner 与决策准则）

1. showback vs chargeback（PRD 开放问题 1）：影响是否需要部门实际分摊结算逻辑。owner：财务 + 总经理；准则：M2 试点前定，chargeback 需在 §5.6 报表加分摊与结算字段。
2. AiManager 转售价审批与更新频率（开放问题 2）：影响 §5.2 价格版本流程。owner：财务；准则：M1 至少有一版经审批的非零价并登记审批人/生效时间。
3. 数据出境边界（开放问题 3）：哪些数据类型允许进境外模型/ycapi 上游。owner：合规 + 总经理；准则：M2 试点前完成数据分类与制度，未定前市场外发与客户数据类用途不得进入推广。
4. 非技术入口形态（开放问题 4）：WeCom Bot / 轻量网页 / 部门管理员代操作。owner：UI/UED + 部门管理员；准则：以 AC-23"5 分钟完成一次合规试用"为验收，M2 前选型。
5. 经营总览实现方式（开放问题 5）：导出报表生成 vs 自研页面。owner：UI/UED；准则：M1 先用导出 + 一页总览（读日聚合表）满足 AC-24 雏形，自研门户视 M2 投入决定。
6. 错误体 request_id 方案 A/B（§7.2）：owner：架构师 + 开发；准则：内部系统是否普遍依赖 body 内 request_id，M1 冒烟前定。

### 11.4 与现有 overlay 的差异

现 `aimanager/` overlay（config + validate_config + compose + test_config）只覆盖 L0 静态校验，没有 L2 运行时封堵，且 image 计价键错、chat/image 价格为 0。按本设计，M1 必须新增 `aimanager/asgi.py` 的中间件、修计价、扩校验，才能让 AC-01/AC-02/AC-05/AC-09 真正可 PASS，而不是仅静态校验通过（与项目知识图谱"进入 M1 前必须确认运行时 passthrough 已封堵"一致）。

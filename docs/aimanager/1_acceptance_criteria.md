# AiManager Acceptance Criteria v3

日期：2026-06-29

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

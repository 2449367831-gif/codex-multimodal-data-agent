# 工具选型与边界

评估日期：2026-08-28。

## 当前采用

| 层 | 工具 | 采用原因 |
|---|---|---|
| 业务语义 | 版本化 YAML 合同 + JSON Schema + Git | 能精确表达指标、来源、Join、粒度、时间、单位、排除项、鲜度、对账和审批；差异可评审、可回滚。 |
| 对话与工具编排 | OpenAI Codex Python SDK + App Server + 项目级 MCP | 复用 Codex 开源线程、模型调用和工具循环；MCP 只暴露一个只读 SQL 工具，Codex 不持有数据库凭据。 |
| 业务知识与 Skill | 项目级 `doris-query-workflow` Skill + YAML 结构化快照 | Skill 负责完整取数工作流；宿主只提供版本绑定的指标、来源和候选证据，模型选择适用口径，不由关键词路由强行执行。 |
| SQL 编译与安全 | SQLGlot Doris AST | 允许模型生成任意查询形状，但强制单条只读语句、作用域物理表、Schema/Catalog、危险构造和返回上限。 |
| Doris 连接 | PyMySQL + Doris 专用只读账号 | Doris FE 兼容 MySQL 协议，部署简单；查询超时、行数上限和审计均在本服务控制。 |
| 服务与审计 | FastAPI + SQLite | 第一阶段依赖轻，便于本机验证；业务结果行不写入审计库。生产可把审计后端换为 PostgreSQL。 |
| 自动迭代 | 只读元数据扫描 + GitHub Actions + 黄金问题 | 自动发现结构、鲜度和键质量变化，只生成候选；业务语义变化必须新版本和人工审批。 |

Codex 采用官方 Python SDK，而不是手写 App Server JSON-RPC。既有 LangGraph/模板内核继续
保留给合同回归，不属于新的单工具 Codex 执行路径。只有在建设自定义多会话 UI、
细粒度事件流或审批界面时才直接接 App Server。已废弃的 `codex mcp-server` 不进入方案。
详细边界见 `docs/CODEX_INTEGRATION.md`。

## 暂不作为第一版核心

### Apache Doris MCP Server

Apache 官方的 Doris MCP Server 1.0 已提供受控只读查询、认证授权、结果边界和审计能力，适合后续作为统一 Doris 访问层，尤其适用于多个 Agent/MCP Host 共用一个网关的场景。

它不替代本仓库的业务指标知识库。官方文档也明确区分了 MCP 执行/grounding 与指标语义所有权；MetricFlow 集成需要外部 provider，并未内置原生 Doris MetricFlow/dbt adapter。因此第一版直接执行器保持简单，后续可增加 `doris_mcp` executor，合同、发布门禁和黄金问题无需变化。

- [Apache Doris MCP Server](https://github.com/apache/doris-mcp-server)
- [Doris MCP 官方文档](https://doris.apache.org/docs/4.x/key-features/mcp-server/)
- [Doris MCP 的 MetricFlow 集成边界](https://github.com/apache/doris-mcp-server/blob/master/docs/integrations/metricflow.md)

### MetricFlow / dbt Semantic Layer

MetricFlow 对复杂维度 Join、比率和累计指标的编译能力很强，但它依赖可工作的 dbt project 与 adapter。评估时 Doris engine 支持仍处于上游待合并工作，官方 Doris MCP 也明确不附带原生 adapter。现在强行接入会把本项目的风险从业务口径治理转成 adapter/dialect 维护；等 Doris 适配稳定且指标数量、动态维度组合显著增长后再评估迁移。

- [MetricFlow](https://github.com/dbt-labs/metricflow)
- [Doris engine support PR](https://github.com/dbt-labs/metricflow/pull/1999)

### OpenMetadata / DataHub

两者都适合企业级数据目录、血缘、Owner、搜索和质量展示，也已有 Doris 连接能力；但部署和治理成本明显高于当前单一 Doris 取数 Agent，而且目录元数据不能自动成为可执行业务口径。指标合同规模扩大、需要跨团队检索或统一血缘门户时，可将本仓库的发布合同同步进去，仍不应让目录观察直接改写指标公式。

- [OpenMetadata](https://github.com/open-metadata/OpenMetadata)
- [DataHub Doris ingestion source](https://docs.datahub.com/docs/generated/ingestion/sources/doris/)

## 升级触发条件

- 需要多个 Agent、IDE 或 BI 客户端共享统一 Doris 鉴权与查询网关：增加官方 Doris MCP executor。
- 需要数百指标、多跳维度 Join 和自由维度组合：重新验证 Doris adapter 后评估 MetricFlow。
- 需要企业级搜索、Owner、血缘、分类和数据产品门户：接入 OpenMetadata 或 DataHub，合同仓库继续作为可执行语义权威。

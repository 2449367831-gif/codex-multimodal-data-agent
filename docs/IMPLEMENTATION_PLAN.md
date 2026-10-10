# 直接 Doris 取数 Agent 实施状态

## 已完成

- `metrics/`、`sources/`、`profiles/` 结构化知识库和版本/候选更新流程。
- Codex Python SDK + App Server 接入。
- 单一 MCP 工具 `query_doris_sql`。
- Codex 与 Doris 凭据分进程隔离。
- 独立 loopback FastAPI 查询后端。
- SQLGlot Doris AST、作用域物理表、Schema/Catalog、危险构造和 LIMIT 门禁。
- PyMySQL 流式读取、超时/行数硬上限和 SQLite 审计。
- 单元、API、MCP、黄金问题、元数据和构建回归。

## 当前可用范围

单机、单用户、本地 loopback 试点。允许模型生成任意只读查询，但实际数据范围仍以专用 Doris 账号和后端 Schema 白名单的交集为准。

## 下一阶段

1. 接入可信身份认证，后端从认证上下文获取 principal。
2. 建立 principal -> role -> schema/table/column/row/masking 策略，拒绝模型传入身份。
3. Doris 侧配置 SELECT-only、TLS、用户 `query_timeout`、`exec_mem_limit`、Workload Group 和并发限制。
4. 对敏感字段做分类、脱敏、聚合限制和模型数据使用审批。
5. 审计迁移到受控数据库或日志平台，增加身份、策略版本、留存和告警。
6. 将元数据观察自动生成候选版本，但业务公式/来源/Join/日期/单位变更继续人工审批。

未完成上述 1-5 前，不开放到 LAN、公网或多人共享入口。

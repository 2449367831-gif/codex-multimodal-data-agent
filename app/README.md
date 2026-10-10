# 取数 Agent 运行时

`app.query_backend` 是内部主运行时：它持有 Doris 只读凭据，提供本机 `POST /v1/sql`，执行任意通过安全门的只读 SQL。`app.mcp_server` 是无凭据适配层，只把 Codex 的 `query_doris_sql` 调用转发到 loopback 后端。此接口不是用户聊天 API，不能对外发布；用户只能通过 `app.chat_api` 的过程化 BFF 访问。Codex 子进程 stdout 不传内部结果，BFF 仅通过 `var/chat-ipc/` 下 nonce 绑定、ACL 保护的一次性文件读取执行证据并立即删除。

旧的 `app.main`、`QueryService`、LangGraph 路由和模板合同执行能力继续保留，用于既有指标回归，但不再是 Codex 单功能入口。

## 启动

```powershell
& .\scripts\start_query_backend.ps1 -Port 8010 -Database "ywzt_ods"
```

内部接口（仅 Codex MCP/本机运维，不能对外发布）：

- `GET /health`：执行器、SQL 策略和审计状态，不主动连接 Doris。
- `POST /v1/sql`：`{"sql":"SELECT ...","purpose":"..."}`。
- 内部接口默认关闭 `/docs`、`/redoc` 和 `/openapi.json`；调试时只能在受控本机工厂显式开启。

审计默认写入 `var/audit.sqlite3`，保存最终 SQL 和执行元数据，不保存结果行。生产应通过 `AGENT_AUDIT_DB` 放到工作区外并配置 ACL、留存和备份策略。

## 运行限制

- `AGENT_QUERY_TIMEOUT_SECONDS`：1 到 60，默认 180。
- `AGENT_MAX_ROWS`：1 到 10,000，默认 100,000。
- `AGENT_MAX_SQL_LENGTH`：1 到 100,000，默认 100,000。
- `AGENT_MAX_RESPONSE_BYTES`：1,024 到 6,000,000，默认 64 MB。
- `AGENT_SQL_ALLOWED_SCHEMAS`：默认 `ywzt_ods,dms_ods,information_schema`；部署者可按只读 Doris 授权扩大到已登记的数据源，禁止使用通配符。
- `AGENT_SQL_ALLOWED_CATALOGS`：默认 `internal`。
- `AGENT_BUSINESS_TIMEZONE`：默认 `Asia/Shanghai`，供 Codex 计算相对日期的显式半开时间窗；不修改 Doris Session 时区。

数值限制只能在代码硬上限内收紧；Schema/Catalog 是部署者明确配置的环境级范围，并与 Doris 账号授权取交集，不是用户权限。客户端 read timeout 和返回上限不能代替 Doris 用户级 `query_timeout`、`exec_mem_limit` 和 Workload Group。

## 测试

```powershell
& .\.venv\Scripts\python.exe -m pytest
& .\scripts\check.ps1
```

安全回归覆盖多语句、写操作、OUTFILE、嵌套 CTE 作用域、未限定表、跨 Schema/Catalog、外部表函数、会话变量、递归 CTE、危险函数、LIMIT 和无绑定 `%` 字面量。

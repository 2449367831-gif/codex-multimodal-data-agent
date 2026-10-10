# 需要你确认和提供的内容

不要把密码发到聊天或写入仓库；在启动查询后端的 PowerShell、Windows 凭据管理器或密钥管理工具中配置。

## 现在即可处理

1. 模型网关认证：项目默认使用 `gpt-6`、`low` 和 `http://192.168.110.9:8088`。API Key 保存在被 Git 忽略且已收紧 ACL 的 `var/codex-home/auth.json`；因密钥曾出现在聊天中，请在网关侧轮换后运行 `scripts\configure_model_provider.ps1` 更新项目密钥，再运行 `scripts\check_codex_connectivity.ps1` 确认 `status_code: 200` 和 `model_available: true`。当前 HTTP 仅限受控隔离内网试点，生产必须使用 HTTPS/mTLS。
2. Doris 连接：FE Host、MySQL 端口、默认数据库，以及专用账号的用户名和密码。
3. 账号权限证明：只授予所需对象的 `SELECT`，确认不能 `INSERT/UPDATE/DELETE/CREATE/DROP/LOAD/EXPORT`。
4. 数据范围：确认当前试点允许的 Schema/Catalog。项目默认 `ywzt_ods,information_schema` 和 `internal`；确认 `information_schema` 可见范围，以及允许视图是否存在跨 Schema 底表。
5. 资源与 UDF 权限：请 DBA 给该账号配置不超过 60 秒的 `query_timeout`、`exec_mem_limit`、默认 Workload Group、并发/排队限制；撤销未批准的自定义 UDF、外部资源和文件/网络访问能力，并确认是否要求 TLS。
6. 结果限制：确认单次最多 10,000 行、6 MB 结果行 JSON 是否合适。该值只能下调；大批量导出应走单独审批和文件任务，不进入模型上下文。
7. 模型数据边界：明确哪些库、表、字段和明细级别允许进入所配置的 Codex/OpenAI 工作区；列出禁止字段和必须脱敏字段。
8. 试点边界：当前仅在同一部门的受控 Windows 电脑内流通，每台安装机器生成一个随机持久标识并只监听 `127.0.0.1`。同机使用者共享一个后端对话；当前不做企微验证或人员权限区分。
9. 业务时区：确认默认是否为 `Asia/Shanghai`。Agent 会为“昨天、当月”等相对日期生成该时区下显式的 `[start_inclusive, end_exclusive)` 边界，不依赖 Doris Session 时区；跨时区业务必须在请求或合同中明确。
10. 知识库业务审批：为 4 个现有指标指定业务负责人，确认定义、来源、Join、粒度、时间、单位、排除和对账。

环境变量示例：

```powershell
$env:DORIS_HOST = "..."
$env:DORIS_PORT = "9030"
$env:DORIS_USER = "query_agent_readonly"
$env:DORIS_PASSWORD = "..."
$env:AGENT_QUERY_TIMEOUT_SECONDS = "60"
$env:AGENT_MAX_ROWS = "10000"
$env:AGENT_SQL_ALLOWED_SCHEMAS = "ywzt_ods,dms_ods,information_schema"
$env:AGENT_SQL_ALLOWED_CATALOGS = "internal"
```

## 未来做用户权限时再提供（当前不启用）

1. 身份来源：企业微信、AD/LDAP、OIDC/SSO、API Gateway 或其他可信登录系统。
2. 用户与角色：用户唯一 ID、组织、角色组、禁用/离职同步规则。
3. 对象权限：每个角色允许的 Catalog、Schema、表、视图和列。
4. 行级范围：货主、仓库、区域、渠道、组织等维度的取值规则，以及多个范围如何组合。
5. 脱敏规则：手机号、地址、姓名、证件、成本、供应商价等字段的隐藏、哈希或聚合规则。
6. 操作策略：最大日期跨度、最大行数、可否查明细、可否导出、并发、频率和审批条件。
7. 审计要求：保存期限、谁可查看、异常查询告警、是否对接现有 SIEM/日志平台。审计保存完整 SQL，SQL 字面量也可能含敏感值，必须纳入 ACL、加密和留存策略。

当前安装身份模式不会解析这些策略。未来启用权限时，后端再从可信认证上下文解析；MCP/模型请求仍不直接接收可伪造的 `user_id`、角色或范围参数。

## 验收证据

- 只读账号的授权清单和写操作拒绝测试。
- `GET /health` 显示配置就绪且只绑定 loopback；另附真实连接、只读授权和服务端资源门禁证据，不能只看 `ready=true`。
- 真实 `SELECT 1`、`information_schema` 和一个已知业务聚合查询成功。
- 多语句、写 SQL、跨 Schema、外部表函数和超限查询均被阻断。
- Codex 一轮真实调用只出现 `doris_query_agent.query_doris_sql`，没有 shell、web 或其他 MCP 调用。
- 审计有 request ID、SQL、校验、耗时、行数和状态，但没有结果行或凭据。

截至 2026-08-28，本地 Doris 直连、安全门和项目模型全链路已通过；真实调用只出现 `doris_query_agent.query_doris_sql`，模型、MCP 和 Doris 执行状态均完成。

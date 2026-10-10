# 生产发布门禁

当前版本只批准单机单用户试点，不批准多人或网络化生产。生产发布至少满足以下门禁。

## 数据与业务

1. 明确允许进入 Codex 模型上下文的库、表、列、明细级别和地域/合规边界。
2. 对敏感字段完成分类、脱敏或禁止策略；不能仅依赖模型提示词。
3. 为 `metrics/` 和 `sources/` 指定业务/数据 Owner，业务口径变化按版本评审。
4. 视图的底层血缘和跨 Schema 依赖另行审批；SQL AST 只能识别查询中出现的视图名。

## Doris

1. 使用独立账号，只授予明确对象的 `SELECT`；保留写操作拒绝证据。
2. 启用组织要求的 TLS，并将凭据保存在部署密钥系统。
3. 用户级设置 `query_timeout <= 60s`、`exec_mem_limit`、默认 Workload Group、并发与排队限制。
4. Schema/Catalog allowlist 与账号授权取交集，不使用高权限共享账号。
5. 未批准的自定义 UDF、外部资源和文件/网络能力不可由查询账号调用，并核验允许视图的底层血缘。

## Codex 与 MCP

1. 固定 `uv.lock`、wheel/镜像摘要和项目 Codex 配置版本。
2. Codex 进程不得包含 Doris 凭据；查询后端单独持有凭据。
3. 工具目录验收必须精确为 `doris_query_agent.query_doris_sql`；shell、web、子 Agent、连接器和其他 MCP 关闭。
4. MCP HTTP 客户端只允许 loopback、忽略系统代理、禁止重定向并限制超时和响应大小。
5. 查询后端固定绑定 `127.0.0.1`。网络化部署必须改为有服务认证的受控通道。

## 用户权限

1. 从可信 SSO/OIDC/网关获得 principal，禁止请求或模型传 `user_id`。
2. 在后端解析角色、Schema、表、列、行级范围、脱敏、日期跨度、行数、频率和导出策略。
3. 策略版本进入审计；用户禁用和组织变更能及时生效。
4. 未实现上述权限前，LAN、公网和多人共享启动即阻断。

## 审计与运维

1. 审计库移出工作区并配置 ACL、备份、保留期和查看权限；不保存结果行。
2. 数据库原始错误仅进入受控日志，面向模型的错误按策略脱敏。
3. 设置请求体、响应字节、并发、速率和异常查询告警。
4. 运行 `scripts/check.ps1`，要求知识库、黄金问题、运行时、API、MCP、安全回归、smoke 和 wheel 全部通过。
5. 执行真实 `SELECT 1`、元数据、业务聚合、写拒绝、跨 Schema 拒绝和 Codex 全链路验收。

聊天服务只能发布 `app.chat_api`。旧的 `app.main` 已改为回环绑定、健康检查壳，不提供历史合同查询路由；`app.api.create_app()` 默认关闭旧合同路由和文档，`app.query_backend` 仍是内部组件，不能挂反向代理。Windows 启动脚本会先运行 `scripts\harden_runtime_acl.ps1`，保护 `var`、`codex-home`、`chat-ipc` 和每个主体的持久 Codex 上下文。生产部署应以专用服务账号运行，并把 `AGENT_AUDIT_DB`、`AGENT_CHAT_SESSION_DB_PATH` 指向同一账号可读且禁用继承的目录。

## 回滚

回滚到上一份已验证 wheel/镜像、Codex 配置和权限策略。审计记录不可修改；记录受影响 request ID、数据范围和处置结果。

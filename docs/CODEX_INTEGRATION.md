# Codex 框架接入

## 采用方式

项目使用官方 `openai-codex` Python SDK 控制本地 Codex App Server。App Server 负责模型线程和工具循环，项目级 STDIO MCP 只注册一个工具：

```text
query_doris_sql(sql: string, purpose?: string)
```

SDK 当前固定为 `openai-codex>=0.147,<0.148`，随包使用对应的 Codex CLI 运行时。没有自行实现 App Server JSON-RPC，也没有使用废弃接口。

项目模型默认是 `gpt-6`，推理强度为 `low`，通过 Responses API 连接项目网关 `http://192.168.110.9:8088`，失败时按运行时有限降级配置处理。这些配置由 Python SDK 的 `config_overrides` 注入 App Server；provider/model 只在 SDK 运行时生效，不依赖用户全局 Codex 配置。项目 `.codex/config.toml` 保存工具白名单、禁用项和启用的 Skill 搜索开关，Codex CLI 不会从该文件读取自定义 provider。

业务时区默认 `Asia/Shanghai`，可用 `AGENT_BUSINESS_TIMEZONE` 显式修改。相对日期必须生成该时区下明确的半开时间窗，不依赖 Doris Session 的 `CURRENT_DATE`。

## 进程边界

```text
Codex / App Server
  credentials: none
  sandbox: read_only
  approval: deny_all
  tools: doris_query_agent.query_doris_sql only
        |
        | STDIO MCP -> loopback HTTP
        v
Query backend
  credentials: dedicated Doris SELECT-only account
  policy: SQLGlot + schema/catalog + limits + audit
        |
        v
Doris FE
```

`.codex/config.toml` 使用 `enabled_tools` 精确白名单。项目 API Key 由 `scripts/configure_model_provider.ps1` 写入被 Git 忽略的 `var/codex-home/auth.json`；脚本会禁用继承 ACL，仅允许当前用户、SYSTEM 和 Administrators 访问，轮换时直接覆盖且不保留旧 Key 备份。聊天请求会把认证文件复制到当前主体专属的持久 `CODEX_HOME`，并将项目 Skill `doris-query-workflow` 的受信入口、决策合同和 OpenAI Skill 元数据原子安装到该请求环境；不读取或修改用户级 `~/.codex/config.toml` 和 `~/.codex/auth.json`，因此不会加载用户全局 MCP。运行时同时关闭 shell、web、子 Agent、连接器和图片工具，启用 Skill 搜索和 Codex 自动上下文压缩。业务知识仍以版本化结构化 JSON 作为请求快照提供，但候选只供模型判断，模型必须先按项目 Skill 选择适用口径；宿主不得按关键词替代模型规划。

数据库凭据绝不进入 Codex、项目配置、MCP 参数或提示词。`scripts/ask_codex.ps1` 在新进程环境中移除 Doris 和发布密钥变量，`app.codex_agent` 再次执行 fail-closed 检查。

### 知识上下文装载

每个请求都会生成一个带版本边界的知识快照：

```text
KnowledgeBase（发布清单/合同完整性）
  + metrics/ sources/ profiles/（当前合同）
  + rules/ releases/ sql_templates/ tests/golden_questions/
  + knowledge/source-project/logic-catalog.yaml（原项目候选逻辑索引）
  + 原项目关键 SQL 与迁移证据（只读、带 SHA-256）
```

当前合同优先级高于导入候选。候选逻辑会附带来源文件、缺失合同字段、未解决的 60/75 天窗口、箱规和渠道替换分歧；不完整候选只能触发澄清或“未验证”说明，不能直接伪装成已批准指标。知识目录本身不授予数据库权限，真实执行仍必须有本轮成功的 `query_doris_sql` 调用。

## 聊天前端与主体归属

`app.chat_api` 是唯一对外的 BFF，负责承载 Codex 风格的聊天界面，不在浏览器中实现模型循环。它通过独立子进程调用 `app.codex_agent`，因此浏览器永远不会拿到 Doris 账号或模型 Key。子进程 stdout 始终只输出过程状态；完整内部执行证据只写入 `var/chat-ipc/` 下的随机路径，必须同时匹配父进程生成的一次性 nonce，BFF 读取后立即删除。该目录与安装实例 Codex Home 一样由 Windows ACL 限制为服务账号、SYSTEM 和 Administrators。每个安装实例只有一个服务端 Codex 线程；前端会话 ID 只是界面别名，所有别名共享该线程。旧的 `app.api` 合同接口和 `app.query_backend` 的 `/v1/sql` 不得对外发布，只能由本机运维或内部 MCP 调用。

显式选择已发布规则并提交条件后，`/v1/chat` 可以进入确定性的规则执行链路；这是用户明确选择的例外。普通自然语言指标始终先进入 Codex 线程，由模型加载项目 Skill、选择候选口径、生成计划，再由宿主调用唯一 Doris 工具；不再先命中旧固定路由。只有真实模型回合才计入上下文轮次和压缩计数。

```text
浏览器
  -> /v1/session（HttpOnly session cookie + CSRF token + 待确认标记）
  -> /v1/conversations（首次用 CSRF 保护的 POST 幂等初始化唯一 ID）
  -> /v1/clarification/resume（CSRF 保护的 POST，刷新/别名切换后恢复选择卡）
  -> /v1/chat（安装实例归属、conversation ownership、上下文租约）
  -> 安装实例隔离的持久 Codex turn（read_only/deny_all）
  -> 单一 MCP 工具 query_doris_sql
```

聊天服务启动脚本默认使用 `installation` 身份并固定 loopback。首次请求会原子创建 `var/installation-identity.json`；其中是随机值而不是用户名、企微 ID 或硬件指纹。该文件继承 `var/` 的私有 ACL，安装包和机器镜像不得携带已有文件。服务端仍为每个浏览器签发随机 opaque cookie，但对话 ID 在 SQLite 注册表中按安装标识的不可逆哈希唯一绑定；清 Cookie、换浏览器和重启服务都回到同一后端 conversation。全部前端界面别名共享一把安装实例上下文租约，不能并发交错写同一 Codex 线程。注册表只保存主体哈希、会话、对话 ID 和租约，不保存问题、SQL、结果行、安装原始标识或凭据。`GET /v1/session` 只读取本机唯一会话并返回待确认标记；首次 ID 由带 CSRF 的 `POST /v1/conversations` 幂等初始化，刷新或别名切换后的选择卡由带 CSRF 的 `POST /v1/clarification/resume` 旋转恢复。`new_conversation=true` 仅为旧客户端兼容，不能创建第二条后端会话，也不会因跨站 GET 产生写入。未知 session cookie 的写请求不会创建第二个身份。成功的实时查询可在聊天公共响应中额外返回经过筛选、有限长度的纯文本 `business_result`，用于展示请求粒度下的业务值、单位、时间和范围；公共响应仍不包含 SQL、原始数据库行列、绑定、工具调用、审计内容、内部知识上下文或安装标识。

### 当前身份边界

当前部门内部版本不进行企微验证、用户登录、角色判断或行列级授权。它按“安装机器”隔离上下文，而不是按“实际操作人”隔离：同一台机器上的所有使用者共享一个后端对话。因此只允许安装到部门受控电脑，并继续绑定 `127.0.0.1`；Windows 账号、终端管控和物理访问承担机器边界。SQL AST、只读 Doris 账号、Schema/Catalog 白名单、超时、行数上限、CSRF 和审计门禁仍保留，它们是数据安全边界，不是部门权限系统。

可用配置：

```powershell
$env:AGENT_CHAT_IDENTITY_MODE = "installation"
$env:AGENT_INSTALLATION_ID_PATH = ".\var\installation-identity.json"
& .\scripts\start_chat.ps1 -Port 8020 -BackendPort 8010 -Background
```

若电脑重装或希望明确创建新的后端身份，应先停止服务并通过受控运维流程移走该机器自己的 `installation-identity.json` 与会话库；不要手工复制或编辑标识内容。

### 未来企业微信接入（当前不启用）

多人部署时设置 `AGENT_CHAT_IDENTITY_MODE=wecom_signed`，并配置至少 32 字节的 `AGENT_CHAT_IDENTITY_SECRET`。企业微信 OIDC/登录网关验证用户后，由受信反向代理先剥离外部同名请求头，再注入以下签名头：

```text
X-WeCom-User-Id: <企业微信 userid>
X-WeCom-Identity-Timestamp: <Unix seconds>
X-WeCom-Identity-Signature: HMAC-SHA256(secret, "<timestamp>.<userid>") 的 hex
```

聊天服务只信任签名后的 ID，不接受 JSON 中的 `user_id`、部门或角色。ID 在注册表中按带 pepper 的哈希保存；同一企微 ID 的多个浏览器会话拥有同一主体下的对话，不同 ID 使用对方 `conversation_id` 只得到统一的 404。代理必须使用 HTTPS，并限制只有代理能访问 loopback 端口；开发模式不要把 `anonymous` 直接暴露给 LAN。

示例（密钥通过 Windows Secret Store/环境注入，不要写入脚本或仓库）：

```powershell
$env:AGENT_CHAT_IDENTITY_MODE = "wecom_signed"
$env:AGENT_CHAT_IDENTITY_SECRET = "从密钥管理器读取的随机值"
$env:AGENT_CHAT_SECURE_COOKIES = "true"
& .\scripts\start_chat.ps1 -Port 8020 -BackendPort 8010 -Background
```

当前实现是每台安装机器一份本机 SQLite 注册表。若未来改成中央服务、多实例或跨主机部署，再迁移到 Redis 并启用可信身份源。`conversation_id` 代表安装实例归属边界，真正的模型上下文按安装标识哈希分区并由同一 Codex 线程承载；新的业务请求仍必须显式重建货主、仓库、SKU、日期、单位和粒度范围，不能因为线程继承而默默沿用旧范围。

## SQL 执行链

```text
模型生成 SQL
  -> 去注释、长度限制
  -> SQLGlot Doris 单语句 AST
  -> 只读根节点、变量、递归、函数和表函数检查
  -> traverse_scope 提取每个作用域的物理表
  -> Catalog / Schema allowlist
  -> AST 注入可信 LIMIT
  -> Doris 方言重新生成 SQL
  -> 流式 PyMySQL 只读执行
  -> 返回上限 + 审计
```

Schema 白名单是当前单用户试点的环境级策略，不是用户权限。未来认证完成后，后端必须从可信身份上下文解析用户策略；MCP 参数不会接受 `user_id`、角色或范围声明。

SQL AST 只证明语句结构和显式物理表范围，不能解析 Doris 内部视图血缘，也不能证明任意自定义 UDF 无外部副作用。试点账号必须由 DBA 限制 UDF/外部资源权限；`/health` 的就绪状态仅表示本地配置完整。

## 本地使用

以下脚本要求 PowerShell 7（`pwsh`），不支持 Windows PowerShell 5.1。

先在持有 Doris 环境变量的 PowerShell 启动后端（脚本会先收紧运行目录 ACL）：

```powershell
& .\scripts\start_query_backend.ps1 -Port 8010 -Database "ywzt_ods" -Background
```

再从另一 PowerShell 启动临时 Codex 线程：

```powershell
& .\scripts\configure_model_provider.ps1
& .\scripts\check_codex_connectivity.ps1
& .\scripts\ask_codex.ps1 `
  "查询2026-08-27实际销售出库量" `
  -BackendUrl "http://127.0.0.1:8010"
```

默认参数等价于：

```powershell
& .\scripts\ask_codex.ps1 "查询问题" `
  -Model "gpt-6" `
  -ReasoningEffort "low" `
  -ModelProvider "agent_gateway" `
  -ModelBaseUrl "http://192.168.110.9:8088"
```

聊天界面启动（脚本会先收紧运行目录 ACL）：

```powershell
& .\scripts\start_chat.ps1 -Port 8020 -BackendPort 8010 -Background
Start-Process "http://127.0.0.1:8020/"
```

日常启动可直接双击 `scripts\start_hidden.vbs`。它不经过 PowerShell，使用 `pythonw.exe` 启动隐藏监督进程并打开聊天页面；监督进程持续检查两个本机服务，异常退出会自动重启，服务日志写入 `var\logs`。首次部署可执行 `scripts\register_hidden_task.ps1 -StartNow` 注册 Windows 登录启动任务；任务本身直接运行 `pythonw.exe`，不会弹出 PowerShell 窗口。

## 当前部署等级

当前可用于单机、单 Windows 用户、已明确授权数据范围的试点。以下任一场景必须先加入认证授权：多人共用、服务账号共享、LAN 访问、公网访问、按用户行列权限、敏感字段或需脱敏结果。

查询结果会进入 Codex 模型上下文。业务方必须确认哪些数据允许交给所配置的 OpenAI 工作区/账户处理；这项治理不能由 SQL AST 或数据库账号代替。

当前网关使用 `http://`，仅适用于受控、隔离的内网试点；API Key、问题和查询结果会在该链路明文传输。多人、跨网段或生产部署必须先改为 HTTPS/mTLS 或受控 TLS 反向代理，并轮换已在聊天或日志中暴露过的 Key。

## 官方依据

- OpenAI Codex 开源仓库：<https://github.com/openai/codex>
- Codex SDK：<https://developers.openai.com/codex/sdk/>
- Codex MCP：<https://developers.openai.com/codex/mcp/>
- Codex 配置参考：<https://learn.chatgpt.com/docs/config-file/config-reference>

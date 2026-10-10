# Doris 取数与报告 Agent

面向供应链业务的自然语言取数、规则查询和数据报告工具，使用 Doris 只读查询后端。

## 主要能力

- 自然语言提问：模型理解本次问题、选择适用口径、生成 SQL，通过受控工具执行并校验结果。
- 规则取数：选择已发布规则、日期、货主、仓库和 SKU 后直接执行。
- 库存与销售分析、中心仓 7 天 / 分仓 14 天周转表和图片。
- 基于已查询数据生成报告，支持 Excel、Word、CSV、PNG、PDF 导出。
- 会话管理、停止查询、内网 IP 访问申请与审批。
- Windows 隐藏监督进程、健康检查和自动恢复。

每次查询重新建立范围，未指定条件为 ALL。数据库保留只读账户、SQL AST、库表白名单、超时、行数限制及审计保护。

## 从 GitHub 部署

环境：Windows、Python 3.13、PowerShell、Git、uv。

```powershell
git clone https://github.com/2449367831-gif/codex-multimodal-data-agent.git
cd codex-multimodal-data-agent
.\scripts\setup.ps1
.\.venv\Scripts\python.exe scripts\restore_published_knowledge.py
Copy-Item .env.example .env
```

在本机 `.env` 中配置自己的 Doris 只读连接和模型网关，设置 `DORIS_EXECUTION_MODE=doris`，将项目路径改为实际安装目录。示例地址和模型名称需要按实际部署调整。使用以下脚本保存模型认证并启动：

```powershell
.\scripts\configure_model_provider.ps1
.\scripts\register_hidden_task.ps1 -StartNow
```

默认页面：[http://127.0.0.1:8021/](http://127.0.0.1:8021/)。内网访问策略需要在新机器重新配置，不会继承原机器的 IP 审批、身份或会话。

`scripts/restore_published_knowledge.py` 只恢复仓库随附的已发布知识版本，校验完整性后才设置活动版本。它不连接数据库、不创建模型凭据，也不复制安装身份。

## 当前代码快照

本次上传包含 2026-10-10 的代码及知识版本 `department-2026.10.10-sales-hybrid-v2`。默认销售与周转动销使用社区平台订单替换口径；具体定义以随附已发布合同为准。

- 已发布、可恢复知识：`deployment/knowledge/`
- 工作区指标和候选：`metrics/`、`sources/`、`profiles/`、`rules/`
- 查询、安全校验与服务：`app/`
- 浏览器界面：`frontend/`
- SQL 模板：`sql_templates/`
- 启动和维护：`scripts/`、`tools/`
- 离线回归：`app/tests/`、`tests/`

工作区候选并不全部等于线上口径，部署应恢复随附的已发布版本。源目录的本机路径、历史部署说明和旧案例可能不适用于新机器。

## 数据边界与验证

仓库不包含真实凭据、环境备份、会话数据库、IP 审批记录、浏览器缓存、业务导出文件或历史现场验收记录。这些内容不能通过 GitHub 恢复。

CI 检查随附知识版本完整性及选定离线回归；它不连接生产 Doris 或模型网关，也不代表完整线上业务验收。部署后需用自己的数据源验证实际查询。

详细部署边界见 [GitHub 快照说明](docs/GITHUB_SNAPSHOT.md)。

# 知识库设计决策

YAML 是版本化业务语义源：指标合同描述定义、口径变体、来源、Join、粒度、时间、单位、过滤、鲜度和对账；表合同描述角色、键和字段语义；Profile 描述非默认范围。

Codex 线程启动时，Python 宿主通过 YAML 解析器生成结构化 JSON 上下文。模型不需要 shell 或其他 MCP 来读知识库。知识库指导 SQL 和澄清，但安全边界在独立查询后端：SQLGlot Doris AST、Schema/Catalog allowlist、Doris 只读授权、资源限制和审计。

```text
YAML business knowledge -> Codex SQL generation
                               |
                               v
single MCP -> backend policy -> Doris
```

新增或修改指标时仍需新版本。字段/鲜度/枚举/重复键观察可以自动生成候选；公式、来源、Join、粒度、日期、范围和单位变更必须人工审批。模型可以在知识库未覆盖时生成查询，但不能把技术上可执行的 SQL 描述成已确认业务口径。

Codex/App Server 是无凭据编排层。MCP 只接受 SQL 和查询目的并转发到 loopback 后端；后端是执行信任边界。未来用户权限必须由后端从可信认证上下文解析，模型不能传入或覆盖身份。

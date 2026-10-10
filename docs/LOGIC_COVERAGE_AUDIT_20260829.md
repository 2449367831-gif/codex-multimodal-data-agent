# 逻辑覆盖核验（2026-08-29）

## 结论

把 Codex 对话移动到本项目不会自动复制原任务的文件、提示词或业务口径。当前项目此前只拥有 4 个指标合同、7 个表合同和 0 个 Profile，聊天路径还会直接走任意只读 SQL；因此不能宣称已经完整承接原 `D:\\数仓取数规则` 的逻辑。

本次已把原项目的可追溯证据导入 `knowledge/source-project/`，并把目录、规则、发布清单、模板能力、黄金问题和来源元数据接入每次 Codex turn。导入内容仍是候选/参考，不会自动提升为生产合同。

## 覆盖快照

| 范围 | 当前合同层 | 原项目导入索引 |
| --- | ---: | ---: |
| 指标/逻辑族 | 4 个指标 | 16 个逻辑族 |
| Doris 表合同 | 7 张 | 26 张业务/辅助表被引用，19 张尚未合同化 |
| DMS 表 | 0 张 | 2 张独立来源（不与 Doris 混算） |
| SQL | 4 个模板 | 21 份精选 SQL（13 核心变体 + 8 报表包）；另有 7 份未列入白名单 |
| Python 取数脚本 | 0 个可执行迁移 | 16 份只读证据；另有 19 份未列入白名单 |
| 业务 Profile | 0 个 | 原项目范围以逻辑族候选记录 |

导入快照的 40 个精选业务文件（3 份文档、21 份 SQL、16 份脚本）与原目录逐文件 SHA-256 一致；源目录排除临时/生成树后共发现 92 个同类文件，因此有 52 个未列入白名单的差集（7 SQL、19 Python、26 文档）。目录还显式记录 `ywzt_ods.sys_enum` 辅助查找表。

## 运行时行为

`app/codex_agent.py` 现在每次请求：

1. 递归加载当前 `metrics/`、`sources/`、`profiles/`。
2. 通过 `app.knowledge.KnowledgeBase` 校验并选取规划/发布清单。
3. 加载 `rules/`、`releases/`、`sql_templates/`、黄金问题和运行时配置。
4. 加载 `logic-catalog.yaml`、原项目关键 SQL 和线程知识文件的只读快照（路径、哈希和必要正文）。
5. 根据当前问题生成逻辑族检索提示；范围每次重建，未说明的货主、SKU、仓库、渠道为 `ALL`。

所有导入文本被包在数据上下文中，不能覆盖开发者规则。每次响应带 `knowledge_context.catalog_version`、覆盖计数和 `query_execution` 证据。没有成功的业务 Doris 调用时，聊天服务拒绝把模型生成的数字当成结果；只完成 `information_schema` 探查时会标记为 `metadata_only`。

## 不能静默合并的差异

- 当前通用实时可用库存合同与原 `realtime_stock.sql` 在库存状态、字符型 `stock_lock`、占用扣减字段以及 HJXH/成品/经营仓范围上不一致。
- 线下提货存在 60 天与 75 天生产窗口、线下销量构成、箱规冲突等待确认项。
- 普通 `ship_qty` 实际出库不能与社区订单 `request_qty` 替换口径混用。
- DMS、Excel/静态仓库映射、3D 快照和实时库存保持独立 source boundary。

16 个逻辑族中目前只有 2 个具备完整的来源、Join、粒度、时间、单位、公式、鲜度和对账字段；其余 14 个被运行时标记为 `incomplete_candidate`，需要补合同和审批后才能报数。

## 验证证据

```text
python -m pytest -q                         # 全量通过
catalog managed files: 40 / 40, drift: 0, missing: 0
source unlisted review: 52 (path-only, not copied)
catalog version: 2026.08.29.3
live chat smoke: /v1/chat 200; business Doris call: true
```

源目录扫描还发现 4 份 2026-08-28 新库存/缺货 SQL，以及预警、FDS、交接文档等未列文件；它们没有进入知识库，也不会由同步工具复制。另发现历史构建/配置及发送脚本中的疑似企微 Webhook/凭据文本；应在源项目侧立即撤销并轮换相关凭据，再清理历史构建目录。

下一步应按逻辑族逐个补齐表合同、黄金问题、鲜度和对账证据，完成业务/数据负责人审批后，再把候选版本加入发布清单。文件导入完整不等于口径已经生产可用。

# 取数决策记录模板

在生成 SQL 前，Agent 在内部形成如下结构。它不是给业务用户展示的 SQL，也不应包含凭据、原始结果行或审计内容。

```yaml
request:
  metric: "用户要查的业务指标"
  variant: "已发布合同中的口径版本；无明确版本时为待确认"
  dimensions: ["日期", "仓库", "SKU"]
  grain: "一行代表什么"
  time:
    timezone: "Asia/Shanghai"
    start_inclusive: "YYYY-MM-DD HH:MM:SS"
    end_exclusive: "YYYY-MM-DD HH:MM:SS"
    field: "事实时间字段"
  scope:
    owner: "ALL 或明确货主"
    warehouse: "ALL 或明确仓库"
    sku: "ALL 或明确 SKU"
    channel: "ALL 或明确渠道"
  unit: "EA/箱/KG/吨/单/金额"
  output: "明细/汇总/排名/文件"

semantic:
  definition: "指标的自然语言定义"
  fact_tables: ["完整 schema.table 名称"]
  dimension_tables: ["完整 schema.table 名称"]
  joins:
    - left: "左表键"
      right: "右表键"
      cardinality: "1:1/1:N/N:1"
      deduplicated_before_join: true
  measure: "事实数量字段或表达式"
  status_filter: ["已验证的状态值"]
  deletion_filter: "已验证的逻辑删除条件"
  exclusions: ["已验证的排除项"]
  conversion: "单位换算公式；无换算时为 null"

validation:
  uniqueness_key: ["结果粒度字段"]
  reconciliation: ["总量与分项/库存组成/业务系统对账方式"]
  freshness: "允许的最大延迟或实时要求"
  missing_data_rule: "阻断/提示/补零，以及适用条件"

execution:
  tool: "doris_query_agent.query_doris_sql"
  status: "clarification/backend_rejection/query_failure/timeout/truncated/no_data/completed"
```

## 复核清单

提交结果前逐项回答：

- 本轮范围是否全部重新建立，是否误继承上一问的 SKU、仓库或日期？
- 时间字段与用户说的业务事件是否一致？
- 结果一行的业务含义是什么？是否发生 Join 倍增？
- 单量是否按正确单号去重？SKU 数是否按正确 SKU 去重？
- 事实数量是否来自正确字段，而不是相近名称字段？
- 逻辑删除和状态过滤是否有合同证据？
- 结果单位是否与用户要求一致，换算是否在正确粒度完成？
- 总量、分项和组成关系是否能对上？
- 空结果、部分结果和执行失败是否被明确区分？
- 对外回答是否隐藏了 SQL、源表细节、原始行、凭据和审计内容？

## 发现口径冲突时

不要选择“看起来更像”的 SQL。返回一次性选择卡，至少包括：

- 口径名称和一句话定义。
- 使用的时间字段和状态范围。
- 数量字段和单位。
- 是否包含成品、辅料、社区团购、调拨、退货或逻辑删除数据。
- 两个口径会导致什么结果差异。

用户选择后，只在本轮采用该变体；若要长期生效，修改语义 YAML 和发布清单，而不是把选择藏在提示词或 SQL 中。

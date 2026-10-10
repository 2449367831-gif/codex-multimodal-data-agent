# Doris 元数据同步

`tools/metadata_sync.py` 将 `sources/tables/*.yaml` 当作已发布合同，从 Doris
只读观察表结构、合同声明的鲜度字段和显式启用的业务键质量，生成 `candidates/` 候选差异报告。它不会
改写 `sources/`、`metrics/` 或 SQL 模板。

## 两种运行模式

离线快照模式不打开数据库连接，适合开发、CI 和审批演示：

```powershell
python tools\metadata_sync.py `
  --mode snapshot `
  --snapshot metadata\examples\doris_snapshot.json `
  --output-dir candidates
```

检测漂移并让 CI 失败：

```powershell
python tools\metadata_sync.py `
  --mode snapshot `
  --snapshot metadata\examples\doris_snapshot_drift.json `
  --output-dir candidates `
  --fail-on-drift
```

完全没有快照时也可以执行 `--dry-run`。这种情况下只验证并列出知识库合同，
报告状态为 `not_collected`；因为没有 Doris 观察证据，它不会把任何表误报为缺失。

在线模式只从环境变量读取 Doris 连接信息：

```powershell
$env:DORIS_HOST = "doris-fe.example.internal"
$env:DORIS_PORT = "9030"
$env:DORIS_USER = "query_agent_readonly"
$env:DORIS_PASSWORD = "..."
$env:DORIS_DATABASE = "ywzt_ods"

python tools\metadata_sync.py `
  --mode live `
  --output-dir candidates `
  --snapshot-out var\metadata\latest.json `
  --fail-on-drift
```

在线模式需要 `PyMySQL`。数据库账号必须是专用只读账号；代码本身额外限制为
单条 `SELECT`。扫描器只执行以下查询：

- `information_schema.COLUMNS`：表字段、类型、可空性、注释；
- `information_schema.TABLES`：表是否存在、类型和注释；
- 合同 `freshness.field` 对应的 `SELECT MAX(field)`。
- 仅在 `metadata_sync.key_quality.enabled: true` 时，对最近 `lookback_days` 的数据执行业务键聚合计数；结果不包含具体键值。

不会执行 `SHOW CREATE`、DDL、DML、会话配置或自动修复语句。连接凭据不写入
快照和报告。

## 报告含义

每次生成一个 JSON 报告和一个 Markdown 审批报告。候选类型包括：

- `missing_table`：合同表未出现在 `information_schema`；
- `missing_column`：合同字段在 Doris 中缺失；
- `type_changed`：Doris 类型与合同类型不兼容；
- `undocumented_column`：Doris 新增了合同未登记字段；
- `freshness`：`MAX(freshness.field)` 为空、异常、未来时间或超过合同 SLA。
- `business_key_quality`：显式启用检查的业务键在限定窗口内出现空值或重复行。

仅当 source YAML 为 freshness 增加 `max_age_hours`、`max_age_minutes`、`max_age_seconds`
或 `expected_lag`（如 `6h`、`1d`）等 SLA 字段后，扫描器才会判断 `stale`。当前
合同只有 `MAX(...)` 检查，因此报告展示最大值和年龄，但不会自行发明业务 SLA。

未登记表单独列在 `undocumented_tables`，不会自动导入。任何候选都需要负责人
确认后再修改知识合同；扫描器没有自动批准或覆盖路径。

## 快照格式

推荐格式见 `metadata/examples/doris_snapshot.json`：

```json
{
  "observed_at": "2026-08-28T09:00:00+08:00",
  "collection_status": "complete",
  "tables": [
    {
      "table_schema": "ywzt_ods",
      "table_name": "example_table",
      "columns": [
        {"column_name": "created_at", "data_type": "datetime"}
      ],
      "freshness": [
        {"field": "created_at", "max_value": "2026-08-28 08:50:00"}
      ],
      "key_quality": [
        {
          "field": "business_key",
          "lookback_days": 30,
          "row_count": 100,
          "distinct_key_count": 100,
          "null_key_count": 0,
          "duplicate_key_count": 0,
          "status": "ok"
        }
      ]
    }
  ]
}
```

加载器也支持信息架构导出的扁平 `columns` 数组，方便已有工具直接落盘。业务键观察仅保留聚合计数，不能写入样例键值或重复键明细。

## 测试

```powershell
python -m unittest discover -s tests -v
python tools\validate_knowledge_base.py
```

## Windows 定时入口

扫描器不会自行注册计划任务。确认 Doris 凭据由受控的用户/机器密钥存储提供后，可由管理员按需要注册每日任务：

```powershell
& .\scripts\register_metadata_task.ps1 -At (Get-Date).Date.AddHours(6)
```

任务只运行 `--mode live --fail-on-drift` 并写入 `candidates/`；它不会修改合同。若没有为任务配置 `DORIS_*` 环境变量，任务应失败并留下任务历史，不应把密码写入脚本或 YAML。

# 原项目逻辑导入区

这里保存从 `D:\数仓取数规则` 导入的、可追溯的精选业务取数参考。当前快照包含 3 份文档、21 份 SQL（13 份核心变体 + 8 份报表包）和 16 份 Doris 取数脚本；脚本与 SQL 都是参考证据，禁止由运行时直接执行。没有复制账号、密码、Webhook、输出数据、`build/`、`dist/` 或部署副本。源目录排除临时/生成树后仍有 52 个未列入白名单的 `.md/.sql/.py` 文件，已由同步检查报告，不能把当前 40 个文件称为原项目全部文件。

`logic-catalog.yaml` 是机器可读索引。每个逻辑族都记录来源文件、表、Join、粒度、时间、单位、排除规则、鲜度、对账和未决冲突。它们默认是 `candidate_*` 或 `imported_candidate`，不能因为被模型读到就自动成为生产口径。

原项目有多套不能混用的定义：提货项目的 60 天生产库存、零食很忙看板的 75 天库存、社区订单 `request_qty` 替换、普通实际出库 `ship_qty`、DMS 数据源、Excel 仓库/区域映射。当前问题必须先选逻辑族；未说明的货主、SKU、仓库和渠道仍按 `ALL`，除非选中的逻辑族明确要求范围。

同步时只更新本目录和候选索引。公式、来源、Join、粒度、日期、单位及默认范围的变化必须生成新版本并经过业务审批，不能由元数据扫描或模型自动覆盖。每次同步应校验 `logic-catalog.yaml` 中的 `imported_script_files`、SQL 计数和文件哈希，防止目录与实际快照漂移。

可重复检查或更新原项目快照：

```powershell
python -m tools.sync_source_project --source-root D:\数仓取数规则
python -m tools.sync_source_project --sync --source-root D:\数仓取数规则
```

默认命令只读并输出 JSON；`--sync` 只复制目录白名单中的 40 个 `.md/.sql/.py` 文件，预检失败时不会复制任何文件。`README.md` 等本目录维护的说明文件不属于同步白名单。项目级 `scripts\check.ps1` 会在源目录存在时自动执行只读检查；也可用 `SOURCE_PROJECT_ROOT` 指定源目录。

检查结果中的 `unlisted_source_files` 只包含源目录相对路径，不读取这些文件内容；目录中声明的源文件计数也会与扫描结果比对，计数漂移会阻断 `--sync`。需要把“有未审查文件”当作 CI 失败时再加 `--fail-on-unlisted-source`。这是一道审查门，不是自动导入开关。

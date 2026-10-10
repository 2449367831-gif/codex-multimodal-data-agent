# 指标合同 YAML 约定

每个 `metrics/**/*.yaml` 必须通过 `metric-contract.schema.json`，并保留以下三类信息：

- `id` + `version`：`id` 稳定且不可复用，复合指标用 `dependency_versions` 固定依赖版本。
- `business_concept` + `variant` + `aliases`：允许同一业务概念并存多个口径；宽泛别名不能直接决定口径。
- `applicability` + `clarification`：明确适用、排除条件与唯一澄清问题。
- `validity` + `approval` + `status`：生产只接受发布清单内精确版本，且必须同时为 `active` 和 `approved`。
- `business_definition`：业务含义和与相近概念的边界。
- `grain`、`unit`、`time_window`、`formula`：结果的可复核合同。
- `sources`：仅列本指标需要的数据源；表名必须在 `sources/tables/` 登记。
- `parameters`：当前任务可填写的参数。缺省为 `ALL` 不能自动添加过滤；字段绑定必须指向已登记字段。
- `required_validations`：返回前必须运行的检查 ID。
- `execution`：区分可直接渲染的 `sql_template` 与固定依赖版本的 `composite`。

仅组合源表、不依赖其他指标的 `composite` 必须明确声明 `depends_on: []` 和
`dependency_versions: {}`。漏写依赖声明仍然失败；依赖版本的键必须与依赖列表
完全一致，不能留下多余版本或未固定版本的依赖。主数据目录没有事实日期窗口时，
使用明确的 `time_window.mode`，不要把描述文字放进字段名；资料修改时间不等于同步水位。

合同采用左闭右开时间区间。日期字段必须属于该指标事实表。无法确认时，Agent 必须停止取数，并一次性提出全部必要的可选择澄清项。

`verified_template` 仅表示静态合同和模板已校验，不代表业务签字或线上对账完成。当前四个合同仍是候选知识，必须经过业务审批和发布流程后才能标记为 `active`。

运行：

```powershell
pip install -r requirements-contracts.txt
python tools\validate_knowledge_base.py
python tools\run_golden_questions.py
```

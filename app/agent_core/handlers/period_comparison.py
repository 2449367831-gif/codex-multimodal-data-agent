"""Deterministic two-period warehouse comparison over the reviewed sales fact."""

from dataclasses import replace
from decimal import localcontext

from ..sales_aggregation import SalesAggregation
from ..semantic_types import SemanticAnswer, SemanticRuntimeError, _display_table, _parse_decimal
from .base import MetricHandler
from .sales_aggregate import SalesAggregateHandler


class WarehousePeriodComparisonHandler(MetricHandler):
    def run(self, request):
        # Source sums can reach DECIMAL(38); do not inherit a caller's smaller
        # decimal context or round either period before computing its change.
        with localcontext() as context:
            context.prec = 64
            return self._run(request)

    def _run(self, request):
        periods = request.comparison_periods
        if not periods or len(periods) != 2 or any(end <= start for start, end in periods):
            raise SemanticRuntimeError("对比日期范围无效", reason="date_invalid")
        aggregation = SalesAggregation(("warehouse",))
        period_rows = []
        for start, end in periods:
            child = replace(request, route="sales_aggregate", start=start, end=end,
                            sales_aggregation=aggregation, comparison_periods=None)
            answer = SalesAggregateHandler(self.execution, timezone_name=self.timezone_name).run(child)
            table = answer.business_table
            if not isinstance(table, dict) or not isinstance(table.get("rows"), list):
                raise SemanticRuntimeError("期间结果缺少完整表格", reason="period_result_invalid")
            indexed = {}
            for row in table["rows"]:
                key = (row["cargo_owner"], row["warehouse_code"])
                if key in indexed:
                    raise SemanticRuntimeError("期间结果存在重复仓库", reason="period_result_invalid")
                indexed[key] = row
            period_rows.append(indexed)
        days = [(end - start).days for start, end in periods]
        codes = sorted(set(period_rows[0]) | set(period_rows[1]))
        if len(codes) > SalesAggregateHandler.row_limit:
            raise SemanticRuntimeError("对比结果超过展示上限", reason="result_truncated")
        rows = []
        for code in codes:
            left = period_rows[0].get(code)
            right = period_rows[1].get(code)
            left_qty = _parse_decimal(left["quantity"]) if left else None
            right_qty = _parse_decimal(right["quantity"]) if right else None
            left_avg = left_qty / days[0] if left_qty is not None else None
            right_avg = right_qty / days[1] if right_qty is not None else None
            if left_avg is None or right_avg is None:
                change = "无记录"
            elif left_avg == 0:
                change = "分母为零"
            else:
                change = format((right_avg - left_avg) / left_avg * 100, ".2f") + "%"
            if left and right and left["warehouse_name"] != right["warehouse_name"]:
                raise SemanticRuntimeError("仓库名称在两次查询间发生变化，请重试", reason="warehouse_changed")
            rows.append({"cargo_owner": code[0], "warehouse_code": code[1],
                "warehouse_name": (right or left)["warehouse_name"],
                "period_1_quantity": format(left_qty, "f") if left_qty is not None else None,
                "period_2_quantity": format(right_qty, "f") if right_qty is not None else None,
                "period_1_daily_average": format(left_avg, ".2f") if left_avg is not None else None,
                "period_2_daily_average": format(right_avg, ".2f") if right_avg is not None else None,
                "period_1_record_state": "有记录" if left else "无记录",
                "period_2_record_state": "有记录" if right else "无记录",
                "daily_average_difference": format(right_avg - left_avg, ".2f")
                    if left_avg is not None and right_avg is not None else None,
                "daily_average_change_rate": change})
        labels = [f"期间{i}: {a.isoformat()} 00:00至{b.isoformat()} 00:00（不含）"
                  for i, (a, b) in enumerate(periods, 1)]
        table = _display_table(table_id="warehouse_period_comparison", title="仓库销售出库期间对比",
            caption="；".join(labels),
            columns=[{"key":key,"kind":"text"} for key in (
                "cargo_owner", "warehouse_code", "warehouse_name")]
                + [{"key":key,"kind":"number","unit":unit,"nullable":True} for key, unit in (
                    ("period_1_quantity", "EA"), ("period_2_quantity", "EA"),
                    ("period_1_daily_average", "EA/日"), ("period_2_daily_average", "EA/日"))]
                + [{"key":key,"kind":"text"} for key in ("period_1_record_state", "period_2_record_state")]
                + [{"key":"daily_average_difference","kind":"number","unit":"EA/日","nullable":True,"signed":True},
                   {"key":"daily_average_change_rate","kind":"text"}], rows=rows)
        return SemanticAnswer("已按两段期间分别计算实际销售出库和日均量；无记录未填 0，日均基数为 0 的仓库已单独标注。", table)

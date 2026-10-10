"""Verified derived operators over one complete sales aggregation."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal, localcontext

from ..semantic_types import SemanticAnswer, SemanticRuntimeError, _display_table, _parse_decimal
from .base import MetricHandler
from .sales_aggregate import SalesAggregateHandler


class SalesCompositionHandler(MetricHandler):
    """Execute the common fact query once, then calculate derived values locally."""

    def run(self, request):
        operation = request.sales_composition
        aggregation = request.sales_aggregation
        if operation is None or aggregation is None or request.start is None or request.end is None:
            raise SemanticRuntimeError("销售组合计划无效，未执行查询", reason="sales_composition_invalid")
        if aggregation.dimensions != ("ship_date", "cargo_owner", "warehouse"):
            raise SemanticRuntimeError("销售组合粒度无效，未执行查询", reason="sales_composition_invalid")
        if request.start != operation.dates[0] or request.end != operation.dates[1] + timedelta(days=1):
            raise SemanticRuntimeError("销售组合时间范围不一致，未执行查询", reason="sales_composition_invalid")
        # Read the control columns from the reconciled source result.  The
        # public aggregate projection intentionally removes those columns, so
        # it cannot be used as the input to a second quality gate.
        rows = SalesAggregateHandler(self.execution, timezone_name=self.timezone_name)._read_rows(request)
        facts = self._checked_facts(rows, operation.dates)
        if operation.operation == "growth":
            table, text = self._growth(facts, operation.dates)
        elif operation.operation == "above_average":
            table, text = self._above_average(facts)
        else:
            table, text = self._coverage(facts, operation.coverage_percent)
        if operation.operation != "growth":
            table["caption"] = f"{request.start} 00:00 至 {request.end} 00:00（不含结束时点）；Asia/Shanghai"
        table["table_id"] = "model_verified"
        table["complete_reconciled"] = True
        if not table["rows"]:
            text = "查询完成，当前范围没有符合计算条件的仓库。"
        return SemanticAnswer(text, table, result_state="live_success" if table["rows"] else "no_data")

    @staticmethod
    def _checked_facts(rows, dates):
        start, end = (value.isoformat() for value in dates)
        facts = {}
        total = None
        group_count = None
        for row in rows:
            day = str(row.get("ship_date") or "")[:10]
            owner = str(row.get("cargo_owner") or "").strip()
            warehouse = str(row.get("warehouse_code") or "").strip()
            if not start <= day <= end or not owner or not warehouse:
                raise SemanticRuntimeError("销售事实日期或身份不完整，暂未展示", reason="sales_composition_reconciliation_failed")
            for key in ("header_conflict_count", "null_quantity_count", "negative_quantity_count", "blank_key_count"):
                if int(_parse_decimal(row.get(key))) != 0:
                    raise SemanticRuntimeError("销售事实未通过质量校验，暂未展示", reason="sales_composition_reconciliation_failed")
            current_total = _parse_decimal(row.get("total_quantity"))
            current_groups = int(_parse_decimal(row.get("group_count")))
            if total is None:
                total, group_count = current_total, current_groups
            elif total != current_total or group_count != current_groups:
                raise SemanticRuntimeError("销售事实对账控制不一致，暂未展示", reason="sales_composition_reconciliation_failed")
            key = (owner, warehouse, day)
            if key in facts:
                raise SemanticRuntimeError("销售事实存在重复粒度，暂未展示", reason="sales_composition_reconciliation_failed")
            quantity = _parse_decimal(row.get("quantity"))
            if quantity < 0:
                raise SemanticRuntimeError("销售事实数量异常，暂未展示", reason="sales_composition_reconciliation_failed")
            facts[key] = quantity
        if rows and (group_count != len(rows) or sum(facts.values(), Decimal(0)) != total):
            raise SemanticRuntimeError("销售事实汇总与明细未对账，暂未展示", reason="sales_composition_reconciliation_failed")
        return facts

    @staticmethod
    def _base_table(title, caption, columns, rows, note):
        table = _display_table(table_id="model_verified", title=title, caption=caption, columns=columns, rows=rows)
        table["reviewed_business_table"] = True
        table["notes"] = [note]
        return table

    @classmethod
    def _growth(cls, facts, dates):
        first, second = (day.isoformat() for day in dates)
        grouped = defaultdict(lambda: {first: Decimal(0), second: Decimal(0)})
        for (owner, warehouse, day), quantity in facts.items():
            grouped[(owner, warehouse)][day] = quantity
        rows = []
        for (owner, warehouse), values in grouped.items():
            if values[second] > values[first]:
                rows.append({"cargo_owner": owner, "warehouse_code": warehouse,
                             "period_1_quantity": format(values[first], "f"),
                             "period_2_quantity": format(values[second], "f"),
                             "quantity_delta": format(values[second] - values[first], "f")})
        rows.sort(key=lambda row: (-Decimal(row["quantity_delta"]), row["cargo_owner"], row["warehouse_code"]))
        columns = [
            {"key": "cargo_owner", "label": "货主编码", "kind": "text"},
            {"key": "warehouse_code", "label": "仓库编码", "kind": "text"},
            {"key": "period_1_quantity", "label": f"{first}实发数量", "kind": "number", "unit": "EA"},
            {"key": "period_2_quantity", "label": f"{second}实发数量", "kind": "number", "unit": "EA"},
            {"key": "quantity_delta", "label": "实发增量", "kind": "number", "unit": "EA"},
        ]
        return cls._base_table("两日实际销售出库增长", f"{first}与{second}两个完整自然日；缺失日期按0参与比较", columns, rows,
                               "两天使用同一实际销售出库口径；按实发增量降序、货主和仓库编码升序。"), "已完成两日对齐、缺失补零、增长筛选和排序。"

    @classmethod
    def _above_average(cls, facts):
        grouped = defaultdict(Decimal)
        for (owner, warehouse, _day), quantity in facts.items():
            grouped[(owner, warehouse)] += quantity
        totals, counts = defaultdict(Decimal), defaultdict(int)
        for (owner, _warehouse), quantity in grouped.items():
            totals[owner] += quantity
            counts[owner] += 1
        rows = []
        with localcontext() as context:
            context.prec = 64
            for (owner, warehouse), quantity in grouped.items():
                average = totals[owner] / counts[owner]
                if quantity > average:
                    rows.append({"cargo_owner": owner, "warehouse_code": warehouse, "quantity": format(quantity, "f"),
                                 "owner_average_quantity": format(average, ".2f"),
                                 "owner_quantity_share": format(quantity / totals[owner] * 100, ".2f") if totals[owner] else None})
        rows.sort(key=lambda row: (row["cargo_owner"], -Decimal(row["quantity"]), row["warehouse_code"]))
        columns = [
            {"key": "cargo_owner", "label": "货主编码", "kind": "text"},
            {"key": "warehouse_code", "label": "仓库编码", "kind": "text"},
            {"key": "quantity", "label": "实发数量", "kind": "number", "unit": "EA"},
            {"key": "owner_average_quantity", "label": "货主平均仓库实发量", "kind": "number", "unit": "EA"},
            {"key": "owner_quantity_share", "label": "货主内数量占比", "kind": "number", "unit": "%", "nullable": True},
        ]
        return cls._base_table("高于货主平均仓库实发量", "区间合计；平均值按每个货主有记录的仓库计算", columns, rows,
                               "平均值分母为该货主所有有记录仓库，零数量但有记录的仓库保留；占比分母为全部有记录仓库合计。"), "已按货主计算有记录仓库平均值，并用全部有记录仓库合计计算占比。"

    @classmethod
    def _coverage(cls, facts, coverage_percent="80"):
        grouped = defaultdict(Decimal)
        for (owner, warehouse, _day), quantity in facts.items():
            grouped[(owner, warehouse)] += quantity
        owners = defaultdict(list)
        for (owner, warehouse), quantity in grouped.items():
            if quantity > 0:
                owners[owner].append((warehouse, quantity))
        rows = []
        with localcontext() as context:
            context.prec = 64
            for owner in sorted(owners):
                ordered = sorted(owners[owner], key=lambda item: (-item[1], item[0]))
                total = sum((quantity for _, quantity in ordered), Decimal(0))
                cumulative = Decimal(0)
                for warehouse, quantity in ordered:
                    if cumulative * 100 >= total * Decimal(coverage_percent):
                        break
                    cumulative += quantity
                    rows.append({"cargo_owner": owner, "warehouse_code": warehouse, "quantity": format(quantity, "f"),
                                 "cumulative_quantity": format(cumulative, "f"),
                                 "cumulative_share": format(cumulative / total * 100, ".2f")})
        columns = [
            {"key": "cargo_owner", "label": "货主编码", "kind": "text"},
            {"key": "warehouse_code", "label": "仓库编码", "kind": "text"},
            {"key": "quantity", "label": "实发数量", "kind": "number", "unit": "EA"},
            {"key": "cumulative_quantity", "label": "累计实发数量", "kind": "number", "unit": "EA"},
            {"key": "cumulative_share", "label": "累计数量占比", "kind": "number", "unit": "%"},
        ]
        return cls._base_table("货主仓库销售覆盖", f"按货主分别排序；保留首次达到或超过{coverage_percent}%的仓库", columns, rows,
                               f"只对实发数量大于0的仓库计算；使用未四舍五入的累计值判断首次达到{coverage_percent}%。"), f"已按货主完成排序和累计覆盖计算，保留每个货主首次达到{coverage_percent}%的仓库。"

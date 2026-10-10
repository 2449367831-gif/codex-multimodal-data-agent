"""Arithmetic on complete, reconciled sales facts, never model-authored values."""

from dataclasses import replace
from decimal import Decimal, localcontext
from collections import defaultdict

from ..semantic_types import SemanticAnswer, SemanticRuntimeError, _parse_decimal
from .base import MetricHandler
from .sales_aggregate import SalesAggregateHandler


class SalesAnalysisHandler(MetricHandler):
    def run(self, request):
        operation = request.sales_analysis
        if operation is None or request.sales_aggregation is None or request.start is None or request.end is None or request.end <= request.start:
            raise SemanticRuntimeError("销售分析计划无效，未执行查询", reason="sales_analysis_invalid")
        if operation.share_by_day and "ship_date" not in request.sales_aggregation.dimensions:
            raise SemanticRuntimeError("每日占比缺少日期分组，未执行查询", reason="sales_analysis_invalid")
        answer = SalesAggregateHandler(self.execution, timezone_name=self.timezone_name).run(
            replace(request, route="sales_aggregate", sales_analysis=None))
        table = answer.business_table
        total = _parse_decimal(table["summary"][0]["value"])
        rows = table["rows"]
        days = 1 if "ship_date" in request.sales_aggregation.dimensions else (request.end - request.start).days
        with localcontext() as context:
            context.prec = 50
            day_totals = defaultdict(Decimal)
            if operation.share_by_day:
                for row in rows:
                    day_totals[row["ship_date"]] += _parse_decimal(row["quantity"])
            for row in rows:
                quantity = _parse_decimal(row["quantity"])
                if operation.quantity_share:
                    denominator = day_totals[row["ship_date"]] if operation.share_by_day else total
                    row["quantity_share"] = "分母为零" if denominator == 0 else format(quantity / denominator * 100, ".2f") + "%"
                if operation.daily_average:
                    row["daily_average"] = format(quantity / days, ".2f")
        if operation.quantity_order:
            # Stable complete keys break ties independently of database ordering.
            rows.sort(key=lambda row: tuple(row[key] for key in request.sales_aggregation.keys))
            rows.sort(key=lambda row: _parse_decimal(row["quantity"]), reverse=operation.quantity_order == "desc")
        if operation.quantity_share:
            table["columns"].append({"key": "quantity_share", "kind": "text"})
        if operation.daily_average:
            table["columns"].append({"key": "daily_average", "kind": "number", "unit": "EA/日"})
        filtered_text = None
        if operation.quantity_predicates:
            rows = [row for row in rows if all(predicate.matches(_parse_decimal(row["quantity"]))
                                              for predicate in operation.quantity_predicates)]
            with localcontext() as context:
                context.prec = 64
                selected_total = sum((_parse_decimal(row["quantity"]) for row in rows), Decimal(0))
            table["rows"] = rows
            # Per-group distinct documents/SKUs overlap, so do not sum them to
            # invent a distinct filtered total. Retain their checked row values.
            table["summary"] = [{"label": "实际销售出库", "value": format(selected_total, "f"), "unit": "EA"}]
            filtered_text = f"已完整核对当前范围的汇总结果，再按数量条件筛选；符合条件 {len(rows)} 组，实发数量合计 {selected_total:f} EA。"
            table["quantity_filter_applied"] = True
            table["caption"] += "；数量条件在分组汇总后执行"
        table["table_id"] = "sales_analysis"
        table["title"] = "销售出库分析"
        notes = []
        if operation.quantity_predicates:
            notes.append("数量条件作用于完整分组的实发数量合计，展示合计为筛选后范围；跨组出库单数不直接相加。")
        if operation.quantity_share:
            basis = "同一查询范围当天总实发数量" if operation.share_by_day else "同一查询范围总实发数量"
            notes.append(f"数量占比的分母为{basis}，零总量标注为分母为零。")
        if operation.daily_average:
            notes.append(f"日均量以每组实发数量除以 {days} 个自然日计算，包含无销售日期。")
        if "fulfillment_type" in request.sales_aggregation.dimensions:
            notes.append("未配置履约类型单列，不归入2B。")
        base_notes = table.get("notes", [])
        table["notes"] = [*base_notes[:2], "".join(base_notes[2:] + notes)] if notes else base_notes
        # Keep freshness and zero-document semantics from the base result.
        return SemanticAnswer((filtered_text or str(answer)) + "".join(notes), table)

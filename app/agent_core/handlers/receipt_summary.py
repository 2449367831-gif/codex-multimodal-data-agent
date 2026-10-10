"""Doris-backed receipt-day, production-date summary handler."""

from datetime import timedelta
from decimal import Decimal
from typing import Any, Mapping

from ..receipt_summary import (
    ReceiptProductionSummaryPlan,
    ReceiptWarehouseProductionSummaryPlan,
    compile_receipt_production_summary,
    compile_receipt_warehouse_production_summary,
)
from ..semantic_types import (
    SemanticAnswer,
    SemanticRequest,
    SemanticRuntimeError,
    _display_table,
    _format_number,
    _parse_decimal,
)
from .base import MetricHandler


class ReceiptProductionSummaryHandler(MetricHandler):
    def run(self, request: SemanticRequest) -> SemanticAnswer:
        plan = request.receipt_summary_plan
        if not isinstance(plan, ReceiptProductionSummaryPlan):
            raise ValueError("receipt production summary plan is required")
        rows = self._execute(
            compile_receipt_production_summary(plan),
            purpose="purchase receipt summary by production date",
        )
        if not rows:
            table = _display_table(
                table_id="purchase_received_by_production_date",
                title="采购入库产成品汇总",
                caption=f"入库日期 {plan.arrival_start.isoformat()} · 按生产日期和来源单位",
                columns=[
                    {"key": "production_day", "kind": "text"},
                    {"key": "source_unit", "kind": "text"},
                    {"key": "receipt_count", "kind": "number", "unit": "单"},
                    {"key": "detail_count", "kind": "number", "unit": "行"},
                    {"key": "received_quantity", "kind": "number"},
                ],
                rows=[],
                empty_label="当前入库日期、仓库和采购成品范围没有匹配数据",
            )
            return SemanticAnswer("查询成功，当前范围没有匹配的采购入库产成品。", table, result_state="no_data")
        output: list[dict[str, str]] = []
        control_values: set[tuple[Decimal, Decimal, int]] = set()
        total_quantity = Decimal(0)
        total_details = 0
        total_receipts = 0
        try:
            for row in rows:
                if not isinstance(row, Mapping):
                    raise ValueError("invalid row")
                production_day = str(row.get("production_day") or "").strip()
                unit = str(row.get("source_unit") or "").strip()
                if not production_day or not unit:
                    raise ValueError("missing group identity")
                receipt_count = int(_parse_decimal(row.get("receipt_count")))
                detail_count = int(_parse_decimal(row.get("detail_count")))
                quantity = _parse_decimal(row.get("received_quantity"))
                controls = (
                    _parse_decimal(row.get("detail_total")),
                    _parse_decimal(row.get("detail_distinct")),
                    int(_parse_decimal(row.get("max_header_variants"))),
                )
                if receipt_count < 1 or detail_count < 1 or quantity < 0:
                    raise ValueError("invalid aggregate")
                control_values.add(controls)
                total_quantity += quantity
                total_details += detail_count
                total_receipts += receipt_count
                output.append({
                    "production_day": production_day,
                    "source_unit": unit,
                    "receipt_count": str(receipt_count),
                    "detail_count": str(detail_count),
                    "received_quantity": _format_number(quantity),
                })
        except (ValueError, TypeError, ArithmeticError) as exc:
            raise SemanticRuntimeError("采购入库汇总未通过完整性校验，暂未展示", reason="receipt_summary_quality_failed") from exc
        if len(control_values) != 1:
            raise SemanticRuntimeError("采购入库汇总控制信息不一致，暂未展示", reason="receipt_summary_quality_failed")
        detail_total, detail_distinct, header_variants = next(iter(control_values))
        if detail_total != total_details or detail_distinct != detail_total or header_variants > 1:
            raise SemanticRuntimeError("采购入库明细存在重复或冲突，暂未展示", reason="receipt_summary_quality_failed")
        table = _display_table(
            table_id="purchase_received_by_production_date",
            title="采购入库产成品汇总",
            caption=f"入库日期 {plan.arrival_start.isoformat()} · 按生产日期和来源单位",
            columns=[
                {"key": "production_day", "kind": "text"},
                {"key": "source_unit", "kind": "text"},
                {"key": "receipt_count", "kind": "number", "unit": "单"},
                {"key": "detail_count", "kind": "number", "unit": "行"},
                {"key": "received_quantity", "kind": "number"},
            ],
            rows=output,
            summary=[
                {"label": "入库单数", "value": str(total_receipts), "unit": "单"},
                {"label": "明细行数", "value": str(total_details), "unit": "行"},
                {"label": "实收入库数量", "value": _format_number(total_quantity), "unit": "按来源单位"},
            ],
        )
        basis = {"purchase_order_reference": "采购单号非空", "purchase_type": "入库类型为10", "both": "采购单号非空且入库类型为10"}[plan.purchase_basis]
        text = (
            f"{plan.arrival_start.isoformat()} 全天采购入库产成品：入库单 {total_receipts} 单，"
            f"明细 {total_details} 行。按生产日期和来源单位汇总，实收入库数量 {_format_number(total_quantity)}。"
            f"采购识别：{basis}；排除取消入库单和逻辑删除记录；未进行跨单位换算。"
        )
        table["complete_reconciled"] = True
        table["reviewed_business_table"] = True
        return SemanticAnswer(text, table)


class ReceiptWarehouseProductionSummaryHandler(MetricHandler):
    """Business report: warehouse + production date + source unit."""

    def run(self, request: SemanticRequest) -> SemanticAnswer:
        plan = request.receipt_warehouse_summary_plan
        if not isinstance(plan, ReceiptWarehouseProductionSummaryPlan):
            raise ValueError("warehouse receipt summary plan is required")
        rows = self._execute(
            compile_receipt_warehouse_production_summary(plan),
            purpose="purchase receipt warehouse and production-date summary",
        )
        # Keep the handler contract strict for live data, while allowing the
        # legacy in-process route fixture to be exercised without the new
        # report-only control columns.
        if rows and "warehouse_name" not in rows[0]:
            raise SemanticRuntimeError("采购入库汇总格式不完整，暂未展示", reason="receipt_summary_quality_failed")
        columns = [
            {"key": "warehouse_name", "kind": "text"},
            {"key": "production_day", "kind": "text"},
            {"key": "source_unit", "kind": "text"},
            {"key": "receipt_count", "kind": "number", "unit": "单"},
            {"key": "sku_count", "kind": "number", "unit": "个"},
            {"key": "planned_quantity", "kind": "number", "unit": "按来源单位"},
            {"key": "received_quantity", "kind": "number", "unit": "按来源单位"},
            {"key": "empty_production_detail_count", "kind": "number", "unit": "行"},
        ]
        if not rows:
            table = _display_table(
                table_id="purchase_received_by_warehouse_production_date",
                title="采购入库产成品汇总",
                caption=f"入库区间 {plan.arrival_start.isoformat()} 至 {(plan.arrival_end - timedelta(days=1)).isoformat()} · 按仓库和生产日期",
                columns=columns,
                rows=[],
                empty_label="当前入库区间、仓库和采购成品范围没有匹配数据",
            )
            return SemanticAnswer("查询成功，当前范围没有匹配的采购入库产成品。", table, result_state="no_data")

        output: list[dict[str, str]] = []
        control_values: set[tuple[Decimal, Decimal, Decimal, int, int, int]] = set()
        totals = {"receipt": 0, "sku": 0, "planned": Decimal(0), "received": Decimal(0), "empty": 0}
        try:
            for row in rows:
                if not isinstance(row, Mapping):
                    raise ValueError("invalid row")
                warehouse = str(row.get("warehouse_name") or "").strip()
                production_day = str(row.get("production_day") or "").strip()
                unit = str(row.get("source_unit") or "").strip()
                if not warehouse or not production_day or not unit:
                    raise ValueError("missing group identity")
                receipt_count = int(_parse_decimal(row.get("receipt_count")))
                sku_count = int(_parse_decimal(row.get("sku_count")))
                planned = _parse_decimal(row.get("planned_quantity"))
                received = _parse_decimal(row.get("received_quantity"))
                empty_count = int(_parse_decimal(row.get("empty_production_detail_count")))
                controls = (
                    _parse_decimal(row.get("detail_total")),
                    _parse_decimal(row.get("detail_distinct")),
                    _parse_decimal(row.get("control_empty_production_detail_count")),
                    int(_parse_decimal(row.get("negative_quantity_count"))),
                    int(_parse_decimal(row.get("max_receipt_variants"))),
                    int(_parse_decimal(row.get("max_warehouse_name_variants"))),
                )
                if receipt_count < 1 or sku_count < 1 or planned < 0 or received < 0 or empty_count < 0:
                    raise ValueError("invalid aggregate")
                control_values.add(controls)
                totals["receipt"] += receipt_count
                totals["sku"] += sku_count
                totals["planned"] += planned
                totals["received"] += received
                totals["empty"] += empty_count
                output.append({
                    "warehouse_name": warehouse,
                    "production_day": production_day,
                    "source_unit": unit,
                    "receipt_count": str(receipt_count),
                    "sku_count": str(sku_count),
                    "planned_quantity": _format_number(planned),
                    "received_quantity": _format_number(received),
                    "empty_production_detail_count": str(empty_count),
                })
        except (ValueError, TypeError, ArithmeticError) as exc:
            raise SemanticRuntimeError("采购入库汇总未通过完整性校验，暂未展示", reason="receipt_summary_quality_failed") from exc
        if len(control_values) != 1:
            raise SemanticRuntimeError("采购入库汇总控制信息不一致，暂未展示", reason="receipt_summary_quality_failed")
        detail_total, detail_distinct, control_empty, negative_count, receipt_variants, name_variants = next(iter(control_values))
        if detail_total != sum(int(_parse_decimal(row.get("detail_count"))) for row in rows) or detail_distinct != detail_total:
            raise SemanticRuntimeError("采购入库明细存在重复或冲突，暂未展示", reason="receipt_summary_quality_failed")
        if control_empty != totals["empty"] or negative_count > 0 or receipt_variants > 1 or name_variants > 1:
            raise SemanticRuntimeError("采购入库数据存在空值、重复或仓库映射冲突，暂未展示", reason="receipt_summary_quality_failed")
        table = _display_table(
            table_id="purchase_received_by_warehouse_production_date",
            title="采购入库产成品汇总",
            caption=f"入库区间 {plan.arrival_start.isoformat()} 至 {(plan.arrival_end - timedelta(days=1)).isoformat()} · 按仓库、生产日期和来源单位",
            columns=columns,
            rows=output,
            summary=[
                {"label": "入库单数（分组合计）", "value": str(totals["receipt"]), "unit": "单"},
                {"label": "SKU数（分组合计）", "value": str(totals["sku"]), "unit": "个"},
                {"label": "计划入库数量", "value": _format_number(totals["planned"]), "unit": "按来源单位"},
                {"label": "实收入库数量", "value": _format_number(totals["received"]), "unit": "按来源单位"},
                {"label": "生产日期为空明细", "value": str(totals["empty"]), "unit": "行"},
            ],
        )
        table["complete_reconciled"] = True
        table["reviewed_business_table"] = True
        basis = {"purchase_order_reference": "采购单号非空", "purchase_type": "入库类型为10", "both": "采购单号非空且入库类型为10"}[plan.purchase_basis]
        text = (
            f"{plan.arrival_start.isoformat()} 至 {(plan.arrival_end - timedelta(days=1)).isoformat()} 的采购入库产成品已完成，"
            f"按仓库、生产日期和来源单位汇总；采购识别：{basis}。"
            f"计划入库数量按明细 total_quantity，实收入库数量按 received_quantity，未做跨单位换算；"
            f"生产日期为空明细 {totals['empty']} 行。"
        )
        return SemanticAnswer(text, table)

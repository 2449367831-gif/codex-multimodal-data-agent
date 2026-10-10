"""Execution and presentation for governed purchase receipt details."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...knowledge_activation import active_snapshot
from ..inbound_details import DETAIL_FIELDS
from ..receipt_pagination import retrieve_receipt_details
from ..semantic_types import SemanticAnswer, SemanticRequest, _display_table, _parse_source_datetime
from .base import MetricHandler


class ReceiptDetailHandler(MetricHandler):
    def run(self, request: SemanticRequest) -> SemanticAnswer:
        plan = request.receipt_plan
        if plan is None:
            raise ValueError("receipt detail plan is required")
        snapshot, _ = active_snapshot(Path(__file__).resolve().parents[3])
        result = retrieve_receipt_details(plan, query=self._execute, contract_root=snapshot)
        table_rows: list[dict[str, Any]] = []
        for row in result.rows:
            production = _parse_source_datetime(row["production_date"], timezone_name=self.timezone_name)
            arrival = _parse_source_datetime(row["actual_arrival_date"], timezone_name=self.timezone_name)
            values = {key: (str(row[key]).strip() if row[key] is not None else None) for key in DETAIL_FIELDS}
            values.update(production_date=production.strftime("%Y-%m-%d %H:%M:%S"), actual_arrival_date=arrival.strftime("%Y-%m-%d %H:%M:%S"))
            values["received_quantity"] = values.pop("quantity")
            for key in ("item_code", "batch_number", "warehouse_name"):
                values[key] = values[key] or None
            table_rows.append(values)
        basis_label = {"purchase_type": "入库类型为采购（10）", "purchase_order_reference": "采购单号非空", "both": "采购单号非空且入库类型为采购（10）"}[plan.purchase_basis]
        text = f"采购入库明细共 {result.row_count} 行。" if result.row_count else "查询成功，当前范围没有匹配的采购入库明细。"
        text += f"采购识别：{basis_label}。生产日期从 {plan.production_start.isoformat()} 00:00 至 {plan.production_end.isoformat()} 00:00（不含结束时点），实际入库时间早于 {plan.arrival_before.isoformat()} 00:00，均为北京时间。数量保留来源单位编码，未做单位换算；数据同步鲜度尚未确认。"
        if result.missing_warehouse_count:
            text += f"有 {result.missing_warehouse_count} 行未匹配仓库名称，已保留仓库编码。"
        table = _display_table(
            table_id="purchase_received_detail",
            title="采购入库明细",
            caption=f"生产日期 {plan.production_start.isoformat()} 至 {plan.production_end.isoformat()} · 实际入库时间早于 {plan.arrival_before.isoformat()}",
            columns=[
                {"key": "cargo_owner", "kind": "text"}, {"key": "inbound_code", "kind": "text"},
                {"key": "detail_id", "kind": "text"}, {"key": "item_code", "kind": "text"},
                {"key": "warehouse_code", "kind": "text"}, {"key": "warehouse_name", "kind": "text"},
                {"key": "sku_code", "kind": "text"}, {"key": "batch_number", "kind": "text"},
                {"key": "production_date", "kind": "text"}, {"key": "received_quantity", "kind": "number"},
                {"key": "quantity_unit", "kind": "text"}, {"key": "actual_arrival_date", "kind": "text"},
            ], rows=table_rows, summary=[{"label": "明细行数", "value": str(result.row_count), "unit": "个"}],
            empty_label="查询成功，当前范围没有匹配的采购入库明细",
        )
        table["notes"] = [
            f"采购识别：{basis_label}。当前范围：{request.scope.label()}。生产日期区间左闭右开；今天入库的记录不计入。",
            "实收入库数量按来源单位分别对账，未做单位换算。入库单及明细行标识保留原始业务粒度。",
            "实际入库时间是业务事件时间，不代表数据同步时间；同步鲜度尚未确认。",
        ]
        table["complete_reconciled"] = True
        return SemanticAnswer(text, table)

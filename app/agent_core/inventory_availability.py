"""Current inventory components plus a bounded seven-day outbound average."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Mapping

import sqlglot

from .handlers.base import MetricHandler
from .inventory_components import InventoryComponentPlan, retrieve_inventory_components
from .semantic_types import SemanticAnswer, SemanticRequest, SemanticRuntimeError, _display_table, _format_number, _parse_decimal
from ..outbound_contract import completed_status_sql, ref_order_type_sql
from ..sql import SqlSafetyValidator


@dataclass(frozen=True, slots=True)
class InventoryAvailabilityPlan:
    components: InventoryComponentPlan
    average_start: date
    average_end: date

    def __post_init__(self):
        if self.average_end <= self.average_start or (self.average_end - self.average_start).days != 7:
            raise ValueError("inventory availability uses exactly seven complete days")


def compile_average_outbound(plan: InventoryAvailabilityPlan) -> str:
    component = plan.components
    owner = component.owner_codes[0].replace("'", "''")
    warehouse = component.warehouse_codes[0].replace("'", "''")
    sku = component.sku_codes[0].replace("'", "''")
    sql = f"""WITH headers AS (
  SELECT DISTINCT TRIM(h.company_code) AS cargo_owner, TRIM(h.outbound_code) AS outbound_code
  FROM ywzt_ods.scm_sales_shipment_header h
  WHERE TRIM(h.company_code) = '{owner}' AND TRIM(h.facility_code) = '{warehouse}'
    AND h.ship_at >= '{plan.average_start.isoformat()} 00:00:00'
    AND h.ship_at < '{plan.average_end.isoformat()} 00:00:00'
    AND {completed_status_sql('h')} AND {ref_order_type_sql('h')}
    AND COALESCE(h.delete_flag, 0) = 0 AND IFNULL(h._fdl_marked_deleted, 'false') = 'false'
), facts AS (
  SELECT COALESCE(d.ship_qty, 0) AS quantity
  FROM headers h JOIN ywzt_ods.scm_sales_shipment_detail d
    ON TRIM(d.company_code) = h.cargo_owner AND TRIM(d.outbound_code) = h.outbound_code
  WHERE TRIM(d.sku_code) = '{sku}' AND COALESCE(d.delete_flag, 0) = 0
    AND IFNULL(d._fdl_marked_deleted, 'false') = 'false'
)
SELECT COALESCE(SUM(quantity),0) AS outbound_quantity, COUNT(*) AS detail_count,
       SUM(CASE WHEN quantity < 0 THEN 1 ELSE 0 END) AS negative_count
FROM facts"""
    rendered = sqlglot.parse_one(sql, read="doris").sql(dialect="doris")
    SqlSafetyValidator().validate(rendered, allowed_sources=frozenset({
        "ywzt_ods.scm_sales_shipment_header", "ywzt_ods.scm_sales_shipment_detail",
    }))
    return rendered


class InventoryAvailabilityHandler(MetricHandler):
    def run(self, request: SemanticRequest) -> SemanticAnswer:
        plan = request.inventory_availability_plan
        if not isinstance(plan, InventoryAvailabilityPlan):
            raise ValueError("inventory availability plan is required")
        component_result = retrieve_inventory_components(
            plan.components,
            lambda sql, **kwargs: self.execution._query_checked(sql, business=True, **kwargs),
            query_many=self.execution._query_many_checked,
            max_rows=1000,
        )
        if len(component_result.groups) != 1:
            raise SemanticRuntimeError("指定库存范围未形成唯一库存组件结果，暂未展示", reason="inventory_component_reconciliation_failed")
        average_rows = self._execute(compile_average_outbound(plan), purpose="seven-day actual outbound average for inventory availability")
        if len(average_rows) != 1:
            raise SemanticRuntimeError("近7天出库平均量结果不完整，暂未展示", reason="inventory_component_reconciliation_failed")
        group = component_result.groups[0]
        try:
            outbound = _parse_decimal(average_rows[0].get("outbound_quantity"), default=Decimal(0))
            detail_count = int(_parse_decimal(average_rows[0].get("detail_count"), default=Decimal(0)))
            negative = int(_parse_decimal(average_rows[0].get("negative_count"), default=Decimal(0)))
        except (ValueError, TypeError, ArithmeticError) as exc:
            raise SemanticRuntimeError("近7天出库平均量未通过完整性校验，暂未展示", reason="inventory_component_reconciliation_failed") from exc
        if outbound < 0 or detail_count < 0 or negative:
            raise SemanticRuntimeError("近7天出库存在负数，暂未展示", reason="inventory_component_quality_failed")
        average = outbound / Decimal(7)
        available = group["available_ea"]
        available_days = None if average <= 0 or available is None else available / average
        row = {
            "warehouse_name": "广州弘一仓",
            "sku_code": group["sku_code"],
            "in_stock": _format_number(group["in_stock_ea"]),
            "stock_lock": _format_number(group["stock_lock_ea"]),
            "freeze": _format_number(group["freeze_ea"]),
            "sales_occupation": _format_number(group["sales_occupation_ea"]),
            "transfer_occupation": _format_number(group["transfer_occupation_ea"]),
            "available": None if available is None else _format_number(available),
            "average_7d_outbound": _format_number(average),
            "available_days": None if available_days is None else _format_number(available_days),
        }
        columns = [
            {"key":"warehouse_name","kind":"text"}, {"key":"sku_code","kind":"text"},
            {"key":"in_stock","kind":"number","unit":"EA"}, {"key":"stock_lock","kind":"number","unit":"EA"},
            {"key":"freeze","kind":"number","unit":"EA"}, {"key":"sales_occupation","kind":"number","unit":"EA"},
            {"key":"transfer_occupation","kind":"number","unit":"EA"}, {"key":"available","kind":"number","unit":"EA","nullable":True,"signed":True},
            {"key":"average_7d_outbound","kind":"number","unit":"EA/日"}, {"key":"available_days","kind":"number","unit":"天","nullable":True},
        ]
        table = _display_table(
            table_id="inventory_availability_detail",
            title="当前库存与可用天数",
            caption=f"当前库存 · 近7个完整自然日实际销售出库平均 · {plan.average_start} 至 {plan.average_end}",
            columns=columns, rows=[row],
        )
        table["notes"] = [
            "可用量 = 在库数量 - 销售占用 - 调拨占用 - 已确认扣减项；库存可用天数 = 可用量 ÷ 近7天日均实际销售出库。",
            "库存占用和库存扣减按库存组件规则独立对账，未使用分配数量替代实际出库。",
        ]
        table["complete_reconciled"] = True
        table["reviewed_business_table"] = True
        text = f"当前库存可用量 {_format_number(available)} EA，近7个完整自然日日均实际销售出库 {_format_number(average)} EA。"
        if available_days is None:
            text += "近7天没有正数出库，库存可用天数无法计算。"
        else:
            text += f"库存可用天数 {_format_number(available_days)} 天。"
        return SemanticAnswer(text, table)

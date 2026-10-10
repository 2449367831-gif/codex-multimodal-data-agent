"""Deterministic sales-outbound warehouse report with order and weight controls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Mapping

import sqlglot

from .handlers.base import MetricHandler
from ..outbound_contract import completed_status_sql, ref_order_type_sql
from .semantic_types import (
    SemanticAnswer,
    SemanticRequest,
    SemanticRuntimeError,
    _display_table,
    _format_number,
    _parse_decimal,
)
from ..sql import SqlSafetyValidator


@dataclass(frozen=True, slots=True)
class SalesWarehouseQualityPlan:
    start: date
    end: date
    owner: str

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError("sales interval must be nonempty")
        if not self.owner or self.owner == "ALL":
            raise ValueError("warehouse quality report requires one owner")


SOURCES = frozenset({
    "ywzt_ods.scm_sales_shipment_header",
    "ywzt_ods.scm_sales_shipment_detail",
    "ywzt_ods.scm_sales_order_header",
    "ywzt_ods.master_sku",
    "ywzt_ods.master_wms",
})


def compile_sales_warehouse_quality(plan: SalesWarehouseQualityPlan) -> str:
    owner = plan.owner.replace("'", "''")
    sql = f"""WITH sku_weight_ranked AS (
  SELECT TRIM(CAST(s.cargo_owner AS STRING)) AS cargo_owner,
         TRIM(s.sku_code) AS sku_code,
         s.net_weight,
         ROW_NUMBER() OVER (
           PARTITION BY TRIM(CAST(s.cargo_owner_id AS STRING)), TRIM(s.sku_code)
           ORDER BY s.updated_date DESC, s.version DESC, s.id DESC
         ) AS rn
  FROM ywzt_ods.master_sku AS s
  WHERE TRIM(CAST(s.cargo_owner AS STRING)) = '{owner}'
    AND TRIM(s.sku_code) <> ''
), sku_weight AS (
  SELECT r.cargo_owner, r.sku_code, r.net_weight,
         COALESCE(v.net_weight_variants, 0) AS net_weight_variants
  FROM sku_weight_ranked AS r
  LEFT JOIN (
    SELECT cargo_owner, sku_code, COUNT(DISTINCT net_weight) AS net_weight_variants
    FROM sku_weight_ranked
    WHERE net_weight IS NOT NULL AND net_weight > 0
    GROUP BY cargo_owner, sku_code
  ) AS v
    ON v.cargo_owner = r.cargo_owner AND v.sku_code = r.sku_code
  WHERE r.rn = 1
), wms_ranked AS (
  SELECT TRIM(CAST(cargo_owner_id AS STRING)) AS cargo_owner,
         TRIM(wms_code) AS wms_code,
         TRIM(COALESCE(wms_desc, '')) AS warehouse_name,
         ROW_NUMBER() OVER (
           PARTITION BY TRIM(CAST(cargo_owner_id AS STRING)), TRIM(wms_code)
           ORDER BY updated_date DESC, version DESC, id DESC
         ) AS rn
  FROM ywzt_ods.master_wms
  WHERE TRIM(CAST(cargo_owner_id AS STRING)) = '{owner}'
    AND COALESCE(TRIM(CAST(status AS STRING)), '0') = '0'
), wms_scope AS (
  SELECT r.cargo_owner, r.wms_code, r.warehouse_name,
         COALESCE(v.warehouse_name_variants, 0) AS warehouse_name_variants
  FROM wms_ranked AS r
  LEFT JOIN (
    SELECT cargo_owner, wms_code,
           COUNT(DISTINCT NULLIF(warehouse_name, '')) AS warehouse_name_variants
    FROM wms_ranked
    GROUP BY cargo_owner, wms_code
  ) AS v
    ON v.cargo_owner = r.cargo_owner AND v.wms_code = r.wms_code
  WHERE r.rn = 1
), header_ranked AS (
  SELECT TRIM(h.outbound_code) AS outbound_code,
         TRIM(h.company_code) AS cargo_owner,
         TRIM(h.facility_code) AS wms_code,
         TRIM(h.ref_order_code) AS sales_order_code,
         ROW_NUMBER() OVER (
           PARTITION BY TRIM(h.company_code), TRIM(h.outbound_code)
           ORDER BY h.updated DESC, h.version DESC, h.id DESC
         ) AS rn
  FROM ywzt_ods.scm_sales_shipment_header AS h
  WHERE TRIM(h.company_code) = '{owner}'
    AND h.ship_at >= '{plan.start.isoformat()} 00:00:00'
    AND h.ship_at < '{plan.end.isoformat()} 00:00:00'
    AND {completed_status_sql()}
    AND {ref_order_type_sql()}
    AND TRIM(h.ref_order_code) <> ''
    AND COALESCE(h.delete_flag, 0) = 0
    AND IFNULL(h._fdl_marked_deleted, 'false') = 'false'
    AND EXISTS (
      SELECT 1 FROM ywzt_ods.scm_sales_order_header AS so
      WHERE TRIM(so.company_code) = TRIM(h.company_code)
        AND TRIM(so.sales_order_code) = TRIM(h.ref_order_code)
        AND COALESCE(so.deleted_flag, 0) = 0
        AND IFNULL(so._fdl_marked_deleted, 'false') = 'false'
    )
), headers AS (
  SELECT outbound_code, cargo_owner, wms_code, sales_order_code
  FROM header_ranked WHERE rn = 1
), facts AS (
  SELECT h.cargo_owner, h.wms_code, h.outbound_code, h.sales_order_code,
         TRIM(d.sku_code) AS sku_code,
         COALESCE(d.ship_qty, 0) AS quantity,
         CASE WHEN d.ship_qty IS NULL THEN 1 ELSE 0 END AS null_quantity,
         sw.net_weight,
         COALESCE(sw.net_weight_variants, 0) AS net_weight_variants,
         CASE WHEN sw.sku_code IS NULL OR sw.net_weight IS NULL OR sw.net_weight <= 0
              THEN COALESCE(d.ship_qty, 0) ELSE 0 END AS missing_net_weight_quantity
  FROM headers AS h
  JOIN ywzt_ods.scm_sales_shipment_detail AS d
    ON TRIM(d.company_code) = h.cargo_owner
   AND TRIM(d.outbound_code) = h.outbound_code
  LEFT JOIN sku_weight AS sw
    ON sw.cargo_owner = h.cargo_owner AND sw.sku_code = TRIM(d.sku_code)
  WHERE COALESCE(d.delete_flag, 0) = 0
    AND IFNULL(d._fdl_marked_deleted, 'false') = 'false'
), grouped AS (
  SELECT wms_code,
         COUNT(DISTINCT outbound_code) AS outbound_count,
         COUNT(DISTINCT sales_order_code) AS sales_order_count,
         SUM(quantity) AS quantity,
         SUM(CASE WHEN net_weight IS NULL OR net_weight <= 0 THEN 0 ELSE quantity * net_weight / 1000 END) AS outbound_tons,
         SUM(missing_net_weight_quantity) AS missing_net_weight_quantity,
         SUM(null_quantity) AS null_quantity_count,
         COUNT(DISTINCT CASE WHEN net_weight_variants > 1 THEN sku_code ELSE NULL END) AS net_weight_conflict_sku_count,
         COUNT(*) AS detail_count
  FROM facts
  GROUP BY wms_code
), controls AS (
  SELECT COUNT(DISTINCT outbound_code) AS total_outbound_count,
         COUNT(DISTINCT sales_order_code) AS total_sales_order_count,
         SUM(quantity) AS total_quantity,
         SUM(CASE WHEN net_weight IS NULL OR net_weight <= 0 THEN 0 ELSE quantity * net_weight / 1000 END) AS total_tons,
         SUM(missing_net_weight_quantity) AS total_missing_net_weight_quantity,
         SUM(null_quantity) AS total_null_quantity_count,
         COUNT(DISTINCT CASE WHEN net_weight_variants > 1 THEN sku_code ELSE NULL END) AS total_net_weight_conflict_sku_count,
         COUNT(*) AS total_detail_count,
         SUM(CASE WHEN TRIM(COALESCE(sku_code, '')) = '' THEN 1 ELSE 0 END) AS blank_sku_count,
         SUM(CASE WHEN quantity < 0 THEN 1 ELSE 0 END) AS negative_quantity_count
  FROM facts
)
SELECT g.wms_code,
       COALESCE(w.warehouse_name, g.wms_code) AS warehouse_name,
       g.outbound_count, g.sales_order_count, g.quantity, g.outbound_tons,
       g.missing_net_weight_quantity, g.null_quantity_count,
       g.net_weight_conflict_sku_count, g.detail_count,
       c.total_outbound_count, c.total_sales_order_count, c.total_quantity,
       c.total_tons, c.total_missing_net_weight_quantity, c.total_null_quantity_count,
       c.total_net_weight_conflict_sku_count, c.total_detail_count,
       c.blank_sku_count, c.negative_quantity_count,
       COUNT(*) OVER () AS warehouse_count,
       COALESCE(w.warehouse_name_variants, 0) AS warehouse_name_variants
FROM grouped AS g
LEFT JOIN wms_scope AS w ON w.cargo_owner = '{owner}' AND w.wms_code = g.wms_code
CROSS JOIN controls AS c
ORDER BY g.quantity DESC"""
    rendered = sqlglot.parse_one(sql, read="doris").sql(dialect="doris")
    SqlSafetyValidator().validate(rendered, allowed_sources=SOURCES)
    return rendered


class SalesWarehouseQualityHandler(MetricHandler):
    def run(self, request: SemanticRequest) -> SemanticAnswer:
        plan = request.sales_warehouse_quality_plan
        if not isinstance(plan, SalesWarehouseQualityPlan):
            raise ValueError("sales warehouse quality plan is required")
        rows = self._execute(
            compile_sales_warehouse_quality(plan),
            purpose="actual sales outbound warehouse order and weight summary",
        )
        columns = [
            {"key": "warehouse_name", "kind": "text"},
            {"key": "outbound_count", "kind": "number", "unit": "单"},
            {"key": "sales_order_count", "kind": "number", "unit": "单"},
            {"key": "quantity", "kind": "number", "unit": "EA"},
            {"key": "outbound_tons", "kind": "number", "unit": "吨"},
            {"key": "missing_net_weight_quantity", "kind": "number", "unit": "EA"},
        ]
        if not rows:
            table = _display_table(
                table_id="sales_outbound_warehouse_quality",
                title="按仓实际销售出库",
                caption=f"{plan.start.isoformat()} 至 {(plan.end).isoformat()} · 左闭右开",
                columns=columns, rows=[],
                empty_label="当前日期没有匹配的有效销售出库记录",
            )
            table["complete_reconciled"] = True
            table["reviewed_business_table"] = True
            return SemanticAnswer("查询成功，当前范围没有有效销售出库记录。", table, result_state="no_data")
        try:
            total_outbound = int(_parse_decimal(rows[0].get("total_outbound_count")))
            total_orders = int(_parse_decimal(rows[0].get("total_sales_order_count")))
            total_quantity = _parse_decimal(rows[0].get("total_quantity"))
            total_tons = _parse_decimal(rows[0].get("total_tons"))
            total_missing = _parse_decimal(rows[0].get("total_missing_net_weight_quantity"))
            total_null_quantity = int(_parse_decimal(rows[0].get("total_null_quantity_count")))
            total_weight_conflicts = int(_parse_decimal(rows[0].get("total_net_weight_conflict_sku_count")))
            detail_total = int(_parse_decimal(rows[0].get("total_detail_count")))
            warehouse_count = int(_parse_decimal(rows[0].get("warehouse_count")))
            blank_sku = int(_parse_decimal(rows[0].get("blank_sku_count")))
            negative = int(_parse_decimal(rows[0].get("negative_quantity_count")))
        except (ValueError, TypeError, ArithmeticError) as exc:
            raise SemanticRuntimeError("销售出库汇总未通过完整性校验，暂未展示", reason="sales_quality_failed") from exc
        if negative or blank_sku or total_null_quantity or total_weight_conflicts:
            raise SemanticRuntimeError("销售出库数量或 SKU 净重存在缺失/冲突，暂未展示", reason="sales_quality_failed")
        if warehouse_count != len(rows) or min(total_outbound, total_orders, detail_total) < 0:
            raise SemanticRuntimeError("销售出库数据存在负数、空 SKU 或控制不一致，暂未展示", reason="sales_quality_failed")
        output: list[dict[str, str]] = []
        sums = [0, 0, Decimal(0), Decimal(0), Decimal(0), 0]
        for row in rows:
            try:
                values = (
                    int(_parse_decimal(row.get("outbound_count"))),
                    int(_parse_decimal(row.get("sales_order_count"))),
                    _parse_decimal(row.get("quantity")),
                    _parse_decimal(row.get("outbound_tons")),
                    _parse_decimal(row.get("missing_net_weight_quantity")),
                    int(_parse_decimal(row.get("detail_count"))),
                )
                name = str(row.get("warehouse_name") or row.get("wms_code") or "未匹配仓库").strip()
                null_quantity = int(_parse_decimal(row.get("null_quantity_count")))
                weight_conflicts = int(_parse_decimal(row.get("net_weight_conflict_sku_count")))
                warehouse_name_variants = int(_parse_decimal(row.get("warehouse_name_variants")))
            except (ValueError, TypeError, ArithmeticError) as exc:
                raise SemanticRuntimeError("销售出库明细未通过完整性校验，暂未展示", reason="sales_quality_failed") from exc
            if min(values[0], values[1], values[2], values[3], values[4], values[5]) < 0:
                raise SemanticRuntimeError("销售出库明细存在负数，暂未展示", reason="sales_quality_failed")
            if null_quantity or weight_conflicts or warehouse_name_variants > 1:
                raise SemanticRuntimeError("销售出库主数据存在缺失或一键多值，暂未展示", reason="sales_quality_failed")
            sums[0] += values[0]; sums[1] += values[1]; sums[2] += values[2]; sums[3] += values[3]; sums[4] += values[4]; sums[5] += values[5]
            output.append({
                "warehouse_name": name,
                "outbound_count": str(values[0]),
                "sales_order_count": str(values[1]),
                "quantity": _format_number(values[2]),
                "outbound_tons": _format_number(values[3]),
                "missing_net_weight_quantity": _format_number(values[4]),
            })
        if (sums[0] != total_outbound or sums[2] != total_quantity or sums[3] != total_tons
                or sums[4] != total_missing or sums[5] != detail_total):
            raise SemanticRuntimeError("销售出库分类合计未通过对账，暂未展示", reason="sales_quality_failed")
        table = _display_table(
            table_id="sales_outbound_warehouse_quality",
            title="按仓实际销售出库",
            caption=f"{plan.start.isoformat()} 至 {plan.end.isoformat()} · 左闭右开 · 销售出库类型和状态已按已发布口径过滤",
            columns=columns, rows=output,
            summary=[
                {"label": "出库单数", "value": str(total_outbound), "unit": "单"},
                {"label": "销售订单数", "value": str(total_orders), "unit": "单"},
                {"label": "实际销售出库", "value": _format_number(total_quantity), "unit": "EA"},
                {"label": "出库吨数", "value": _format_number(total_tons), "unit": "吨"},
                {"label": "缺少净重数量", "value": _format_number(total_missing), "unit": "EA"},
            ],
        )
        table["complete_reconciled"] = True
        table["reviewed_business_table"] = True
        text = (
            f"{plan.start.isoformat()} 实际销售出库：{total_outbound} 单、关联销售订单 {total_orders} 个、"
            f"实发 {_format_number(total_quantity)} EA、按 SKU 净重计算 {_format_number(total_tons)} 吨。"
            f"缺少有效净重的实发数量 {_format_number(total_missing)} EA，已单独列示，未将其虚算为吨数。"
        )
        return SemanticAnswer(text, table)

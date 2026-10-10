"""Doris-backed inventory occupation split handler."""

from decimal import Decimal
from typing import Any, Mapping

from ..occupation_planning import InventoryOccupationSplitPlan
from ..semantic_types import (
    SemanticAnswer,
    SemanticRequest,
    SemanticRuntimeError,
    _display_table,
    _format_number,
    _parse_decimal,
)
from .base import MetricHandler


class OccupationSplitHandler(MetricHandler):
    @staticmethod
    def _sql(plan: InventoryOccupationSplitPlan) -> str:
        since = plan.since.strftime("%Y-%m-%d %H:%M:%S")
        observed = plan.observed_at.strftime("%Y-%m-%d %H:%M:%S")
        return f"""WITH facts AS (
  SELECT TRIM(CAST(o.cargo_owner_id AS STRING)) AS cargo_owner,
         TRIM(o.wms_code) AS warehouse_code,
         TRIM(o.sku_code) AS sku_code,
         CAST(COALESCE(o.freeze_quantity, 0) AS DECIMAL(28, 4)) AS occupation_quantity,
         NULLIF(TRIM(o.pre_order_code), '') AS associated_order_code,
         o.created_date,
         TRIM(CAST(o.status AS STRING)) AS occupation_status
  FROM ywzt_ods.scm_warehous_occupy AS o
  WHERE o.created_date >= '{since}'
    AND o.created_date < '{observed}'
    AND TRIM(CAST(o.status AS STRING)) <> '1'
), controls AS (
  SELECT COUNT(*) AS source_row_count,
         SUM(CASE WHEN associated_order_code IS NULL THEN 1 ELSE 0 END) AS missing_order_count,
         SUM(CASE WHEN occupation_quantity < 0 THEN 1 ELSE 0 END) AS negative_quantity_count
  FROM facts
)
SELECT COALESCE(SUM(CASE WHEN associated_order_code LIKE '{plan.transfer_prefix}%' THEN occupation_quantity ELSE 0 END), 0) AS transfer_occupation,
       COALESCE(SUM(CASE WHEN associated_order_code IS NULL OR associated_order_code NOT LIKE '{plan.transfer_prefix}%' THEN occupation_quantity ELSE 0 END), 0) AS sales_occupation,
       COALESCE(SUM(occupation_quantity), 0) AS occupation_total,
       COUNT(DISTINCT associated_order_code) AS associated_order_count,
       c.source_row_count, c.missing_order_count, c.negative_quantity_count
FROM facts CROSS JOIN controls AS c
GROUP BY c.source_row_count, c.missing_order_count, c.negative_quantity_count"""

    def run(self, request: SemanticRequest) -> SemanticAnswer:
        plan = request.occupation_split_plan
        if not isinstance(plan, InventoryOccupationSplitPlan):
            raise ValueError("occupation split plan is required")
        rows = self._execute(self._sql(plan), purpose="inventory occupation split")
        if len(rows) > 1:
            raise SemanticRuntimeError("库存占用汇总返回了重复控制行，暂未展示", reason="occupation_result_invalid")
        row: Mapping[str, Any] = rows[0] if rows else {
            "transfer_occupation": 0,
            "sales_occupation": 0,
            "occupation_total": 0,
            "associated_order_count": 0,
            "source_row_count": 0,
            "missing_order_count": 0,
            "negative_quantity_count": 0,
        }
        try:
            transfer = _parse_decimal(row.get("transfer_occupation"), default=Decimal(0))
            sales = _parse_decimal(row.get("sales_occupation"), default=Decimal(0))
            total = _parse_decimal(row.get("occupation_total"), default=Decimal(0))
            order_count = int(_parse_decimal(row.get("associated_order_count"), default=Decimal(0)))
            source_rows = int(_parse_decimal(row.get("source_row_count"), default=Decimal(0)))
            missing_orders = int(_parse_decimal(row.get("missing_order_count"), default=Decimal(0)))
            negative = int(_parse_decimal(row.get("negative_quantity_count"), default=Decimal(0)))
        except (ValueError, TypeError, ArithmeticError) as exc:
            raise SemanticRuntimeError("库存占用结果未通过完整性校验，暂未展示", reason="occupation_quality_failed") from exc
        if transfer + sales != total or min(order_count, source_rows, missing_orders, negative) < 0 or negative:
            raise SemanticRuntimeError("库存占用结果未通过完整性校验，暂未展示", reason="occupation_quality_failed")
        if source_rows == 0 and rows:
            raise SemanticRuntimeError("库存占用控制信息不一致，暂未展示", reason="occupation_result_invalid")
        unit = "EA"
        table = _display_table(
            table_id="inventory_occupation_split",
            title="库存占用拆分",
            caption=f"{plan.since.strftime('%Y-%m-%d %H:%M:%S')} 至 {plan.observed_at.strftime('%Y-%m-%d %H:%M:%S')} · 左闭右开",
            columns=[
                {"key": "transfer_occupation", "kind": "number", "unit": unit},
                {"key": "sales_occupation", "kind": "number", "unit": unit},
                {"key": "occupation_total", "kind": "number", "unit": unit},
                {"key": "associated_order_count", "kind": "number", "unit": "个"},
            ],
            rows=[{
                "transfer_occupation": _format_number(transfer),
                "sales_occupation": _format_number(sales),
                "occupation_total": _format_number(total),
                "associated_order_count": str(order_count),
            }],
            summary=[{"label": "来源记录", "value": str(source_rows), "unit": "条"}],
        )
        text = (
            f"库存占用合计 {_format_number(total)} EA，其中调拨占用 {_format_number(transfer)} EA，"
            f"销售占用 {_format_number(sales)} EA，关联单号 {order_count} 个。"
            f"口径：取占用表 freeze_quantity；status 不等于 1；pre_order_code 以 {plan.transfer_prefix} 开头计入调拨，其余计入销售。"
        )
        if missing_orders:
            text += f"有 {missing_orders} 条占用记录没有关联单号，按销售占用计入，但不计入关联单号数。"
        table["complete_reconciled"] = True
        return SemanticAnswer(text, table, result_state="no_data" if source_rows == 0 else "live_success")

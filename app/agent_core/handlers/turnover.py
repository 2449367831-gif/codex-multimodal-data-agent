"TurnoverHandler SQL and result reconciliation."

from __future__ import annotations
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Mapping
from ..semantic_types import (
    SemanticAnswer,
    SemanticRequest,
    SemanticRuntimeError,
    _display_table,
    _format_number,
    _parse_decimal,
    _parse_source_datetime,
    _period_label,
)
from .base import MetricHandler
from ..dms_points_orders import _count
from ...outbound_contract import completed_status_sql, ref_order_type_sql
from ...inventory_source import compile_with_current_inventory
from ...sales_source import BASIS_LABEL, compile_with_business_sales


def _turnover_value(row, sales_ea):
    if any(_count(row.get(key)) != 0 for key in
           ("null_sales_quantity_count", "negative_sales_quantity_count")):
        raise ValueError("invalid source sales quantities")
    if "turnover_days" not in row:
        raise ValueError("missing turnover projection")
    if sales_ea == 0:
        if row["turnover_days"] is not None:
            raise ValueError("zero sales requires a null turnover")
        return None
    return _parse_decimal(row["turnover_days"])


class TurnoverHandler(MetricHandler):
    @compile_with_business_sales
    def _turnover_sql(self, start: date, end: date) -> str:
        start_text = self._sql_date(start)
        end_text = self._sql_date(end)
        snapshot = end - timedelta(days=1)
        snapshot_text = self._sql_date(snapshot)
        owner = self.owner.replace("'", "''")
        day_count = (end - start).days
        return f"""WITH finished_goods_skus AS (
  SELECT sku_code
  FROM (
    SELECT TRIM(sku_code) AS sku_code,
           ROW_NUMBER() OVER (
             PARTITION BY TRIM(sku_code)
             ORDER BY updated_date DESC, version DESC, id DESC
           ) AS rn
    FROM ywzt_ods.master_sku
    WHERE TRIM(sku_code) <> ''
      AND TRIM(sku_group) = '50'
      AND COALESCE(TRIM(CAST(status AS STRING)), '0') = '0'
  ) AS ranked_sku
  WHERE rn = 1
), snapshot_by_key AS (
  SELECT TRIM(b.cargo_owner_id) AS cargo_owner,
         TRIM(b.wms_code) AS wms_code,
         TRIM(b.sku_code) AS sku_code,
         SUM(COALESCE(b.in_stock_quantity, 0)) AS stock_ea
  FROM ywzt_ods.scm_warehous_manage_backups AS b
  JOIN finished_goods_skus AS s ON s.sku_code = TRIM(b.sku_code)
  WHERE TRIM(b.cargo_owner_id) = '{owner}'
    AND b.backups_date = '{snapshot_text}'
    AND b._fdl_marked_deleted = 'false'
  GROUP BY TRIM(b.cargo_owner_id), TRIM(b.wms_code), TRIM(b.sku_code)
), snapshot_total AS (
  SELECT COALESCE(SUM(stock_ea), 0) AS snapshot_ea,
         COUNT(*) AS snapshot_key_count,
         COUNT(DISTINCT wms_code) AS snapshot_warehouse_count,
         COALESCE(SUM(CASE WHEN stock_ea < 0 THEN 1 ELSE 0 END), 0) AS negative_snapshot_key_count
  FROM snapshot_by_key
), sales_by_key AS (
   SELECT e.source_kind, e.document_code AS outbound_code, e.cargo_owner, e.wms_code, e.sku_code, SUM(e.quantity) AS sales_ea,
          SUM(CASE WHEN e.quantity IS NULL THEN 1 ELSE 0 END) AS null_sales_quantity_count,
          SUM(CASE WHEN e.quantity < 0 THEN 1 ELSE 0 END) AS negative_sales_quantity_count
   FROM sales_current e JOIN finished_goods_skus s ON s.sku_code=e.sku_code
   GROUP BY e.source_kind, e.document_code, e.cargo_owner, e.wms_code, e.sku_code
), sales_total AS (
  SELECT COALESCE(SUM(sales_ea), 0) AS sales_ea,
         COALESCE(SUM(null_sales_quantity_count), 0) AS null_sales_quantity_count,
         COALESCE(SUM(negative_sales_quantity_count), 0) AS negative_sales_quantity_count
  FROM sales_by_key
)
SELECT st.snapshot_ea,
       st.snapshot_key_count,
       st.snapshot_warehouse_count,
       st.negative_snapshot_key_count,
       se.sales_ea,
       se.null_sales_quantity_count,
       se.negative_sales_quantity_count,
       {day_count} AS complete_natural_days,
       CASE WHEN se.sales_ea = 0 THEN NULL
            ELSE st.snapshot_ea / (se.sales_ea / {day_count}) END AS turnover_days
FROM snapshot_total AS st
CROSS JOIN sales_total AS se"""

    def _run_historical_turnover(self, request: SemanticRequest) -> SemanticAnswer:
        rows = self._execute(
            self._turnover_sql(request.start, request.end),
            purpose="historical finished-goods inventory turnover",
        )
        if len(rows) != 1:
            raise SemanticRuntimeError("数据未通过完整性校验，暂未展示结果", reason="turnover_scalar_invalid")
        row = rows[0]
        try:
            snapshot_ea = _parse_decimal(row.get("snapshot_ea"))
            snapshot_key_count = int(_parse_decimal(row.get("snapshot_key_count")))
            snapshot_warehouse_count = int(_parse_decimal(row.get("snapshot_warehouse_count")))
            negative_snapshot_key_count = int(
                _parse_decimal(row.get("negative_snapshot_key_count"), default=Decimal(0))
            )
            sales_ea = _parse_decimal(row.get("sales_ea"))
            days = _count(row.get("complete_natural_days"))
            turnover = _turnover_value(row, sales_ea)
        except (ValueError, TypeError) as exc:
            raise SemanticRuntimeError("数据未通过完整性校验，暂未展示结果", reason="turnover_values_invalid") from exc
        if (
            snapshot_ea < 0
            or snapshot_key_count <= 0
            or snapshot_warehouse_count <= 0
            or negative_snapshot_key_count < 0
            or sales_ea < 0
            or days != (request.end - request.start).days
            or days <= 0
            or (turnover is not None and turnover < 0)
        ):
            raise SemanticRuntimeError("数据未通过完整性校验，暂未展示结果", reason="turnover_reconciliation_failed")
        expected = snapshot_ea / (sales_ea / Decimal(days)) if sales_ea else None
        if expected is not None and abs(expected - turnover) > Decimal("0.000001"):
            raise SemanticRuntimeError("数据未通过完整性校验，暂未展示结果", reason="turnover_formula_mismatch")
        period = _period_label(request.start, request.end, request.period_label)
        warning = (
            "结果保留了源数据中的异常库存记录，未做负数截断。"
            if negative_snapshot_key_count
            else ""
        )
        answer = (
            (f"{period}库存周转：{_format_number(turnover)}天。" if turnover is not None
             else f"{period}无动销，库存周转天数无法计算。") +
            f"口径：区间末成品库存除以{period}新销售口径成品日均数量，统计{days}个完整自然日。"
            f"动销：{BASIS_LABEL}。"
            f"{warning}"
        )
        table = _display_table(
            table_id="historical_inventory_turnover",
            title="库存周转",
            caption=f"{period} · 区间末成品库存 ÷ 新销售口径成品日均数量",
            columns=[
                {"key": "metric", "kind": "text"},
                {"key": "value", "kind": "number"},
                {"key": "unit", "kind": "text"},
            ],
            rows=[
                {
                    "metric": "区间末成品库存",
                    "value": _format_number(snapshot_ea, places=0),
                    "unit": "EA",
                },
                {
                    "metric": "新口径销售日均",
                    "value": _format_number(sales_ea / Decimal(days)),
                    "unit": "EA/日",
                },
                {
                    "metric": "库存周转",
                    "value": _format_number(turnover) if turnover is not None else None,
                    "unit": "天",
                },
            ],
            summary=[
                {"label": "统计天数", "value": str(days), "unit": "个完整自然日"},
            ],
        )
        return SemanticAnswer(answer, table)

    @compile_with_current_inventory
    @compile_with_business_sales
    def _realtime_turnover_sql(self, start: date, end: date) -> str:
        """Build the current-real-time numerator variant of turnover."""

        start_text = self._sql_date(start)
        end_text = self._sql_date(end)
        owner = self.owner.replace("'", "''")
        day_count = (end - start).days
        return f"""WITH finished_goods_skus AS (
  SELECT sku_code
  FROM (
    SELECT TRIM(sku_code) AS sku_code,
           ROW_NUMBER() OVER (
             PARTITION BY TRIM(sku_code)
             ORDER BY updated_date DESC, version DESC, id DESC
           ) AS rn
    FROM ywzt_ods.master_sku
    WHERE TRIM(sku_code) <> ''
      AND TRIM(sku_group) = '50'
      AND COALESCE(TRIM(CAST(status AS STRING)), '0') = '0'
  ) AS ranked_sku
  WHERE rn = 1
), source_table_meta AS (
  SELECT MAX(CASE WHEN table_name = 'scm_warehous_manage' THEN update_time END) AS inventory_update_time
  FROM information_schema.tables
  WHERE table_schema = 'ywzt_ods' AND table_name = 'scm_warehous_manage'
), current_by_key AS (
  SELECT TRIM(i.owner) AS cargo_owner,
          TRIM(i.wms_code) AS wms_code,
         TRIM(i.sku_code) AS sku_code,
         SUM(COALESCE(i.available_quantity, 0) + COALESCE(i.in_transit_quantity, 0)) AS inventory_ea,
         MAX(meta.inventory_update_time) AS latest_freshness
  FROM inventory_current AS i
  JOIN finished_goods_skus AS s ON s.sku_code = TRIM(i.sku_code)
  CROSS JOIN source_table_meta AS meta
  WHERE TRIM(i.owner) = '{owner}'
   GROUP BY TRIM(i.owner), TRIM(i.wms_code), TRIM(i.sku_code)
), current_total AS (
  SELECT COALESCE(SUM(inventory_ea), 0) AS inventory_ea,
         COUNT(*) AS inventory_key_count,
         COUNT(DISTINCT wms_code) AS inventory_warehouse_count,
         COALESCE(SUM(CASE WHEN inventory_ea < 0 THEN 1 ELSE 0 END), 0) AS negative_inventory_key_count,
         MAX(latest_freshness) AS inventory_latest_freshness
  FROM current_by_key
), sales_by_key AS (
   SELECT e.source_kind, e.document_code AS outbound_code, e.cargo_owner, e.wms_code, e.sku_code, SUM(e.quantity) AS sales_ea,
          SUM(CASE WHEN e.quantity IS NULL THEN 1 ELSE 0 END) AS null_sales_quantity_count,
          SUM(CASE WHEN e.quantity < 0 THEN 1 ELSE 0 END) AS negative_sales_quantity_count
   FROM sales_current e JOIN finished_goods_skus s ON s.sku_code=e.sku_code
   GROUP BY e.source_kind, e.document_code, e.cargo_owner, e.wms_code, e.sku_code
), sales_total AS (
  SELECT COALESCE(SUM(sales_ea), 0) AS sales_ea,
         COALESCE(SUM(null_sales_quantity_count), 0) AS null_sales_quantity_count,
         COALESCE(SUM(negative_sales_quantity_count), 0) AS negative_sales_quantity_count
  FROM sales_by_key
)
SELECT ct.inventory_ea,
       ct.inventory_key_count,
       ct.inventory_warehouse_count,
       ct.negative_inventory_key_count,
       ct.inventory_latest_freshness,
       st.sales_ea,
       st.null_sales_quantity_count,
       st.negative_sales_quantity_count,
       {day_count} AS complete_natural_days,
       CASE WHEN st.sales_ea = 0 THEN NULL
            ELSE ct.inventory_ea / (st.sales_ea / {day_count}) END AS turnover_days
FROM current_total AS ct
CROSS JOIN sales_total AS st"""

    def _run_realtime_turnover(self, request: SemanticRequest) -> SemanticAnswer:
        rows = self._execute(
            self._realtime_turnover_sql(request.start, request.end),
            purpose="current real-time finished-goods inventory turnover",
        )
        if len(rows) != 1:
            raise SemanticRuntimeError(
                "数据未通过完整性校验，暂未展示结果",
                reason="realtime_turnover_scalar_invalid",
            )
        row = rows[0]
        try:
            inventory_ea = _parse_decimal(row.get("inventory_ea"))
            inventory_key_count = int(_parse_decimal(row.get("inventory_key_count")))
            inventory_warehouse_count = int(
                _parse_decimal(row.get("inventory_warehouse_count"))
            )
            negative_inventory_key_count = int(
                _parse_decimal(row.get("negative_inventory_key_count"), default=Decimal(0))
            )
            inventory_freshness = _parse_source_datetime(
                row.get("inventory_latest_freshness"),
                timezone_name=self.timezone_name,
            )
            sales_ea = _parse_decimal(row.get("sales_ea"))
            days = _count(row.get("complete_natural_days"))
            turnover = _turnover_value(row, sales_ea)
        except (ValueError, TypeError) as exc:
            raise SemanticRuntimeError(
                "数据未通过完整性校验，暂未展示结果",
                reason="realtime_turnover_values_invalid",
            ) from exc
        if (
            inventory_ea < 0
            or inventory_key_count <= 0
            or inventory_warehouse_count <= 0
            or negative_inventory_key_count < 0
            or inventory_freshness is None
            or sales_ea < 0
            or days != (request.end - request.start).days
            or days <= 0
            or (turnover is not None and turnover < 0)
        ):
            raise SemanticRuntimeError(
                "数据未通过完整性校验，暂未展示结果",
                reason="realtime_turnover_reconciliation_failed",
            )
        expected = inventory_ea / (sales_ea / Decimal(days)) if sales_ea else None
        if expected is not None and abs(expected - turnover) > Decimal("0.000001"):
            raise SemanticRuntimeError(
                "数据未通过完整性校验，暂未展示结果",
                reason="realtime_turnover_formula_mismatch",
            )
        period = _period_label(request.start, request.end, request.period_label)
        answer = (
            (f"{period}当前实时可用成品库存（含在途）周转：{_format_number(turnover)}天。"
             if turnover is not None else f"{period}无动销，当前实时成品库存周转天数无法计算。") +
            f"口径：当前可用成品库存加在途库存，除以所选区间内新销售口径成品日均数量，"
            f"统计{days}个完整自然日。"
            f"动销：{BASIS_LABEL}。"
        )
        if negative_inventory_key_count:
            answer += "库存保留了源数据中的负数记录，未截断为零。"
        table = _display_table(
            table_id="realtime_inventory_turnover",
            title="库存周转",
            caption=f"{period} · 当前可用成品库存 + 在途 ÷ 新销售口径成品日均数量",
            columns=[
                {"key": "metric", "kind": "text"},
                {"key": "value", "kind": "number"},
                {"key": "unit", "kind": "text"},
            ],
            rows=[
                {
                    "metric": "当前可用成品库存 + 在途",
                    "value": _format_number(inventory_ea, places=0),
                    "unit": "EA",
                },
                {
                    "metric": "新口径销售日均",
                    "value": _format_number(sales_ea / Decimal(days)),
                    "unit": "EA/日",
                },
                {
                    "metric": "库存周转",
                    "value": _format_number(turnover) if turnover is not None else None,
                    "unit": "天",
                },
            ],
            summary=[
                {"label": "统计天数", "value": str(days), "unit": "个完整自然日"},
            ],
        )
        return SemanticAnswer(answer, table)

    @compile_with_current_inventory
    @compile_with_business_sales
    def _realtime_turnover_by_warehouse_sql(self, start: date, end: date) -> str:
        """Build the shared-scope warehouse-grain turnover query.

        Inventory stores a warehouse display name while outbound headers store
        the warehouse code. The mapping is made unique before either component
        is aggregated so a name collision cannot multiply inventory or sales.
        """

        start_text = self._sql_date(start)
        end_text = self._sql_date(end)
        owner = self.owner.replace("'", "''")
        day_count = (end - start).days
        return f"""WITH finished_goods_skus AS (
  SELECT sku_code
  FROM (
    SELECT TRIM(sku_code) AS sku_code,
           ROW_NUMBER() OVER (
             PARTITION BY TRIM(sku_code)
             ORDER BY updated_date DESC, version DESC, id DESC
           ) AS rn
    FROM ywzt_ods.master_sku
    WHERE TRIM(sku_code) <> ''
      AND TRIM(sku_group) = '50'
      AND COALESCE(TRIM(CAST(status AS STRING)), '0') = '0'
  ) AS ranked_sku
  WHERE rn = 1
), warehouse_ranked AS (
  SELECT TRIM(cargo_owner_id) AS cargo_owner,
         TRIM(wms_code) AS wms_code,
         TRIM(wms_desc) AS warehouse_name,
         ROW_NUMBER() OVER (
           PARTITION BY TRIM(cargo_owner_id), TRIM(wms_code)
           ORDER BY updated_date DESC, version DESC, id DESC
         ) AS rn
  FROM ywzt_ods.master_wms
  WHERE TRIM(cargo_owner_id) = '{owner}'
    AND TRIM(wms_code) <> ''
    AND COALESCE(TRIM(CAST(status AS STRING)), '0') = '0'
), warehouse_map AS (
  SELECT cargo_owner, wms_code, warehouse_name
  FROM warehouse_ranked
  WHERE rn = 1 AND warehouse_name <> ''
), warehouse_name_counts AS (
  SELECT cargo_owner, warehouse_name, COUNT(DISTINCT wms_code) AS code_count
  FROM warehouse_map
  GROUP BY cargo_owner, warehouse_name
), unique_warehouse_map AS (
  SELECT m.cargo_owner, m.wms_code, m.warehouse_name
  FROM warehouse_map AS m
  JOIN warehouse_name_counts AS c
    ON c.cargo_owner = m.cargo_owner
   AND c.warehouse_name = m.warehouse_name
   AND c.code_count = 1
), source_table_meta AS (
  SELECT MAX(CASE WHEN table_name = 'scm_warehous_manage' THEN update_time END) AS inventory_update_time
  FROM information_schema.tables
  WHERE table_schema = 'ywzt_ods' AND table_name = 'scm_warehous_manage'
), current_by_warehouse AS (
  SELECT m.cargo_owner,
         m.wms_code,
         m.warehouse_name,
         SUM(COALESCE(i.available_quantity, 0) + COALESCE(i.in_transit_quantity, 0)) AS inventory_ea
  FROM inventory_current AS i
  JOIN unique_warehouse_map AS m
    ON m.cargo_owner = TRIM(i.owner)
   AND m.wms_code = TRIM(i.wms_code)
  JOIN finished_goods_skus AS s ON s.sku_code = TRIM(i.sku_code)
  WHERE TRIM(i.owner) = '{owner}'
  GROUP BY m.cargo_owner, m.wms_code, m.warehouse_name
), unmapped_inventory AS (
  SELECT TRIM(i.owner) AS cargo_owner,
         TRIM(i.wms_desc) AS warehouse_name,
         SUM(COALESCE(i.available_quantity, 0) + COALESCE(i.in_transit_quantity, 0)) AS inventory_ea
  FROM inventory_current AS i
  LEFT JOIN unique_warehouse_map AS m
    ON m.cargo_owner = TRIM(i.owner)
   AND m.wms_code = TRIM(i.wms_code)
  JOIN finished_goods_skus AS s ON s.sku_code = TRIM(i.sku_code)
  WHERE TRIM(i.owner) = '{owner}' AND m.wms_code IS NULL
  GROUP BY TRIM(i.owner), TRIM(i.wms_desc)
), sales_by_warehouse AS (
   SELECT e.cargo_owner, e.wms_code, SUM(e.quantity) AS sales_ea,
          SUM(CASE WHEN e.quantity IS NULL THEN 1 ELSE 0 END) AS null_sales_quantity_count,
          SUM(CASE WHEN e.quantity < 0 THEN 1 ELSE 0 END) AS negative_sales_quantity_count
   FROM sales_current e JOIN finished_goods_skus s ON s.sku_code=e.sku_code
   GROUP BY e.cargo_owner, e.wms_code
), warehouse_keys AS (
  SELECT cargo_owner, wms_code FROM current_by_warehouse
  UNION
  SELECT cargo_owner, wms_code FROM sales_by_warehouse
)
SELECT k.cargo_owner,
       k.wms_code,
       COALESCE(i.warehouse_name, m.warehouse_name, k.wms_code) AS warehouse_name,
       i.inventory_ea AS inventory_ea,
       COALESCE(s.sales_ea, 0) AS sales_ea,
       COALESCE(s.null_sales_quantity_count, 0) AS null_sales_quantity_count,
       COALESCE(s.negative_sales_quantity_count, 0) AS negative_sales_quantity_count,
       meta.inventory_update_time AS inventory_latest_freshness,
       {day_count} AS complete_natural_days,
       CASE WHEN COALESCE(s.sales_ea, 0) = 0 OR i.wms_code IS NULL THEN NULL
            ELSE i.inventory_ea / (s.sales_ea / {day_count}) END AS turnover_days,
       CASE WHEN i.wms_code IS NULL THEN 0 ELSE 1 END AS inventory_present,
       CASE WHEN m.wms_code IS NULL THEN 0 ELSE 1 END AS warehouse_mapping_present
FROM warehouse_keys AS k
LEFT JOIN current_by_warehouse AS i
  ON i.cargo_owner = k.cargo_owner AND i.wms_code = k.wms_code
LEFT JOIN sales_by_warehouse AS s
  ON s.cargo_owner = k.cargo_owner AND s.wms_code = k.wms_code
LEFT JOIN unique_warehouse_map AS m
  ON m.cargo_owner = k.cargo_owner AND m.wms_code = k.wms_code
CROSS JOIN source_table_meta AS meta
UNION ALL
SELECT u.cargo_owner,
       NULL AS wms_code,
       u.warehouse_name,
       u.inventory_ea,
       NULL AS sales_ea,
       0 AS null_sales_quantity_count,
       0 AS negative_sales_quantity_count,
       meta.inventory_update_time AS inventory_latest_freshness,
       {day_count} AS complete_natural_days,
       NULL AS turnover_days,
       1 AS inventory_present,
       0 AS warehouse_mapping_present
FROM unmapped_inventory AS u
CROSS JOIN source_table_meta AS meta
ORDER BY wms_code, warehouse_name"""

    def _run_realtime_turnover_by_warehouse(self, request: SemanticRequest) -> SemanticAnswer:
        rows = self._execute(
            self._realtime_turnover_by_warehouse_sql(request.start, request.end),
            purpose="current real-time finished-goods inventory turnover by warehouse",
        )
        output_rows = []
        abnormal_warehouses = 0
        incomplete_warehouses = 0
        days_expected = (request.end - request.start).days
        for row in rows:
            try:
                inventory_present = _count(row.get("inventory_present")) == 1
                mapping_present = _count(row.get("warehouse_mapping_present")) == 1
                inventory = _parse_decimal(row.get("inventory_ea")) if inventory_present else None
                sales = _parse_decimal(row.get("sales_ea")) if mapping_present else None
                days = _count(row.get("complete_natural_days"))
                turnover = _turnover_value(row, sales) if inventory_present and mapping_present else None
                freshness = _parse_source_datetime(row.get("inventory_latest_freshness"), timezone_name=self.timezone_name)
                warehouse_code = str(row.get("wms_code") or "").strip()
                warehouse_name = str(row.get("warehouse_name") or warehouse_code or "仓库名称缺失").strip()
            except (ValueError, TypeError) as exc:
                raise SemanticRuntimeError("数据未通过完整性校验，暂未展示结果", reason="warehouse_turnover_values_invalid") from exc
            if (
                (mapping_present and not warehouse_code)
                or (sales is not None and sales < 0)
                or days != days_expected
                or days <= 0
                or freshness is None
                or _count(row.get("null_sales_quantity_count"))
                or _count(row.get("negative_sales_quantity_count"))
            ):
                raise SemanticRuntimeError("数据未通过完整性校验，暂未展示结果", reason="warehouse_turnover_reconciliation_failed")
            expected = inventory / (sales / Decimal(days)) if inventory is not None and sales else None
            if expected is not None and turnover is None:
                raise SemanticRuntimeError("数据未通过完整性校验，暂未展示结果", reason="warehouse_turnover_formula_missing")
            if expected is not None and abs(expected - turnover) > Decimal("0.000001"):
                raise SemanticRuntimeError("数据未通过完整性校验，暂未展示结果", reason="warehouse_turnover_formula_mismatch")
            if not mapping_present:
                incomplete_warehouses += 1
                display_turnover = None
                turnover_state = "仓库映射缺失"
            elif not inventory_present:
                incomplete_warehouses += 1
                display_turnover = None
                turnover_state = "库存缺失"
            elif inventory < 0:
                abnormal_warehouses += 1
                display_turnover = None
                turnover_state = "库存异常"
            elif sales == 0:
                display_turnover = None
                turnover_state = "无动销"
            else:
                display_turnover = turnover
                turnover_state = "正常"
            output_rows.append({
                "warehouse_code": warehouse_code or None,
                "warehouse_name": warehouse_name,
                "inventory": _format_number(inventory, places=0) if inventory is not None else None,
                "daily_average": _format_number(sales / Decimal(days), places=2) if sales is not None else None,
                "turnover_days": _format_number(display_turnover) if display_turnover is not None else None,
                "turnover_state": turnover_state,
            })
        period = _period_label(request.start, request.end, request.period_label)
        table = _display_table(
            table_id="realtime_inventory_turnover_by_warehouse",
            title="按仓库库存周转",
            caption=f"{period} · 可用成品库存 + 在途 ÷ 新销售口径成品日均数量",
            columns=[
                {"key": "warehouse_code", "kind": "text"},
                {"key": "warehouse_name", "kind": "text"},
                {"key": "inventory", "kind": "number"},
                {"key": "daily_average", "kind": "number"},
                {"key": "turnover_days", "kind": "number"},
                {"key": "turnover_state", "kind": "text"},
            ],
            rows=output_rows,
            summary=[
                {"label": "统计天数", "value": str(days_expected), "unit": "个完整自然日"},
                {"label": "仓库数", "value": str(len(output_rows)), "unit": "个"},
            ],
        )
        answer = (
            f"{period}按仓库计算库存周转，统计{days_expected}个完整自然日。"
            "口径：可用成品库存加在途库存，除以同一仓库范围内新销售口径成品日均数量。"
            f"动销：{BASIS_LABEL}。"
        )
        if abnormal_warehouses:
            answer += f"其中{abnormal_warehouses}个仓库库存为负，已保留源库存并标注异常，未计算这些仓库的周转天数。"
        if incomplete_warehouses:
            answer += f"另有{incomplete_warehouses}个仓库库存或映射缺失，已保留可确认数据并标注，未将缺失值当作零。"
        return SemanticAnswer(answer, table, result_state="live_success" if output_rows else "no_data")

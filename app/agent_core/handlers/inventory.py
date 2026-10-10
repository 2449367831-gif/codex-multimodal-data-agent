"InventoryHandler SQL and result reconciliation."

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
)
from .base import MetricHandler
from ...outbound_contract import completed_status_sql, ref_order_type_sql
from ...inventory_source import compile_with_current_inventory


class InventoryHandler(MetricHandler):

    @compile_with_current_inventory
    def _owner_summary_sql(self) -> str:
        """Build the reviewed current finished-goods owner summary."""

        return """WITH finished_goods_skus AS (
  SELECT DISTINCT TRIM(CAST(sku_code AS STRING)) AS sku_code
  FROM ywzt_ods.master_sku
  WHERE TRIM(CAST(sku_group AS STRING)) = '50'
    AND TRIM(CAST(sku_code AS STRING)) <> ''
), source_table_meta AS (
  SELECT MAX(update_time) AS inventory_update_time
  FROM information_schema.tables
  WHERE table_schema = 'ywzt_ods' AND table_name = 'scm_warehous_manage'
), inventory_by_key AS (
  SELECT TRIM(CAST(i.owner AS STRING)) AS cargo_owner,
         TRIM(CAST(i.wms_desc AS STRING)) AS warehouse_code,
         TRIM(CAST(i.sku_code AS STRING)) AS sku_code,
         SUM(COALESCE(i.available_quantity, 0)) AS available_ea,
         SUM(COALESCE(i.in_transit_quantity, 0)) AS in_transit_ea,
         COUNT(*) AS source_row_count,
         MAX(meta.inventory_update_time) AS latest_freshness
  FROM inventory_current AS i
  JOIN finished_goods_skus AS fg ON fg.sku_code = TRIM(CAST(i.sku_code AS STRING))
  CROSS JOIN source_table_meta AS meta
  GROUP BY TRIM(CAST(i.owner AS STRING)), TRIM(CAST(i.wms_desc AS STRING)), TRIM(CAST(i.sku_code AS STRING))
), inventory_by_warehouse AS (
  SELECT cargo_owner, warehouse_code,
         SUM(available_ea) AS available_ea,
         SUM(in_transit_ea) AS in_transit_ea,
         SUM(available_ea + in_transit_ea) AS finished_goods_ea,
         SUM(source_row_count) AS source_row_count,
         SUM(CASE WHEN available_ea + in_transit_ea < 0 THEN 1 ELSE 0 END) AS negative_warehouse_count,
         MAX(latest_freshness) AS latest_freshness
  FROM inventory_by_key
  GROUP BY cargo_owner, warehouse_code
), owner_groups AS (
  SELECT cargo_owner, SUM(available_ea) AS available_ea,
         SUM(in_transit_ea) AS in_transit_ea, SUM(finished_goods_ea) AS finished_goods_ea,
         SUM(source_row_count) AS source_row_count,
         SUM(negative_warehouse_count) AS negative_warehouse_count,
         MAX(latest_freshness) AS latest_freshness
  FROM inventory_by_warehouse GROUP BY cargo_owner
), controls AS (
  SELECT COALESCE(SUM(available_ea), 0) AS total_available_ea,
         COALESCE(SUM(in_transit_ea), 0) AS total_in_transit_ea,
         COALESCE(SUM(finished_goods_ea), 0) AS total_finished_goods_ea,
         COUNT(*) AS owner_count,
         COALESCE(SUM(CASE WHEN cargo_owner IS NULL OR cargo_owner = '' THEN 1 ELSE 0 END), 0) AS invalid_owner_count,
         COALESCE(SUM(negative_warehouse_count), 0) AS negative_warehouse_count,
         MAX(latest_freshness) AS latest_freshness
  FROM owner_groups
)
SELECT g.cargo_owner, g.available_ea, g.in_transit_ea, g.finished_goods_ea,
       g.source_row_count, g.negative_warehouse_count, g.latest_freshness,
       c.total_available_ea, c.total_in_transit_ea, c.total_finished_goods_ea,
       c.owner_count, c.invalid_owner_count,
       c.negative_warehouse_count AS total_negative_warehouse_count,
       c.latest_freshness AS total_latest_freshness
FROM owner_groups AS g CROSS JOIN controls AS c
WHERE g.cargo_owner IS NOT NULL AND g.cargo_owner <> ''
ORDER BY g.finished_goods_ea DESC, g.cargo_owner ASC"""

    def _run_owner_summary(self, request: SemanticRequest) -> SemanticAnswer:
        rows = self._execute(self._owner_summary_sql(), purpose="current finished-goods inventory by owner")
        if not rows:
            return SemanticAnswer("查询完成，当前没有可展示的成品库存记录。", _display_table(
                table_id="current_inventory_owner_summary", title="按货主汇总的当前成品库存",
                caption="当前实时可用成品库存加在途 · Asia/Shanghai", columns=[], rows=[],
                empty_label="当前范围没有可展示的成品库存记录"), result_state="no_data")
        try:
            first = rows[0]
            total_available = _parse_decimal(first.get("total_available_ea"))
            total_transit = _parse_decimal(first.get("total_in_transit_ea"))
            total_inventory = _parse_decimal(first.get("total_finished_goods_ea"))
            owner_count = int(_parse_decimal(first.get("owner_count")))
            invalid_owner_count = int(_parse_decimal(first.get("invalid_owner_count")))
            negative_warehouse_count = int(_parse_decimal(first.get("total_negative_warehouse_count")))
            freshness = _parse_source_datetime(first.get("total_latest_freshness"), timezone_name=self.timezone_name)
        except (ValueError, TypeError) as exc:
            raise SemanticRuntimeError("库存数据未通过完整性校验，暂未展示", reason="inventory_quality_failed") from exc
        if (owner_count != len(rows) or owner_count <= 0 or invalid_owner_count or negative_warehouse_count
                or freshness is None or total_inventory != total_available + total_transit or total_inventory < 0):
            raise SemanticRuntimeError("库存数据未通过完整性校验，暂未展示", reason="inventory_quality_failed")
        output, seen, sums = [], set(), [Decimal(0), Decimal(0), Decimal(0)]
        for row in rows:
            owner = str(row.get("cargo_owner") or "").strip()
            if not owner or owner in seen:
                raise SemanticRuntimeError("库存货主标识未通过完整性校验，暂未展示", reason="inventory_quality_failed")
            seen.add(owner)
            available = _parse_decimal(row.get("available_ea"))
            transit = _parse_decimal(row.get("in_transit_ea"))
            inventory = _parse_decimal(row.get("finished_goods_ea"))
            if (int(_parse_decimal(row.get("source_row_count"))) <= 0
                    or int(_parse_decimal(row.get("negative_warehouse_count"))) != 0
                    or inventory != available + transit or inventory < 0):
                raise SemanticRuntimeError("库存数量未通过完整性校验，暂未展示", reason="inventory_quality_failed")
            sums = [left + right for left, right in zip(sums, [available, transit, inventory])]
            output.append((owner, available, transit, inventory))
        if tuple(sums) != (total_available, total_transit, total_inventory):
            raise SemanticRuntimeError("库存汇总未通过完整性校验，暂未展示", reason="inventory_quality_failed")
        columns = [
            {"key": "cargo_owner", "label": "货主编码", "kind": "text"},
            {"key": "available_inventory", "label": "可用库存", "kind": "number", "unit": "EA"},
            {"key": "in_transit_inventory", "label": "在途库存", "kind": "number", "unit": "EA"},
            {"key": "finished_goods_inventory", "label": "成品库存", "kind": "number", "unit": "EA"},
            {"key": "in_transit_share", "label": "在途占比", "kind": "number", "unit": "%", "nullable": True},
            {"key": "inventory_share", "label": "库存占比", "kind": "number", "unit": "%", "nullable": True},
        ]
        table_rows = [{
            "cargo_owner": owner,
            "available_inventory": _format_number(available, places=0),
            "in_transit_inventory": _format_number(transit, places=0),
            "finished_goods_inventory": _format_number(inventory, places=0),
            "in_transit_share": _format_number(transit / inventory * 100, places=2) if inventory else None,
            "inventory_share": _format_number(inventory / total_inventory * 100, places=2) if total_inventory else None,
        } for owner, available, transit, inventory in output]
        table = _display_table(table_id="current_inventory_owner_summary", title="按货主汇总的当前成品库存",
            caption="当前实时可用成品库存加在途 · Asia/Shanghai", columns=columns, rows=table_rows,
            summary=[{"label": "成品库存合计", "value": _format_number(total_inventory, places=0), "unit": "EA"},
                     {"label": "货主数", "value": str(owner_count), "unit": "个"}])
        answer = (f"当前实时成品库存共 {_format_number(total_inventory, places=0)} EA，按货主编码汇总。"
                  "成品库存=可用库存+在途库存；占比按未四舍五入的库存计算。"
                  f"来源更新时间：{freshness.strftime('%Y-%m-%d %H:%M:%S')}（Asia/Shanghai）。")
        return SemanticAnswer(answer, table, result_state="live_success")

    @compile_with_current_inventory
    def _current_inventory_sql(self, request: SemanticRequest) -> str:
        """Build the aggregate-only query for an explicit current inventory request."""

        owner = self.owner.replace("'", "''")
        product_filter = ""
        if request.product_keyword == "sausage":
            product_filter = """
    AND (
      sku_name LIKE '%烤肠%'
      OR sku_name LIKE '%香肠%'
      OR sku_name LIKE '%脆皮肠%'
    )"""
        return f"""WITH sku_scope AS (
  SELECT sku_code,
         MAX(sku_name) AS sku_name,
         COUNT(DISTINCT sku_name) AS sku_name_variant_count
  FROM (
    SELECT TRIM(sku_code) AS sku_code,
           NULLIF(TRIM(sku_name), '') AS sku_name
    FROM ywzt_ods.master_sku
    WHERE TRIM(sku_code) <> ''
      AND TRIM(CAST(sku_group AS STRING)) = '50'{product_filter}
  ) AS sku_source
  GROUP BY sku_code
), source_table_meta AS (
  SELECT MAX(CASE WHEN table_name = 'scm_warehous_manage' THEN update_time END) AS inventory_update_time
  FROM information_schema.tables
  WHERE table_schema = 'ywzt_ods' AND table_name = 'scm_warehous_manage'
), stock_by_key AS (
  SELECT TRIM(i.owner) AS cargo_owner,
         TRIM(i.wms_desc) AS wms_code,
         TRIM(i.sku_code) AS sku_code,
         SUM(COALESCE(i.available_quantity, 0) + COALESCE(i.in_transit_quantity, 0)) AS actual_ea,
         MAX(meta.inventory_update_time) AS latest_freshness
  FROM inventory_current AS i
  JOIN sku_scope AS s ON s.sku_code = TRIM(i.sku_code)
  CROSS JOIN source_table_meta AS meta
  WHERE TRIM(i.owner) = '{owner}'
  GROUP BY TRIM(i.owner), TRIM(i.wms_desc), TRIM(i.sku_code)
), stock_by_warehouse AS (
  SELECT wms_code,
         SUM(actual_ea) AS actual_ea,
         COUNT(*) AS sku_count,
         SUM(CASE WHEN actual_ea < 0 THEN 1 ELSE 0 END) AS negative_key_count,
         MAX(latest_freshness) AS latest_freshness
  FROM stock_by_key
  GROUP BY wms_code
), controls AS (
  SELECT COALESCE(SUM(actual_ea), 0) AS total_ea,
         COUNT(*) AS warehouse_count,
         COALESCE(SUM(sku_count), 0) AS sku_count,
         COALESCE(SUM(negative_key_count), 0) AS negative_key_count,
         COALESCE(SUM(CASE WHEN actual_ea < 0 THEN 1 ELSE 0 END), 0) AS negative_warehouse_count,
         0 AS warehouse_name_conflict_count,
         MAX(latest_freshness) AS latest_freshness
  FROM stock_by_warehouse AS b
)
 SELECT b.wms_code,
        COALESCE(b.wms_code, '') AS warehouse_name,
        b.actual_ea,
       b.sku_count,
       b.negative_key_count,
       b.latest_freshness,
       c.total_ea,
       c.warehouse_count,
       c.sku_count AS total_sku_count,
       c.negative_key_count AS total_negative_key_count,
       c.negative_warehouse_count,
       c.warehouse_name_conflict_count,
       c.latest_freshness AS total_latest_freshness
FROM stock_by_warehouse AS b
CROSS JOIN controls AS c
ORDER BY b.actual_ea DESC, b.wms_code ASC"""

    def _run_current_inventory(self, request: SemanticRequest) -> SemanticAnswer:
        rows = self._execute(
            self._current_inventory_sql(request),
            purpose="current finished-goods inventory",
        )
        product_label = "烤肠" if request.product_keyword == "sausage" else "成品"
        scope_label = request.warehouse_label or "全部仓库"
        if not rows:
            table = _display_table(
                table_id="current_inventory",
                title="当前实时库存",
                caption=f"{scope_label} · {product_label}",
                columns=[
                    {"key": "warehouse", "kind": "text"},
                    {"key": "quantity", "kind": "number", "unit": "EA"},
                ],
                rows=[],
                summary=[
                    {"label": "合计", "value": "0", "unit": "EA"},
                    {"label": "有效 SKU", "value": "0", "unit": "个"},
                ],
                empty_label="当前范围没有可展示的库存数据",
            )
            return SemanticAnswer(
                f"{scope_label}{product_label}当前实时库存：0 EA。当前没有匹配的有效库存记录。",
                table,
            )

        try:
            total = _parse_decimal(rows[0].get("total_ea"), default=Decimal(0))
            warehouse_count = int(
                _parse_decimal(rows[0].get("warehouse_count"), default=Decimal(0))
            )
            sku_count = int(
                _parse_decimal(rows[0].get("total_sku_count"), default=Decimal(0))
            )
            negative_key_count = int(
                _parse_decimal(rows[0].get("total_negative_key_count"), default=Decimal(0))
            )
            negative_warehouse_count = int(
                _parse_decimal(rows[0].get("negative_warehouse_count"), default=Decimal(0))
            )
            name_conflicts = int(
                _parse_decimal(rows[0].get("warehouse_name_conflict_count"), default=Decimal(0))
            )
            freshness = _parse_source_datetime(
                rows[0].get("total_latest_freshness"),
                timezone_name=self.timezone_name,
            )
        except (ValueError, TypeError) as exc:
            raise SemanticRuntimeError(
                "数据未通过完整性校验，暂未展示结果",
                reason="current_inventory_controls_invalid",
            ) from exc
        if (
            total < 0
            or warehouse_count != len(rows)
            or warehouse_count <= 0
            or sku_count <= 0
            or negative_warehouse_count > 0
            or name_conflicts > 0
            or freshness is None
        ):
            raise SemanticRuntimeError(
                "数据未通过完整性校验，暂未展示结果",
                reason="current_inventory_quality_failed",
            )

        detail_requested = request.show_warehouses and not request.warehouse_code
        if detail_requested:
            named_rows: list[tuple[str, str, Decimal]] = []
            displayed_total = Decimal(0)
            for row in rows:
                raw_name = str(row.get("warehouse_name") or "").strip()
                code = str(row.get("wms_code") or "").strip()
                name = raw_name or code or "未匹配仓库"
                quantity = _parse_decimal(row.get("actual_ea"), default=Decimal(0))
                if quantity < 0:
                    raise SemanticRuntimeError(
                        "数据未通过完整性校验，暂未展示结果",
                        reason="current_inventory_row_invalid",
                    )
                named_rows.append((name, code, quantity))
                displayed_total += quantity
            if displayed_total != total:
                raise SemanticRuntimeError(
                    "数据未通过完整性校验，暂未展示结果",
                    reason="current_inventory_total_mismatch",
                )
            name_counts: dict[str, int] = {}
            for name, _code, _quantity in named_rows:
                name_counts[name] = name_counts.get(name, 0) + 1
            table_rows = []
            for name, code, quantity in named_rows:
                display_name = name
                if name_counts[name] > 1 and code:
                    display_name = f"{name}（{code}）"
                table_rows.append(
                    {"warehouse": display_name, "quantity": _format_number(quantity, places=0)}
                )
        else:
            table_rows = [
                {
                    "warehouse": request.warehouse_label or "全部仓库",
                    "quantity": _format_number(total, places=0),
                }
            ]
        answer = (
            f"{scope_label}{product_label}当前实时库存：{_format_number(total, places=0)} EA。"
            "口径：可用成品库存加在途。"
            f"库存数据更新时间：{freshness.strftime('%Y-%m-%d %H:%M:%S')}（Asia/Shanghai）。"
        )
        if negative_key_count:
            answer += "含负数库存项，已按原值计入合计。"
        table = _display_table(
            table_id="current_inventory",
            title="当前实时库存",
            caption=f"{scope_label} · {product_label} · 当前实时库存数量",
            columns=[
                {"key": "warehouse", "kind": "text"},
                {"key": "quantity", "kind": "number", "unit": "EA"},
            ],
            rows=table_rows,
            summary=[
                {"label": "合计", "value": _format_number(total, places=0), "unit": "EA"},
                {"label": "有效 SKU", "value": str(sku_count), "unit": "个"},
                {"label": "仓库数", "value": str(warehouse_count), "unit": "个"},
            ],
        )
        return SemanticAnswer(answer, table)

"SalesHandler SQL and result reconciliation."

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
    _period_label,
)
from .base import MetricHandler
from ...outbound_contract import completed_status_sql, ref_order_type_sql


def _ranking_count(value: Any) -> int:
    count = _parse_decimal(value)
    if count < 0 or count != count.to_integral_value():
        raise ValueError("invalid ranking count")
    return int(count)


class SalesHandler(MetricHandler):
    def _sales_sql(self, start: date, end: date, *, transfer: bool = False) -> str:
        start_text = self._sql_date(start)
        end_text = self._sql_date(end)
        owner = self.owner.replace("'", "''")
        if transfer:
            order_source_filter = """
    AND TRIM(h.ref_order_type) = 'DBCK'"""
            order_link_filter = ""
        else:
            order_source_filter = f"""
    AND {ref_order_type_sql()}
    AND TRIM(h.ref_order_code) <> ''"""
            order_link_filter = """
    AND EXISTS (
      SELECT 1
      FROM ywzt_ods.scm_sales_order_header AS so
      WHERE TRIM(so.sales_order_code) = TRIM(h.ref_order_code)
        AND TRIM(so.company_code) = TRIM(h.company_code)
        AND COALESCE(so.deleted_flag, 0) = 0
        AND TRIM(so.sales_order_code) <> ''
        AND IFNULL(so._fdl_marked_deleted, 'false') = 'false'
    )"""
        return f"""WITH wms_scope AS (
  SELECT wms_code, MAX(warehouse_name) AS warehouse_name
  FROM (
    SELECT TRIM(wms_code) AS wms_code,
           TRIM(wms_desc) AS warehouse_name,
           ROW_NUMBER() OVER (
             PARTITION BY TRIM(CAST(cargo_owner_id AS STRING)), TRIM(wms_code)
             ORDER BY updated_date DESC, version DESC, id DESC
           ) AS rn
    FROM ywzt_ods.master_wms
    WHERE TRIM(CAST(cargo_owner_id AS STRING)) = '{owner}'
      AND COALESCE(TRIM(CAST(status AS STRING)), '0') = '0'
      AND TRIM(wms_code) <> ''
  ) AS ranked_wms
  WHERE rn = 1
  GROUP BY wms_code
), header_ranked AS (
  SELECT TRIM(h.outbound_code) AS outbound_code,
         TRIM(h.company_code) AS cargo_owner,
         TRIM(h.facility_code) AS wms_code,
         ROW_NUMBER() OVER (
           PARTITION BY TRIM(h.company_code), TRIM(h.outbound_code)
           ORDER BY h.updated DESC, h.version DESC, h.id DESC
         ) AS rn
  FROM ywzt_ods.scm_sales_shipment_header AS h
  WHERE TRIM(h.company_code) = '{owner}'
    AND h.ship_at >= '{start_text} 00:00:00'
    AND h.ship_at < '{end_text} 00:00:00'
     AND {completed_status_sql()}
    AND COALESCE(h.delete_flag, 0) = 0
    AND IFNULL(h._fdl_marked_deleted, 'false') = 'false'
    {order_source_filter}
    {order_link_filter}
), valid_headers AS (
  SELECT outbound_code, cargo_owner, wms_code
  FROM header_ranked
  WHERE rn = 1
), detail_by_warehouse AS (
  SELECT h.wms_code,
         SUM(COALESCE(d.ship_qty, 0)) AS outbound_ea
  FROM valid_headers AS h
  JOIN ywzt_ods.scm_sales_shipment_detail AS d
    ON TRIM(d.outbound_code) = h.outbound_code
   AND TRIM(d.company_code) = h.cargo_owner
  WHERE COALESCE(d.delete_flag, 0) = 0
    AND IFNULL(d._fdl_marked_deleted, 'false') = 'false'
  GROUP BY h.wms_code
), totals AS (
  SELECT COALESCE(SUM(d.outbound_ea), 0) AS total_ea,
         COUNT(*) AS warehouse_count
  FROM detail_by_warehouse AS d
)
        SELECT d.wms_code,
               COALESCE(w.warehouse_name, '') AS warehouse_name,
       d.outbound_ea,
       t.total_ea,
       t.warehouse_count
FROM detail_by_warehouse AS d
LEFT JOIN wms_scope AS w ON w.wms_code = d.wms_code
CROSS JOIN totals AS t
ORDER BY d.outbound_ea DESC"""

    def _run_warehouse_outbound(self, request: SemanticRequest, *, transfer: bool = False) -> SemanticAnswer:
        rows = self._execute(
            self._sales_sql(request.start, request.end, transfer=transfer),
            purpose="actual transfer outbound by warehouse" if transfer else "actual sales outbound by warehouse",
        )
        label = _period_label(request.start, request.end, request.period_label)
        if not rows:
            return SemanticAnswer(
                f"{label}{'调拨' if transfer else '实际销售'}出库：0 EA。当前没有匹配的有效出库记录。",
                _display_table(
                    table_id="sales_outbound_by_warehouse",
                    title=("按仓调拨出库" if transfer else "按仓销售出库") if request.show_warehouses else ("调拨出库合计" if transfer else "销售出库合计"),
                    caption=f"{label} · {'完成调拨出库' if transfer else '实际完成销售出库'}",
                    columns=[
                        {"key": "warehouse", "kind": "text"},
                        {"key": "quantity", "kind": "number", "unit": "EA"},
                    ],
                    rows=[],
                    summary=[
                        {"label": "合计", "value": "0", "unit": "EA"},
                        {"label": "仓库数", "value": "0", "unit": "个"},
                    ],
                    empty_label="当前日期没有匹配的有效销售出库记录",
                ),
            )
        total_values = [row.get("total_ea") for row in rows]
        try:
            total = _parse_decimal(total_values[0])
            warehouse_count = int(_parse_decimal(rows[0].get("warehouse_count")))
        except (ValueError, TypeError, IndexError) as exc:
            raise SemanticRuntimeError("数据未通过完整性校验，暂未展示结果", reason="sales_controls_invalid") from exc
        if total < 0 or warehouse_count != len(rows):
            raise SemanticRuntimeError("数据未通过完整性校验，暂未展示结果", reason="sales_controls_mismatch")
        names: list[tuple[str, str, Decimal]] = []
        for row in rows:
            try:
                row_total = _parse_decimal(row.get("total_ea"))
                quantity = _parse_decimal(row.get("outbound_ea"))
                name = str(row.get("warehouse_name") or "").strip()
                code = str(row.get("wms_code") or "").strip()
            except (ValueError, TypeError) as exc:
                raise SemanticRuntimeError("数据未通过完整性校验，暂未展示结果", reason="sales_row_invalid") from exc
            if row_total != total or quantity < 0:
                raise SemanticRuntimeError("数据未通过完整性校验，暂未展示结果", reason="sales_reconciliation_failed")
            names.append((name or code or "未匹配仓库", code, quantity))
        displayed_total = sum((quantity for _name, _code, quantity in names), Decimal(0))
        if displayed_total != total:
            raise SemanticRuntimeError("数据未通过完整性校验，暂未展示结果", reason="sales_total_mismatch")
        lines = [f"{label}{'调拨' if transfer else '实际销售'}出库合计：{_format_number(total, places=0)} EA。"]
        if request.show_warehouses:
            name_counts: dict[str, int] = {}
            for name, _code, _quantity in names:
                name_counts[name] = name_counts.get(name, 0) + 1
            display_names: list[tuple[str, Decimal]] = []
            for name, code, quantity in names:
                display_name = name
                if name_counts[name] > 1 and code:
                    display_name = f"{name}（{code}）"
                display_names.append((display_name, quantity))
            lines.append("按仓汇总：")
            for offset in range(0, len(display_names), 2):
                lines.append("；".join(
                    f"{name} { _format_number(quantity, places=0) } EA"
                    for name, quantity in display_names[offset : offset + 2]
                ))
        else:
            display_names = []
        table = _display_table(
            table_id="transfer_outbound_by_warehouse" if transfer else "sales_outbound_by_warehouse",
            title=("按仓调拨出库" if transfer else "按仓销售出库") if request.show_warehouses else ("调拨出库合计" if transfer else "销售出库合计"),
            caption=f"{label} · {'完成调拨出库' if transfer else '实际完成销售出库'}",
            columns=[
                {"key": "warehouse", "kind": "text"},
                {"key": "quantity", "kind": "number", "unit": "EA"},
            ],
            rows=(
                [
                    {
                        "warehouse": name,
                        "quantity": _format_number(quantity, places=0),
                    }
                    for name, quantity in display_names
                ]
                if request.show_warehouses
                else [
                    {
                        "warehouse": request.warehouse_label or "全部仓库",
                        "quantity": _format_number(total, places=0),
                    }
                ]
            ),
            summary=[
                {"label": "合计", "value": _format_number(total, places=0), "unit": "EA"},
                {"label": "仓库数", "value": str(warehouse_count), "unit": "个"},
            ],
        )
        return SemanticAnswer("\n".join(lines), table)

    def _run_sales_outbound(self, request: SemanticRequest) -> SemanticAnswer:
        return self._run_warehouse_outbound(request, transfer=False)

    def _run_transfer_outbound(self, request: SemanticRequest) -> SemanticAnswer:
        return self._run_warehouse_outbound(request, transfer=True)

    def _sales_by_type_sql(
        self,
        start: date,
        end: date,
        *,
        fulfillment_type: str | None = None,
        group_by_owner: bool = False,
    ) -> str:
        """Build the reviewed type/2C-2B aggregate on actual shipped EA."""

        start_text = self._sql_date(start)
        end_text = self._sql_date(end)
        owner = self.owner.replace("'", "''")
        group_key = "cargo_owner" if group_by_owner else "ref_order_type"
        fulfillment_filter = ""
        if fulfillment_type in {"2C", "2B"}:
            fulfillment_filter = (
                "\n    AND (CASE WHEN TRIM(CAST(h.terms_of_shipment AS STRING)) = '20' "
                f"THEN '2C' ELSE '2B' END) = '{fulfillment_type}'"
            )
        return f"""WITH header_ranked AS (
  SELECT TRIM(h.outbound_code) AS outbound_code,
         TRIM(h.company_code) AS cargo_owner,
         TRIM(h.ref_order_type) AS ref_order_type,
         CASE WHEN TRIM(CAST(h.terms_of_shipment AS STRING)) = '20'
              THEN '2C' ELSE '2B' END AS fulfillment_type,
         ROW_NUMBER() OVER (
           PARTITION BY TRIM(h.company_code), TRIM(h.outbound_code)
           ORDER BY h.updated DESC, h.version DESC, h.id DESC
         ) AS rn
  FROM ywzt_ods.scm_sales_shipment_header AS h
  WHERE TRIM(h.company_code) = '{owner}'
    AND h.ship_at >= '{start_text} 00:00:00'
    AND h.ship_at < '{end_text} 00:00:00'
    AND {completed_status_sql()}
    AND COALESCE(h.delete_flag, 0) = 0
    AND IFNULL(h._fdl_marked_deleted, 'false') = 'false'
    AND {ref_order_type_sql()}{fulfillment_filter}
    AND TRIM(h.ref_order_code) <> ''
    AND EXISTS (
      SELECT 1
      FROM ywzt_ods.scm_sales_order_header AS so
      WHERE TRIM(so.sales_order_code) = TRIM(h.ref_order_code)
        AND TRIM(so.company_code) = TRIM(h.company_code)
        AND COALESCE(so.deleted_flag, 0) = 0
        AND TRIM(so.sales_order_code) <> ''
        AND IFNULL(so._fdl_marked_deleted, 'false') = 'false'
    )
), valid_headers AS (
  SELECT outbound_code, cargo_owner, ref_order_type, fulfillment_type
  FROM header_ranked
  WHERE rn = 1
), grouped AS (
  SELECT h.{group_key},
         h.fulfillment_type,
         SUM(COALESCE(d.ship_qty, 0)) AS outbound_ea
  FROM valid_headers AS h
  JOIN ywzt_ods.scm_sales_shipment_detail AS d
    ON TRIM(d.outbound_code) = h.outbound_code
   AND TRIM(d.company_code) = h.cargo_owner
  WHERE COALESCE(d.delete_flag, 0) = 0
    AND IFNULL(d._fdl_marked_deleted, 'false') = 'false'
  GROUP BY h.{group_key}, h.fulfillment_type
), controls AS (
  SELECT COALESCE(SUM(outbound_ea), 0) AS total_ea,
         COUNT(*) AS group_count
  FROM grouped
)
SELECT g.{group_key},
       g.fulfillment_type,
       g.outbound_ea,
       c.total_ea,
       c.group_count
FROM grouped AS g
CROSS JOIN controls AS c
ORDER BY g.{group_key} ASC, g.fulfillment_type ASC"""

    def _run_sales_outbound_by_type(self, request: SemanticRequest) -> SemanticAnswer:
        by_owner = request.route == "sales_outbound_by_owner"
        group_key = "cargo_owner" if by_owner else "ref_order_type"
        group_label = "货主" if by_owner else "出库类型"
        rows = self._execute(
            self._sales_by_type_sql(
                request.start,
                request.end,
                fulfillment_type=request.fulfillment_type,
                group_by_owner=by_owner,
            ),
            purpose="actual sales outbound by owner and fulfillment type" if by_owner else "actual sales outbound by order and fulfillment type",
        )
        label = _period_label(request.start, request.end, request.period_label)
        total = Decimal(0)
        group_count = 0
        sum_quantity = Decimal(0)
        seen_groups: set[tuple[str, str]] = set()
        table_rows: list[dict[str, str]] = []
        for row in rows:
            try:
                quantity = _parse_decimal(row.get("outbound_ea"))
                row_total = _parse_decimal(row.get("total_ea"))
                row_count = _parse_decimal(row.get("group_count"))
            except (ValueError, TypeError) as exc:
                raise SemanticRuntimeError("数据未通过完整性校验，暂未展示结果", reason="sales_type_row_invalid") from exc
            if quantity < 0:
                raise SemanticRuntimeError("数据未通过完整性校验，暂未展示结果", reason="sales_type_negative_quantity")
            if not table_rows:
                total, group_count = row_total, row_count
            if row_total != total or row_count != len(rows):
                raise SemanticRuntimeError("数据未通过完整性校验，暂未展示结果", reason="sales_type_reconciliation_failed")
            group = (str(row.get(group_key) or "").strip(), str(row.get("fulfillment_type") or "").strip())
            if not group[0] or group[1] not in {"2C", "2B"} or group in seen_groups:
                raise SemanticRuntimeError("数据未通过完整性校验，暂未展示结果", reason="sales_type_group_invalid")
            seen_groups.add(group)
            sum_quantity += quantity
            table_rows.append({
                group_key: group[0],
                "fulfillment_type": group[1],
                "quantity": _format_number(quantity, places=3),
            })
        if not rows:
            total = Decimal(0)
        if sum_quantity != total:
            raise SemanticRuntimeError("数据未通过完整性校验，暂未展示结果", reason="sales_type_total_mismatch")
        table = _display_table(
            table_id=request.route,
            title=f"按{group_label}销售出库",
            caption=f"{label} · 实际完成销售出库 · 按{group_label}与履约类型",
            columns=[
                {"key": group_key, "kind": "text"},
                {"key": "fulfillment_type", "kind": "text"},
                {"key": "quantity", "kind": "number", "unit": "EA"},
            ],
            rows=table_rows,
            summary=[
                {"label": "合计", "value": _format_number(total, places=0), "unit": "EA"},
            ],
            empty_label="当前日期没有匹配的有效销售出库记录",
        )
        return SemanticAnswer(
            f"{label}实际销售出库合计：{_format_number(total, places=0)} EA。",
            table,
        )

    def _sales_top_sku_sql(
        self,
        start: date,
        end: date,
        top_n: int,
        *,
        ref_order_type: str | None = None,
        finished_goods_only: bool = False,
    ) -> str:
        """Build the governed SKU ranking query for completed sales outbound."""

        start_text = self._sql_date(start)
        end_text = self._sql_date(end)
        owner = self.owner.replace("'", "''")
        bounded_top_n = max(1, min(int(top_n), 100))
        type_filter = ""
        if ref_order_type in {"JYCK", "HHCK", "BFCK", "QTCK"} or (
            isinstance(ref_order_type, str) and ref_order_type.startswith("SO")
        ):
            safe_type = ref_order_type.replace("'", "''")
            type_filter = f"\n    AND TRIM(h.ref_order_type) = '{safe_type}'"
        finished_filter = "\n    AND TRIM(CAST(sku_group AS STRING)) = '50'" if finished_goods_only else ""
        finished_join = "\n  JOIN sku_scope AS finished_skus ON finished_skus.sku_code = TRIM(d.sku_code)" if finished_goods_only else ""
        return f"""WITH sku_scope AS (
  SELECT TRIM(sku_code) AS sku_code,
         MAX(NULLIF(TRIM(sku_name), '')) AS sku_name,
         COUNT(DISTINCT NULLIF(TRIM(sku_name), '')) AS sku_name_variant_count
  FROM ywzt_ods.master_sku
  WHERE TRIM(sku_code) <> ''
    {finished_filter.strip()}
  GROUP BY TRIM(sku_code)
), valid_headers AS (
  SELECT TRIM(h.outbound_code) AS outbound_code,
         TRIM(h.company_code) AS cargo_owner
  FROM ywzt_ods.scm_sales_shipment_header AS h
  WHERE TRIM(h.company_code) = '{owner}'
    AND h.ship_at >= '{start_text} 00:00:00'
    AND h.ship_at < '{end_text} 00:00:00'
     AND {completed_status_sql()}
    AND COALESCE(h.delete_flag, 0) = 0
    AND IFNULL(h._fdl_marked_deleted, 'false') = 'false'
     AND {ref_order_type_sql()}
    {type_filter.strip()}
    AND TRIM(h.ref_order_code) <> ''
    AND EXISTS (
      SELECT 1
      FROM ywzt_ods.scm_sales_order_header AS so
      WHERE TRIM(so.sales_order_code) = TRIM(h.ref_order_code)
        AND TRIM(so.company_code) = TRIM(h.company_code)
        AND COALESCE(so.deleted_flag, 0) = 0
        AND IFNULL(so._fdl_marked_deleted, 'false') = 'false'
    )
  GROUP BY TRIM(h.outbound_code), TRIM(h.company_code)
), sku_totals AS (
  SELECT TRIM(d.sku_code) AS sku_code,
         SUM(COALESCE(d.ship_qty, 0)) AS outbound_ea,
         COUNT(DISTINCT h.cargo_owner, h.outbound_code) AS outbound_order_count,
         SUM(CASE WHEN COALESCE(d.ship_qty, 0) < 0 THEN 1 ELSE 0 END) AS negative_detail_count
  FROM valid_headers AS h
  JOIN ywzt_ods.scm_sales_shipment_detail AS d
    ON TRIM(d.outbound_code) = h.outbound_code
   AND TRIM(d.company_code) = h.cargo_owner
{finished_join}
  WHERE COALESCE(d.delete_flag, 0) = 0
    AND IFNULL(d._fdl_marked_deleted, 'false') = 'false'
  GROUP BY TRIM(d.sku_code)
), controls AS (
  SELECT COALESCE(SUM(t.outbound_ea), 0) AS total_ea,
         COUNT(*) AS all_sku_count,
         COALESCE(SUM(CASE WHEN COALESCE(t.sku_code, '') = '' THEN 1 ELSE 0 END), 0) AS blank_sku_count,
         COALESCE(SUM(CASE WHEN COALESCE(s.sku_name_variant_count, 0) > 1 THEN 1 ELSE 0 END), 0) AS sku_name_conflict_count,
         COALESCE(SUM(t.negative_detail_count), 0) AS negative_detail_count
  FROM sku_totals AS t
  LEFT JOIN sku_scope AS s ON s.sku_code = t.sku_code
)
SELECT t.sku_code,
       COALESCE(NULLIF(TRIM(s.sku_name), ''), t.sku_code) AS sku_name,
       t.outbound_ea,
       t.outbound_order_count,
       c.total_ea,
       c.all_sku_count,
       c.blank_sku_count,
       c.sku_name_conflict_count,
       c.negative_detail_count
FROM sku_totals AS t
LEFT JOIN sku_scope AS s ON s.sku_code = t.sku_code
CROSS JOIN controls AS c
ORDER BY t.outbound_ea DESC, t.sku_code ASC
LIMIT {bounded_top_n}"""

    def _run_sales_outbound_top_sku(self, request: SemanticRequest) -> SemanticAnswer:
        top_n = 20 if request.top_n is None else request.top_n
        if type(top_n) is not int or not 1 <= top_n <= 100:
            raise SemanticRuntimeError(
                "排名数量无效，暂未执行查询", reason="sales_top_sku_limit_mismatch",
            )
        rows = self._execute(
            self._sales_top_sku_sql(
                request.start,
                request.end,
                top_n,
                ref_order_type=request.ref_order_type,
                finished_goods_only=request.finished_goods_only,
            ),
            purpose="actual sales outbound top sku",
        )
        label = _period_label(request.start, request.end, request.period_label)
        if not rows:
            return SemanticAnswer(
                f"{label}销售出库 Top {top_n} SKU：没有匹配的有效出库记录。",
                _display_table(
                    table_id="sales_outbound_top_sku",
                    title=f"销售出库 Top {top_n} SKU",
                    caption=f"{label} · Top {top_n} · 按销售出库 EA 降序",
                    columns=[
                        {"key": "rank", "kind": "number"},
                        {"key": "sku_name", "kind": "text"},
                        {"key": "sku_code", "kind": "text"},
                        {"key": "quantity", "kind": "number", "unit": "EA"},
                        {"key": "order_count", "kind": "number", "unit": "单"},
                    ],
                    rows=[],
                    summary=[
                        {"label": "有效 SKU", "value": "0", "unit": "个"},
                        {"label": "统计范围销售出库", "value": "0", "unit": "EA"},
                    ],
                    empty_label="当前日期没有匹配的有效销售出库记录",
                ),
            )
        if len(rows) > top_n:
            raise SemanticRuntimeError(
                "数据未通过完整性校验，暂未展示结果",
                reason="sales_top_sku_limit_mismatch",
            )
        try:
            total_ea = _parse_decimal(rows[0].get("total_ea"))
            all_sku_count = _ranking_count(rows[0].get("all_sku_count"))
            blank_sku_count = _ranking_count(rows[0].get("blank_sku_count"))
            sku_name_conflict_count = _ranking_count(rows[0].get("sku_name_conflict_count"))
            negative_detail_count = _ranking_count(rows[0].get("negative_detail_count"))
        except (ValueError, TypeError) as exc:
            raise SemanticRuntimeError(
                "数据未通过完整性校验，暂未展示结果",
                reason="sales_top_sku_controls_invalid",
            ) from exc
        if (
            total_ea < 0
            or len(rows) != min(top_n, all_sku_count)
            or blank_sku_count > 0
            or sku_name_conflict_count > 0
            or negative_detail_count > 0
        ):
            raise SemanticRuntimeError(
                "数据未通过完整性校验，暂未展示结果",
                reason="sales_top_sku_quality_failed",
            )

        items: list[tuple[str, str, Decimal, int]] = []
        seen_codes: set[str] = set()
        previous_quantity: Decimal | None = None
        previous_code: str | None = None
        top_total = Decimal(0)
        for row in rows:
            sku_code = str(row.get("sku_code") or "").strip()
            sku_name = str(row.get("sku_name") or "").strip()
            try:
                quantity = _parse_decimal(row.get("outbound_ea"))
                order_count = _ranking_count(row.get("outbound_order_count"))
                row_total = _parse_decimal(row.get("total_ea"))
                row_sku_count = _ranking_count(row.get("all_sku_count"))
                row_blank_sku_count = _ranking_count(row.get("blank_sku_count"))
                row_name_conflict_count = _ranking_count(row.get("sku_name_conflict_count"))
                row_negative_detail_count = _ranking_count(row.get("negative_detail_count"))
            except (ValueError, TypeError) as exc:
                raise SemanticRuntimeError(
                    "数据未通过完整性校验，暂未展示结果",
                    reason="sales_top_sku_row_invalid",
                ) from exc
            if (
                not sku_code
                or not sku_name
                or sku_code in seen_codes
                or quantity < 0
                or order_count < 0
                or row_total != total_ea
                or row_sku_count != all_sku_count
                or row_blank_sku_count != blank_sku_count
                or row_name_conflict_count != sku_name_conflict_count
                or row_negative_detail_count != negative_detail_count
                or (previous_quantity is not None and quantity > previous_quantity)
                or (quantity == previous_quantity and previous_code is not None and sku_code < previous_code)
            ):
                raise SemanticRuntimeError(
                    "数据未通过完整性校验，暂未展示结果",
                    reason="sales_top_sku_reconciliation_failed",
                )
            seen_codes.add(sku_code)
            previous_quantity = quantity
            previous_code = sku_code
            top_total += quantity
            items.append((sku_code, sku_name, quantity, order_count))
        # If the entire population fits in the ranking, a smaller sum indicates
        # missing/changed data, not a legitimate partial Top-N partition.
        if top_total > total_ea or (all_sku_count <= top_n and top_total != total_ea):
            raise SemanticRuntimeError(
                "数据未通过完整性校验，暂未展示结果",
                reason="sales_top_sku_total_mismatch",
            )
        lines = [
            f"{label}销售出库 Top {top_n} SKU（按 EA 降序）：",
        ]
        for offset in range(0, len(items), 2):
            lines.append("；".join(
                f"{name}（{code}） {_format_number(quantity, places=0)} EA / {order_count} 单"
                for code, name, quantity, order_count in items[offset : offset + 2]
            ))
        lines.append(
            f"统计范围内有效销售出库合计：{_format_number(total_ea, places=0)} EA，共 {all_sku_count} 个 SKU。"
        )
        table = _display_table(
            table_id="sales_outbound_top_sku",
            title=f"销售出库 Top {top_n} SKU",
            caption=f"{label} · Top {top_n} · 按销售出库 EA 降序",
            columns=[
                {"key": "rank", "kind": "number"},
                {"key": "sku_name", "kind": "text"},
                {"key": "sku_code", "kind": "text"},
                {"key": "quantity", "kind": "number", "unit": "EA"},
                {"key": "order_count", "kind": "number", "unit": "单"},
            ],
            rows=[
                {
                    "rank": str(index),
                    "sku_name": name,
                    "sku_code": code,
                    "quantity": _format_number(quantity, places=0),
                    "order_count": str(order_count),
                }
                for index, (code, name, quantity, order_count) in enumerate(items, start=1)
            ],
            summary=[
                {"label": "有效 SKU", "value": str(all_sku_count), "unit": "个"},
                {"label": "统计范围销售出库", "value": _format_number(total_ea, places=0), "unit": "EA"},
            ],
        )
        return SemanticAnswer("\n".join(lines), table)

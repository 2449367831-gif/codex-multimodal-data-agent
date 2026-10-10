"InboundHandler SQL and result reconciliation."

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


class InboundHandler(MetricHandler):
    def _inbound_sql(
        self, start: date, end: date, *, purchase_only: bool = False
    ) -> str:
        start_text = self._sql_date(start)
        end_text = self._sql_date(end)
        owner = self.owner.replace("'", "''")
        purchase_filter = (
            "    AND TRIM(CAST(h.warehous_type AS STRING)) = '10'\n"
            if purchase_only
            else ""
        )
        return f"""WITH sku_ranked AS (
  SELECT TRIM(sku_code) AS sku_code,
         TRIM(sku_name) AS sku_name,
         COALESCE(net_weight, 0) AS net_weight,
         ROW_NUMBER() OVER (
           PARTITION BY TRIM(sku_code)
           ORDER BY updated_date DESC, version DESC, id DESC
         ) AS rn
  FROM ywzt_ods.master_sku
  WHERE TRIM(CAST(cargo_owner AS STRING)) = '{owner}'
    AND TRIM(sku_code) <> ''
), sku_scope AS (
  SELECT sku_code, sku_name, net_weight
  FROM sku_ranked
  WHERE rn = 1
), wms_ranked AS (
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
), wms_scope AS (
  SELECT wms_code, MAX(warehouse_name) AS warehouse_name
  FROM wms_ranked
  WHERE rn = 1
  GROUP BY wms_code
), header_ranked AS (
  SELECT TRIM(h.warehous_code) AS warehous_code,
         TRIM(CAST(h.cargo_owner_id AS STRING)) AS cargo_owner,
         ROW_NUMBER() OVER (
           PARTITION BY TRIM(CAST(h.cargo_owner_id AS STRING)), TRIM(h.warehous_code)
           ORDER BY h.updated_date DESC, h.version DESC, h.id DESC
         ) AS rn
  FROM ywzt_ods.scm_warehous_header AS h
  WHERE TRIM(CAST(h.cargo_owner_id AS STRING)) = '{owner}'
    AND h.actual_arrival_date >= '{start_text} 00:00:00'
    AND h.actual_arrival_date < '{end_text} 00:00:00'
    AND COALESCE(TRIM(CAST(h.status AS STRING)), '0') = '0'
    AND COALESCE(TRIM(CAST(h.approve_status AS STRING)), '') = '12'
{purchase_filter}    AND IFNULL(h._fdl_marked_deleted, 'false') = 'false'
), valid_headers AS (
  SELECT warehous_code, cargo_owner
  FROM header_ranked
  WHERE rn = 1
), inbound_base AS (
  SELECT TRIM(d.wms_code) AS wms_code,
         TRIM(d.sku_code) AS sku_code,
         SUM(COALESCE(d.received_quantity, 0)) AS inbound_ea,
         SUM(COALESCE(d.received_quantity, 0) * COALESCE(s.net_weight, 0) / 1000) AS inbound_tons,
         SUM(CASE WHEN s.sku_code IS NULL OR COALESCE(s.net_weight, 0) = 0
                  THEN COALESCE(d.received_quantity, 0) ELSE 0 END) AS unweighted_ea,
         SUM(CASE WHEN s.sku_code IS NULL THEN COALESCE(d.received_quantity, 0) ELSE 0 END) AS missing_master_ea
  FROM valid_headers AS h
  JOIN ywzt_ods.scm_warehous_detail AS d
    ON TRIM(d.warehous_code) = h.warehous_code
   AND TRIM(CAST(d.cargo_owner_id AS STRING)) = h.cargo_owner
  LEFT JOIN sku_scope AS s ON s.sku_code = TRIM(d.sku_code)
  WHERE COALESCE(TRIM(CAST(d.status AS STRING)), '0') = '0'
    AND IFNULL(d._fdl_marked_deleted, 'false') = 'false'
  GROUP BY TRIM(d.wms_code), TRIM(d.sku_code)
), warehouse_totals AS (
  SELECT b.wms_code,
         COALESCE(w.warehouse_name, '') AS warehouse_name,
         SUM(b.inbound_ea) AS inbound_ea,
         SUM(b.inbound_tons) AS inbound_tons,
         SUM(b.unweighted_ea) AS unweighted_ea,
         SUM(b.missing_master_ea) AS missing_master_ea
  FROM inbound_base AS b
  LEFT JOIN wms_scope AS w ON w.wms_code = b.wms_code
  GROUP BY b.wms_code, COALESCE(w.warehouse_name, '')
), sku_totals AS (
  SELECT b.sku_code,
         COALESCE(s.sku_name, '') AS sku_name,
         SUM(b.inbound_ea) AS inbound_ea,
         SUM(b.inbound_tons) AS inbound_tons,
         SUM(b.unweighted_ea) AS unweighted_ea,
         SUM(b.missing_master_ea) AS missing_master_ea
  FROM inbound_base AS b
  LEFT JOIN sku_scope AS s ON s.sku_code = b.sku_code
  GROUP BY b.sku_code, COALESCE(s.sku_name, '')
), summary AS (
  SELECT COALESCE(SUM(inbound_ea), 0) AS total_ea,
         COALESCE(SUM(inbound_tons), 0) AS total_tons,
         COALESCE(SUM(unweighted_ea), 0) AS unweighted_ea,
         COALESCE(SUM(missing_master_ea), 0) AS missing_master_ea,
         COUNT(*) AS warehouse_count
  FROM warehouse_totals
)
 SELECT 'summary' AS grain, '' AS wms_code, '' AS sku_code,
        '' AS warehouse_name, '' AS sku_name,
       total_ea AS inbound_ea, total_tons AS inbound_tons,
       unweighted_ea, missing_master_ea, warehouse_count
FROM summary
UNION ALL
 SELECT 'warehouse' AS grain, wms_code, '' AS sku_code,
        warehouse_name, '' AS sku_name,
       inbound_ea, inbound_tons, unweighted_ea, missing_master_ea,
       (SELECT warehouse_count FROM summary) AS warehouse_count
FROM warehouse_totals
UNION ALL
 SELECT 'sku' AS grain, '' AS wms_code, sku_code, '' AS warehouse_name, sku_name,
       inbound_ea, inbound_tons, unweighted_ea, missing_master_ea,
       (SELECT warehouse_count FROM summary) AS warehouse_count
FROM sku_totals"""

    def _run_inbound_summary(self, request: SemanticRequest) -> SemanticAnswer:
        period_label = _period_label(request.start, request.end)
        rows = self._execute(
            self._inbound_sql(
                request.start,
                request.end,
                purchase_only=request.inbound_basis == "inbound_type_10",
            ),
            purpose="actual inbound quantity and tons by warehouse",
        )
        summary_rows = [row for row in rows if row.get("grain") == "summary"]
        warehouse_rows = [row for row in rows if row.get("grain") == "warehouse"]
        sku_rows = [row for row in rows if row.get("grain") == "sku"]
        if len(summary_rows) != 1:
            raise SemanticRuntimeError("数据未通过完整性校验，暂未展示结果", reason="inbound_summary_missing")
        summary = summary_rows[0]
        try:
            total_ea = _parse_decimal(summary.get("inbound_ea"), default=Decimal(0))
            total_tons = _parse_decimal(summary.get("inbound_tons"), default=Decimal(0))
            unweighted_ea = _parse_decimal(summary.get("unweighted_ea"), default=Decimal(0))
            missing_master_ea = _parse_decimal(summary.get("missing_master_ea"), default=Decimal(0))
            warehouse_count = int(_parse_decimal(summary.get("warehouse_count"), default=Decimal(0)))
        except (ValueError, TypeError) as exc:
            raise SemanticRuntimeError("数据未通过完整性校验，暂未展示结果", reason="inbound_summary_invalid") from exc
        if (
            min(total_ea, total_tons, unweighted_ea, missing_master_ea) < 0
            or warehouse_count != len(warehouse_rows)
        ):
            raise SemanticRuntimeError("数据未通过完整性校验，暂未展示结果", reason="inbound_quality_failed")
        warehouse_total = Decimal(0)
        warehouse_tons_total = Decimal(0)
        for row in warehouse_rows:
            try:
                quantity = _parse_decimal(row.get("inbound_ea"), default=Decimal(0))
                tons = _parse_decimal(row.get("inbound_tons"), default=Decimal(0))
                row_warehouse_count = int(
                    _parse_decimal(row.get("warehouse_count"), default=Decimal(0))
                )
            except (ValueError, TypeError) as exc:
                raise SemanticRuntimeError("数据未通过完整性校验，暂未展示结果", reason="inbound_warehouse_invalid") from exc
            if quantity < 0 or tons < 0 or row_warehouse_count != warehouse_count:
                raise SemanticRuntimeError("数据未通过完整性校验，暂未展示结果", reason="inbound_warehouse_mapping")
            warehouse_total += quantity
            warehouse_tons_total += tons
        if warehouse_total != total_ea or warehouse_tons_total != total_tons:
            raise SemanticRuntimeError("数据未通过完整性校验，暂未展示结果", reason="inbound_total_mismatch")

        sku_total = Decimal(0)
        sku_tons_total = Decimal(0)
        for row in sku_rows:
            quantity = _parse_decimal(row.get("inbound_ea"), default=Decimal(0))
            tons = _parse_decimal(row.get("inbound_tons"), default=Decimal(0))
            if quantity < 0 or tons < 0:
                raise SemanticRuntimeError("数据未通过完整性校验，暂未展示结果", reason="inbound_sku_invalid")
            sku_total += quantity
            sku_tons_total += tons
        if sku_total != total_ea or sku_tons_total != total_tons:
            raise SemanticRuntimeError("数据未通过完整性校验，暂未展示结果", reason="inbound_sku_total_mismatch")

        warehouse_names: dict[str, int] = {}
        warehouse_entries: list[tuple[str, str, Mapping[str, Any]]] = []
        for row in warehouse_rows:
            base_name = str(row.get("warehouse_name") or "").strip()
            code = str(row.get("wms_code") or "").strip()
            if not base_name:
                base_name = "未匹配仓库"
            warehouse_names[base_name] = warehouse_names.get(base_name, 0) + 1
            warehouse_entries.append((base_name, code, row))

        def display_warehouse(base_name: str, code: str) -> str:
            if (warehouse_names[base_name] > 1 or base_name == "未匹配仓库") and code:
                return f"{base_name} ({code})"
            return base_name

        ordered_warehouses = sorted(
            (
                (
                    display_warehouse(base_name, code),
                    row,
                )
                for base_name, code, row in warehouse_entries
            ),
            key=lambda item: _parse_decimal(item[1].get("inbound_ea"), default=Decimal(0)),
            reverse=True,
        )
        valid_skus: list[tuple[str, Decimal, Decimal]] = []
        top_skus: list[tuple[str, Decimal, Decimal]] = []
        lines = [
            f"{period_label}实际入库合计：{_format_number(total_ea, places=0)} EA；{_format_number(total_tons)} 吨。",
        ]
        if request.show_warehouses:
            lines.append("按仓汇总：")
            for offset in range(0, len(ordered_warehouses), 2):
                parts = []
                for name, row in ordered_warehouses[offset : offset + 2]:
                    quantity = _parse_decimal(row.get("inbound_ea"), default=Decimal(0))
                    tons = _parse_decimal(row.get("inbound_tons"), default=Decimal(0))
                    parts.append(f"{name} {_format_number(quantity, places=0)} EA / {_format_number(tons)} 吨")
                lines.append("；".join(parts))
        if request.show_top_sku:
            for row in sku_rows:
                name = str(row.get("sku_name") or "").strip()
                sku_code = str(row.get("sku_code") or "").strip()
                quantity = _parse_decimal(row.get("inbound_ea"), default=Decimal(0))
                tons = _parse_decimal(row.get("inbound_tons"), default=Decimal(0))
                if quantity < 0 or tons < 0:
                    raise SemanticRuntimeError("数据未通过完整性校验，暂未展示结果", reason="inbound_sku_invalid")
                display_name = name or (f"SKU {sku_code}" if sku_code else "未匹配 SKU")
                if name or sku_code:
                    valid_skus.append((display_name, quantity, tons))
            if not valid_skus:
                raise SemanticRuntimeError("数据未通过完整性校验，暂未展示结果", reason="inbound_sku_name_missing")
            top_limit = request.top_n if isinstance(request.top_n, int) and request.top_n > 0 else 5
            top_skus = sorted(
                valid_skus,
                key=lambda item: (-item[1], item[0]),
            )[:top_limit]
            top_text = "；".join(
                f"{name} {_format_number(quantity, places=0)} EA / {_format_number(tons)} 吨"
                for name, quantity, tons in top_skus
            )
            lines.append(f"入库量最高的前{top_limit}个 SKU（按{period_label}）：{top_text}。")
        if missing_master_ea > 0:
            lines.append(
                "说明：EA 数量已包含全部有效入库记录；部分 SKU 未匹配主数据，因此这部分未计入吨数。"
            )
        elif unweighted_ea > 0:
            lines.append("说明：部分入库数量没有有效净重，因此未计入吨数。")
        if request.show_warehouses:
            table = _display_table(
                table_id="inbound_by_warehouse",
                title="按仓实际入库",
                caption=f"{period_label} · 有效完成入库",
                columns=[
                    {"key": "warehouse", "kind": "text"},
                    {"key": "inbound_quantity", "kind": "number", "unit": "EA"},
                    {"key": "inbound_tons", "kind": "number", "unit": "吨"},
                ],
                rows=[
                    {
                        "warehouse": name,
                        "inbound_quantity": _format_number(
                            _parse_decimal(row.get("inbound_ea"), default=Decimal(0)),
                            places=0,
                        ),
                        "inbound_tons": _format_number(
                            _parse_decimal(row.get("inbound_tons"), default=Decimal(0))
                        ),
                    }
                    for name, row in ordered_warehouses
                ],
                summary=[
                    {"label": "合计", "value": _format_number(total_ea, places=0), "unit": "EA"},
                    {"label": "仓库数", "value": str(warehouse_count), "unit": "个"},
                ],
                empty_label="当前日期没有匹配的有效入库记录",
            )
        else:
            table = _display_table(
                table_id="inbound_top_sku",
                title="入库量最高 SKU",
                caption=f"{period_label} · 按入库数量排序",
                columns=[
                    {"key": "sku_name", "kind": "text"},
                    {"key": "inbound_quantity", "kind": "number", "unit": "EA"},
                    {"key": "inbound_tons", "kind": "number", "unit": "吨"},
                ],
                rows=[
                    {
                        "sku_name": name,
                        "inbound_quantity": _format_number(quantity, places=0),
                        "inbound_tons": _format_number(tons),
                    }
                    for name, quantity, tons in top_skus
                ],
                summary=[
                    {"label": "合计", "value": _format_number(total_ea, places=0), "unit": "EA"},
                ],
                empty_label="当前日期没有匹配的有效入库记录",
            )
        return SemanticAnswer("\n".join(lines), table)

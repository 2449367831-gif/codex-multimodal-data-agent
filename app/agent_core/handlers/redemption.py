"RedemptionHandler SQL and result reconciliation."

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
    _month_label,
    _parse_decimal,
    _parse_source_datetime,
)
from .base import MetricHandler
from ...outbound_contract import completed_status_sql, ref_order_type_sql


class RedemptionHandler(MetricHandler):
    def _dms_points_redemption_sql(
        self,
        start: date,
        end: date,
        product_code: str,
    ) -> str:
        """Build the reviewed DMS points-redemption aggregate.

        ``item_type=2`` is the product line for points orders. Type 3 is a
        settlement duplicate in this source and is intentionally excluded.
        The query returns one control row plus monthly aggregates so the
        application can reconcile the displayed table before exposing it.
        """

        start_text = self._sql_date(start)
        end_text = self._sql_date(end)
        escaped_product_code = product_code.replace("'", "''")
        monthly_grain = start.day == 1 and end.day == 1
        period_format = '%Y-%m' if monthly_grain else '%Y-%m-%d'
        return f"""WITH order_ranked AS (
  SELECT TRIM(o.sales_order_code) AS sales_order_code,
         o.paid_time,
         TRIM(CAST(o.order_type AS STRING)) AS order_type,
         TRIM(CAST(o.paid_status AS STRING)) AS paid_status,
         TRIM(CAST(o.order_status AS STRING)) AS order_status,
         TRIM(CAST(o.after_sales_status AS STRING)) AS after_sales_status,
         o.is_points_order,
         o.deleted_flag,
         o._fdl_marked_deleted,
         ROW_NUMBER() OVER (
           PARTITION BY TRIM(o.sales_order_code)
           ORDER BY o.updated_time DESC, o.version DESC, o.id DESC
         ) AS rn
  FROM dms_ods.t_sales_order AS o
  WHERE TRIM(o.sales_order_code) <> ''
), valid_orders AS (
  SELECT sales_order_code,
         DATE_FORMAT(paid_time, '{period_format}') AS month_label
  FROM order_ranked
  WHERE rn = 1
    AND paid_time >= '{start_text} 00:00:00'
    AND paid_time < '{end_text} 00:00:00'
    AND order_type = 'SO16'
    AND paid_status = '1'
    AND COALESCE(deleted_flag, 0) = 0
    AND LOWER(IFNULL(_fdl_marked_deleted, 'false')) NOT IN ('true', '1')
    AND COALESCE(order_status, '') NOT IN ('0', '100', '107', '108', '199')
    AND NOT (order_status = '101' AND COALESCE(after_sales_status, '') = '107')
    AND is_points_order = 1
), detail_ranked AS (
  SELECT TRIM(d.sales_order_code) AS sales_order_code,
         TRIM(d.item_code) AS item_code,
         TRIM(d.sku_code) AS sku_code,
         NULLIF(TRIM(d.sku_name), '') AS sku_name,
         TRIM(CAST(d.item_type AS STRING)) AS item_type,
         d.bag_quantity,
         d.deleted_flag,
         d._fdl_marked_deleted,
         ROW_NUMBER() OVER (
           PARTITION BY TRIM(d.sales_order_code), TRIM(d.item_code), TRIM(d.sku_code)
           ORDER BY d.updated_time DESC, d.version DESC, d.id DESC
         ) AS rn
  FROM dms_ods.t_sales_order_detail AS d
  WHERE TRIM(d.sales_order_code) <> ''
    AND COALESCE(TRIM(CAST(d.item_type AS STRING)), '') = '2'
    AND TRIM(d.sku_code) = '{escaped_product_code}'
), valid_details AS (
  SELECT sales_order_code,
         sku_code,
         sku_name,
         bag_quantity
  FROM detail_ranked
  WHERE rn = 1
    AND COALESCE(item_type, '') = '2'
    AND COALESCE(deleted_flag, 0) = 0
    AND LOWER(IFNULL(_fdl_marked_deleted, 'false')) NOT IN ('true', '1')
), detail_lines AS (
  SELECT o.month_label,
         o.sales_order_code,
         d.sku_code,
         d.sku_name,
         SUM(COALESCE(d.bag_quantity, 0)) AS redemption_quantity
  FROM valid_orders AS o
  JOIN valid_details AS d
    ON d.sales_order_code = o.sales_order_code
  GROUP BY o.month_label, o.sales_order_code, d.sku_code, d.sku_name
), monthly AS (
  SELECT month_label,
         sku_code,
         MAX(sku_name) AS sku_name,
         COUNT(DISTINCT sales_order_code) AS order_count,
         SUM(redemption_quantity) AS redemption_quantity
  FROM detail_lines
  GROUP BY month_label, sku_code
), source_freshness AS (
  SELECT
    (SELECT MAX(updated_time) FROM dms_ods.t_sales_order) AS header_max_updated_time,
    (SELECT MAX(updated_time) FROM dms_ods.t_sales_order_detail) AS detail_max_updated_time
), detail_summary AS (
  SELECT COALESCE(SUM(redemption_quantity), 0) AS total_quantity,
         COUNT(DISTINCT sales_order_code) AS total_order_count,
         COUNT(DISTINCT sku_code) AS product_count,
         COUNT(DISTINCT month_label) AS month_count,
         COALESCE(SUM(CASE WHEN redemption_quantity < 0 THEN 1 ELSE 0 END), 0) AS negative_line_count,
         COUNT(*) AS logical_line_count,
         COUNT(DISTINCT NULLIF(sku_name, '')) AS name_variant_count
  FROM detail_lines
), summary AS (
  SELECT ds.total_quantity,
         ds.total_order_count,
         ds.product_count,
         ds.month_count,
         ds.negative_line_count,
         ds.logical_line_count,
         ds.name_variant_count,
         sf.header_max_updated_time,
         sf.detail_max_updated_time,
          CASE
            WHEN sf.header_max_updated_time IS NOT NULL
             AND sf.detail_max_updated_time IS NOT NULL
            THEN 1 ELSE 0
          END AS freshness_ok
  FROM detail_summary AS ds
  CROSS JOIN source_freshness AS sf
)
SELECT 'summary' AS grain,
       '' AS month_label,
       '' AS sku_code,
       '' AS sku_name,
       total_quantity AS redemption_quantity,
       total_order_count AS order_count,
       product_count,
       month_count,
       negative_line_count,
       logical_line_count,
       name_variant_count,
       freshness_ok,
       header_max_updated_time,
       detail_max_updated_time
FROM summary
UNION ALL
SELECT 'month' AS grain,
       month_label,
       sku_code,
       sku_name,
       redemption_quantity,
       order_count,
       (SELECT product_count FROM summary),
       (SELECT month_count FROM summary),
       (SELECT negative_line_count FROM summary),
       (SELECT logical_line_count FROM summary),
       (SELECT name_variant_count FROM summary),
       (SELECT freshness_ok FROM summary),
       (SELECT header_max_updated_time FROM summary),
       (SELECT detail_max_updated_time FROM summary)
FROM monthly
ORDER BY CASE WHEN grain = 'summary' THEN 0 ELSE 1 END, month_label ASC"""

    def _run_dms_points_redemption(self, request: SemanticRequest) -> SemanticAnswer:
        product_code = request.product_code or "8030101"
        product_name = request.product_name or "皇家小虎空气炸锅5L"
        rows = self._execute(
            self._dms_points_redemption_sql(request.start, request.end, product_code),
            purpose="DMS points redemption air fryer by payment month",
        )
        summary_rows = [row for row in rows if row.get("grain") == "summary"]
        month_rows = [row for row in rows if row.get("grain") == "month"]
        if len(summary_rows) != 1 or len(summary_rows) + len(month_rows) != len(rows):
            raise SemanticRuntimeError(
                "数据未通过完整性校验，暂未展示结果",
                reason="dms_summary_shape_invalid",
            )
        summary = summary_rows[0]
        try:
            total_quantity = _parse_decimal(summary.get("redemption_quantity"), default=Decimal(0))
            total_order_count = int(_parse_decimal(summary.get("order_count"), default=Decimal(0)))
            product_count = int(_parse_decimal(summary.get("product_count"), default=Decimal(0)))
            month_count = int(_parse_decimal(summary.get("month_count"), default=Decimal(0)))
            negative_line_count = int(
                _parse_decimal(summary.get("negative_line_count"), default=Decimal(0))
            )
            logical_line_count = int(
                _parse_decimal(summary.get("logical_line_count"), default=Decimal(0))
            )
            name_variant_count = int(
                _parse_decimal(summary.get("name_variant_count"), default=Decimal(0))
            )
            freshness_ok = int(_parse_decimal(summary.get("freshness_ok"), default=Decimal(0)))
            header_max_updated_time = _parse_source_datetime(
                summary.get("header_max_updated_time"),
                timezone_name=self.timezone_name,
            )
            detail_max_updated_time = _parse_source_datetime(
                summary.get("detail_max_updated_time"),
                timezone_name=self.timezone_name,
            )
        except (ValueError, TypeError) as exc:
            raise SemanticRuntimeError(
                "数据未通过完整性校验，暂未展示结果",
                reason="dms_summary_values_invalid",
            ) from exc
        if (
            total_quantity < 0
            or total_order_count < 0
            or product_count < 0
            or month_count < 0
            or negative_line_count != 0
            or logical_line_count < 0
            or name_variant_count > 1
            or month_count != len(month_rows)
            or (month_count == 0 and any(value != 0 for value in (total_quantity, total_order_count, product_count, logical_line_count)))
            or (month_count > 0 and (product_count != 1 or name_variant_count != 1))
            or freshness_ok != 1
        ):
            raise SemanticRuntimeError(
                "数据未通过完整性校验，暂未展示结果",
                reason="dms_summary_quality_or_freshness_failed",
            )

        monthly_quantity = Decimal(0)
        monthly_orders = 0
        seen_months: set[str] = set()
        previous_month: str | None = None
        display_rows: list[dict[str, str]] = []
        for row in month_rows:
            month_label = str(row.get("month_label") or "").strip()
            sku_code = str(row.get("sku_code") or "").strip()
            sku_name = str(row.get("sku_name") or "").strip()
            try:
                quantity = _parse_decimal(row.get("redemption_quantity"), default=Decimal(0))
                order_count = int(_parse_decimal(row.get("order_count"), default=Decimal(0)))
                row_product_count = int(_parse_decimal(row.get("product_count"), default=Decimal(0)))
                row_month_count = int(_parse_decimal(row.get("month_count"), default=Decimal(0)))
                row_negative_count = int(_parse_decimal(row.get("negative_line_count"), default=Decimal(0)))
                row_line_count = int(_parse_decimal(row.get("logical_line_count"), default=Decimal(0)))
                row_name_variant_count = int(_parse_decimal(row.get("name_variant_count"), default=Decimal(0)))
                row_freshness_ok = int(_parse_decimal(row.get("freshness_ok"), default=Decimal(0)))
                row_header_max_updated_time = _parse_source_datetime(
                    row.get("header_max_updated_time"),
                    timezone_name=self.timezone_name,
                )
                row_detail_max_updated_time = _parse_source_datetime(
                    row.get("detail_max_updated_time"),
                    timezone_name=self.timezone_name,
                )
            except (ValueError, TypeError) as exc:
                raise SemanticRuntimeError(
                    "数据未通过完整性校验，暂未展示结果",
                    reason="dms_month_values_invalid",
                ) from exc
            try:
                month_date = (
                    date.fromisoformat(month_label)
                    if len(month_label) == 10
                    else date.fromisoformat(f"{month_label}-01")
                )
            except ValueError as exc:
                raise SemanticRuntimeError(
                    "数据未通过完整性校验，暂未展示结果",
                    reason="dms_month_label_invalid",
                ) from exc
            if (
                not month_label
                or month_label in seen_months
                or (previous_month is not None and month_label <= previous_month)
                or month_date < request.start
                or month_date >= request.end
                or sku_code != product_code
                or sku_name != product_name
                or quantity < 0
                or order_count < 0
                or row_product_count != product_count
                or row_month_count != month_count
                or row_negative_count != negative_line_count
                or row_line_count != logical_line_count
                or row_name_variant_count != name_variant_count
                or row_freshness_ok != freshness_ok
                or row_header_max_updated_time != header_max_updated_time
                or row_detail_max_updated_time != detail_max_updated_time
            ):
                raise SemanticRuntimeError(
                    "数据未通过完整性校验，暂未展示结果",
                    reason="dms_month_reconciliation_failed",
                )
            seen_months.add(month_label)
            previous_month = month_label
            monthly_quantity += quantity
            monthly_orders += order_count
            display_rows.append(
                {
                    "month": month_label if len(month_label) == 10 else _month_label(month_date.year, month_date.month),
                    "sku_name": sku_name,
                    "redemption_quantity": _format_number(quantity, places=0),
                    "order_count": str(order_count),
                }
            )
        if monthly_quantity != total_quantity or monthly_orders != total_order_count:
            raise SemanticRuntimeError(
                "数据未通过完整性校验，暂未展示结果",
                reason="dms_total_reconciliation_failed",
            )

        inclusive_end = request.end - timedelta(days=1)
        full_month = request.start.day == 1 and request.end.day == 1
        if full_month:
            if request.start.year == inclusive_end.year:
                period = f"{request.start.year}年{request.start.month}月至{inclusive_end.month}月"
            else:
                period = (
                    f"{request.start.year}年{request.start.month}月至"
                    f"{inclusive_end.year}年{inclusive_end.month}月"
                )
        elif request.start == inclusive_end:
            period = request.start.isoformat()
        else:
            period = f"{request.start.isoformat()}至{inclusive_end.isoformat()}"
        if total_quantity == 0:
            answer = f"{period} DMS 积分兑换空气炸锅：没有匹配的有效兑换记录。"
        else:
            answer = (
                f"{period} DMS 积分兑换空气炸锅：{_format_number(total_quantity, places=0)} 台，"
                f"共 {total_order_count} 单。按付款{'月份' if full_month else '日期'}统计，表格仅展示有兑换记录的区间。"
            )
        table = _display_table(
            table_id="dms_points_redemption",
            title="DMS 积分兑换",
            caption=f"{period} · 空气炸锅 · 按付款{'月份' if full_month else '日期'}（仅展示有记录区间）",
            columns=[
                {"key": "month", "kind": "text"},
                {"key": "sku_name", "kind": "text"},
                {"key": "redemption_quantity", "kind": "number", "unit": "台"},
                {"key": "order_count", "kind": "number", "unit": "单"},
            ],
            rows=display_rows,
            summary=[
                {"label": "兑换总量", "value": _format_number(total_quantity, places=0), "unit": "台"},
                {"label": "兑换订单数", "value": str(total_order_count), "unit": "单"},
                {"label": "有数据月份", "value": str(month_count), "unit": "个月"},
            ],
            empty_label="当前范围没有有效的积分兑换记录",
        )
        return SemanticAnswer(answer, table)

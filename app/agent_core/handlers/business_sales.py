"""Explicit sales forms using the approved community-platform replacement."""
from decimal import Decimal

from .base import MetricHandler
from ..semantic_types import SemanticAnswer, SemanticRuntimeError, _display_table, _format_number, _parse_decimal, _period_label
from ...sales_source import BASIS_LABEL, bind_business_sales


class BusinessSalesHandler(MetricHandler):
    def _sql(self, request):
        by_sku = request.show_top_sku
        group = "sku_code, sku_name" if by_sku else "wms_code, warehouse_name"
        key = "sku_code" if by_sku else "wms_code"
        name = "sku_name" if by_sku else "warehouse_name"
        # Ranking is a deliberate output selection, not a transport row cap.
        top = request.top_n or 20
        if not 1 <= top <= 100:
            raise ValueError("invalid top count")
        limit = f"WHERE ranking <= {top}" if by_sku else ""
        sql = f"""WITH grouped AS (
          SELECT {group}, SUM(quantity) AS quantity,
                 SUM(CASE WHEN source_kind='shipment' THEN quantity ELSE 0 END) AS shipment_quantity,
                 SUM(CASE WHEN source_kind='community_order' THEN quantity ELSE 0 END) AS community_quantity,
                 COUNT(DISTINCT CASE WHEN source_kind='shipment' THEN document_code END) AS outbound_documents,
                 COUNT(DISTINCT CASE WHEN source_kind='community_order' THEN document_code END) AS community_orders,
                 SUM(CASE WHEN quantity IS NULL OR quantity < 0 THEN 1 ELSE 0 END) AS invalid_quantities
          FROM sales_current GROUP BY {group}
        ), ranked AS (
          SELECT *, SUM(quantity) OVER () AS all_quantity,
                    SUM(invalid_quantities) OVER () AS all_invalid_quantities,
                    COUNT(*) OVER () AS group_count,
                    ROW_NUMBER() OVER (ORDER BY quantity DESC, {key}, {name}) AS ranking
          FROM grouped
        ) SELECT * FROM ranked {limit} ORDER BY ranking"""
        return bind_business_sales(sql, request.start, request.end, scope=request.scope)

    def run(self, request):
        rows = self.execution._query_checked(self._sql(request), purpose="approved hybrid business sales", business=True)
        label = _period_label(request.start, request.end, request.period_label)
        by_sku = request.show_top_sku
        key = "sku_code" if by_sku else "wms_code"
        name = "sku_name" if by_sku else "warehouse_name"
        table_rows = []
        total = Decimal(0)
        for row in rows:
            quantity = _parse_decimal(row.get("quantity"))
            actual = _parse_decimal(row.get("shipment_quantity"))
            community = _parse_decimal(row.get("community_quantity"))
            if quantity != actual + community or _parse_decimal(row.get("all_invalid_quantities")) != 0:
                raise SemanticRuntimeError("销售分项与总量不一致，未展示结果", reason="sales_reconciliation_failed")
            total += quantity
            table_rows.append({
                "code": str(row.get(key) or "未维护"), "name": str(row.get(name) or "未维护"),
                "quantity": _format_number(quantity, places=2),
                "shipment_quantity": _format_number(actual, places=2),
                "community_quantity": _format_number(community, places=2),
                "outbound_documents": _format_number(_parse_decimal(row.get("outbound_documents")), places=2),
                "community_orders": _format_number(_parse_decimal(row.get("community_orders")), places=2),
            })
        if rows:
            totals = {_parse_decimal(r.get("all_quantity")) for r in rows}
            counts = {_parse_decimal(r.get("group_count")) for r in rows}
            if len(totals) != 1 or len(counts) != 1 or (not by_sku and (total != next(iter(totals)) or len(rows) != next(iter(counts)))) or (by_sku and len(rows) != min(request.top_n or 20, next(iter(counts)))):
                raise SemanticRuntimeError("销售结果不完整，未展示结果", reason="sales_controls_mismatch")
        missing = any(not row.get(name) or not row.get(key) for row in rows)
        caption = f"{label} · EA · {BASIS_LABEL}。出库单与社区平台订单分别去重，不跨 SKU 累加单数。"
        if missing:
            caption += "存在未维护的商品或仓库名称，已保留编码。"
        table = _display_table(
            table_id="business_sales_top_sku" if by_sku else "business_sales_by_warehouse",
            title="销售数量 · SKU TOP" if by_sku else "销售数量 · 按仓库", caption=caption,
            columns=[{"key":"code", "label":"商品编码" if by_sku else "仓库编码", "kind":"text"},
                     {"key":"name", "label":"商品描述" if by_sku else "仓库描述", "kind":"text"},
                     {"key":"quantity", "label":"总发货量", "kind":"number", "unit":"EA"},
                     {"key":"shipment_quantity", "label":"其余渠道实发量", "kind":"number", "unit":"EA"},
                     {"key":"community_quantity", "label":"社区平台请求量", "kind":"number", "unit":"EA"},
                     {"key":"outbound_documents", "label":"出库单数", "kind":"number", "unit":"单"},
                     {"key":"community_orders", "label":"社区平台订单数", "kind":"number", "unit":"单"}],
            rows=table_rows, summary=[{"label":"本表数量合计", "value":_format_number(total, places=2), "unit":"EA"}],
            empty_label="查询成功，但当前条件下没有符合条件的数据",
        )
        text = "查询成功，但当前条件下没有符合条件的数据。" if not rows else f"{label}，{'前'+str(len(rows))+'个SKU' if by_sku else '按仓汇总'}销售数量：{_format_number(total, places=2)} EA。"
        return SemanticAnswer(text + "\n" + caption, table)

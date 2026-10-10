"""Present checked relationship fields without internal SQL or quality controls."""

from ..outbound_links import compile_outbound_links, reconcile_outbound_links
from ..semantic_types import SemanticAnswer, SemanticRuntimeError, _display_table
from ...query_scope import QueryScope
from .base import MetricHandler


PUBLIC_FIELDS = {
    "cargo_owner": "cargo_owner", "outbound_code": "outbound_code",
    "referenced_order_code": "referenced_order_code", "linked_sales_order_code": "sales_order_code",
    "source_order_code": "source_order_code", "detail_source_order_code": "detail_source_order_code",
    "source_platform_code": "source_platform_code", "referenced_order_type": "ref_order_type",
    "order_type": "order_type", "link_ship_at": "ship_at", "relationship_state": "relationship_state",
}


class OutboundLinksHandler(MetricHandler):
    def run(self, request):
        plan = request.outbound_link_plan
        if (plan is None or request.scope != QueryScope(owner=plan.owner, unit="none")
                or (request.start, request.end) != (plan.start, plan.end)):
            raise SemanticRuntimeError("出库关联查询范围不一致，未执行查询", reason="outbound_link_scope_invalid")
        sql = compile_outbound_links(plan)
        # The compiler checks the complete owner-qualified document observations.
        # Generic fact scoping must not filter away conflicting source tuples.
        first = self.execution._query_checked(sql, purpose="Outbound document relationships", business=True)
        checked = reconcile_outbound_links(first, plan)
        second = self.execution._query_checked(sql, purpose="Outbound relationship stability verification", business=True)
        repeated = reconcile_outbound_links(second, plan)
        if checked != repeated:
            raise SemanticRuntimeError("查询期间出库关联记录发生变化，暂未展示结果", reason="outbound_links_changed")
        rows = []
        for source in checked.rows:
            row = {public: source[private] for public, private in PUBLIC_FIELDS.items()}
            if row["link_ship_at"] is not None:
                row["link_ship_at"] = str(row["link_ship_at"]).replace("T", " ")
            row["relationship_state"] = "已匹配销售订单" if row["relationship_state"] == "matched" else "未匹配销售订单"
            rows.append(row)
        window = "全部时间" if plan.start is None else f"{plan.start} 00:00 至 {plan.end} 00:00（不含结束时点）"
        caption = f"{window} · {plan.owner if plan.owner != 'ALL' else '全部货主'}"
        if plan.latest:
            caption += " · 最新出库的一张单"
        table = _display_table(table_id="sales_outbound_link", title="出库单关联链路", caption=caption,
            columns=[{"key": key, "kind": "text"} for key in PUBLIC_FIELDS], rows=rows,
            summary=[{"label": "出库单数", "value": str(checked.document_count), "unit": "单"}],
            empty_label="查询成功，当前范围没有匹配的出库单")
        table["notes"] = [
            "出库单号、引用的销售订单和出库关联类型来自出库记录；已匹配销售订单、头部来源单号、来源平台及订单类型来自销售订单主记录。订单来源编码来自有效订单明细，多个来源分别保留。",
            "按货主和单号核对关联，未匹配时保留出库单；头部来源单号与订单来源编码不是同一字段，来源平台也不是单号。本表不计算销售数量或金额。",
            "采用当前未删除的关联记录，两次核对一致不等于原子快照。出库时间不是同步时间，尚不能据此证明数仓已完整同步。最新一张按出库时间排序，同时间按货主和单号稳定排序。",
        ]
        text = (f"已核对{checked.document_count}张出库单，{checked.unmatched_document_count}张未匹配销售订单；关联来源见表格。"
                if rows else "查询成功，当前范围没有匹配的出库单。")
        return SemanticAnswer(text, table)

"""Post-aggregation box conversion, preserving every original EA group."""

from decimal import Decimal

from ..box_conversion import box_control_sql, resolve_box_specs, convert_box_rows
from ..sales_aggregation import compile_sales_aggregation
from ..sales_pages import retrieve_sales_groups
from ..semantic_types import SemanticAnswer, SemanticRuntimeError
from .sales_aggregate import SalesAggregateHandler


class OutboundBoxesHandler(SalesAggregateHandler):
    row_limit = 100000

    def _read_rows(self, request):
        sql = compile_sales_aggregation(request.sales_aggregation,start=request.start,end=request.end)
        if request.route == "sales_outbound_boxes_channel":
            return self._execute(sql, purpose="actual channel sales quantity and box conversion")
        return retrieve_sales_groups(sql,self._execute)

    def run(self, request):
        answer = super().run(request)
        table = dict(answer.business_table)
        rows = table["rows"]
        if request.finished_goods_only:
            self._verify_finished_membership(rows, ())
        codes = tuple(sorted({row["sku_code"] for row in rows}))
        def specs_pass():
            controls = []
            for offset in range(0,len(codes),200):
                # The approved unit lookup is SKU-global, not silently owner-filtered.
                controls.extend(self.execution._query_checked(box_control_sql(codes[offset:offset+200]),purpose="box specification verification",business=False))
            return resolve_box_specs(codes,controls)
        try:
            specs = specs_pass()
            if specs != specs_pass():
                raise ValueError("box specifications changed")
            converted = convert_box_rows(rows,specs)
        except (ValueError,KeyError,TypeError,ArithmeticError) as exc:
            raise SemanticRuntimeError("箱规校验失败或查询期间发生变化，暂未展示结果",reason="box_spec_controls_invalid") from exc
        states = {"valid":"已换算", "missing":"缺少箱规", "invalid":"箱规为空或非正数", "conflict":"箱规冲突"}
        unavailable = sum(row["box_state"] != "valid" for row in converted)
        for row in converted:
            row["box_state"] = states[row["box_state"]]
        channel_route = request.route == "sales_outbound_boxes_channel"
        table.update(
            # Reuse the reviewed public schema; the route changes the input
            # filter, not the business table contract.
            table_id="sales_outbound_boxes",
            title="渠道实际出库与箱数" if channel_route else "成品实际出库与箱数",
            rows=converted,
            complete_reconciled=True,
            scope_finished_goods=request.finished_goods_only,
        )
        if channel_route:
            table["caption"] += " · " + request.sales_aggregation.channel_filter + "渠道"
        table["columns"] += [{"key":"box_spec","kind":"number"},{"key":"box_quantity","kind":"number","unit":"箱"},{"key":"box_state","kind":"text"}]
        missing = table.get("missing_customer_quantity","0")
        table["notes"] = [
            "按日期、货主、仓库、SKU及线上／线下／其他汇总后换算，箱数四舍五入不保留小数。原始EA数量保持不变。",
            f"采用当前箱规。{unavailable} 个分组因箱规缺失、无效或冲突无法换算，空值不是零箱；不提供跨箱规的总箱数。",
            f"客户分类缺失的 {missing} EA 已保留到其他。出库时间不是同步时间，数据同步鲜度尚未确认。",
        ]
        text = str(answer) + f"按线上、线下、其他分组后换算；{unavailable} 个分组的箱数为空，原因已逐行列出。客户分类缺失的 {missing} EA 已保留到其他。"
        return SemanticAnswer(text,table)

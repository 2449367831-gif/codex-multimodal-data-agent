"""Rank first, then fetch a selected SKU using the original checked plan."""

from ..sales_drilldown import DRILLDOWN_PAGE_SIZE, SalesDrilldownPlan, retrieve_sales_ranking, retrieve_ranked_sku
from ..semantic_types import SemanticAnswer, _display_table, _period_label
from .base import MetricHandler


class RankedSalesHandler(MetricHandler):
    def _compiled_query(self, sql, *, purpose):
        # The compiler already scoped the facts. Rescoping master observations
        # would hide conflicting names/categories for the selected fact keys.
        return self.execution._query_checked(sql, purpose=purpose, business=True)

    def run(self, request):
        plan = SalesDrilldownPlan(request.start, request.end, request.top_n, request.scope)
        snapshot = retrieve_sales_ranking(plan, self._compiled_query,
            query_many=self.execution._query_many_checked,
            page_size=DRILLDOWN_PAGE_SIZE, page_cap=DRILLDOWN_PAGE_SIZE)
        table = _display_table(table_id='sales_ranked_drilldown', title=f'成品销售出库 Top{plan.top_n}',
            caption=f'{_period_label(plan.start, plan.end)} · 北京时间 · 实际出库',
            columns=[{'key': key, 'kind': 'text' if key in {'sku_code', 'sku_name'} else 'number'}
                     for key in ('rank', 'sku_code', 'sku_name', 'quantity', 'outbound_count')],
            rows=[{'rank': str(row.rank), 'sku_code': row.sku_code, 'sku_name': row.sku_name,
                   'quantity': format(row.quantity, 'f'), 'outbound_count': str(row.outbound_count)}
                  for row in snapshot.ranking],
            summary=[{'label': '入榜实发数量', 'value': format(snapshot.selected_quantity, 'f'), 'unit': 'EA'},
                     {'label': '入榜出库单数', 'value': str(snapshot.selected_outbound_count), 'unit': '单'}])
        table['notes'] = [
            '成品实发数量降序，同量按SKU编码排序；出库单按货主及单号去重，同一单涉及多个SKU时不可累加各行单数。',
            '客户编码取出库单记录；明细沿用本次范围，并与原排名数量和单数对账。',
            f'本范围最近出库时间：{snapshot.latest_shipment_at or "暂无"}。出库时间不是同步时间；排名与后续明细不是同一原子快照。',
        ]
        answer = SemanticAnswer('排名汇总已对账。' if snapshot.ranking else '查询成功，当前范围没有匹配的成品销售出库。', table)
        answer.ranking_snapshot = snapshot
        return answer

    def detail(self, snapshot, sku_code):
        checked = retrieve_ranked_sku(snapshot, sku_code, self._compiled_query,
            query_many=self.execution._query_many_checked,
            page_size=DRILLDOWN_PAGE_SIZE, page_cap=DRILLDOWN_PAGE_SIZE)
        ranked = checked.ranking[0]
        fields = ('sku_code', 'cargo_owner', 'warehouse_code', 'warehouse_name', 'customer_code',
                  'outbound_code', 'ship_at', 'quantity')
        rows = []
        for row in checked.details:
            item = {key: getattr(row, key) for key in fields}
            item['ship_at'] = row.ship_at.isoformat(sep=' ')
            item['quantity'] = format(row.quantity, 'f')
            rows.append(item)
        table = _display_table(table_id='sales_ranked_sku_detail', title='成品销售出库明细',
            caption=f'{_period_label(snapshot.plan.start, snapshot.plan.end)} · 北京时间 · 原排名第{ranked.rank}名',
            columns=[{'key': key, 'kind': 'number' if key == 'quantity' else 'text'} for key in fields], rows=rows,
            summary=[{'label': '实发数量', 'value': format(ranked.quantity, 'f'), 'unit': 'EA'},
                     {'label': '出库单数', 'value': str(ranked.outbound_count), 'unit': '单'}])
        table['complete_reconciled'] = True
        table['notes'] = [
            '按货主、出库单和SKU汇总明细；客户编码取出库单，缺失时保留空值，不额外过滤客户。',
            '完整明细与原排名数量、出库单数对账差异均为0；两次一致读取不代表原子快照。',
        ]
        return SemanticAnswer('该SKU完整明细已与原排名对账。', table)

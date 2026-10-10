"""Present complete paged sales details separately from capacity-only summaries."""

from decimal import Decimal, Inexact, localcontext
from fractions import Fraction

from ..sales_detail_partitions import SalesDetailPartitionPlan, retrieve_partitioned_sales_details
from ..sales_detail_planning import actual_details_enabled
from ..sales_detail_batches import SalesDetailCursor, definition_signature, changed
from ..semantic_types import SemanticAnswer, SemanticRuntimeError, _display_table, _parse_source_datetime
from .base import MetricHandler


def batch_table(batch, *, timezone_name='Asia/Shanghai'):
    cursor, source = batch.cursor, batch.source
    rows = []
    for raw in batch.rows:
        row = dict(raw)
        row['quantity'] = format(Decimal(str(raw['quantity'])), 'f')
        row['ship_at'] = _parse_source_datetime(raw['ship_at'], timezone_name=timezone_name).isoformat(sep=' ')
        rows.append(row)
    quantity = sum((Fraction(str(raw['quantity'])) for raw in batch.rows), Fraction(0))
    # Each source quantity is a terminating decimal; avoid rounding the total.
    with localcontext() as context:
        context.prec = len(str(quantity.numerator)) + len(str(quantity.denominator)) + 2
        context.traps[Inexact] = True
        quantity_text = format(Decimal(quantity.numerator) / Decimal(quantity.denominator), 'f')
    table = _display_table(table_id='sales_outbound_details', title=f'实际销售出库明细 · 第{source.batch_index}批',
        caption=f'{source.plan.start.isoformat()} 00:00 至 {source.plan.end.isoformat()} 00:00（不含结束时点） · {source.plan.scope.label()}',
        columns=[{'key': key, 'kind': 'number' if key == 'quantity' else 'text'} for key in
                 ('cargo_owner', 'warehouse_code', 'outbound_code', 'sku_code', 'ship_at', 'quantity')],
        rows=rows, summary=[{'label': '实发数量', 'value': quantity_text, 'unit': 'EA'},
                           {'label': '出库单数', 'value': str(len({(r['cargo_owner'], r['outbound_code']) for r in rows})), 'unit': '单'}])
    table['notes'] = [f'本批为原范围第{source.loaded + 1}至{cursor.loaded}行，共{cursor.baseline.detail_count}行。',
        '数量和单数为本批数据；同一出库单跨批次时不能累加单数。',
        '全部批次已与原范围累计数量和单数对账。' if cursor.complete else '本批已对账，原范围明细尚未全部取回。']
    return table


class SalesDetailsHandler(MetricHandler):
    def run(self, request):
        if not actual_details_enabled():
            raise SemanticRuntimeError('实际销售出库口径尚未启用，未执行查询', reason='business_definition_unpublished')
        plan = SalesDetailPartitionPlan(request.start, request.end, request.scope)
        signature = definition_signature(plan)
        checked = retrieve_partitioned_sales_details(plan,
            lambda sql, **kwargs: self.execution._query_checked(sql, business=True, **kwargs),
            query_many=self.execution._query_many_checked)
        summary = [
            {'label': '实发数量', 'value': format(checked.summary.quantity, 'f'), 'unit': 'EA'},
            {'label': '出库单数', 'value': str(checked.summary.document_count), 'unit': '单'},
        ]
        caption = f'{plan.start.isoformat()} 00:00 至 {plan.end.isoformat()} 00:00（不含结束时点）'
        if checked.complete:
            fields = ('cargo_owner', 'warehouse_code', 'outbound_code', 'sku_code', 'ship_at', 'quantity')
            rows = []
            for raw in checked.rows:
                row = dict(raw)
                row['quantity'] = format(Decimal(str(raw['quantity'])), 'f')
                row['ship_at'] = _parse_source_datetime(raw['ship_at'], timezone_name=self.timezone_name).isoformat(sep=' ')
                rows.append(row)
            table = _display_table(table_id='sales_outbound_details', title='实际销售出库明细', caption=caption,
                columns=[{'key': key, 'kind': 'number' if key == 'quantity' else 'text'} for key in fields],
                rows=rows, summary=summary, empty_label='查询成功，当前范围没有有效销售出库明细')
            table['complete_reconciled'] = True
            text = '完整明细已与整个查询范围的数量和单数对账。' if rows else '查询成功，当前范围没有有效销售出库明细。'
        else:
            table = _display_table(table_id='sales_aggregate', title='实际销售出库范围汇总', caption=caption,
                columns=[{'key': key, 'kind': 'number'} for key in ('quantity', 'outbound_count')],
                rows=[{'quantity': format(checked.summary.quantity, 'f'), 'outbound_count': str(checked.summary.document_count)}],
                summary=summary)
            text = f'完整范围汇总已取得；明细共{checked.summary.detail_count}行，超过本次处理容量，尚未完整取回明细。'
        table['notes'] = [
            '明细按货主、出库单和SKU汇总，不是原始物理明细行；同一出库单包含多个SKU时不能累加单数。',
            '明细已完整对账。' if checked.complete else '当前仅显示完整范围汇总，未返回部分明细；需要按月份、仓库或SKU分片继续查询。',
            f'最近业务出库时间：{checked.summary.latest_shipment_at or "暂无"}。该时间不是数仓同步时间。',
        ]
        answer = SemanticAnswer(text, table)
        answer.sales_detail_result = checked
        if signature != definition_signature(plan):
            raise changed()
        answer.sales_detail_cursor = None if checked.complete else SalesDetailCursor(plan, checked.summary, signature)
        return answer

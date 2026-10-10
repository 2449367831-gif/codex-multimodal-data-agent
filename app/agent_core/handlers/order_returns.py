"""Candidate order/return quantity tables; money is deliberately not projected."""

from pathlib import Path

import yaml

from ...knowledge_activation import active_snapshot
from ..order_components import OrderComponentPlan
from ..order_return_composition import retrieve_order_returns
from ..semantic_types import SemanticAnswer, SemanticRequest, SemanticRuntimeError, _display_table
from .base import MetricHandler


ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ('sales_order.components', 'sales_order.return_components', 'sales_order.net_returns')
ISSUES = {
    'missing_sales_sku': '缺少销售明细，净数量未计算',
    'negative_sales_source': '销售实发存在负数记录',
    'negative_return_source': '退货存在负数记录',
    'returns_exceed_shipment': '退货数量超过实发数量',
}
ORDER_FIELDS = ('cargo_owner', 'sales_order_code', 'order_created', 'order_detail_state')
SKU_FIELDS = ('cargo_owner', 'sales_order_code', 'sku_code', 'order_ship_quantity',
              'returned_quantity', 'net_sales_quantity', 'return_document_count',
              'sales_detail_count', 'return_detail_count', 'quantity_state')


def return_quantity_contract_active(root):
    for metric in CONTRACTS:
        path = Path(root) / 'metrics' / (metric + '.yaml')
        if not path.is_file():
            return False
        try:
            contract = yaml.safe_load(path.read_text(encoding='utf-8'))
        except (OSError, yaml.YAMLError):
            return False
        if not isinstance(contract, dict):
            return False
        approval = contract.get('approval')
        if (contract.get('id') != metric or contract.get('version') != '1.0.0'
                or contract.get('status') != 'active' or not isinstance(approval, dict)
                or approval.get('state') != 'approved'):
            return False
        if metric == 'sales_order.net_returns' and contract.get('dependency_versions') != {
                'sales_order.components': '1.0.0', 'sales_order.return_components': '1.0.0'}:
            return False
    return True


class OrderReturnQuantityHandler(MetricHandler):
    def run(self, request):
        plan = request.order_return_plan
        if not isinstance(plan, OrderComponentPlan):
            raise SemanticRuntimeError('订单退货查询范围不一致，未执行查询', reason='order_return_scope_invalid')
        scope = plan.execution_scope()
        if request != SemanticRequest('sales_order_net_quantity', plan.start, plan.end,
                                      scope=scope, order_return_plan=plan):
            raise SemanticRuntimeError('订单退货查询范围不一致，未执行查询', reason='order_return_scope_invalid')
        if not return_quantity_contract_active(active_snapshot(ROOT)[0]):
            raise SemanticRuntimeError('订单退货数量口径尚未审批发布，未执行查询', reason='order_return_not_published')
        def query(sql, **kwargs):
            return self.execution._query_checked(sql, business=True, **kwargs)
        checked = retrieve_order_returns(plan, query, amount_mode='pending',
                                         query_many=self.execution._query_many_checked)
        orders = [dict(cargo_owner=row['cargo_owner'], sales_order_code=row['sales_order_code'],
                       order_created=row['order_created'].replace('T', ' '),
                       order_detail_state='缺少范围内有效销售明细' if row['missing_details'] else '存在范围内有效销售明细')
                  for row in checked.orders]
        rows = []
        for source in checked.groups:
            unknown = set(source['issues']) - set(ISSUES) - {'amount_choice_pending'}
            if unknown:
                raise SemanticRuntimeError('订单退货数量状态无法确认，暂未展示', reason='order_return_presentation_invalid')
            row = {name: source[name] for name in ('cargo_owner', 'sales_order_code', 'sku_code')}
            for public, private in (('order_ship_quantity', 'order_ship_qty'), ('returned_quantity', 'returned_qty'),
                                    ('net_sales_quantity', 'net_qty'), ('return_document_count', 'return_document_count'),
                                    ('sales_detail_count', 'sales_detail_count'), ('return_detail_count', 'return_detail_count')):
                row[public] = None if source[private] is None else str(int(source[private]))
            row['quantity_state'] = '；'.join(ISSUES[issue] for issue in source['issues'] if issue in ISSUES) or '数量对账通过'
            rows.append(row)
        window = ('全部订单创建时间' if plan.start is None else
                  f"订单创建：{plan.start.strftime('%Y-%m-%d %H:%M:%S')} 至 {plan.end.strftime('%Y-%m-%d %H:%M:%S')}（不含结束时点）")
        owner_label = '全部货主'
        if plan.owner_codes:
            names = '、'.join(plan.owner_codes)
            owner_label = '货主 ' + names if len(names) <= 100 else f'指定{len(plan.owner_codes)}个货主'
        caption = window + ' · 北京时间 · ' + owner_label
        if plan.sku_codes:
            caption += ' · 指定商品范围'
        summary = [{'label': '订单数', 'value': str(len(orders)), 'unit': '单'}]
        order_table = _display_table(table_id='sales_order_return_orders', title='退货分析订单范围', caption=caption,
            columns=[{'key': key, 'kind': 'text'} for key in ORDER_FIELDS], rows=orders, summary=summary,
            empty_label='查询成功，当前范围没有匹配的销售订单')
        sku_table = _display_table(table_id='sales_order_return_quantities', title='订单退货数量核对', caption=caption,
            columns=[{'key': key, 'kind': 'number' if key in SKU_FIELDS[3:9] else 'text'} for key in SKU_FIELDS], rows=rows,
            empty_label='当前订单范围没有可展示的商品数量')
        order_table['notes'] = ['保留范围内订单；没有有效销售明细不等于没有订单。订单数量按货主和订单号去重。']
        sku_table['notes'] = [
            '订单实发与相关有效退货分别汇总后计算净数量；不是仓库实际销售出库口径。各表状态独立筛选，退货不套用订单创建日期窗口。',
            '数量保留来源计量值，未进行件、箱或重量换算；不跨商品合计。各商品关联退货单数不可直接相加作为全局退货单数。',
            '缺少销售数量时净数量留空；超退及负数保留提示，引用不一致时整次查询停止。本表不含金额。重复读取一致不代表原子快照或完整同步。',
        ]
        for table in (order_table, sku_table):
            table['complete_reconciled'] = True
        answer = f'已核对{len(orders)}个订单，商品数量和异常状态见表格。' if orders else '查询成功，当前范围没有匹配的销售订单。'
        return SemanticAnswer(answer, order_table, business_tables=[order_table, sku_table])

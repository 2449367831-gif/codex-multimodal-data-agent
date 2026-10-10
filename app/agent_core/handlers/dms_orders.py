"""Order-header counts and details share one reconciled result, not a join."""

from ..dms_points_orders import retrieve_dms_points_orders
from ..dms_manager_orders import retrieve_manager_groups
from ..semantic_types import SemanticAnswer, _display_table, _period_label
from .base import MetricHandler


class DmsManagerOrdersHandler(MetricHandler):
    def run(self, request):
        plan = request.dms_manager_plan
        if plan is None:
            raise ValueError('DMS manager order plan required')
        result = retrieve_manager_groups(plan, self._execute, query_many=self.execution.query_many,
                                         page_size=2000, page_cap=2000)
        period = _period_label(plan.start, plan.end) if plan.start else '全部创建日期'
        states = {
            'matched':'已匹配', 'missing_reference':'未配置经理', 'invalid_reference':'经理标识无效',
            'not_found':'未找到经理', 'deleted_employee':'经理记录已删除',
            'missing_name':'经理姓名缺失', 'conflicting_employee':'经理资料冲突',
        }
        keys = ('order_date','province','manager_key','manager_name','manager_state','customer_code','customer_name',
                'order_count','missing_manager_order_count')
        rows = []
        for group in result.groups:
            row = {key:group[key] for key in keys}
            row['manager_state'] = states[group['manager_state']]
            for key in ('order_count','missing_manager_order_count'):
                row[key] = str(group[key])
            rows.append(row)
        table = _display_table(table_id='dms_manager_orders', title='DMS城市经理订单统计',
            caption=f'{period} · 按订单创建时间 · 北京时间',
            columns=[{'key':key,'kind':'number' if key.endswith('order_count') else 'text'} for key in keys],
            rows=rows, summary=[{'label':'订单数','value':str(result.order_count),'unit':'单'},
                               {'label':'城市经理缺失订单数','value':str(result.missing_manager_order_count),'unit':'单'}])
        table['complete_reconciled'] = True
        table['notes'] = [
            '订单号去重后按创建日期、省区、经理标识及客户编码和名称分组；未添加积分、付款或出库条件。省区和客户名称取订单记录，经理姓名取当前员工资料，不代表历史姓名或省市组织匹配。',
            f'城市经理缺失或异常 {result.missing_manager_order_count} 单，省区缺失 {result.missing_province_order_count} 单，客户编码缺失 {result.missing_customer_code_count} 单，客户名称缺失 {result.missing_customer_name_count} 单；这些订单仍计入总数，经理状态已逐组列出。',
            f'本范围最近修改：{result.source_latest_updated or "暂无"}；修改时间缺失 {result.missing_updated_count} 单。修改时间不是同步时间，两次一致读取不代表原子快照。',
        ]
        text = f'{period}共有 {result.order_count} 单，城市经理缺失或异常 {result.missing_manager_order_count} 单，分组与订单总数已对账。' if result.order_count else '查询成功，当前范围没有匹配的DMS销售订单。'
        return SemanticAnswer(text,table)


class DmsPointsOrdersHandler(MetricHandler):
    def run(self, request):
        plan = request.dms_order_plan
        if plan is None:
            raise ValueError('DMS points order plan required')
        result = retrieve_dms_points_orders(plan, self._execute, query_many=self.execution.query_many)
        period = _period_label(plan.start, plan.end) if plan.start else '全部创建日期'
        caption = f'{period} · 按订单创建时间 · 北京时间'
        notes = [
            '积分订单按订单号去重，排除逻辑删除；未附加付款、出库完成或商品条件。',
            f'客户编码缺失 {result.missing_customer_code_count} 单，客户名称缺失 {result.missing_customer_name_count} 单；缺失订单仍计入总数。',
            f'本范围最近修改：{result.source_latest_updated or "暂无"}；修改时间缺失 {result.missing_updated_count} 单。修改时间不代表同步时间，历史同步完整性尚未确认。',
        ]
        monthly = _display_table(table_id='dms_points_orders_monthly', title='积分订单月度统计', caption=caption,
            columns=[{'key':'month','kind':'text'},{'key':'order_count','kind':'number'}],
            rows=[{'month':r['month'],'order_count':str(r['order_count'])} for r in result.months],
            summary=[{'label':'兑换订单数','value':str(result.order_count),'unit':'单'}])
        details = _display_table(table_id='dms_points_orders_detail', title='积分订单明细', caption=caption,
            columns=[{'key':key,'kind':'text'} for key in ('sales_order_code','customer_code','customer_name','created_time')],
            rows=[{key:row[key] for key in ('sales_order_code','customer_code','customer_name','created_time')} for row in result.orders])
        for table in (monthly, details):
            table['complete_reconciled'] = True
            table['notes'] = notes if table is monthly else ['订单粒度；客户名称取订单记录，客户编码保留前导零。月度汇总已与完整订单明细逐月对账。']
        text = f'{period}积分订单共 {result.order_count} 单，月度统计与订单明细已对账。' if result.order_count else '查询成功，当前范围没有匹配的积分订单。'
        return SemanticAnswer(text, monthly, business_tables=[monthly, details])

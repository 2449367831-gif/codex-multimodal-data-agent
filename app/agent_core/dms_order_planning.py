"""Conservative, request-local routing for creation-month points orders."""

import re

import yaml

from .dms_points_orders import DmsPointsOrderPlan
from .dms_manager_orders import DmsManagerOrderPlan
from .semantic_types import SemanticRequest, _compact


def manager_order_request(question, *, interval_parser, contract_root):
    text = _compact(question)
    if not all(token in text for token in ('dms','销售订单','城市经理','订单数')):
        return None
    annotations = (
        ('城市经理通过','t_sales_order.owner_manager=t_employee.employee_id','关联'),
        ('展示','t_employee.actual_name',''),
        ('客户名称直接取','t_sales_order.customer_name',''),
    )
    for prefix, field, suffix in annotations:
        token = re.escape(field)
        text = re.sub(re.escape(prefix)+f'(?:{token}|`{token}`)'+re.escape(suffix), '', text)
    text = re.sub(r'客户编码|客户名称|城市经理|销售订单|订单数量|订单数|省区|创建日期|创建时间|日期|dms(?:系统)?|'
                  r'查询|统计|返回|查看|列出|展示|显示|所有|全部|按|的|和|及|并|请|表格', '', text)
    date_text = re.sub(r'[，,。;；、：:（）()]', '', text)
    interval = None
    if date_text:
        grammar = r'(?:20\d{2}年)?(?:1[0-2]|0?[1-9])月?(?:至|到|-|~)(?:1[0-2]|0?[1-9])月|(?:20\d{2}年)?(?:1[0-2]|0?[1-9])月|上月|上个月|上个自然月|昨天|昨日|今天|今日|前天|最近\d{1,3}个完整自然日|最近\d{1,3}天|20\d{2}-\d{2}-\d{2}(?:(?:至|到)20\d{2}-\d{2}-\d{2})?'
        if re.fullmatch(grammar, date_text) is None:
            return None
        try:
            interval = interval_parser(date_text)
        except ValueError:
            return None
        if interval is None:
            return None
    for metric in ('dms.manager_orders','dms.city_manager'):
        path = contract_root / f'metrics/{metric}.yaml'
        definition = yaml.safe_load(path.read_text(encoding='utf-8')) if path.exists() else {}
        if definition.get('status') != 'active' or definition.get('approval', {}).get('state') != 'approved':
            return None
    start, end = interval or (None, None)
    return SemanticRequest(route='dms_manager_orders',start=start,end=end,
                           dms_manager_plan=DmsManagerOrderPlan(start=start,end=end))


def points_order_request(question, *, interval_parser, contract_root):
    text = _compact(question)
    if 'dms' not in text or not re.search(r'积分(?:兑换)?订单', text):
        return None
    if not re.search(r'订单数|订单明细|订单号|客户编码|客户名称', text):
        return None
    # Consume only reviewed presentation clauses and exclusions. Unknown
    # predicates must not disappear into an otherwise valid aggregate route.
    text = re.sub(r'(?:不要擅自增加|不要增加|不增加|不要求)wms出库完成条件', '', text)
    text = re.sub(r'(?:要求)?排除逻辑删除数据', '', text)
    text = re.sub(r'按(?:订单)?创建(?:时间|日期|月份)|按月份|按月|按创建月', '', text)
    text = re.sub(r'dms(?:系统)?|积分(?:兑换)?订单(?:数量|数|明细)?|订单数量|订单数|客户名称|客户编码|订单号|创建时间|创建日期', '', text)
    text = re.sub(r'查询|统计|返回|查看|列出|请|的|并且|并|以及|和|及|明细|汇总|用表格|以表格形式|表格|展示|显示|全部|所有', '', text)
    date_text = re.sub(r'[，,。;；、：:（）()]', '', text)
    interval = interval_parser(date_text) if date_text else None
    if date_text:
        date_grammar = r'(?:20\d{2}年)?(?:1[0-2]|0?[1-9])月?(?:至|到|-|~)(?:1[0-2]|0?[1-9])月|(?:20\d{2}年)?(?:1[0-2]|0?[1-9])月|上月|上个月|上个自然月|昨天|昨日|今天|今日|前天|最近\d{1,3}个完整自然日|最近\d{1,3}天|20\d{2}-\d{2}-\d{2}(?:(?:至|到)20\d{2}-\d{2}-\d{2})?'
        if interval is None or re.fullmatch(date_grammar, date_text) is None:
            return None
    path = contract_root / 'metrics/dms.points_orders.yaml'
    definition = yaml.safe_load(path.read_text(encoding='utf-8')) if path.exists() else {}
    if definition.get('status') != 'active' or definition.get('approval', {}).get('state') != 'approved':
        return None
    start, end = interval or (None, None)
    return SemanticRequest(route='dms_points_orders',start=start,end=end,
                           dms_order_plan=DmsPointsOrderPlan(start=start,end=end))

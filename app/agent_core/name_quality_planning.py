"""Request-local name diagnostics and one-shot outbound-basis selection."""

from dataclasses import replace
from pathlib import Path
import re

import yaml

from ..sku_references import extract_sku_references
from .semantic_types import SemanticRequest
from .sales_aggregation import SalesAggregation


def name_quality_scope(question, resolver):
    codes, text = extract_sku_references(question)
    if not ('出库' in text and '名称' in text and re.search(r'不一致|冲突|来源|为空|哪个名称|名称检查',text)):
        return None
    if re.search(r'分配|请求量|请求数量|未完成|取消|退货|调拨|调整|历史名称|快照名称|入库|库存|在途|吨|箱|重量|按(?:仓|日期|天|月)|各仓|每日|状态\s*(?:为|是|=|:)?\s*\d',text):
        return None
    phrases = ('订单明细名称','出库明细名称','主数据名称','商品主数据名称','名称来源','名称冲突','名称检查',
        '应该显示哪个名称','显示哪个名称','并说明处理原则','说明处理原则','处理原则','名称不一致','名称为空',
        '名称','不一致','冲突','为空','检查','如果','查询','返回','说明','的','数据','和','并','、','，','。')
    scope_text = re.sub('|'.join(re.escape(p) for p in sorted(phrases,key=len,reverse=True)),'',text)
    scope, _, unsupported = resolver.resolve(scope_text)
    if unsupported:
        return None
    return replace(scope,sku_codes=codes), text


def active_name_contract(root: Path):
    path=root/'metrics/sales.sku_name_quality.yaml'
    if not path.exists():
        return False
    contract=yaml.safe_load(path.read_text(encoding='utf-8'))
    return contract.get('status')=='active' and contract.get('approval',{}).get('state')=='approved'


def name_quality_request(question, *, interval, resolver, contract_root, selected=False):
    parsed=name_quality_scope(question,resolver)
    if parsed is None or not active_name_contract(contract_root):
        return None
    scope,text=parsed
    if not selected and not re.search(r'实际.*出库|销售出库|有效.*出库|完成.*出库',text):
        return None
    if interval is None and re.search(r'昨天|今天|昨日|今日|本月|上月|最近|近\d|20\d{2}|\d+月',text):
        return None
    return SemanticRequest('sales_sku_name_quality',*(interval or (None,None)),scope=scope,
                           sales_aggregation=SalesAggregation(('cargo_owner','sku')))


def name_quality_clarification(question, *, resolver, contract_root):
    parsed=name_quality_scope(question,resolver)
    if parsed is None or not active_name_contract(contract_root):
        return None
    if re.search(r'实际.*出库|销售出库|有效.*出库|完成.*出库',parsed[1]):
        return None
    return {'outcome':'clarification_required','questions':[{
        'id':'sku_name_outbound_basis','prompt':'名称检查对应哪种出库口径？','options':[
            {'id':'sku_name_actual','label':'实际销售出库，核对名称'},
            {'id':'sku_name_defer','label':'其他出库口径，暂不执行'}],
        'selection':'single'}]}

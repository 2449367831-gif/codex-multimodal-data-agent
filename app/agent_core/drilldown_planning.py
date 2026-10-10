"""Explicit ranked-detail requests; display fields never become scope filters."""

import re
from dataclasses import replace

from ..sku_references import extract_sku_references
from .catalog import approved_metric_contracts
from .semantic_types import SemanticRequest, _compact, _requested_top_n


def ranked_sales_request(question, *, interval_parser, resolver, contract_root):
    sku_codes, semantic_question = extract_sku_references(question)
    text = _compact(semantic_question)
    if not all(word in text for word in ('成品', '销售出库')):
        return None
    # Capture the ranking bound before consuming display/drilldown clauses.
    # Those clauses can contain the same words used by the ranking grammar and
    # must never erase the user's requested Top-N value.
    sizes = re.findall(r'(?:top|前)(\d+)', text)
    top_n = _requested_top_n(text)
    # Consume whole reviewed display clauses, leaving all other predicates intact.
    text = re.sub(r'用户点击其中一个sku后[，,]?要能回钻到仓库[、,，]货主[、,，]客户[、,，]出库单号和出库时间明细', '', text)
    text = re.sub(r'要求top\d{1,3}汇总与明细回钻完全对账|支持(?:点击sku)?回钻(?:明细)?', '', text)
    text = re.sub(r'(?:返回|输出)(?:排名|sku编码|sku名称|实发数量|出库单数|[、,，]|和)+', '', text)
    if re.search(r'jyck|hhck|bfck|qtck|so\d|2c|2b|例外', text):
        return None
    if not sizes or len(set(sizes)) != 1 or not 1 <= int(sizes[0]) <= 100:
        return None
    interval = interval_parser(text)
    if top_n is None or interval is None:
        return None
    scope, _, unsupported = resolver.resolve(text)
    if unsupported or not scope.finished_goods or scope.channel != 'ALL' or scope.unit != 'EA':
        return None
    scope = replace(scope, sku_codes=sku_codes)
    contracts = approved_metric_contracts(contract_root)
    for metric, version in (('sales.ranked_drilldown', '1.0.0'), ('sales.outbound_actual', '2.0.0')):
        contract = contracts.get(metric)
        if contract is None or contract.version != version:
            return None
    return SemanticRequest(route='sales_ranked_drilldown', start=interval[0], end=interval[1],
                           top_n=top_n, show_top_sku=True, finished_goods_only=True, scope=scope)

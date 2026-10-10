"""SO07 request quantities never use shipment facts or infer physical units."""

from pathlib import Path

import yaml

from ...knowledge_activation import active_snapshot
from ..sales_order_details import SalesOrderDetailPlan, retrieve_sales_order_answer
from ..semantic_types import SemanticRequest, SemanticRuntimeError
from .base import MetricHandler


ROOT = Path(__file__).resolve().parents[3]


def so07_contract_active(root):
    try:
        contract = yaml.safe_load((Path(root) / 'metrics/sales_order.so07_detail.yaml').read_text(encoding='utf8'))
    except (TypeError, OSError, yaml.YAMLError):
        return False
    return (isinstance(contract, dict) and contract.get('id') == 'sales_order.so07_detail'
            and contract.get('version') == '1.0.0' and contract.get('status') == 'active'
            and isinstance(contract.get('approval'), dict)
            and contract['approval'].get('state') == 'approved')


class So07OrdersHandler(MetricHandler):
    def run(self, request):
        plan = request.so07_plan
        if (not isinstance(plan, SalesOrderDetailPlan) or request != SemanticRequest(
                'so07_order_details', plan.start, plan.end, scope=plan.execution_scope(), so07_plan=plan)):
            raise SemanticRuntimeError('社团订单查询范围不一致，未执行查询', reason='so07_scope_invalid')
        if not so07_contract_active(active_snapshot(ROOT)[0]):
            raise SemanticRuntimeError('社团订单口径尚未审批发布，未执行查询', reason='so07_not_published')
        def query(sql, **kwargs):
            return self.execution._query_checked(sql, business=True, **kwargs)
        return retrieve_sales_order_answer(plan, query, query_many=self.execution._query_many_checked)

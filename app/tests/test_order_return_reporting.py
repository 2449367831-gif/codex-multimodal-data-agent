from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.agent_core.order_components import OrderComponentPlan
from app.agent_core.handlers.order_returns import return_quantity_contract_active as actual_active_gate
from app.agent_core.runtime import SemanticQueryRuntime
from app.agent_core.semantic_types import SemanticRequest, SemanticRuntimeError
from app.chat_api import _public_business_table, ChatExecutionError
from app.result_pages import ResultPageStore, ResultPageError
from app.query_scope import QueryScope
from app.tests.test_order_components import facts as order_facts, header, detail, read
from app.tests.test_return_components import facts as return_facts, header as return_header, detail as return_detail


@pytest.fixture(autouse=True)
def published(monkeypatch):
    monkeypatch.setattr('app.agent_core.handlers.order_returns.return_quantity_contract_active', lambda _: True)


def request(plan=None):
    plan = plan or OrderComponentPlan()
    return SemanticRequest('sales_order_net_quantity', plan.start, plan.end,
        scope=QueryScope(owner=plan.owner_codes[0] if len(plan.owner_codes) == 1 else 'ALL',
                         sku_codes=plan.sku_codes, unit='source_quantity'), order_return_plan=plan)


class Query:
    def __init__(self, orders, returns):
        self.orders, self.returns = orders, returns
        self.calls = []
        self.truncated = False

    def query(self, *, sql, purpose):
        self.calls.append(sql)
        rows = read(self.orders if 'scm_sales_order_' in sql else self.returns, sql)
        return {'status': 'succeeded', 'execution': {'executor_name': 'doris', 'is_mock': False,
                'rows': rows, 'row_count': len(rows), 'truncated': self.truncated}}


def run(orders, returns, plan=None):
    client = Query(orders, returns)
    value = SemanticQueryRuntime(query_client=client).execute_plan(request(plan), question='订单退货数量')
    return value, client


def test_checked_quantity_tables_keep_orders_separate_and_do_not_publish_money(order_facts, return_facts):
    header(order_facts)
    detail(order_facts, quantity=6)
    detail(order_facts, identity='D2', quantity=4)
    return_header(return_facts)
    return_header(return_facts, code='R2')
    return_detail(return_facts, quantity=2)
    return_detail(return_facts, identity='R2', code='R2', quantity=1)
    value, client = run(order_facts, return_facts)
    assert value['result_state'] == 'live_success'
    assert value['query_plan']['grain'] == 'owner+order+sku'
    assert value['query_plan']['scope']['unit'] == 'source_quantity'
    assert value['query_plan']['order_codes'] == []
    tables = [_public_business_table(table, live=True) for table in value['business_tables']]
    assert all(tables) and len(tables) == 2
    orders, skus = tables
    assert len(orders['rows']) == len(skus['rows']) == 1
    row = skus['rows'][0]
    assert row['order_ship_quantity'] == '10'
    assert row['returned_quantity'] == '3' and row['net_sales_quantity'] == '7'
    assert row['return_document_count'] == '2'
    assert row['sales_detail_count'] == '2'
    assert not any('payment' in key or 'refund' in key for table in tables for row in table['rows'] for key in row)
    assert not any('scm_sales_shipment_' in sql for sql in client.calls)
    assert not any('EA' == column.get('unit') for column in skus['columns'])
    assert len(skus['notes']) == 3


def test_missing_sale_lines_and_missing_return_sku_do_not_become_zero(order_facts, return_facts):
    header(order_facts)
    header(order_facts, order='S2')
    return_header(return_facts)
    return_detail(return_facts)
    value, _ = run(order_facts, return_facts)
    assert value['result_state'] == 'live_success'
    tables = [_public_business_table(table, live=True) for table in value['business_tables']]
    assert len(tables[0]['rows']) == 2
    assert all(row['order_detail_state'] == '缺少范围内有效销售明细' for row in tables[0]['rows'])
    row = tables[1]['rows'][0]
    assert row['order_ship_quantity'] is None and row['net_sales_quantity'] is None
    assert row['returned_quantity'] == '3'
    assert '缺少销售明细' in row['quantity_state']


def test_excess_return_retains_negative_and_clear_business_warning(order_facts, return_facts):
    header(order_facts)
    detail(order_facts, quantity=2)
    return_header(return_facts)
    return_detail(return_facts, quantity=3)
    value, _ = run(order_facts, return_facts)
    row = _public_business_table(value['business_tables'][1], live=True)['rows'][0]
    assert row['net_sales_quantity'] == '-1'
    assert '退货数量超过实发数量' in row['quantity_state']


def test_no_data_and_order_without_details_are_distinct(order_facts, return_facts):
    empty, _ = run(order_facts, return_facts)
    assert empty['result_state'] == 'no_data'
    header(order_facts)
    nonempty, _ = run(order_facts, return_facts)
    assert nonempty['result_state'] == 'live_success'
    assert nonempty['business_tables'][1]['rows'] == []


@pytest.mark.parametrize('scope', [QueryScope(owner='B', unit='source_quantity'), QueryScope(unit='EA'),
                                  QueryScope(warehouse_codes=('W1',), unit='source_quantity'),
                                  QueryScope(channel='2C', unit='source_quantity')])
def test_unexpected_scope_stops_before_query(order_facts, return_facts, scope):
    client = Query(order_facts, return_facts)
    with pytest.raises(SemanticRuntimeError) as failure:
        SemanticQueryRuntime(query_client=client).execute_plan(replace(request(), scope=scope), question='query')
    assert failure.value.reason == 'order_return_scope_invalid'
    assert not client.calls


def test_explicit_dates_and_identifier_are_preserved(order_facts, return_facts):
    header(order_facts, order='SO-2026-08-03')
    zone = ZoneInfo('Asia/Shanghai')
    plan = OrderComponentPlan(order_codes=('SO-2026-08-03',), start=datetime(2026, 9, 1, tzinfo=zone),
                              end=datetime(2026, 10, 1, tzinfo=zone))
    value, _ = run(order_facts, return_facts, plan)
    assert value['query_plan']['order_codes'] == ['SO-2026-08-03']
    assert '2026-09-01' in value['business_table']['caption']
    assert '2026-10-01' in value['business_table']['caption']


def test_conflict_and_truncation_never_produce_partial_tables(order_facts, return_facts):
    header(order_facts)
    detail(order_facts)
    return_header(return_facts)
    return_detail(return_facts, order='CONFLICT')
    with pytest.raises(SemanticRuntimeError) as failure: run(order_facts, return_facts)
    assert failure.value.reason == 'return_component_quality_failed'
    client = Query(order_facts, return_facts)
    client.truncated = True
    with pytest.raises(SemanticRuntimeError) as failure:
        SemanticQueryRuntime(query_client=client).execute_plan(request(), question='query')
    assert failure.value.reason == 'result_truncated'


def test_unpublished_contract_cannot_execute_even_with_direct_plan(order_facts, return_facts, monkeypatch):
    monkeypatch.setattr('app.agent_core.handlers.order_returns.return_quantity_contract_active', lambda _: False)
    client = Query(order_facts, return_facts)
    with pytest.raises(SemanticRuntimeError) as failure:
        SemanticQueryRuntime(query_client=client).execute_plan(request(), question='query')
    assert failure.value.reason == 'order_return_not_published'
    assert not client.calls


@pytest.mark.parametrize('field', ['return_document_count', 'sales_detail_count', 'return_detail_count'])
def test_signed_quantity_permission_does_not_allow_negative_counts(order_facts, return_facts, field):
    header(order_facts)
    detail(order_facts)
    value, _ = run(order_facts, return_facts)
    raw = value['business_tables'][1]
    raw['rows'][0][field] = '-1'
    raw['columns'] = [{'key': field, 'signed': True}]
    assert _public_business_table(raw, live=True) is None


@pytest.mark.parametrize('defect', ['unreviewed', 'not_live', 'missing_table', 'incomplete', 'unsafe_last_page'])
def test_both_tables_checked_before_any_page_is_created(order_facts, return_facts, defect):
    from app.chat_api import _public_order_return_tables
    header(order_facts)
    detail(order_facts)
    value, _ = run(order_facts, return_facts)
    tables = value['business_tables']
    tables[0]['rows'] *= 101
    tables[1]['rows'] *= 101
    if defect == 'missing_table': tables.pop()
    if defect == 'incomplete': tables[1]['complete_reconciled'] = False
    if defect == 'unsafe_last_page': tables[1]['rows'][-1] = dict(tables[1]['rows'][-1], sku_code='SELECT hidden FROM private.table')
    class NoPages:
        def create(self, *args, **kwargs): raise AssertionError('partial result was cached')
    with pytest.raises(ChatExecutionError):
        _public_order_return_tables(tables, reviewed=defect != 'unreviewed', live=defect != 'not_live',
                                    pages_store=NoPages(), owner='A')


def test_page_results_remain_bound_to_owner(order_facts, return_facts):
    from app.chat_api import _public_order_return_tables
    header(order_facts)
    detail(order_facts)
    value, _ = run(order_facts, return_facts)
    value['business_tables'][1]['rows'] *= 101
    store = ResultPageStore()
    tables = _public_order_return_tables(value['business_tables'], reviewed=True, live=True, pages_store=store, owner='A')
    assert len(tables[1]['rows']) == 100
    assert tables[1]['pagination']['total_rows'] == 101
    rid = tables[1]['pagination']['result_id']
    assert len(store.page('A', rid, 2)['rows']) == 1
    with pytest.raises(ResultPageError) as failure: store.page('B', rid, 2)
    assert failure.value.http_status == 404


@pytest.mark.parametrize('defect', ['candidate', 'unapproved', 'version', 'dependency_version', 'malformed_approval'])
def test_real_publication_check_requires_whole_supported_dependency_set(tmp_path, defect):
    import yaml
    metrics = tmp_path / 'metrics'
    metrics.mkdir()
    ids = ('sales_order.components', 'sales_order.return_components', 'sales_order.net_returns')
    for name in ids:
        value = {'id': name, 'version': '1.0.0', 'status': 'active', 'approval': {'state': 'approved'}}
        if name == 'sales_order.net_returns':
            value['dependency_versions'] = {ids[0]: '1.0.0', ids[1]: '1.0.0'}
        (metrics / (name + '.yaml')).write_text(yaml.safe_dump(value), encoding='utf-8')
    assert actual_active_gate(tmp_path)
    path = metrics / (('sales_order.net_returns' if defect == 'dependency_version' else ids[0]) + '.yaml')
    value = yaml.safe_load(path.read_text(encoding='utf-8'))
    if defect == 'candidate': value['status'] = 'candidate'
    if defect == 'unapproved': value['approval']['state'] = 'pending_business_approval'
    if defect == 'version': value['version'] = '2.0.0'
    if defect == 'dependency_version': value['dependency_versions'][ids[0]] = '2.0.0'
    if defect == 'malformed_approval': value['approval'] = None
    path.write_text(yaml.safe_dump(value), encoding='utf-8')
    assert not actual_active_gate(tmp_path)

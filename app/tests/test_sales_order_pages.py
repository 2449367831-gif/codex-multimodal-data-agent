from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import runpy

import pytest
import sqlglot
from sqlglot import exp

from app.agent_core.sales_order_details import retrieve_sales_order_details
from app.agent_core.semantic_types import SemanticRuntimeError
from app.tests.test_sales_order_details import PLAN, orders


class Query:
    def __init__(self, db, mutate=None):
        self.db, self.mutate, self.calls = db, mutate, []
        self.quality_calls = []

    def __call__(self, sql, **kwargs):
        if kwargs.get('purpose') == 'SO07 reference integrity':
            self.quality_calls.append(sql)
            return [dict(row) for row in self.db.execute(sqlglot.transpile(sql, read='doris', write='sqlite')[0])]
        self.calls.append(sql)
        rows = [dict(row) for row in self.db.execute(sqlglot.transpile(sql, read='doris', write='sqlite')[0])]
        return self.mutate(rows, len(self.calls)) if self.mutate else rows


def test_complete_reader_exceeds_single_probe_without_increasing_page_limit(orders):
    query = Query(orders)
    result = retrieve_sales_order_details(replace(PLAN, page_limit=2), query)
    assert result.row_count == 4 and result.order_count == 3
    assert result.total_request_quantity == Decimal('9.5')
    assert len(query.calls) == 4
    assert len(query.quality_calls) == 2
    assert all(int(sqlglot.parse_one(sql, read='doris').args['limit'].expression.this) == 2 for sql in query.calls)
    assert {row['cargo_owner'] for row in result.rows} == {'A', 'B'}


def test_same_code_in_other_owner_and_missing_detail_remain_separate(orders):
    orders.execute("UPDATE ywzt_ods.scm_sales_order_header SET sales_order_code='SO1' WHERE company_code='B'")
    orders.execute("UPDATE ywzt_ods.scm_sales_order_detail SET sales_order_code='SO1' WHERE company_code='B'")
    orders.execute("DELETE FROM ywzt_ods.scm_sales_order_detail WHERE sales_order_code='SO2'")
    result = retrieve_sales_order_details(replace(PLAN, page_limit=1), Query(orders))
    assert result.order_count == 3 and result.row_count == 4
    assert next(row for row in result.rows if row['sales_order_code'] == 'SO2')['request_quantity'] is None
    assert {(row['cargo_owner'], row['sales_order_code']) for row in result.rows} == {('A', 'SO1'), ('A', 'SO2'), ('B', 'SO1')}


@pytest.mark.parametrize('failure', ['missing_page', 'duplicate_page', 'bad_ordinal', 'changed_count', 'changed_content'])
def test_missing_or_changed_pages_never_return_partial_results(orders, failure):
    first = []
    def mutate(rows, call):
        if call == 1:
            first.extend(dict(row) for row in rows)
        if call == 2:
            if failure == 'missing_page': return []
            if failure == 'duplicate_page': return first
            if failure == 'bad_ordinal': rows[0]['result_ordinal'] = 100
            if failure == 'changed_count': rows[0]['group_count'] += 1
        if call == 3 and failure == 'changed_content':
            rows[0]['customer_name'] = 'Changed during read'
        return rows
    with pytest.raises(SemanticRuntimeError):
        retrieve_sales_order_details(replace(PLAN, page_limit=2), Query(orders, mutate))


@pytest.mark.parametrize('budget', [{'max_rows': 3}, {'max_bytes': 1}])
def test_total_budget_stops_before_remainder_is_queried(orders, budget):
    query = Query(orders)
    with pytest.raises(SemanticRuntimeError) as failure:
        retrieve_sales_order_details(replace(PLAN, page_limit=2), query, **budget)
    assert failure.value.reason == 'result_truncated' and len(query.calls) == 1


@pytest.mark.parametrize('budget', [{'max_rows': 100001}, {'max_rows': True}, {'max_bytes': 0},
                                  {'timeout_seconds': 0}, {'timeout_seconds': float('inf')}])
def test_invalid_budget_is_rejected_before_any_query(orders, budget):
    query = Query(orders)
    with pytest.raises(ValueError):
        retrieve_sales_order_details(PLAN, query, **budget)
    assert not query.calls
    assert not query.quality_calls


def test_empty_result_is_rechecked_before_returning_no_data(orders):
    query = Query(orders)
    result = retrieve_sales_order_details(replace(PLAN, owner='MISSING'), query)
    assert result.rows == () and result.row_count == result.order_count == 0
    assert len(query.calls) == 2


def test_empty_then_nonempty_is_a_change_not_success(orders):
    with pytest.raises(SemanticRuntimeError):
        retrieve_sales_order_details(PLAN, Query(orders, lambda rows, call: [] if call == 1 else rows))


def test_scope_remains_exact_on_every_page_and_new_request(orders):
    query = Query(orders)
    scoped = retrieve_sales_order_details(replace(PLAN, owner='B', warehouse_codes=('W1',), sku_codes=('S6',), page_limit=1), query)
    assert len(scoped.rows) == 1 and scoped.rows[0]['cargo_owner'] == 'B'
    assert retrieve_sales_order_details(PLAN, query).row_count == 4


def test_late_query_failure_propagates_without_returning_first_page(orders):
    def mutate(rows, call):
        if call == 2:
            raise SemanticRuntimeError('query failed', reason='query_failed')
        return rows
    with pytest.raises(SemanticRuntimeError) as failure:
        retrieve_sales_order_details(replace(PLAN, page_limit=2), Query(orders, mutate))
    assert failure.value.reason == 'query_failed'


def test_global_quantity_control_is_checked_after_all_pages(orders):
    def mutate(rows, call):
        for row in rows:
            row['total_request_quantity'] = '100'
        return rows
    with pytest.raises(SemanticRuntimeError) as failure:
        retrieve_sales_order_details(replace(PLAN, page_limit=2), Query(orders, mutate))
    assert failure.value.reason == 'sales_order_quality_failed'


def test_query_many_keeps_wave_bound_and_identical_output(orders):
    query = Query(orders)
    waves = []
    def query_many(statements, **kwargs):
        waves.append(len(statements))
        return [query(sql) for sql in statements]
    result = retrieve_sales_order_details(replace(PLAN, page_limit=1), query, query_many=query_many)
    assert result.row_count == 4 and waves == [1, 3, 1, 3]


def test_overall_deadline_stops_paging_before_more_queries(orders, monkeypatch):
    now = [0.0]
    monkeypatch.setattr('app.agent_core.sales_pages.time.monotonic', lambda: now[0])
    def mutate(rows, call):
        now[0] += 1
        return rows
    query = Query(orders, mutate)
    with pytest.raises(SemanticRuntimeError) as failure:
        retrieve_sales_order_details(replace(PLAN, page_limit=1), query, timeout_seconds=2)
    assert failure.value.reason == 'query_timeout' and len(query.calls) == 2


def test_so07_compatibility_fixture_replaces_nested_physical_sources(orders):
    fixture = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'tests/acceptance/check_so07_pages_live.py'))
    query = Query(orders)
    retrieve_sales_order_details(replace(PLAN, page_limit=1), query)
    for sql in query.calls + query.quality_calls:
        replaced = fixture['FIXTURE']['synthetic_sql'](PLAN, fixture['FIXTURE']['fixtures'](), source_sql=sql)
        tree = sqlglot.parse_one(replaced, read='doris')
        assert not any(table.db or table.catalog for table in tree.find_all(exp.Table))
        assert int(tree.args['limit'].expression.this) == 1
    cases = list(fixture['fixture_cases']())
    assert {case[0] for case in cases} == {
        'sequential', 'batched', 'multiple_owners_missing_detail', 'exact_scope', 'empty',
        'row_budget', 'changed_between_passes', 'invalid_source',
        'all_time_keeps_old_order_without_detail', 'dated_excludes_old_order',
        'all_time_exact_sku', 'all_time_checks_old_reference_conflict',
    }
    for _, plan, source, _, _ in cases:
        replaced = fixture['FIXTURE']['synthetic_sql'](plan, source)
        tree = sqlglot.parse_one(replaced, read='doris')
        assert not any(table.db or table.catalog for table in tree.find_all(exp.Table))
        assert not list(tree.find_all(exp.Placeholder))

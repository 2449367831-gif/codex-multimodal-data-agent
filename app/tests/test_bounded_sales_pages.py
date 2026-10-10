from copy import deepcopy

import pytest
import sqlglot
from sqlglot import exp

from app.agent_core.sales_pages import retrieve_sales_groups, aggregate_page_sql
from app.agent_core.semantic_types import SemanticRuntimeError


SQL = 'SELECT 1 AS quantity ORDER BY quantity LIMIT 501'


class Pages:
    def __init__(self, count=11, mutate=None):
        self.rows = [dict(result_ordinal=i, group_count=count, quantity=str(i)) for i in range(1, count+1)]
        self.calls, self.waves, self.deadlines = [], [], []
        self.passes = 0
        self.mutate = mutate

    def query(self, sql, *, purpose):
        tree = sqlglot.parse_one(sql, read='doris')
        offset = int(tree.find(exp.GT).expression.this)
        size = int(tree.args['limit'].expression.this)
        if offset == 0:
            self.passes += 1
        self.calls.append((offset, size))
        rows = deepcopy(self.rows[offset:offset+size])
        return self.mutate(rows, offset, self.passes) if self.mutate else rows

    def many(self, statements, *, purpose, timeout_seconds):
        self.waves.append(len(statements))
        self.deadlines.append(timeout_seconds)
        return [self.query(sql, purpose=purpose) for sql in statements]


def test_bounded_waves_match_sequential_two_complete_reads():
    sequential, batched = Pages(), Pages()
    expected = retrieve_sales_groups(SQL, sequential.query, page_size=1)
    actual = retrieve_sales_groups(SQL, batched.query, query_many=batched.many, page_size=1)
    assert actual == expected
    assert batched.calls == sequential.calls
    assert batched.waves == [1, 4, 4, 2]*2
    assert all(0 < value <= 180 for value in batched.deadlines)
    assert batched.deadlines == sorted(batched.deadlines, reverse=True)


@pytest.mark.parametrize('count,expected', [(0, [1, 1]), (1, [1, 1]), (10, [1, 4, 1, 4]), (11, [1, 4, 1]*2)])
def test_first_page_controls_empty_and_short_last_pages(count, expected):
    pages = Pages(count)
    rows = retrieve_sales_groups(SQL, pages.query, query_many=pages.many, page_size=2)
    assert len(rows) == count and pages.waves == expected


@pytest.mark.parametrize('mutation', ['missing', 'ordinal', 'count', 'changed_between_passes', 'missing_wave', 'swapped_pages'])
def test_invalid_later_pages_never_return_partial_results(mutation):
    def mutate(rows, offset, pass_number):
        if offset == 2:
            if mutation == 'missing':return []
            if mutation == 'ordinal':rows[0]['result_ordinal'] = 999
            if mutation == 'count':rows[0]['group_count'] += 1
            if mutation == 'changed_between_passes' and pass_number == 2:rows[0]['quantity'] = 'changed'
        return rows
    pages = Pages(mutate=mutate)
    def many(*args, **kwargs):
        result = pages.many(*args, **kwargs)
        if len(result) > 1:
            if mutation == 'missing_wave':result.pop()
            if mutation == 'swapped_pages':result.reverse()
        return result
    with pytest.raises(SemanticRuntimeError) as failure:
        retrieve_sales_groups(SQL, pages.query, query_many=many, page_size=1)
    assert failure.value.reason == 'sales_page_invalid'


def test_content_change_can_be_rechecked_with_explicit_bounded_retry():
    def mutate(rows, offset, pass_number):
        if offset == 2 and pass_number == 2:
            rows[0]['quantity'] = 'changed-once'
        return rows

    pages = Pages(mutate=mutate)
    rows = retrieve_sales_groups(SQL, pages.query, query_many=pages.many,
                                 page_size=1, consistency_retries=1)
    assert len(rows) == 11
    assert pages.passes == 4


def test_consistency_retry_budget_is_bounded():
    pages = Pages()
    with pytest.raises(ValueError):
        retrieve_sales_groups(SQL, pages.query, consistency_retries=3)


@pytest.mark.parametrize('budget', ['rows', 'bytes'])
def test_first_page_over_budget_does_not_dispatch_more_pages(budget):
    pages = Pages()
    with pytest.raises(SemanticRuntimeError) as failure:
        retrieve_sales_groups(SQL, pages.query, query_many=pages.many, page_size=1,
                              **({'max_rows': 10} if budget == 'rows' else {'max_bytes': 1}))
    assert failure.value.reason == 'result_truncated'
    assert pages.waves == [1]


def test_timeout_is_shared_by_both_passes_and_stops_new_dispatch(monkeypatch):
    now = [0.0]
    monkeypatch.setattr('app.agent_core.sales_pages.time.monotonic', lambda: now[0])
    pages = Pages(3)
    def many(*args, **kwargs):
        result = pages.many(*args, **kwargs)
        now[0] += 1
        return result
    with pytest.raises(SemanticRuntimeError) as failure:
        retrieve_sales_groups(SQL, pages.query, query_many=many, page_size=1, timeout_seconds=3)
    assert failure.value.reason == 'query_timeout'
    assert pages.waves == [1, 2, 1] and pages.deadlines == [3, 2, 1]


@pytest.mark.parametrize('reason', ['backend_rejected', 'query_failed', 'query_timeout', 'result_truncated'])
def test_batch_failure_category_is_preserved(reason):
    pages = Pages()
    def many(*args, **kwargs):
        if pages.waves:
            raise SemanticRuntimeError('synthetic failure', reason=reason)
        return pages.many(*args, **kwargs)
    with pytest.raises(SemanticRuntimeError) as failure:
        retrieve_sales_groups(SQL, pages.query, query_many=many, page_size=1)
    assert failure.value.reason == reason
    assert pages.waves == [1]


@pytest.mark.parametrize('budget', [dict(page_size=501), dict(max_rows=100001), dict(max_bytes=64*1024*1024+1),
                                   dict(timeout_seconds=181), dict(timeout_seconds=float('nan')),
                                   dict(timeout_seconds=float('inf')), dict(timeout_seconds=True)])
def test_budgets_cannot_be_relaxed_or_non_finite(budget):
    pages = Pages()
    with pytest.raises(ValueError):
        retrieve_sales_groups(SQL, pages.query, query_many=pages.many, **budget)
    assert not pages.calls and not pages.waves


def test_larger_internal_pages_require_explicit_cap_and_backend_validation():
    from app.adhoc_sql import AdHocSqlValidator
    from app.errors import SqlSafetyError
    source='SELECT sku_code,COUNT(*) OVER() AS group_count FROM ywzt_ods.master_sku ORDER BY sku_code LIMIT 501'
    with pytest.raises(ValueError):aggregate_page_sql(source,0,2000)
    sql=aggregate_page_sql(source,0,2000,page_cap=2000)
    assert sqlglot.parse_one(sql,read='doris').args['limit'].expression.this=='2000'
    AdHocSqlValidator(allowed_schemas=('ywzt_ods',),max_rows=10000).validate(sql)
    with pytest.raises(SqlSafetyError):AdHocSqlValidator(allowed_schemas=('ywzt_ods',),max_rows=500).validate(sql)


def test_explicit_larger_pages_preserve_two_complete_passes():
    pages=Pages(4501)
    rows=retrieve_sales_groups(SQL,pages.query,query_many=pages.many,page_size=2000,page_cap=2000)
    assert len(rows)==4501 and pages.waves==[1,2,1,2]
    assert pages.calls==[(0,2000),(2000,2000),(4000,2000)]*2


@pytest.mark.parametrize('cap,size',[(10001,500),(0,1),(True,1),(2000,2001)])
def test_invalid_explicit_page_cap_never_executes(cap,size):
    pages=Pages()
    with pytest.raises(ValueError):retrieve_sales_groups(SQL,pages.query,page_cap=cap,page_size=size)
    assert not pages.calls

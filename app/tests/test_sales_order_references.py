"""Synthetic missing-reference counterexamples, before numerical acceptance."""

from dataclasses import replace

import pytest

from app.agent_core.sales_order_details import retrieve_sales_order_details
from app.agent_core.semantic_types import SemanticRuntimeError
from app.tests.test_sales_order_details import PLAN, orders
from app.tests.test_sales_order_pages import Query


@pytest.mark.parametrize('assignment', [
    "company_code=NULL", "company_code=' '", "sales_order_code=NULL",
    "sales_order_code=' '", "company_code='B'", "sales_order_code='OTHER'",
    "company_code=NULL,sales_order_code=NULL", "sales_order_id='H3'",
    "sales_order_id='MISSING'",
])
def test_reachable_bad_reference_stops_complete_reader(orders, assignment):
    orders.execute("UPDATE ywzt_ods.scm_sales_order_detail SET sales_order_id='H1' WHERE id='D1'")
    orders.execute(f"UPDATE ywzt_ods.scm_sales_order_detail SET {assignment} WHERE id='D1'")
    with pytest.raises(SemanticRuntimeError) as failure:
        retrieve_sales_order_details(PLAN, Query(orders))
    assert failure.value.reason == 'sales_order_reference_failed'


@pytest.mark.parametrize('assignment', ["company_code=NULL", "sales_order_code=NULL"])
def test_partial_business_reference_without_physical_id_is_not_silent_missing_detail(orders, assignment):
    orders.execute(f"UPDATE ywzt_ods.scm_sales_order_detail SET {assignment} WHERE id='D1'")
    with pytest.raises(SemanticRuntimeError) as failure:
        retrieve_sales_order_details(replace(PLAN, sku_codes=('S1',)), Query(orders))
    assert failure.value.reason == 'sales_order_reference_failed'


def test_conflict_reachable_from_either_selected_order_is_rejected(orders):
    orders.execute("UPDATE ywzt_ods.scm_sales_order_detail SET sales_order_id='H5' WHERE id='D1'")
    for owner in ('A', 'B'):
        with pytest.raises(SemanticRuntimeError) as failure:
            retrieve_sales_order_details(replace(PLAN, owner=owner), Query(orders))
        assert failure.value.reason == 'sales_order_reference_failed'


@pytest.mark.parametrize('scope', [dict(owner='B'), dict(warehouse_codes=('W2',)), dict(sku_codes=('S3',))])
def test_known_unrelated_reference_failure_does_not_expand_scope(orders, scope):
    orders.execute("UPDATE ywzt_ods.scm_sales_order_detail SET sales_order_id='MISSING' WHERE id='D1'")
    assert retrieve_sales_order_details(replace(PLAN, **scope), Query(orders)).row_count == 1


@pytest.mark.parametrize('excluded', ["status='1'", "_fdl_marked_deleted='true'"])
def test_known_excluded_detail_is_not_a_reference_failure(orders, excluded):
    orders.execute(f"UPDATE ywzt_ods.scm_sales_order_detail SET sales_order_id='MISSING', {excluded} WHERE id='D1'")
    assert retrieve_sales_order_details(PLAN, Query(orders)).row_count == 3


def test_optional_physical_reference_does_not_replace_business_join(orders):
    orders.execute("UPDATE ywzt_ods.scm_sales_order_detail SET sales_order_id='H1' WHERE id='D1'")
    result = retrieve_sales_order_details(PLAN, Query(orders))
    assert result.row_count == 4 and str(result.total_request_quantity) == '9.5'
    assert all('sales_order_id' not in row for row in result.rows)


@pytest.mark.parametrize('stage', [1, 2])
@pytest.mark.parametrize('controls', [[], None, [{}], [{'missing_business_reference': 0}],
    [{'missing_business_reference': 0, 'conflicting_physical_reference': None}],
    [{'missing_business_reference': 0, 'conflicting_physical_reference': False}],
    [{'missing_business_reference': 0, 'conflicting_physical_reference': -1}],
    [{'missing_business_reference': 0, 'conflicting_physical_reference': 0, 'extra': 'untrusted'}]])
def test_pre_and_post_checks_require_complete_controls(orders, stage, controls):
    delegate, checks = Query(orders), []
    def query(sql, **kwargs):
        if kwargs.get('purpose') == 'SO07 reference integrity':
            checks.append(sql)
            if len(checks) == stage:
                return controls
        return delegate(sql, **kwargs)
    with pytest.raises(SemanticRuntimeError) as failure:
        retrieve_sales_order_details(PLAN, query)
    assert failure.value.reason == 'sales_order_reference_failed'
    assert len(delegate.calls) == (0 if stage == 1 else 2)


def test_reference_change_after_last_page_never_returns_saved_results(orders):
    def mutate(rows, call):
        if call == 2:
            orders.execute("UPDATE ywzt_ods.scm_sales_order_detail SET sales_order_id='MISSING' WHERE id='D1'")
        return rows
    with pytest.raises(SemanticRuntimeError) as failure:
        retrieve_sales_order_details(PLAN, Query(orders, mutate))
    assert failure.value.reason == 'sales_order_reference_failed'


@pytest.mark.parametrize('stage', [1, 2])
def test_reference_checks_share_overall_deadline(orders, monkeypatch, stage):
    now, checks, delegate = [0.0], [], Query(orders)
    monkeypatch.setattr('app.agent_core.sales_order_details.clock.monotonic', lambda: now[0])
    def query(sql, **kwargs):
        if kwargs.get('purpose') == 'SO07 reference integrity':
            checks.append(sql)
            if len(checks) == stage:
                now[0] += 3
        return delegate(sql, **kwargs)
    with pytest.raises(SemanticRuntimeError) as failure:
        retrieve_sales_order_details(PLAN, query, timeout_seconds=2)
    assert failure.value.reason == 'query_timeout'
    assert len(delegate.calls) == (0 if stage == 1 else 2)


def test_reference_backend_failure_is_not_called_bad_business_data(orders):
    def query(sql, **kwargs):
        raise SemanticRuntimeError('backend failure', reason='query_failed')
    with pytest.raises(SemanticRuntimeError) as failure:
        retrieve_sales_order_details(PLAN, query)
    assert failure.value.reason == 'query_failed'


def test_completely_unlinked_detail_is_not_assigned_an_invented_order_or_date(orders):
    orders.execute("UPDATE ywzt_ods.scm_sales_order_detail SET company_code=NULL,sales_order_code=NULL WHERE id='D1'")
    # Without any surviving reference the SO07 type/date cannot be established.
    # This scoped diagnostic does not assert global source completeness.
    assert retrieve_sales_order_details(PLAN, Query(orders)).row_count == 3


def test_duplicate_header_identity_with_different_business_keys_is_not_arbitrarily_chosen(orders):
    orders.execute("UPDATE ywzt_ods.scm_sales_order_header SET id='H1' WHERE id='H5'")
    orders.execute("UPDATE ywzt_ods.scm_sales_order_detail SET sales_order_id='H1' WHERE id='D1'")
    with pytest.raises(SemanticRuntimeError) as failure:
        retrieve_sales_order_details(PLAN, Query(orders))
    assert failure.value.reason == 'sales_order_reference_failed'


def test_constant_reference_scenarios_are_source_free_and_match_expected_flags(orders):
    from pathlib import Path
    import runpy
    import sqlglot
    from sqlglot import exp
    fixture = runpy.run_path(str(Path(__file__).resolve().parents[2] /
                                'tests/acceptance/check_so07_references_live.py'))
    for name, sql, expected in fixture['cases']():
        tree = sqlglot.parse_one(sql, read='doris')
        assert not any(table.db or table.catalog for table in tree.find_all(exp.Table)), name
        for cast in list(tree.find_all(exp.Cast)):
            if cast.to.is_type('DATETIME', 'TIMESTAMP') and isinstance(cast.this, exp.Literal):
                cast.replace(cast.this.copy())
        rows = [dict(row) for row in orders.execute(tree.sql(dialect='sqlite'))]
        assert len(rows) == 1 and any(rows[0].values()) == expected, name


def test_reference_reachability_uses_keyed_branches_not_disjunctive_fact_join():
    import sqlglot
    from sqlglot import exp
    from app.agent_core.sales_order_references import compile_sales_order_reference_quality
    tree = sqlglot.parse_one(compile_sales_order_reference_quality(PLAN), read='doris')
    joins = list(tree.find_all(exp.Join))
    assert len(joins) == 4
    for join in joins:
        predicate = join.args['on']
        assert not list(predicate.find_all(exp.Or))
        assert any(isinstance(eq.this, exp.Column) and isinstance(eq.expression, exp.Column)
                   and {eq.this.table, eq.expression.table} == {'h', 'd'}
                   for eq in predicate.find_all(exp.EQ))

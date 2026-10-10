"""Independent SO07 source reconstruction, before any real-source comparison."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import runpy

import pytest
import sqlglot

from app.agent_core.sales_order_details import reconcile_sales_order_details
from app.tests.test_sales_order_details import PLAN, orders, execute


REFERENCE = runpy.run_path(str(Path(__file__).resolve().parents[2] /
                             'tests/acceptance/check_so07_source_live.py'))
reference_sql = REFERENCE['reference_sql']
expected_details = REFERENCE['expected_details']
canonical_rows = REFERENCE['canonical_rows']


def raw(db, plan=PLAN):
    return {source: [dict(row) for row in db.execute(
        sqlglot.parse_one(reference_sql(plan, source), read='doris').sql(dialect='sqlite'))]
        for source in ('header', 'detail', 'warehouse')}


def unfiltered(db):
    return {source: [dict(row) for row in db.execute(f'SELECT * FROM ywzt_ods.{table}')]
            for source, table in REFERENCE['TABLES'].items()}


@pytest.mark.parametrize('changes', [
    '',
    "DELETE FROM ywzt_ods.scm_sales_order_detail WHERE sales_order_code='SO2'",
    "UPDATE ywzt_ods.scm_sales_order_detail SET facility_code='' WHERE id='D1'",
    "UPDATE ywzt_ods.scm_sales_order_detail SET facility_code='W2' WHERE id='D1'",
    "UPDATE ywzt_ods.scm_sales_order_detail SET quantity_u_m=' 010 ',sales_unit='040' WHERE id='D1'",
    "UPDATE ywzt_ods.scm_sales_order_detail SET quantity_u_m='020',sales_unit='040' WHERE id='D2'",
    "UPDATE ywzt_ods.scm_sales_order_header SET store_name=NULL WHERE id='H2'",
    "DELETE FROM ywzt_ods.master_wms WHERE cargo_owner_id='B'",
    "INSERT INTO ywzt_ods.master_wms SELECT * FROM ywzt_ods.master_wms WHERE cargo_owner_id='B'",
    "INSERT INTO ywzt_ods.scm_sales_order_header SELECT 'H1-alt',company_code,sales_order_code,facility_code,store_code,store_name,order_type,created,status,deleted_flag,'FALSE',updated FROM ywzt_ods.scm_sales_order_header WHERE id='H1'",
    "UPDATE ywzt_ods.scm_sales_order_detail SET _fdl_marked_deleted='true' WHERE id='D3'",
    "UPDATE ywzt_ods.scm_sales_order_detail SET status='1' WHERE id='D3'",
])
def test_raw_reference_matches_candidate_without_reusing_its_sql(orders, changes):
    if changes:
        orders.execute(changes)
    expected = expected_details(PLAN, raw(orders))
    result = reconcile_sales_order_details(execute(orders), PLAN)
    assert canonical_rows(result.rows) == expected


@pytest.mark.parametrize('plan', [
    PLAN, replace(PLAN, owner='B'), replace(PLAN, owner='MISSING'),
    replace(PLAN, warehouse_codes=('W2',)), replace(PLAN, sku_codes=('S2',)),
    replace(PLAN, start=None, end=None),
])
def test_reference_filters_current_request_scope_only(orders, plan):
    assert expected_details(plan, unfiltered(orders)) == expected_details(plan, raw(orders, plan)) == canonical_rows(
        reconcile_sales_order_details(execute(orders, plan), plan).rows)


@pytest.mark.parametrize('change', [
    "UPDATE ywzt_ods.scm_sales_order_header SET company_code=NULL WHERE id='H2'",
    "UPDATE ywzt_ods.scm_sales_order_header SET created=NULL WHERE id='H2'",
    "UPDATE ywzt_ods.scm_sales_order_header SET deleted_flag=NULL WHERE id='H2'",
    "UPDATE ywzt_ods.scm_sales_order_detail SET _fdl_marked_deleted=NULL WHERE id='D1'",
    "UPDATE ywzt_ods.scm_sales_order_detail SET request_qty=NULL WHERE id='D1'",
    "UPDATE ywzt_ods.scm_sales_order_detail SET request_qty=-1 WHERE id='D1'",
    "UPDATE ywzt_ods.scm_sales_order_detail SET id='' WHERE id='D1'",
    "UPDATE ywzt_ods.scm_sales_order_detail SET sales_order_id='OTHER' WHERE id='D1'",
    "UPDATE ywzt_ods.scm_sales_order_detail SET sales_order_id='H1',company_code='B' WHERE id='D1'",
    "UPDATE ywzt_ods.scm_sales_order_detail SET company_code=NULL WHERE id='D1'",
    "UPDATE ywzt_ods.scm_sales_order_detail SET sales_order_code=NULL WHERE id='D1'",
    "INSERT INTO ywzt_ods.scm_sales_order_detail SELECT * FROM ywzt_ods.scm_sales_order_detail WHERE id='D1'",
    "INSERT INTO ywzt_ods.scm_sales_order_header SELECT 'other',company_code,sales_order_code,facility_code,store_code,store_name,'JYCK','2020-01-01',status,deleted_flag,_fdl_marked_deleted,updated FROM ywzt_ods.scm_sales_order_header WHERE id='H2'",
    "INSERT INTO ywzt_ods.master_wms VALUES ('A','W1','conflict','0')",
])
def test_reference_rejects_source_defects_without_using_candidate_controls(orders, change):
    orders.execute(change)
    with pytest.raises(ValueError):
        expected_details(PLAN, raw(orders))
    with pytest.raises(ValueError):
        expected_details(PLAN, unfiltered(orders))


def test_reference_checks_every_field_not_just_total_quantity(orders):
    rows = list(reconcile_sales_order_details(execute(orders), PLAN).rows)
    expected = expected_details(PLAN, raw(orders))
    for key in rows[0]:
        changed = deepcopy(rows)
        changed[0][key] = '2000-01-01' if key.endswith('_at') else 'WRONG'
        try:
            assert canonical_rows(changed) != expected
        except (ValueError, ArithmeticError):
            pass


def test_reference_sources_never_use_shipment_or_candidate_ctes():
    for source in ('header', 'detail', 'warehouse'):
        sql = reference_sql(PLAN, source)
        assert 'scm_sales_shipment' not in sql
        assert 'order_facts' not in sql and 'scoped_facts' not in sql
        assert 'request_qty' not in sql or source == 'detail'


@pytest.mark.parametrize('changed', [False, True])
def test_complete_verifier_compares_before_runtime_and_after(orders, changed):
    stages, calls = [], []
    def report(stage):
        stages.append(stage)
        if changed and stage == 'reference_after.header':
            orders.execute("UPDATE ywzt_ods.scm_sales_order_header SET store_name='Changed' WHERE id='H2'")
    def query(sql, **kwargs):
        calls.append(sql)
        return [dict(row) for row in orders.execute(sqlglot.parse_one(sql, read='doris').sql(dialect='sqlite'))]
    result = REFERENCE['verify'](PLAN, query, report=report)
    assert result['passed'] is not changed
    assert result['nonempty_verified'] is not changed
    assert result['checks']['source_stable'] is not changed
    assert len(calls) >= 16
    assert stages.index('reference_before.reconstruct') < stages.index('candidate') < stages.index('reference_after.header')


def test_complete_verifier_does_not_call_empty_results_numeric_acceptance(orders):
    def query(sql, **kwargs):
        return [dict(row) for row in orders.execute(sqlglot.parse_one(sql, read='doris').sql(dialect='sqlite'))]
    result = REFERENCE['verify'](replace(PLAN, owner='MISSING'), query)
    assert result['passed'] and not result['nonempty_verified']


@pytest.mark.parametrize('budget', [0, 181, True, float('nan'), float('inf'), '180'])
def test_invalid_shared_deadline_rejected_before_reading(budget):
    def query(*args, **kwargs):
        pytest.fail('invalid verifier must not query')
    with pytest.raises(ValueError):
        REFERENCE['verify'](PLAN, query, timeout_seconds=budget)


def test_backend_failure_is_not_converted_to_an_empty_reference():
    def query(*args, **kwargs):
        raise ConnectionError('synthetic failure')
    with pytest.raises(ConnectionError):
        REFERENCE['verify'](PLAN, query)


def test_warehouse_blank_status_does_not_create_an_active_mapping(orders):
    orders.execute("INSERT INTO ywzt_ods.master_wms VALUES ('A','W1','Inactive mapping','')")
    assert expected_details(PLAN, unfiltered(orders)) == expected_details(PLAN, raw(orders))


def test_warehouse_reference_keeps_detail_override_and_owner_but_skips_unrelated_keys(orders):
    orders.execute("INSERT INTO ywzt_ods.master_wms VALUES ('Z','W1','Unrelated owner','0')")
    orders.execute("INSERT INTO ywzt_ods.master_wms VALUES ('A','W9','Unrelated warehouse','0')")
    orders.execute("UPDATE ywzt_ods.scm_sales_order_detail SET facility_code='W9' WHERE id='D2'")
    source = raw(orders)
    assert ('Z', 'W1') not in {(row['cargo_owner_id'], row['wms_code']) for row in source['warehouse']}
    assert ('A', 'W9') in {(row['cargo_owner_id'], row['wms_code']) for row in source['warehouse']}
    assert expected_details(PLAN, source) == expected_details(PLAN, unfiltered(orders))


def test_full_verifier_uses_bounded_batches_with_a_budget_per_phase(orders):
    calls = []
    def query(sql, **kwargs):
        return [dict(row) for row in orders.execute(sqlglot.parse_one(sql, read='doris').sql(dialect='sqlite'))]
    def query_many(statements, *, purpose, timeout_seconds):
        assert 1 <= len(statements) <= 4 and 0 < timeout_seconds <= 180
        calls.append(timeout_seconds)
        return [query(sql) for sql in statements]
    result = REFERENCE['verify'](PLAN, query, query_many=query_many)
    assert result['passed'] and calls
    assert set(result['phase_seconds']) == {'reference_before', 'candidate', 'reference_after'}
    assert all(0 <= value < result['phase_budget_seconds'] for value in result['phase_seconds'].values())


def test_reference_work_does_not_consume_the_candidates_query_budget(orders, monkeypatch):
    elapsed, phase = [0.0], ['']
    monkeypatch.setattr(REFERENCE['verify'].__globals__['clock'], 'monotonic', lambda: elapsed[0])
    def report(stage):
        if stage in {'reference_before', 'candidate', 'reference_after'}:
            phase[0] = stage
        if stage.endswith('.reconstruct'):
            elapsed[0] += 120
    def query(sql, **kwargs):
        if phase[0] == 'candidate':
            elapsed[0] += 20
        return [dict(row) for row in orders.execute(sqlglot.parse_one(sql, read='doris').sql(dialect='sqlite'))]
    result = REFERENCE['verify'](PLAN, query, report=report)
    assert result['passed']
    assert result['phase_seconds'] == {'reference_before': 120, 'candidate': 80, 'reference_after': 120}


def test_each_phase_still_rejects_exhausted_query_budget(orders, monkeypatch):
    elapsed = [0.0]
    monkeypatch.setattr(REFERENCE['verify'].__globals__['clock'], 'monotonic', lambda: elapsed[0])
    def query(sql, **kwargs):
        elapsed[0] += 181
        return []
    with pytest.raises(TimeoutError):
        REFERENCE['verify'](PLAN, query)


def test_candidate_counts_detail_versions_only_for_eligible_order_keys(orders):
    from app.agent_core.sales_order_details import compile_sales_order_details
    from sqlglot import exp
    tree = sqlglot.parse_one(compile_sales_order_details(PLAN), read='doris')
    source = exp.select('cargo_owner', 'sales_order_code', 'detail_id', 'detail_variants').from_('detail_source')
    ctes = []
    for cte in tree.args['with_'].expressions:
        ctes.append(cte.copy())
        if cte.alias == 'detail_source':
            break
    source.set('with_', exp.With(expressions=ctes))
    rows = [dict(row) for row in orders.execute(source.sql(dialect='sqlite'))]
    assert {(row['cargo_owner'], row['sales_order_code']) for row in rows} == {
        ('A', 'SO1'), ('A', 'SO2'), ('B', 'SO3')}
    assert len(rows) == 4
    orders.execute("INSERT INTO ywzt_ods.scm_sales_order_detail SELECT * FROM ywzt_ods.scm_sales_order_detail WHERE id='D1'")
    repeated = [dict(row) for row in orders.execute(source.sql(dialect='sqlite'))]
    assert [row['detail_variants'] for row in repeated if row['detail_id'] == 'D1'] == [2, 2]


def test_candidate_header_key_pushdown_preserves_out_of_window_conflicts(orders):
    from app.agent_core.sales_order_details import compile_sales_order_details
    from sqlglot import exp
    tree = sqlglot.parse_one(compile_sales_order_details(PLAN), read='doris')
    source = exp.select('cargo_owner', 'sales_order_code', 'order_type', 'order_variants').from_('order_controls')
    ctes = []
    for cte in tree.args['with_'].expressions:
        ctes.append(cte.copy())
        if cte.alias == 'order_controls':
            break
    source.set('with_', exp.With(expressions=ctes))
    rows = [dict(row) for row in orders.execute(source.sql(dialect='sqlite'))]
    assert {(row['cargo_owner'], row['sales_order_code']) for row in rows} == {
        ('A', 'SO1'), ('A', 'SO2'), ('B', 'SO3')}
    orders.execute("INSERT INTO ywzt_ods.scm_sales_order_header SELECT 'H2-alt',company_code,sales_order_code,facility_code,store_code,store_name,'JYCK','2020-01-01',status,deleted_flag,_fdl_marked_deleted,updated FROM ywzt_ods.scm_sales_order_header WHERE id='H2'")
    conflicting = [dict(row) for row in orders.execute(source.sql(dialect='sqlite')) if row['sales_order_code'] == 'SO2']
    assert {row['order_type'] for row in conflicting} == {'SO07', 'JYCK'}
    assert [row['order_variants'] for row in conflicting] == [2, 2]

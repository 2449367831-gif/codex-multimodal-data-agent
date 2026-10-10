from decimal import Decimal, localcontext
import sqlite3

import pytest
import sqlglot

from app.agent_core.return_components import ReturnComponentPlan, compile_return_components, reconcile_return_components, retrieve_return_components
from app.agent_core.semantic_types import SemanticRuntimeError


@pytest.fixture
def facts():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.execute("ATTACH DATABASE ':memory:' AS ywzt_ods")
    db.execute('CREATE TABLE ywzt_ods.scm_return_order_header (company_code TEXT, return_order_code TEXT, ref_order_code TEXT, status INT, updated_date TEXT, _fdl_marked_deleted TEXT)')
    db.execute('CREATE TABLE ywzt_ods.scm_return_order_detail (id TEXT, company_code TEXT, return_order_code TEXT, ref_order_code TEXT, sku_code TEXT, fulfill_qty INT, status INT, updated_date TEXT, _fdl_marked_deleted TEXT)')
    yield db
    db.close()


def header(db, code='R1', owner='A', order='S1', status=100, deleted='false', updated='2026-09-11 08:00:00'):
    db.execute('INSERT INTO ywzt_ods.scm_return_order_header VALUES (?,?,?,?,?,?)', (owner, code, order, status, updated, deleted))


def detail(db, identity='D1', code='R1', owner='A', order='S1', sku='001', quantity=3, status=0, deleted='false', updated='2026-09-11 09:00:00'):
    db.execute('INSERT INTO ywzt_ods.scm_return_order_detail VALUES (?,?,?,?,?,?,?,?,?)', (identity, owner, code, order, sku, quantity, status, updated, deleted))


def read(db, sql, **kwargs):
    return [dict(row) for row in db.execute(sqlglot.transpile(sql, read='doris', write='sqlite')[0])]


def result(db, plan=None):
    plan = plan or ReturnComponentPlan()
    return reconcile_return_components(read(db, compile_return_components(plan)), plan)


def test_all_returns_and_same_sku_lines_aggregate_once(facts):
    header(facts)
    header(facts, code='R2')
    detail(facts, quantity=3)
    detail(facts, identity='D2', quantity=4)
    detail(facts, identity='D3', code='R2', quantity=5)
    checked = result(facts)
    assert len(checked.groups) == 1
    assert checked.groups[0]['returned_qty'] == 12
    assert checked.groups[0]['return_document_count'] == 2
    assert checked.groups[0]['return_detail_count'] == 3
    assert checked.total_quantity == 12
    assert checked.document_count == 2


def test_document_count_is_not_sum_of_sku_document_counts(facts):
    header(facts)
    detail(facts)
    detail(facts, identity='D2', sku='002')
    checked = result(facts)
    assert len(checked.groups) == 2
    assert checked.document_count == 1
    assert sum(row['return_document_count'] for row in checked.groups) == 2


def test_same_codes_in_different_owners_never_merge(facts):
    for owner in ('A', 'B'):
        header(facts, owner=owner)
        detail(facts, owner=owner)
    assert len(result(facts).groups) == 2
    assert result(facts).document_count == 2
    assert len(result(facts, ReturnComponentPlan(owner_codes=('A',))).groups) == 1
    assert len(result(facts).groups) == 2


def test_order_and_sku_scopes_are_exact_and_not_inherited(facts):
    header(facts)
    header(facts, code='R2', order='S2')
    detail(facts)
    detail(facts, identity='D2', code='R2', order='S2', sku='1')
    assert len(result(facts, ReturnComponentPlan(order_codes=('S2',), sku_codes=('1',))).groups) == 1
    assert result(facts, ReturnComponentPlan(order_codes=('S2',), sku_codes=('001',))).groups == []
    assert len(result(facts).groups) == 2


def test_statuses_belong_to_each_table_not_shipment_or_sales(facts):
    header(facts, status=901)
    detail(facts)
    detail(facts, identity='cancelled', quantity=100, status=1)
    assert result(facts).total_quantity == 3
    sql = compile_return_components(ReturnComponentPlan())
    assert 'scm_sales_' not in sql and 'ship_qty' not in sql
    assert 'created_date' not in sql


@pytest.mark.parametrize('side', ['header', 'detail'])
def test_deleted_rows_excluded_on_their_own_table(facts, side):
    header(facts, deleted='true' if side == 'header' else 'false')
    detail(facts, deleted='true' if side == 'detail' else 'false')
    assert result(facts).groups == []


def test_zero_and_signed_return_quantities_are_not_erased(facts):
    header(facts)
    detail(facts, quantity=-3)
    detail(facts, identity='D2', quantity=5)
    detail(facts, identity='D3', sku='ZERO', quantity=0)
    checked = result(facts)
    assert checked.total_quantity == 2
    assert checked.groups[0]['negative_detail_count'] == 1
    assert checked.groups[1]['returned_qty'] == 0
    assert checked.groups[1]['return_detail_count'] == 1


@pytest.mark.parametrize('defect', ['duplicate_header', 'conflicting_header', 'duplicate_detail', 'missing_quantity', 'missing_sku', 'missing_id', 'conflicting_reference', 'invalid_header_delete', 'invalid_detail_delete'])
def test_bad_sources_reject_instead_of_deduplicating_or_losing_returns(facts, defect):
    header(facts, deleted='invalid' if defect == 'invalid_header_delete' else 'false')
    detail(facts, identity='' if defect == 'missing_id' else 'D1',
           sku='' if defect == 'missing_sku' else '001',
           quantity=None if defect == 'missing_quantity' else 3,
           order='OTHER' if defect == 'conflicting_reference' else 'S1',
           deleted='invalid' if defect == 'invalid_detail_delete' else 'false')
    if defect == 'duplicate_header': header(facts)
    if defect == 'conflicting_header': header(facts, order='OTHER')
    if defect == 'duplicate_detail': detail(facts)
    with pytest.raises(SemanticRuntimeError) as failure:
        result(facts)
    assert failure.value.reason == 'return_component_quality_failed'


@pytest.mark.parametrize('reference', [None, '', '   '])
def test_missing_detail_reference_cannot_prove_agreement(facts, reference):
    header(facts)
    detail(facts, order=reference)
    with pytest.raises(SemanticRuntimeError) as failure:
        result(facts)
    assert failure.value.reason == 'return_component_quality_failed'


@pytest.mark.parametrize('plan', [
    ReturnComponentPlan(order_codes=('S1',)),
    ReturnComponentPlan(order_keys=(('A', 'S1'),)),
])
@pytest.mark.parametrize('header_order,detail_order', [('OTHER', 'S1'), ('S1', 'OTHER'), (None, 'S1')])
def test_either_reference_in_scope_keeps_conflict_for_rejection(facts, plan, header_order, detail_order):
    header(facts, order=header_order)
    detail(facts, order=detail_order)
    with pytest.raises(SemanticRuntimeError) as failure:
        result(facts, plan)
    assert failure.value.reason == 'return_component_quality_failed'


@pytest.mark.parametrize('excluded', ['owner', 'sku', 'status', 'detail_deleted', 'header_deleted', 'empty_cohort'])
def test_reverse_reference_check_respects_established_exclusions(facts, excluded):
    header(facts, order='OTHER', deleted='true' if excluded == 'header_deleted' else 'false')
    detail(facts, order='S1', status=1 if excluded == 'status' else 0,
           deleted='true' if excluded == 'detail_deleted' else 'false')
    plan = ReturnComponentPlan(order_codes=('S1',),
        owner_codes=('B',) if excluded == 'owner' else (),
        sku_codes=('OTHER_SKU',) if excluded == 'sku' else (),
        order_keys=() if excluded == 'empty_cohort' else None)
    assert result(facts, plan).groups == []


def test_unrelated_owner_quality_does_not_poison_scope(facts):
    header(facts)
    detail(facts)
    header(facts, owner='B', deleted='invalid')
    assert result(facts, ReturnComponentPlan(owner_codes=('A',))).total_quantity == 3


@pytest.mark.parametrize('field', ['returned_qty', 'return_detail_count', 'return_document_count', 'total_returned_qty', 'total_document_count', 'group_count'])
def test_corruption_does_not_pass_reconciliation(facts, field):
    header(facts)
    detail(facts)
    rows = read(facts, compile_return_components(ReturnComponentPlan()))
    group = next(row for row in rows if row['row_kind'] == 'group')
    group[field] += 1
    with pytest.raises(SemanticRuntimeError): reconcile_return_components(rows, ReturnComponentPlan())


def test_full_paging_and_truncation_are_distinct_from_empty(facts):
    assert result(facts).groups == []
    header(facts)
    for n in range(7): detail(facts, identity=str(n), sku=str(n))
    checked = retrieve_return_components(ReturnComponentPlan(), lambda sql, **kw: read(facts, sql), page_size=2)
    assert len(checked.groups) == 7
    with pytest.raises(SemanticRuntimeError) as failure:
        retrieve_return_components(ReturnComponentPlan(), lambda sql, **kw: read(facts, sql), max_rows=4)
    assert failure.value.reason == 'result_truncated'


@pytest.mark.parametrize('scope', [{'owner_codes': 'A'}, {'order_codes': ('',)}, {'sku_codes': (' 001',)}])
def test_invalid_scope_cannot_become_all(scope):
    with pytest.raises(ValueError): ReturnComponentPlan(**scope)


@pytest.mark.parametrize('order_codes', [(), ('S1',)])
def test_missing_header_cannot_silently_drop_eligible_detail(facts, order_codes):
    detail(facts)
    with pytest.raises(SemanticRuntimeError) as failure:
        result(facts, ReturnComponentPlan(order_codes=order_codes))
    assert failure.value.reason == 'return_component_quality_failed'


def test_orphan_scope_uses_detail_reference_only_for_diagnosis(facts):
    detail(facts, owner='B', order='OTHER')
    assert result(facts, ReturnComponentPlan(owner_codes=('A',))).groups == []
    assert result(facts, ReturnComponentPlan(order_codes=('S1',))).groups == []
    with pytest.raises(SemanticRuntimeError): result(facts)


def test_header_existence_is_owner_qualified(facts):
    header(facts, owner='A', deleted='true')
    detail(facts, owner='B')
    with pytest.raises(SemanticRuntimeError): result(facts)


def test_missing_orphan_reference_is_not_invented(facts):
    detail(facts, order=None)
    with pytest.raises(SemanticRuntimeError): result(facts)
    assert result(facts, ReturnComponentPlan(order_codes=('S1',))).groups == []


def test_quality_failure_stops_paging_before_remaining_rows(facts):
    header(facts)
    for n in range(5): detail(facts, identity=str(n), sku=str(n))
    detail(facts, identity='0', sku='0')
    calls = []
    def query(sql, **kwargs):
        calls.append(sql)
        return read(facts, sql)
    with pytest.raises(SemanticRuntimeError) as failure:
        retrieve_return_components(ReturnComponentPlan(), query, page_size=1)
    assert failure.value.reason == 'return_component_quality_failed'
    assert len(calls) == 1


@pytest.mark.parametrize('change', ['missing', 'changed', 'ordinal'])
def test_page_failures_remain_distinct_from_source_anomalies(facts, change):
    header(facts)
    detail(facts)
    calls = 0
    def query(sql, **kwargs):
        nonlocal calls
        calls += 1
        rows = read(facts, sql)
        if calls == 2:
            if change == 'missing': return []
            if change == 'changed': rows[0]['returned_qty'] += 1
            if change == 'ordinal': rows[0]['result_ordinal'] = 1
        return rows
    with pytest.raises(SemanticRuntimeError) as failure:
        retrieve_return_components(ReturnComponentPlan(), query, page_size=1)
    assert failure.value.reason == 'return_component_page_invalid'
    assert failure.value.page_failure == {'missing': 'page_length', 'changed': 'content_changed', 'ordinal': 'ordinal_changed'}[change]


def test_complete_large_integer_quantity_does_not_use_caller_precision(facts):
    header(facts)
    detail(facts)
    rows = read(facts, compile_return_components(ReturnComponentPlan()))
    exact = Decimal('10000000000000000000000000001')
    for row in rows:
        row['total_returned_qty'] = str(exact)
        if row['row_kind'] == 'group': row['returned_qty'] = str(exact)
    with localcontext() as context:
        context.prec = 6
        checked = reconcile_return_components(rows, ReturnComponentPlan())
        assert checked.total_quantity == exact and context.prec == 6


def test_quality_diagnostic_is_separate_from_business_quantities(facts):
    from app.agent_core.return_components import compile_return_quality
    header(facts)
    header(facts)
    detail(facts)
    detail(facts)
    detail(facts, identity='BAD', quantity=None, order='OTHER')
    detail(facts, identity='ORPHAN', code='MISSING')
    rows = read(facts, compile_return_quality(ReturnComponentPlan()))
    assert rows == [dict(invalid_header_count=1, invalid_detail_count=1,
                         duplicate_detail_key_count=1, missing_header_count=1, reference_conflict_count=1)]
    assert not any(key in rows[0] for key in ('returned_qty', 'total_returned_qty', 'sku_code'))


def test_exact_order_cohort_is_not_owner_order_cartesian_product(facts):
    for owner in ('A', 'B'):
        for order in ('S1', 'S2'):
            header(facts, owner=owner, order=order, code=order)
            detail(facts, owner=owner, order=order, code=order, identity=order)
    selected = ReturnComponentPlan(order_keys=(('A', 'S1'), ('B', 'S2')))
    assert {(row['cargo_owner'], row['sales_order_code']) for row in result(facts, selected).groups} == {('A', 'S1'), ('B', 'S2')}
    assert result(facts, ReturnComponentPlan(order_keys=())).groups == []


@pytest.mark.parametrize('keys', [('A',), (('A', ''),), (('A', 'S1'), ('A', 'S1')), [('A', 'S1')]])
def test_invalid_exact_order_cohort_rejected(keys):
    with pytest.raises(ValueError): ReturnComponentPlan(order_keys=keys)


def test_quality_breakdown_preserves_distinct_causes_without_business_rows(facts):
    from app.agent_core.return_components import compile_return_quality_details
    header(facts)
    header(facts)
    header(facts, code='BAD', deleted=None)
    header(facts, code='NO_REF', order=None)
    detail(facts, quantity=None, sku=None, deleted='?')
    detail(facts, identity='CONFLICT', order='OTHER')
    detail(facts, identity='ORPHAN', code='MISSING', order=None)
    rows = read(facts, compile_return_quality_details(ReturnComponentPlan()))
    assert rows == [dict(header_missing_identity=1, header_invalid_delete=1, header_multiple_active=1,
                         detail_missing_identity=1, detail_missing_quantity=1, detail_invalid_delete=1,
                         orphan_details=1, orphan_missing_reference=1, conflicting_detail_reference=1)]


def test_quality_breakdown_respects_exact_cohort_and_empty_scope(facts):
    from app.agent_core.return_components import compile_return_quality_details
    header(facts, owner='B', order='S2', deleted=None)
    detail(facts, owner='B', order='S2', code='ORPHAN')
    for plan in (ReturnComponentPlan(order_keys=(('A', 'S1'),)), ReturnComponentPlan(order_keys=())):
        assert not any(read(facts, compile_return_quality_details(plan))[0].values())


def test_synthetic_strict_agreement_cases_contain_no_business_sources(facts):
    from pathlib import Path
    import runpy
    from sqlglot import exp
    fixture = runpy.run_path(str(Path(__file__).resolve().parents[2] /
                                'tests/acceptance/check_return_agreement_sql_live.py'))
    for name, sql, must_reject in fixture['cases']():
        tree = sqlglot.parse_one(sql, read='doris')
        assert not any(table.db or table.catalog for table in tree.find_all(exp.Table)), name
        for cast in list(tree.find_all(exp.Cast)):
            if cast.to.is_type('DATETIME', 'TIMESTAMP') and isinstance(cast.this, exp.Literal):
                cast.replace(cast.this.copy())
        rows = read(facts, tree.sql(dialect='doris'))
        assert len(rows) == 1, name
        assert bool(rows[0]['reference_conflict_count']) == must_reject, name

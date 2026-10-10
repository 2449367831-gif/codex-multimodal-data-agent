from copy import deepcopy
from pathlib import Path
import runpy

import pytest

from app.agent_core.return_components import ReturnComponentPlan
from app.agent_core.semantic_types import SemanticRuntimeError
from app.tests.test_return_components import facts, header, detail, read, result


@pytest.fixture(scope='module')
def reference():
    return runpy.run_path(str(Path(__file__).resolve().parents[2] / 'tests/acceptance/check_return_components_live.py'))


def expected(reference, facts, plan=None):
    plan = plan or ReturnComponentPlan()
    return reference['expected_result'](read(facts, reference['reference_sql'](plan, 'header')),
                                        read(facts, reference['reference_sql'](plan, 'detail')), plan)


def test_independent_reference_all_documents_owners_skus_and_missing_times(facts, reference):
    for owner in ('A', 'B'):
        header(facts, owner=owner)
        header(facts, owner=owner, code='R2')
        detail(facts, owner=owner)
        detail(facts, owner=owner, identity='D2', quantity=-1)
        detail(facts, owner=owner, identity='D3', code='R2', quantity=6)
        detail(facts, owner=owner, identity='D4', sku='OTHER', quantity=0, updated=None)
    wanted = expected(reference, facts)
    assert wanted['total_quantity'] == 16
    assert wanted['document_count'] == 4
    assert wanted['detail_count'] == 8
    assert all(reference['compare'](result(facts), wanted).values())
    checked = reference['verify'](ReturnComponentPlan(), lambda sql, **kw: read(facts, sql), page_size=2)
    assert all(checked.values())


@pytest.mark.parametrize('defect', ['duplicate_header', 'conflicting_header', 'duplicate_line', 'missing_header', 'missing_quantity', 'missing_key', 'reference_conflict', 'bad_header_delete', 'bad_detail_delete'])
def test_reference_and_runtime_independently_reject_source_defects(facts, reference, defect):
    if defect != 'missing_header': header(facts, deleted='' if defect == 'bad_header_delete' else 'false')
    detail(facts, quantity=None if defect == 'missing_quantity' else 3,
           sku=None if defect == 'missing_key' else '001',
           order='OTHER' if defect == 'reference_conflict' else 'S1',
           deleted=None if defect == 'bad_detail_delete' else 'false')
    if defect == 'duplicate_header': header(facts)
    if defect == 'conflicting_header': header(facts, order='OTHER')
    if defect == 'duplicate_line': detail(facts)
    with pytest.raises(ValueError): expected(reference, facts)
    with pytest.raises(SemanticRuntimeError): result(facts)


@pytest.mark.parametrize('field', ['returned_qty', 'return_document_count', 'return_detail_count', 'negative_detail_count', 'missing_update_count', 'latest_update', 'cargo_owner', 'sales_order_code', 'sku_code'])
def test_reference_detects_field_corruption(facts, reference, field):
    header(facts)
    detail(facts)
    wanted = expected(reference, facts)
    corrupted = deepcopy(wanted)
    corrupted['groups'][0][field] = 'changed'
    assert not reference['compare'](result(facts), corrupted)['groups']


def test_scope_and_deleted_headers_do_not_become_orphan_errors(facts, reference):
    header(facts)
    detail(facts)
    header(facts, owner='B', deleted='true')
    detail(facts, owner='B')
    detail(facts, owner='OTHER', code='ORPHAN', order='OTHER')
    for plan in (ReturnComponentPlan(owner_codes=('A',)), ReturnComponentPlan(order_codes=('S1',))):
        assert all(reference['compare'](result(facts, plan), expected(reference, facts, plan)).values())


def test_reference_observes_change_without_selecting_a_new_scope(facts, reference):
    header(facts)
    detail(facts)
    changed = False
    def query(sql, **kwargs):
        nonlocal changed
        if 'document_skus' in sql and not changed:
            changed = True
            facts.execute('UPDATE ywzt_ods.scm_return_order_detail SET fulfill_qty=8')
        return read(facts, sql)
    checks = reference['verify'](ReturnComponentPlan(), query, page_size=2)
    assert changed
    assert not checks['reference_stable']
    assert not checks['total_quantity']
    assert not checks['groups']


def test_reference_does_not_depend_on_runtime_compiler_for_expected_values(facts, reference, monkeypatch):
    import app.agent_core.return_components as runtime
    header(facts)
    detail(facts)
    def forbidden(*args, **kwargs):
        raise AssertionError('runtime compiler used by independent reference')
    monkeypatch.setattr(runtime, 'compile_return_components', forbidden)
    assert expected(reference, facts)['total_quantity'] == 3


@pytest.mark.parametrize('header_order,detail_order', [
    ('OTHER', 'S1'), ('S1', 'OTHER'), (None, 'S1'), ('S1', None), ('S1', ''), ('S1', '   '),
])
@pytest.mark.parametrize('plan', [ReturnComponentPlan(), ReturnComponentPlan(order_codes=('S1',)),
                                 ReturnComponentPlan(order_keys=(('A', 'S1'),))])
def test_strict_agreement_independent_reference(facts, reference, header_order, detail_order, plan):
    header(facts, order=header_order)
    detail(facts, order=detail_order)
    with pytest.raises(ValueError):
        expected(reference, facts, plan)
    with pytest.raises(SemanticRuntimeError):
        result(facts, plan)


@pytest.mark.parametrize('defect,flag', [
    ('missing_header', 'orphan_details'),
    ('missing_header_order', 'header_missing_identity'),
    ('duplicate_header', 'header_multiple_active'),
    ('bad_header_delete', 'header_invalid_delete'),
    ('missing_key', 'detail_missing_identity'),
    ('missing_quantity', 'detail_missing_quantity'),
    ('bad_detail_delete', 'detail_invalid_delete'),
    ('duplicate_line', 'detail_duplicate_identity'),
    ('reference_conflict', 'conflicting_detail_reference'),
])
def test_independent_quality_preflight_before_large_raw_scan(facts, reference, defect, flag):
    if defect != 'missing_header':
        header(facts, order=None if defect == 'missing_header_order' else 'S1',
               deleted=None if defect == 'bad_header_delete' else 'false')
    detail(facts, quantity=None if defect == 'missing_quantity' else 3,
           sku=None if defect == 'missing_key' else '001',
           deleted=None if defect == 'bad_detail_delete' else 'false',
           order='OTHER' if defect == 'reference_conflict' else 'S1')
    if defect == 'duplicate_header': header(facts)
    if defect == 'duplicate_line': detail(facts)
    calls = []
    def query(sql, **kwargs):
        calls.append(sql)
        return read(facts, sql)
    with pytest.raises(reference['ReferenceQualityError']) as failure:
        reference['verify'](ReturnComponentPlan(), query, max_rows=1)
    assert flag in failure.value.quality_flags
    assert len(calls) == 1
    assert 'reference_return_quality' in calls[0]
    checks = reference['verify_quality_rejection'](ReturnComponentPlan(), query, page_size=1)
    assert all(checks.values())


@pytest.mark.parametrize('header_order,detail_order', [
    ('OTHER', 'S1'), ('S1', 'OTHER'), (None, 'S1'), ('S1', None), ('S1', '   '),
])
@pytest.mark.parametrize('plan', [ReturnComponentPlan(), ReturnComponentPlan(order_codes=('S1',)),
                                 ReturnComponentPlan(order_keys=(('A', 'S1'),))])
def test_quality_preflight_keeps_both_reference_directions(facts, reference, header_order, detail_order, plan):
    header(facts, order=header_order)
    detail(facts, order=detail_order)
    flags = reference['read_return_quality'](plan, lambda sql, **kw: read(facts, sql))
    assert flags['conflicting_detail_reference']


@pytest.mark.parametrize('excluded', ['owner', 'sku', 'status', 'detail_deleted', 'header_deleted', 'empty_cohort'])
def test_quality_preflight_preserves_exclusions(facts, reference, excluded):
    header(facts, order='OTHER', deleted='true' if excluded == 'header_deleted' else 'false')
    detail(facts, order='S1', status=1 if excluded == 'status' else 0,
           deleted='true' if excluded == 'detail_deleted' else 'false')
    plan = ReturnComponentPlan(order_codes=('S1',),
        owner_codes=('B',) if excluded == 'owner' else (),
        sku_codes=('OTHER_SKU',) if excluded == 'sku' else (),
        order_keys=() if excluded == 'empty_cohort' else None)
    flags = reference['read_return_quality'](plan, lambda sql, **kw: read(facts, sql))
    assert not any(flags.values())


def test_quality_reference_is_independent_of_runtime(facts, reference, monkeypatch):
    import app.agent_core.return_components as runtime
    header(facts)
    detail(facts, order='OTHER')
    def forbidden(*args, **kwargs):
        raise AssertionError('production quality compiler used by independent reference')
    monkeypatch.setattr(runtime, 'compile_return_components', forbidden)
    monkeypatch.setattr(runtime, 'compile_return_quality', forbidden)
    flags = reference['read_return_quality'](ReturnComponentPlan(), lambda sql, **kw: read(facts, sql))
    assert flags['conflicting_detail_reference']


@pytest.mark.parametrize('value', [None, True, 'invalid', -1, 2, 'NaN', 'Infinity', '0.5'])
def test_quality_preflight_does_not_accept_invalid_flags(reference, value):
    flags = dict.fromkeys(reference['RETURN_QUALITY_FLAGS'], 0)
    flags['orphan_details'] = value
    with pytest.raises(ValueError):
        reference['read_return_quality'](ReturnComponentPlan(), lambda *a, **kw: [flags])


def test_quality_only_does_not_claim_clean_data_as_rejection(facts, reference):
    header(facts)
    detail(facts)
    with pytest.raises(ValueError, match='requires independent source defects'):
        reference['verify_quality_rejection'](ReturnComponentPlan(), lambda sql, **kw: read(facts, sql))


def test_quality_verifier_does_not_accept_timeout_as_quality_rejection(facts, reference):
    header(facts)
    detail(facts, order='OTHER')
    def query(sql, **kwargs):
        if 'reference_return_quality' not in sql:
            raise SemanticRuntimeError('timed out', reason='query_timeout')
        return read(facts, sql)
    with pytest.raises(SemanticRuntimeError) as failure:
        reference['verify_quality_rejection'](ReturnComponentPlan(), query)
    assert failure.value.reason == 'query_timeout'


def test_quality_rejection_requires_stable_independent_evidence(facts, reference):
    header(facts)
    detail(facts, order='OTHER')
    def query(sql, **kwargs):
        rows = read(facts, sql)
        if 'reference_return_quality' not in sql:
            facts.execute("UPDATE ywzt_ods.scm_return_order_detail SET ref_order_code='S1'")
        return rows
    checks = reference['verify_quality_rejection'](ReturnComponentPlan(), query)
    assert checks['runtime_quality_rejected']
    assert not checks['reference_stable']

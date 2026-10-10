from datetime import datetime, timezone
from pathlib import Path
import runpy

import pytest

from app.agent_core.order_components import OrderComponentPlan
from app.agent_core.semantic_types import SemanticRuntimeError
from app.sql import SqlSafetyValidator
from app.tests.test_order_components import facts, header, detail, read


REFERENCE = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'tests/acceptance/check_order_returns_live.py'))


def quality(db, plan=None):
    return REFERENCE['read_order_quality'](plan or OrderComponentPlan(), lambda sql, **kw: read(db, sql))


@pytest.mark.parametrize('defect,flag', [
    ('missing_owner','header_missing_identity'), ('missing_order','header_missing_identity'),
    ('header_delete','header_invalid_delete'), ('missing_created','header_missing_created'),
    ('duplicate_header','header_multiple_active'), ('missing_id','detail_missing_identity'),
    ('missing_sku','detail_missing_identity'), ('detail_delete','detail_invalid_delete'),
    ('missing_quantity','detail_missing_quantity'), ('duplicate_detail','detail_duplicate_identity'),
])
def test_independent_preflight_names_source_defects_without_rows(facts, defect, flag):
    header(facts, owner=None if defect=='missing_owner' else 'A', order=None if defect=='missing_order' else 'S1',
           deleted=None if defect=='header_delete' else 'false', created=None if defect=='missing_created' else '2026-09-01 00:00:00')
    detail(facts, identity=None if defect=='missing_id' else 'D1', sku=None if defect=='missing_sku' else '001',
           deleted='?' if defect=='detail_delete' else 'false', quantity=None if defect=='missing_quantity' else 10)
    if defect=='duplicate_header': header(facts, created='2020-01-01 00:00:00')
    if defect=='duplicate_detail': detail(facts)
    flags=quality(facts)
    assert flags[flag] is True
    assert all(type(value) is bool for value in flags.values())
    assert set(flags)==set(REFERENCE['ORDER_QUALITY_FLAGS'])


def test_clean_preflight_is_not_numerical_acceptance_and_keeps_raw_cap(facts):
    header(facts)
    detail(facts, quantity=-1, payment=None)
    assert not any(quality(facts).values())
    calls=[]
    def query(sql, **kw):
        calls.append(sql)
        if 'reference_order_quality' not in sql:
            raise SemanticRuntimeError('private query detail',reason='result_truncated')
        return read(facts,sql)
    with pytest.raises(REFERENCE['VerificationFailure']) as failure:
        REFERENCE['reference_snapshot'](OrderComponentPlan(),query)
    assert failure.value.stage=='reference.sales_header'
    assert failure.value.reason=='result_truncated'
    assert len(calls)==2
    assert 'private' not in str(failure.value)


def test_preflight_stops_before_raw_or_runtime_queries(facts):
    header(facts)
    detail(facts, quantity=None)
    calls=[]
    def query(sql, **kw):
        calls.append(sql)
        assert 'reference_order_quality' in sql
        return read(facts,sql)
    with pytest.raises(REFERENCE['VerificationFailure']) as failure:
        REFERENCE['verify'](OrderComponentPlan(),query)
    error=failure.value
    assert error.stage=='reference_before.sales_preflight'
    assert error.reason=='order_reference_quality_failed'
    assert error.quality_flags==('detail_missing_quantity',)
    assert len(calls)==1


def test_scoped_preflight_keeps_duplicates_outside_window_but_not_other_owners(facts):
    header(facts)
    header(facts, created='2020-01-01 00:00:00')
    header(facts, owner='B', deleted='?')
    detail(facts, owner='B', quantity=None)
    plan=OrderComponentPlan(owner_codes=('A',),start=datetime(2026,8,31,16,tzinfo=timezone.utc),
                           end=datetime(2026,9,30,16,tzinfo=timezone.utc))
    flags=quality(facts,plan)
    assert flags['header_multiple_active']
    assert not flags['header_invalid_delete'] and not flags['detail_missing_quantity']
    assert not any(quality(facts,OrderComponentPlan(owner_codes=('NONE',))).values())


@pytest.mark.parametrize('bad',[[],[{}],[{'bad':1}],None,'private payload'])
def test_malformed_control_response_fails_closed(bad):
    with pytest.raises(ValueError): REFERENCE['read_order_quality'](OrderComponentPlan(),lambda *a,**kw:bad)


@pytest.mark.parametrize('bad',[True,False,-1,2,None,'NaN','private',0.5])
def test_control_flags_are_strict_zero_or_one(bad):
    row={key:0 for key in REFERENCE['ORDER_QUALITY_FLAGS']}
    row['header_missing_identity']=bad
    with pytest.raises(ValueError): REFERENCE['read_order_quality'](OrderComponentPlan(),lambda *a,**kw:[row])


def test_quality_compiler_is_readonly_without_runtime_compilers(monkeypatch):
    import app.agent_core.order_components as production
    def forbidden(*args,**kwargs): raise AssertionError('not independent')
    monkeypatch.setattr(production,'compile_order_components',forbidden)
    sql=REFERENCE['reference_quality_sql'](OrderComponentPlan())
    SqlSafetyValidator().validate(sql,allowed_sources=production.SOURCES)
    assert 'LIMIT 1' in sql

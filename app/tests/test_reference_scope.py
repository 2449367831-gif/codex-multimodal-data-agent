from datetime import datetime
from pathlib import Path
import random
import runpy
from zoneinfo import ZoneInfo

import pytest

from app.agent_core.order_components import OrderComponentPlan
from app.agent_core.return_components import ReturnComponentPlan
from app.tests.test_order_components import facts as orders, header, detail, read
from app.tests.test_return_components import facts as returns, header as rh, detail as rd


ROOT = Path(__file__).resolve().parents[2]
ORDER = runpy.run_path(str(ROOT / 'tests/acceptance/check_order_returns_live.py'))
RETURN = ORDER['RETURN_REFERENCE']


def raw(reference, db, plan, source):
    return read(db, reference['reference_sql'](plan, source))


def test_order_dates_bound_raw_reads_but_keep_conflicting_versions(orders):
    header(orders)
    header(orders, created='2020-01-01 00:00:00')
    header(orders, order='OLD', created='2020-01-01 00:00:00')
    header(orders, order='END', created='2026-10-01 00:00:00')
    header(orders, order='MISSING_DATE', created=None)
    detail(orders)
    detail(orders, order='OLD', identity='OLD')
    detail(orders, order='END', identity='END')
    plan = OrderComponentPlan(start=datetime(2026, 9, 1, tzinfo=ZoneInfo('Asia/Shanghai')),
                              end=datetime(2026, 10, 1, tzinfo=ZoneInfo('Asia/Shanghai')))
    headers, details = (raw(ORDER, orders, plan, source) for source in ('header', 'detail'))
    assert sorted(r['sales_order_code'] for r in headers) == ['MISSING_DATE', 'S1', 'S1']
    assert [r['id'] for r in details] == ['D1']
    with pytest.raises(ValueError): ORDER['expected_orders'](headers, details, plan)


def test_raw_order_exclusions_match_python_reference_without_hiding_invalid_flags(orders):
    header(orders)
    header(orders, order='DELETED', deleted='true')
    detail(orders)
    detail(orders, identity='CANCELLED', status=1)
    detail(orders, identity='DELETED', deleted='true')
    detail(orders, identity='BAD_FLAG', deleted=None)
    detail(orders, identity='ORPHAN', order='UNMATCHED')
    plan = OrderComponentPlan()
    assert len(raw(ORDER, orders, plan, 'header')) == 1
    assert {r['id'] for r in raw(ORDER, orders, plan, 'detail')} == {'D1', 'BAD_FLAG'}


def test_return_scope_keeps_bad_reference_and_other_header_versions(returns):
    rh(returns, code='R1', order='S1')
    rh(returns, code='R1', order='OUTSIDE')
    rh(returns, code='OTHER', order='OUTSIDE')
    rd(returns, code='R1', order='OUTSIDE', identity='CONFLICT')
    rd(returns, code='OTHER', order='OUTSIDE', identity='OTHER')
    plan = ReturnComponentPlan(order_keys=(('A', 'S1'),))
    assert len(raw(RETURN, returns, plan, 'header')) == 2
    assert [r['id'] for r in raw(RETURN, returns, plan, 'detail')] == ['CONFLICT']


def test_return_scope_preserves_known_excluded_header_and_true_orphans(returns):
    rh(returns, code='DELETED', order='OTHER', deleted='true')
    rd(returns, code='DELETED', order='S1', identity='EXCLUDED')
    rd(returns, code='MISSING', order='S1', identity='ORPHAN')
    rh(returns, code='UNRELATED', order='OTHER')
    rd(returns, code='UNRELATED', order='OTHER', identity='OUTSIDE')
    plan = ReturnComponentPlan(order_keys=(('A', 'S1'),))
    headers, details = (raw(RETURN, returns, plan, source) for source in ('header', 'detail'))
    assert [r['return_order_code'] for r in headers] == ['DELETED']
    assert {r['id'] for r in details} == {'EXCLUDED', 'ORPHAN'}
    with pytest.raises(ValueError, match='orphan'): RETURN['expected_result'](headers, details, plan)


def test_exact_return_pairs_are_not_cartesian_and_empty_means_none(returns):
    for owner in ('A', 'B'):
        for order in ('S1', 'S2'):
            rh(returns, code=order, owner=owner, order=order)
            rd(returns, code=order, owner=owner, order=order, identity=order)
    plan = ReturnComponentPlan(order_keys=(('A', 'S1'), ('B', 'S2')))
    for source in ('header', 'detail'):
        rows = raw(RETURN, returns, plan, source)
        assert {(r['company_code'], r['ref_order_code']) for r in rows} == set(plan.order_keys)
        assert not raw(RETURN, returns, ReturnComponentPlan(order_keys=()), source)


def test_return_scope_keeps_header_conflicts_even_when_sku_has_no_details(returns):
    rh(returns, order='S1')
    rh(returns, order='OTHER')
    rd(returns, sku='UNSELECTED')
    rh(returns, code='UNRELATED', order='OTHER')
    plan = ReturnComponentPlan(order_codes=('S1',), sku_codes=('001',))
    headers = raw(RETURN, returns, plan, 'header')
    assert len(headers) == 2
    assert not raw(RETURN, returns, plan, 'detail')
    with pytest.raises(ValueError): RETURN['expected_result'](headers, [], plan)


def test_raw_scoped_reference_still_matches_unfiltered_python_selection(returns):
    rh(returns)
    rd(returns)
    rh(returns, code='OTHER', order='OTHER')
    rd(returns, code='OTHER', order='OTHER', identity='OTHER')
    rd(returns, identity='CANCELLED', status=1)
    plan = ReturnComponentPlan(order_codes=('S1',))
    all_rows = [raw(RETURN, returns, ReturnComponentPlan(), source) for source in ('header', 'detail')]
    scoped_rows = [raw(RETURN, returns, plan, source) for source in ('header', 'detail')]
    assert RETURN['expected_result'](*scoped_rows, plan) == RETURN['expected_result'](*all_rows, plan)
    assert len(scoped_rows[0]) < len(all_rows[0])


@pytest.mark.parametrize('seed', range(12))
def test_return_scope_equivalent_to_unfiltered_selection_across_source_anomalies(returns, seed):
    rng = random.Random(seed)
    for index in range(14):
        owner, code, order = rng.choice(('A','B',None,' A ')), rng.choice(('R1','R2','R3',None)), rng.choice(('S1','S2',None,' S1 '))
        if rng.choice((True,False)):
            rh(returns,owner=owner,code=code,order=order,deleted=rng.choice(('true','false',None)))
        rd(returns,owner=owner,code=code,order=order,identity=str(index),sku=rng.choice(('001','002',None)),
           quantity=rng.choice((-1,0,2,None)),status=rng.choice((0,1)),deleted=rng.choice(('true','false',None)))
    all_rows = [[dict(row) for row in returns.execute('SELECT * FROM ywzt_ods.scm_return_order_' + source)]
                for source in ('header','detail')]
    plans = [ReturnComponentPlan(),ReturnComponentPlan(order_keys=()),ReturnComponentPlan(order_codes=('S1',)),
             ReturnComponentPlan(order_keys=(('A','S1'),('B','S2'))),
             ReturnComponentPlan(owner_codes=('A',),order_codes=('S1',),sku_codes=('001',))]
    def evaluate(rows, plan):
        try: return RETURN['expected_result'](*rows, plan)
        except ValueError: return 'source_quality_failure'
    for plan in plans:
        scoped = [raw(RETURN,returns,plan,source) for source in ('header','detail')]
        assert evaluate(scoped,plan)==evaluate(all_rows,plan)


@pytest.mark.parametrize('seed', range(12))
def test_order_scope_equivalent_to_unfiltered_selection_across_source_anomalies(orders, seed):
    rng = random.Random(seed)
    for index in range(12):
        owner, order = rng.choice(('A','B',None,' A ')), rng.choice(('S1','S2',None,' S1 '))
        if rng.choice((True,False)):
            header(orders,owner=owner,order=order,created=rng.choice(('2026-09-01 00:00:00','2026-10-01 00:00:00',None)),
                   deleted=rng.choice(('true','false',None)))
        detail(orders,owner=owner,order=order,identity=str(index),sku=rng.choice(('001','002',None)),
               quantity=rng.choice((-1,0,2,None)),status=rng.choice((0,1)),deleted=rng.choice(('true','false',None)))
    all_rows = [[dict(row) for row in orders.execute('SELECT * FROM ywzt_ods.scm_sales_order_' + source)]
                for source in ('header','detail')]
    plans = [OrderComponentPlan(),OrderComponentPlan(owner_codes=('A',)),
             OrderComponentPlan(order_codes=('S1',),sku_codes=('001',)),
             OrderComponentPlan(start=datetime(2026,9,1,tzinfo=ZoneInfo('Asia/Shanghai')),
                                end=datetime(2026,10,1,tzinfo=ZoneInfo('Asia/Shanghai')))]
    def evaluate(rows, plan):
        try: return ORDER['expected_orders'](*rows, plan)
        except ValueError: return 'source_quality_failure'
    for plan in plans:
        scoped = [raw(ORDER,orders,plan,source) for source in ('header','detail')]
        assert evaluate(scoped,plan)==evaluate(all_rows,plan)


def test_synthetic_doris_cases_are_fixed_and_do_not_reference_business_tables(orders):
    import sqlglot
    from sqlglot import exp
    fixture = runpy.run_path(str(ROOT / 'tests/acceptance/check_reference_scope_sql_live.py'))
    for name, sql, field, expected in fixture['cases']():
        statement = fixture['synthetic_sql'](sql)
        tree = sqlglot.parse_one(statement, read='doris')
        assert not any(table.db for table in tree.find_all(exp.Table)), name
        # SQLite has no DATETIME storage type: casting fixture text yields a year
        # number there. Keep its literal representation only for this local check.
        for cast in list(tree.find_all(exp.Cast)):
            if cast.to.is_type('DATETIME', 'TIMESTAMP') and isinstance(cast.this, exp.Literal):
                cast.replace(cast.this.copy())
        actual = read(orders, tree.sql(dialect='doris'))
        assert sorted(row[field] for row in actual)==expected, name

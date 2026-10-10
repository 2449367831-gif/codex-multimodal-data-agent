from copy import deepcopy
from decimal import Decimal, localcontext

import pytest

from app.agent_core.inventory_components import reconcile_inventory_components, compile_inventory_components
from app.tests.test_inventory_components import facts, stock, occupy, plan, query, result


@pytest.mark.parametrize('field,counter', [
    ('lock', 'negative_lock_rows'),
    ('freeze', 'negative_freeze_rows'),
    ('move', 'negative_move_rows'),
])
def test_source_negative_survives_positive_group_sum(facts, field, counter):
    stock(facts, **{field: '-5' if field == 'lock' else -5})
    stock(facts, **{field: '10' if field == 'lock' else 10})
    checked = result(facts)
    assert checked.groups[0][counter] == 1
    assert checked.totals[counter] == 1
    diagnostic = next(row for row in checked.diagnostics if row['issue'] == counter)
    assert diagnostic['category'] == 'source_data'
    assert diagnostic['affected_source_rows'] == 1
    assert tuple(diagnostic[key] for key in ('cargo_owner_id', 'wms_code', 'sku_code')) == ('A', 'W', '001')


def test_flooring_explained_without_claiming_sql_error(facts):
    stock(facts, quantity=-3, freeze=0)
    checked = result(facts)
    indexed = {row['issue']: row for row in checked.diagnostics}
    assert indexed['negative_stock_rows']['category'] == 'source_data'
    assert indexed['clamped']['category'] == 'calculation_effect'
    assert indexed['available_above_stock']['category'] == 'calculation_effect'
    assert indexed['clamped']['raw_available_ea'] == -3
    assert indexed['clamped']['available_ea'] == 0
    assert indexed['clamped']['in_stock_ea'] == -3
    assert indexed['clamped']['affected_source_rows'] is None


def test_orphan_diagnostics_do_not_invent_stock(facts):
    occupy(facts, reference=None, quantity=-2)
    checked = result(facts)
    indexed = {row['issue']: row for row in checked.diagnostics}
    assert indexed['missing_stock']['available_ea'] is None
    assert indexed['missing_stock']['category'] == 'source_data'
    assert indexed['negative_occupation_rows']['sales_occupation_ea'] == -2
    assert indexed['missing_reference_rows']['affected_source_rows'] == 1
    assert 'clamped' not in indexed
    assert 'available_above_stock' not in indexed


def test_metadata_diagnostics_are_not_etl_or_formula_failures(facts):
    stock(facts, changed=None)
    occupy(facts, created='2026-09-12 10:00:00')
    indexed = {row['issue']: row for row in result(facts).diagnostics}
    assert indexed['missing_stock_time_rows']['category'] == 'source_metadata'
    assert indexed['future_occupation_rows']['category'] == 'source_metadata'


def test_diagnostic_rows_are_complete_independent_projections(facts):
    stock(facts, lock='-5', freeze=-2, move=-1)
    checked = result(facts)
    before = deepcopy(checked.groups)
    diagnostics = checked.diagnostics
    for row in diagnostics:
        assert all(key in row for key in (
            'cargo_owner_id', 'wms_code', 'sku_code', 'in_stock_ea', 'move_in_ea',
            'stock_lock_ea', 'freeze_ea', 'sales_occupation_ea', 'transfer_occupation_ea',
            'deduction_ea', 'raw_available_ea', 'available_ea',
        ))
        assert 'sql' not in row and 'audit' not in row
    diagnostics[0]['in_stock_ea'] = -999
    assert checked.groups == before
    assert checked.diagnostics[0]['in_stock_ea'] == 100


def test_healthy_and_empty_reports_have_no_diagnostics(facts):
    assert result(facts).diagnostics == []
    stock(facts)
    assert result(facts).diagnostics == []


@pytest.mark.parametrize('counter', ['negative_lock_rows', 'negative_freeze_rows', 'negative_move_rows'])
def test_negative_component_counters_reconcile_to_independent_sources(facts, counter):
    stock(facts, lock='-1', freeze=-1, move=-1)
    rows = query(facts, compile_inventory_components(plan()))
    group = next(row for row in rows if row['row_kind'] == 'group')
    group[counter] = 0
    with pytest.raises(ValueError, match='prejoin controls'):
        reconcile_inventory_components(rows, plan())


def large_report(facts):
    stock(facts, freeze=0)
    rows = query(facts, compile_inventory_components(plan()))
    # A SUM of DECIMAL(28,4) values can have more than 28 significant digits.
    exact = Decimal('1000000000000000000000000.0001')
    for row in rows:
        row['total_in_stock_ea'] = str(exact)
        if row['row_kind'] == 'group':
            for field in ('in_stock_ea', 'raw_available_ea', 'available_ea'):
                row[field] = str(exact)
    return rows, exact


@pytest.mark.parametrize('precision', [6, 28])
def test_reconciliation_preserves_sum_precision_independent_of_caller(facts, precision):
    rows, exact = large_report(facts)
    with localcontext() as context:
        context.prec = precision
        checked = reconcile_inventory_components(rows, plan())
        assert checked.totals['in_stock_ea'] == exact
        assert checked.groups[0]['available_ea'] == exact
        assert context.prec == precision


def test_rounded_result_cannot_pass_large_number_reconciliation(facts):
    rows, _ = large_report(facts)
    group = next(row for row in rows if row['row_kind'] == 'group')
    group['available_ea'] = '1000000000000000000000000.0000'
    group['raw_available_ea'] = group['available_ea']
    for row in rows:
        row['total_in_stock_ea'] = group['available_ea']
    with pytest.raises(ValueError):
        reconcile_inventory_components(rows, plan())


def test_reference_independently_checks_all_new_negative_counts(facts):
    import runpy
    from pathlib import Path
    reference = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'tests/acceptance/check_inventory_components_live.py'))
    stock(facts, lock='-2', freeze=-3, move=-4)
    stock(facts, lock='5', freeze=6, move=7)
    expected = reference['expected_groups'](
        query(facts, reference['reference_sql'](plan(), 'stock')),
        query(facts, reference['reference_sql'](plan(), 'occupation')),
    )
    checked = result(facts)
    assert all(reference['compare'](checked, expected).values())
    for field in ('negative_lock_rows', 'negative_freeze_rows', 'negative_move_rows'):
        assert expected[0][field] == 1
        corrupted = deepcopy(expected)
        corrupted[0][field] = 0
        assert not reference['compare'](checked, corrupted)['all_components_and_diagnostics']
        assert not reference['compare'](checked, corrupted)['all_source_totals']


def test_reference_uses_independent_exact_arithmetic(facts):
    import runpy
    from pathlib import Path
    reference = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'tests/acceptance/check_inventory_components_live.py'))
    rows, exact = large_report(facts)
    raw = query(facts, reference['reference_sql'](plan(), 'stock'))
    raw[0]['in_stock_ea'] = str(exact)
    with localcontext() as context:
        context.prec = 6
        expected = reference['expected_groups'](raw, [])
        checked = reconcile_inventory_components(rows, plan())
        assert expected[0]['available_ea'] == exact
        assert all(reference['compare'](checked, expected).values())

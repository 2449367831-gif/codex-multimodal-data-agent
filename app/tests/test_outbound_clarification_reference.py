from datetime import date
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import sqlglot
import pytest

from app.tests.test_sales_aggregation import facts


def test_independent_reference_before_live(facts):
    path = Path(__file__).resolve().parents[2] / 'tests/acceptance/check_outbound_clarification_live.py'
    spec = spec_from_file_location('outbound_reference', path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    sql = module.reference_sql(date(2026, 9, 9), date(2026, 9, 10))
    rows = [dict(row) for row in facts.execute(sqlglot.transpile(sql, read='doris', write='sqlite')[0])]
    assert rows == [{'quantity': 18, 'outbound_count': 4}]
    assert module.compare({'rows': [{'quantity': '18', 'outbound_count': '4'}]}, rows)
    assert not module.compare({'rows': [{'quantity': '18', 'outbound_count': '5'}]}, rows)
    assert module.matches_original_window({'caption': '2026-09-09 00:00 至 2026-09-10 00:00（不含结束时点） · 全部货主'}, date(2026, 9, 10))
    assert not module.matches_original_window({'caption': '2026-09-10 00:00 至 2026-09-11 00:00（不含结束时点）'}, date(2026, 9, 10))
    assert not module.matches_original_window({'caption': None}, date(2026, 9, 10))


@pytest.mark.parametrize('owner,warehouse,quantity,documents', [
    ('HJXH', 'W1', '8', '2'), ('HJXH', 'W2', '0', '1'), ('B', 'W1', '10', '1'),
])
def test_scoped_independent_reference_retains_owner_warehouse_and_zero_quantity_documents(facts, owner, warehouse, quantity, documents):
    path = Path(__file__).resolve().parents[2] / 'tests/acceptance/check_outbound_clarification_live.py'
    spec = spec_from_file_location('scoped_outbound_reference', path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    sql = module.reference_sql(date(2026, 9, 9), date(2026, 9, 10), owner=owner,
                               warehouse_code=warehouse, group_by_warehouse=True)
    rows = [dict(row) for row in facts.execute(sqlglot.transpile(sql, read='doris', write='sqlite')[0])]
    table = {'rows': [{'cargo_owner': owner, 'warehouse_code': warehouse,
                      'quantity': quantity, 'outbound_count': documents}]}
    assert module.compare(table, rows, group_by_warehouse=True)
    for key, wrong in [('cargo_owner', 'OTHER'), ('warehouse_code', 'OTHER'), ('quantity', '999'), ('outbound_count', '999')]:
        changed = {'rows': [{**table['rows'][0], key: wrong}]}
        assert not module.compare(changed, rows, group_by_warehouse=True)
    assert not module.compare({'rows': table['rows'] * 2}, rows, group_by_warehouse=True)


def test_reference_scope_identifiers_are_literals_not_sql(facts):
    path = Path(__file__).resolve().parents[2] / 'tests/acceptance/check_outbound_clarification_live.py'
    spec = spec_from_file_location('quoted_outbound_reference', path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    sql = module.reference_sql(date(2026, 9, 9), date(2026, 9, 10), owner="HJXH' OR 1=1 --",
                               warehouse_code='W1', group_by_warehouse=True)
    rows = list(facts.execute(sqlglot.transpile(sql, read='doris', write='sqlite')[0]))
    assert rows == []

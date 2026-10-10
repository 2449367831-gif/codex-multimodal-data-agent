from dataclasses import replace
from decimal import Decimal, localcontext
from pathlib import Path
import runpy

import pytest
import sqlglot
from sqlglot import exp

from app.agent_core.sales_order_details import SalesOrderDetailPlan, reconcile_sales_order_details
from app.agent_core.semantic_types import SemanticRuntimeError
from app.tests.test_sales_order_details import PLAN, orders, execute


@pytest.mark.parametrize('change', [
    "UPDATE ywzt_ods.scm_sales_order_header SET company_code=NULL WHERE sales_order_code='SO1'",
    "UPDATE ywzt_ods.scm_sales_order_header SET sales_order_code=NULL WHERE sales_order_code='SO1'",
    "UPDATE ywzt_ods.scm_sales_order_header SET created=NULL WHERE sales_order_code='SO1'",
    "UPDATE ywzt_ods.scm_sales_order_header SET deleted_flag=NULL WHERE sales_order_code='SO1'",
    "UPDATE ywzt_ods.scm_sales_order_header SET deleted_flag=7 WHERE sales_order_code='SO1'",
    "UPDATE ywzt_ods.scm_sales_order_header SET _fdl_marked_deleted=NULL WHERE sales_order_code='SO1'",
    "UPDATE ywzt_ods.scm_sales_order_header SET _fdl_marked_deleted='invalid' WHERE sales_order_code='SO1'",
    "UPDATE ywzt_ods.scm_sales_order_detail SET _fdl_marked_deleted=NULL WHERE id='D1'",
    "UPDATE ywzt_ods.scm_sales_order_detail SET _fdl_marked_deleted='invalid' WHERE id='D1'",
])
def test_source_uncertainty_is_not_dropped_or_defaulted_to_valid(orders, change):
    orders.execute(change)
    with pytest.raises(SemanticRuntimeError) as failure:
        reconcile_sales_order_details(execute(orders), PLAN)
    assert failure.value.reason == 'sales_order_quality_failed'


@pytest.mark.parametrize('change', [
    "INSERT INTO ywzt_ods.scm_sales_order_header SELECT 'deleted',company_code,sales_order_code,facility_code,store_code,'Old name',order_type,created,status,1,'false',updated FROM ywzt_ods.scm_sales_order_header WHERE id='H2'",
    "INSERT INTO ywzt_ods.scm_sales_order_detail SELECT id,company_code,sales_order_code,facility_code,sku_code,sku_name,request_qty,status,'true',sales_order_id,updated,quantity_u_m,sales_unit FROM ywzt_ods.scm_sales_order_detail WHERE id='D1'",
    "INSERT INTO ywzt_ods.scm_sales_order_detail SELECT id,company_code,sales_order_code,facility_code,sku_code,sku_name,request_qty,'1','false',sales_order_id,updated,quantity_u_m,sales_unit FROM ywzt_ods.scm_sales_order_detail WHERE id='D1'",
])
def test_known_excluded_versions_do_not_poison_effective_facts(orders, change):
    before = reconcile_sales_order_details(execute(orders), PLAN)
    orders.execute(change)
    assert reconcile_sales_order_details(execute(orders), PLAN) == before


@pytest.mark.parametrize('changed_field,changed_value', [('created', '2026-08-01 00:00:00'), ('order_type', 'JYCK')])
def test_header_conflicts_are_checked_before_type_and_window_filtering(orders, changed_field, changed_value):
    rows = orders.execute("SELECT * FROM ywzt_ods.scm_sales_order_header WHERE id='H2'").fetchall()
    duplicate = dict(rows[0])
    duplicate[changed_field] = changed_value
    orders.execute('INSERT INTO ywzt_ods.scm_sales_order_header VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', tuple(duplicate.values()))
    with pytest.raises(SemanticRuntimeError):
        reconcile_sales_order_details(execute(orders), PLAN)


@pytest.mark.parametrize('fields', [
    {'owner': None}, {'owner': 1}, {'owner': ' A'}, {'owner': 'A\n'},
    {'warehouse_codes': 'W1'}, {'sku_codes': ['S1']}, {'sku_codes': ('S1', 'S1')},
    {'sku_codes': (' S1',)}, {'warehouse_codes': ('W\x00',)},
])
def test_malformed_scope_cannot_execute_a_different_scope(fields):
    with pytest.raises(ValueError):
        replace(PLAN, **fields)


@pytest.mark.parametrize('axis,value', [('cargo_owner', 'B'), ('warehouse_code', 'W2'), ('sku_code', 'S2')])
def test_reconciler_rejects_rows_outside_exact_scope(orders, axis, value):
    plan = replace(PLAN, owner='A', warehouse_codes=('W1',), sku_codes=('S1',))
    rows = execute(orders, plan)
    rows[0][axis] = value
    with pytest.raises(SemanticRuntimeError):
        reconcile_sales_order_details(rows, plan)


def test_quantity_controls_are_independent_of_process_decimal_precision(orders):
    rows = execute(orders, replace(PLAN, owner='A', warehouse_codes=('W1',)))
    for row, value in zip(rows, ['123456789012345678901234567890.123456', '0.000001']):
        row['request_quantity'] = value
        row['total_request_quantity'] = '123456789012345678901234567890.123457'
    with localcontext() as context:
        context.prec = 6
        result = reconcile_sales_order_details(rows, PLAN)
    assert result.total_request_quantity == Decimal('123456789012345678901234567890.123457')


def test_missing_total_is_not_fabricated_as_zero_when_zero_details_exist(orders):
    plan = replace(PLAN, sku_codes=('S3',))
    rows = execute(orders, plan)
    rows[0]['total_request_quantity'] = None
    with pytest.raises(SemanticRuntimeError):
        reconcile_sales_order_details(rows, plan)


def test_doris_compatibility_cases_cannot_read_physical_sources():
    fixture = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'tests/acceptance/check_so07_sql_live.py'))
    cases = list(fixture['cases']())
    assert len(cases) == 14 and len({item[0] for item in cases}) == 14
    for _, plan, rows, _ in cases:
        tree = sqlglot.parse_one(fixture['synthetic_sql'](plan, rows), read='doris')
        assert not any(table.db or table.catalog for table in tree.find_all(exp.Table))
        assert {'fixture_' + name for name in fixture['FIELDS']} <= {cte.alias for cte in tree.find_all(exp.CTE)}

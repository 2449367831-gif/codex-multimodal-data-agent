"""User-approved raw SO07 quantities, without inferred conversion or totals."""

from dataclasses import replace

import pytest

from app.agent_core import sales_order_details
from app.agent_core.semantic_types import SemanticRuntimeError
from app.tests.test_sales_order_details import PLAN, orders
from app.tests.test_sales_order_pages import Query


def answer(orders, plan=PLAN, query=None):
    return sales_order_details.retrieve_sales_order_answer(plan, query or Query(orders))


def test_table_keeps_raw_quantity_and_both_source_codes_without_cross_unit_total(orders):
    orders.execute("UPDATE ywzt_ods.scm_sales_order_detail SET quantity_u_m='010',sales_unit='040' WHERE id='D1'")
    result = answer(orders)
    table = result.business_table
    first = next(row for row in table['rows'] if row['sku_code'] == 'S1')
    assert first['request_quantity'] == '2.5'
    assert first['quantity_unit_reference'] == '010' and first['sales_unit_reference'] == '040'
    assert table['summary'] == [{'label': '订单数', 'value': '3', 'unit': '单'}]
    assert table['table_id'] == 'so07_order_details' and table['complete_reconciled'] is True
    assert {col['key'] for col in table['columns'] if col['kind'] == 'number'} == {'request_quantity'}
    assert '9.5' not in str(result)


def test_missing_detail_is_displayed_as_missing_not_zero_requested_quantity(orders):
    orders.execute("DELETE FROM ywzt_ods.scm_sales_order_detail WHERE sales_order_code='SO2'")
    row = next(row for row in answer(orders).business_table['rows'] if row['sales_order_code'] == 'SO2')
    assert row['request_quantity'] is None and row['quantity_unit_reference'] is None
    assert row['order_detail_state'] == '缺少范围内有效明细'


def test_presentation_does_not_include_sql_controls_or_physical_detail_id(orders):
    table = answer(orders).business_table
    forbidden = {'detail_id', 'group_count', 'result_ordinal', 'source_quality_errors',
                 'total_request_quantity', 'order_variants', 'detail_variants', 'sql'}
    assert not forbidden.intersection(col['key'] for col in table['columns'])
    assert all(not forbidden.intersection(row) for row in table['rows'])
    assert '同步完整性未验证' in ' '.join(table['notes'])
    assert '不换算' in ' '.join(table['notes'])


def test_empty_answer_is_successful_no_data_not_connection_failure(orders):
    result = answer(orders, replace(PLAN, owner='MISSING'))
    assert '查询成功' in str(result) and not result.business_table['rows']
    assert result.business_table['summary'] == [{'label': '订单数', 'value': '0', 'unit': '单'}]


def test_partial_or_invalid_result_never_becomes_a_table(orders):
    def mutate(rows, call):
        if call == 2:
            return []
        return rows
    with pytest.raises(SemanticRuntimeError):
        answer(orders, query=Query(orders, mutate))

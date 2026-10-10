"""Synthetic SO07 presentation and owner-bound paging acceptance."""

from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from app.chat_api import _public_business_table
from app.tests.test_sales_order_details import orders
from app.tests.test_sales_order_reporting import answer
from app.tests.test_result_pages_api import build_app, submit


def test_checked_answer_survives_public_screening(orders):
    orders.execute("UPDATE ywzt_ods.scm_sales_order_detail SET quantity_u_m='010',sales_unit='040'")
    raw = answer(orders).business_table
    table = _public_business_table(raw, live=True)
    assert table is not None
    assert len(table['columns']) == 14
    assert table['rows'][0]['quantity_unit_reference'] == '010'
    assert table['rows'][0]['sales_unit_reference'] == '040'
    assert table['summary'] == raw['summary']
    assert table['notes'] == raw['notes'] and len(table['notes']) <= 3
    assert not any(c.get('unit') for c in table['columns'])


def test_missing_detail_keeps_nulls_and_order(orders):
    orders.execute("DELETE FROM ywzt_ods.scm_sales_order_detail WHERE sales_order_code='SO2'")
    table = _public_business_table(answer(orders).business_table, live=True)
    assert table is not None
    row = next(r for r in table['rows'] if r['sales_order_code'] == 'SO2')
    assert row['sku_code'] is None and row['sku_name'] is None
    assert row['request_quantity'] is None
    assert row['quantity_unit_reference'] is None and row['sales_unit_reference'] is None


def test_mixed_source_codes_keep_raw_quantities_without_quantity_summary(orders):
    orders.execute("UPDATE ywzt_ods.scm_sales_order_detail SET quantity_u_m='010',sales_unit='040' WHERE id='D1'")
    orders.execute("UPDATE ywzt_ods.scm_sales_order_detail SET quantity_u_m='020',sales_unit=NULL WHERE id<>'D1'")
    raw = answer(orders).business_table
    table = _public_business_table(raw, live=True)
    assert table is not None
    assert table['summary'] == [{'label': '订单数', 'value': '3', 'unit': '单'}]
    assert {row['quantity_unit_reference'] for row in table['rows']} == {'010', '020'}
    assert {row['sales_unit_reference'] for row in table['rows']} == {'040', None}
    assert [row['request_quantity'] for row in table['rows']] == [
        row['request_quantity'] for row in raw['rows']
    ]
    assert not any(column.get('unit') for column in table['columns'])
    assert all('total_request_quantity' not in row for row in table['rows'])


def test_public_decimal_is_exact_and_private_controls_are_removed(orders):
    raw = answer(orders).business_table
    raw['rows'][0].update(request_quantity='123456789012345678901234567890.000001',
                          detail_id='PRIVATE', total_request_quantity='999')
    table = _public_business_table(raw, live=True)
    assert table is not None
    assert table['rows'][0]['request_quantity'] == '123456789012345678901234567890.000001'
    assert 'detail_id' not in table['rows'][0] and 'total_request_quantity' not in table['rows'][0]


@pytest.mark.parametrize('mutation', ['total', 'units', 'negative', 'float', 'unverified', 'missing_field', 'unsafe'])
def test_invalid_raw_presentation_is_rejected(orders, mutation):
    raw = answer(orders).business_table
    if mutation == 'total': raw['summary'].append({'label': '合计', 'value': '9.5', 'unit': ''})
    elif mutation == 'units': raw['summary'][0]['unit'] = '箱'
    elif mutation == 'negative': raw['rows'][0]['request_quantity'] = '-1'
    elif mutation == 'float': raw['rows'][0]['request_quantity'] = 1.25
    elif mutation == 'unverified': raw['complete_reconciled'] = False
    elif mutation == 'missing_field': del raw['rows'][0]['sales_unit_reference']
    else: raw['rows'][0]['sku_name'] = '<script>'
    assert _public_business_table(raw, live=True) is None


def test_no_live_evidence_cannot_show_table(orders):
    assert _public_business_table(answer(orders).business_table, live=False) is None


@pytest.mark.parametrize('invalid', [False, True])
def test_all_pages_screened_before_display_and_bound_to_owner(orders, tmp_path, invalid):
    raw = answer(orders).business_table
    raw['rows'] = [deepcopy(raw['rows'][0]) for _ in range(501)]
    for i, row in enumerate(raw['rows']): row['sales_order_code'] = f'TEST-{i:04}'
    raw['summary'] = [{'label': '订单数', 'value': '501', 'unit': '单'}]
    if invalid: raw['rows'][-1]['quantity_unit_reference'] = '<script>'
    app = build_app(tmp_path, raw, route='so07_order_details')
    with TestClient(app) as client, TestClient(app) as other:
        response = submit(client)
        if invalid:
            assert response.status_code >= 400
            assert not app.state.result_pages._results
            assert 'business_table' not in response.json()
            return
        assert response.status_code == 200
        table = response.json()['business_table']
        paging = table['pagination']
        assert paging['total_rows'] == 501 and paging['page_count'] == 6
        uri = f"/v1/results/{paging['result_id']}/pages/6"
        tail = client.get(uri).json()['business_table']
        assert tail['rows'][0]['sales_order_code'] == 'TEST-0500'
        assert tail['notes'] == table['notes'] and tail['summary'] == table['summary']
        assert submit(client).json()['business_table']['pagination']['result_id'] == paging['result_id']
        other.get('/v1/session')
        assert other.get(uri).status_code == 404


@pytest.mark.parametrize('reviewed,complete', [(False, True), (True, False)])
def test_no_unreviewed_or_incomplete_table_even_when_small(orders, tmp_path, reviewed, complete):
    raw = answer(orders).business_table
    raw['complete_reconciled'] = complete
    with TestClient(build_app(tmp_path, raw, route='so07_order_details', reviewed=reviewed)) as client:
        result = submit(client)
        assert result.status_code >= 400
        assert 'business_table' not in result.json()

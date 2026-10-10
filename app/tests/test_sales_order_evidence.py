"""Source timestamps and unit references are evidence, not inferred freshness."""

from dataclasses import replace

import pytest

from app.agent_core.sales_order_details import reconcile_sales_order_details, retrieve_sales_order_details
from app.agent_core.semantic_types import SemanticRuntimeError
from app.tests.test_sales_order_details import PLAN, orders, execute
from app.tests.test_sales_order_pages import Query


def test_missing_header_times_count_orders_not_repeated_detail_rows(orders):
    evidence = retrieve_sales_order_details(PLAN, Query(orders)).source_evidence
    assert evidence.missing_header_updated_count == 3
    assert evidence.missing_detail_updated_count == 4
    assert evidence.source_header_latest_updated is None
    assert evidence.source_detail_latest_updated is None
    assert evidence.ingestion_freshness == 'unverified'


def test_missing_detail_does_not_fabricate_missing_unit_or_detail_timestamp(orders):
    orders.execute("DELETE FROM ywzt_ods.scm_sales_order_detail WHERE sales_order_code='SO2'")
    evidence = retrieve_sales_order_details(PLAN, Query(orders)).source_evidence
    assert evidence.missing_header_updated_count == 3
    assert evidence.missing_detail_updated_count == 3
    assert evidence.missing_quantity_unit_reference_count == 3
    assert evidence.unit_reference_pairs == ((None, None, 3),)


def test_unit_references_are_not_labels_conversions_or_cross_unit_totals(orders):
    orders.execute("UPDATE ywzt_ods.scm_sales_order_detail SET quantity_u_m=' 010 ',sales_unit=' 040 ' WHERE id='D1'")
    orders.execute("UPDATE ywzt_ods.scm_sales_order_detail SET quantity_u_m='020',sales_unit='040' WHERE id='D2'")
    result = retrieve_sales_order_details(PLAN, Query(orders))
    assert result.source_evidence.unit_reference_pairs == ((None, None, 2), ('010', '040', 1), ('020', '040', 1))
    assert result.source_evidence.quantity_unit_status == 'unconfirmed'
    first = next(row for row in result.rows if row['detail_id'] == 'D1')
    assert first['quantity_unit_reference'] == '010' and first['sales_unit_reference'] == '040'
    assert first['request_quantity'] == '2.5'


def test_source_change_times_are_separate_from_creation_window_and_ingestion(orders):
    orders.execute("UPDATE ywzt_ods.scm_sales_order_header SET updated='2026-09-11 01:00:00' WHERE id='H1'")
    orders.execute("UPDATE ywzt_ods.scm_sales_order_detail SET updated='2026-09-11 02:00:00' WHERE id='D2'")
    evidence = retrieve_sales_order_details(PLAN, Query(orders)).source_evidence
    assert evidence.source_header_latest_updated == '2026-09-11T01:00:00+08:00'
    assert evidence.source_detail_latest_updated == '2026-09-11T02:00:00+08:00'
    assert evidence.missing_header_updated_count == 2 and evidence.missing_detail_updated_count == 3
    assert evidence.ingestion_freshness == 'unverified'


@pytest.mark.parametrize('field', ['header_updated_at', 'detail_updated_at', 'quantity_unit_reference', 'sales_unit_reference'])
def test_evidence_change_between_passes_rejects_result(orders, field):
    def mutate(rows, call):
        if call == 2:
            rows[0][field] = 'changed'
        return rows
    with pytest.raises(SemanticRuntimeError) as failure:
        retrieve_sales_order_details(PLAN, Query(orders, mutate))
    assert failure.value.reason == 'sales_page_invalid'


@pytest.mark.parametrize('control', ['missing_header_updated_count', 'missing_detail_updated_count',
    'missing_quantity_unit_reference_count', 'missing_sales_unit_reference_count',
    'source_header_latest_updated', 'source_detail_latest_updated'])
def test_source_controls_must_reconcile_not_just_repeat_consistently(orders, control):
    rows = execute(orders)
    for row in rows:
        row[control] = '2026-09-11 00:00:00' if control.startswith('source_') else 99
    with pytest.raises(SemanticRuntimeError) as failure:
        reconcile_sales_order_details(rows, PLAN)
    assert failure.value.reason == 'sales_order_quality_failed'


@pytest.mark.parametrize('field,value', [('header_updated_at', 'not-time'), ('detail_updated_at', 'not-time'),
    ('quantity_unit_reference', ''), ('quantity_unit_reference', ' 010'), ('sales_unit_reference', 40)])
def test_malformed_source_evidence_is_not_silently_dropped(orders, field, value):
    rows = execute(orders)
    rows[0][field] = value
    with pytest.raises(SemanticRuntimeError) as failure:
        reconcile_sales_order_details(rows, PLAN)
    assert failure.value.reason == 'sales_order_quality_failed'


def test_empty_source_evidence_does_not_claim_synchronization_is_complete(orders):
    result = retrieve_sales_order_details(replace(PLAN, owner='MISSING'), Query(orders))
    assert result.source_evidence.ingestion_freshness == 'unverified'
    assert result.source_evidence.unit_reference_pairs == ()
    assert result.source_evidence.source_header_latest_updated is None


def test_evidence_scope_does_not_use_updates_or_units_from_other_orders(orders):
    orders.execute("UPDATE ywzt_ods.scm_sales_order_header SET updated='2026-09-12 00:00:00' WHERE company_code='A'")
    orders.execute("UPDATE ywzt_ods.scm_sales_order_detail SET updated='2026-09-12 00:00:00',quantity_u_m='OTHER',sales_unit='OTHER' WHERE company_code='A'")
    scoped = retrieve_sales_order_details(replace(PLAN, owner='B'), Query(orders)).source_evidence
    assert scoped.source_header_latest_updated is None and scoped.source_detail_latest_updated is None
    assert scoped.missing_header_updated_count == scoped.missing_detail_updated_count == 1
    assert scoped.unit_reference_pairs == ((None, None, 1),)


@pytest.mark.parametrize('field', ['header_updated_at', 'detail_updated_at', 'quantity_unit_reference',
    'sales_unit_reference', 'missing_header_updated_count', 'source_detail_latest_updated'])
def test_omitted_evidence_fields_are_not_treated_as_source_nulls(orders, field):
    rows = execute(orders)
    del rows[0][field]
    with pytest.raises(SemanticRuntimeError) as failure:
        reconcile_sales_order_details(rows, PLAN)
    assert failure.value.reason == 'sales_order_quality_failed'


def test_head_timestamp_conflict_cannot_hide_in_other_detail_lines(orders):
    rows = execute(orders)
    rows[0]['header_updated_at'] = '2026-09-11 01:00:00'
    with pytest.raises(SemanticRuntimeError):
        reconcile_sales_order_details(rows, PLAN)


def test_missing_detail_cannot_carry_real_detail_evidence(orders):
    orders.execute("DELETE FROM ywzt_ods.scm_sales_order_detail WHERE sales_order_code='SO2'")
    rows = execute(orders)
    next(row for row in rows if row['detail_id'] is None)['sales_unit_reference'] = '040'
    with pytest.raises(SemanticRuntimeError):
        reconcile_sales_order_details(rows, PLAN)


def test_timezones_are_normalized_before_comparing_source_controls(orders):
    rows = execute(orders)
    for row in rows:
        row.update(header_updated_at='2026-09-10T17:00:00Z', detail_updated_at='2026-09-11 02:00:00',
                   missing_header_updated_count=0, missing_detail_updated_count=0,
                   source_header_latest_updated='2026-09-11 01:00:00',
                   source_detail_latest_updated='2026-09-10T18:00:00Z')
    result = reconcile_sales_order_details(rows, PLAN)
    assert result.source_evidence.source_header_latest_updated == '2026-09-11T01:00:00+08:00'
    assert result.source_evidence.ingestion_freshness == 'unverified'


def test_source_evidence_doris_fixtures_match_independent_expectations(orders):
    from pathlib import Path
    import runpy
    import sqlglot
    from sqlglot import exp
    fixture = runpy.run_path(str(Path(__file__).resolve().parents[2] /
                                'tests/acceptance/check_so07_evidence_live.py'))
    def query(sql):
        tree = sqlglot.parse_one(sql, read='doris')
        assert not any(table.db or table.catalog for table in tree.find_all(exp.Table))
        for cast in list(tree.find_all(exp.Cast)):
            if cast.to.is_type('DATETIME', 'TIMESTAMP') and isinstance(cast.this, exp.Literal):
                cast.replace(cast.this.copy())
        return [dict(row) for row in orders.execute(tree.sql(dialect='sqlite'))]
    for name, plan, source, expected in fixture['cases']():
        assert fixture['check_case'](plan, source, expected, query), name

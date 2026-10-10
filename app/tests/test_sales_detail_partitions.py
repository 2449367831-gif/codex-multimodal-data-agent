"""Requirements-first coverage for bounded, reconciled monthly sales details."""

from datetime import date
from decimal import Decimal

import pytest
import sqlglot

from app.agent_core.sales_detail_partitions import (
    SalesDetailPartitionPlan, month_windows, retrieve_partitioned_sales_details,
    compile_sales_detail_control, compile_sales_detail_partition,
)
from app.agent_core.semantic_types import SemanticRuntimeError
from app.query_scope import QueryScope
from app.sql import SqlSafetyValidator
from app.tests.test_sales_aggregation import facts


def query_fixture(facts, mutate=None):
    calls = []

    def query(sql, *, purpose):
        calls.append(purpose)
        rows = [dict(row) for row in facts.execute(sqlglot.transpile(sql, read='doris', write='sqlite')[0])]
        return mutate(rows, purpose, len(calls)) if mutate else rows

    return query, calls


def test_month_windows_cover_original_interval_without_overlap_or_today_invention():
    assert month_windows(date(2024, 1, 31), date(2024, 3, 2)) == (
        (date(2024, 1, 31), date(2024, 2, 1)),
        (date(2024, 2, 1), date(2024, 3, 1)),
        (date(2024, 3, 1), date(2024, 3, 2)),
    )
    assert month_windows(date(2026, 12, 31), date(2027, 1, 1)) == ((date(2026, 12, 31), date(2027, 1, 1)),)


@pytest.mark.parametrize('start,end', [(None, None), (date(2026, 9, 1), date(2026, 9, 1)), ('2026-09-01', '2026-09-02')])
def test_explicit_valid_interval_required(start, end):
    with pytest.raises(ValueError):
        SalesDetailPartitionPlan(start, end)


def test_complete_details_reconcile_without_multiplying_repeated_document_skus(facts):
    query, calls = query_fixture(facts)
    result = retrieve_partitioned_sales_details(SalesDetailPartitionPlan(date(2026, 9, 9), date(2026, 9, 10)), query)
    assert result.complete is True and len(result.rows) == 5
    assert result.summary.quantity == Decimal('18')
    assert result.summary.document_count == 4 and result.summary.detail_count == 5
    assert sum(Decimal(str(row['quantity'])) for row in result.rows) == Decimal('18')
    assert sum(row['quantity'] == 0 for row in result.rows) == 2
    assert calls[0] == calls[-1] == 'sales detail full-range control'


def test_scope_applies_to_summary_and_every_month(facts):
    facts.execute("UPDATE ywzt_ods.scm_sales_shipment_header SET ship_at='2026-08-31 23:59:59' WHERE company_code='HJXH' AND outbound_code='O1'")
    query, _ = query_fixture(facts)
    plan = SalesDetailPartitionPlan(date(2026, 8, 31), date(2026, 9, 10), QueryScope(owner='HJXH', warehouse_codes=('W1',), sku_codes=('A',)))
    result = retrieve_partitioned_sales_details(plan, query)
    assert len(result.partitions) == 2 and result.complete
    assert result.summary.quantity == Decimal('1003.75')
    assert result.summary.document_count == 3 and len(result.rows) == 3
    assert all(row['cargo_owner'] == 'HJXH' and row['warehouse_code'] == 'W1' and row['sku_code'] == 'A' for row in result.rows)


def test_over_capacity_retains_summary_and_plan_but_never_partial_details(facts):
    query, calls = query_fixture(facts)
    result = retrieve_partitioned_sales_details(SalesDetailPartitionPlan(date(2026, 8, 1), date(2026, 10, 1)), query, max_rows=1)
    assert result.complete is False and result.rows is None
    assert result.summary.detail_count > 1 and len(result.partitions) == 2
    assert calls == ['sales detail full-range control']


def test_successful_empty_is_complete_without_invented_rows(facts):
    query, _ = query_fixture(facts)
    result = retrieve_partitioned_sales_details(SalesDetailPartitionPlan(date(2025, 1, 1), date(2025, 3, 1)), query)
    assert result.complete and result.rows == ()
    assert result.summary.quantity == 0 and result.summary.document_count == 0


@pytest.mark.parametrize('fault', ['source_moved', 'quantity_changed', 'key_changed', 'time_outside', 'missing_row'])
def test_no_complete_result_when_partition_or_full_range_controls_disagree(facts, fault):
    def mutate(rows, purpose, call):
        if purpose == 'sales detail full-range control' and call > 1 and fault == 'source_moved':
            rows[0]['total_quantity'] += 1
        elif purpose != 'sales detail full-range control' and rows:
            if fault == 'quantity_changed': rows[0]['quantity'] += 1
            if fault == 'key_changed': rows[1]['outbound_code'] = rows[0]['outbound_code']; rows[1]['sku_code'] = rows[0]['sku_code']; rows[1]['cargo_owner'] = rows[0]['cargo_owner']
            if fault == 'time_outside': rows[0]['ship_at'] = '2026-09-10 00:00:00'
            if fault == 'missing_row': rows.pop()
        return rows
    query, _ = query_fixture(facts, mutate)
    with pytest.raises(SemanticRuntimeError):
        retrieve_partitioned_sales_details(SalesDetailPartitionPlan(date(2026, 9, 9), date(2026, 9, 10)), query)


@pytest.mark.parametrize('field,value', [('ship_qty', None), ('ship_qty', -1)])
def test_source_quantity_errors_block_before_detail_fanout(facts, field, value):
    facts.execute(f"UPDATE ywzt_ods.scm_sales_shipment_detail SET {field}=? WHERE company_code='HJXH' AND outbound_code='O1'", (value,))
    query, calls = query_fixture(facts)
    with pytest.raises(SemanticRuntimeError):
        retrieve_partitioned_sales_details(SalesDetailPartitionPlan(date(2026, 9, 9), date(2026, 9, 10)), query)
    assert len(calls) == 1


def test_exact_row_capacity_with_later_empty_months_is_complete(facts):
    query, _ = query_fixture(facts)
    plan = SalesDetailPartitionPlan(date(2026, 9, 9), date(2026, 12, 1), QueryScope(owner='B'))
    result = retrieve_partitioned_sales_details(plan, query, max_rows=1)
    assert result.complete and len(result.rows) == 1
    assert len(result.partitions) == 3


@pytest.mark.parametrize('option,value', [
    ('max_rows', 0), ('max_rows', 100001), ('max_rows', True),
    ('max_bytes', 0), ('max_bytes', 64*1024*1024+1), ('max_bytes', True),
    ('timeout_seconds', 0), ('timeout_seconds', 181), ('timeout_seconds', float('inf')),
    ('timeout_seconds', float('nan')), ('timeout_seconds', True),
])
def test_invalid_budget_never_queries(facts, option, value):
    query, calls = query_fixture(facts)
    with pytest.raises(ValueError):
        retrieve_partitioned_sales_details(SalesDetailPartitionPlan(date(2026, 9, 9), date(2026, 9, 10)), query, **{option: value})
    assert not calls


def test_one_timeout_budget_includes_control_queries_and_all_months(facts, monkeypatch):
    elapsed = [0]
    monkeypatch.setattr('app.agent_core.sales_detail_partitions.time.monotonic', lambda: elapsed[0])
    def mutate(rows, purpose, call):
        elapsed[0] += 2
        return rows
    query, calls = query_fixture(facts, mutate)
    with pytest.raises(SemanticRuntimeError) as error:
        retrieve_partitioned_sales_details(SalesDetailPartitionPlan(date(2026, 8, 1), date(2026, 10, 1)), query, timeout_seconds=5)
    assert error.value.reason == 'query_timeout'
    assert len(calls) == 3


def test_byte_capacity_is_shared_across_months(facts):
    facts.execute("UPDATE ywzt_ods.scm_sales_shipment_header SET ship_at='2026-08-31 23:59:59' WHERE company_code='HJXH' AND outbound_code='O1'")
    query, _ = query_fixture(facts)
    with pytest.raises(SemanticRuntimeError) as error:
        retrieve_partitioned_sales_details(SalesDetailPartitionPlan(date(2026, 8, 1), date(2026, 10, 1)), query, max_bytes=1000)
    assert error.value.reason == 'result_truncated'


def test_completed_rows_cannot_be_mutated_after_reconciliation(facts):
    query, _ = query_fixture(facts)
    result = retrieve_partitioned_sales_details(SalesDetailPartitionPlan(date(2026, 9, 9), date(2026, 9, 10)), query)
    with pytest.raises(TypeError):
        result.rows[0]['quantity'] = '999'


@pytest.mark.parametrize('batched', [False, True])
def test_real_page_sized_months_close_against_full_scope_control(facts, batched):
    records = [('TEST', f'PART-{i:04}', 'W1', f'2026-{7 + i // 600:02}-15 12:00:00',
                700, 'JYCK', f'ORDER-{i:04}', '20', 0, 'false') for i in range(1200)]
    facts.executemany('INSERT INTO ywzt_ods.scm_sales_shipment_header VALUES (?,?,?,?,?,?,?,?,?,?)', records)
    facts.executemany("INSERT INTO ywzt_ods.scm_sales_order_header VALUES (?,?,0,'false')", [('TEST', row[6]) for row in records])
    facts.executemany("INSERT INTO ywzt_ods.scm_sales_shipment_detail VALUES (?,?,?,1,0,'false')", [('TEST', row[1], 'A') for row in records])
    def inspect(rows, purpose, call):
        assert len(rows) <= 500
        return rows
    query, calls = query_fixture(facts, inspect)
    def query_many(statements, *, purpose, timeout_seconds):
        assert 0 < timeout_seconds <= 180 and 1 <= len(statements) <= 4
        return [query(sql, purpose=purpose) for sql in statements]
    plan = SalesDetailPartitionPlan(date(2026, 7, 1), date(2026, 9, 1))
    result = retrieve_partitioned_sales_details(plan, query, query_many=query_many if batched else None)
    assert result.complete and len(result.rows) == 1200
    assert result.summary.quantity == result.summary.document_count == result.summary.detail_count == 1200
    assert len({(row['cargo_owner'], row['outbound_code'], row['sku_code']) for row in result.rows}) == 1200
    assert len(calls) == 10


@pytest.mark.parametrize('timestamp', [None, 'invalid', '2026-09-10 00:00:00'])
def test_nonempty_summary_requires_valid_in_window_business_time(facts, timestamp):
    def mutate(rows, purpose, call):
        if purpose == 'sales detail full-range control':
            rows[0]['latest_shipment_at'] = timestamp
        return rows
    query, calls = query_fixture(facts, mutate)
    with pytest.raises(SemanticRuntimeError):
        retrieve_partitioned_sales_details(SalesDetailPartitionPlan(date(2026, 9, 9), date(2026, 9, 10)), query)
    assert len(calls) == 1


@pytest.mark.parametrize('scope', [QueryScope(), QueryScope(owner='HJXH', warehouse_codes=('W1',), sku_codes=('QY-YwKC500G',)), QueryScope(finished_goods=True)])
def test_control_and_partitions_keep_existing_single_readonly_catalog_gate(scope):
    plan = SalesDetailPartitionPlan(date(2026, 8, 31), date(2026, 9, 10), scope)
    statements = [compile_sales_detail_control(plan)] + [compile_sales_detail_partition(plan, start, end) for start, end in month_windows(plan.start, plan.end)]
    allowed = {f'ywzt_ods.{name}' for name in ('scm_sales_shipment_header', 'scm_sales_shipment_detail', 'scm_sales_order_header', 'master_wms', 'master_sku')}
    for sql in statements:
        SqlSafetyValidator().validate(sql, allowed_sources=allowed)

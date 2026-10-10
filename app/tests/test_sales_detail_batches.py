from dataclasses import replace
from datetime import date
from decimal import Decimal
from fractions import Fraction

import pytest

from app.agent_core.sales_detail_batches import (
    SalesDetailCursor, compile_batch, definition_signature, retrieve_sales_detail_batch,
)
from app.agent_core.sales_detail_partitions import SalesDetailPartitionPlan, retrieve_partitioned_sales_details
from app.agent_core.semantic_types import SemanticRuntimeError
from app.tests.test_sales_aggregation import facts
from app.tests.test_sales_detail_partitions import query_fixture


def initial(facts):
    query, _ = query_fixture(facts)
    plan = SalesDetailPartitionPlan(date(2026, 9, 9), date(2026, 9, 10))
    checked = retrieve_partitioned_sales_details(plan, query, max_rows=1)
    return SalesDetailCursor(plan, checked.summary, definition_signature(plan)), query


def test_every_batch_keeps_original_scope_and_final_controls(facts):
    cursor, query = initial(facts)
    baseline = cursor.baseline
    rows, sizes = [], []
    while not cursor.complete:
        batch = retrieve_sales_detail_batch(cursor, query, max_rows=2)
        assert batch.source is cursor
        assert batch.cursor.plan is cursor.plan
        assert batch.cursor.baseline is baseline
        rows.extend(batch.rows)
        sizes.append(len(batch.rows))
        cursor = batch.cursor
    assert sizes == [2, 2, 1]
    assert cursor.loaded == len(rows) == baseline.detail_count
    assert cursor.documents == baseline.document_count == 4
    assert cursor.quantity == Fraction(baseline.quantity) == 18
    keys = [tuple(row[x] for x in ('cargo_owner', 'outbound_code', 'sku_code')) for row in rows]
    assert keys == sorted(set(keys))
    assert sum(Fraction(str(row['quantity'])) for row in rows) == cursor.quantity


def test_failed_batch_does_not_mutate_cursor_and_retry_reads_same_batch(facts):
    cursor, query = initial(facts)
    def fail(sql, **kwargs):
        if 'batch_facts' in sql:
            raise SemanticRuntimeError('test', reason='query_timeout')
        return query(sql, **kwargs)
    with pytest.raises(SemanticRuntimeError):
        retrieve_sales_detail_batch(cursor, fail, max_rows=2)
    assert cursor.loaded == 0 and cursor.after is None and cursor.batch_index == 1
    first = retrieve_sales_detail_batch(cursor, query, max_rows=2)
    replay = retrieve_sales_detail_batch(cursor, query, max_rows=2)
    assert first.cursor == replay.cursor and first.rows == replay.rows


def test_source_change_between_batches_stops_before_returning_more_rows(facts):
    cursor, query = initial(facts)
    batch = retrieve_sales_detail_batch(cursor, query, max_rows=2)
    facts.execute('UPDATE ywzt_ods.scm_sales_shipment_detail SET ship_qty=ship_qty+1 WHERE ship_qty IS NOT NULL')
    with pytest.raises(SemanticRuntimeError) as error:
        retrieve_sales_detail_batch(batch.cursor, query, max_rows=2)
    assert error.value.reason == 'sales_detail_source_changed'


def test_prefix_drift_cannot_be_hidden_by_unchanged_global_totals(facts):
    cursor, query = initial(facts)
    batch = retrieve_sales_detail_batch(cursor, query, max_rows=2)
    def drift(sql, **kwargs):
        rows = query(sql, **kwargs)
        if 'prefix_facts' in sql:
            rows = [{**rows[0], 'quantity': str(Decimal(str(rows[0]['quantity'])) + 1)}]
        return rows
    with pytest.raises(SemanticRuntimeError) as error:
        retrieve_sales_detail_batch(batch.cursor, drift, max_rows=2)
    assert error.value.reason == 'sales_detail_source_changed'


def test_changed_contract_and_forged_cursor_are_rejected(facts, monkeypatch):
    cursor, query = initial(facts)
    monkeypatch.setattr('app.agent_core.sales_detail_batches.definition_signature', lambda plan: '0' * 64)
    with pytest.raises(SemanticRuntimeError):
        retrieve_sales_detail_batch(cursor, query)
    for fields in ({'loaded': True}, {'after': ('A', 'B', 'C')}, {'documents': 1}, {'quantity': Fraction(1)}):
        with pytest.raises(ValueError):
            replace(cursor, **fields)


@pytest.mark.parametrize('bad', [0, -1, 5001, True, 1.5])
def test_batch_never_relaxes_operation_bounds(facts, bad):
    cursor, query = initial(facts)
    with pytest.raises(ValueError):
        retrieve_sales_detail_batch(cursor, query, max_rows=bad)


def test_cursor_key_is_sql_literal_not_executable_input(facts):
    import sqlglot
    from sqlglot import exp
    cursor, _ = initial(facts)
    cursor = replace(cursor, after=("A' OR 1=1 --", 'B', 'C'), loaded=1, documents=1, batch_index=2)
    tree = sqlglot.parse_one(compile_batch(cursor, 2), read='doris')
    assert any(literal.this == "A' OR 1=1 --" for literal in tree.find_all(exp.Literal))
    assert len(sqlglot.parse(compile_batch(cursor, 2), read='doris')) == 1


def test_independent_source_prefix_matches_each_completed_batch(facts, monkeypatch):
    from pathlib import Path
    import runpy
    import sqlglot
    directory = Path(__file__).resolve().parents[2] / 'tests/acceptance'
    monkeypatch.syspath_prepend(str(directory))
    verifier = runpy.run_path(str(directory / 'check_sales_detail_batches_live.py'))
    cursor, query = initial(facts)
    base = verifier['independent_control_sql'](cursor.plan.start, cursor.plan.end)
    rows = []
    while not cursor.complete:
        batch = retrieve_sales_detail_batch(cursor, query, max_rows=2)
        rows.extend(batch.rows)
        cursor = batch.cursor
        statement = verifier['prefix_sql'](base, cursor.after)
        reference_rows = [dict(row) for row in facts.execute(sqlglot.transpile(statement, read='doris', write='sqlite')[0])]
        controls = verifier['reference'](reference_rows)
        assert verifier['verify_rows'](rows, controls, start=cursor.plan.start, end=cursor.plan.end)


def test_batch_and_prefix_sql_keep_ast_catalog_and_row_gates(facts):
    from app.sql import SqlSafetyValidator
    from app.agent_core.sales_detail_batches import compile_prefix_control
    from app.agent_core.sales_pages import aggregate_page_sql
    cursor, _ = initial(facts)
    cursor = replace(cursor, after=('HJXH', 'O1', 'A'), loaded=1, documents=1, batch_index=2)
    allowed = {f'ywzt_ods.{name}' for name in ('scm_sales_shipment_header', 'scm_sales_shipment_detail',
               'scm_sales_order_header', 'master_wms', 'master_sku')}
    for statement in (compile_prefix_control(cursor), aggregate_page_sql(compile_batch(cursor, 5000), 0, 500)):
        SqlSafetyValidator().validate(statement, allowed_sources=allowed)

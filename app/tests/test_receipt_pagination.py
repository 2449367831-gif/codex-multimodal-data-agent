"""Large-result requirements, using synthetic facts before any Doris check."""

from dataclasses import replace
from datetime import date
from decimal import Decimal
import sqlite3

import pytest
import sqlglot

from app.agent_core.inbound_details import InboundDetailPlan
from app.agent_core.receipt_pagination import compile_receipt_page, retrieve_receipt_details
from app.agent_core.semantic_types import SemanticRuntimeError


PLAN = InboundDetailPlan(date(2026, 9, 3), date(2026, 9, 10), date(2026, 9, 10), "purchase_type")


@pytest.fixture
def facts():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("ATTACH DATABASE ':memory:' AS ywzt_ods")
    db.executescript("""
    CREATE TABLE ywzt_ods.scm_warehous_header (cargo_owner_id TEXT, warehous_code TEXT, po_order_code TEXT, warehous_type TEXT, actual_arrival_date TEXT, status TEXT, _fdl_marked_deleted TEXT);
    CREATE TABLE ywzt_ods.scm_warehous_detail (id TEXT, cargo_owner_id TEXT, warehous_code TEXT, item_code TEXT, wms_code TEXT, sku_code TEXT, batch_number TEXT, production_date TEXT, received_quantity NUMERIC, unit TEXT, status TEXT, _fdl_marked_deleted TEXT);
    CREATE TABLE ywzt_ods.master_wms (cargo_owner_id TEXT, wms_code TEXT, wms_desc TEXT, status TEXT);
    INSERT INTO ywzt_ods.scm_warehous_header VALUES ('A','R1','','10','2026-09-09 12:00:00','0','false');
    INSERT INTO ywzt_ods.master_wms VALUES ('A','W1','Synthetic warehouse','0');
    """)
    db.executemany("INSERT INTO ywzt_ods.scm_warehous_detail VALUES (?,'A','R1',?,'W1','S1',?,'2026-09-04 12:00:00',?,?, '0','false')", [
        (f"D{i:05d}", str(i), f"B{i}", "1.25" if i % 2 else "0", "10" if i % 3 else "40") for i in range(1001)
    ])
    yield db
    db.close()


def execute(db, sql):
    return [dict(r) for r in db.execute(sqlglot.transpile(sql, read="doris", write="sqlite")[0])]


def test_large_result_preserves_all_lines_units_and_zero_quantities(facts):
    calls = []
    def query(sql, *, purpose):
        rows = execute(facts, sql)
        calls.append(len(rows))
        return rows
    result = retrieve_receipt_details(PLAN, query=query, page_size=200)
    assert result.row_count == 1001
    assert len(calls) == 12 and max(calls) <= 200
    assert sum(result.totals_by_unit.values()) == Decimal("625")
    assert len({r["detail_id"] for r in result.rows}) == 1001
    assert result.rows[-1]["detail_id"] == "D01000"


def test_page_boundaries_are_nonoverlapping(facts):
    first = execute(facts, compile_receipt_page(PLAN, offset=0, page_size=200))
    second = execute(facts, compile_receipt_page(PLAN, offset=200, page_size=200))
    assert first[-1]["result_ordinal"] == 200 and second[0]["result_ordinal"] == 201
    assert first[0]["matched_count"] == second[0]["matched_count"] == 1001
    assert not {r["detail_id"] for r in first} & {r["detail_id"] for r in second}


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "wrong_ordinal", "wrong_count", "failed"])
def test_bad_later_page_never_becomes_complete(facts, mutation):
    calls = 0
    def query(sql, *, purpose):
        nonlocal calls
        calls += 1
        rows = execute(facts, sql)
        if calls == 2:
            if mutation == "failed":
                raise SemanticRuntimeError("查询失败", reason="query_failed")
            if mutation == "missing": rows.pop()
            if mutation == "duplicate": rows[0]["detail_id"] = "D00000"
            if mutation == "wrong_ordinal": rows[0]["result_ordinal"] = 1
            if mutation == "wrong_count": rows[0]["matched_count"] = 1000
        return rows
    with pytest.raises(SemanticRuntimeError):
        retrieve_receipt_details(PLAN, query=query, page_size=200)


def test_source_change_with_unchanged_quantity_totals_is_detected(facts):
    calls = 0
    def query(sql, *, purpose):
        nonlocal calls
        calls += 1
        if calls == 7:
            facts.execute("UPDATE ywzt_ods.scm_warehous_detail SET batch_number='CHANGED' WHERE id='D00000'")
        return execute(facts, sql)
    with pytest.raises(SemanticRuntimeError) as failure:
        retrieve_receipt_details(PLAN, query=query, page_size=200)
    assert failure.value.reason == "receipt_source_changed"


def test_capacity_exhaustion_reports_truncation_not_success(facts):
    with pytest.raises(SemanticRuntimeError) as failure:
        retrieve_receipt_details(PLAN, query=lambda sql, **_: execute(facts, sql), max_rows=1000)
    assert failure.value.reason == "result_truncated"


def test_empty_result_is_checked_twice_and_remains_no_data():
    calls = []
    result = retrieve_receipt_details(PLAN, query=lambda sql, **_: calls.append(sql) or [])
    assert result.row_count == 0 and len(calls) == 2


@pytest.mark.parametrize("offset,size", [(-1, 200), (True, 200), (0, 0), (0, 501), (0, True)])
def test_invalid_page_bounds_rejected_before_query(offset, size):
    with pytest.raises(ValueError): compile_receipt_page(PLAN, offset=offset, page_size=size)

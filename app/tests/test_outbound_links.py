"""Relationship tests use synthetic identities, never expected warehouse answers."""

from dataclasses import replace
from datetime import date
import importlib.util
from pathlib import Path
import sqlite3

import pytest
import sqlglot

from app.agent_core.outbound_links import (
    OutboundLinkPlan, compile_outbound_links, reconcile_outbound_links,
)
from app.agent_core.semantic_types import SemanticRuntimeError


PLAN = OutboundLinkPlan(outbound_codes=("OUT-1",))


@pytest.fixture
def links():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("ATTACH DATABASE ':memory:' AS ywzt_ods")
    db.executescript("""
    CREATE TABLE ywzt_ods.scm_sales_shipment_header (
      company_code TEXT, outbound_code TEXT, ref_order_code TEXT, ref_order_type TEXT,
      ship_at TEXT, delete_flag INTEGER, _fdl_marked_deleted TEXT);
    CREATE TABLE ywzt_ods.scm_sales_order_header (
      company_code TEXT, sales_order_code TEXT, source_order_code TEXT,
      source_platform_code TEXT, order_type TEXT, updated TEXT,
      deleted_flag INTEGER, _fdl_marked_deleted TEXT);
    CREATE TABLE ywzt_ods.scm_sales_order_detail (
      company_code TEXT, sales_order_code TEXT, source_order_code TEXT,
      status INTEGER, _fdl_marked_deleted TEXT);
    INSERT INTO ywzt_ods.scm_sales_shipment_header VALUES
      ('A','OUT-1','SO-1','JYCK','2026-09-10 12:00:00',0,'false'),
      ('A','OUT-1','SO-1','JYCK','2026-09-10 12:00:00',0,'false'),
      ('B','OUT-1','SO-1','SO07','2026-09-10 12:00:00',0,'false'),
      ('A','OUT-2','MISSING','QTCK','2026-09-10 13:00:00',0,'false'),
      ('C','OUT-3','SO-1','JYCK','2026-09-10 14:00:00',0,'false'),
      ('A','OLD','SO-1','JYCK','2026-09-09 23:59:59',0,'false'),
      ('A','END','SO-1','JYCK','2026-09-11 00:00:00',0,'false');
    INSERT INTO ywzt_ods.scm_sales_order_header VALUES
      ('A','SO-1','HEADER-A','PLATFORM-A','SO01','2026-09-10 12:00:00',0,'false'),
      ('A','SO-1','HEADER-A','PLATFORM-A','SO01','2026-09-10 12:00:00',0,'false'),
      ('B','SO-1','HEADER-B','PLATFORM-B','SO07','2026-09-10 12:00:00',0,'false');
    INSERT INTO ywzt_ods.scm_sales_order_detail VALUES
      ('A','SO-1','LINE-A1',0,'false'),
      ('A','SO-1','LINE-A1',0,'false'),
      ('A','SO-1','LINE-A2',0,'false'),
      ('A','SO-1','CANCELLED',1,'false'),
      ('A','SO-1','DELETED',0,'true'),
      ('B','SO-1','LINE-B',0,'false');
    """)
    yield db
    db.close()


def execute(db, plan=PLAN):
    tree = sqlglot.parse_one(compile_outbound_links(plan), read="doris")
    return [dict(row) for row in db.execute(tree.sql(dialect="sqlite"))]


def test_sources_are_distinct_and_multiple_line_sources_are_retained(links):
    result = reconcile_outbound_links(execute(links), PLAN)
    assert result.document_count == 2
    assert len(result.rows) == 3
    assert {(r["cargo_owner"], r["source_order_code"], r["detail_source_order_code"])
            for r in result.rows} == {("A", "HEADER-A", "LINE-A1"),
                                     ("A", "HEADER-A", "LINE-A2"),
                                     ("B", "HEADER-B", "LINE-B")}
    assert all(r["sales_order_code"] == "SO-1" for r in result.rows)
    row = result.rows[0]
    assert row["source_platform_code"] == "PLATFORM-A"
    assert row["order_type"] == "SO01" and row["ref_order_type"] == "JYCK"


def test_missing_order_keeps_outbound_and_does_not_match_another_owner(links):
    plan = replace(PLAN, outbound_codes=("OUT-2", "OUT-3"))
    result = reconcile_outbound_links(execute(links, plan), plan)
    assert result.document_count == 2 and result.unmatched_document_count == 2
    assert all(r["sales_order_code"] is None for r in result.rows)
    assert {r["referenced_order_code"] for r in result.rows} == {"MISSING", "SO-1"}


def test_deleted_order_keeps_outbound_and_no_line_sources(links):
    links.execute("UPDATE ywzt_ods.scm_sales_order_header SET deleted_flag=1 WHERE company_code='A'")
    result = reconcile_outbound_links(execute(links), PLAN)
    row = next(r for r in result.rows if r["cargo_owner"] == "A")
    assert row["sales_order_code"] is None and row["detail_source_order_code"] is None


def test_all_scope_and_half_open_dates(links):
    plan = OutboundLinkPlan(start=date(2026, 9, 10), end=date(2026, 9, 11))
    result = reconcile_outbound_links(execute(links, plan), plan)
    assert result.document_count == 4
    assert {r["outbound_code"] for r in result.rows} == {"OUT-1", "OUT-2", "OUT-3"}
    scoped = replace(plan, owner="B")
    assert {r["cargo_owner"] for r in reconcile_outbound_links(execute(links, scoped), scoped).rows} == {"B"}


@pytest.mark.parametrize("mutation", [
    "INSERT INTO ywzt_ods.scm_sales_shipment_header VALUES ('A','OUT-1','OTHER','JYCK','2026-09-10 12:00:00',0,'false')",
    "INSERT INTO ywzt_ods.scm_sales_order_header SELECT company_code,sales_order_code,'OTHER',source_platform_code,order_type,updated,deleted_flag,_fdl_marked_deleted FROM ywzt_ods.scm_sales_order_header WHERE company_code='A'",
    "UPDATE ywzt_ods.scm_sales_shipment_header SET _fdl_marked_deleted='unexpected' WHERE company_code='A'",
    "UPDATE ywzt_ods.scm_sales_order_header SET deleted_flag=7 WHERE company_code='A'",
    "UPDATE ywzt_ods.scm_sales_order_detail SET _fdl_marked_deleted='unexpected' WHERE company_code='A'",
    "UPDATE ywzt_ods.scm_sales_shipment_header SET company_code='' WHERE company_code='A'",
    "UPDATE ywzt_ods.scm_sales_shipment_header SET company_code=NULL WHERE company_code='A'",
])
def test_conflicting_or_invalid_evidence_is_not_arbitrarily_resolved(links, mutation):
    links.execute(mutation)
    with pytest.raises(SemanticRuntimeError):
        reconcile_outbound_links(execute(links), PLAN)


def test_missing_detail_source_is_retained_as_null(links):
    links.execute("UPDATE ywzt_ods.scm_sales_order_detail SET source_order_code=NULL WHERE company_code='B'")
    result = reconcile_outbound_links(execute(links), PLAN)
    row = next(r for r in result.rows if r["cargo_owner"] == "B")
    assert row["detail_source_order_code"] is None and row["sales_order_code"] == "SO-1"


@pytest.mark.parametrize("field,value", [("matched_count", 999), ("total_document_count", 99),
                                        ("cargo_owner", "FORGED"), ("sales_order_code", "WRONG")])
def test_corrupt_controls_and_identity_are_rejected(links, field, value):
    plan = replace(PLAN, owner="A")
    rows = execute(links, plan)
    rows[0][field] = value
    with pytest.raises(SemanticRuntimeError):
        reconcile_outbound_links(rows, plan)


def test_incomplete_overflow_and_duplicate_rows_are_not_complete(links):
    rows = execute(links)
    for bad in (rows[:-1], rows + [rows[0]]):
        with pytest.raises(SemanticRuntimeError):
            reconcile_outbound_links(bad, PLAN)
    plan = replace(PLAN, page_limit=1)
    with pytest.raises(SemanticRuntimeError, match="截断"):
        reconcile_outbound_links(execute(links, plan), plan)


def test_empty_result_and_identifier_literals(links):
    plan = replace(PLAN, outbound_codes=("x' OR 1=1 --",))
    assert reconcile_outbound_links(execute(links, plan), plan).document_count == 0


def test_null_document_key_is_not_dropped_before_quality_check(links):
    links.execute("UPDATE ywzt_ods.scm_sales_shipment_header SET outbound_code=NULL WHERE company_code='A'")
    plan = OutboundLinkPlan()
    with pytest.raises(SemanticRuntimeError):
        reconcile_outbound_links(execute(links, plan), plan)


def test_zero_first_control_cannot_be_overwritten_by_later_valid_control(links):
    rows = execute(links)
    rows[0]["total_document_count"] = 0
    with pytest.raises(SemanticRuntimeError):
        reconcile_outbound_links(rows, PLAN)


def test_header_conflict_outside_window_is_not_hidden_by_date_filter(links):
    links.execute("INSERT INTO ywzt_ods.scm_sales_shipment_header VALUES ('A','OUT-1','OTHER','JYCK','2026-08-01 12:00:00',0,'false')")
    plan = replace(PLAN, start=date(2026, 9, 10), end=date(2026, 9, 11))
    with pytest.raises(SemanticRuntimeError):
        reconcile_outbound_links(execute(links, plan), plan)


@pytest.mark.parametrize("kwargs", [{"start": date(2026, 9, 10)}, {"owner": ""},
    {"outbound_codes": ("",)}, {"page_limit": True}, {"page_limit": 10000},
    {"start": date(2026, 9, 11), "end": date(2026, 9, 10)}])
def test_invalid_plan_is_rejected(kwargs):
    with pytest.raises(ValueError):
        OutboundLinkPlan(**kwargs)


def reference_module():
    path = Path(__file__).resolve().parents[2] / "tests/acceptance/check_outbound_links_live.py"
    spec = importlib.util.spec_from_file_location("outbound_links_reference", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_independent_reference_matches_all_source_fields_and_missing_orders(links):
    ref = reference_module()
    codes = ("OUT-1", "OUT-2", "OUT-3")
    queries = ref.reference_queries(codes)
    observations = [[dict(r) for r in links.execute(sqlglot.transpile(q, read="doris", write="sqlite")[0])] for q in queries]
    expected = ref.expected_links(*observations)
    plan = replace(PLAN, outbound_codes=codes)
    actual = reconcile_outbound_links(execute(links, plan), plan)
    assert ref.canonical(actual.rows) == ref.canonical(expected)
    assert actual.document_count == 4 and actual.unmatched_document_count == 2
    altered = [dict(r) for r in actual.rows]
    altered[0]["source_order_code"] = "WRONG"
    assert ref.canonical(altered) != ref.canonical(expected)


@pytest.mark.parametrize("table,mutation", [
    ("scm_sales_shipment_header", "INSERT INTO ywzt_ods.scm_sales_shipment_header VALUES ('A','OUT-1','OTHER','JYCK','2026-09-10 12:00:00',0,'false')"),
    ("scm_sales_order_header", "UPDATE ywzt_ods.scm_sales_order_header SET deleted_flag=7"),
    ("scm_sales_order_detail", "UPDATE ywzt_ods.scm_sales_order_detail SET _fdl_marked_deleted='bad'"),
])
def test_independent_reference_rejects_invalid_sources(links, table, mutation):
    ref = reference_module()
    links.execute(mutation)
    observations = [[dict(r) for r in links.execute(sqlglot.transpile(q, read="doris", write="sqlite")[0])] for q in ref.reference_queries(PLAN.outbound_codes)]
    with pytest.raises(ValueError):
        ref.expected_links(*observations)

"""Requirement-first synthetic receipt facts; no recorded Doris result rows."""

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
import sqlite3

import pytest
import sqlglot
import yaml

from app.agent_core.inbound_details import (
    InboundDetailPlan, compile_inbound_details, reconcile_inbound_details,
)
from app.agent_core.semantic_types import SemanticRuntimeError
from app.adhoc_sql import AdHocSqlValidator
from app.query_scope import QueryScope, apply_scope
from app.agent_core.execution import QueryExecutionSession
from app.agent_core.handlers.receipt_detail import ReceiptDetailHandler
from app.agent_core.semantic_types import SemanticRequest
from app.chat_api import _public_business_table


PLAN = InboundDetailPlan(date(2026, 9, 3), date(2026, 9, 10), date(2026, 9, 10), "purchase_order_reference")


@pytest.fixture
def receipts():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("ATTACH DATABASE ':memory:' AS ywzt_ods")
    db.executescript("""
    CREATE TABLE ywzt_ods.scm_warehous_header (
      cargo_owner_id TEXT, warehous_code TEXT, po_order_code TEXT,
      warehous_type TEXT, actual_arrival_date TEXT, status TEXT,
      _fdl_marked_deleted TEXT);
    CREATE TABLE ywzt_ods.scm_warehous_detail (
      id TEXT, cargo_owner_id TEXT, warehous_code TEXT, item_code TEXT,
      wms_code TEXT, sku_code TEXT, batch_number TEXT, production_date TEXT,
      received_quantity NUMERIC, unit TEXT, status TEXT, _fdl_marked_deleted TEXT);
    CREATE TABLE ywzt_ods.master_wms (
      cargo_owner_id TEXT, wms_code TEXT, wms_desc TEXT, status TEXT);
    """)
    headers = [
        ("A", "R1", "P1", "10", "2026-09-09 18:00:00", "0", "false"),
        ("A", "R1", "P1", "10", "2026-09-09 18:00:00", "0", "false"),
        ("B", "R1", "P2", "10", "2026-09-09 23:59:59", "0", "false"),
        ("A", "TODAY", "P3", "10", "2026-09-10 00:00:00", "0", "false"),
        ("A", "FUTURE", "P4", "10", "2026-09-11 00:00:00", "0", "false"),
        ("A", "TYPE_ONLY", " ", "10", "2026-09-08 00:00:00", "0", "false"),
        ("A", "REF_ONLY", "P5", "11", "2026-09-08 00:00:00", "0", "false"),
        ("A", "DELETED", "P6", "10", "2026-09-09 00:00:00", "0", "true"),
        ("A", "INVALID", "P7", "10", "2026-09-09 00:00:00", "1", "false"),
        ("A", "NO_ARRIVAL", "P8", "10", None, "0", "false"),
    ]
    db.executemany("INSERT INTO ywzt_ods.scm_warehous_header VALUES (?,?,?,?,?,?,?)", headers)
    details = [
        ("D1", "A", "R1", "1", "W1", "S1", "B1", "2026-09-03 00:00:00", "1.25", "10", "0", "false"),
        ("D2", "A", "R1", "2", "W1", "S1", "B2", "2026-09-09 23:59:59", "2.50", "10", "0", "false"),
        ("D1", "B", "R1", "1", "W1", "S1", "B1", "2026-09-04 00:00:00", "4.75", "10", "0", "false"),
        ("D3", "A", "R1", "3", "W1", "S1", "OLD", "2026-09-02 23:59:59", "100", "10", "0", "false"),
        ("D4", "A", "R1", "4", "W1", "S1", "TODAY", "2026-09-10 00:00:00", "100", "10", "0", "false"),
        ("D5", "A", "R1", "5", "W1", "S1", "DELETED", "2026-09-04 00:00:00", "100", "10", "0", "true"),
        ("D6", "A", "R1", "6", "W1", "S1", "INVALID", "2026-09-04 00:00:00", "100", "10", "1", "false"),
    ]
    for owner, code, *_ in headers[3:]:
        details.append(("D-" + code, owner, code, "1", "W1", "S1", "B3", "2026-09-04 00:00:00", "8", "10", "0", "false"))
    db.executemany("INSERT INTO ywzt_ods.scm_warehous_detail VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", details)
    db.executemany("INSERT INTO ywzt_ods.master_wms VALUES (?,?,?,'0')", [
        ("A", "W1", "Test warehouse A"), ("A", "W1", "Test warehouse A"),
        ("B", "W1", "Test warehouse B"),
    ])
    yield db
    db.close()


def execute(db, plan=PLAN):
    sql = compile_inbound_details(plan)
    tree = sqlglot.parse_one(sql, read="doris")
    assert tree is not None
    assert all(t.db == "ywzt_ods" for t in tree.find_all(sqlglot.exp.Table) if t.db)
    return [dict(r) for r in db.execute(tree.sql(dialect="sqlite"))]


def test_dual_time_axes_preserve_batch_lines_and_all_owners(receipts):
    rows = execute(receipts)
    assert {(r["cargo_owner"], r["inbound_code"], r["detail_id"]) for r in rows} == {
        ("A", "R1", "D1"), ("A", "R1", "D2"), ("B", "R1", "D1"), ("A", "REF_ONLY", "D-REF_ONLY"),
    }
    result = reconcile_inbound_details(rows, PLAN)
    assert result.row_count == 4
    assert result.totals_by_unit == {"10": Decimal("16.50")}
    assert [r["batch_number"] for r in result.rows if r["cargo_owner"] == "A" and r["inbound_code"] == "R1"] == ["B1", "B2"]


@pytest.mark.parametrize("basis,only_code", [
    ("purchase_order_reference", "REF_ONLY"), ("purchase_type", "TYPE_ONLY"), ("both", None),
])
def test_procurement_versions_are_explicit_not_interchangeable(receipts, basis, only_code):
    plan = replace(PLAN, purchase_basis=basis)
    result = reconcile_inbound_details(execute(receipts, plan), plan)
    assert {r["inbound_code"] for r in result.rows} == ({"R1", only_code} if only_code else {"R1"})


def test_arrival_cutoff_is_independent_of_production_end(receipts):
    plan = replace(PLAN, arrival_before=date(2026, 9, 9))
    rows = execute(receipts, plan)
    assert {r["inbound_code"] for r in rows} == {"REF_ONLY"}
    assert reconcile_inbound_details(rows, plan).row_count == 1


def test_unknown_warehouse_keeps_fact_without_guessing_name(receipts):
    receipts.execute("DELETE FROM ywzt_ods.master_wms WHERE cargo_owner_id='B'")
    result = reconcile_inbound_details(execute(receipts), PLAN)
    assert result.missing_warehouse_count == 1
    assert next(r for r in result.rows if r["cargo_owner"] == "B")["warehouse_name"] is None


def test_unit_totals_are_separate_and_never_assumed_to_be_ea(receipts):
    receipts.execute("UPDATE ywzt_ods.scm_warehous_detail SET unit='40' WHERE cargo_owner_id='B'")
    result = reconcile_inbound_details(execute(receipts), PLAN)
    assert result.totals_by_unit == {"10": Decimal("11.75"), "40": Decimal("4.75")}


@pytest.mark.parametrize("mutation", [
    "INSERT INTO ywzt_ods.scm_warehous_header SELECT cargo_owner_id,warehous_code,po_order_code,warehous_type,'2026-09-10 00:00:00',status,_fdl_marked_deleted FROM ywzt_ods.scm_warehous_header WHERE cargo_owner_id='B'",
    "INSERT INTO ywzt_ods.master_wms VALUES ('A','W1','Conflicting name','0')",
    "INSERT INTO ywzt_ods.scm_warehous_detail SELECT * FROM ywzt_ods.scm_warehous_detail WHERE id='D2'",
    "UPDATE ywzt_ods.scm_warehous_detail SET received_quantity=NULL WHERE id='D2'",
    "UPDATE ywzt_ods.scm_warehous_detail SET received_quantity=-1 WHERE id='D2'",
    "UPDATE ywzt_ods.scm_warehous_detail SET id='' WHERE id='D2'",
    "UPDATE ywzt_ods.scm_warehous_detail SET unit=NULL WHERE id='D2'",
])
def test_source_anomalies_are_not_silently_collapsed_or_zero_filled(receipts, mutation):
    receipts.execute(mutation)
    with pytest.raises(SemanticRuntimeError):
        reconcile_inbound_details(execute(receipts), PLAN)


@pytest.mark.parametrize("key,value", [
    ("matched_count", "4.5"), ("matched_count", "5"), ("unit_quantity_total", "999"),
    ("header_variants", "2"), ("detail_variants", "0"), ("warehouse_variants", "2"),
    ("production_date", "2026-09-10 00:00:00"), ("actual_arrival_date", "2026-09-10 00:00:00"),
    ("quantity", "NaN"), ("quantity", "-1"), ("quantity", None),
])
def test_corrupt_evidence_never_becomes_a_business_result(receipts, key, value):
    rows = execute(receipts)
    rows[0][key] = value
    with pytest.raises(SemanticRuntimeError):
        reconcile_inbound_details(rows, PLAN)


def test_row_cap_does_not_claim_complete(receipts):
    plan = replace(PLAN, page_limit=3)
    rows = execute(receipts, plan)
    assert len(rows) == 4
    with pytest.raises(SemanticRuntimeError, match="截断"):
        reconcile_inbound_details(rows, plan)
    with pytest.raises(SemanticRuntimeError):
        reconcile_inbound_details(rows[:2], PLAN)


def test_successful_empty_evidence_is_not_zero_quantity():
    result = reconcile_inbound_details([], PLAN)
    assert result.row_count == 0 and result.rows == () and result.totals_by_unit == {}


@pytest.mark.parametrize("kwargs", [
    {"purchase_basis": "unknown"}, {"purchase_basis": "10 OR 1=1"},
    {"production_end": date(2026, 9, 3)}, {"page_limit": 0}, {"page_limit": 10001},
])
def test_invalid_plans_fail_before_database_access(kwargs):
    with pytest.raises(ValueError):
        replace(PLAN, **kwargs)


def test_receipt_template_remains_inside_the_real_ast_gate():
    result = AdHocSqlValidator(allowed_schemas=("ywzt_ods",), max_rows=10000).validate(compile_inbound_details(PLAN))
    assert set(result.source_tables) == {
        "ywzt_ods.scm_warehous_header", "ywzt_ods.scm_warehous_detail", "ywzt_ods.master_wms",
    }


def test_scope_is_request_local_and_applies_owner_warehouse_sku(receipts):
    sql = compile_inbound_details(PLAN)
    scoped = apply_scope(sql, QueryScope(owner="B", warehouse_codes=("W1",), sku_codes=("S1",)), legacy_owner="HJXH")
    rows = [dict(row) for row in receipts.execute(sqlglot.transpile(scoped, read="doris", write="sqlite")[0])]
    assert len(rows) == 1 and rows[0]["cargo_owner"] == "B"
    assert len(execute(receipts)) == 4


def test_zero_received_quantity_is_a_real_line_not_no_data(receipts):
    receipts.execute("UPDATE ywzt_ods.scm_warehous_detail SET received_quantity=0 WHERE id='D2'")
    result = reconcile_inbound_details(execute(receipts), PLAN)
    assert result.row_count == 4
    assert next(row for row in result.rows if row["detail_id"] == "D2")["quantity"] == "0"


def test_header_conflict_outside_logical_filters_is_not_hidden(receipts):
    receipts.execute("INSERT INTO ywzt_ods.scm_warehous_header VALUES ('B','R1','P2','10','2026-09-09 23:59:59','1','true')")
    with pytest.raises(SemanticRuntimeError):
        reconcile_inbound_details(execute(receipts), PLAN)


def test_duplicate_detail_across_time_boundaries_is_not_hidden(receipts):
    receipts.execute("INSERT INTO ywzt_ods.scm_warehous_detail SELECT id,cargo_owner_id,warehous_code,item_code,wms_code,sku_code,batch_number,'2026-09-02 00:00:00',received_quantity,unit,status,_fdl_marked_deleted FROM ywzt_ods.scm_warehous_detail WHERE id='D2'")
    with pytest.raises(SemanticRuntimeError):
        reconcile_inbound_details(execute(receipts), PLAN)


def test_missing_production_date_does_not_fall_back_to_arrival_date(receipts):
    receipts.execute("UPDATE ywzt_ods.scm_warehous_detail SET production_date=NULL WHERE id='D2'")
    result = reconcile_inbound_details(execute(receipts), PLAN)
    assert result.row_count == 3 and all(row["detail_id"] != "D2" for row in result.rows)


def test_t03_contract_records_user_choice_without_merging_other_variants():
    root = Path(__file__).resolve().parents[2]
    contract = yaml.safe_load((root / "metrics/purchase.received_detail.yaml").read_text(encoding="utf-8"))
    assert contract["status"] == "active"
    assert contract["approval"]["state"] == "approved"
    assert contract["approval"]["approved_by"] == "current_project_requester"
    assert contract["parameters"]["purchase_basis"]["required"] is True
    assert contract["parameters"]["purchase_basis"]["default"] == "purchase_type"
    assert set(contract["parameters"]["purchase_basis"]["examples"]) == {
        "purchase_order_reference", "purchase_type", "both",
    }


@pytest.mark.parametrize("basis,label", [
    ("purchase_type", "入库类型为采购（10）"),
    ("purchase_order_reference", "采购单号非空"),
    ("both", "采购单号非空且入库类型为采购（10）"),
])
def test_receipt_handler_keeps_identity_precision_and_selected_definition(receipts, monkeypatch, basis, label):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.setattr("app.agent_core.handlers.receipt_detail.active_snapshot", lambda _: (root, None))
    receipts.execute("UPDATE ywzt_ods.scm_warehous_detail SET received_quantity=1.2345 WHERE id='D2'")
    plan = replace(PLAN, purchase_basis=basis)

    class Client:
        def query(self, *, sql, purpose):
            rows = [dict(row) for row in receipts.execute(sqlglot.transpile(sql, read="doris", write="sqlite")[0])]
            return {"status": "succeeded", "execution": {"executor_name": "doris", "is_mock": False, "rows": rows, "row_count": len(rows), "truncated": False}}

    session = QueryExecutionSession(Client(), QueryScope(), "HJXH")
    request = SemanticRequest("purchase_received_detail", plan.production_start, plan.production_end, receipt_plan=plan)
    answer = ReceiptDetailHandler(session, timezone_name="Asia/Shanghai").run(request)
    assert label in answer and "数据同步鲜度尚未确认" in answer
    public = _public_business_table(answer.business_table, live=True)
    assert public is not None and len(public["columns"]) == 12
    assert next(row for row in public["rows"] if row["detail_id"] == "D2")["received_quantity"] == "1.2345"
    assert len({(r["cargo_owner"], r["inbound_code"], r["detail_id"]) for r in public["rows"]}) == len(public["rows"])
    assert all("header_variants" not in row for row in public["rows"])
    assert all("unit" not in col for col in public["columns"] if col["key"] == "received_quantity")
    assert _public_business_table(answer.business_table, live=False) is None
    assert session.evidence()["live_business_result_executed"] is True


def test_empty_receipt_handler_never_fabricates_zero_inventory_or_quantity(monkeypatch):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.setattr("app.agent_core.handlers.receipt_detail.active_snapshot", lambda _: (root, None))

    class Client:
        def query(self, **_):
            return {"status": "succeeded", "execution": {"executor_name": "doris", "is_mock": False, "rows": [], "row_count": 0, "truncated": False}}

    session = QueryExecutionSession(Client(), QueryScope(), "HJXH")
    request = SemanticRequest("purchase_received_detail", PLAN.production_start, PLAN.production_end, receipt_plan=PLAN)
    answer = ReceiptDetailHandler(session, timezone_name="Asia/Shanghai").run(request)
    assert "没有匹配" in answer and not answer.business_table["rows"]
    assert session.evidence()["result_state"] == "no_data"

"""SO07 order-detail tests use an isolated synthetic order system."""

from dataclasses import replace
from datetime import date
from decimal import Decimal
import sqlite3

import pytest
import sqlglot

from app.agent_core.sales_order_details import (
    SalesOrderDetailPlan, compile_sales_order_details, reconcile_sales_order_details,
)
from app.agent_core.semantic_types import SemanticRuntimeError


PLAN = SalesOrderDetailPlan(date(2026, 9, 10), date(2026, 9, 11), order_type="SO07")


@pytest.fixture
def orders():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("ATTACH DATABASE ':memory:' AS ywzt_ods")
    db.executescript("""
    CREATE TABLE ywzt_ods.scm_sales_order_header (
      id TEXT, company_code TEXT, sales_order_code TEXT, facility_code TEXT,
      store_code TEXT, store_name TEXT, order_type TEXT, created TEXT,
      status TEXT, deleted_flag INTEGER, _fdl_marked_deleted TEXT, updated TEXT);
    CREATE TABLE ywzt_ods.scm_sales_order_detail (
      id TEXT, company_code TEXT, sales_order_code TEXT, facility_code TEXT,
      sku_code TEXT, sku_name TEXT, request_qty NUMERIC, status TEXT, _fdl_marked_deleted TEXT DEFAULT 'false',
      sales_order_id TEXT, updated TEXT, quantity_u_m TEXT, sales_unit TEXT);
    CREATE TABLE ywzt_ods.master_wms (
      cargo_owner_id TEXT, wms_code TEXT, wms_desc TEXT, status TEXT);
    """)
    headers = [
        ("H1", "A", "SO1", "W1", "C1", "Customer A", "SO07", "2026-09-10 01:00:00", "10", 0, "false"),
        ("H1", "A", "SO1", "W1", "C1", "Customer A", "SO07", "2026-09-10 01:00:00", "10", 0, "false"),
        ("H2", "A", "SO2", "W2", "C2", "Customer B", "SO07", "2026-09-10 23:59:59", "20", 0, "false"),
        ("H3", "A", "JY1", "W1", "C1", "Customer A", "JYCK", "2026-09-10 02:00:00", "10", 0, "false"),
        ("H4", "A", "OLD", "W1", "C1", "Customer A", "SO07", "2026-09-09 23:59:59", "10", 0, "false"),
        ("H5", "B", "SO3", "W1", "C3", "Customer C", "SO07", "2026-09-10 03:00:00", "10", 0, "false"),
        ("H6", "A", "DEL", "W1", "C1", "Customer A", "SO07", "2026-09-10 04:00:00", "10", 1, "false"),
    ]
    db.executemany("INSERT INTO ywzt_ods.scm_sales_order_header (id,company_code,sales_order_code,facility_code,store_code,store_name,order_type,created,status,deleted_flag,_fdl_marked_deleted) VALUES (?,?,?,?,?,?,?,?,?,?,?)", headers)
    details = [
        ("D1", "A", "SO1", "W1", "S1", "SKU 1", "2.5", "0"),
        ("D2", "A", "SO1", "W1", "S2", "SKU 2", "3", "0"),
        ("D3", "A", "SO2", "W2", "S3", "SKU 3", "0", "0"),
        ("D4", "A", "JY1", "W1", "S4", "SKU 4", "99", "0"),
        ("D5", "A", "OLD", "W1", "S5", "SKU 5", "99", "0"),
        ("D6", "B", "SO3", "W1", "S6", "SKU 6", "4", "0"),
        ("D7", "A", "DEL", "W1", "S7", "SKU 7", "99", "0"),
        ("D8", "A", "SO1", "W1", "S8", "SKU 8", "100", "1"),
    ]
    db.executemany("INSERT INTO ywzt_ods.scm_sales_order_detail (id,company_code,sales_order_code,facility_code,sku_code,sku_name,request_qty,status) VALUES (?,?,?,?,?,?,?,?)", details)
    db.executemany("INSERT INTO ywzt_ods.master_wms VALUES (?,?,?,'0')", [
        ("A", "W1", "Warehouse 1"), ("A", "W2", "Warehouse 2"), ("B", "W1", "Warehouse B"),
    ])
    yield db
    db.close()


def execute(db, plan=PLAN):
    tree = sqlglot.parse_one(compile_sales_order_details(plan), read="doris")
    return [dict(row) for row in db.execute(tree.sql(dialect="sqlite"))]


def test_so07_filters_order_type_on_header_and_does_not_need_shipments(orders):
    rows = execute(orders)
    assert {(r["cargo_owner"], r["sales_order_code"], r["detail_id"]) for r in rows} == {
        ("A", "SO1", "D1"), ("A", "SO1", "D2"), ("A", "SO2", "D3"), ("B", "SO3", "D6"),
    }
    result = reconcile_sales_order_details(rows, PLAN)
    assert result.row_count == 4
    assert result.order_count == 3
    assert result.total_request_quantity == Decimal("9.5")
    assert all(row["order_type"] == "SO07" for row in result.rows)


def test_order_created_time_is_natural_day_and_status_is_only_displayed(orders):
    rows = execute(orders)
    assert all("2026-09-10" in r["order_created_at"] for r in rows)
    assert {r["order_status"] for r in rows} == {"10", "20"}


def test_cross_owner_order_codes_remain_separate(orders):
    rows = execute(orders)
    assert {r["cargo_owner"] for r in rows} == {"A", "B"}
    assert {r["total_order_count"] for r in rows} == {3}


@pytest.mark.parametrize("mutation", [
    "INSERT INTO ywzt_ods.scm_sales_order_header SELECT 'H-conf',company_code,sales_order_code,facility_code,store_code,'Changed customer','SO07',created,status,deleted_flag,_fdl_marked_deleted,updated FROM ywzt_ods.scm_sales_order_header WHERE sales_order_code='SO1'",
    "INSERT INTO ywzt_ods.scm_sales_order_detail SELECT * FROM ywzt_ods.scm_sales_order_detail WHERE id='D1'",
    "UPDATE ywzt_ods.scm_sales_order_detail SET request_qty=NULL WHERE id='D1'",
    "UPDATE ywzt_ods.scm_sales_order_detail SET request_qty=-1 WHERE id='D1'",
    "UPDATE ywzt_ods.scm_sales_order_detail SET id='' WHERE id='D1'",
    "INSERT INTO ywzt_ods.master_wms VALUES ('A','W1','Conflicting','0')",
])
def test_order_anomalies_block_result(mutation, orders):
    orders.execute(mutation)
    with pytest.raises(SemanticRuntimeError):
        reconcile_sales_order_details(execute(orders), PLAN)


def test_scope_is_rebuilt_for_each_order_request(orders):
    scoped_plan = replace(PLAN, owner="B", warehouse_codes=("W1",), sku_codes=("S6",))
    rows = execute(orders, scoped_plan)
    assert len(rows) == 1 and rows[0]["cargo_owner"] == "B" and rows[0]["sku_code"] == "S6"
    assert len(execute(orders, PLAN)) == 4


def test_zero_requested_quantity_is_retained(orders):
    result = reconcile_sales_order_details(execute(orders), PLAN)
    assert next(r for r in result.rows if r["detail_id"] == "D3")["request_quantity"] == "0"


def test_empty_success_is_not_a_connection_failure():
    result = reconcile_sales_order_details([], PLAN)
    assert result.row_count == 0 and result.order_count == 0


def test_truncated_order_detail_is_never_complete(orders):
    plan = replace(PLAN, page_limit=3)
    with pytest.raises(SemanticRuntimeError, match="截断"):
        reconcile_sales_order_details(execute(orders, plan), plan)


def test_order_without_any_active_detail_is_retained_with_null_not_zero(orders):
    orders.execute("DELETE FROM ywzt_ods.scm_sales_order_detail WHERE sales_order_code='SO2'")
    result = reconcile_sales_order_details(execute(orders), PLAN)
    order = next(r for r in result.rows if r["sales_order_code"] == "SO2")
    assert order["request_quantity"] is None and order["sku_code"] is None
    assert result.order_count == 3


def test_order_detail_can_have_warehouse_different_from_header(orders):
    orders.execute("UPDATE ywzt_ods.scm_sales_order_detail SET facility_code='W2' WHERE id='D1'")
    result = reconcile_sales_order_details(execute(orders), PLAN)
    assert next(r for r in result.rows if r["detail_id"] == "D1")["warehouse_code"] == "W2"


def test_deleted_detail_does_not_delete_its_order(orders):
    orders.execute("UPDATE ywzt_ods.scm_sales_order_detail SET _fdl_marked_deleted='true' WHERE sales_order_code='SO2'")
    result = reconcile_sales_order_details(execute(orders), PLAN)
    assert next(r for r in result.rows if r["sales_order_code"] == "SO2")["request_quantity"] is None

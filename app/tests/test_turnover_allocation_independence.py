"""Turnover must not depend on quantities whose unit relationship is unapproved."""

from datetime import date
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
import sqlglot
from sqlglot import exp

from app.agent_core.handlers.turnover import TurnoverHandler
from app.agent_core.semantic_types import SemanticRequest, SemanticRuntimeError
from app.agent_core.runtime import SemanticQueryRuntime
from app.query_scope import QueryScope
from app.tests.test_semantic_runtime import RecordingQueryClient


START, END = date(2026, 8, 1), date(2026, 9, 1)


@pytest.fixture(params=["historical", "realtime"])
def variant(request):
    return request.param


def handler(rows=()):
    return TurnoverHandler(SimpleNamespace(legacy_owner="HJXH", query=lambda *a, **kw: rows),
                           timezone_name="Asia/Shanghai")


def compiled(variant):
    instance = handler()
    return (instance._turnover_sql if variant == "historical" else
            instance._realtime_turnover_sql)(START, END)


def source_row(variant):
    prefix = "snapshot" if variant == "historical" else "inventory"
    return {f"{prefix}_ea": "100", f"{prefix}_key_count": "1",
            f"{prefix}_warehouse_count": "1", f"negative_{prefix}_key_count": "0",
            "inventory_latest_freshness": "2026-09-11 09:00:00",
            "null_sales_quantity_count": "0", "negative_sales_quantity_count": "0",
            "sales_ea": "31", "complete_natural_days": "31", "turnover_days": "100"}


def test_turnover_sql_does_not_query_or_compare_allocation(variant):
    tree = sqlglot.parse_one(compiled(variant), read="doris")
    assert "scm_sales_shipment_allocate_wms" not in {t.name for t in tree.find_all(exp.Table)}
    assert not {"allocate_qty", "allocation_ea", "reconciliation_difference"} & {
        c.name for c in tree.find_all(exp.Column)}


@pytest.mark.parametrize("mutation,quantity,nulls,negatives", [
    ("", 31, 0, 0),
    ("UPDATE ywzt_ods.scm_sales_shipment_header SET _fdl_marked_deleted=NULL", 31, 0, 0),
    ("UPDATE ywzt_ods.scm_sales_shipment_detail SET _fdl_marked_deleted=NULL", 31, 0, 0),
    ("UPDATE ywzt_ods.scm_sales_order_header SET _fdl_marked_deleted=NULL", 31, 0, 0),
    ("UPDATE ywzt_ods.scm_sales_shipment_header SET _fdl_marked_deleted='true'", 0, 0, 0),
    ("UPDATE ywzt_ods.scm_sales_shipment_detail SET _fdl_marked_deleted='true'", 0, 0, 0),
    ("UPDATE ywzt_ods.scm_sales_order_header SET _fdl_marked_deleted='true'", 0, 0, 0),
    ("UPDATE ywzt_ods.scm_sales_shipment_header SET delete_flag=1", 0, 0, 0),
    ("UPDATE ywzt_ods.scm_sales_shipment_detail SET delete_flag=1", 0, 0, 0),
    ("UPDATE ywzt_ods.scm_sales_order_header SET deleted_flag=1", 0, 0, 0),
    ("INSERT INTO ywzt_ods.scm_sales_shipment_detail VALUES('O1','OTHER','A',999,0,'false')", 31, 0, 0),
    ("INSERT INTO ywzt_ods.scm_sales_order_header SELECT * FROM ywzt_ods.scm_sales_order_header", 31, 0, 0),
    ("UPDATE ywzt_ods.scm_sales_shipment_detail SET ship_qty=NULL", 0, 1, 0),
    ("INSERT INTO ywzt_ods.scm_sales_shipment_detail VALUES('O1','HJXH','A',-31,0,'false')", 0, 0, 1),
])
def test_actual_sql_executes_without_any_allocation_table(variant, mutation, quantity, nulls, negatives):
    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        db.executescript("""
        ATTACH DATABASE ':memory:' AS ywzt_ods;
        ATTACH DATABASE ':memory:' AS information_schema;
        CREATE TABLE information_schema.tables(table_schema,table_name,update_time);
        INSERT INTO information_schema.tables VALUES('ywzt_ods','sku_inventory','2026-09-11 09:00:00');
        CREATE TABLE ywzt_ods.master_sku(sku_code,sku_group,status,updated_date,version,id);
        INSERT INTO ywzt_ods.master_sku VALUES('A','50','0','2026-09-01',1,1);
        CREATE TABLE ywzt_ods.sku_inventory(owner,wms_desc,sku_code,available_quantity,in_transit_quantity);
        INSERT INTO ywzt_ods.sku_inventory VALUES('HJXH','W1','A',80.0,20.0);
        CREATE TABLE ywzt_ods.scm_warehous_manage_backups(cargo_owner_id,wms_code,sku_code,
          in_stock_quantity,backups_date,_fdl_marked_deleted);
        INSERT INTO ywzt_ods.scm_warehous_manage_backups VALUES('HJXH','W1','A',100.0,'2026-08-31','false');
        CREATE TABLE ywzt_ods.scm_sales_shipment_header(outbound_code,company_code,facility_code,
          updated,version,id,ship_at,status,delete_flag,_fdl_marked_deleted,ref_order_type,ref_order_code);
        INSERT INTO ywzt_ods.scm_sales_shipment_header VALUES
          ('O1','HJXH','W1','2026-08-10',1,1,'2026-08-10',700,0,'false','JYCK','S1');
        CREATE TABLE ywzt_ods.scm_sales_order_header(sales_order_code,company_code,deleted_flag,_fdl_marked_deleted);
        INSERT INTO ywzt_ods.scm_sales_order_header VALUES('S1','HJXH',0,'false');
        CREATE TABLE ywzt_ods.scm_sales_shipment_detail(outbound_code,company_code,sku_code,
          ship_qty,delete_flag,_fdl_marked_deleted);
        INSERT INTO ywzt_ods.scm_sales_shipment_detail VALUES('O1','HJXH','A',31.0,0,'false');
        """)
        if mutation:
            db.execute(mutation)
        rows = list(db.execute(sqlglot.transpile(compiled(variant), read="doris", write="sqlite")[0]))
        assert len(rows) == 1
        assert rows[0]["sales_ea"] == quantity
        assert rows[0]["turnover_days"] == (100 if quantity else None)
        assert rows[0]["null_sales_quantity_count"] == nulls
        assert rows[0]["negative_sales_quantity_count"] == negatives
        request = SemanticRequest(f"{variant}_turnover", START, END)
        execute = getattr(handler([dict(rows[0])]), f"_run_{variant}_turnover")
        if nulls or negatives:
            with pytest.raises(SemanticRuntimeError):
                execute(request)
        else:
            answer = execute(request)
            assert ("无动销" in answer) == (quantity == 0)


@pytest.mark.parametrize("alias", ["h", "d", "so"])
def test_v2_template_preserves_approved_null_marker_semantics(alias):
    template = Path(__file__).resolve().parents[2] / "sql_templates/sales.outbound_actual.v2.sql"
    tree = sqlglot.parse_one(template.read_text(encoding="utf-8"), read="doris")
    marker = next(c for c in tree.find_all(exp.Column)
                  if c.name == "_fdl_marked_deleted" and c.table == alias)
    assert isinstance(marker.parent, exp.Coalesce)
    predicate = marker.parent.parent
    assert isinstance(predicate, exp.EQ)
    assert predicate.expression == exp.Literal.string("false")


def test_checked_answer_requires_no_allocation_payload(variant):
    request = SemanticRequest(f"{variant}_turnover", START, END)
    answer = getattr(handler([source_row(variant)]), f"_run_{variant}_turnover")(request)
    assert "100" in answer
    assert "分配" not in answer


def test_wrong_turnover_formula_still_fails(variant):
    row = source_row(variant)
    row["turnover_days"] = "99"
    request = SemanticRequest(f"{variant}_turnover", START, END)
    with pytest.raises(SemanticRuntimeError) as failure:
        getattr(handler([row]), f"_run_{variant}_turnover")(request)
    assert failure.value.reason.endswith("formula_mismatch")


def test_runtime_scope_reset_and_public_table_without_allocation(variant, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.setattr("app.query_scope.active_snapshot", lambda _: (root, None))
    purpose = ("historical finished-goods inventory turnover" if variant == "historical"
               else "current real-time finished-goods inventory turnover")
    client = RecordingQueryClient({purpose: [source_row(variant)]})
    runtime = SemanticQueryRuntime(query_client=client)
    for owner in ("HJXH", "ALL"):
        request = SemanticRequest(f"{variant}_turnover", START, END, scope=QueryScope(owner=owner))
        result = runtime.execute_plan(request, question="库存周转", context_reused=True)
        assert result["result_state"] == "live_success"
        assert result["query_plan"]["scope"]["owner"] == owner
        assert result["business_table"]["table_id"] == f"{variant}_inventory_turnover"
        assert result["query_execution"]["query_call_count"] == 1
        assert "allocation" not in client.calls[-1]["sql"]
        assert "分配" not in result["final_response"]
    assert "HJXH" in client.calls[0]["sql"]
    assert "HJXH" not in client.calls[1]["sql"]

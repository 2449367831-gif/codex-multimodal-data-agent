"""Channel classification must preserve quantities before any box rounding."""

from datetime import date
from decimal import Decimal

import pytest
import sqlglot

from app.tests.test_sales_aggregation import facts
from app.agent_core.sales_aggregation import SalesAggregation, compile_sales_aggregation
from app.query_scope import QueryScope, apply_scope


@pytest.fixture
def channels(facts):
    facts.execute("ALTER TABLE ywzt_ods.scm_sales_shipment_header ADD COLUMN store_code TEXT")
    facts.execute("UPDATE ywzt_ods.scm_sales_shipment_header SET store_code='C1'")
    facts.execute("CREATE TABLE ywzt_ods.master_store (company_code TEXT, code TEXT, type TEXT)")
    facts.executemany("INSERT INTO ywzt_ods.master_store VALUES (?,?,?)", [("HJXH","C1","Z004"),("HJXH","C1","Z004"),("B","C1","Z001")])
    return facts


def execute_channels(db):
    plan = SalesAggregation(("ship_date","warehouse","sku","customer_channel"))
    sql = apply_scope(compile_sales_aggregation(plan,start=date(2026,9,9),end=date(2026,9,10)),QueryScope(finished_goods=True),legacy_owner="HJXH")
    return [dict(r) for r in db.execute(sqlglot.transpile(sql,read="doris",write="sqlite")[0])]


def test_channel_filter_is_applied_before_controls_and_totals(channels):
    plan = SalesAggregation(("ship_date", "warehouse", "sku", "customer_channel"), channel_filter="线上")
    sql = apply_scope(
        compile_sales_aggregation(plan, start=date(2026, 9, 9), end=date(2026, 9, 10)),
        QueryScope(finished_goods=True),
        legacy_owner="HJXH",
    )
    rows = [dict(r) for r in channels.execute(sqlglot.transpile(sql, read="doris", write="sqlite")[0])]
    assert rows and {row["customer_channel"] for row in rows} == {"线上"}
    assert sum(Decimal(str(row["quantity"])) for row in rows) == Decimal("3.75")


def test_duplicate_customers_and_other_owner_same_code_do_not_multiply(channels):
    rows = execute_channels(channels)
    assert sum(Decimal(str(r["quantity"])) for r in rows) == Decimal("13.75")
    assert {r["customer_channel"] for r in rows if r["cargo_owner"] == "HJXH"} == {"线上"}
    assert {r["customer_channel"] for r in rows if r["cargo_owner"] == "B"} == {"线下"}
    assert all(r["customer_conflict_count"] == 0 for r in rows)


def test_missing_customer_preserved_as_other_with_impact(channels):
    channels.execute("DELETE FROM ywzt_ods.master_store WHERE company_code='B'")
    rows = execute_channels(channels)
    other = next(r for r in rows if r["cargo_owner"] == "B")
    assert other["customer_channel"] == "其他" and other["quantity"] == 10
    assert all(r["missing_customer_quantity"] == 10 for r in rows)


def test_conflicting_classification_is_not_resolved_by_min(channels):
    channels.execute("INSERT INTO ywzt_ods.master_store VALUES ('HJXH','C1','Z001')")
    rows = execute_channels(channels)
    assert all(r["customer_conflict_count"] > 0 for r in rows)
    assert sum(Decimal(str(r["quantity"])) for r in rows) == Decimal("13.75")


def test_header_customer_conflict_detected_before_channel_grouping(channels):
    channels.execute("INSERT INTO ywzt_ods.scm_sales_shipment_header SELECT company_code,outbound_code,facility_code,ship_at,status,ref_order_type,ref_order_code,terms_of_shipment,delete_flag,_fdl_marked_deleted,'DIFFERENT' FROM ywzt_ods.scm_sales_shipment_header WHERE outbound_code='O2'")
    assert all(r["header_conflict_count"] > 0 for r in execute_channels(channels))


def test_empty_customer_code_never_matches_empty_master_record(channels):
    channels.execute("UPDATE ywzt_ods.scm_sales_shipment_header SET store_code='' WHERE company_code='B'")
    channels.execute("INSERT INTO ywzt_ods.master_store VALUES ('B','','Z004')")
    rows=execute_channels(channels)
    assert next(row for row in rows if row["cargo_owner"]=="B")["customer_channel"] == "其他"
    assert all(row["missing_customer_quantity"] == 10 for row in rows)

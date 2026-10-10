"""The live verifier must detect wrong counts, windows and corrupted values."""

from copy import deepcopy
from datetime import date
from decimal import Decimal
from pathlib import Path
import runpy

import pytest
import sqlglot

from app.adhoc_sql import AdHocSqlValidator
from app.tests.test_sales_aggregation import facts


START, END = date(2026, 9, 9), date(2026, 9, 10)


@pytest.fixture(scope="module")
def verifier():
    return runpy.run_path(str(Path(__file__).resolve().parents[2] / "tests/acceptance/check_sales_detail_windows_live.py"))


def read_control(verifier, facts):
    sql = verifier["independent_control_sql"](START, END)
    rows = [dict(row) for row in facts.execute(sqlglot.transpile(sql, read="doris", write="sqlite")[0])]
    return verifier["reference"](rows)


def test_reference_uses_actual_quantities_and_owner_qualified_document_counts(verifier, facts):
    result = read_control(verifier, facts)
    assert result["quantity"] == Decimal("18")
    assert result["document_count"] == 4 and result["detail_count"] == 5
    assert result["latest_shipment_at"] == "2026-09-09 23:59:59"


def test_reference_scope_is_explicit_and_does_not_drop_zero_quantity_documents(verifier, facts):
    sql = verifier["independent_control_sql"](START, END, warehouse="W1")
    rows = [dict(row) for row in facts.execute(sqlglot.transpile(sql, read="doris", write="sqlite")[0])]
    assert verifier["reference"](rows)["document_count"] == 3
    assert read_control(verifier, facts)["document_count"] == 4


def test_public_row_must_match_explicit_warehouse(verifier):
    assert verifier["verify_rows"](sample(), control(), start=START, end=END, warehouse="W1")
    with pytest.raises(ValueError):
        verifier["verify_rows"](sample(), control(), start=START, end=END, warehouse="W2")


@pytest.mark.parametrize("mutation", [
    "UPDATE ywzt_ods.scm_sales_shipment_detail SET ship_qty=NULL WHERE outbound_code='O2'",
    "UPDATE ywzt_ods.scm_sales_shipment_detail SET ship_qty=-1 WHERE outbound_code='O2'",
    "UPDATE ywzt_ods.scm_sales_shipment_detail SET sku_code='' WHERE outbound_code='O2'",
    "UPDATE ywzt_ods.scm_sales_shipment_header SET facility_code='' WHERE outbound_code='O2'",
    "INSERT INTO ywzt_ods.scm_sales_shipment_header SELECT company_code,outbound_code,'OTHER',ship_at,status,ref_order_type,ref_order_code,terms_of_shipment,delete_flag,_fdl_marked_deleted FROM ywzt_ods.scm_sales_shipment_header WHERE outbound_code='O2'",
])
def test_source_quality_failure_is_not_accepted_as_an_independent_baseline(verifier, facts, mutation):
    facts.execute(mutation)
    with pytest.raises(ValueError):
        read_control(verifier, facts)


def sample():
    return [dict(cargo_owner=owner, outbound_code="SAME", sku_code="SKU1", warehouse_code="W1",
                 ship_at="2026-09-09 12:00:00", quantity="1.234567890123456789012345678901") for owner in ("A", "B")]


def control():
    return {"quantity": Decimal("2.469135780246913578024691357802"), "detail_count": 2, "document_count": 2}


def test_complete_row_check_keeps_exact_decimals_and_compound_document_identity(verifier):
    assert verifier["verify_rows"](sample(), control(), start=START, end=END)
    assert verifier["verify_rows"]([], {"quantity": Decimal(0), "detail_count": 0, "document_count": 0}, start=START, end=END)


@pytest.mark.parametrize("mutation", ["quantity", "duplicate", "owner", "missing", "midnight", "blank_warehouse", "negative", "null"])
def test_wrong_or_partial_public_rows_cannot_pass(verifier, mutation):
    rows = deepcopy(sample())
    if mutation == "quantity": rows[0]["quantity"] = "1.234567890123456789012345678900"
    if mutation == "duplicate": rows[1] = dict(rows[0])
    if mutation == "owner": rows[1]["cargo_owner"] = "A"
    if mutation == "missing": rows.pop()
    if mutation == "midnight": rows[0]["ship_at"] = "2026-09-10 00:00:00"
    if mutation == "blank_warehouse": rows[0]["warehouse_code"] = " "
    if mutation == "negative": rows[0]["quantity"] = "-1"
    if mutation == "null": rows[0]["quantity"] = None
    try:
        passed = verifier["verify_rows"](rows, control(), start=START, end=END)
    except ValueError:
        passed = False
    assert passed is False


def test_independent_reference_still_passes_the_doris_ast_gate(verifier):
    result = AdHocSqlValidator(allowed_schemas=("ywzt_ods",), max_rows=10000).validate(verifier["independent_control_sql"](START, END))
    assert set(result.source_tables) == {"ywzt_ods.scm_sales_shipment_header", "ywzt_ods.scm_sales_shipment_detail", "ywzt_ods.scm_sales_order_header"}


def test_public_capacity_summary_uses_screened_columns_not_private_table_id(verifier):
    table = {"format": "table", "columns": [{"key": "quantity"}, {"key": "outbound_count"}],
             "rows": [{"quantity": "10", "outbound_count": "2"}]}
    assert verifier["is_capacity_summary"](table, {"detail_count": 100001})
    assert not verifier["is_capacity_summary"](table, {"detail_count": 100000})
    assert not verifier["is_capacity_summary"]({**table, "pagination": {"page_count": 3}}, {"detail_count": 100001})
    assert not verifier["is_capacity_summary"]({**table, "columns": [{"key": "sku_code"}, {"key": "quantity"}]}, {"detail_count": 100001})

"""Synthetic facts prove the independent acceptance checks before live use."""

from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
import sqlite3

import pytest
import sqlglot

from scripts.evaluate_sales_semantics import cases, compare, complete_table, independent_sql, reference_values


@pytest.fixture
def facts():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("ATTACH DATABASE ':memory:' AS ywzt_ods")
    db.execute("CREATE TABLE ywzt_ods.scm_sales_shipment_header (company_code TEXT,outbound_code TEXT,ship_at TEXT,status INTEGER,facility_code TEXT,ref_order_type TEXT,ref_order_code TEXT,terms_of_shipment TEXT,delete_flag INTEGER,_fdl_marked_deleted TEXT)")
    db.execute("CREATE TABLE ywzt_ods.scm_sales_order_header (company_code TEXT,sales_order_code TEXT,deleted_flag INTEGER,_fdl_marked_deleted TEXT)")
    db.execute("CREATE TABLE ywzt_ods.scm_sales_shipment_detail (company_code TEXT,outbound_code TEXT,sku_code TEXT,ship_qty NUMERIC,delete_flag INTEGER,_fdl_marked_deleted TEXT)")
    db.execute("CREATE TABLE ywzt_ods.master_sku (sku_code TEXT,sku_group TEXT)")

    def add(owner, document, quantity, *, status=700, kind="JYCK", sku="F", when="2026-09-01 10:00:00", deleted=0):
        db.execute("INSERT INTO ywzt_ods.scm_sales_shipment_header VALUES (?,?,?,?,?,?,?,?,?,?)",
            (owner, document, when, status, "W", kind, "S" + document, "20", deleted, "false"))
        db.execute("INSERT INTO ywzt_ods.scm_sales_order_header VALUES (?,?,0,'false')", (owner, "S" + document))
        db.execute("INSERT INTO ywzt_ods.scm_sales_shipment_detail VALUES (?,?,?,?,0,'false')", (owner, document, sku, quantity))

    yield db, add
    db.close()


def query(db, case):
    sql = sqlglot.transpile(independent_sql(case), read="doris", write="sqlite")[0]
    return [dict(row) for row in db.execute(sql)]


def test_reference_keeps_zero_documents_owner_identity_and_all_sales_types(facts):
    db, add = facts
    add("A", "same", 10)
    add("B", "same", 0, status=701, kind="HHCK")
    add("A", "cancel_zero", 0, status=901, kind="BFCK")
    add("A", "other", 4, status=900, kind="QTCK")
    add("A", "social", 6, status=700, kind="SO07")
    add("A", "excluded_status", 999, status=600)
    add("A", "excluded_type", 999, kind="DBCK")
    add("A", "deleted", 999, deleted=1)
    db.execute("INSERT INTO ywzt_ods.scm_sales_shipment_header SELECT * FROM ywzt_ods.scm_sales_shipment_header WHERE outbound_code='same'")
    db.execute("INSERT INTO ywzt_ods.scm_sales_order_header SELECT * FROM ywzt_ods.scm_sales_order_header")
    rows = query(db, cases(date(2026, 9, 2))[0])
    assert reference_values(rows) == {
        "700": (Decimal(16), Decimal(2), Decimal(0), Decimal(1)),
        "701": (Decimal(0), Decimal(1), Decimal(1), Decimal(1)),
        "900": (Decimal(4), Decimal(1), Decimal(0), Decimal(1)),
        "901": (Decimal(0), Decimal(1), Decimal(1), Decimal(1)),
    }


def test_reference_union_rejects_first_type_only_answers(facts):
    db, add = facts
    for kind, qty in [("JYCK", 10), ("HHCK", 2), ("BFCK", 0), ("QTCK", 3), ("SO07", 999)]:
        add("A", kind, qty, kind=kind)
    case = cases(date(2026, 9, 2))[4]
    reference = query(db, case)
    assert set(reference_values(reference)) == set(case.order_types)
    table = {"columns": [{"key": "a", "label": "出库类型"}, {"key": "b", "label": "实发数量"}, {"key": "c", "label": "出库单数"}],
        "rows": [{"a": row["group_key"], "b": row["quantity"], "c": row["outbound_count"]} for row in reference]}
    assert all(compare(case, {"result_state": "live_success"}, table, reference).values())
    table["rows"] = [row for row in table["rows"] if row["a"] == "JYCK"]
    assert not all(compare(case, {"result_state": "live_success"}, table, reference).values())


def test_reference_exception_union_includes_missing_master_and_does_not_multiply(facts):
    db, add = facts
    for sku, group in [("F", "50"), ("F", "50"), ("5101010010008", "60"), ("N", "60")]:
        db.execute("INSERT INTO ywzt_ods.master_sku VALUES (?,?)", (sku, group))
    for index, sku in enumerate(["F", "5101010010008", "5101010010009", "N"]):
        add("A", f"doc{index}", index + 1, sku=sku)
    value = reference_values(query(db, cases(date(2026, 9, 2))[1]))
    assert set(value) == {"F", "5101010010008", "5101010010009"}
    assert sum(row[0] for row in value.values()) == 6


def test_reference_observes_both_intraday_boundaries_and_unique_skus(facts):
    db, add = facts
    add("A", "start", 5, when="2026-09-01 08:30:00")
    add("A", "before", 999, when="2026-09-01 08:29:59")
    add("A", "end", 999, when="2026-09-02 12:00:00")
    add("A", "inside", 7, when="2026-09-02 11:59:59", sku="G")
    add("B", "sharedsku", 3, when="2026-09-02 11:59:59")
    value = reference_values(query(db, cases(date(2026, 9, 2))[3]))
    assert value == {"JYCK": (Decimal(15), Decimal(3), Decimal(0), Decimal(2))}


def test_reference_excludes_the_exact_midnight_cutoff(facts):
    db, add = facts
    add("A", "first", 5, when="2026-09-01 00:00:00")
    add("A", "last", 7, when="2026-09-09 23:59:59")
    add("A", "end", 999, when="2026-09-10 00:00:00")
    value = reference_values(query(db, cases(date(2026, 9, 2))[2]))
    assert set(value) == {"2026-09-01", "2026-09-09"}
    assert sum(row[0] for row in value.values()) == 12


@pytest.mark.parametrize("problem", ["missing_quantity", "negative_quantity", "conflicting_header", "missing_owner"])
def test_reference_does_not_certify_uncertain_source_values(facts, problem):
    db, add = facts
    add("" if problem == "missing_owner" else "A", "doc", None if problem == "missing_quantity" else -2 if problem == "negative_quantity" else 2)
    if problem == "conflicting_header":
        db.execute("INSERT INTO ywzt_ods.scm_sales_shipment_header SELECT company_code,outbound_code,ship_at,status,'different',ref_order_type,ref_order_code,terms_of_shipment,delete_flag,_fdl_marked_deleted FROM ywzt_ods.scm_sales_shipment_header")
    with pytest.raises(ValueError):
        reference_values(query(db, cases(date(2026, 9, 2))[0]))


def public_case():
    case = cases(date(2026, 9, 2))[0]
    table = {"columns": [{"key": k, "label": label} for k, label in
        [("a", "出库状态"), ("b", "实发数量"), ("c", "出库单数"), ("d", "零数量出库单数")]],
        "rows": [{"a": "701", "b": "0", "c": "2", "d": "2"}]}
    ref = [{"group_key": "701", "quantity": 0, "outbound_count": 2, "zero_documents": 2, "sku_count": 1, "invalid_rows": 0}]
    return case, {"result_state": "live_success"}, table, ref


def test_zero_quantity_business_result_is_not_no_data():
    assert all(compare(*public_case()).values())


def test_zero_document_column_alias_keeps_numeric_validation():
    case, result, table, ref = public_case()
    table["columns"][-1]["label"] = "零实发单数"
    assert all(compare(case, result, table, ref).values())
    table["rows"][0]["d"] = "1"
    assert not all(compare(case, result, table, ref).values())


@pytest.mark.parametrize("problem", ["missing_zero", "wrong_quantity", "wrong_count", "wrong_state", "wrong_group", "duplicate_group", "leak"])
def test_public_acceptance_rejects_semantic_counterexamples(problem):
    case, result, table, ref = deepcopy(public_case())
    if problem == "missing_zero": table["columns"].pop()
    elif problem == "wrong_quantity": table["rows"][0]["b"] = "1"
    elif problem == "wrong_count": table["rows"][0]["c"] = "1"
    elif problem == "wrong_state": result["result_state"] = "no_data"
    elif problem == "wrong_group": table["rows"][0]["a"] = "700"
    elif problem == "duplicate_group": table["rows"] *= 2
    elif problem == "leak": result["answer"] = "SELECT secret FROM ywzt_ods.private"
    assert not all(compare(case, result, table, ref).values())


def test_reference_capacity_and_missing_control_cannot_be_ignored():
    rows = public_case()[-1]
    with pytest.raises(ValueError): reference_values(rows * 1001)
    rows[0]["invalid_rows"] = None
    with pytest.raises(ValueError): reference_values(rows)


def test_bounded_page_reader_requires_matching_identity_and_full_coverage():
    from unittest.mock import Mock
    first = {"title": "销售", "columns": [{"key": "a", "label": "数量"}], "rows": [{"a": "1"}],
        "pagination": {"page": 1, "page_count": 2, "total_rows": 2, "result_id": "reference", "start_row": 1}}
    second = {**first, "rows": [{"a": "2"}], "pagination": {**first["pagination"], "page": 2, "start_row": 2}}
    client = Mock()
    client.request.return_value = {"business_table": second}
    assert complete_table(client, first)["rows"] == [{"a": "1"}, {"a": "2"}]
    second["pagination"]["result_id"] = "another"
    with pytest.raises(ValueError): complete_table(client, first)


@pytest.mark.parametrize("field,value", [("unit", "box"), ("formula", "SUM(d.allocate_qty)"), ("version", "3.0.0"), ("status", "candidate")])
def test_reference_must_follow_the_released_authority(field, value):
    from pathlib import Path
    import yaml
    from scripts.evaluate_sales_semantics import validate_reference_authority
    root = Path(__file__).resolve().parents[2]
    metric = yaml.safe_load((root / "metrics/sales.outbound_actual.v2.yaml").read_text(encoding="utf-8"))
    exceptions = yaml.safe_load((root / "metrics/sales.finished_goods_exceptions.yaml").read_text(encoding="utf-8"))
    validate_reference_authority(metric, exceptions)
    metric[field] = value
    with pytest.raises(ValueError): validate_reference_authority(metric, exceptions)


def test_released_outbound_contract_resolves_fulfillment_type_without_channel_guessing():
    from pathlib import Path
    import yaml

    root = Path(__file__).resolve().parents[2]
    metric = yaml.safe_load((root / "metrics/sales.outbound_actual.v2.yaml").read_text(encoding="utf-8"))
    source = yaml.safe_load((root / "sources/ywzt_ods/shipment.yaml").read_text(encoding="utf-8"))
    assert metric["parameters"]["fulfillment_scope"]["default"] == "ALL"
    assert metric["filters"]["fulfillment_type"]["field"] == "h.terms_of_shipment"
    assert "terms_of_shipment='20'" in metric["filters"]["fulfillment_type"]["semantics"]
    assert source["fulfillment_type"]["values"] == {"2C": "20", "2B": "not 20"}
    assert "channel_code" in source["fulfillment_type"]["meaning"]

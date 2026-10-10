"""Synthetic adversarial cases established before box conversion implementation."""

from decimal import Decimal, localcontext
import sqlite3

import pytest
import sqlglot

from app.agent_core.box_conversion import box_control_sql, resolve_box_specs, rounded_boxes, convert_box_rows


def control(code="A", records=1, variants=1, invalid=0, spec=10):
    return {"sku_code":code, "record_count":records, "positive_variants":variants, "invalid_count":invalid, "box_spec":spec}


def test_box_controls_deduplicate_identical_values_and_keep_invalid_evidence():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("ATTACH DATABASE ':memory:' AS ywzt_ods")
    db.execute("CREATE TABLE ywzt_ods.master_sku_unit (sku_code TEXT, unit TEXT, ea_convert_quantity INTEGER)")
    db.executemany("INSERT INTO ywzt_ods.master_sku_unit VALUES (?,?,?)", [
        ("A","40",10), ("A","40",10), ("A","10",99),
        ("B","40",10), ("B","40",20), ("C","40",0),
        ("D","40",None), ("E","40",-1), ("F","40",10), ("F","40",0),
    ])
    codes = tuple("ABCDEFG")
    sql = box_control_sql(codes)
    rows = [dict(r) for r in db.execute(sqlglot.transpile(sql, read="doris", write="sqlite")[0])]
    specs = resolve_box_specs(codes, rows)
    assert specs["A"].value == Decimal(10) and specs["A"].state == "valid"
    assert specs["B"].state == "conflict" and specs["B"].value is None
    assert all(specs[c].state == "invalid" and specs[c].value is None for c in "CDEF")
    assert specs["G"].state == "missing" and specs["G"].value is None
    db.close()


@pytest.mark.parametrize("quantity,spec,expected", [("4.9",10,"0"),("5",10,"1"),("15",10,"2"),("0",10,"0"),("2.5",1,"3"),("99999999999999999999999999999.49",1,"99999999999999999999999999999")])
def test_half_up_is_exact_even_with_small_decimal_context(quantity,spec,expected):
    with localcontext() as context:
        context.prec = 6
        assert rounded_boxes(Decimal(quantity), Decimal(spec)) == expected


def test_aggregate_before_rounding_not_detail_rounding():
    assert rounded_boxes(Decimal(8),Decimal(10)) == "1"
    assert sum(int(rounded_boxes(Decimal(4),Decimal(10))) for _ in range(2)) == 0


@pytest.mark.parametrize("quantity,spec", [("NaN","1"),("Infinity","1"),("-1","10"),("1","0"),("1","-5")])
def test_invalid_arithmetic_is_never_a_number(quantity,spec):
    with pytest.raises(ValueError):
        rounded_boxes(Decimal(quantity),Decimal(spec))


def test_conversion_preserves_raw_quantity_keys_and_nulls_without_mutation():
    source = [{"cargo_owner":"A","warehouse_code":"W","sku_code":"A","quantity":"8.00"},
              {"cargo_owner":"B","warehouse_code":"W","sku_code":"B","quantity":"15"}]
    specs = resolve_box_specs(("A","B"), [control()])
    converted = convert_box_rows(source, specs)
    assert source[0] == {"cargo_owner":"A","warehouse_code":"W","sku_code":"A","quantity":"8.00"}
    assert converted[0]["quantity"] == "8.00" and converted[0]["box_quantity"] == "1"
    assert converted[1]["quantity"] == "15" and converted[1]["box_quantity"] is None
    assert converted[1]["box_state"] == "missing" and converted[1]["cargo_owner"] == "B"


@pytest.mark.parametrize("rows", [[control(),control()], [control(code="OTHER")], [control(records=0)], [control(variants=2)], [control(invalid=2)], [control(spec=0)], [control(records="1.5")], [control(variants=0,spec=10)]])
def test_corrupt_control_evidence_rejected(rows):
    with pytest.raises(ValueError):
        resolve_box_specs(("A",), rows)


def test_box_query_bounded_escaped_and_fully_qualified():
    tree = sqlglot.parse_one(box_control_sql(("A' OR 1=1 --",)), read="doris")
    assert {t.sql() for t in tree.find_all(sqlglot.exp.Table)} == {"ywzt_ods.master_sku_unit"}
    assert tree.args["limit"].expression.this == "201"
    with pytest.raises(ValueError):
        box_control_sql(tuple(str(i) for i in range(201)))
    with pytest.raises(ValueError):
        box_control_sql(())

"""Acceptance must reject plausible but incorrect turnover and derived values."""

from copy import deepcopy
from datetime import date

import pytest
import sqlglot

from tests.acceptance.check_turnover_derived_live import compare, reference_sql


def fixture():
    labels = ("完整自然日数", "当前可用成品库存加在途", "实际销售出库成品数量",
              "实际销售出库成品日均量", "原始周转天数", "两倍周转天数")
    units = ("个完整自然日", "EA", "EA", "EA/日", "天", "天")
    columns = [{"key": f"c{i}", "label": label, "kind": "number", "unit": unit}
               for i, (label, unit) in enumerate(zip(labels, units))]
    row = dict(zip((c["key"] for c in columns), ("31", "3100", "6200", "200", "15.5", "31")))
    reference = [{"inventory_qty": "3100", "sales_qty": "6200", "inventory_facts": 200,
                  "sales_facts": 100, "bad_quantity": 0, "negative_warehouses": 0}]
    return {"columns": columns}, [row], reference


def test_independent_derived_result_passes():
    compare(*fixture(), 31)


@pytest.mark.parametrize("index", range(6))
def test_wrong_measure_is_rejected(index):
    table, rows, reference = fixture()
    rows[0][f"c{index}"] = "999"
    with pytest.raises(AssertionError, match="measure_mismatch"):
        compare(table, rows, reference, 31)


@pytest.mark.parametrize("value", [None, "NaN", "Infinity", True])
def test_missing_or_invalid_derived_value_is_rejected(value):
    table, rows, reference = fixture()
    rows[0]["c5"] = value
    with pytest.raises(AssertionError):
        compare(table, rows, reference, 31)


def test_unit_and_scalar_grain_must_match():
    table, rows, reference = fixture()
    wrong = deepcopy(table)
    wrong["columns"][5]["unit"] = "箱"
    with pytest.raises(AssertionError, match="unit"):
        compare(wrong, rows, reference, 31)
    with pytest.raises(AssertionError, match="scalar_grain"):
        compare(table, rows * 2, reference, 31)


def test_correct_total_cannot_hide_a_blocking_warehouse_anomaly():
    table, rows, reference = fixture()
    reference[0]["negative_warehouses"] = 1
    with pytest.raises(AssertionError, match="negative_inventory_warehouse"):
        compare(table, rows, reference, 31)


def test_reference_uses_shared_finished_goods_set_and_half_open_window():
    query = reference_sql(date(2026, 8, 1), date(2026, 9, 1))
    tree = sqlglot.parse_one(query, read="doris")
    assert "available_quantity" in query and "in_transit_quantity" in query
    assert "ship_qty" in query and "allocate_qty" not in query
    assert "'2026-08-01 00:00:00'" in query and "<'2026-09-01 00:00:00'" in query
    assert len([x for x in tree.find_all(sqlglot.exp.CTE) if x.alias == "products"]) == 1
    assert "master_wms" not in query and "HAVING COUNT(*)" not in query

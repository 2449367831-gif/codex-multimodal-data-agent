"""Synthetic counterexamples for the independent composition oracle."""

from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest
import sqlglot
from sqlglot import exp

from tests.acceptance.check_composed_operators_live import (
    compare_periods, compare_threshold, independent_sql,
)

PERIODS = ((date(2026, 9, 1), date(2026, 9, 8)), (date(2026, 9, 8), date(2026, 9, 14)))
CONTROLS = dict(missing_quantity=0, negative_quantity=0, missing_key=0)


def reference(**values):
    return dict(CONTROLS, owner="synthetic", warehouse="001", **values)


def period_fixture():
    columns = [
        ("owner", "货主编码", None), ("warehouse", "仓库编码", None),
        ("left", "期间一日均量", "EA/日"), ("right", "期间二日均量", "EA/日"),
        ("ls", "期间一记录状态", None), ("rs", "期间二记录状态", None),
        ("difference", "日均量差额", "EA/日"),
    ]
    table = {"columns": [{"key": k, "label": label, "kind": "number" if unit else "text",
                          **({"unit": unit} if unit else {})} for k, label, unit in columns]}
    rows = [{"owner": "synthetic", "warehouse": "001", "left": "10", "right": "20",
             "ls": "有记录", "rs": "有记录", "difference": "10"}]
    source = [reference(left_quantity=70, right_quantity=120, left_records=2, right_records=3, expected_rows=1)]
    return table, rows, source


def test_different_period_lengths_and_partial_record_coverage_use_calendar_days():
    compare_periods(*period_fixture(), PERIODS)


@pytest.mark.parametrize("field,value", [
    ("left", "35"), ("right", "17.14"), ("difference", "7.14"),
    ("ls", "无记录"), ("left", "NaN"), ("right", True), ("right", None),
])
def test_reject_wrong_average_length_difference_or_state(field, value):
    table, rows, source = period_fixture()
    rows[0][field] = value
    with pytest.raises(AssertionError):
        compare_periods(table, rows, source, PERIODS)


def test_missing_records_are_not_zero_and_zero_is_not_missing():
    table, rows, source = period_fixture()
    source[0].update(left_quantity=None, left_records=0, right_quantity=0)
    rows[0].update(left=None, right="0", ls="无记录", difference=None)
    compare_periods(table, rows, source, PERIODS)
    rows[0]["left"] = "0"
    with pytest.raises(AssertionError, match="missing_records_replaced"):
        compare_periods(table, rows, source, PERIODS)
    rows[0].update(left=None, rs="无记录")
    with pytest.raises(AssertionError, match="zero_misclassified_as_missing"):
        compare_periods(table, rows, source, PERIODS)


def test_missing_period_cannot_produce_a_difference():
    table, rows, source = period_fixture()
    source[0].update(left_quantity=None, left_records=0)
    rows[0].update(left=None, ls="无记录", difference=20)
    with pytest.raises(AssertionError, match="missing_difference_replaced"):
        compare_periods(table, rows, source, PERIODS)


@pytest.mark.parametrize("corruption", ["dropped_group", "duplicate_group", "wrong_unit", "truncated_source", "source_null", "fractional_count"])
def test_period_results_need_complete_groups_valid_units_and_source(corruption):
    table, rows, source = period_fixture()
    if corruption == "dropped_group":
        rows.clear()
    elif corruption == "duplicate_group":
        rows.append(deepcopy(rows[0]))
    elif corruption == "wrong_unit":
        table["columns"][2]["unit"] = "EA"
    elif corruption == "truncated_source":
        source[0]["expected_rows"] = 2
    elif corruption == "source_null":
        source[0]["missing_quantity"] = 1
    else:
        source[0]["left_records"] = "2.5"
    with pytest.raises(AssertionError):
        compare_periods(table, rows, source, PERIODS)


def threshold_fixture():
    table = {"columns": [
        {"key": "owner", "label": "货主编码", "kind": "text"},
        {"key": "warehouse", "label": "仓库编码", "kind": "text"},
        {"key": "sku", "label": "SKU编码", "kind": "text"},
        {"key": "quantity", "label": "实发数量", "kind": "number", "unit": "EA"},
    ]}
    rows = [dict(owner="synthetic", warehouse="001", sku="000123", quantity="1100")]
    source = [reference(sku="000123", quantity=1100, expected_rows=1)]
    return table, rows, source


def test_post_aggregate_threshold():
    compare_threshold(*threshold_fixture(), Decimal(1000))


@pytest.mark.parametrize("corruption", ["drop", "duplicate", "quantity", "identifier", "unit", "boundary", "fractional_control"])
def test_threshold_oracle_rejects_partial_or_misinterpreted_answers(corruption):
    table, rows, source = threshold_fixture()
    if corruption == "drop":
        rows.clear()
    elif corruption == "duplicate":
        rows.append(deepcopy(rows[0]))
    elif corruption == "quantity":
        rows[0]["quantity"] = "1200"
    elif corruption == "identifier":
        rows[0]["sku"] = "123"
    elif corruption == "unit":
        table["columns"][-1]["unit"] = "箱"
    elif corruption == "boundary":
        rows[0]["quantity"] = source[0]["quantity"] = 1000
    else:
        source[0]["expected_rows"] = "1.5"
    with pytest.raises(AssertionError):
        compare_threshold(table, rows, source, Decimal(1000))


def test_reference_filters_after_aggregation_and_uses_actual_quantity():
    sql = independent_sql("threshold", *PERIODS[0])
    tree = sqlglot.parse_one(sql, read="doris")
    assert len(list(tree.find_all(exp.Having))) == 1
    assert "SUM(quantity) > 1000" in sql
    assert "ship_qty" in sql and "allocate_qty" not in sql
    assert all(kind in sql for kind in ("JYCK", "HHCK", "BFCK", "QTCK", "SO%"))


def test_period_reference_preserves_both_half_open_windows_and_union():
    sql = independent_sql("period", *PERIODS[0], second=PERIODS[1])
    sqlglot.parse_one(sql, read="doris")
    assert "day >= '2026-09-01' AND day < '2026-09-08'" in sql
    assert "day >= '2026-09-08' AND day < '2026-09-14'" in sql
    assert " OR " in sql and "ELSE 0 END" in sql


@pytest.mark.parametrize("threshold", ["NaN", "Infinity", True, -1, "1; SELECT x"])
def test_invalid_reference_thresholds_rejected(threshold):
    with pytest.raises(AssertionError):
        independent_sql("threshold", *PERIODS[0], threshold=threshold)

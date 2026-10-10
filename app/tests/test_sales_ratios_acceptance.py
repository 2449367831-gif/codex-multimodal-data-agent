"""Wrong denominators must not pass a superficially plausible ratio report."""

from copy import deepcopy
from datetime import date

import pytest
import sqlglot

from tests.acceptance.check_sales_ratios_live import compare, independent_sql


def fixture(case):
    if case == "daily_average":
        columns = [("day", "出库日期", None), ("qty", "实发数量", "EA"),
                   ("docs", "出库单数", "单"), ("ratio", "每单平均出库量", "EA/单")]
        rows = [{"day": "2026-09-01", "qty": "250", "docs": "10", "ratio": "25"},
                {"day": "2026-09-02", "qty": "300", "docs": "5", "ratio": "60"}]
        reference = [{"day": r["day"], "quantity": r["qty"], "documents": r["docs"]} for r in rows]
    else:
        columns = [("owner", "货主", None), ("warehouse", "仓库编码", None), ("sku", "SKU", None),
                   ("rank", "仓内排名", "个"), ("qty", "实发数量", "EA"), ("ratio", "数量占比", "%")]
        rows = [
            {"owner": "A", "warehouse": "W1", "sku": "A", "rank": "1", "qty": "60", "ratio": "60"},
            {"owner": "A", "warehouse": "W1", "sku": "B", "rank": "2", "qty": "20", "ratio": "20"},
            {"owner": "B", "warehouse": "W1", "sku": "A", "rank": "1", "qty": "45", "ratio": "90"},
        ]
        reference = [{"owner": r["owner"], "warehouse": r["warehouse"], "sku": r["sku"],
                      "quantity": r["qty"], "ranking": r["rank"],
                      "warehouse_quantity": "100" if r["owner"] == "A" else "50"} for r in rows]
    for r in reference:
        r.update(missing_quantity=0, negative_quantity=0, missing_key=0, expected_rows=len(reference))
    table = {"columns": [{"key": k, "label": label, "kind": "number" if unit else "text",
                           **({"unit": unit} if unit else {})} for k, label, unit in columns]}
    return table, rows, reference


@pytest.mark.parametrize("case", ["daily_average", "partition_share"])
def test_independently_matched_ratios_pass(case):
    compare(case, *fixture(case))


def test_top_subset_is_not_the_share_denominator():
    table, rows, reference = fixture("partition_share")
    rows[0]["ratio"], rows[1]["ratio"] = "75", "25"
    with pytest.raises(AssertionError, match="ratio_mismatch"):
        compare("partition_share", table, rows, reference)


def test_period_document_count_is_not_daily_denominator():
    table, rows, reference = fixture("daily_average")
    rows[0]["ratio"] = "16.67"
    with pytest.raises(AssertionError, match="ratio_mismatch"):
        compare("daily_average", table, rows, reference)


@pytest.mark.parametrize("case", ["daily_average", "partition_share"])
@pytest.mark.parametrize("mutation", ["quantity", "dropped", "duplicate", "unit", "quality", "reference_count"])
def test_incomplete_or_changed_evidence_is_rejected(case, mutation):
    table, rows, reference = fixture(case)
    if mutation == "quantity": rows[0]["qty"] = "999"
    elif mutation == "dropped": rows.pop()
    elif mutation == "duplicate": rows.append(deepcopy(rows[0]))
    elif mutation == "unit": table["columns"][-1]["unit"] = "元"
    elif mutation == "quality": reference[0]["missing_quantity"] = 1
    else: reference[0]["expected_rows"] += 1
    with pytest.raises(AssertionError):
        compare(case, table, rows, reference)


def test_zero_warehouse_denominator_must_stay_null():
    table, rows, reference = fixture("partition_share")
    rows[2].update(qty="0", ratio=None)
    reference[2].update(quantity="0", warehouse_quantity="0")
    compare("partition_share", table, rows, reference)
    rows[2]["ratio"] = "0"
    with pytest.raises(AssertionError, match="zero_denominator"):
        compare("partition_share", table, rows, reference)


@pytest.mark.parametrize("case", ["daily_average", "partition_share"])
def test_source_reference_binds_full_half_open_range(case):
    query = independent_sql(case, date(2026, 9, 1), date(2026, 9, 15))
    tree = sqlglot.parse_one(query, read="doris")
    assert "2026-09-01 00:00:00" in query and "2026-09-15 00:00:00" in query
    assert "ship_qty" in query and "allocate_qty" not in query
    ctes = {c.alias: c for c in tree.find_all(sqlglot.exp.CTE)}
    if case == "partition_share":
        assert len(list(ctes["ranked"].find_all(sqlglot.exp.Window))) == 2
        assert ctes["ranked"].this.args.get("where") is None
    else:
        assert {e.name for e in ctes["doc_totals"].this.args["group"].expressions} == {"day", "owner", "doc"}

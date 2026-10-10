"""A global Top-N or a plausible table must not pass a per-warehouse check."""

from copy import deepcopy
from datetime import date

import pytest

from tests.acceptance.check_partition_topn_live import compare, independent_sql


@pytest.fixture
def ranking():
    reference = [
        dict(owner="A", warehouse="W1", sku="S1", ranking=1, quantity="20.50", documents=2),
        dict(owner="A", warehouse="W1", sku="S2", ranking=2, quantity="10.25", documents=1),
        dict(owner="A", warehouse="W2", sku="S3", ranking=1, quantity="1.00", documents=1),
    ]
    for row in reference:
        row.update(expected_rows=3, missing_quantity=0, negative_quantity=0, missing_key=0)
    columns = [
        {"key": "owner", "label": "货主编码"}, {"key": "warehouse", "label": "仓库编码"},
        {"key": "sku", "label": "SKU编码"}, {"key": "ranking", "label": "仓内排名"},
        {"key": "quantity", "label": "实发数量", "unit": "EA"},
        {"key": "documents", "label": "出库单数", "unit": "单"},
    ]
    rows = [{c["key"]: str(row[c["key"]]) for c in columns} for row in reference]
    return {"columns": columns}, rows, reference


def test_complete_partitioned_ranking_matches_independent_reference(ranking):
    assert all(compare(*ranking, top_n=3).values())


@pytest.mark.parametrize("field,value", [
    ("owner", "B"), ("warehouse", "OTHER"), ("sku", "OTHER"),
    ("quantity", "10.50"), ("documents", "3"), ("ranking", "2"),
    ("quantity", "NaN"), ("documents", "1.5"),
])
def test_plausible_but_wrong_scope_quantity_count_or_rank_is_rejected(ranking, field, value):
    table, rows, reference = ranking
    rows[0][field] = value
    with pytest.raises(AssertionError):
        compare(table, rows, reference, 3)


def test_missing_other_warehouse_is_not_global_top_n_success(ranking):
    table, rows, reference = ranking
    with pytest.raises(AssertionError, match="independent_partition_mismatch"):
        compare(table, rows[:2], reference, 3)


def test_duplicate_or_missing_partition_rank_is_rejected(ranking):
    table, rows, reference = ranking
    with pytest.raises(AssertionError):
        compare(table, rows + [deepcopy(rows[0])], reference, 3)
    rows[1]["ranking"] = "3"
    with pytest.raises(AssertionError, match="missing_partition_rank"):
        compare(table, rows, reference, 3)


def test_missing_units_or_bad_source_quality_cannot_pass(ranking):
    table, rows, reference = ranking
    table["columns"][-1]["unit"] = "个"
    with pytest.raises(AssertionError, match="document_unit_mismatch"):
        compare(table, rows, reference, 3)
    table["columns"][-1]["unit"] = "单"
    reference[0]["missing_quantity"] = 1
    with pytest.raises(AssertionError, match="reference_quality_failed"):
        compare(table, rows, reference, 3)


@pytest.mark.parametrize("top_n", [0, 101, True, 1.5, "3"])
def test_reference_rejects_invalid_rank_limits(top_n):
    with pytest.raises(ValueError):
        independent_sql(date(2026, 9, 1), date(2026, 9, 15), top_n)

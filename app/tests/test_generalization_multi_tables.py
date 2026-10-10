"""Independent arithmetic checks for multi-table live acceptance (synthetic rows)."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from scripts.evaluate_generalization import check_answer


def fixture():
    cases = json.loads((Path(__file__).resolve().parents[2] / "tests/evaluations/generalization.json").read_text(encoding="utf-8"))["cases"]
    case = next(c for c in cases if c["id"] == "generic-warehouse-summary-detail")
    tables = []
    for spec in case["required_tables"]:
        tables.append({"title": spec["titles"][0], "columns": [
            {"key": f"c{i}", "label": names[0]} for i, names in enumerate(spec["required_columns"])], "rows": []})
    tables[0]["rows"] = [{"c0": "中心仓", "c1": "2", "c2": "66.67"}, {"c0": "分仓", "c1": "1", "c2": "33.33"}]
    tables[1]["rows"] = [
        {"c0": "A", "c1": "001", "c2": "沈阳测试仓", "c3": "中心仓"},
        {"c0": "B", "c1": "001", "c2": "沈阳测试仓", "c3": "中心仓"},
        {"c0": "A", "c1": "002", "c2": "沈阳测试仓二", "c3": "分仓"}]
    return case, {"state": "completed", "result": {"result_state": "live_success", "business_tables": tables,
        "business_table": tables[0], "business_result": {"text": "已查询并核对。"}}}


def test_complete_bundle_matches_counts_grain_and_scope():
    assert check_answer(*fixture()) == []


@pytest.mark.parametrize("mutation", ["missing_table", "duplicate_key", "wrong_group", "wrong_scope", "missing_column", "wrong_title", "paged", "private_value"])
def test_bundle_counterexamples_do_not_pass_on_one_correct_table(mutation):
    case, task = deepcopy(fixture())
    tables = task["result"]["business_tables"]
    if mutation == "missing_table":
        tables.pop()
    elif mutation == "duplicate_key":
        tables[1]["rows"][1]["c0"] = "A"
    elif mutation == "wrong_group":
        tables[1]["rows"][1]["c3"] = "分仓"
    elif mutation == "wrong_scope":
        tables[1]["rows"][1]["c2"] = "长沙测试仓"
    elif mutation == "missing_column":
        tables[1]["columns"].pop()
    elif mutation == "wrong_title":
        tables[1]["title"] = "对账明细"
    elif mutation == "paged":
        tables[1]["pagination"] = {"page_count": 2}
    elif mutation == "private_value":
        tables[1]["rows"][0]["c2"] = "SELECT secret FROM private"
    assert check_answer(case, task)

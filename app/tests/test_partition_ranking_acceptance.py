"""The ranking oracle must reject globally plausible but locally wrong answers."""

from copy import deepcopy
from datetime import datetime
from pathlib import Path
import runpy

import pytest

from app.agent_core.planning import RequestPlanner


CHECKER = runpy.run_path(str(Path(__file__).resolve().parents[2] / "tests/acceptance/check_partition_ranking_live.py"))


def fixture():
    dimensions = ["货主编码", "仓库编码", "SKU编码"]
    full = {"title": "各仓SKU全集", "columns": [{"key": str(i), "label": label}
             for i, label in enumerate([*dimensions, "实发数量"])], "rows": []}
    ranking = {"title": "各仓排名", "columns": [{"key": str(i), "label": label}
                for i, label in enumerate([*dimensions, "排名", "实发数量", "数量占比（%）"])], "rows": []}
    for values in [("A", "W1", "S1", 40), ("A", "W1", "S2", 30), ("A", "W1", "S3", 20),
                   ("A", "W1", "S4", 10), ("B", "W1", "S2", 50), ("B", "W1", "S1", 50),
                   ("A", "W2", "S0", 0)]:
        full["rows"].append(dict(zip(map(str, range(4)), values)))
    for values in [("A", "W1", "S1", 1, 40, "40%"), ("A", "W1", "S2", 2, 30, "30%"),
                   ("A", "W1", "S3", 3, 20, "20%"), ("B", "W1", "S1", 1, 50, "50%"),
                   ("B", "W1", "S2", 2, 50, "50%"), ("A", "W2", "S0", 1, 0, "分母为零")]:
        ranking["rows"].append(dict(zip(map(str, range(6)), values)))
    return {"state": "completed", "http_status": 200, "result": {"result_state": "live_success",
            "business_tables": [ranking, full], "business_result": {"text": "合成结果"}}}


def test_partition_rank_ties_and_unsliced_denominators():
    assert CHECKER["verify"](None, fixture(), 3) == {"groups": 3, "source_rows": 7, "ranking_rows": 6}


@pytest.mark.parametrize("mutation", ["denominator", "rank", "tie", "owner", "missing_group", "missing_rank",
                                      "duplicate", "quantity", "source_duplicate", "zero", "leak", "nan"])
def test_plausible_wrong_rankings_cannot_pass(mutation):
    task = deepcopy(fixture())
    ranked, full = task["result"]["business_tables"]
    if mutation == "denominator":
        ranked["rows"][0]["5"] = "44.44%"
    elif mutation == "rank":
        ranked["rows"][3]["3"] = 4
    elif mutation == "tie":
        ranked["rows"][3]["2"], ranked["rows"][4]["2"] = "S2", "S1"
    elif mutation == "owner":
        ranked["rows"][3]["0"] = "A"
    elif mutation == "missing_group":
        ranked["rows"] = ranked["rows"][:3]
    elif mutation == "missing_rank":
        ranked["rows"].pop(1)
    elif mutation == "duplicate":
        ranked["rows"].append(ranked["rows"][0])
    elif mutation == "quantity":
        ranked["rows"][0]["4"] = 50
    elif mutation == "source_duplicate":
        full["rows"].append(full["rows"][0])
    elif mutation == "zero":
        ranked["rows"][-1]["5"] = "0%"
    elif mutation == "leak":
        task["result"]["business_result"]["text"] = "SELECT private FROM ywzt_ods.example"
    elif mutation == "nan":
        ranked["rows"][0]["4"] = "NaN"
    with pytest.raises((AssertionError, ValueError)):
        CHECKER["verify"](None, task, 3)


def test_all_pages_are_checked_not_only_visible_first_page():
    task = fixture()
    ranked, full = task["result"]["business_tables"]
    remainder = full["rows"][4:]
    full["rows"] = full["rows"][:4]
    full["pagination"] = {"result_id": "synthetic", "page_count": 2, "total_rows": 7, "reconciled": True}

    class Client:
        def request(self, path):
            assert path == "/v1/results/synthetic/pages/2"
            return {"business_table": {**full, "rows": remainder}}

    assert CHECKER["verify"](Client(), task, 3)["source_rows"] == 7
    remainder.pop()
    with pytest.raises(AssertionError, match="missing_page_rows"):
        CHECKER["verify"](Client(), task, 3)


@pytest.mark.parametrize("top", [1, 3, 5, 20])
def test_partition_ranking_does_not_fall_into_global_top_accelerator(top):
    planner = RequestPlanner(clock=lambda: datetime(2026, 9, 15, 12))
    assert planner.classify(CHECKER["question"](top)) is None


def test_business_reference_uses_reviewed_ship_quantity_compiler():
    request = RequestPlanner().classify(CHECKER["reference_question"]("2026-09-14"))
    assert request is not None and request.route == "sales_aggregate"
    assert request.sales_aggregation.dimensions == ("cargo_owner", "warehouse", "sku")
    assert request.finished_goods_only and request.scope.owner == "ALL"


@pytest.mark.parametrize("mutation", [None, "quantity", "missing", "owner", "sku"])
def test_model_tables_must_match_independently_reviewed_business_quantities(mutation):
    task = fixture()
    keys = ("cargo_owner", "warehouse_code", "sku_code", "quantity")
    reference = {"state": "completed", "http_status": 200, "result": {"result_state": "live_success", "business_table": {
        "columns": [{"key": key} for key in keys], "rows": [dict(zip(keys, (row[str(i)] for i in range(4))))
            for row in task["result"]["business_tables"][1]["rows"]]}}}
    if mutation == "quantity":
        reference["result"]["business_table"]["rows"][0]["quantity"] = 99
    elif mutation == "missing":
        reference["result"]["business_table"]["rows"].pop()
    elif mutation == "owner":
        reference["result"]["business_table"]["rows"][0]["cargo_owner"] = "other"
    elif mutation == "sku":
        reference["result"]["business_table"]["rows"][0]["sku_code"] = "other"
    if mutation is None:
        assert CHECKER["verify_reference"](None, task, reference) == 7
    else:
        with pytest.raises(AssertionError, match="reviewed_business_reference_mismatch"):
            CHECKER["verify_reference"](None, task, reference)

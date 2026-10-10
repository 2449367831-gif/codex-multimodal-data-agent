"""Independent counts and unit checks for a cross-domain model holdout."""

from copy import deepcopy
from pathlib import Path
import runpy

import pytest

from app.agent_core.planning import RequestPlanner

CHECKER = runpy.run_path(str(Path(__file__).resolve().parents[2] / "tests/acceptance/check_dms_monthly_analysis_live.py"))


def fixture(empty=False):
    orders = [] if empty else [
        {"sales_order_code": "S1", "customer_code": "001", "created_time": "2026-01-01 00:00:00"},
        {"sales_order_code": "S2", "customer_code": "001", "created_time": "2026-01-10 00:00:00"},
        {"sales_order_code": "S3", "customer_code": "1", "created_time": "2026-01-20 00:00:00"},
        {"sales_order_code": "S4", "customer_code": None, "created_time": "2026-01-31 16:00:00+00:00"},
        {"sales_order_code": "S5", "customer_code": "001", "created_time": "2026-08-20 00:00:00"},
    ]
    rows = []
    total = 0
    for month in range(1, 9):
        count = 0 if empty else {1: 3, 2: 1, 8: 1}.get(month, 0)
        total += count
        customers = 0 if empty else {1: 2, 8: 1}.get(month, 0)
        rows.append({"m": f"2026-{month:02}", "n": str(count), "c": str(customers),
                     "p": None if empty else str(count * 20), "s": str(total)})
    columns = [{"key": "m", "label": "月份", "kind": "text"}]
    for key, label, unit in [("n", "订单数", "单"), ("c", "活跃客户数", "个"),
                             ("p", "数量占比（%）", "%"), ("s", "累计订单数", "单")]:
        columns.append({"key": key, "label": label, "kind": "number", "unit": unit})
    task = {"state": "completed", "http_status": 200, "result": {"result_state": "live_success",
            "business_table": {"columns": columns, "rows": rows}}}
    reference = {"state": "completed", "http_status": 200, "result": {"result_state": "no_data" if empty else "live_success",
                 "business_tables": [{"table_id": "dms_points_orders_monthly", "rows": []},
                     {"columns": [{"key": key} for key in (
                         "sales_order_code", "customer_code", "customer_name", "created_time")],
                      "rows": orders}]}}
    return task, reference


@pytest.mark.parametrize("empty", [False, True])
def test_independent_counts_include_zero_months_and_missing_customers(empty):
    task, reference = fixture(empty)
    assert CHECKER["verify"](None, task, reference) == {"months": 8, "reference_orders": 0 if empty else 5}


@pytest.mark.parametrize("mutation", ["omit_month", "order", "count", "customer", "share", "cumulative",
                                      "wrong_unit", "text_quantity", "duplicate_order", "changed_window", "leak"])
def test_wrong_business_measures_cannot_pass_on_a_successful_table(mutation):
    task, reference = deepcopy(fixture())
    table = task["result"]["business_table"]
    if mutation == "omit_month":
        table["rows"].pop(3)
    elif mutation == "order":
        table["rows"].reverse()
    elif mutation in {"count", "customer", "share", "cumulative"}:
        table["rows"][0][{"count": "n", "customer": "c", "share": "p", "cumulative": "s"}[mutation]] = "999"
    elif mutation == "wrong_unit":
        table["columns"][1]["unit"] = "箱"
    elif mutation == "text_quantity":
        table["columns"][1]["kind"] = "text"
    elif mutation == "duplicate_order":
        reference["result"]["business_tables"][1]["rows"].append(reference["result"]["business_tables"][1]["rows"][0])
    elif mutation == "changed_window":
        reference["result"]["business_tables"][1]["rows"][0]["created_time"] = "2026-09-01 00:00:00"
    else:
        task["result"]["business_result"] = {"text": "SELECT secret FROM dms_ods.private"}
    with pytest.raises(AssertionError):
        CHECKER["verify"](None, task, reference)


def test_reference_uses_reviewed_path_but_holdout_uses_generic_model():
    planner = RequestPlanner()
    assert planner.classify(CHECKER["QUESTION"]) is None
    request = planner.classify(CHECKER["REFERENCE_QUESTION"])
    assert request is not None and request.route == "dms_points_orders"


@pytest.mark.parametrize("question", [CHECKER["QUESTION"],
    "查询DMS订单按月累计订单数，不加WMS出库条件",
    "查询DMS积分订单的客户数和数量占比",
    "查询DMS销售订单按城市经理统计订单数和占比",
    "查询DMS积分兑换的其他商品数量",
])
def test_dms_analysis_never_gets_an_unrelated_outbound_or_air_fryer_card(question):
    from app.codex_agent import reviewed_clarification_for_question, fallback_interaction_for_question
    assert reviewed_clarification_for_question(question) is None
    assert fallback_interaction_for_question(question)["questions"] == []


def test_unqualified_actual_outbound_still_gets_its_real_decisions():
    from app.codex_agent import reviewed_clarification_for_question
    card = reviewed_clarification_for_question("昨天出库量")
    assert {q["id"] for q in card["questions"]} == {"outbound_business", "outbound_measure", "outbound_time_axis"}

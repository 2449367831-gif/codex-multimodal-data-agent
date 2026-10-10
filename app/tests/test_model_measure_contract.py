"""Typed generic measures preserve exact values, identity, units and pages."""

from copy import deepcopy
from decimal import Decimal, localcontext
import json

import pytest

from app.chat_api import _public_model_verified_table, _public_model_result_tables, _public_table_number, ChatExecutionError
from app.model_query_contract import result_columns, validate_business_projection
from app.query_outcomes import DorisResultError
from app.result_pages import ResultPageStore
from app.result_summary import public_result_summary
from app.tests.test_model_execution_states import model_host, execute, turn
from app.tests.test_model_query_contract import payload


def fixture():
    return {"result_id": "mixed", "result_title": "业务明细",
            "columns": ["code", "amount", "quantity", "source_unit"],
            "result_columns": [
                {"key": "code", "label": "SKU编码", "kind": "text"},
                {"key": "amount", "label": "金额", "kind": "number", "unit": "元"},
                {"key": "quantity", "label": "请求数量", "kind": "number"},
                {"key": "source_unit", "label": "源单位代码", "kind": "text"}],
            "rows": [{"code": "000123", "amount": "123456789012345678901234567890.000001",
                      "quantity": "1.2500", "source_unit": "010"}]}


def test_exact_decimal_amount_and_raw_unit_code_do_not_depend_on_decimal_context():
    raw = fixture()
    with localcontext() as context:
        context.prec = 3
        validate_business_projection(raw, raw["result_columns"])
        table = _public_model_verified_table(raw)
    assert table["rows"] == [{"col_1": "000123", "col_2": raw["rows"][0]["amount"], "col_3": "1.25", "col_4": "010"}]
    assert [c["kind"] for c in table["columns"]] == ["text", "number", "number", "text"]
    assert [c.get("unit") for c in table["columns"]] == [None, "元", None, None]
    assert table["summary"] == []


@pytest.mark.parametrize("label", ["SKU数", "客户数", "仓库数量", "货主数量", "订单数", "客户金额", "SKU数量占比"])
def test_identity_related_measures_remain_numeric(label):
    declarations = [{"key": "measure", "label": label, "kind": "number"}]
    assert result_columns(declarations) == declarations


@pytest.mark.parametrize("label", ["SKU", "客户编码", "仓库名称", "销售订单号", "源单位代码", "货主"])
def test_identifier_labels_cannot_be_numeric(label):
    with pytest.raises(ValueError, match="identity"):
        result_columns([{"key": "identity", "label": label, "kind": "number"}])


@pytest.mark.parametrize("extra", [{"kind": None}, {"kind": []}, {"kind": True}, {"kind": "float"},
                                   {"unit": "EA"}, {"kind": "text", "unit": "EA"},
                                   {"kind": "number", "unit": "unknown"},
                                   {"kind": "number", "unit": None}, {"kind": "number", "unit": []}])
def test_invalid_declarations_fail_before_query_execution(extra):
    with pytest.raises(ValueError):
        result_columns([{"key": "quantity", "label": "数量", **extra}])


@pytest.mark.parametrize("value", [True, False, "NaN", "Infinity", "无记录", "1,234", [], {}, ""])
def test_declared_numeric_measure_rejects_invalid_values_not_zero(value):
    raw = fixture()
    raw["rows"][0]["quantity"] = value
    with pytest.raises(DorisResultError):
        validate_business_projection(raw, raw["result_columns"])
    assert _public_model_verified_table(raw) is None


@pytest.mark.parametrize("value,expected", [(None, None), (0, "0"), ("0.000", "0"), ("-12.3750", "-12.375"), ("1.25e2", "125")])
def test_missing_zero_signed_and_exponential_values_are_distinct(value, expected):
    raw = fixture()
    raw["rows"][0]["quantity"] = value
    validate_business_projection(raw, raw["result_columns"])
    assert _public_model_verified_table(raw)["rows"][0]["col_3"] == expected


@pytest.mark.parametrize("value,expected", [("1e1000000000", None), ("1e-1000000000", None),
                                          ("0e-1000000000", "0"), ("1" + "0" * 79, "1" + "0" * 79),
                                          ("1" + "0" * 80, None)])
def test_fixed_point_size_is_bounded_before_rendering(value, expected):
    assert _public_table_number(value, allow_negative=True) == expected


def test_units_are_not_conversions_and_different_source_codes_do_not_create_a_total():
    raw = fixture()
    raw["result_columns"][1]["unit"] = "万元"
    raw["rows"].append({**raw["rows"][0], "code": "000124", "source_unit": "020"})
    table = _public_model_verified_table(raw)
    assert table["rows"][0]["col_2"] == raw["rows"][0]["amount"]
    assert table["columns"][1]["unit"] == "万元"
    assert [r["col_4"] for r in table["rows"]] == ["010", "020"]
    assert table["summary"] == [] and "unit" not in table["columns"][2]
    next_raw = deepcopy(raw)
    next_raw["result_columns"][1].pop("unit")
    assert "unit" not in _public_model_verified_table(next_raw)["columns"][1]


def test_units_and_types_survive_all_pages_and_host_owned_summary():
    raw = fixture()
    raw["rows"] = [{**raw["rows"][0], "code": f"{i:06}"} for i in range(501)]
    store = ResultPageStore()
    tables = _public_model_result_tables([raw], pages_store=store, owner="owner")
    first = tables[0]
    rid = first["pagination"]["result_id"]
    last = store.page("owner", rid, 6)
    assert first["columns"] == last["columns"]
    assert last["rows"][0]["col_1"] == "000500"
    assert last["rows"][0]["col_2"] == raw["rows"][0]["amount"]
    summary = public_result_summary(tables, [{"result_id": "mixed", "row_count": 501}],
        [{"result_id": "mixed", "row": 500}], page_reader=lambda identifier, page: store.page("owner", identifier, page))
    assert raw["rows"][0]["amount"] + " 元" in summary["text"]
    assert "010" in summary["text"] and "000500" in summary["text"]
    assert '"result_columns"' not in json.dumps(tables)


def test_bad_late_numeric_row_prevents_any_page_publication():
    raw = fixture()
    raw["rows"] = [dict(raw["rows"][0]) for _ in range(501)]
    raw["rows"][-1]["quantity"] = "NaN"
    store = ResultPageStore()
    with pytest.raises(ChatExecutionError):
        _public_model_result_tables([raw], pages_store=store, owner="owner")
    assert not store._results


def test_model_host_preserves_typed_declarations_and_never_recomputes_amounts(model_host):
    thread, bridge = model_host
    raw = fixture()
    plan = {"outcome": "query_plan", "kind": "business", "purpose": "synthetic typed query",
            "sql": "SELECT wms_code AS code, 1 AS amount, 2 AS quantity, wms_name AS source_unit FROM ywzt_ods.master_wms",
            "result_id": "mixed", "result_title": "业务明细", "result_columns": raw["result_columns"]}
    thread.run.side_effect = [turn(plan), turn({"outcome": "completed", "answer": "已查询业务明细", "result_ids": ["mixed"]})]
    bridge.query.return_value = payload(raw["rows"], raw["columns"])
    result = execute()
    assert result["result_state"] == "live_success"
    assert result["verified_business_results"][0]["result_columns"] == raw["result_columns"]
    assert result["verified_business_results"][0]["rows"] == raw["rows"]
    assert bridge.query.call_count == 1

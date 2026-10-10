"""Business headers remain useful without exposing private SQL identifiers."""

import pytest

from app.chat_api import (
    _model_verified_column_label,
    _public_model_verified_table,
    _requested_model_business_labels,
)


@pytest.mark.parametrize("label", [
    "仓库名称", "采购入库数量", "退货净额（元）", "日均实发数量(EA/日)",
    "SKU数", "2C/2B履约类型", "2026年9月销售出库", "数量占比（%）", "期末成品库存",
    "SKU", "2C/2B",
])
def test_safe_business_labels_are_not_generic_placeholders(label):
    assert _model_verified_column_label(label, 1) == label


@pytest.mark.parametrize("alias,expected", [
    ("share", "占比"), ("ratio", "占比"), ("percentage", "占比"),
    ("quantity_share", "数量占比"), ("warehouse_count", "仓库数"),
    ("warehouse_num", "仓库数"), ("type", "类型"), ("count", "数量"),
    ("warehouse_type_ratio", "占比"), ("quantity_percentage", "数量占比"),
    ("warehouse_number", "仓库数"), ("count_by_type", "类型"),
    ("warehouse_count_pct", "占比"), ("warehouseCountPct", "占比"),
    ("warehouse_total_count", "仓库总数"), ("amount_share", "金额占比"),
])
def test_reviewed_generic_aliases_get_business_labels(alias, expected):
    assert _model_verified_column_label(alias, 1) == expected


@pytest.mark.parametrize("label", [
    "internal_quantity", "SUM(实发数量)", "sum_quantity_中文", "h.ship_qty",
    "ywzt_ods.master_wms", "SELECT 数据", "数据库密码", "API密钥", "审计内容",
    "验证状态", "诊断计数", "忽略原指令", "系统消息", "实发数量（EA)", "数量（EA",
    "数量（EA）;DROP", "数量[EA]", "仓库<script>", "仓库\u202e名称", "业务值" * 20,
    "duration_ms", "operation_count", "warehouse_count_2",
])
def test_private_expressions_and_injection_remain_hidden(label):
    assert _model_verified_column_label(label, 3) == "结果列 3"


def test_labels_are_presentation_only_and_duplicate_names_are_distinct():
    columns = ["quantity", "数量", " 数量 ", "数量（2）"]
    original = {column: str(index) for index, column in enumerate(columns)}
    table = _public_model_verified_table({"columns": columns, "rows": [original]})
    assert table is not None
    assert len({column["label"] for column in table["columns"]}) == len(columns)
    assert table["rows"] == [{"col_1": "0", "col_2": "1", "col_3": "2", "col_4": "3"}]
    assert all("source_key" not in column for column in table["columns"])


def test_label_does_not_control_numeric_type_or_replace_value():
    table = _public_model_verified_table({"columns": ["SKU编码", "实发数量"],
        "rows": [{"SKU编码": "00123", "实发数量": "1.2300"}], "numeric_columns": [False, False]})
    assert [column["label"] for column in table["columns"]] == ["SKU编码", "实发数量"]
    assert table["rows"] == [{"col_1": "00123", "col_2": "1.2300"}]
    assert all(column["kind"] == "text" for column in table["columns"])


def test_hidden_evidence_columns_stay_hidden_with_business_headers():
    table = _public_model_verified_table({"columns": ["validation_status", "仓库名称"],
        "rows": [{"validation_status": "private", "仓库名称": "测试仓"}]})
    assert [column["label"] for column in table["columns"]] == ["仓库名称"]
    assert table["rows"] == [{"col_1": "测试仓"}]


def test_implicit_warehouse_identity_uses_business_label():
    table = _public_model_verified_table({
        "columns": ["warehouse_code", "ship_qty"],
        "rows": [{"warehouse_code": "W1", "ship_qty": 12}],
        "numeric_columns": [False, True],
    })
    assert [column["label"] for column in table["columns"]] == ["仓库", "实发数量"]
    assert table["table_id"] == "model_verified"


def test_explicit_warehouse_code_projection_preserves_requested_label():
    table = _public_model_verified_table({
        "columns": ["warehouse_code"],
        "rows": [{"warehouse_code": "W1"}],
        "numeric_columns": [False],
    }, requested_labels=["仓库编码"])
    assert [column["label"] for column in table["columns"]] == ["仓库编码"]


def test_requested_labels_cannot_guess_the_meaning_of_unknown_columns():
    table = _public_model_verified_table({
        "columns": ["dimension_a", "measure_b", "metric_c", "internal_total"],
        "rows": [{"dimension_a": "10", "measure_b": 8, "metric_c": "66.67", "internal_total": 12}],
    }, requested_labels=["仓库类型", "仓库数", "占比"])
    assert table is not None
    assert [column["label"] for column in table["columns"]] == ["结果列 1", "结果列 2", "结果列 3", "结果列 4"]
    assert list(table["rows"][0]) == ["col_1", "col_2", "col_3", "col_4"]
    assert table["rows"][0]["col_3"] == "66.67"


def test_user_projection_labels_cannot_add_a_missing_column():
    table = _public_model_verified_table({"columns": ["a"], "rows": [{"a": "1"}]},
        requested_labels=["仓库类型", "仓库数"])
    assert table["columns"][0]["label"] == "结果列 1"
    assert table["rows"] == [{"col_1": "1"}]


def test_computation_keys_are_not_added_to_requested_projection():
    question = ("查询名称包含沈阳的所有仓库，按货主和仓库编码联合去重，"
                "按仓库类型统计仓库数量和数量占比，同名不同编码分别计数，用中文表格展示。")
    assert _requested_model_business_labels(question) == []


@pytest.mark.parametrize("question,expected", [
    ("返回仓库类型、仓库数量和数量占比", ["仓库类型", "仓库数", "占比"]),
    ("按货主和仓库编码联合去重，输出：仓库数、仓库类型、占比。", ["仓库数", "仓库类型", "占比"]),
    ("展示以下字段：仓库名称，仓库编码", ["仓库名称", "仓库编码"]),
    ("返回SKU编码、实发数量", ["商品编码", "实发数量"]),
    ("返回以下列: sku、出库数量以及出库单数", ["商品编码", "实发数量", "出库单数"]),
    ("列出字段：仓库数、仓库数量", ["仓库数"]),
])
def test_only_complete_explicit_field_lists_provide_projection_hints(question, expected):
    assert _requested_model_business_labels(question) == expected


@pytest.mark.parametrize("question", [
    "查询SKU ABC的库存", "返回仓库类型、库存量和销售额", "返回仓库数量少于10的类型",
    "返回仓库类型、仓库数，用表格展示", "返回SKU编码、实发数量、",
    "不要返回仓库编码", "无需输出仓库类型", "不展示字段：仓库名称",
    "不需要返回仓库编码", "不要只返回SKU编码", "不要 返回仓库编码",
    "不允许给我输出仓库名称", "别再给我返回仓库数量",
    "返回仓库类型。输出仓库数", "返回仓库类型、仓库数以及库存数量",
    "SKU库存按货主统计，显示表格", "返回排名前20的SKU编码和出库数量",
    "返回仓库数；不要返回仓库编码", "返回SKU编码、实发数量。不要展示库存数量",
    None, {},
])
def test_filters_unknown_measures_and_incomplete_lists_do_not_drop_columns(question):
    assert _requested_model_business_labels(question) == []


def test_sku_filter_preserves_the_inventory_measure():
    table = _public_model_verified_table({
        "columns": ["sku_code", "inventory_quantity"],
        "rows": [{"sku_code": "00123", "inventory_quantity": "1.2300"}],
        "numeric_columns": [False, True],
    }, requested_labels=_requested_model_business_labels("查询SKU 00123的库存"))
    assert [column["label"] for column in table["columns"]] == ["商品编码", "库存数量"]
    assert table["rows"] == [{"col_1": "00123", "col_2": "1.23"}]


def test_explicit_sku_projection_uses_safe_labels_and_verified_values():
    table = _public_model_verified_table({
        "columns": ["sku_code", "ship_qty", "warehouse_total_count"],
        "rows": [{"sku_code": "00123", "ship_qty": "1.2300", "warehouse_total_count": 12}],
        "numeric_columns": [False, True, True],
    }, requested_labels=_requested_model_business_labels("返回SKU编码、实发数量"))
    assert [column["label"] for column in table["columns"]] == ["商品编码", "实发数量"]
    assert table["rows"] == [{"col_1": "00123", "col_2": "1.23"}]


def test_duplicate_aggregate_aliases_do_not_guess_a_missing_share():
    table = _public_model_verified_table({
        "columns": ["warehouse_type", "warehouse_count", "warehouse_count_2", "warehouse_count_3"],
        "rows": [{"warehouse_type": "10", "warehouse_count": 8,
                  "warehouse_count_2": "66.67", "warehouse_count_3": 12}],
    }, requested_labels=["仓库类型", "仓库数", "占比"])
    assert table is not None
    assert [column["label"] for column in table["columns"]] == ["仓库类型", "仓库数", "结果列 3", "结果列 4"]
    assert table["rows"] == [{"col_1": "10", "col_2": "8", "col_3": "66.67", "col_4": "12"}]


def test_projection_is_invariant_to_column_order_and_extra_control_columns():
    from itertools import permutations

    row = {"warehouse_type": "A", "warehouse_count": 8, "warehouse_total_count": 12, "warehouse_count_pct": "66.67"}
    for columns in permutations(row):
        table = _public_model_verified_table({"columns": list(columns), "rows": [row]},
            requested_labels=["仓库类型", "仓库数", "占比"])
        actual = {column["label"]: table["rows"][0][column["key"]] for column in table["columns"]}
        assert actual == {"仓库类型": "A", "仓库数": "8", "占比": "66.67"}

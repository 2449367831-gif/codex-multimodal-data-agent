"""Summaries may select screened records, but cannot invent their values."""

from copy import deepcopy

import pytest

from app.result_pages import ResultPageError, ResultPageStore
from app.result_summary import normalize_answer_references, public_result_summary


def table(rows):
    return {"title": "销售汇总", "columns": [
        {"key": "col_1", "label": "仓库名称"},
        {"key": "col_2", "label": "实发数量", "unit": "件"},
    ], "rows": [{"col_1": name, "col_2": value} for name, value in rows]}


def manifest(count):
    return [{"result_id": "sales", "row_count": count}]


def reference(index):
    return [{"result_id": "sales", "row": index}]


def test_singleton_summary_uses_all_record_dimensions_and_exact_value():
    result = public_result_summary([table([("测试仓", "123.4500")])], manifest(1))
    assert result == {"format": "plain_text", "text": "销售汇总：仓库名称：测试仓；实发数量：123.4500 件"}


def test_detail_first_row_is_not_implicitly_used_as_a_total():
    result = public_result_summary([table([("甲仓", "1"), ("乙仓", "99")])], manifest(2))
    assert result["text"] == "查询已完成，结果见下方表格。"
    assert normalize_answer_references(None, manifest(2)) == []


def test_null_empty_and_zero_are_different():
    assert "缺失" in public_result_summary([table([("测试仓", None)])], manifest(1))["text"]
    assert "0 件" in public_result_summary([table([("测试仓", "0")])], manifest(1))["text"]
    assert public_result_summary([table([])], manifest(0))["text"] == "销售汇总：无数据。"


@pytest.mark.parametrize("refs", [
    reference(-1), reference(2), reference(True), reference("0"), reference(None),
    [{"result_id": "previous_request", "row": 0}],
    [{"result_id": "sales", "row": 0, "value": "999"}],
    reference(0) * 2, reference(0) * 9, "sales", {}, [None],
])
def test_invalid_references_cannot_supply_values_or_other_result_ids(refs):
    with pytest.raises(ValueError):
        normalize_answer_references(refs, manifest(2))


@pytest.mark.parametrize("bad", [[], None, {}, [None], [{"result_id": "sales"}],
    manifest(True), manifest(-1), manifest(1) * 2])
def test_invalid_manifests_are_rejected(bad):
    with pytest.raises(ValueError):
        normalize_answer_references(None, bad)


@pytest.mark.parametrize("index", [0, 99, 100, 199, 200])
def test_references_read_exact_rows_including_short_final_page(index):
    rows = [(f"测试仓{i}", str(i)) for i in range(201)]
    store = ResultPageStore()
    first = store.create("owner", [table(rows[i:i + 100]) for i in range(0, 201, 100)])
    result = public_result_summary([first], manifest(201), reference(index),
        page_reader=lambda rid, page: store.page("owner", rid, page))
    assert f"第{index + 1}条" in result["text"]
    assert f"仓库名称：测试仓{index}；实发数量：{index} 件" in result["text"]


def test_pagination_does_not_read_another_owners_result():
    store = ResultPageStore()
    first = store.create("owner", [table([("甲仓", "1")]), table([("乙仓", "2")])])
    with pytest.raises(ResultPageError):
        public_result_summary([first], manifest(2), reference(1),
            page_reader=lambda rid, page: store.page("other-owner", rid, page))


@pytest.mark.parametrize("mutation", [
    {"result_id": "previous_result"}, {"page": 1}, {"total_rows": 99},
    {"start_row": 1}, {"end_row": 99},
])
def test_changed_page_identity_or_coverage_cannot_supply_a_value(mutation):
    store = ResultPageStore()
    first = store.create("owner", [table([("甲仓", "1")]), table([("乙仓", "2")])])

    def changed(rid, page):
        result = store.page("owner", rid, page)
        result["pagination"].update(mutation)
        return result

    with pytest.raises(ValueError):
        public_result_summary([first], manifest(2), reference(1), page_reader=changed)


def test_wide_record_is_not_partially_quoted():
    value = table([("测试仓" * 220, "123")])
    assert public_result_summary([value], manifest(1))["text"] == "销售汇总：完整记录已返回。"


@pytest.mark.parametrize("empty", [True, False])
def test_private_titles_are_rejected_even_for_empty_results(empty):
    value = table([] if empty else [("测试仓", "123")])
    value["title"] = "SELECT password FROM secret"
    with pytest.raises(ValueError):
        public_result_summary([value], manifest(len(value["rows"])))


def test_private_cell_is_not_quoted_and_input_is_unchanged():
    value = table([("SELECT password FROM secret", "123")])
    before = deepcopy(value)
    with pytest.raises(ValueError):
        public_result_summary([value], manifest(1))
    assert value == before

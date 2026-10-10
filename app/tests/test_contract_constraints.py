from pathlib import Path

import pytest

from app.agent_core.contract_constraints import enforce_comparison_contract, UnpublishedComparisonError


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def candidate(monkeypatch):
    monkeypatch.setattr("app.agent_core.contract_constraints.active_snapshot", lambda _: (ROOT, None))


@pytest.mark.parametrize("question", [
    "查询昨天所有货主有效销售出库，分别给出实际出库数量、分配数量和两者差异，并按货主、2C/2B 汇总。找出差异最大的分组。",
    "统计昨天实发数量与分配量差异",
    "查询ship_qty和allocate_qty的差额",
    "查询实际出库数量，不要用分配量，但也返回分配量和实际出库差异",
    "查询实际出库数量，不要分配数量大于实际出库的记录",
])
def test_unconfirmed_comparison_never_reaches_execution(question):
    with pytest.raises(UnpublishedComparisonError) as failure:
        enforce_comparison_contract(question, ROOT)
    public = failure.value.as_public_dict()
    assert public["result_state"] == "blocked" and public["details"] == {}
    assert "尚未确认" in public["message"]


@pytest.mark.parametrize("question", [
    "查询昨天各仓实际销售出库数量",
    "查询实际出库数量，不要分配数量",
    "查询昨天实际销售出库量TOP20的SKU，按实发数量从高到低，不能使用分配数量",
    "统计实际出库数量，不使用分配量",
    "查询ship_qty，不能使用allocate_qty",
    "查询实际出库数量，不要用分配数量（allocate_qty）",
    "解释实际出库量和分配量为什么不能直接相减",
    "旧SQL的分配数量和实际出库数量有差异，请分析变更影响",
    "昨天实际出库，分配仓库为长沙黄兴",
])
def test_actual_only_and_nondata_assessments_are_not_blocked(question):
    enforce_comparison_contract(question, ROOT)


@pytest.mark.parametrize("exclusion", ["不能使用分配数量", "不使用分配量", "不要用allocate_qty"])
def test_top_ranking_consumes_source_and_presentation_constraints(exclusion):
    from app.agent_core.planning import RequestPlanner
    request = RequestPlanner().classify(
        f"昨天实际销售出库量TOP20的SKU，按实发数量从高到低用表格展示，{exclusion}。"
    )
    assert request is not None
    assert request.route == "sales_outbound_top_sku"
    assert request.top_n == 20
    assert request.scope.owner == "ALL"


@pytest.mark.parametrize("suffix", ["，仅限客户甲", "，按金额从高到低", "，排除零销量"])
def test_source_constraint_does_not_erase_other_requirements(suffix):
    from app.agent_core.planning import RequestPlanner
    assert RequestPlanner().classify(
        "昨天实际销售出库量TOP20的SKU，不能使用分配数量" + suffix
    ) is None


@pytest.mark.parametrize("ordering", ["降序", "从高到低", "降序排序"])
def test_explicit_actual_quantity_descending_ranking(ordering):
    from app.agent_core.planning import RequestPlanner
    request = RequestPlanner().classify("昨日实际发货TOP20商品按实发数量" + ordering)
    assert request and request.route == "sales_outbound_top_sku"
    assert request.top_n == 20


@pytest.mark.parametrize("ordering", ["升序", "从低到高", "按金额降序", "按SKU编码排序"])
def test_other_ranking_requirements_cannot_be_swallowed(ordering):
    from app.agent_core.planning import RequestPlanner
    assert RequestPlanner().classify("昨日实际发货TOP20商品，" + ordering) is None


def test_each_warehouse_synonym_preserves_grain():
    from app.agent_core.planning import RequestPlanner
    request = RequestPlanner().classify("2026年9月13日每仓实际销售出库")
    assert request and request.route == "sales_outbound_by_warehouse"
    assert request.show_warehouses and request.scope.owner == "ALL"

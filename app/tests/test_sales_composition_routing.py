from datetime import date

import pytest

from app.agent_core.generic_sales_composition import sales_composition_request
from app.agent_core.planning import RequestPlanner
from tests.acceptance.check_model_holdout_live import coverage_question


FIRST, SECOND = date(2026, 9, 12), date(2026, 9, 13)


@pytest.mark.parametrize("threshold", ["50", "80", "90.5", "100"])
@pytest.mark.parametrize("projection", ["结果返回", "完整返回", "返回"])
def test_coverage_wording_preserves_dates_threshold_and_dimensions(threshold, projection):
    question = coverage_question(FIRST, SECOND).replace("80%", threshold + "%")
    question = question.replace("完整返回货主编码", projection + "货主编码")
    question = question.replace("只对数量大于零的仓库", "只对汇总数量大于零的仓库")
    question = question.replace("累计值判定是否达到" + threshold + "%", "累计值判定")
    request = sales_composition_request(question, planner=RequestPlanner())
    assert request is not None
    assert request.route == "sales_composition"
    assert request.start == FIRST and request.end == date(2026, 9, 14)
    assert request.sales_composition.operation == "coverage"
    assert str(request.sales_composition.coverage_percent) == threshold
    assert request.sales_aggregation.dimensions == ("ship_date", "cargo_owner", "warehouse")


@pytest.mark.parametrize("extra", [
    "仅限货主HJXH。", "仅限广州仓。", "不含调拨出库。", "改为箱数。",
    "仅限SKU5001060111000362。", "仅限线上渠道。", "另一个阈值90%。",
])
def test_unknown_predicates_never_collapse_to_all_scope(extra):
    assert sales_composition_request(
        coverage_question(FIRST, SECOND) + extra, planner=RequestPlanner(),
    ) is None

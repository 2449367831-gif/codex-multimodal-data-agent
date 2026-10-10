from datetime import date, datetime

import pytest

from app.agent_core.planning import RequestPlanner


@pytest.mark.parametrize('question,start,end,owner,skus', [
    ('昨天实际销售出库明细', date(2026, 9, 10), date(2026, 9, 11), 'ALL', ()),
    ('查询2026-08-01至2026-09-01 0点前HJXH实际销售出库明细', date(2026, 8, 1), date(2026, 9, 1), 'HJXH', ()),
    ('上月SKU QY-YwKC500G实际销售出库明细', date(2026, 8, 1), date(2026, 9, 1), 'ALL', ('QY-YwKC500G',)),
])
def test_explicit_actual_detail_request_retains_dates_and_scopes(question, start, end, owner, skus):
    planner = RequestPlanner(clock=lambda: datetime(2026, 9, 11, 12))
    request = planner.classify(question)
    assert request is not None and request.route == 'sales_outbound_details'
    assert (request.start, request.end) == (start, end)
    assert request.scope.owner == owner and request.scope.sku_codes == skus


@pytest.mark.parametrize('question', [
    '昨天所有出库明细', '实际销售出库明细', '昨天实际销售出库明细排除客户甲',
    '昨天实际销售出库明细，按订单创建时间', '昨天实际销售出库明细，数量大于5',
    '昨天实际销售出库明细，换算成箱', '昨天HJXH所有货主实际销售出库明细',
    '昨天实际销售出库原始物理明细', '近一年所有出库明细',
])
def test_no_scope_time_unit_or_physical_grain_guessing(question):
    from app.agent_core.sales_detail_planning import sales_detail_request
    assert sales_detail_request(question, planner=RequestPlanner()) is None


def test_plain_sales_outbound_detail_uses_the_reviewed_batch_contract():
    request = RequestPlanner(clock=lambda: datetime(2026, 9, 11, 12)).classify(
        "昨天的销售出库明细"
    )
    assert request is not None and request.route == "sales_outbound_details"
    assert (request.start, request.end) == (date(2026, 9, 10), date(2026, 9, 11))

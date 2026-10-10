import pytest
from datetime import date, datetime
from pathlib import Path

from app.query_scope import ScopeResolver
from app.agent_core.planning import RequestPlanner


@pytest.mark.parametrize("reference,codes", [
    ("SKU `QY-YWKC500G`", ("QY-YWKC500G",)),
    ('SKU "AbC123"', ("AbC123",)),
    ("SKU 'AbC123'", ("AbC123",)),
    ("SKU编码：AbC123", ("AbC123",)),
    ("商品编码 AbC123", ("AbC123",)),
    ("SKU `AbC123`、`Def456`", ("AbC123", "Def456")),
    ("SKU AbC123、Def456", ("AbC123", "Def456")),
    ("SKU `AbC123`、`AbC123`", ("AbC123",)),
])
def test_explicit_codes_preserve_case_quotes_and_lists(reference, codes):
    scope, _, unsupported = ScopeResolver().resolve("昨天" + reference + "实际销售出库")
    assert scope.sku_codes == codes
    assert not unsupported


def test_code_tokens_do_not_create_owner_product_or_warehouse_scope():
    scope, _, unsupported = ScopeResolver().resolve("昨天SKU `HJXH-TJWWC-777`实际销售出库")
    assert scope.sku_codes == ("HJXH-TJWWC-777",)
    assert scope.owner == "ALL" and not scope.warehouse_codes
    assert not unsupported


@pytest.mark.parametrize("question", ["昨天销售出库 SKU Top20", "昨天按SKU汇总实际销售出库"])
def test_dimension_and_ranking_words_are_not_codes(question):
    assert ScopeResolver().resolve(question)[0].sku_codes == ()


@pytest.mark.parametrize("reference", ["SKU `A' OR 1=1 --`", "SKU `" + "X" * 41 + "`", "SKU AbC123 except Def456"])
def test_malformed_and_unhandled_predicates_cannot_become_all_scope(reference):
    assert ScopeResolver().resolve("昨天" + reference + "实际销售出库")[2]


def test_following_request_does_not_reuse_explicit_codes():
    resolver = ScopeResolver()
    resolver.resolve("昨天SKU `AbC123`实际销售出库")
    assert resolver.resolve("昨天实际销售出库")[0].sku_codes == ()


@pytest.mark.parametrize("code", ["HJXH-TJWWC-777", "QY-2C-SO07", "QY-2026-08-01", "QY-Top20", "AbC123"])
def test_identifiers_cannot_become_planner_filters(code):
    planner=RequestPlanner(clock=lambda:datetime(2026,9,10,12))
    request=planner.classify(f"昨天SKU `{code}`实际销售出库")
    assert request and request.scope.sku_codes==(code,)
    assert request.start==date(2026,9,9) and request.end==date(2026,9,10)
    assert request.ref_order_type is None and request.fulfillment_type is None and request.top_n is None
    assert request.scope.owner=='ALL' and not request.scope.warehouse_codes


def test_box_planner_preserves_code_case_and_opaque_tokens(monkeypatch):
    root=Path(__file__).resolve().parents[2]
    monkeypatch.setattr('app.agent_core.planning.active_snapshot',lambda _: (root,None))
    question="查询昨天 JYCK 成品出库 SKU `AbC-2C-2026-08-01`，返回原始实发数量、箱规和实发箱数。箱数按汇总后的数量除以箱规，四舍五入且不保留小数。"
    request=RequestPlanner(clock=lambda:datetime(2026,9,10,12)).classify(question)
    assert request and request.scope.sku_codes==('AbC-2C-2026-08-01',)
    assert request.start==date(2026,9,9) and request.ref_order_type=='JYCK'
    assert request.fulfillment_type is None


def test_receipt_planner_preserves_explicit_identifier_case(monkeypatch):
    root=Path(__file__).resolve().parents[2]
    monkeypatch.setattr('app.agent_core.planning.active_snapshot',lambda _: (root,None))
    question="查询最近7个完整自然日生产日期的采购入库明细，不包含今天完成入库的数据，SKU `AbC-HJXH`，返回仓库、入库单、SKU、生产日期、实收入库数量和实际入库时间。"
    request=RequestPlanner(clock=lambda:datetime(2026,9,10,12)).classify(question)
    assert request and request.scope.sku_codes==('AbC-HJXH',)
    assert request.receipt_plan.sku_codes==('AbC-HJXH',) and request.scope.owner=='ALL'

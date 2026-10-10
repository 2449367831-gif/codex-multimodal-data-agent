"""Explicit SKU rankings execute the reviewed plan without a model round trip."""

from datetime import date, datetime
from itertools import product

import pytest

from app.agent_core.planning import RequestPlanner
from app.request_syntax import consume_sku_ranking_output, normalize_query_wording


QUESTION = "查询昨天HJXH货主的有效销售出库，按SKU汇总发货数量和去重后的出库单量，列出发货数量最高的前20个SKU"


@pytest.fixture
def planner():
    return RequestPlanner(clock=lambda: datetime(2026, 9, 20, 12))


@pytest.mark.parametrize("window,start", [
    ("昨天", date(2026, 9, 19)), ("最近7个完整自然日", date(2026, 9, 13)),
    ("最近14个完整自然日", date(2026, 9, 6)),
])
@pytest.mark.parametrize("quantity,documents,ranking", list(product(
    ["发货数量", "实发数量"], ["去重后的出库单量", "去重出库单数", "出库单数"],
    ["最高的前20个SKU", "最高的20个SKU", "TOP20 SKU"],
)))
def test_quantity_and_document_count_compose_with_ranking(planner, window, start, quantity, documents, ranking):
    request = planner.classify(f"查询{window}HJXH货主的有效销售出库，按SKU汇总{quantity}和{documents}，列出{quantity}{ranking}")
    assert request is not None
    assert request.route == "sales_outbound_top_sku"
    assert request.top_n == 20 and request.show_top_sku
    assert (request.start, request.end) == (start, date(2026, 9, 20))
    assert request.scope.owner == "HJXH"
    assert not request.scope.finished_goods
    assert not request.scope.warehouse_codes and not request.scope.sku_codes
    assert request.scope.channel == "ALL" and request.scope.unit == "EA"


def test_scope_does_not_carry_over_and_sku_literals_remain_opaque(planner):
    assert planner.classify(QUESTION).scope.owner == "HJXH"
    request = planner.classify(QUESTION.replace("HJXH货主的", ""))
    assert request.scope.owner == "ALL"
    for literal in ("'发货数量'", '"去重后的出库单量"', '`SKU-top20-发货数量`'):
        assert literal in normalize_query_wording(QUESTION + "，名称包含" + literal)


@pytest.mark.parametrize("change", [
    lambda q: q + "，渠道为抖音",
    lambda q: q + "，只统计未知仓库",
    lambda q: q + "，排除客户甲",
    lambda q: q + "，单位为箱",
    lambda q: q + "，按订单创建时间",
    lambda q: q + "，改用分配数量",
    lambda q: q.replace("按SKU", "按仓库和SKU"),
    lambda q: q.replace("按SKU", "按日期和SKU"),
    lambda q: q.replace("发货数量最高", "出库单量最高"),
    lambda q: q.replace("发货数量最高", "日均发货数量最高"),
    lambda q: q.replace("20", "101"),
])
def test_ranking_never_discards_unsupported_scope_or_changes_sort_measure(planner, change):
    request = planner.classify(change(QUESTION))
    assert request is None or request.route != "sales_outbound_top_sku"


def test_quantity_basis_is_not_inferred_for_unqualified_or_planned_shipments():
    for question in ("昨天发货数量", "昨天计划发货数量", "昨天实际销售出库和计划发货数量"):
        assert normalize_query_wording(question) == question


def test_ranked_quantity_and_outbound_count_are_supported_together(planner):
    assert planner.classify("昨天销售出库Top20 SKU，返回数量和出库单数").route == "sales_outbound_top_sku"


@pytest.mark.parametrize("question", [
    "查询昨天HJXH销售出库，按SKU汇总实际发货量、出库单量，取发货量TOP20",
    "查询昨天HJXH销售出库，按SKU汇总出库数量和去重出库单数，按数量降序取前20个",
    "昨天HJXH实际销售出库按SKU统计数量和出库单数，取销量最高20个",
    "查询最近14个完整自然日HJXH有效销售出库，按SKU汇总出库量、出库单量，列出前20个SKU",
    "昨天销售出库Top20 SKU，返回排名、SKU编码、SKU名称、实发数量、出库单数",
    "昨天实际销售出库TOP20 SKU，列出SKU、商品名称、发货数量、去重出库单量",
])
def test_common_measure_wording_reaches_the_same_reviewed_ranking(planner, question):
    request = planner.classify(question)
    assert request is not None and request.route == "sales_outbound_top_sku"
    assert request.top_n == 20


@pytest.mark.parametrize("sort", [
    "数量降序", "按实发数量降序", "实发数量降序", "发货数量从高到低",
    "按出库量降序排列", "销量从高到低排序",
])
def test_descending_quantity_command_is_not_a_scope_filter(planner, sort):
    request = planner.classify(
        f"昨天HJXH实际销售出库，按SKU汇总发货数量和出库单数，{sort}前20个SKU"
    )
    assert request is not None and request.route == "sales_outbound_top_sku"
    assert request.top_n == 20 and request.scope.owner == "HJXH"


@pytest.mark.parametrize("sort", ["出库单量降序", "出库单数量降序", "数量升序", "数量从低到高"])
def test_other_sort_orders_are_not_removed(planner, sort):
    request = planner.classify(
        f"昨天HJXH实际销售出库，按SKU汇总发货数量和出库单数，{sort}前20个SKU"
    )
    assert request is None or request.route != "sales_outbound_top_sku"


def test_ranking_output_cleanup_preserves_quoted_business_values():
    question = 'SKU `实发数量降序`，名称包含"输出SKU编码、数量"，按实发数量降序'
    cleaned = consume_sku_ranking_output(question)
    assert '`实发数量降序`' in cleaned
    assert '"输出SKU编码、数量"' in cleaned
    assert cleaned.endswith('，')


@pytest.mark.parametrize("question", [
    "昨天HJXH实际销售出库，按SKU汇总分配数量和出库单量，列出数量最高的前20个SKU",
    "昨天HJXH实际销售出库，按SKU汇总发货数量，列出出库单量最高的前20个SKU",
    "昨天HJXH实际销售出库，按仓库和SKU汇总发货数量和出库单量，列出发货数量最高的前20个SKU",
    "昨天HJXH实际销售出库，按SKU汇总发货数量和出库单量，按订单创建时间取前20",
])
def test_unsupported_measure_or_grain_never_falls_into_quantity_ranking(planner, question):
    request = planner.classify(question)
    assert request is None or request.route != "sales_outbound_top_sku"


def test_actual_shipment_synonym_is_normalized_without_doubling_actual(planner):
    normalized = normalize_query_wording("查询昨天销售出库，按SKU汇总实际发货量和出库单量")
    assert "实际实发" not in normalized
    assert "实发数量" in normalized


@pytest.mark.parametrize("suffix", [
    "返回SKU编码、实发数量、销售订单数", "返回SKU编码、请求数量、出库单数",
    "返回SKU编码、实发数量、仓库名称", "返回SKU编码、分配数量、出库单数",
    "返回SKU编码、实发数量、出库单数，排除客户甲", "返回SKU编码、实发数量、出库单数大于10",
])
def test_ranking_column_list_cannot_erase_other_measures_or_predicates(planner, suffix):
    assert planner.classify("昨天实际销售出库TOP20 SKU，" + suffix) is None


def test_ranking_column_list_preserves_explicit_identifier(planner):
    request = planner.classify("昨天实际销售出库TOP20 SKU，SKU `AbC123`，返回SKU编码、实发数量、出库单数")
    assert request.scope.sku_codes == ("AbC123",)


@pytest.mark.parametrize("count", [1, 5, 10, 20, 50, 100])
@pytest.mark.parametrize("window", ["昨天", "最近7个完整自然日", "最近14个完整自然日", "上个月"])
def test_rank_limit_composes_with_any_reviewed_window(planner, count, window):
    request = planner.classify(QUESTION.replace("昨天", window).replace("20", str(count)))
    assert request is not None and request.route == "sales_outbound_top_sku"
    assert request.top_n == count
    assert request.scope.owner == "HJXH"


@pytest.mark.parametrize("empty", [False, True])
def test_chat_executes_once_without_model_and_returns_verified_table(tmp_path, empty):
    from fastapi.testclient import TestClient
    from app.chat_api import create_chat_app
    from app.chat_api import IdentityResolver
    from app.chat_sessions import ChatSessionStore
    from app.semantic_runtime import SemanticQueryRuntime
    from app.tests.test_chat_api import FakeChatRunner
    from app.tests.test_semantic_runtime import RecordingQueryClient

    rows = [] if empty else [dict(
        sku_code=f"SKU{rank:03d}", sku_name=f"测试商品{rank}", outbound_ea=str(21-rank),
        outbound_order_count=21-rank, total_ea="210", all_sku_count=20,
        blank_sku_count=0, sku_name_conflict_count=0, negative_detail_count=0,
    ) for rank in range(1, 21)]
    query = RecordingQueryClient({"actual sales outbound top sku": rows})
    runner = FakeChatRunner()
    runtime = SemanticQueryRuntime(query_client=query, clock=lambda: datetime(2026, 9, 20, 12))
    app = create_chat_app(runner, semantic_runtime=runtime,
        session_store=ChatSessionStore(tmp_path / "sessions.sqlite3"),
        identity_resolver=IdentityResolver(mode="anonymous"), enforce_loopback=False)
    with TestClient(app) as client:
        session = client.get("/v1/session").json()
        headers = {"X-CSRF-Token": session["csrf_token"], "Idempotency-Key": "direct-ranking-test-0001"}
        conversation = client.post("/v1/conversations", headers=headers).json()["conversation_id"]
        payload = {
            "execution_mode": "rule",
            "rule_id": "actual_sales_top_sku",
            "rule_params": {
                "window": "yesterday",
                "owner": "HJXH",
                "top_n": "20",
            },
            "conversation_id": conversation,
        }
        first = client.post("/v1/chat", headers=headers, json=payload)
        assert first.status_code == 200, first.json()
        answer = first.json()
        assert answer["query_status"] == ("empty_result" if empty else "succeeded")
        table = answer["business_table"]
        assert len(table["rows"]) == len(rows)
        assert [column["label"] for column in table["columns"]][-1] == "出库单数"
        if not empty:
            assert table["rows"][0]["quantity"] == "20"
            assert table["rows"][0]["order_count"] == "20"
        assert "2026年9月19日" in table["caption"] and "HJXH" in table["caption"]
        if empty:
            assert answer["message"] == "查询成功，但当前条件下没有符合条件的数据。"
        second = client.post("/v1/chat", headers=headers, json=payload)
        assert second.status_code == 200
        assert second.json()["business_table"] == table
    assert runner.questions == []
    assert len(query.calls) == 1
    sql = query.calls[0]["sql"]
    assert "ship_qty" in sql and "allocate_qty" not in sql
    assert "COUNT(DISTINCT h.cargo_owner, h.outbound_code)" in sql
    assert "2026-09-19" in sql and "2026-09-20" in sql

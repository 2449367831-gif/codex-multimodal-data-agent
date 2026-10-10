"""Inventory timing is explicit; a historical sales window is not a snapshot."""

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.agent import AgentWorkflow
from app.agent_core.planning import RequestPlanner


@pytest.fixture
def planner(monkeypatch):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.setattr("app.query_scope.active_snapshot", lambda _: (root, None))
    return RequestPlanner(clock=lambda: datetime(2026, 9, 11, 12,
                                                tzinfo=ZoneInfo("Asia/Shanghai")))


@pytest.mark.parametrize("wording", [
    "历史库存", "历史成品库存", "库存快照", "成品库存快照",
    "期末库存", "期末成品库存", "区间末库存", "区间末成品库存",
])
def test_explicit_snapshot_routes_directly_and_after_selection(planner, wording):
    question = f"2026年8月{wording}周转"
    direct = planner.classify(question)
    selected = planner.plan_selected(question, [
        {"question_id": "turnover_inventory_point", "option_id": "period_end_finished_goods_snapshot"},
    ])
    for request in (direct, selected):
        assert request is not None
        assert request.route == "historical_turnover"
        assert (request.start, request.end) == (date(2026, 8, 1), date(2026, 9, 1))
        assert request.scope.owner == "ALL"
        assert request.scope.finished_goods
    assert AgentWorkflow._turnover_route(question, now=planner._now()) == "inventory.turnover_finished_goods_snapshot"


@pytest.mark.parametrize("question", [
    "上月库存周转", "上个月成品库存周转", "2026年8月库存周转",
    "2026年8月当前库存周转", "2026年8月实时成品库存周转",
])
def test_historical_sales_window_keeps_realtime_default(planner, question):
    request = planner.classify(question)
    assert request is not None
    assert request.route == "realtime_turnover"
    assert (request.start, request.end) == (date(2026, 8, 1), date(2026, 9, 1))
    assert AgentWorkflow._turnover_route(question, now=planner._now()) == "inventory.turnover_finished_goods_realtime"


def test_explicit_published_current_formula_does_not_trigger_clarification(planner):
    request = planner.classify(
        "上月库存周转是多少？按当前可用成品库存加在途，除以该期间实际销售出库成品日均数量。"
    )
    assert request is not None
    assert request.route == "realtime_turnover"
    assert (request.start, request.end) == (date(2026, 8, 1), date(2026, 9, 1))
    assert request.scope.finished_goods


@pytest.mark.parametrize("position", ["before", "inside", "after"])
@pytest.mark.parametrize("scope_text,field,expected", [
    ("SKU AbC-2026-08-15", "sku_codes", ("AbC-2026-08-15",)),
    ("SKU 10001、10002", "sku_codes", ("10001", "10002")),
    ("皇家小虎", "owner", "HJXH"),
    ("长沙黄兴仓", "warehouse_codes", ("10114790",)),
])
def test_formula_scope_is_not_discarded_by_clause_position(planner, position, scope_text, field, expected):
    numerator = "按当前可用成品库存加在途"
    denominator = "除以该期间实际销售出库成品日均数量"
    clauses = {
        "before": [scope_text, numerator, denominator],
        "inside": [numerator, scope_text, denominator],
        "after": [numerator, denominator, scope_text],
    }
    question = "上月库存周转，" + "，".join(clauses[position])
    for request in (planner.classify(question), planner.plan_selected(question, [
        {"question_id": "turnover_inventory_point", "option_id": "current_realtime_finished_goods"},
    ])):
        assert request is not None
        assert request.route == "realtime_turnover"
        assert getattr(request.scope, field) == expected
        assert request.scope.finished_goods
        assert (request.start, request.end) == (date(2026, 8, 1), date(2026, 9, 1))
    next_request = planner.classify("上月库存周转")
    assert next_request.scope.owner == "ALL"
    assert next_request.scope.sku_codes == ()
    assert next_request.scope.warehouse_codes == ()


@pytest.mark.parametrize("scope_text", [
    "排除长沙黄兴仓", "只要库存超过1000的商品", "仅限某客户", "线上渠道",
    "所有SKU，SKU 10001", "全部仓库，长沙黄兴仓", "全部货主，皇家小虎",
    "库存>1000", "库存≤1000", "×2", "+10", "-10", "/2",
])
def test_formula_cleanup_does_not_hide_unsupported_predicates_or_conflicts(planner, scope_text):
    from app.codex_agent import reviewed_clarification_for_question

    question = f"上月库存周转，按当前可用成品库存加在途，除以该期间实际销售出库成品日均数量，{scope_text}"
    assert planner.classify(question) is None
    assert planner.plan_selected(question, [
        {"question_id": "turnover_inventory_point", "option_id": "current_realtime_finished_goods"},
    ]) is None
    assert reviewed_clarification_for_question(question) is None


def test_formula_sku_filter_reaches_both_stock_and_sales_sources(planner):
    import sqlglot
    from sqlglot import exp
    from app.agent_core.execution import QueryExecutionSession
    from app.agent_core.handlers.turnover import TurnoverHandler

    code = "AbC-2026-08-15"
    questions = [
        f"上月SKU `{code}`库存周转",
        f"上月库存周转，按当前可用成品库存加在途，除以该期间实际销售出库成品日均数量，SKU `{code}`",
    ]
    statements = []
    for question in questions:
        request = planner.classify(question)
        assert request is not None
        execution = QueryExecutionSession(client=None, scope=request.scope, legacy_owner="HJXH")
        handler = TurnoverHandler(execution, timezone_name="Asia/Shanghai")
        sql = execution._scoped_sql(handler._realtime_turnover_sql(request.start, request.end))
        statements.append(sql)
        tree = sqlglot.parse_one(sql, read="doris")
        for name in ("sku_inventory", "scm_sales_shipment_detail"):
            sources = [table for table in tree.find_all(exp.Table) if table.name == name]
            assert sources
            for source in sources:
                predicate = source.find_ancestor(exp.Select).args.get("where")
                assert predicate is not None
                assert code in {literal.this for literal in predicate.find_all(exp.Literal) if literal.is_string}
        assert "allocate_qty" not in sql
    assert statements[0] == statements[1]


@pytest.mark.parametrize("operation", ["库存>1000", "×2", "/2"])
def test_composed_formula_reaches_general_model_without_losing_request(planner, tmp_path, operation):
    from fastapi.testclient import TestClient
    from app.agent_core.runtime import SemanticQueryRuntime
    from app.chat_api import create_chat_app, IdentityResolver
    from app.chat_sessions import ChatSessionStore
    from app.tests.test_chat_api import FakeChatRunner
    from app.tests.test_semantic_runtime import RecordingQueryClient

    runner, source = FakeChatRunner(), RecordingQueryClient({})
    runtime = SemanticQueryRuntime(query_client=source, clock=planner.clock)
    app = create_chat_app(runner, semantic_runtime=runtime,
        session_store=ChatSessionStore(tmp_path / "sessions.sqlite3"),
        identity_resolver=IdentityResolver(mode="anonymous"), enforce_loopback=False)
    question = f"上月库存周转，按当前可用成品库存加在途，除以该期间实际销售出库成品日均数量，{operation}"
    with TestClient(app) as client:
        session = client.get("/v1/session").json()
        headers = {"X-CSRF-Token": session["csrf_token"], "Idempotency-Key": "composed-formula-routing"}
        conversation = client.post("/v1/conversations", headers=headers).json()["conversation_id"]
        payload = {"question": question, "conversation_id": conversation}
        response = client.post("/v1/chat", headers=headers, json=payload)
        assert response.status_code == 200, response.json()
        assert runner.questions == [question]
        assert not source.calls
        assert response.json()["clarification"]["required"] is False
        assert client.post("/v1/chat", headers=headers, json=payload).json() == response.json()
        assert runner.questions == [question]


def test_explicit_current_turnover_by_warehouse_uses_reconciled_route(planner):
    request = planner.classify(
        "当前可用成品库存加在途除以近14天平均实际销售成品数量，按仓库计算库存周转"
    )
    assert request is not None
    assert request.route == "realtime_turnover_by_warehouse"
    assert request.turnover_grain == "warehouse"
    assert (request.start, request.end) == (date(2026, 8, 28), date(2026, 9, 11))
    assert request.scope.owner == "ALL"


def test_short_warehouse_turnover_wording_uses_active_default_basis(planner):
    request = planner.classify("近14天库存周转按仓库")
    assert request is not None
    assert request.route == "realtime_turnover_by_warehouse"
    assert request.turnover_grain == "warehouse"


@pytest.mark.parametrize("question", [
    "2026年8月历史库存周转线上渠道",
    "2026年8月历史库存周转神秘仓",
    "2026年8月历史库存周转仅限某客户",
    "2026年8月历史平均库存周转",
    "2026年8月历史库存周转不含在途",
    "2026年8月历史库存周转箱",
    "2026年8月历史库存周转各仓",
    "2026年8月历史库存周转按SKU",
])
def test_snapshot_normalization_does_not_discard_scope_or_formula(planner, question):
    assert planner.classify(question) is None
    assert planner.plan_selected(question, [
        {"question_id": "turnover_inventory_point", "option_id": "period_end_finished_goods_snapshot"},
    ]) is None


@pytest.mark.parametrize("question", [
    "2026年8月当前历史库存周转", "2026年8月实时库存快照周转",
    "2026年8月历史库存和当前库存周转",
    "2026年8月指定日期库存周转", "2026年8月月初库存周转",
    "2026年8月15日库存快照计算2026年8月库存周转",
    "2026年10月历史库存周转",
])
def test_conflicting_or_unsupported_snapshot_timing_is_not_guessed(planner, question):
    assert planner.classify(question) is None


def test_snapshot_words_do_not_enable_an_unrelated_fixed_route(planner):
    assert planner.classify("昨天历史库存销售出库量") is None


@pytest.mark.parametrize("question", [
    "2026年8月15日库存快照计算2026年8月库存周转",
    "2026年8月15日库存快照计算上月库存周转",
    "2026年10月历史库存周转",
])
def test_selection_does_not_guess_snapshot_date_or_include_future_sales(planner, question):
    assert planner.plan_selected(question, [
        {"question_id": "turnover_inventory_point", "option_id": "period_end_finished_goods_snapshot"},
    ]) is None


def test_historical_scope_preserves_exact_sku_codes_and_resets_next_request(planner):
    question = "2026年8月皇家小虎SKU AbC-2026-08-15历史库存周转"
    selected = planner.plan_selected(question, [
        {"question_id": "turnover_inventory_point", "option_id": "period_end_finished_goods_snapshot"},
    ])
    for request in (planner.classify(question), selected):
        assert request is not None
        assert request.route == "historical_turnover"
        assert request.scope.sku_codes == ("AbC-2026-08-15",)
        assert request.scope.owner == "HJXH"
        assert request.start == date(2026, 8, 1)
    next_request = planner.classify("上月库存周转")
    assert next_request.route == "realtime_turnover"
    assert next_request.scope.owner == "ALL"
    assert next_request.scope.sku_codes == ()


@pytest.mark.parametrize("question,start,end", [
    ("2026年8月1日至2026年8月31日历史库存周转", date(2026, 8, 1), date(2026, 9, 1)),
    ("2026-08-01至2026-09-01 00:00库存快照周转", date(2026, 8, 1), date(2026, 9, 1)),
    ("昨天期末库存周转", date(2026, 9, 10), date(2026, 9, 11)),
])
def test_snapshot_period_keeps_inclusive_and_midnight_boundaries(planner, question, start, end):
    request = planner.classify(question)
    assert request is not None
    assert request.route == "historical_turnover"
    assert (request.start, request.end) == (start, end)

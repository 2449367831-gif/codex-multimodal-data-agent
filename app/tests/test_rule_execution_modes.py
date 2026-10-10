from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.chat_api import IdentityResolver, create_chat_app
from app.chat_sessions import ChatSessionStore
from app.rule_catalog import build_rule_request, rule_catalog
from app.tests.test_chat_api import FakeChatRunner
from app.codex_agent import interaction_envelope
import json
import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("question", [
    "查询本月采购入库数量最高的前10个SKU",
    "查询昨天各仓库的实际销售出库量",
    "生成中心仓和分仓14天周转数据图片",
    "查询上周线下渠道出库数量、出库单量和箱数",
])
def test_natural_language_never_preselects_or_executes_a_rule(tmp_path, question):
    class RuleExecutor:
        def classify(self, *args, **kwargs):
            raise AssertionError("The model must select the metric first")

        def execute_plan(self, *args, **kwargs):
            raise AssertionError("No rule may execute before a model plan")

    runner = FakeChatRunner()
    with TestClient(create_chat_app(
        runner, session_store=ChatSessionStore(tmp_path / "sessions.sqlite3"),
        semantic_runtime=RuleExecutor(), identity_resolver=IdentityResolver(mode="anonymous"),
        enforce_loopback=False,
    )) as client:
        session = client.get("/v1/session").json()
        headers = {"X-CSRF-Token": session["csrf_token"]}
        conversation_id = client.post("/v1/conversations", headers=headers).json()["conversation_id"]
        response = client.post("/v1/chat", headers=headers, json={
            "question": question, "conversation_id": conversation_id,
        })
    assert response.status_code == 200
    assert runner.questions == [question]


def test_model_choice_copy_and_ids_are_not_replaced_by_legacy_positions():
    question = {
        "id": "metric", "prompt": "请选择采购入库统计方式", "selection": "single",
        "options": [
            {"id": "purchase_top_skus", "label": "本月采购入库数量最高的前10个SKU"},
            {"id": "purchase_by_warehouse", "label": "按仓库汇总采购入库数量"},
        ],
    }
    envelope = interaction_envelope(json.dumps({
        "outcome": "clarification_required", "questions": [question],
    }, ensure_ascii=False))
    assert envelope["outcome"] == "clarification_required"
    normalized = envelope["questions"][0]
    assert normalized["prompt"] == question["prompt"]
    assert [opt["id"] for opt in normalized["options"]] == [opt["id"] for opt in question["options"]]
    assert [opt["label"] for opt in normalized["options"]] == [opt["label"] for opt in question["options"]]


@pytest.mark.parametrize("has_result", [True, False])
def test_custom_model_choices_survive_persistence_and_selection(tmp_path, monkeypatch, has_result):
    store = ChatSessionStore(tmp_path / "sessions.sqlite3")

    def reject_rule_reclassification(*args, **kwargs):
        raise AssertionError("Model selections must not be reinterpreted by fixed rules")

    monkeypatch.setattr(
        "app.agent_core.outbound_clarification.selected_outbound_block_reason",
        reject_rule_reclassification,
    )

    class ClarifyingRunner(FakeChatRunner):
        def run(self, question, **kwargs):
            self.questions.append((question, kwargs))
            if kwargs.get("ui_selections"):
                if has_result:
                    return super().run(question)
                return {
                    "status": "completed", "thread_id": "thread-test",
                    "interaction": {"outcome": "blocked", "questions": []},
                    "final_response": json.dumps({"outcome": "blocked", "questions": []}),
                }
            interaction = {
                "outcome": "clarification_required", "questions": [{
                    "id": "receipt_measure", "prompt": "请选择入库数量口径", "selection": "single",
                    "options": [
                        {"id": "received_quantity", "label": "实收入库数量"},
                        {"id": "planned_quantity", "label": "计划入库数量"},
                    ],
                }],
            }
            return {"status": "completed", "thread_id": "thread-test",
                    "interaction": interaction, "final_response": json.dumps(interaction, ensure_ascii=False)}

    runner = ClarifyingRunner()
    with TestClient(create_chat_app(
        runner, session_store=store, identity_resolver=IdentityResolver(mode="anonymous"),
        enforce_loopback=False,
    )) as client:
        session = client.get("/v1/session").json()
        headers = {"X-CSRF-Token": session["csrf_token"]}
        conversation_id = client.post("/v1/conversations", headers=headers).json()["conversation_id"]
        first = client.post("/v1/chat", headers=headers, json={
            "question": "查询本月采购入库前10个SKU", "conversation_id": conversation_id,
        }).json()
        questions = first["clarification"]["questions"]
        assert questions[0]["id"] == "receipt_measure"
        assert questions[0]["options"][0]["id"] == "received_quantity"
        second = client.post("/v1/chat", headers=headers, json={
            "conversation_id": conversation_id,
            "clarification_token": first["clarification"]["token"],
            "clarification_answers": [
                {"question_id": "receipt_measure", "option_id": "received_quantity",
                 "prompt": "篡改问题", "selected_options": [{"id": "received_quantity", "label": "篡改答案"}]},
            ],
        })
        assert second.status_code == (200 if has_result else 502)
        if not has_result:
            assert second.json()["error"]["code"] == "query_plan_invalid"
        assert runner.questions[1][1]["ui_selections"] == [
            {"question_id": "receipt_measure", "option_ids": ["received_quantity"],
             "prompt": "请选择入库数量口径",
             "selected_options": [{"id": "received_quantity", "label": "实收入库数量"}]},
        ]
        assert not second.json().get("clarification", {}).get("required")


def test_rule_catalog_compiles_explicit_conditions() -> None:
    catalog = rule_catalog(ROOT)
    assert catalog["version"] == "rules-v2"
    assert {item["id"] for item in catalog["rules"]} >= {
        "actual_sales_by_warehouse",
        "inventory_turnover_by_warehouse",
        "center_warehouse_turnover_report",
    }
    warehouse_field = next(
        field
        for field in next(
            item for item in catalog["rules"] if item["id"] == "actual_sales_by_warehouse"
        )["fields"]
        if field["id"] == "warehouses"
    )
    assert len(warehouse_field["options"]) == 51
    assert any(option["id"] == "WMS000296" for option in warehouse_field["options"])
    assert any(option["id"] == "P854CSB" for option in warehouse_field["options"])

    request = build_rule_request(
        "actual_sales_by_warehouse",
        {
            "window": "custom",
            "start_date": "2026-09-01",
            "end_date": "2026-09-15",
            "owner": "HJXH",
            "warehouses": ["10114790"],
        },
        root=ROOT,
        now=datetime(2026, 9, 20, 12, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    assert "2026-09-01" in request["question"]
    assert "2026-09-15" in request["question"]
    assert "长沙黄兴" in request["question"]
    assert request["plan"].route == "business_sales_by_warehouse"
    assert request["plan"].start.isoformat() == "2026-09-01"
    assert request["plan"].end.isoformat() == "2026-09-15"
    assert request["plan"].scope.owner == "HJXH"

    turnover = build_rule_request(
        "center_warehouse_turnover_report",
        {},
        root=ROOT,
        now=datetime(2026, 9, 23, 12, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    assert turnover["report_kind"] == "center_warehouse_turnover_7d"
    assert turnover["report_profile_id"] == "actual_finished_goods"
    assert turnover["window_days"] == 14
    assert "图片" in turnover["question"]

    with __import__("pytest").raises(ValueError):
        build_rule_request(
            "actual_sales_by_warehouse", {"window": "yesterday", "unsupported": "x"},
            root=ROOT, now=datetime(2026, 9, 20, 12, tzinfo=ZoneInfo("Asia/Shanghai")),
        )


def test_natural_language_uses_model_and_rule_request_does_not(tmp_path) -> None:
    runner = FakeChatRunner()
    app = create_chat_app(
        runner,
        session_store=ChatSessionStore(tmp_path / "sessions.sqlite3"),
        identity_resolver=IdentityResolver(mode="anonymous"),
        enforce_loopback=False,
    )
    with TestClient(app) as client:
        session = client.get("/v1/session").json()
        headers = {
            "X-CSRF-Token": session["csrf_token"],
            "Idempotency-Key": "rule-mode-regression-0001",
        }
        assert client.get("/v1/rules", headers=headers).status_code == 200
        conversation_id = client.post(
            "/v1/conversations", headers=headers
        ).json()["conversation_id"]

        natural = client.post(
            "/v1/chat",
            headers=headers,
            json={
                "question": "查询昨天各仓库的实际销售出库量",
                "conversation_id": conversation_id,
            },
        )
        assert natural.status_code == 200
        assert runner.questions == ["查询昨天各仓库的实际销售出库量"]

        rule = client.post(
            "/v1/chat",
            headers={**headers, "Idempotency-Key": "rule-mode-regression-0002"},
            json={
                "execution_mode": "rule",
                "rule_id": "actual_sales_by_warehouse",
                "rule_params": {
                    "window": "yesterday",
                    "owner": "HJXH",
                    "warehouses": [],
                },
                "conversation_id": conversation_id,
            },
        )
        assert rule.status_code == 200
        assert len(runner.questions) == 1
        assert rule.json()["query_status"] == "not_executed"


def test_explicit_rule_dispatches_bound_plan_without_model(tmp_path) -> None:
    class DirectExecutor:
        def __init__(self):
            self.calls = []

        def execute_plan(self, request, *, question, context_reused=False):
            self.calls.append((request, question, context_reused))
            interaction = {"outcome": "completed", "answer": "已完成", "questions": []}
            return {
                "status": "completed",
                "final_response": __import__("json").dumps(interaction, ensure_ascii=False),
                "thread_id": None,
                "interaction": interaction,
                "query_execution": {
                    "live_business_result_executed": True,
                    "status": "succeeded",
                },
                "mcp_calls": [{"server": "doris_query_agent", "tool": "query_doris_sql"}],
                "query_plan": {
                    "route": request.route,
                    "scope": request.scope.as_dict(),
                },
                "business_table": {
                    "format": "table",
                    "result_id": "test_result",
                    "result_title": "按仓销售出库",
                    "table_id": "sales_outbound_by_warehouse",
                    "title": "按仓销售出库",
                    "caption": "测试",
                    "columns": [{"key": "warehouse", "label": "仓库", "kind": "text"}, {"key": "quantity", "label": "实际销售出库", "kind": "number", "unit": "EA"}],
                    "rows": [{"warehouse": "仓甲", "quantity": "1"}],
                },
            }

    executor = DirectExecutor()
    app = create_chat_app(
        FakeChatRunner(),
        session_store=ChatSessionStore(tmp_path / "sessions.sqlite3"),
        identity_resolver=IdentityResolver(mode="anonymous"),
        semantic_runtime=executor,
        enforce_loopback=False,
    )
    with TestClient(app) as client:
        session = client.get("/v1/session").json()
        conversation_id = client.post(
            "/v1/conversations", headers={"X-CSRF-Token": session["csrf_token"]}
        ).json()["conversation_id"]
        response = client.post(
            "/v1/chat",
            headers={
                "X-CSRF-Token": session["csrf_token"],
                "Idempotency-Key": "rule-direct-plan-0001",
            },
            json={
                "execution_mode": "rule",
                "rule_id": "actual_sales_by_warehouse",
                "rule_params": {"window": "custom", "start_date": "2026-09-01", "end_date": "2026-09-15", "owner": "ALL"},
                "conversation_id": conversation_id,
            },
        )
    assert response.status_code == 200, response.text
    assert len(executor.calls) == 1
    plan = executor.calls[0][0]
    assert plan.route == "business_sales_by_warehouse"
    assert plan.start.isoformat() == "2026-09-01"
    assert plan.end.isoformat() == "2026-09-15"
    assert plan.scope.owner == "ALL"

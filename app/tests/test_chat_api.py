from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import threading
from datetime import datetime
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.chat_api import (
    ChatExecutionError,
    ChatRunner,
    IdentityResolver,
    _prepare_model_question,
    _hidden_process_options,
    _public_paged_business_table,
    _process_failure_signature,
    _windowless_python,
    create_chat_app,
)
from app.chat_sessions import ChatSessionStore, SESSION_COOKIE_NAME
from app.result_pages import ResultPageStore
from app.semantic_runtime import SemanticQueryRuntime, SemanticRuntimeError


class ModelQuestionPreparationTestCase(unittest.TestCase):
    def test_model_question_is_not_rewritten_before_planning(self) -> None:
        prepared = _prepare_model_question("查询上周线下渠道出库数量、出库单量和箱数，按日期汇总")
        self.assertEqual(
            "查询上周线下渠道出库数量、出库单量和箱数，按日期汇总",
            prepared,
        )

    def test_explicit_channel_replacement_is_not_overridden(self) -> None:
        question = "查询上周社区平台替换口径出库量"
        self.assertEqual(question, _prepare_model_question(question))


class FakeChatRunner:
    backend_url = "http://127.0.0.1:9"
    model = "gpt-5.6-sol"
    reasoning_effort = "xhigh"
    auth_configured = True

    def __init__(self) -> None:
        self.questions: list[str] = []

    def run(self, question: str) -> dict[str, object]:
        self.questions.append(question)
        return {
            "status": "completed",
            "final_response": "查询完成",
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "thread_id": "thread-test",
            "turn_id": "turn-test",
            "mcp_calls": [
                {
                    "server": "doris_query_agent",
                    "tool": "query_doris_sql",
                    "status": "completed",
                }
            ],
            "query_execution": {
                "live_query_executed": True,
                "status": "live_result",
            },
            "verified_business_result": {
                "columns": ["库存数量"], "rows": [{"库存数量": "128"}],
            },
            "usage": None,
            "elapsed_ms": 2,
        }

    def public_health(self, _backend_health: dict[str, object] | None) -> dict[str, object]:
        return {
            "status": "ok",
            "ready": True,
            "query_backend": {"ready": True, "status": "ok"},
        }


def signed_headers(user_id: str, secret: str) -> dict[str, str]:
    timestamp = str(int(time.time()))
    message = f"{timestamp}.{user_id}".encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return {
        "X-WeCom-User-Id": user_id,
        "X-WeCom-Identity-Timestamp": timestamp,
        "X-WeCom-Identity-Signature": signature,
    }


class ChatApiIsolationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory(prefix="chat-api-tests-")
        self.runner = FakeChatRunner()
        self.store = ChatSessionStore(Path(self.directory.name) / "sessions.sqlite3")
        self.client = TestClient(
            create_chat_app(
                self.runner,
                session_store=self.store,
                identity_resolver=IdentityResolver(mode="anonymous"),
                enforce_loopback=False,
            )
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_unapproved_lan_browser_is_redirected_but_api_stays_json(self) -> None:
        with patch("app.chat_api._chat_network_mode", return_value="lan"), patch(
            "app.chat_api._request_is_allowed_network", return_value=False
        ):
            landing = self.client.get(
                "/",
                headers={"Accept": "text/html"},
                follow_redirects=False,
            )
            self.assertEqual(303, landing.status_code)
            self.assertEqual("/network-access/request", landing.headers["location"])

            api = self.client.get(
                "/v1/health",
                headers={"Accept": "text/html"},
                follow_redirects=False,
            )
            self.assertEqual(403, api.status_code)
            self.assertEqual("network_access_pending", api.json()["error"]["code"])

            request_page = self.client.get("/network-access/request")
            self.assertEqual(200, request_page.status_code)

    def test_complete_route_table_can_be_paged_without_partial_publication(self) -> None:
        store = ResultPageStore()
        rows = [
            {
                "cargo_owner": "owner",
                "warehouse_code": f"W{index:04d}",
                "warehouse_name": "沈阳仓",
                "warehouse_type": "中心仓",
            }
            for index in range(501)
        ]
        table = _public_paged_business_table(
            {
                "format": "table",
                "table_id": "warehouse_directory_detail",
                "rows": rows,
                "caption": "完整清单",
            },
            pages_store=store,
            owner="principal",
            live=True,
        )
        self.assertEqual(6, table["pagination"]["page_count"])
        self.assertEqual(501, table["pagination"]["total_rows"])
        self.assertEqual(100, len(table["rows"]))
        last = store.page("principal", table["pagination"]["result_id"], 6)
        self.assertEqual(1, len(last["rows"]))
        self.assertEqual("W0500", last["rows"][0]["warehouse_code"])

    def test_bootstrap_sets_cookie_and_chat_requires_csrf(self) -> None:
        bootstrap = self.client.get("/v1/session?new_conversation=true")
        self.assertEqual(200, bootstrap.status_code)
        payload = bootstrap.json()
        self.assertIsNone(payload["conversation_id"])
        self.assertGreater(len(payload["csrf_token"]), 20)
        self.assertIn("doris_query_session", bootstrap.headers.get("set-cookie", ""))

        conversation = self.client.post(
            "/v1/conversations",
            headers={"X-CSRF-Token": payload["csrf_token"]},
        )
        self.assertEqual(200, conversation.status_code)
        conversation_id = conversation.json()["conversation_id"]
        self.assertTrue(conversation_id.startswith("c_"))

        missing_csrf = self.client.post(
            "/v1/chat",
            json={"question": "查询库存", "conversation_id": conversation_id},
        )
        self.assertEqual(403, missing_csrf.status_code)
        self.assertEqual("session_security_error", missing_csrf.json()["error"]["code"])

        response = self.client.post(
            "/v1/chat",
            headers={"X-CSRF-Token": payload["csrf_token"]},
            json={"question": "查询库存", "conversation_id": conversation_id},
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual(conversation_id, response.json()["conversation_id"])
        self.assertEqual(["查询库存"], self.runner.questions)

    def test_natural_comparison_reaches_model_before_rule_resolution(self) -> None:
        bootstrap = self.client.get("/v1/session").json()
        headers = {"X-CSRF-Token": bootstrap["csrf_token"], "Idempotency-Key": "req_units_deferred_123"}
        conversation_id = self.client.post("/v1/conversations", headers=headers).json()["conversation_id"]
        body = {"conversation_id": conversation_id, "question": "查询昨天实际出库数量和分配数量的差异"}
        with patch("app.chat_api.enforce_comparison_contract") as guard:
            response = self.client.post("/v1/chat", headers=headers, json=body)
            replay = self.client.post("/v1/chat", headers=headers, json=body)
        guard.assert_not_called()
        self.assertEqual(200, response.status_code)
        self.assertEqual(response.json(), replay.json())
        self.assertEqual([body["question"]], self.runner.questions)

    def test_idempotency_key_replays_completed_chat_without_second_model_turn(self) -> None:
        bootstrap = self.client.get("/v1/session").json()
        conversation_id = self.client.post(
            "/v1/conversations",
            headers={"X-CSRF-Token": bootstrap["csrf_token"]},
        ).json()["conversation_id"]
        headers = {
            "X-CSRF-Token": bootstrap["csrf_token"],
            "Idempotency-Key": "req_replay_123456",
        }
        body = {"question": "查询库存", "conversation_id": conversation_id}

        first = self.client.post("/v1/chat", headers=headers, json=body)
        replay = self.client.post("/v1/chat", headers=headers, json=body)

        self.assertEqual(200, first.status_code)
        self.assertEqual(200, replay.status_code)
        self.assertEqual(first.json(), replay.json())
        self.assertEqual(1, len(self.runner.questions))

    def test_incomplete_model_turn_does_not_invent_a_business_choice(self) -> None:
        def incomplete_runner(_question: str, **_kwargs: object) -> dict[str, object]:
            return {
                "status": "completed",
                "final_response": json.dumps(
                    {"outcome": "blocked", "questions": []}, ensure_ascii=False
                ),
                "mcp_calls": [],
                "query_execution": {"status": "no_live_result"},
            }

        with patch.object(self.runner, "run", side_effect=incomplete_runner):
            bootstrap = self.client.get("/v1/session").json()
            conversation_id = self.client.post(
                "/v1/conversations",
                headers={"X-CSRF-Token": bootstrap["csrf_token"]},
            ).json()["conversation_id"]
            response = self.client.post(
                "/v1/chat",
                headers={"X-CSRF-Token": bootstrap["csrf_token"]},
                json={
                    "question": "请按仓库统计昨天已经完成的销售发货数量",
                    "conversation_id": conversation_id,
                },
            )

        self.assertEqual(502, response.status_code)
        payload = response.json()
        self.assertEqual("query_plan_invalid", payload["error"]["code"])
        self.assertNotIn("clarification", payload)
        self.assertNotIn("business_result", payload)
        self.assertNotIn("请选择", response.text)

    def test_malformed_model_clarification_does_not_replace_the_original_request(self) -> None:
        def malformed_runner(_question: str, **_kwargs: object) -> dict[str, object]:
            return {
                "status": "completed",
                "final_response": json.dumps(
                    {
                        "outcome": "clarification_required",
                        "questions": [
                            {
                                "id": "unknown_question",
                                "prompt": "请选一个",
                                "selection": "single",
                                "options": [
                                    {"id": "option_1", "label": "该选项"},
                                    {"id": "option_2", "label": "该选项"},
                                ],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                "mcp_calls": [],
                "query_execution": {"status": "no_live_result"},
            }

        with patch.object(self.runner, "run", side_effect=malformed_runner):
            bootstrap = self.client.get("/v1/session").json()
            conversation_id = self.client.post(
                "/v1/conversations",
                headers={"X-CSRF-Token": bootstrap["csrf_token"]},
            ).json()["conversation_id"]
            response = self.client.post(
                "/v1/chat",
                headers={"X-CSRF-Token": bootstrap["csrf_token"]},
                json={
                    "question": "昨天各仓库实际销售出库量",
                    "conversation_id": conversation_id,
                },
            )

        self.assertEqual(502, response.status_code)
        payload = response.json()
        self.assertEqual("query_plan_invalid", payload["error"]["code"])
        self.assertNotIn("clarification", payload)
        self.assertNotIn("该选项", response.text)

    def test_no_live_model_error_stays_an_error_without_fake_choices(self) -> None:
        def failed_runner(_question: str, **_kwargs: object) -> dict[str, object]:
            raise ChatExecutionError(
                "model did not query",
                details={"reason": "no_live_query"},
            )

        with patch.object(self.runner, "run", side_effect=failed_runner):
            bootstrap = self.client.get("/v1/session").json()
            conversation_id = self.client.post(
                "/v1/conversations",
                headers={"X-CSRF-Token": bootstrap["csrf_token"]},
            ).json()["conversation_id"]
            response = self.client.post(
                "/v1/chat",
                headers={"X-CSRF-Token": bootstrap["csrf_token"]},
                json={
                    "question": "查询库存周转",
                    "conversation_id": conversation_id,
                },
            )

        self.assertEqual(502, response.status_code)
        payload = response.json()
        self.assertEqual("chat_execution_error", payload["error"]["code"])
        self.assertNotIn("business_result", payload)
        self.assertNotIn("clarification", payload)

    def test_failed_model_turn_releases_the_conversation_for_the_next_request(self) -> None:
        calls = 0

        def flaky_runner(question: str, **_kwargs: object) -> dict[str, object]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ChatExecutionError(
                    "temporary model failure",
                    details={"reason": "unexpected_runtime_failure"},
                )
            return FakeChatRunner.run(self.runner, question)

        with patch.object(self.runner, "run", side_effect=flaky_runner):
            bootstrap = self.client.get("/v1/session").json()
            conversation_id = self.client.post(
                "/v1/conversations",
                headers={"X-CSRF-Token": bootstrap["csrf_token"]},
            ).json()["conversation_id"]
            first = self.client.post(
                "/v1/chat",
                headers={
                    "X-CSRF-Token": bootstrap["csrf_token"],
                    "Idempotency-Key": "req_failed_model_123456",
                },
                json={"question": "第一次请求", "conversation_id": conversation_id},
            )
            second = self.client.post(
                "/v1/chat",
                headers={
                    "X-CSRF-Token": bootstrap["csrf_token"],
                    "Idempotency-Key": "req_after_model_123456",
                },
                json={"question": "第二次请求", "conversation_id": conversation_id},
            )

        self.assertEqual(502, first.status_code)
        self.assertEqual("chat_execution_error", first.json()["error"]["code"])
        self.assertEqual(200, second.status_code)
        self.assertEqual("completed", second.json()["status"])
        self.assertEqual(["第二次请求"], self.runner.questions)

    def test_failed_semantic_route_releases_the_conversation_for_the_next_request(self) -> None:
        class FailOnceSemanticRuntime:
            def __init__(self) -> None:
                self.calls = 0

            def execute_plan(
                self, _request, *, question: str, context_reused: bool = False
            ) -> dict[str, object] | None:
                del context_reused
                self.calls += 1
                if self.calls == 1:
                    raise SemanticRuntimeError(
                        "temporary Doris failure", reason="query_failed"
                    )
                return None

        semantic = FailOnceSemanticRuntime()
        app = create_chat_app(
            self.runner,
            session_store=self.store,
            identity_resolver=IdentityResolver(mode="anonymous"),
            semantic_runtime=semantic,
            enforce_loopback=False,
        )
        client = TestClient(app)
        bootstrap = client.get("/v1/session").json()
        conversation_id = client.post(
            "/v1/conversations",
            headers={"X-CSRF-Token": bootstrap["csrf_token"]},
        ).json()["conversation_id"]

        first = client.post(
            "/v1/chat",
            headers={
                "X-CSRF-Token": bootstrap["csrf_token"],
                "Idempotency-Key": "req_failed_semantic_123456",
            },
            json={
                "execution_mode": "rule",
                "rule_id": "actual_sales_by_warehouse",
                "rule_params": {"window": "yesterday", "owner": "HJXH"},
                "conversation_id": conversation_id,
            },
        )
        second = client.post(
            "/v1/chat",
            headers={
                "X-CSRF-Token": bootstrap["csrf_token"],
                "Idempotency-Key": "req_after_semantic_123456",
            },
            json={"question": "第二次普通查询", "conversation_id": conversation_id},
        )

        self.assertEqual(502, first.status_code)
        self.assertEqual("query_failed", first.json()["error"]["code"])
        self.assertEqual(200, second.status_code)
        self.assertEqual("completed", second.json()["status"])
        self.assertEqual(["第二次普通查询"], self.runner.questions)

    def test_selected_clarification_stays_on_model_route(self) -> None:
        class SelectionQueryClient:
            def __init__(self) -> None:
                self.calls = 0

            def query(self, *, sql: str, purpose: str | None = None) -> dict[str, object]:
                self.calls += 1
                self.assert_purpose = purpose
                return {
                    "status": "succeeded",
                    "execution": {
                        "columns": ["warehouse_name", "outbound_ea", "allocation_ea", "reconciliation_difference", "total_ea", "warehouse_count"],
                        "rows": [
                            {
                                "warehouse_name": "仓甲",
                                "outbound_ea": "12",
                                "allocation_ea": "12",
                                "reconciliation_difference": "0",
                                "total_ea": "20",
                                "warehouse_count": 2,
                            },
                            {
                                "warehouse_name": "仓乙",
                                "outbound_ea": "8",
                                "allocation_ea": "8",
                                "reconciliation_difference": "0",
                                "total_ea": "20",
                                "warehouse_count": 2,
                            },
                        ],
                        "row_count": 2,
                        "truncated": False,
                        "executor_name": "doris",
                        "is_mock": False,
                    },
                }

        clarification = {
            "outcome": "clarification_required",
            "questions": [
                {
                    "id": "metric",
                    "prompt": "请选择要查询的业务指标。",
                    "selection": "single",
                    "options": [
                        {"id": "sales_outbound_actual", "label": "实际销售出库量"},
                    ],
                },
                {
                    "id": "time_window",
                    "prompt": "请选择销售时间窗口",
                    "selection": "single",
                    "options": [
                        {"id": "last_7_complete_days", "label": "最近7个完整自然日"},
                    ],
                },
                {
                    "id": "output_grain",
                    "prompt": "请选择结果粒度",
                    "selection": "single",
                    "options": [{"id": "total", "label": "总计"}],
                },
            ],
        }
        calls = 0

        def clarification_runner(_question: str, **_kwargs: object) -> dict[str, object]:
            nonlocal calls
            calls += 1
            if calls > 1:
                self.assertIsNone(_kwargs["thread_id"])
                self.assertEqual("帮我看看最近业务情况", _kwargs["original_question"])
                self.assertTrue(_kwargs["ui_selections"])
                result = FakeChatRunner.run(self.runner, _question)
                result["thread_id"] = "thread-selection-deterministic"
                return result
            return {
                "status": "completed",
                "final_response": json.dumps(clarification, ensure_ascii=False),
                "thread_id": "thread-selection-deterministic",
                "mcp_calls": [],
                "query_execution": {"status": "no_live_result"},
            }

        query_client = SelectionQueryClient()
        semantic = SemanticQueryRuntime(
            query_client=query_client,
            timezone_name="Asia/Shanghai",
            clock=lambda _tz=None: datetime(
                2026,
                9,
                2,
                12,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            ),
        )
        with patch.object(self.runner, "run", side_effect=clarification_runner):
            app = create_chat_app(
                self.runner,
                session_store=self.store,
                identity_resolver=IdentityResolver(mode="anonymous"),
                semantic_runtime=semantic,
                enforce_loopback=False,
            )
            client = TestClient(app)
            bootstrap = client.get("/v1/session").json()
            conversation_id = client.post(
                "/v1/conversations",
                headers={"X-CSRF-Token": bootstrap["csrf_token"]},
            ).json()["conversation_id"]
            headers = {"X-CSRF-Token": bootstrap["csrf_token"]}
            first = client.post(
                "/v1/chat",
                headers=headers,
                json={
                    "question": "帮我看看最近业务情况",
                    "conversation_id": conversation_id,
                },
            )
            self.assertEqual(200, first.status_code)
            token = first.json()["clarification"]["token"]
            completed = client.post(
                "/v1/chat",
                headers=headers,
                json={
                    "conversation_id": conversation_id,
                    "clarification_token": token,
                    "clarification_answers": [
                        {"question_id": "metric", "option_id": "sales_outbound_actual"},
                        {"question_id": "time_window", "option_id": "last_7_complete_days"},
                        {"question_id": "output_grain", "option_id": "total"},
                    ],
                },
            )

        self.assertEqual(200, completed.status_code)
        payload = completed.json()
        self.assertEqual("completed", payload["status"])
        self.assertTrue(payload["business_table"]["rows"])
        self.assertEqual(2, calls)
        self.assertEqual(0, query_client.calls)
        self.assertFalse(payload["clarification"]["required"])

    def test_generic_inventory_uses_model_instead_of_reviewed_card(self) -> None:
        class InventoryQueryClient:
            def __init__(self) -> None:
                self.calls = 0

            def query(self, *, sql: str, purpose: str | None = None) -> dict[str, object]:
                self.calls += 1
                self.assert_purpose = purpose
                return {
                    "status": "succeeded",
                    "execution": {
                        "columns": ["warehouse_name", "actual_ea", "sku_count", "negative_key_count", "latest_freshness", "total_ea", "warehouse_count", "total_sku_count", "total_negative_key_count", "negative_warehouse_count", "warehouse_name_conflict_count", "total_latest_freshness"],
                        "rows": [
                            {
                                "warehouse_name": "仓甲",
                                "actual_ea": "42",
                                "sku_count": 2,
                                "negative_key_count": 0,
                                "latest_freshness": "2026-09-03 10:00:00",
                                "total_ea": "42",
                                "warehouse_count": 1,
                                "total_sku_count": 2,
                                "total_negative_key_count": 0,
                                "negative_warehouse_count": 0,
                                "warehouse_name_conflict_count": 0,
                                "total_latest_freshness": "2026-09-03 10:00:00",
                            }
                        ],
                        "row_count": 1,
                        "truncated": False,
                        "executor_name": "doris",
                        "is_mock": False,
                    },
                }

        query_client = InventoryQueryClient()
        semantic = SemanticQueryRuntime(
            query_client=query_client,
            timezone_name="Asia/Shanghai",
            clock=lambda _tz=None: datetime(
                2026,
                9,
                3,
                12,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            ),
        )
        app = create_chat_app(
            self.runner,
            session_store=self.store,
            identity_resolver=IdentityResolver(mode="anonymous"),
            semantic_runtime=semantic,
            enforce_loopback=False,
        )
        client = TestClient(app)
        bootstrap = client.get("/v1/session").json()
        conversation_id = client.post(
            "/v1/conversations",
            headers={"X-CSRF-Token": bootstrap["csrf_token"]},
        ).json()["conversation_id"]
        headers = {"X-CSRF-Token": bootstrap["csrf_token"]}

        first = client.post(
            "/v1/chat",
            headers=headers,
            json={"question": "现在库存有多少", "conversation_id": conversation_id},
        )

        self.assertEqual(200, first.status_code)
        first_payload = first.json()
        self.assertEqual("completed", first_payload["status"])
        self.assertFalse(first_payload["clarification"]["required"])
        self.assertEqual(["现在库存有多少"], self.runner.questions)
        self.assertEqual(0, query_client.calls)

    def test_untemplated_selection_continues_in_the_same_model_context(self) -> None:
        class NoopSemanticRuntime:
            def run_if_supported(
                self, _question: str, *, context_reused: bool = False
            ) -> dict[str, object] | None:
                del context_reused
                return None

            def run_selected_if_supported(
                self,
                _source_question: str | None,
                _selections: list[dict[str, object]] | None,
                *,
                context_reused: bool = False,
            ) -> dict[str, object] | None:
                del context_reused
                return None

        calls = 0

        def clarification_runner(question: str, **_kwargs: object) -> dict[str, object]:
            nonlocal calls
            calls += 1
            if calls == 1:
                return {
                    "status": "completed",
                    "final_response": json.dumps(
                        {
                            "outcome": "clarification_required",
                            "questions": [
                                {
                                    "id": "inventory_quantity_basis",
                                    "prompt": "选择库存口径",
                                    "selection": "single",
                                    "options": [
                                        {
                                            "id": "current_realtime_finished_goods",
                                            "label": "当前实时成品库存",
                                        }
                                    ],
                                }
                            ],
                        },
                        ensure_ascii=False,
                    ),
                    "thread_id": "thread-legacy-selection",
                    "mcp_calls": [],
                    "query_execution": {"status": "no_live_result"},
                }
            result = FakeChatRunner.run(self.runner, question)
            result["thread_id"] = "thread-legacy-selection"
            return result

        with patch.object(self.runner, "run", side_effect=clarification_runner):
            app = create_chat_app(
                self.runner,
                session_store=self.store,
                identity_resolver=IdentityResolver(mode="anonymous"),
                semantic_runtime=NoopSemanticRuntime(),
                enforce_loopback=False,
            )
            client = TestClient(app)
            bootstrap = client.get("/v1/session").json()
            conversation_id = client.post(
                "/v1/conversations",
                headers={"X-CSRF-Token": bootstrap["csrf_token"]},
            ).json()["conversation_id"]
            headers = {"X-CSRF-Token": bootstrap["csrf_token"]}
            first = client.post(
                "/v1/chat",
                headers=headers,
                json={"question": "查询2026-08-03生产日期库存", "conversation_id": conversation_id},
            )
            token = first.json()["clarification"]["token"]
            continued = client.post(
                "/v1/chat",
                headers=headers,
                json={
                    "conversation_id": conversation_id,
                    "clarification_token": token,
                    "clarification_answers": [
                        {
                            "question_id": "inventory_quantity_basis",
                            "option_id": "current_realtime_finished_goods",
                        }
                    ],
                },
            )
            calls_after_continued = calls
            next_request = client.post(
                "/v1/chat",
                headers=headers,
                json={"question": "第二次普通查询", "conversation_id": conversation_id},
            )

        self.assertEqual(200, continued.status_code)
        self.assertNotEqual("blocked", continued.json()["status"])
        self.assertEqual(2, calls_after_continued)
        self.assertEqual(200, next_request.status_code, next_request.text)
        self.assertEqual("completed", next_request.json()["status"])
        # The third request is a new user question and must be re-planned;
        # only the clarification continuation shares the existing context.
        self.assertEqual(3, calls)

    def test_idempotency_key_cannot_be_reused_for_a_different_question(self) -> None:
        bootstrap = self.client.get("/v1/session").json()
        conversation_id = self.client.post(
            "/v1/conversations",
            headers={"X-CSRF-Token": bootstrap["csrf_token"]},
        ).json()["conversation_id"]
        headers = {
            "X-CSRF-Token": bootstrap["csrf_token"],
            "Idempotency-Key": "req_conflict_123456",
        }
        first = self.client.post(
            "/v1/chat",
            headers=headers,
            json={"question": "查询库存", "conversation_id": conversation_id},
        )
        conflict = self.client.post(
            "/v1/chat",
            headers=headers,
            json={"question": "查询出库", "conversation_id": conversation_id},
        )

        self.assertEqual(200, first.status_code)
        self.assertEqual(409, conflict.status_code)
        self.assertEqual("idempotency_conflict", conflict.json()["error"]["code"])
        self.assertEqual(1, len(self.runner.questions))

    def test_historical_turnover_is_forwarded_to_model_execution(self) -> None:
        bootstrap = self.client.get("/v1/session").json()
        conversation_id = self.client.post(
            "/v1/conversations",
            headers={"X-CSRF-Token": bootstrap["csrf_token"]},
        ).json()["conversation_id"]
        response = self.client.post(
            "/v1/chat",
            headers={
                "X-CSRF-Token": bootstrap["csrf_token"],
                "Idempotency-Key": "req_historical_123456",
            },
            json={
                "question": "上个月的库存周转是37天吗？",
                "conversation_id": conversation_id,
            },
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual(["上个月的库存周转是37天吗？"], self.runner.questions)

    def test_period_end_snapshot_selection_continues_model_execution(self) -> None:
        calls = 0

        def clarification_runner(_question: str, **_kwargs: object) -> dict[str, object]:
            nonlocal calls
            calls += 1
            if calls > 1:
                return {
                    "status": "completed",
                    "final_response": json.dumps(
                        {
                            "outcome": "completed",
                            "answer": "2026年8月区间末成品库存周转为37天。",
                        },
                        ensure_ascii=False,
                    ),
                    "thread_id": "thread-clarification-test",
                    "verified_business_result": {
                        "columns": ["周转天数"], "rows": [{"周转天数": "37"}],
                    },
                    "mcp_calls": [
                        {
                            "server": "doris_query_agent",
                            "tool": "query_doris_sql",
                            "status": "completed",
                        }
                    ],
                    "query_execution": {
                        "live_query_executed": True,
                        "live_business_result_executed": True,
                        "status": "live_result",
                    },
                }
            return {
                "status": "completed",
                "final_response": json.dumps(
                    {
                        "outcome": "clarification_required",
                        "questions": [
                            {
                                "id": "turnover_inventory_point",
                                "prompt": "请选择周转分子；分母固定为所选区间内实际完成销售出库成品的日均数量。",
                                "selection": "single",
                                "options": [
                                    {
                                        "id": "period_end_finished_goods_snapshot",
                                        "label": "区间末日成品库存快照",
                                    },
                                    {
                                        "id": "current_realtime_finished_goods",
                                        "label": "当前实时成品库存",
                                    },
                                ],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                "thread_id": "thread-clarification-test",
                "mcp_calls": [],
                "query_execution": {
                    "live_query_executed": False,
                    "status": "no_live_result",
                },
            }

        with patch.object(self.runner, "run", side_effect=clarification_runner):
            bootstrap = self.client.get("/v1/session").json()
            conversation_id = self.client.post(
                "/v1/conversations",
                headers={"X-CSRF-Token": bootstrap["csrf_token"]},
            ).json()["conversation_id"]
            first = self.client.post(
                "/v1/chat",
                headers={
                    "X-CSRF-Token": bootstrap["csrf_token"],
                    "Idempotency-Key": "req_clarify_historical_123456",
                },
                json={
                    "question": "查询库存周转",
                    "conversation_id": conversation_id,
                },
            )
            self.assertEqual(200, first.status_code)
            token = first.json()["clarification"]["token"]
            completed = self.client.post(
                "/v1/chat",
                headers={
                    "X-CSRF-Token": bootstrap["csrf_token"],
                    "Idempotency-Key": "req_selected_historical_123456",
                },
                json={
                    "conversation_id": conversation_id,
                    "clarification_token": token,
                    "clarification_answers": [
                        {
                            "question_id": "turnover_inventory_point",
                            "option_id": "period_end_finished_goods_snapshot",
                        }
                    ],
                },
            )

        self.assertEqual(200, completed.status_code)
        self.assertEqual(2, calls)
        self.assertEqual("37", next(iter(completed.json()["business_table"]["rows"][0].values())))

    def test_live_unapproved_contract_answer_is_not_returned(self) -> None:
        bootstrap = self.client.get("/v1/session").json()
        conversation_id = self.client.post(
            "/v1/conversations",
            headers={"X-CSRF-Token": bootstrap["csrf_token"]},
        ).json()["conversation_id"]

        def unapproved_runner(_question: str, **_kwargs: object) -> dict[str, object]:
            return {
                "status": "completed",
                "final_response": json.dumps(
                    {
                        "outcome": "completed",
                        "answer": "本月库存周转为37天，该口径当前尚待业务审批。",
                    },
                    ensure_ascii=False,
                ),
                "thread_id": "thread-unapproved-test",
                "mcp_calls": [
                    {
                        "server": "doris_query_agent",
                        "tool": "query_doris_sql",
                        "status": "completed",
                    }
                ],
                "query_execution": {
                    "live_query_executed": True,
                    "live_business_result_executed": True,
                    "status": "live_result",
                },
            }

        with patch.object(self.runner, "run", side_effect=unapproved_runner):
            response = self.client.post(
                "/v1/chat",
                headers={
                    "X-CSRF-Token": bootstrap["csrf_token"],
                    "Idempotency-Key": "req_unapproved_result_123456",
                },
                json={
                    "question": "查询库存周转",
                    "conversation_id": conversation_id,
                },
            )

        self.assertEqual(409, response.status_code)
        body = response.json()
        self.assertEqual("unapproved_contract_result", body["error"]["code"])
        self.assertNotIn("37天", response.text)

    def test_live_historical_turnover_answer_is_returned(self) -> None:
        bootstrap = self.client.get("/v1/session").json()
        conversation_id = self.client.post(
            "/v1/conversations",
            headers={"X-CSRF-Token": bootstrap["csrf_token"]},
        ).json()["conversation_id"]

        def historical_runner(_question: str, **_kwargs: object) -> dict[str, object]:
            return {
                "status": "completed",
                "final_response": json.dumps(
                    {
                        "outcome": "completed",
                        "answer": "2026年8月库存周转为37天。",
                    },
                    ensure_ascii=False,
                ),
                "thread_id": "thread-historical-result-test",
                "verified_business_result": {
                    "columns": ["周转天数"], "rows": [{"周转天数": "37"}],
                },
                "mcp_calls": [
                    {
                        "server": "doris_query_agent",
                        "tool": "query_doris_sql",
                        "status": "completed",
                    }
                ],
                "query_execution": {
                    "live_query_executed": True,
                    "live_business_result_executed": True,
                    "status": "live_result",
                },
            }

        with patch.object(self.runner, "run", side_effect=historical_runner):
            response = self.client.post(
                "/v1/chat",
                headers={
                    "X-CSRF-Token": bootstrap["csrf_token"],
                    "Idempotency-Key": "req_historical_result_123456",
                },
                json={
                    "question": "确认这个库存指标",
                    "conversation_id": conversation_id,
                },
            )

        self.assertEqual(200, response.status_code)
        self.assertEqual("37", next(iter(response.json()["business_table"]["rows"][0].values())))

    def test_foreign_conversation_id_is_not_readable(self) -> None:
        first_bootstrap = self.client.get("/v1/session?new_conversation=true").json()
        first = self.client.post(
            "/v1/conversations",
            headers={"X-CSRF-Token": first_bootstrap["csrf_token"]},
        ).json()
        second_client = TestClient(
            create_chat_app(
                self.runner,
                session_store=self.store,
                identity_resolver=IdentityResolver(mode="anonymous"),
                enforce_loopback=False,
            )
        )
        second_bootstrap = second_client.get("/v1/session?new_conversation=true").json()
        response = second_client.post(
            "/v1/chat",
            headers={"X-CSRF-Token": second_bootstrap["csrf_token"]},
            json={"question": "查询库存", "conversation_id": first["conversation_id"]},
        )
        self.assertEqual(404, response.status_code)
        self.assertEqual("conversation_not_found", response.json()["error"]["code"])

    def test_non_object_payload_is_stable_422(self) -> None:
        bootstrap = self.client.get("/v1/session").json()
        response = self.client.post(
            "/v1/chat",
            headers={
                "X-CSRF-Token": bootstrap["csrf_token"],
                "Content-Type": "application/json",
            },
            content="null",
        )
        self.assertEqual(422, response.status_code)
        self.assertEqual("invalid_parameters", response.json()["error"]["code"])

    def test_malformed_json_does_not_echo_body_or_framework_detail(self) -> None:
        bootstrap = self.client.get("/v1/session").json()
        response = self.client.post(
            "/v1/chat",
            headers={
                "X-CSRF-Token": bootstrap["csrf_token"],
                "Content-Type": "application/json",
            },
            content='{"question":"SELECT secret FROM private_table",',
        )
        self.assertEqual(422, response.status_code)
        body = response.json()
        self.assertEqual({"error"}, set(body))
        self.assertEqual(
            {"code", "message", "details"}, set(body["error"])
        )
        self.assertEqual({}, body["error"]["details"])
        self.assertNotIn("SELECT", response.text.upper())
        self.assertNotIn("private_table", response.text)

    def test_chat_validation_error_does_not_echo_field_details(self) -> None:
        bootstrap = self.client.get("/v1/session").json()
        response = self.client.post(
            "/v1/chat",
            headers={"X-CSRF-Token": bootstrap["csrf_token"]},
            json={"question": "查询库存", "sql": "SELECT secret FROM private_table"},
        )
        self.assertEqual(422, response.status_code)
        body = response.json()
        self.assertEqual("invalid_parameters", body["error"]["code"])
        self.assertEqual({}, body["error"]["details"])
        self.assertNotIn("SELECT secret", response.text)

    def test_cross_origin_state_change_is_rejected(self) -> None:
        bootstrap = self.client.get("/v1/session").json()
        response = self.client.post(
            "/v1/chat",
            headers={
                "X-CSRF-Token": bootstrap["csrf_token"],
                "Origin": "https://attacker.example",
            },
            json={"question": "查询库存", "conversation_id": bootstrap["conversation_id"]},
        )
        self.assertEqual(403, response.status_code)
        self.assertEqual("session_security_error", response.json()["error"]["code"])


    def test_unknown_session_cookie_post_does_not_create_session(self) -> None:
        connection = sqlite3.connect(self.store.path)
        try:
            before = connection.execute(
                "SELECT COUNT(*) FROM chat_sessions_v2"
            ).fetchone()[0]
        finally:
            connection.close()

        unknown_cookie = secrets.token_urlsafe(32)
        response = self.client.post(
            "/v1/chat",
            headers={"Cookie": f"{SESSION_COOKIE_NAME}={unknown_cookie}"},
            json={"question": "查询库存"},
        )

        self.assertEqual(403, response.status_code)
        self.assertEqual("session_security_error", response.json()["error"]["code"])
        connection = sqlite3.connect(self.store.path)
        try:
            after = connection.execute(
                "SELECT COUNT(*) FROM chat_sessions_v2"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(before, after)

    def test_session_bootstrap_does_not_create_conversation(self) -> None:
        def conversation_count() -> int:
            connection = sqlite3.connect(self.store.path)
            try:
                return connection.execute(
                    "SELECT COUNT(*) FROM chat_conversations_v2"
                ).fetchone()[0]
            finally:
                connection.close()

        before = conversation_count()
        fresh = self.client.get("/v1/session?new_conversation=true")
        self.assertEqual(200, fresh.status_code)
        self.assertIsNone(fresh.json()["conversation_id"])
        self.assertEqual(before, conversation_count())

        foreign = self.client.get(
            "/v1/session?conversation_id=c_not_owned_123456"
        )
        self.assertEqual(200, foreign.status_code)
        self.assertIsNone(foreign.json()["conversation_id"])
        self.assertEqual(before, conversation_count())

    def test_cross_site_session_bootstrap_is_rejected(self) -> None:
        response = self.client.get(
            "/v1/session?new_conversation=true",
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        self.assertEqual(403, response.status_code)
        self.assertEqual("session_security_error", response.json()["error"]["code"])


class ChatRunnerEvidenceTestCase(unittest.TestCase):
    def test_child_failure_signature_is_non_sensitive(self) -> None:
        signature = _process_failure_signature(
            "Traceback: unauthorized request; connection reset while calling model"
        )
        self.assertEqual("auth,network,exception", signature)
        self.assertEqual("stderr_empty", _process_failure_signature(""))

    def test_worker_prefers_windowless_python_when_available(self) -> None:
        with TemporaryDirectory(prefix="windowless-python-") as directory:
            root = Path(directory)
            console_python = root / "python.exe"
            windowless_python = root / "pythonw.exe"
            console_python.write_bytes(b"")
            windowless_python.write_bytes(b"")
            expected = str(windowless_python) if os.name == "nt" else str(console_python)
            self.assertEqual(expected, _windowless_python(str(console_python)))

    def test_worker_process_options_hide_windows_on_windows(self) -> None:
        options = _hidden_process_options()
        if os.name == "nt":
            self.assertTrue(options["creationflags"] & subprocess.CREATE_NO_WINDOW)
            self.assertEqual(subprocess.SW_HIDE, options["startupinfo"].wShowWindow)
        else:
            self.assertEqual({"creationflags": 0}, options)

    def test_child_command_has_no_internal_result_switch(self) -> None:
        runner = ChatRunner(project_root=Path(__file__).resolve().parents[2])
        command = runner._command("查询库存")
        self.assertNotIn("--internal-json", command)

    def test_cancel_event_terminates_isolated_model_process(self) -> None:
        class BlockingProcess:
            pid = 12345
            returncode = None

            def __init__(self) -> None:
                self.cancelled = False

            def poll(self):
                return 0 if self.cancelled else None

            def communicate(self, timeout=None):
                if self.cancelled:
                    return "", ""
                cancel_event.set()
                raise subprocess.TimeoutExpired(["python"], timeout or 1)

        cancel_event = threading.Event()
        process = BlockingProcess()
        with TemporaryDirectory(prefix="chat-runner-cancel-") as directory:
            home = Path(directory)
            (home / "auth.json").write_text("{}", encoding="utf-8")
            runner = ChatRunner(
                project_root=Path(__file__).resolve().parents[2],
                timeout_seconds=10,
            )
            runner.codex_home = home
            with patch("app.chat_api.subprocess.Popen", return_value=process), \
                 patch("app.chat_api._terminate_process_tree", side_effect=lambda item: setattr(item, "cancelled", True)):
                with self.assertRaises(ChatExecutionError) as raised:
                    runner.run("查询库存", cancel_event=cancel_event)
        self.assertEqual("query_cancelled", raised.exception.details["reason"])
        self.assertTrue(process.cancelled)

    def test_child_command_carries_original_question_for_selection_continuation(self) -> None:
        runner = ChatRunner(project_root=Path(__file__).resolve().parents[2])
        command = runner._command(
            "根据已选择的业务口径继续本次取数。",
            ui_selections=[
                {"question_id": "metric", "option_ids": ["sales_outbound_actual"]}
            ],
            original_question="昨天各仓库实际销售出库量",
        )
        self.assertIn("--original-question", command)
        index = command.index("--original-question")
        self.assertEqual("昨天各仓库实际销售出库量", command[index + 1])

    def test_full_stdout_is_not_accepted_as_private_result(self) -> None:
        with TemporaryDirectory(prefix="chat-runner-stdout-tests-") as directory:
            home = Path(directory)
            (home / "auth.json").write_text("{}", encoding="utf-8")
            runner = ChatRunner(
                project_root=Path(__file__).resolve().parents[2],
                timeout_seconds=10,
            )
            runner.codex_home = home
            completed = subprocess.CompletedProcess(
                args=["python"],
                returncode=0,
                stdout=json.dumps(
                    {
                        "status": "completed",
                        "final_response": "SELECT secret FROM private_table",
                    }
                ),
                stderr="",
            )
            with patch("app.chat_api.subprocess.run", return_value=completed):
                with self.assertRaises(ChatExecutionError) as raised:
                    runner.run("查询库存")
            self.assertEqual(
                "internal_result_unavailable", raised.exception.details["reason"]
            )

    def _run_child_payload(self, payload: dict[str, object]) -> dict[str, object]:
        with TemporaryDirectory(prefix="chat-runner-tests-") as directory:
            home = Path(directory)
            (home / "auth.json").write_text("{}", encoding="utf-8")
            runner = ChatRunner(
                project_root=Path(__file__).resolve().parents[2],
                timeout_seconds=10,
            )
            runner.codex_home = home
            completed = subprocess.CompletedProcess(
                args=["python"],
                returncode=0,
                # The child stdout is process-only. Write the mocked full
                # result to the same nonce-bound private channel used in
                # production.
                stdout=json.dumps(
                    {
                        "status": "completed",
                        "process": [{"id": "query", "state": "completed"}],
                    },
                    ensure_ascii=False,
                ),
                stderr="",
            )
            def run_child(*_args: object, **kwargs: object) -> subprocess.CompletedProcess:
                environment = kwargs.get("env")
                assert isinstance(environment, dict)
                path = Path(str(environment["AGENT_CHAT_INTERNAL_RESULT_PATH"]))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(
                        {
                            "nonce": environment["AGENT_CHAT_INTERNAL_RESULT_NONCE"],
                            "result": payload,
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                return completed

            with patch("app.chat_api.subprocess.run", side_effect=run_child):
                return runner.run("查询库存")

    def test_numeric_answer_without_business_query_is_rejected(self) -> None:
        with self.assertRaises(ChatExecutionError) as raised:
            self._run_child_payload(
                {
                    "status": "completed",
                    "final_response": "库存是 100 箱",
                    "mcp_calls": [],
                }
            )
        self.assertEqual("no_live_query", raised.exception.details["reason"])

    def test_numeric_answer_with_question_mark_is_still_rejected(self) -> None:
        with self.assertRaises(ChatExecutionError) as raised:
            self._run_child_payload(
                {
                    "status": "completed",
                    "final_response": "库存是 100？",
                    "mcp_calls": [],
                }
            )
        self.assertEqual("no_live_query", raised.exception.details["reason"])

    def test_live_business_answer_is_screened_and_preserved(self) -> None:
        result = self._run_child_payload(
            {
                "status": "completed",
                "final_response": json.dumps(
                    {
                        "outcome": "completed",
                        "answer": "截至 2026-09-01 00:00，当前可用库存为 128 箱。",
                    },
                    ensure_ascii=False,
                ),
                "mcp_calls": [
                    {
                        "server": "doris_query_agent",
                        "tool": "query_doris_sql",
                        "status": "completed",
                    }
                ],
            }
        )
        self.assertEqual("completed", result["interaction"]["outcome"])
        self.assertIn("128 箱", result["interaction"]["answer"])

    def test_live_business_answer_with_query_details_is_rejected(self) -> None:
        with self.assertRaises(ChatExecutionError) as raised:
            self._run_child_payload(
                {
                    "status": "completed",
                    "final_response": json.dumps(
                        {
                            "outcome": "completed",
                            "answer": "SELECT value FROM private_table",
                        }
                    ),
                    "mcp_calls": [
                        {
                            "server": "doris_query_agent",
                            "tool": "query_doris_sql",
                            "status": "completed",
                        }
                    ],
                }
            )
        self.assertEqual(
            "business_answer_unavailable",
            raised.exception.details["reason"],
        )

    def test_live_probe_with_valid_late_clarification_is_not_reported_as_model_failure(self) -> None:
        result = self._run_child_payload(
            {
                "status": "completed",
                "final_response": json.dumps(
                    {
                        "outcome": "clarification_required",
                        "questions": [
                            {
                                "id": "metric",
                                "prompt": "请选择本次业务指标。",
                                "selection": "single",
                                "options": [
                                    {"id": "actual_outbound", "label": "实际销售出库量"},
                                    {"id": "available_inventory", "label": "当前可用库存"},
                                ],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                "mcp_calls": [
                    {
                        "server": "doris_query_agent",
                        "tool": "query_doris_sql",
                        "status": "completed",
                    }
                ],
                "query_execution": {
                    "live_query_executed": True,
                    "live_business_result_executed": True,
                    "status": "live_result",
                },
            }
        )
        self.assertEqual("clarification_required", result["interaction"]["outcome"])
        self.assertEqual("metric", result["interaction"]["questions"][0]["id"])

    def test_clarification_without_query_is_allowed(self) -> None:
        result = self._run_child_payload(
            {
                "status": "completed",
                "final_response": "请确认你要查询实时库存还是历史快照？",
                "mcp_calls": [],
            }
        )
        self.assertEqual("no_live_result", result["query_execution"]["status"])

    def test_unexpected_tool_evidence_is_rejected(self) -> None:
        with self.assertRaises(ChatExecutionError) as raised:
            self._run_child_payload(
                {
                    "status": "completed",
                    "final_response": "请确认口径",
                    "mcp_calls": [
                        {
                            "server": "other_tool",
                            "tool": "read_file",
                            "status": "completed",
                        }
                    ],
                }
            )
        self.assertEqual("unexpected_tool_call", raised.exception.details["reason"])

    def test_reported_metadata_only_evidence_is_preserved(self) -> None:
        result = self._run_child_payload(
            {
                "status": "completed",
                "final_response": "请确认需要的业务口径。",
                "mcp_calls": [
                    {
                        "server": "doris_query_agent",
                        "tool": "query_doris_sql",
                        "status": "completed",
                    }
                ],
                "query_execution": {
                    "live_query_executed": True,
                    "live_business_result_executed": False,
                    "query_call_count": 1,
                    "successful_query_call_count": 1,
                    "metadata_query_call_count": 1,
                    "successful_business_query_call_count": 0,
                    "failed_query_call_count": 0,
                    "unexpected_tool_call_count": 0,
                    "status": "metadata_only",
                },
            }
        )
        self.assertEqual("metadata_only", result["query_execution"]["status"])
        self.assertFalse(result["query_execution"]["live_business_result_executed"])

    def test_inconsistent_report_is_rejected(self) -> None:
        with self.assertRaises(ChatExecutionError) as raised:
            self._run_child_payload(
                {
                    "status": "completed",
                    "final_response": "查询完成",
                    "mcp_calls": [
                        {
                            "server": "doris_query_agent",
                            "tool": "query_doris_sql",
                            "status": "completed",
                        }
                    ],
                    "query_execution": {
                        "query_call_count": 99,
                        "successful_query_call_count": 99,
                        "metadata_query_call_count": 99,
                        "successful_business_query_call_count": 0,
                        "failed_query_call_count": 0,
                        "unexpected_tool_call_count": 0,
                        "status": "metadata_only",
                    },
                }
            )
        self.assertEqual("invalid_query_evidence", raised.exception.details["reason"])


class InstallationPrincipalTestCase(unittest.TestCase):
    def test_installation_identity_is_persistent_and_ignores_request_headers(self) -> None:
        with TemporaryDirectory(prefix="installation-identity-") as directory:
            identity_path = Path(directory) / "installation-identity.json"
            first_resolver = IdentityResolver(
                mode="installation", installation_id_path=identity_path
            )
            first = first_resolver.resolve({})
            forged_header = first_resolver.resolve({"x-wecom-user-id": "forged"})
            restarted = IdentityResolver(
                mode="installation", installation_id_path=identity_path
            ).resolve({})

            self.assertIsNotNone(first)
            self.assertEqual("installation", first.kind)
            self.assertEqual(first, forged_header)
            self.assertEqual(first, restarted)
            self.assertTrue(identity_path.is_file())

    def test_one_installation_owns_one_conversation_across_cookies_and_restart(self) -> None:
        with TemporaryDirectory(prefix="installation-chat-") as directory:
            root = Path(directory)
            identity_path = root / "installation-identity.json"
            store = ChatSessionStore(root / "sessions.sqlite3")
            runner = FakeChatRunner()
            resolver = IdentityResolver(
                mode="installation", installation_id_path=identity_path
            )
            app = create_chat_app(
                runner,
                session_store=store,
                identity_resolver=resolver,
                enforce_loopback=False,
            )

            first_client = TestClient(app)
            first_bootstrap = first_client.get("/v1/session").json()
            first_conversation = first_client.post(
                "/v1/conversations",
                headers={"X-CSRF-Token": first_bootstrap["csrf_token"]},
            ).json()["conversation_id"]

            cookie_free_client = TestClient(app)
            cookie_free_bootstrap = cookie_free_client.get("/v1/session").json()
            self.assertEqual(first_conversation, cookie_free_bootstrap["conversation_id"])

            restarted_app = create_chat_app(
                runner,
                session_store=store,
                identity_resolver=IdentityResolver(
                    mode="installation", installation_id_path=identity_path
                ),
                enforce_loopback=False,
            )
            restarted_bootstrap = TestClient(restarted_app).get("/v1/session").json()
            self.assertEqual(first_conversation, restarted_bootstrap["conversation_id"])
            self.assertEqual("installation", restarted_bootstrap["identity_mode"])

            raw_installation_id = json.loads(
                identity_path.read_text(encoding="ascii")
            )["installation_id"]
            self.assertNotIn(raw_installation_id, json.dumps(restarted_bootstrap))

    def test_different_installation_cannot_reuse_conversation(self) -> None:
        with TemporaryDirectory(prefix="installation-isolation-") as directory:
            root = Path(directory)
            store = ChatSessionStore(root / "sessions.sqlite3")
            runner = FakeChatRunner()

            first_app = create_chat_app(
                runner,
                session_store=store,
                identity_resolver=IdentityResolver(
                    mode="installation", installation_id_path=root / "machine-a.json"
                ),
                enforce_loopback=False,
            )
            first_client = TestClient(first_app)
            first_bootstrap = first_client.get("/v1/session").json()
            first_conversation = first_client.post(
                "/v1/conversations",
                headers={"X-CSRF-Token": first_bootstrap["csrf_token"]},
            ).json()["conversation_id"]

            second_app = create_chat_app(
                runner,
                session_store=store,
                identity_resolver=IdentityResolver(
                    mode="installation", installation_id_path=root / "machine-b.json"
                ),
                enforce_loopback=False,
            )
            second_client = TestClient(second_app)
            second_bootstrap = second_client.get("/v1/session").json()
            foreign = second_client.post(
                "/v1/chat",
                headers={"X-CSRF-Token": second_bootstrap["csrf_token"]},
                json={"question": "查询库存", "conversation_id": first_conversation},
            )
            self.assertEqual(404, foreign.status_code)
            self.assertEqual("conversation_not_found", foreign.json()["error"]["code"])

    def test_invalid_identity_file_fails_closed(self) -> None:
        with TemporaryDirectory(prefix="installation-invalid-") as directory:
            identity_path = Path(directory) / "installation-identity.json"
            identity_path.write_text('{"version":1,"installation_id":"bad"}\n', encoding="ascii")
            resolver = IdentityResolver(
                mode="installation", installation_id_path=identity_path
            )
            with self.assertRaisesRegex(RuntimeError, "invalid identifier"):
                resolver.resolve({})


class WeComPrincipalIsolationTestCase(unittest.TestCase):
    def test_verified_wecom_id_owns_conversation_across_sessions(self) -> None:
        secret = "s" * 32
        with TemporaryDirectory(prefix="wecom-chat-tests-") as directory:
            store = ChatSessionStore(
                Path(directory) / "sessions.sqlite3", principal_pepper=secret
            )
            runner = FakeChatRunner()
            app = create_chat_app(
                runner,
                session_store=store,
                identity_resolver=IdentityResolver(
                    mode="wecom_signed", secret=secret
                ),
                enforce_loopback=False,
            )
            alice_one = TestClient(app)
            alice_two = TestClient(app)
            bob = TestClient(app)
            alice_headers = signed_headers("alice", secret)
            bob_headers = signed_headers("bob", secret)
            first = alice_one.get(
                "/v1/session?new_conversation=true", headers=alice_headers
            ).json()
            first_conversation = alice_one.post(
                "/v1/conversations",
                headers={**alice_headers, "X-CSRF-Token": first["csrf_token"]},
            ).json()
            repeated_conversation = alice_one.post(
                "/v1/conversations",
                headers={**alice_headers, "X-CSRF-Token": first["csrf_token"]},
            ).json()
            self.assertEqual(
                first_conversation["conversation_id"],
                repeated_conversation["conversation_id"],
            )
            second = alice_two.get(
                "/v1/session?new_conversation=true", headers=alice_headers
            ).json()
            self.assertEqual(
                first_conversation["conversation_id"], second["conversation_id"]
            )
            own = alice_two.post(
                "/v1/chat",
                headers={**alice_headers, "X-CSRF-Token": second["csrf_token"]},
                json={
                    "question": "查询库存",
                    "conversation_id": first_conversation["conversation_id"],
                },
            )
            self.assertEqual(200, own.status_code)
            foreign_bootstrap = bob.get(
                "/v1/session?new_conversation=true", headers=bob_headers
            ).json()
            foreign = bob.post(
                "/v1/chat",
                headers={**bob_headers, "X-CSRF-Token": foreign_bootstrap["csrf_token"]},
                json={
                    "question": "查询库存",
                    "conversation_id": first_conversation["conversation_id"],
                },
            )
            self.assertEqual(404, foreign.status_code)


if __name__ == "__main__":
    unittest.main()

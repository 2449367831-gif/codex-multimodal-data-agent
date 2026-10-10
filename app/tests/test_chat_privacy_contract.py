from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from app.chat_api import IdentityResolver, _public_business_table, create_chat_app
from app.codex_agent import interaction_envelope
from app.chat_sessions import (
    ChatSessionStore,
    ClarificationValidationError,
    ConversationNotFoundError,
    ContextOwnershipError,
    Principal,
)


def _session(store: ChatSessionStore, subject: str):
    """Create a server-side principal session without using request JSON data."""

    principal = Principal(kind="wecom", subject=subject)
    return store.ensure_session(None, None, principal)


class ChatContextPrivacyContractTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory(prefix="chat-privacy-contract-")
        self.path = Path(self.directory.name) / "sessions.sqlite3"
        self.store = ChatSessionStore(self.path, principal_pepper="p" * 32)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_public_context_state_contains_lifecycle_only(self) -> None:
        session = _session(self.store, "alice")
        context = self.store.bind_context_thread(session, "thread-alice-001")
        public = self.store.public_context_state(context)
        serialized = json.dumps(public, ensure_ascii=False)

        self.assertEqual("principal", public["scope"])
        self.assertTrue(public["has_context"])
        self.assertNotIn("thread-alice-001", serialized)
        self.assertNotIn("codex_thread_id", public)
        self.assertNotIn("principal_hash", public)
        self.assertNotIn("question", serialized)
        self.assertNotIn("sql", serialized.casefold())
        self.assertNotIn("rows", serialized.casefold())

    def test_context_registry_schema_has_no_transcript_or_result_columns(self) -> None:
        _session(self.store, "alice")
        connection = sqlite3.connect(self.path)
        try:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(chat_principal_contexts_v1)"
                ).fetchall()
            }
            forbidden = {
                "question",
                "prompt",
                "answer",
                "sql",
                "sql_text",
                "bindings",
                "rows",
                "columns",
                "transcript",
                "messages",
            }
            self.assertFalse(columns & forbidden)
        finally:
            connection.close()

    def test_same_principal_sessions_share_context_but_other_principal_cannot(self) -> None:
        alice_one = _session(self.store, "alice")
        alice_two = _session(self.store, "alice")
        bob = _session(self.store, "bob")

        first = self.store.bind_context_thread(alice_one, "thread-alice-001")
        second = self.store.context(alice_two)
        self.assertEqual(first.codex_thread_id, second.codex_thread_id)
        self.assertEqual("thread-alice-001", second.codex_thread_id)

        with self.assertRaises(ContextOwnershipError):
            self.store.record_context_turn(bob, "thread-alice-001")
        with self.assertRaises(ContextOwnershipError):
            self.store.record_context_compaction(bob, "thread-alice-001")

    def test_context_turn_and_compaction_increment_only_counters(self) -> None:
        session = _session(self.store, "alice")
        self.store.bind_context_thread(session, "thread-alice-001")
        after_turn = self.store.record_context_turn(session, "thread-alice-001")
        after_compact = self.store.record_context_compaction(
            session, "thread-alice-001", status="ready"
        )
        public = self.store.public_context_state(after_compact)
        self.assertEqual(1, after_turn.turn_count)
        self.assertEqual(1, after_compact.compaction_count)
        self.assertEqual(1, public["turn_count"])
        self.assertEqual(1, public["compaction_count"])
        self.assertNotIn("last_compaction_at", public)


class ClarificationAndFeedbackPrivacyContractTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory(prefix="chat-privacy-contract-")
        self.store = ChatSessionStore(
            Path(self.directory.name) / "sessions.sqlite3", principal_pepper="p" * 32
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_clarification_token_is_principal_bound_and_requires_all_answers(self) -> None:
        alice = _session(self.store, "alice")
        alice_other_browser = _session(self.store, "alice")
        bob = _session(self.store, "bob")
        conversation_id = self.store.create_conversation(alice)
        token = self.store.create_clarification_token(
            alice,
            conversation_id,
            [
                {
                    "id": "inventory_basis",
                    "selection": "single",
                    "options": [
                        {"id": "available", "label": "可用库存"},
                        {"id": "gross", "label": "毛库存"},
                    ],
                },
                {
                    "id": "snapshot",
                    "selection": "single",
                    "options": [
                        {"id": "realtime", "label": "实时"},
                        {"id": "date", "label": "指定日期"},
                    ],
                },
            ],
        )

        with self.assertRaises(ClarificationValidationError):
            self.store.validate_clarification_token(
                alice_other_browser,
                conversation_id,
                token,
                [{"question_id": "inventory_basis", "option_id": "available"}],
            )
        with self.assertRaises(ConversationNotFoundError):
            self.store.validate_clarification_token(
                bob,
                conversation_id,
                token,
                [
                    {"question_id": "inventory_basis", "option_id": "available"},
                    {"question_id": "snapshot", "option_id": "realtime"},
                ],
            )

        answers = self.store.validate_clarification_token(
            alice,
            conversation_id,
            token,
            [
                {"question_id": "inventory_basis", "option_id": "available"},
                {"question_id": "snapshot", "option_id": "realtime"},
            ],
        )
        self.assertEqual(
            [
                {"question_id": "inventory_basis", "option_ids": ["available"]},
                {"question_id": "snapshot", "option_ids": ["realtime"]},
            ],
            answers,
        )
        with self.assertRaises(ClarificationValidationError):
            self.store.validate_clarification_token(
                alice,
                conversation_id,
                token,
                [
                    {"question_id": "inventory_basis", "option_id": "available"},
                    {"question_id": "snapshot", "option_id": "realtime"},
                ],
            )

    def test_interaction_envelope_replaces_unsafe_choice_copy(self) -> None:
        envelope = interaction_envelope(
            json.dumps(
                {
                    "outcome": "clarification_required",
                    "questions": [
                        {
                            "id": "q1",
                            "prompt": "SELECT * FROM secret_table; ROW-SENTINEL",
                            "options": [
                                {
                                    "id": "o1",
                                    "label": "DROP TABLE secret_table; 123456",
                                    "description": "columns=secret rows=[PRIVATE-ROW]",
                                }
                            ],
                        }
                    ],
                },
                ensure_ascii=False,
            )
        )
        serialized = json.dumps(envelope, ensure_ascii=False)
        self.assertNotIn("SELECT * FROM", serialized.upper())
        self.assertNotIn("DROP TABLE", serialized.upper())
        self.assertNotIn("secret_table", serialized)
        self.assertNotIn("ROW-SENTINEL", serialized)
        self.assertNotIn("PRIVATE-ROW", serialized)

    def test_plain_language_clarification_gets_safe_selectable_recovery(self) -> None:
        envelope = interaction_envelope("请确认本次业务口径后继续")
        self.assertEqual("clarification_required", envelope["outcome"])
        self.assertEqual("q_specification", envelope["questions"][0]["id"])
        self.assertEqual(
            "provide_business_definition",
            envelope["questions"][0]["options"][0]["id"],
        )

    def test_completed_business_answer_keeps_values_but_rejects_query_details(self) -> None:
        safe = interaction_envelope(
            json.dumps(
                {
                    "outcome": "completed",
                    "answer": "截至 2026-09-01 00:00，当前可用库存为 128 箱。\n范围：全部货主；粒度：总计。",
                },
                ensure_ascii=False,
            )
        )
        self.assertEqual("completed", safe["outcome"])
        self.assertIn("128 箱", safe["answer"])

        unsafe = interaction_envelope(
            json.dumps(
                {
                    "outcome": "completed",
                    "answer": "SELECT value FROM private_table",
                },
                ensure_ascii=False,
            )
        )
        self.assertEqual("blocked", unsafe["outcome"])
        self.assertTrue(unsafe["needs_review"])
        self.assertNotIn("answer", unsafe)

    def test_completed_business_answer_accepts_gateway_alias(self) -> None:
        envelope = interaction_envelope(
            json.dumps(
                {
                    "status": "completed",
                    "final_answer": "查询完成，合计 12 箱。",
                },
                ensure_ascii=False,
            )
        )
        self.assertEqual("completed", envelope["outcome"])
        self.assertEqual("查询完成，合计 12 箱。", envelope["answer"])

    def test_reviewed_semantic_choice_ids_keep_business_copy(self) -> None:
        """Known gateway IDs must not collapse into the generic placeholder."""

        payload = {
            "outcome": "clarification_required",
            "questions": [
                {
                    "id": "turnover_inventory_point",
                    # The prompt/labels are treated as untrusted; reviewed
                    # semantic IDs provide the stable browser copy.
                    "prompt": "internal prompt with source details",
                    "selection": "single",
                    "options": [
                        {
                            "id": "period_end_finished_goods_snapshot",
                            "label": "internal row detail 123",
                        },
                        {
                            "id": "current_realtime_finished_goods",
                            "label": "internal row detail 456",
                        },
                    ],
                }
            ],
        }
        envelope = interaction_envelope(json.dumps(payload, ensure_ascii=False))
        self.assertEqual("clarification_required", envelope["outcome"])
        question = envelope["questions"][0]
        self.assertEqual(
            "请选择周转分子；分母固定为所选区间内实际完成销售出库成品的日均数量。",
            question["prompt"],
        )
        self.assertEqual(
            ["period_end_finished_goods_snapshot", "current_realtime_finished_goods"],
            [option["id"] for option in question["options"]],
        )
        self.assertEqual(
            [
                "区间末日成品库存快照",
                "当前实时成品库存",
            ],
            [option["label"] for option in question["options"]],
        )

    def test_reviewed_metric_and_inventory_choice_ids_keep_business_copy(self) -> None:
        payloads = (
            {
                "id": "metric",
                "prompt": "unreviewed prompt",
                "options": [
                    {"id": "sales_outbound_actual", "label": "unreviewed"},
                    {"id": "inventory_available_realtime", "label": "unreviewed"},
                    {"id": "inventory_snapshot_quantity", "label": "unreviewed"},
                    {"id": "inventory_turnover_finished_goods_snapshot", "label": "unreviewed"},
                    {"id": "inventory_turnover_finished_goods_realtime", "label": "unreviewed"},
                ],
            },
            {
                "id": "inventory_quantity_basis",
                "prompt": "unreviewed prompt",
                "options": [
                    {"id": "current_available_realtime", "label": "unreviewed"},
                    {"id": "today_historical_snapshot", "label": "unreviewed"},
                ],
            },
            {
                "id": "output_grain",
                "prompt": "unreviewed prompt",
                "options": [
                    {"id": "total", "label": "unreviewed"},
                    {"id": "warehouse", "label": "unreviewed"},
                    {"id": "sku", "label": "unreviewed"},
                    {"id": "warehouse_sku", "label": "unreviewed"},
                ],
            },
            {
                "id": "scope",
                "prompt": "unreviewed prompt",
                "selection": "multiple",
                "options": [
                    {"id": "all_owner", "label": "unreviewed"},
                    {"id": "all_warehouse", "label": "unreviewed"},
                    {"id": "all_sku", "label": "unreviewed"},
                ],
            },
        )
        envelope = interaction_envelope(
            json.dumps(
                {"outcome": "clarification_required", "questions": list(payloads)},
                ensure_ascii=False,
            )
        )
        self.assertEqual("clarification_required", envelope["outcome"])
        self.assertEqual(
            ["metric", "inventory_quantity_basis", "output_grain", "scope"],
            [question["id"] for question in envelope["questions"]],
        )
        self.assertEqual(
            ["unreviewed"] * 5,
            [option["label"] for option in envelope["questions"][0]["options"]],
        )
        self.assertEqual(
            ["unreviewed"] * 2,
            [option["label"] for option in envelope["questions"][1]["options"]],
        )
        self.assertEqual(
            ["unreviewed"] * 4,
            [option["label"] for option in envelope["questions"][2]["options"]],
        )
        self.assertEqual(
            ["unreviewed"] * 3,
            [option["label"] for option in envelope["questions"][3]["options"]],
        )
        # Safe model-authored business copy is preserved; SQL and source-detail
        # text are still screened at the same boundary.
        self.assertNotIn("2026-08-29", json.dumps(envelope, ensure_ascii=False))

    def test_reviewed_inventory_variant_ids_keep_business_copy(self) -> None:
        envelope = interaction_envelope(
            json.dumps(
                {
                    "outcome": "clarification_required",
                    "questions": [
                        {
                            "id": "inventory_basis",
                            "prompt": "unreviewed prompt",
                            "options": [
                                {"id": "production_date", "label": "unreviewed"},
                                {"id": "in_transit", "label": "unreviewed"},
                                {"id": "pickup_availability", "label": "unreviewed"},
                            ],
                        }
                    ],
                },
                ensure_ascii=False,
            )
        )
        self.assertEqual("clarification_required", envelope["outcome"])
        self.assertEqual(
            ["unreviewed"] * 3,
            [option["label"] for option in envelope["questions"][0]["options"]],
        )

    def test_reviewed_snapshot_date_choice_keeps_business_copy(self) -> None:
        envelope = interaction_envelope(
            json.dumps(
                {
                    "outcome": "clarification_required",
                    "questions": [
                        {
                            "id": "snapshot_date",
                            "prompt": "unreviewed prompt",
                            "options": [{"id": "provide_date", "label": "unreviewed"}],
                        }
                    ],
                },
                ensure_ascii=False,
            )
        )
        self.assertEqual("unreviewed prompt", envelope["questions"][0]["prompt"])
        self.assertEqual("unreviewed", envelope["questions"][0]["options"][0]["label"])

    def test_reviewed_turnover_and_sales_window_ids_keep_business_copy(self) -> None:
        envelope = interaction_envelope(
            json.dumps(
                {
                    "outcome": "clarification_required",
                    "questions": [
                        {
                            "id": "turnover_basis",
                            "prompt": "unreviewed prompt",
                            "options": [
                                {"id": "historical_snapshot_actual_outbound", "label": "unreviewed"},
                                {"id": "current_available_actual_outbound", "label": "unreviewed"},
                            ],
                        },
                        {
                            "id": "sales_window",
                            "prompt": "unreviewed prompt",
                            "options": [
                                {"id": "mtd_complete_days", "label": "unreviewed"},
                                {"id": "custom_period", "label": "unreviewed"},
                                {"id": "last_30_complete_days", "label": "unreviewed"},
                            ],
                        },
                    ],
                },
                ensure_ascii=False,
            )
        )
        self.assertEqual("unreviewed prompt", envelope["questions"][1]["prompt"])
        self.assertEqual("unreviewed prompt", envelope["questions"][0]["prompt"])
        self.assertEqual("turnover_inventory_point", envelope["questions"][0]["id"])
        self.assertEqual(
            ["unreviewed", "unreviewed"],
            [option["label"] for option in envelope["questions"][0]["options"]],
        )
        self.assertEqual(
            ["unreviewed", "unreviewed", "unreviewed"],
            [option["label"] for option in envelope["questions"][1]["options"]],
        )
        serialized = json.dumps(envelope, ensure_ascii=False)
        self.assertNotIn("该选项", serialized)
        self.assertIn("unreviewed", serialized)

    def test_reviewed_outbound_and_time_window_alias_ids_keep_business_copy(self) -> None:
        """Gateway aliases must remain semantic before the positional fallback."""

        envelope = interaction_envelope(
            json.dumps(
                {
                    "outcome": "clarification_required",
                    "questions": [
                        {
                            "id": "outbound_basis",
                            "prompt": "unreviewed prompt",
                            "options": [
                                {"id": "actual_completed_outbound", "label": "unreviewed"},
                                {"id": "order_requested_quantity", "label": "unreviewed"},
                            ],
                        },
                        {
                            "id": "time_window",
                            "prompt": "unreviewed prompt",
                            "options": [
                                {"id": "mtd_to_today_00", "label": "unreviewed"},
                                {"id": "explicit_period", "label": "unreviewed"},
                            ],
                        },
                    ],
                },
                ensure_ascii=False,
            )
        )
        self.assertEqual(
            ["outbound_basis", "time_window"],
            [question["id"] for question in envelope["questions"]],
        )
        self.assertEqual("unreviewed prompt", envelope["questions"][0]["prompt"])
        self.assertEqual(
            ["actual_completed_outbound", "order_requested_quantity"],
            [option["id"] for option in envelope["questions"][0]["options"]],
        )
        self.assertEqual(
            ["unreviewed", "unreviewed"],
            [option["label"] for option in envelope["questions"][0]["options"]],
        )
        self.assertEqual(
            ["mtd_to_today_00", "explicit_period"],
            [option["id"] for option in envelope["questions"][1]["options"]],
        )
        self.assertEqual(
            ["unreviewed", "unreviewed"],
            [option["label"] for option in envelope["questions"][1]["options"]],
        )
        self.assertNotIn("该选项", json.dumps(envelope, ensure_ascii=False))

    def test_missing_choice_copy_is_blocked(self) -> None:
        envelope = interaction_envelope(
            json.dumps(
                {
                    "outcome": "clarification_required",
                    "questions": [
                        {
                            "id": "basis",
                            "prompt": "选择库存口径",
                            "options": [{"id": "available"}],
                        }
                    ],
                },
                ensure_ascii=False,
            )
        )
        self.assertEqual("blocked", envelope["outcome"])
        self.assertTrue(envelope["needs_review"])

    def test_safe_model_option_copy_is_preserved_for_unknown_question(self) -> None:
        envelope = interaction_envelope(
            json.dumps(
                {
                    "outcome": "clarification_required",
                    "questions": [
                        {
                            "id": "future_business_question",
                            "prompt": "请确认本次业务口径。",
                            "options": [
                                {"id": "future_option", "label": "provider generated copy"}
                            ],
                        }
                    ],
                },
                ensure_ascii=False,
            )
        )
        self.assertEqual("clarification_required", envelope["outcome"])
        self.assertEqual("future_business_question", envelope["questions"][0]["id"])
        self.assertEqual("future_option", envelope["questions"][0]["options"][0]["id"])
        self.assertIn("provider generated copy", json.dumps(envelope, ensure_ascii=False))

    def test_unknown_explicit_status_is_blocked(self) -> None:
        envelope = interaction_envelope(
            json.dumps({"status": "provider_partial", "questions": []})
        )
        self.assertEqual("blocked", envelope["outcome"])
        self.assertTrue(envelope["needs_review"])

    def test_interaction_envelope_preserves_valid_model_ids(self) -> None:
        envelope = interaction_envelope(
            json.dumps(
                {
                    "outcome": "clarification_required",
                    "questions": [
                        {
                            "id": "warehouse_WH_001",
                            "prompt": "请确认本次业务口径。",
                            "options": [
                                {"id": "SKU_ABC_2026", "label": "可用库存"},
                                {"id": "warehouse_华东", "label": "毛库存"},
                            ],
                        }
                    ],
                },
                ensure_ascii=False,
            )
        )
        serialized = json.dumps(envelope, ensure_ascii=False)
        self.assertIn("warehouse_WH_001", serialized)
        self.assertIn("SKU_ABC_2026", serialized)
        self.assertEqual("warehouse_WH_001", envelope["questions"][0]["id"])
        self.assertEqual(["SKU_ABC_2026", "o_2"], [item["id"] for item in envelope["questions"][0]["options"]])

    def test_interaction_envelope_accepts_gateway_value_option_ids(self) -> None:
        envelope = interaction_envelope(
            json.dumps(
                {
                    "outcome": "clarification_required",
                    "questions": [
                        {
                            "id": "scope",
                            "question": "请选择统计范围",
                            "options": [
                                {"value": "order_type", "label": "按订单类型"},
                                {"value": "channel", "label": "按渠道标识"},
                            ],
                        }
                    ],
                },
                ensure_ascii=False,
            )
        )
        self.assertEqual("clarification_required", envelope["outcome"])
        self.assertEqual("scope", envelope["questions"][0]["id"])
        self.assertEqual(
            ["order_type", "channel"],
            [option["id"] for option in envelope["questions"][0]["options"]],
        )

    def test_interaction_envelope_assigns_ids_when_gateway_emits_only_choice_copy(self) -> None:
        envelope = interaction_envelope(
            json.dumps(
                {
                    "outcome": "clarification_required",
                    "questions": [
                        {
                            "question": "请选择统计口径",
                            "options": [
                                {"label": "按业务类型", "description": "选择业务类型"},
                                {"label": "按渠道标识", "description": "选择渠道标识"},
                            ],
                        }
                    ],
                },
                ensure_ascii=False,
            )
        )
        self.assertEqual("clarification_required", envelope["outcome"])
        self.assertEqual("q_1", envelope["questions"][0]["id"])
        self.assertEqual(
            ["o_1", "o_2"],
            [option["id"] for option in envelope["questions"][0]["options"]],
        )

    def test_interaction_envelope_rejects_question_overflow_without_truncating(self) -> None:
        questions = [
            {
                "id": f"q_{index}",
                "prompt": "选择库存口径",
                "selection": "single",
                "options": [{"id": "available", "label": "可用库存"}],
            }
            for index in range(1, 13)
        ]
        questions.append(
            {
                "id": "warehouse_WH_SECRET",
                "prompt": "ROW-SENTINEL",
                "options": [{"id": "secret_option", "label": "PRIVATE-ROW"}],
            }
        )
        envelope = interaction_envelope(
            json.dumps(
                {"outcome": "clarification_required", "questions": questions},
                ensure_ascii=False,
            )
        )
        self.assertEqual("blocked", envelope["outcome"])
        self.assertTrue(envelope["needs_review"])
        self.assertEqual([], envelope["questions"])
        serialized = json.dumps(envelope, ensure_ascii=False)
        self.assertNotIn("WH_SECRET", serialized)
        self.assertNotIn("ROW-SENTINEL", serialized)

    def test_interaction_envelope_rejects_option_overflow_without_truncating(self) -> None:
        options = [
            {"id": f"o_{index}", "label": "可用库存"}
            for index in range(1, 21)
        ]
        options.append({"id": "secret_option", "label": "PRIVATE-ROW"})
        envelope = interaction_envelope(
            json.dumps(
                {
                    "outcome": "clarification_required",
                    "questions": [
                        {
                            "id": "basis",
                            "prompt": "选择库存口径",
                            "selection": "single",
                            "options": options,
                        }
                    ],
                },
                ensure_ascii=False,
            )
        )
        self.assertEqual("blocked", envelope["outcome"])
        self.assertTrue(envelope["needs_review"])
        self.assertEqual([], envelope["questions"])
        self.assertNotIn("PRIVATE-ROW", json.dumps(envelope, ensure_ascii=False))

    def test_interaction_envelope_rejects_structural_anomalies(self) -> None:
        malformed_payloads = (
            {"outcome": "clarification_required", "questions": {"id": "basis"}},
            {"outcome": "clarification_required", "questions": [None]},
            {
                "outcome": "clarification_required",
                "questions": [{"id": "basis", "prompt": "选择库存口径", "options": []}],
            },
            {
                "outcome": "clarification_required",
                "questions": [
                    {
                        "id": "basis",
                        "prompt": "选择库存口径",
                        "options": [{"id": "available", "label": "可用库存"}],
                    },
                    {
                        "id": "basis",
                        "prompt": "选择库存口径",
                        "options": [{"id": "gross", "label": "毛库存"}],
                    },
                ],
            },
        )
        for payload in malformed_payloads:
            with self.subTest(payload=payload):
                envelope = interaction_envelope(json.dumps(payload, ensure_ascii=False))
                self.assertEqual("blocked", envelope["outcome"])
                self.assertTrue(envelope["needs_review"])
                self.assertEqual([], envelope["questions"])

    def test_feedback_receipt_does_not_contain_submitted_definition(self) -> None:
        session = _session(self.store, "alice")
        secret_definition = "INTERNAL-DEFINITION-DO-NOT-ECHO-9f2a"
        receipt = self.store.create_metric_feedback(
            session,
            metric_name="新指标",
            description=secret_definition,
            example_request="INTERNAL-EXAMPLE-DO-NOT-ECHO",
        )
        self.assertRegex(receipt, r"^mf_[A-Za-z0-9_-]{10,120}$")
        self.assertNotIn(secret_definition, receipt)
        self.assertNotIn("INTERNAL-EXAMPLE-DO-NOT-ECHO", receipt)

        connection = sqlite3.connect(self.store.path)
        try:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(chat_metric_feedback_v1)"
                ).fetchall()
            }
            self.assertIn("description", columns)
            self.assertIn("status", columns)
        finally:
            connection.close()


class _PrivacyFakeRunner:
    """Return deliberately sensitive child data to exercise the BFF boundary."""

    backend_url = "http://127.0.0.1:9"
    model = "gpt-5.6-sol"
    reasoning_effort = "xhigh"
    auth_configured = True

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def context_home_for_ref(self, principal_ref: str) -> Path:
        return Path("C:/private-context") / principal_ref

    def run(self, question: str, **kwargs: object) -> dict[str, object]:
        self.calls.append({"question": question, **kwargs})
        if "澄清" in question and not kwargs.get("ui_selections"):
            return {
                "status": "completed",
                "final_response": "RAW-ASSISTANT-ANSWER SELECT * FROM secret_table ROW-SENTINEL",
                "thread_id": "thread-shared-001",
                "turn_id": "turn-private-001",
                "context_reused": bool(kwargs.get("thread_id")),
                "context_reset": False,
                "context_compaction": {"status": "not_due", "attempted": False},
                "interaction": {
                    "outcome": "clarification_required",
                    "questions": [
                        {
                            "id": "basis",
                            "prompt": "选择库存口径",
                            "selection": "single",
                            "options": [
                                {"id": "available", "label": "可用库存"},
                                {"id": "gross", "label": "毛库存"},
                            ],
                        },
                        {
                            "id": "window",
                            "prompt": "选择时间窗口",
                            "selection": "single",
                            "options": [
                                {"id": "today", "label": "今天"},
                                {"id": "yesterday", "label": "昨天"},
                            ],
                        },
                    ],
                },
                "query_execution": {
                    "live_business_result_executed": False,
                    "status": "no_live_result",
                },
                "mcp_calls": [],
            }
        return {
            "status": "completed",
            "final_response": "RAW-ASSISTANT-ANSWER SELECT * FROM secret_table ROW-SENTINEL",
            "thread_id": "thread-shared-001",
            "turn_id": "turn-private-002",
            "context_reused": bool(kwargs.get("thread_id")),
            "context_reset": False,
            "context_compaction": {"status": "not_due", "attempted": False},
            "interaction": {
                "outcome": "completed",
                "questions": [],
                "answer": "截至 2026-09-01 00:00，当前可用库存为 128 箱。",
            },
            "verified_business_result": {
                "columns": ["库存箱数"], "rows": [{"库存箱数": "128"}],
            },
            "query_execution": {
                "live_business_result_executed": True,
                "status": "live_result",
                "query_call_count": 1,
                "successful_query_call_count": 1,
            },
            "mcp_calls": [
                {
                    "server": "doris_query_agent",
                    "tool": "query_doris_sql",
                    "status": "completed",
                    "arguments": {"sql": "SELECT * FROM secret_table"},
                    "result": {"rows": [{"secret": "ROW-SENTINEL"}], "columns": ["secret"]},
                }
            ],
            "knowledge_context": {"source_tables": ["secret_table"], "raw": "KNOWLEDGE-SENTINEL"},
            "usage": {"prompt_tokens": 123, "completion_tokens": 456},
        }

    def public_health(self, _backend_health: dict[str, object] | None) -> dict[str, object]:
        return {
            "status": "ok",
            "ready": True,
            "query_backend": {"ready": True, "status": "ok"},
        }


class _MalformedInteractionRunner(_PrivacyFakeRunner):
    """Return an over-limit choice envelope to exercise public fail-closed state."""

    def run(self, question: str, **kwargs: object) -> dict[str, object]:
        result = super().run(question, **kwargs)
        result["interaction"] = {
            "outcome": "clarification_required",
            "questions": [
                {
                    "id": f"q_{index}",
                    "prompt": "选择库存口径",
                    "selection": "single",
                    "options": [{"id": "available", "label": "可用库存"}],
                }
                for index in range(1, 14)
            ],
        }
        result["query_execution"] = {
            "live_business_result_executed": False,
            "status": "no_live_result",
        }
        result["mcp_calls"] = []
        return result


class ChatHttpPrivacyContractTestCase(unittest.TestCase):
    SECRET = "q" * 32

    def setUp(self) -> None:
        self.directory = TemporaryDirectory(prefix="chat-http-privacy-")
        self.store = ChatSessionStore(
            Path(self.directory.name) / "sessions.sqlite3",
            principal_pepper=self.SECRET,
        )
        self.runner = _PrivacyFakeRunner()
        self.app = create_chat_app(
            self.runner,
            session_store=self.store,
            identity_resolver=IdentityResolver(mode="wecom_signed", secret=self.SECRET),
            enforce_loopback=False,
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_public_business_table_keeps_only_reviewed_aggregate_cells(self) -> None:
        raw = {
            "format": "table",
            "table_id": "sales_outbound_by_warehouse",
            "title": "模型不应控制标题",
            "columns": [{"key": "secret_column", "label": "内部字段"}],
            "rows": [
                {
                    "warehouse": "仓甲",
                    "quantity": "1200",
                    "secret_column": "SELECT * FROM private_rows",
                }
            ],
            "summary": [{"label": "合计", "value": "1200", "unit": "EA"}],
            "caption": "2026年9月1日 · 实际完成销售出库",
        }
        public = _public_business_table(raw, live=True)
        self.assertIsNotNone(public)
        assert public is not None
        self.assertEqual("按仓销售出库", public["title"])
        self.assertEqual(["warehouse", "quantity"], [item["key"] for item in public["columns"]])
        self.assertEqual({"warehouse": "仓甲", "quantity": "1200"}, public["rows"][0])
        self.assertNotIn("secret_column", json.dumps(public, ensure_ascii=False))
        self.assertIsNone(
            _public_business_table(
                {
                    **raw,
                    "rows": [{"warehouse": "private.schema", "quantity": "1"}],
                },
                live=True,
            )
        )

    def test_chat_api_publishes_screened_business_table(self) -> None:
        class ModelTableStub(_PrivacyFakeRunner):
            def run(self, _question: str, **_kwargs: object) -> dict[str, object]:
                return {
                    "status": "completed",
                    "final_response": json.dumps(
                        {"outcome": "completed", "answer": "按仓汇总已完成。"},
                        ensure_ascii=False,
                    ),
                    "thread_id": None,
                    "context_reused": False,
                    "interaction": {
                        "outcome": "completed",
                        "questions": [],
                        "answer": "按仓汇总已完成。",
                    },
                    "business_table": {
                        "format": "table",
                        "table_id": "sales_outbound_by_warehouse",
                        "caption": "2026年9月1日 · 实际完成销售出库",
                        "rows": [{"warehouse": "仓甲", "quantity": "128"}],
                        "summary": [{"label": "合计", "value": "128", "unit": "EA"}],
                    },
                    "query_execution": {
                        "live_business_result_executed": True,
                        "status": "live_result",
                    },
                    "mcp_calls": [],
                }

        app = create_chat_app(
            ModelTableStub(),
            session_store=self.store,
            identity_resolver=IdentityResolver(mode="wecom_signed", secret=self.SECRET),
            enforce_loopback=False,
        )
        client = TestClient(app)
        identity_headers = self._signed_headers("table-user")
        bootstrap = client.get("/v1/session?new_conversation=true", headers=identity_headers)
        csrf = bootstrap.json()["csrf_token"]
        conversation = client.post(
            "/v1/conversations",
            headers={**identity_headers, "X-CSRF-Token": csrf},
        ).json()["conversation_id"]
        response = client.post(
            "/v1/chat",
            headers={**identity_headers, "X-CSRF-Token": csrf},
            json={"question": "查询按仓销售出库", "conversation_id": conversation},
        )
        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("table", payload["business_table"]["format"])
        self.assertNotIn("table_id", payload["business_table"])
        self.assertEqual({"warehouse": "仓甲", "quantity": "128"}, payload["business_table"]["rows"][0])

    @staticmethod
    def _signed_headers(subject: str) -> dict[str, str]:
        timestamp = str(int(__import__("time").time()))
        message = f"{timestamp}.{subject}".encode("utf-8")
        signature = hmac.new(
            ChatHttpPrivacyContractTestCase.SECRET.encode("utf-8"),
            message,
            hashlib.sha256,
        ).hexdigest()
        return {
            "X-WeCom-User-Id": subject,
            "X-WeCom-Identity-Timestamp": timestamp,
            "X-WeCom-Identity-Signature": signature,
        }

    def _ready_client(self, subject: str) -> tuple[TestClient, dict[str, str], str]:
        client = TestClient(self.app)
        identity_headers = self._signed_headers(subject)
        bootstrap = client.get(
            "/v1/session?new_conversation=true", headers=identity_headers
        )
        self.assertEqual(200, bootstrap.status_code)
        csrf = bootstrap.json()["csrf_token"]
        created = client.post(
            "/v1/conversations",
            headers={**identity_headers, "X-CSRF-Token": csrf},
        )
        self.assertEqual(200, created.status_code)
        return client, identity_headers, csrf

    def test_chat_public_response_exposes_only_safe_business_result(self) -> None:
        client, identity_headers, csrf = self._ready_client("alice")
        conversation = client.get("/v1/session", headers=identity_headers).json()["conversation_id"]
        response = client.post(
            "/v1/chat",
            headers={**identity_headers, "X-CSRF-Token": csrf},
            json={"question": "查询实时库存", "conversation_id": conversation},
        )
        self.assertEqual(200, response.status_code)
        payload = response.json()
        serialized = json.dumps(payload, ensure_ascii=False)
        forbidden_keys = {
            "answer",
            "final_response",
            "mcp_calls",
            "knowledge_context",
            "usage",
            "thread_id",
            "turn_id",
            "sql",
            "bindings",
            "rows",
            "columns",
            "result",
            "source_tables",
            "parameters",
            "model",
            "reasoning_effort",
        }
        self.assertFalse(forbidden_keys & set(payload))
        required_public = {
            "request_id",
            "conversation_id",
            "status",
            "process",
            "outcome",
            "clarification",
            "context_state",
        }
        allowed_process_metadata = required_public | {
            "result_state",
            "query_status",
            "normalized_result_state",
            "query_execution",
            "elapsed_ms",
            "business_result",
            "business_table",
        }
        self.assertTrue(required_public <= set(payload))
        self.assertTrue(set(payload) <= allowed_process_metadata)
        self.assertEqual("live_success", payload["result_state"])
        for marker in ("RAW-ASSISTANT-ANSWER", "secret_table", "ROW-SENTINEL", "KNOWLEDGE-SENTINEL"):
            self.assertNotIn(marker, serialized)
        self.assertNotIn("SELECT", serialized.upper())
        self.assertEqual("completed", payload["outcome"]["state"])
        self.assertEqual("plain_text", payload["business_result"]["format"])
        self.assertEqual("128", next(iter(payload["business_table"]["rows"][0].values())))

    def test_clarification_returns_all_choices_and_selection_does_not_echo_raw_data(self) -> None:
        client, identity_headers, csrf = self._ready_client("alice")
        conversation = client.get("/v1/session", headers=identity_headers).json()["conversation_id"]
        response = client.post(
            "/v1/chat",
            headers={**identity_headers, "X-CSRF-Token": csrf},
            json={"question": "澄清库存口径", "conversation_id": conversation},
        )
        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("clarification_required", payload["outcome"]["state"])
        questions = payload["clarification"]["questions"]
        self.assertEqual(2, len(questions))
        self.assertEqual({"basis", "window"}, {item["id"] for item in questions})
        self.assertEqual("waiting_for_selection", payload["process"]["state"])
        token = payload["clarification"]["token"]
        follow_up = client.post(
            "/v1/chat",
            headers={**identity_headers, "X-CSRF-Token": csrf},
            json={
                "conversation_id": conversation,
                "clarification_token": token,
                "clarification_answers": [
                    {"question_id": "basis", "option_id": "available"},
                    {"question_id": "window", "option_id": "today"},
                ],
            },
        )
        self.assertEqual(200, follow_up.status_code)
        self.assertEqual("completed", follow_up.json()["outcome"]["state"])
        self.assertNotIn("SELECT", json.dumps(follow_up.json()).upper())

    def test_pending_clarification_is_replaced_by_a_new_question(self) -> None:
        first, identity_headers, csrf = self._ready_client("alice")
        conversation = first.get("/v1/session", headers=identity_headers).json()[
            "conversation_id"
        ]
        clarification = first.post(
            "/v1/chat",
            headers={**identity_headers, "X-CSRF-Token": csrf},
            json={"question": "澄清库存口径", "conversation_id": conversation},
        )
        self.assertEqual(200, clarification.status_code)
        token = clarification.json()["clarification"]["token"]

        second = TestClient(self.app)
        second_bootstrap = second.get(
            "/v1/session?new_conversation=true", headers=identity_headers
        ).json()
        self.assertEqual(conversation, second_bootstrap["conversation_id"])
        replaced = second.post(
            "/v1/chat",
            headers={
                **identity_headers,
                "X-CSRF-Token": second_bootstrap["csrf_token"],
            },
            json={"question": "继续查询别的指标", "conversation_id": conversation},
        )
        self.assertEqual(200, replaced.status_code)
        self.assertEqual("completed", replaced.json()["outcome"]["state"])
        self.assertEqual(2, len(self.runner.calls))
        with self.assertRaises(ClarificationValidationError):
            self.store.validate_clarification_token(
                _session(self.store, "alice"),
                conversation,
                token,
                [{"question_id": "basis", "option_id": "available"}],
            )

    def test_pending_clarification_is_reported_and_can_be_resumed_with_csrf(self) -> None:
        first, identity_headers, csrf = self._ready_client("alice")
        conversation = first.get("/v1/session", headers=identity_headers).json()[
            "conversation_id"
        ]
        created = first.post(
            "/v1/chat",
            headers={**identity_headers, "X-CSRF-Token": csrf},
            json={"question": "澄清库存口径", "conversation_id": conversation},
        )
        self.assertEqual(200, created.status_code)
        original = created.json()["clarification"]

        second = TestClient(self.app)
        bootstrap = second.get("/v1/session", headers=identity_headers)
        self.assertEqual(200, bootstrap.status_code)
        self.assertTrue(bootstrap.json()["clarification_pending"])
        second_csrf = bootstrap.json()["csrf_token"]

        missing_csrf = second.post(
            "/v1/clarification/resume",
            json={"conversation_id": conversation},
        )
        self.assertEqual(403, missing_csrf.status_code)

        resumed = second.post(
            "/v1/clarification/resume",
            headers={**identity_headers, "X-CSRF-Token": second_csrf},
            json={"conversation_id": conversation},
        )
        self.assertEqual(200, resumed.status_code)
        resumed_payload = resumed.json()
        self.assertEqual("needs_selection", resumed_payload["status"])
        self.assertTrue(resumed_payload["clarification"]["required"])
        self.assertNotEqual(original["token"], resumed_payload["clarification"]["token"])
        self.assertEqual(
            original["questions"], resumed_payload["clarification"]["questions"]
        )

        completed = second.post(
            "/v1/chat",
            headers={**identity_headers, "X-CSRF-Token": second_csrf},
            json={
                "conversation_id": conversation,
                "clarification_token": resumed_payload["clarification"]["token"],
                "clarification_answers": [
                    {"question_id": "basis", "option_id": "available"},
                    {"question_id": "window", "option_id": "today"},
                ],
            },
        )
        self.assertEqual(200, completed.status_code)

    def test_malformed_choice_envelope_is_blocked_by_public_chat_boundary(self) -> None:
        runner = _MalformedInteractionRunner()
        app = create_chat_app(
            runner,
            session_store=self.store,
            identity_resolver=IdentityResolver(mode="wecom_signed", secret=self.SECRET),
            enforce_loopback=False,
        )
        client = TestClient(app)
        identity_headers = self._signed_headers("alice")
        bootstrap = client.get(
            "/v1/session?new_conversation=true", headers=identity_headers
        )
        self.assertEqual(200, bootstrap.status_code)
        csrf = bootstrap.json()["csrf_token"]
        conversation = client.post(
            "/v1/conversations",
            headers={**identity_headers, "X-CSRF-Token": csrf},
        ).json()["conversation_id"]
        response = client.post(
            "/v1/chat",
            headers={**identity_headers, "X-CSRF-Token": csrf},
            json={"question": "查询库存", "conversation_id": conversation},
        )
        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("blocked", payload["status"])
        self.assertEqual("blocked", payload["outcome"]["state"])
        self.assertFalse(payload["clarification"]["required"])
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("SELECT", serialized.upper())
        self.assertNotIn("q_13", serialized)

    def test_feedback_endpoint_returns_only_receipt_and_does_not_echo_definition(self) -> None:
        client, identity_headers, csrf = self._ready_client("alice")
        secret_text = "DEFINITION-PRIVATE-SENTINEL"
        response = client.post(
            "/v1/metric-feedback",
            headers={**identity_headers, "X-CSRF-Token": csrf},
            json={
                "metric_name": "新指标",
                "business_definition": secret_text,
                "scope_and_grain": "DEFINITION-SCOPE-SENTINEL",
            },
        )
        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual({"status", "receipt_id"}, set(payload))
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(secret_text, serialized)
        self.assertNotIn("DEFINITION-SCOPE-SENTINEL", serialized)

    def test_same_principal_ui_conversations_share_thread_but_foreign_principal_is_rejected(self) -> None:
        alice, alice_headers, alice_csrf = self._ready_client("alice")
        alice_session = alice.get("/v1/session", headers=alice_headers).json()
        first_id = alice_session["conversation_id"]
        first = alice.post(
            "/v1/chat",
            headers={**alice_headers, "X-CSRF-Token": alice_csrf},
            json={"question": "查询实时库存", "conversation_id": first_id},
        )
        self.assertEqual(200, first.status_code)

        second_conversation = alice.post(
            "/v1/conversations",
            headers={**alice_headers, "X-CSRF-Token": alice_csrf},
        ).json()["conversation_id"]
        self.assertEqual(first_id, second_conversation)
        second = alice.post(
            "/v1/chat",
            headers={**alice_headers, "X-CSRF-Token": alice_csrf},
            json={"question": "查询实时库存", "conversation_id": second_conversation},
        )
        self.assertEqual(200, second.status_code)
        third = alice.post(
            "/v1/chat",
            headers={**alice_headers, "X-CSRF-Token": alice_csrf},
            json={"question": "查询实时库存", "conversation_id": first_id},
        )
        self.assertEqual(200, third.status_code)
        self.assertGreaterEqual(len(self.runner.calls), 3)
        self.assertEqual(
            self.runner.calls[1]["thread_id"], self.runner.calls[2]["thread_id"]
        )
        self.assertEqual(
            self.runner.calls[1]["context_home"], self.runner.calls[2]["context_home"]
        )

        bob, bob_headers, bob_csrf = self._ready_client("bob")
        foreign = bob.post(
            "/v1/chat",
            headers={**bob_headers, "X-CSRF-Token": bob_csrf},
            json={"question": "查询实时库存", "conversation_id": first_id},
        )
        self.assertEqual(404, foreign.status_code)
        self.assertEqual("conversation_not_found", foreign.json()["error"]["code"])


if __name__ == "__main__":
    unittest.main()

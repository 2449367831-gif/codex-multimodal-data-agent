from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
import unittest

from fastapi.testclient import TestClient

from app.chat_api import IdentityResolver, create_chat_app
from app.chat_sessions import (
    ChatSessionStore,
    ClarificationValidationError,
    ConversationNotFoundError,
    ConversationBusyError,
    Principal,
)


class ContextAwareRunner:
    model = "gpt-5.6-sol"
    reasoning_effort = "xhigh"
    backend_url = "http://127.0.0.1:9"
    auth_configured = True

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self._counter = 0

    def context_home_for_ref(self, principal_ref: str) -> Path:
        return Path("D:/private-context") / principal_ref

    def run(self, question: str, **kwargs: object) -> dict[str, object]:
        self.calls.append({"question": question, **kwargs})
        self._counter += 1
        return {
            "status": "completed",
            # This is intentionally a raw-looking model response. The API must
            # never expose it to the browser.
            "final_response": "SELECT secret FROM ywzt_ods.private_rows",
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "thread_id": "thread-context-12345678",
            "turn_id": f"turn-{self._counter}",
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
            "interaction": {
                "outcome": "completed",
                "questions": [],
                "answer": "当前可用库存为 128 箱。",
            },
            "verified_business_result": {
                "columns": ["库存箱数"], "rows": [{"库存箱数": "128"}],
            },
            "context_compaction": {"status": "not_due", "attempted": False},
        }

    def public_health(self, _backend_health: object) -> dict[str, object]:
        return {"status": "ok", "ready": True, "query_backend": {"ready": True}}


def _signed_headers(user_id: str, secret: str) -> dict[str, str]:
    timestamp = str(int(time.time()))
    signature = hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.{user_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-WeCom-User-Id": user_id,
        "X-WeCom-Identity-Timestamp": timestamp,
        "X-WeCom-Identity-Signature": signature,
    }


class PrincipalContextStoreTestCase(unittest.TestCase):
    def test_aliases_share_context_and_context_lease_is_principal_scoped(self) -> None:
        with TemporaryDirectory(prefix="context-store-") as directory:
            store = ChatSessionStore(Path(directory) / "sessions.sqlite3")
            session = store.ensure_session(None, None, Principal("wecom", "alice"))
            first = store.create_conversation(session)
            second = store.create_conversation(session)
            self.assertEqual(first, second)
            with store._connection() as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM chat_conversations_v2 WHERE principal_hash = ?",
                    (session.principal_hash,),
                ).fetchone()[0]
            self.assertEqual(1, count)
            lease = store.acquire_context(session)
            with self.assertRaises(ConversationBusyError):
                store.acquire_context(session)
            store.release_context(session, lease)

            bound = store.bind_context_thread(session, "thread-context-12345678")
            self.assertEqual("thread-context-12345678", bound.codex_thread_id)
            after = store.record_context_turn(session, bound.codex_thread_id)
            self.assertEqual(1, after.turn_count)
            self.assertEqual("principal", store.public_context_state(after)["scope"])
            self.assertNotIn("codex_thread_id", store.public_context_state(after))

    def test_legacy_duplicate_conversations_are_migrated_to_one_canonical_row(self) -> None:
        with TemporaryDirectory(prefix="conversation-migration-") as directory:
            path = Path(directory) / "sessions.sqlite3"
            store = ChatSessionStore(path)
            session = store.ensure_session(None, None, Principal("wecom", "alice"))
            canonical = store.create_conversation(session)
            duplicate = "c_legacy_duplicate_123456"
            now = time.time()
            connection = sqlite3.connect(path)
            try:
                connection.execute("DROP INDEX ux_chat_conversations_v2_principal")
                connection.execute(
                    """
                    INSERT INTO chat_conversations_v2
                        (conversation_id, principal_hash, created_by_token_hash,
                         created_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (duplicate, session.principal_hash, "legacy-token", now - 20, now - 20),
                )
                connection.execute(
                    """
                    INSERT INTO chat_clarification_tokens_v1
                        (token_hash, principal_hash, conversation_id,
                         question_ids_json, option_ids_json, selection_json,
                         created_at, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "legacy-token-hash",
                        session.principal_hash,
                        duplicate,
                        '["basis"]',
                        '{"basis":["available"]}',
                        '{"basis":"single"}',
                        now,
                        now + 600,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO chat_metric_feedback_v1
                        (receipt_id, principal_hash, conversation_id, metric_name,
                         description, example_request, scope_and_grain,
                         unit_and_window, notes, created_at, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "mf_legacy_receipt_123456",
                        session.principal_hash,
                        duplicate,
                        "legacy_metric",
                        "legacy definition",
                        None,
                        None,
                        None,
                        None,
                        now,
                        "pending_review",
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO chat_leases_v2
                        (conversation_id, principal_hash, lease_id, acquired_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (duplicate, session.principal_hash, "legacy-lease", now),
                )
                connection.commit()
            finally:
                connection.close()

            migrated = ChatSessionStore(path)
            self.assertEqual(canonical, migrated.find_default_conversation(session))
            connection = sqlite3.connect(path)
            try:
                self.assertEqual(
                    1,
                    connection.execute(
                        "SELECT COUNT(*) FROM chat_conversations_v2 WHERE principal_hash = ?",
                        (session.principal_hash,),
                    ).fetchone()[0],
                )
                self.assertEqual(
                    0,
                    connection.execute(
                        "SELECT COUNT(*) FROM chat_conversations_v2 WHERE conversation_id = ?",
                        (duplicate,),
                    ).fetchone()[0],
                )
                self.assertEqual(
                    canonical,
                    connection.execute(
                        "SELECT conversation_id FROM chat_clarification_tokens_v1 WHERE token_hash = ?",
                        ("legacy-token-hash",),
                    ).fetchone()[0],
                )
                self.assertEqual(
                    canonical,
                    connection.execute(
                        "SELECT conversation_id FROM chat_metric_feedback_v1 WHERE receipt_id = ?",
                        ("mf_legacy_receipt_123456",),
                    ).fetchone()[0],
                )
                self.assertEqual(
                    canonical,
                    connection.execute(
                        "SELECT conversation_id FROM chat_leases_v2 WHERE lease_id = ?",
                        ("legacy-lease",),
                    ).fetchone()[0],
                )
                unique_indexes = connection.execute(
                    "PRAGMA index_list(chat_conversations_v2)"
                ).fetchall()
                self.assertTrue(any(row[1] == "ux_chat_conversations_v2_principal" and row[2] for row in unique_indexes))
            finally:
                connection.close()

    def test_clarification_token_is_owned_and_single_use(self) -> None:
        with TemporaryDirectory(prefix="clarification-store-") as directory:
            store = ChatSessionStore(Path(directory) / "sessions.sqlite3")
            alice = store.ensure_session(None, None, Principal("wecom", "alice"))
            bob = store.ensure_session(None, None, Principal("wecom", "bob"))
            conversation = store.create_conversation(alice)
            token = store.create_clarification_token(
                alice,
                conversation,
                [
                    {
                        "id": "basis",
                        "selection": "single",
                        "options": [
                            {"id": "actual", "label": "实际完成"},
                            {"id": "order", "label": "订单请求"},
                        ],
                    }
                ],
            )
            self.assertTrue(store.has_pending_clarification(alice, conversation))
            with self.assertRaises(ConversationNotFoundError):
                store.validate_clarification_token(
                    bob,
                    conversation,
                    token,
                    [{"question_id": "basis", "option_id": "actual"}],
                )
            answers = store.validate_clarification_token(
                alice,
                conversation,
                token,
                [{"question_id": "basis", "option_id": "actual"}],
            )
            self.assertEqual(
                [{"question_id": "basis", "option_ids": ["actual"]}],
                answers,
            )
            self.assertFalse(store.has_pending_clarification(alice, conversation))
            with self.assertRaises(ClarificationValidationError):
                store.validate_clarification_token(
                    alice,
                    conversation,
                    token,
                    [{"question_id": "basis", "option_id": "actual"}],
                )

    def test_legacy_placeholder_clarification_is_retired_on_resume(self) -> None:
        """A card whose semantic copy cannot be recovered must not reach the UI."""

        with TemporaryDirectory(prefix="placeholder-clarification-") as directory:
            store = ChatSessionStore(Path(directory) / "sessions.sqlite3")
            alice = store.ensure_session(None, None, Principal("wecom", "alice"))
            conversation = store.create_conversation(alice)
            now = time.time()
            connection = sqlite3.connect(store.path)
            try:
                connection.execute(
                    """
                    INSERT INTO chat_clarification_tokens_v1
                        (token_hash, principal_hash, conversation_id,
                         question_ids_json, option_ids_json, selection_json,
                         public_questions_json, created_at, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "legacy-placeholder-token",
                        alice.principal_hash,
                        conversation,
                        '["q_2"]',
                        '{"q_2":["o_1","o_2"]}',
                        '{"q_2":"single"}',
                        json.dumps(
                            [
                                {
                                    "id": "q_2",
                                    "prompt": "请确认本次业务口径。",
                                    "selection": "single",
                                    "options": [
                                        {"id": "o_1", "label": "该选项"},
                                        {"id": "o_2", "label": "该选项"},
                                    ],
                                }
                            ],
                            ensure_ascii=False,
                        ),
                        now,
                        now + 600,
                    ),
                )
                connection.commit()
            finally:
                connection.close()
            self.assertTrue(store.has_pending_clarification(alice, conversation))
            self.assertIsNone(store.resume_clarification(alice, conversation))
            self.assertFalse(store.has_pending_clarification(alice, conversation))


class ChatContextApiTestCase(unittest.TestCase):
    def test_public_chat_response_contains_safe_result_and_feedback_is_receipted(self) -> None:
        with TemporaryDirectory(prefix="context-api-") as directory:
            store = ChatSessionStore(Path(directory) / "sessions.sqlite3")
            runner = ContextAwareRunner()
            app = create_chat_app(
                runner,
                session_store=store,
                identity_resolver=IdentityResolver(mode="anonymous"),
                enforce_loopback=False,
            )
            client = TestClient(app)
            bootstrap = client.get("/v1/session").json()
            csrf = bootstrap["csrf_token"]
            conversation = client.post(
                "/v1/conversations",
                headers={"X-CSRF-Token": csrf},
            ).json()["conversation_id"]
            response = client.post(
                "/v1/chat",
                headers={"X-CSRF-Token": csrf},
                json={"question": "查询库存", "conversation_id": conversation},
            )
            self.assertEqual(200, response.status_code)
            body = response.json()
            for forbidden in (
                "answer",
                "final_response",
                "mcp_calls",
                "knowledge_context",
                "usage",
                "thread_id",
                "turn_id",
                "query_execution",
            ):
                self.assertNotIn(forbidden, body)
            self.assertEqual("completed", body["outcome"]["state"])
            self.assertEqual("128", next(iter(body["business_table"]["rows"][0].values())))
            self.assertEqual(0, body["context_state"]["turn_count"])
            self.assertEqual("request", body["context_state"]["scope"])
            self.assertEqual("fresh_per_request", body["context_state"]["query_context_mode"])
            self.assertTrue(body["context_state"]["learning_summary_only"])
            self.assertIn("thread_id", runner.calls[0])
            self.assertIsNone(runner.calls[0]["thread_id"])

            second = client.post(
                "/v1/chat",
                headers={"X-CSRF-Token": csrf},
                json={"question": "继续", "conversation_id": conversation},
            )
            self.assertEqual(200, second.status_code)
            self.assertIsNone(runner.calls[1]["thread_id"])
            self.assertIsNone(runner.calls[1]["context_home"])
            self.assertEqual(0, second.json()["context_state"]["turn_count"])

            feedback = client.post(
                "/v1/metric-feedback",
                headers={"X-CSRF-Token": csrf},
                json={
                    "metric_name": "仓库周转",
                    "business_definition": "按业务确认的出库口径计算",
                    "scope_and_grain": "货主+仓库+SKU",
                    "unit_and_window": "EA，月初至当前",
                    "example_question": "查询本月周转",
                    "conversation_id": conversation,
                },
            )
            self.assertEqual(200, feedback.status_code)
            feedback_body = feedback.json()
            self.assertEqual("accepted", feedback_body["status"])
            self.assertTrue(feedback_body["receipt_id"].startswith("mf_"))
            self.assertNotIn("business_definition", feedback_body)
            self.assertNotIn("scope_and_grain", feedback_body)

    def test_different_wecom_users_do_not_reuse_thread(self) -> None:
        secret = "x" * 32
        with TemporaryDirectory(prefix="context-principal-api-") as directory:
            store = ChatSessionStore(
                Path(directory) / "sessions.sqlite3", principal_pepper=secret
            )
            runner = ContextAwareRunner()
            app = create_chat_app(
                runner,
                session_store=store,
                identity_resolver=IdentityResolver(mode="wecom_signed", secret=secret),
                enforce_loopback=False,
            )
            alice = TestClient(app)
            bob = TestClient(app)
            a_bootstrap = alice.get(
                "/v1/session", headers=_signed_headers("alice", secret)
            ).json()
            a_conversation = alice.post(
                "/v1/conversations",
                headers={
                    **_signed_headers("alice", secret),
                    "X-CSRF-Token": a_bootstrap["csrf_token"],
                },
            ).json()["conversation_id"]
            b_bootstrap = bob.get(
                "/v1/session", headers=_signed_headers("bob", secret)
            ).json()
            b_conversation = bob.post(
                "/v1/conversations",
                headers={
                    **_signed_headers("bob", secret),
                    "X-CSRF-Token": b_bootstrap["csrf_token"],
                },
            ).json()["conversation_id"]
            for client, bootstrap, conversation, user in (
                (alice, a_bootstrap, a_conversation, "alice"),
                (bob, b_bootstrap, b_conversation, "bob"),
            ):
                response = client.post(
                    "/v1/chat",
                    headers={
                        **_signed_headers(user, secret),
                        "X-CSRF-Token": bootstrap["csrf_token"],
                    },
                    json={"question": "查询", "conversation_id": conversation},
                )
                self.assertEqual(200, response.status_code)
            self.assertIsNone(runner.calls[-2]["thread_id"])
            self.assertIsNone(runner.calls[-1]["thread_id"])


if __name__ == "__main__":
    unittest.main()

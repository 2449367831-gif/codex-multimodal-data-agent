from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from app.chat_api import IdentityResolver, create_chat_app
from app.chat_sessions import ChatSessionStore
from app.learning import (
    LearningFeedbackNotFoundError,
    LearningLedger,
)


class LearningLedgerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory(prefix="query-learning-")
        self.root = Path(self.directory.name)
        self.ledger = LearningLedger(self.root / "learning.sqlite3")
        self.principal = "principal_hash_aaaaaaaa"
        self.other_principal = "principal_hash_bbbbbbbb"
        self.conversation = "c_aaaaaaaaaaaa"

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_query_events_are_sanitized_clustered_and_idempotent(self) -> None:
        first = self.ledger.record_query(
            principal_hash=self.principal,
            conversation_id=self.conversation,
            request_id="req_learning_0001",
            question="昨天各仓库入库数量",
            outcome="completed",
            execution_status="completed",
            has_business_result=True,
        )
        second = self.ledger.record_query(
            principal_hash=self.principal,
            conversation_id=self.conversation,
            request_id="req_learning_0002",
            question="昨日各仓库到货量",
            outcome="failed",
            execution_status="timeout",
            has_business_result=False,
        )
        replay = self.ledger.record_query(
            principal_hash=self.principal,
            conversation_id=self.conversation,
            request_id="req_learning_0001",
            question="昨天各仓库入库数量",
            outcome="completed",
            execution_status="completed",
            has_business_result=True,
        )

        self.assertTrue(first.startswith("le_"))
        self.assertEqual(first, replay)
        self.assertNotEqual(first, second)
        connection = sqlite3.connect(self.root / "learning.sqlite3")
        try:
            events = connection.execute(
                "SELECT COUNT(*) FROM chat_learning_events_v1 WHERE principal_hash = ?",
                (self.principal,),
            ).fetchone()[0]
            candidates = connection.execute(
                "SELECT occurrence_count, payload_json FROM chat_learning_candidates_v1 "
                "WHERE principal_hash = ? AND candidate_type = 'question_pattern'",
                (self.principal,),
            ).fetchall()
        finally:
            connection.close()
        self.ledger.record_query(
            principal_hash=self.principal,
            conversation_id=self.conversation,
            request_id="req_learning_0004",
            question="请执行 SELECT * FROM secret_table sk-1234567890123456",
            outcome="blocked",
            execution_status="blocked",
            has_business_result=False,
        )
        connection = sqlite3.connect(self.root / "learning.sqlite3")
        try:
            events = connection.execute(
                "SELECT COUNT(*) FROM chat_learning_events_v1 WHERE principal_hash = ?",
                (self.principal,),
            ).fetchone()[0]
            all_candidates = connection.execute(
                "SELECT occurrence_count, payload_json FROM chat_learning_candidates_v1 "
                "WHERE principal_hash = ?",
                (self.principal,),
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(3, events)
        self.assertEqual(1, len(candidates))
        self.assertEqual(2, candidates[0][0])
        self.assertTrue(all("SELECT * FROM" not in row[1].upper() for row in all_candidates))
        self.assertTrue(all("secret_table" not in row[1] for row in all_candidates))
        self.assertTrue(all("sk-1234567890123456" not in row[1] for row in all_candidates))

        summary = self.ledger.public_summary(self.principal)
        serialized = json.dumps(summary, ensure_ascii=False)
        self.assertEqual(3, summary["question_count"])
        self.assertEqual(1, summary["completed_count"])
        self.assertNotIn("昨天各仓库", serialized)
        self.assertNotIn("SELECT", serialized.upper())

    def test_feedback_update_withdraws_old_correction_evidence(self) -> None:
        request_id = "req_learning_0003"
        self.ledger.record_query(
            principal_hash=self.principal,
            conversation_id=self.conversation,
            request_id=request_id,
            question="上个月库存周转",
            outcome="completed",
            execution_status="completed",
            has_business_result=True,
        )
        first = self.ledger.record_feedback(
            principal_hash=self.principal,
            conversation_id=self.conversation,
            request_id=request_id,
            feedback_type="correction",
            issue_type="definition",
            note="口径需要确认",
        )
        replay = self.ledger.record_feedback(
            principal_hash=self.principal,
            conversation_id=self.conversation,
            request_id=request_id,
            feedback_type="correction",
            issue_type="definition",
            note="口径需要确认",
        )
        self.assertEqual(first["feedback_id"], replay["feedback_id"])
        rows = self.ledger.list_candidates(self.principal, status="needs_review")
        correction = [row for row in rows if row["candidate_type"] == "correction"]
        self.assertEqual(1, len(correction))
        self.assertEqual(1, correction[0]["occurrence_count"])

        self.ledger.record_feedback(
            principal_hash=self.principal,
            conversation_id=self.conversation,
            request_id=request_id,
            feedback_type="accepted",
        )
        rows_after = self.ledger.list_candidates(self.principal, status="needs_review")
        self.assertFalse([row for row in rows_after if row["candidate_type"] == "correction"])
        summary = self.ledger.public_summary(self.principal)
        self.assertEqual(1, summary["accepted_count"])
        self.assertEqual(0, summary["correction_count"])

        with self.assertRaises(LearningFeedbackNotFoundError):
            self.ledger.record_feedback(
                principal_hash=self.other_principal,
                conversation_id=self.conversation,
                request_id=request_id,
                feedback_type="accepted",
            )

    def test_dms_questions_are_grouped_under_their_own_route(self) -> None:
        self.ledger.record_query(
            principal_hash=self.principal,
            conversation_id=self.conversation,
            request_id="req_learning_dms_01",
            question="看下dms的1-8月积分兑换的空气炸锅有多少",
            outcome="completed",
            execution_status="completed",
            has_business_result=True,
        )
        summary = self.ledger.public_summary(self.principal)
        routes = {item["id"]: item["count"] for item in summary["route_families"]}
        self.assertEqual(1, routes["dms_points_redemption"])

    def test_definition_submission_is_private_candidate_material(self) -> None:
        self.ledger.record_definition_submission(
            principal_hash=self.principal,
            conversation_id=self.conversation,
            metric_name="库存周转",
            definition_text="成品库存除以时间区间内平均销售成品数量",
            example_question="上个月库存周转是多少",
        )
        digest = self.ledger.build_digest(self.principal)
        self.assertEqual(1, digest["counts"]["definition_submissions"])
        self.assertFalse(digest["policy"]["direct_production_promotion"])
        self.assertTrue((self.root / "learning-digests").is_dir())


class _LearningFakeRunner:
    backend_url = "http://127.0.0.1:9"
    model = "gpt-5.6-sol"
    reasoning_effort = "xhigh"
    auth_configured = True
    include_verified_table = True

    def run(self, _question: str, **_kwargs: object) -> dict[str, object]:
        result = {
            "status": "completed",
            "final_response": json.dumps(
                {"outcome": "completed", "answer": "合计 12 EA。"},
                ensure_ascii=False,
            ),
            "thread_id": "thread-learning-test",
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
        if self.include_verified_table:
            result["verified_business_result"] = {
                "columns": ["quantity"], "rows": [{"quantity": "12"}],
            }
        return result

    def public_health(self, _backend_health: dict[str, object] | None) -> dict[str, object]:
        return {
            "status": "ok",
            "ready": True,
            "query_backend": {"ready": True, "status": "ok"},
        }


class LearningApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory(prefix="query-learning-api-")
        path = Path(self.directory.name) / "sessions.sqlite3"
        store = ChatSessionStore(path)
        self.runner = _LearningFakeRunner()
        app = create_chat_app(
            self.runner,
            session_store=store,
            identity_resolver=IdentityResolver(mode="anonymous"),
            enforce_loopback=False,
        )
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_chat_feedback_and_summary_form_a_safe_loop(self) -> None:
        bootstrap = self.client.get("/v1/session").json()
        conversation_id = self.client.post(
            "/v1/conversations",
            headers={"X-CSRF-Token": bootstrap["csrf_token"]},
        ).json()["conversation_id"]
        response = self.client.post(
            "/v1/chat",
            headers={
                "X-CSRF-Token": bootstrap["csrf_token"],
                "Idempotency-Key": "req_api_learning_01",
            },
            json={"question": "查询昨天入库", "conversation_id": conversation_id},
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual("live_success", response.json()["result_state"])
        self.assertEqual([{"col_1": "12"}], response.json()["business_table"]["rows"])
        request_id = response.json()["request_id"]
        self.assertEqual(1, self.client.get("/v1/learning/summary").json()["question_count"])

        feedback = self.client.post(
            "/v1/query-feedback",
            headers={"X-CSRF-Token": bootstrap["csrf_token"]},
            json={
                "conversation_id": conversation_id,
                "request_id": request_id,
                "feedback_type": "accepted",
            },
        )
        self.assertEqual(200, feedback.status_code)
        summary = self.client.get("/v1/learning/summary").json()
        self.assertEqual(1, summary["accepted_count"])
        serialized = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn("查询昨天入库", serialized)
        self.assertNotIn("sql", serialized.casefold())

    def test_model_prose_without_a_table_is_not_learned_as_success(self) -> None:
        self.runner.include_verified_table = False
        session = self.client.get("/v1/session").json()
        response = self.client.post(
            "/v1/chat",
            headers={"X-CSRF-Token": session["csrf_token"], "Idempotency-Key": "req_api_learning_no_table"},
            json={"question": "查询昨天入库", "conversation_id": session["conversation_id"]},
        )
        self.assertEqual(502, response.status_code)
        self.assertNotIn("business_table", response.json())
        self.assertNotIn("合计 12", response.text)
        summary = self.client.get("/v1/learning/summary").json()
        self.assertEqual(1, summary["question_count"])
        self.assertEqual(0, summary["completed_count"])


if __name__ == "__main__":
    unittest.main()

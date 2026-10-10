from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import httpx

from app.chat_api import IdentityResolver, create_chat_app
from app.chat_sessions import ChatSessionStore, Principal
from app.codex_agent import _codex_recovery_prompt, _run_codex_turn
from app.mcp_server import HttpQueryBackend


class CodexTurnResilienceTestCase(unittest.TestCase):
    def test_transient_transport_failure_is_retried_once(self) -> None:
        class Thread:
            def __init__(self) -> None:
                self.calls = 0

            def run(self, prompt: str, **kwargs: object) -> str:
                self.calls += 1
                if self.calls == 1:
                    raise ConnectionError("connection reset")
                return "ok"

        thread = Thread()
        with patch("app.codex_agent.time.sleep") as sleep:
            result = _run_codex_turn(
                thread,
                "query",
                effort="xhigh",
                model="gpt-5.6-sol",
                sandbox="read_only",
            )
        self.assertEqual("ok", result)
        self.assertEqual(2, thread.calls)
        sleep.assert_called_once()

    def test_completed_live_query_without_answer_gets_result_recovery(self) -> None:
        prompt = _codex_recovery_prompt(
            '{"outcome":"blocked","questions":[]}',
            {"live_business_result_executed": True},
        )
        self.assertIsNotNone(prompt)
        self.assertIn("不要再次查询", str(prompt))

    def test_clarification_and_release_blocks_are_not_retried(self) -> None:
        self.assertIsNone(
            _codex_recovery_prompt(
                '{"outcome":"clarification_required","questions":[]}',
                {"live_business_result_executed": False},
            )
        )
        self.assertIsNone(
            _codex_recovery_prompt(
                "当前口径尚未审批发布",
                {"live_business_result_executed": False},
            )
        )


class HttpQueryResilienceTestCase(unittest.TestCase):
    def test_transient_transport_failure_is_retried_once(self) -> None:
        backend = HttpQueryBackend("http://127.0.0.1:8010")
        with (
            patch.object(
                backend,
                "_query_once",
                side_effect=[
                    httpx.ConnectError("connection reset"),
                    (200, b'{"status":"succeeded"}'),
                ],
            ) as query_once,
            patch("app.mcp_server.time.sleep") as sleep,
        ):
            result = backend.query(sql="SELECT 1")
        self.assertEqual({"status": "succeeded"}, result)
        self.assertEqual(2, query_once.call_count)
        sleep.assert_called_once()

    def test_policy_error_is_not_retried(self) -> None:
        backend = HttpQueryBackend("http://127.0.0.1:8010")
        with (
            patch.object(
                backend,
                "_query_once",
                return_value=(403, b'{"error":{"code":"policy_blocked","message":"blocked"}}'),
            ) as query_once,
            patch("app.mcp_server.time.sleep") as sleep,
        ):
            with self.assertRaises(RuntimeError) as caught:
                backend.query(sql="SELECT 1")
            self.assertEqual("backend_rejected", caught.exception.reason)
            self.assertEqual("rejected", caught.exception.result_state)
        query_once.assert_called_once()
        sleep.assert_not_called()


class ChatStartupRecoveryTestCase(unittest.TestCase):
    def test_app_lifespan_recovers_abandoned_claims(self) -> None:
        with TemporaryDirectory(prefix="chat-lifespan-recovery-") as directory:
            path = Path(directory) / "sessions.sqlite3"
            store = ChatSessionStore(path)
            session = store.ensure_session(
                None,
                None,
                Principal(kind="installation", subject="inst_" + "l" * 40),
            )
            conversation_id = store.create_conversation(session)
            store.acquire_context(session)
            store.acquire(session, conversation_id)
            store.claim_request(
                session,
                conversation_id,
                route="chat",
                idempotency_key="req_lifespan_123456",
                fingerprint="e" * 64,
                request_id="request-lifespan-1",
            )
            app = create_chat_app(
                runner=None,
                session_store=store,
                identity_resolver=IdentityResolver(mode="anonymous"),
                enforce_loopback=False,
            )
            # The lifespan runs before a request can enter the application.
            from fastapi.testclient import TestClient

            with TestClient(app):
                replacement_context = store.acquire_context(session)
                replacement_conversation = store.acquire(session, conversation_id)
            store.release_context(session, replacement_context)
            store.release(session, conversation_id, replacement_conversation)


if __name__ == "__main__":
    unittest.main()

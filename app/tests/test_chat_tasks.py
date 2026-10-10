from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest

from fastapi.testclient import TestClient

from app.chat_api import IdentityResolver, create_chat_app
from app.chat_sessions import ChatSessionStore, ConversationNotFoundError, RequestIdempotencyConflictError
from app.chat_tasks import ChatTaskStore, STALE_RUNNING_SECONDS
from app.tests.test_chat_api import FakeChatRunner


class TaskStoreTests(unittest.TestCase):
    def test_http_success_does_not_turn_business_failure_into_completion(self):
        with TemporaryDirectory() as folder:
            store = ChatTaskStore(Path(folder) / "tasks.sqlite3")
            for status in ("blocked", "failed"):
                task, _ = store.claim("machine", "conversation", status, status)
                store.finish("machine", task["task_id"], {"status": status}, 200)
                restored = store.get("machine", task["task_id"])
                self.assertEqual("failed", restored["state"])
                self.assertEqual(status, restored["result"]["status"])

    def test_owner_fencing_idempotence_and_restart(self):
        with TemporaryDirectory() as folder:
            store = ChatTaskStore(Path(folder) / "tasks.sqlite3")
            task, claimed = store.claim("machine-a", "conversation-a", "key-one", "fingerprint-a")
            self.assertTrue(claimed)
            replay, claimed = store.claim("machine-a", "conversation-a", "key-one", "fingerprint-a")
            self.assertFalse(claimed)
            self.assertEqual(task, replay)
            with self.assertRaises(ConversationNotFoundError):
                store.get("machine-b", task["task_id"])
            with self.assertRaises(RequestIdempotencyConflictError):
                store.claim("machine-a", "conversation-a", "key-one", "different")
            store.recover()
            self.assertEqual("interrupted", store.get("machine-a", task["task_id"])["state"])
            resumed, claimed = store.claim("machine-a", "conversation-a", "key-one", "fingerprint-a")
            self.assertTrue(claimed)
            self.assertEqual(task["task_id"], resumed["task_id"])
            store.finish("machine-a", task["task_id"], {"status": "completed"}, 200)
            restored = ChatTaskStore(store.path)
            restored.recover()
            self.assertEqual("completed", restored.get("machine-a", task["task_id"])["state"])

    def test_stale_running_task_becomes_retryable(self):
        with TemporaryDirectory() as folder:
            store = ChatTaskStore(Path(folder) / "tasks.sqlite3")
            task, claimed = store.claim("machine", "conversation", "stale-key", "stale-fingerprint")
            self.assertTrue(claimed)
            store.progress("machine", task["task_id"], "query")
            with store.connection() as db:
                db.execute(
                    "UPDATE chat_tasks_v1 SET updated_at=? WHERE id=?",
                    (time.time() - STALE_RUNNING_SECONDS - 1, task["task_id"]),
                )
            self.assertEqual("interrupted", store.get("machine", task["task_id"])["state"])
            resumed, claimed = store.claim("machine", "conversation", "stale-key", "stale-fingerprint")
            self.assertTrue(claimed)
            self.assertEqual(task["task_id"], resumed["task_id"])

    def test_worker_exit_can_interrupt_task_before_writeback(self):
        with TemporaryDirectory() as folder:
            store = ChatTaskStore(Path(folder) / "tasks.sqlite3")
            task, claimed = store.claim("machine", "conversation", "worker-exit", "fingerprint")
            self.assertTrue(claimed)
            store.progress("machine", task["task_id"], "model")
            store.interrupt("machine", task["task_id"])
            interrupted = store.get("machine", task["task_id"])
            self.assertEqual("interrupted", interrupted["state"])
            self.assertEqual("interrupted", interrupted["stage"])

    def test_live_worker_is_not_reclaimed_or_duplicated_when_stage_is_old(self):
        with TemporaryDirectory() as folder:
            live = set()
            store = ChatTaskStore(Path(folder) / "tasks.sqlite3", live_task_ids=lambda: live)
            for state in ("queued", "running"):
                task, _ = store.claim("machine", "conversation", state, state)
                identifier = task["task_id"]
                live.add(identifier)
                with store.connection() as db:
                    db.execute("UPDATE chat_tasks_v1 SET state=?, updated_at=? WHERE id=?",
                               (state, time.time() - STALE_RUNNING_SECONDS - 1, identifier))
                self.assertEqual(state, store.get("machine", identifier)["state"])
                replay, claimed = store.claim("machine", "conversation", state, state)
                self.assertFalse(claimed)
                self.assertEqual(identifier, replay["task_id"])
                store.finish("machine", identifier, {"status": "completed"}, 200)
                self.assertEqual("completed", store.get("machine", identifier)["state"])
                live.discard(identifier)

    def test_expired_receipt_recovered_only_after_worker_disappears(self):
        with TemporaryDirectory() as folder:
            live = set()
            store = ChatTaskStore(Path(folder) / "tasks.sqlite3", live_task_ids=lambda: live)
            task, _ = store.claim("machine", "conversation", "worker-key", "fingerprint")
            live.add(task["task_id"])
            with store.connection() as db:
                db.execute("UPDATE chat_tasks_v1 SET updated_at=? WHERE id=?",
                           (time.time() - STALE_RUNNING_SECONDS - 1, task["task_id"]))
            self.assertEqual(0, store.recover_stale())
            live.clear()
            self.assertEqual(1, store.recover_stale())
            self.assertEqual("interrupted", store.get("machine", task["task_id"])["state"])

    def test_detached_request_survives_polling_and_runs_only_once(self):
        class SlowRunner(FakeChatRunner):
            def run(self, question):
                started.set()
                release.wait(5)
                return super().run(question)

        started, release = threading.Event(), threading.Event()
        with TemporaryDirectory() as folder:
            runner = SlowRunner()
            app = create_chat_app(runner, session_store=ChatSessionStore(Path(folder) / "sessions.sqlite3"), identity_resolver=IdentityResolver(mode="anonymous"), enforce_loopback=False)
            with TestClient(app) as client:
                session = client.get("/v1/session").json()
                headers = {"X-CSRF-Token": session["csrf_token"], "Idempotency-Key": "req_detached_test"}
                body = {"question": "查询库存"}
                accepted = client.post("/v1/tasks", json=body, headers=headers)
                self.assertEqual(202, accepted.status_code)
                task_id = accepted.json()["task_id"]
                self.assertTrue(started.wait(2))
                # A live request may wait without a new stage for a long time.
                # The API's worker registry must protect both polling and replay.
                with app.state.task_store.connection() as db:
                    db.execute(
                        "UPDATE chat_tasks_v1 SET updated_at=? WHERE id=?",
                        (time.time() - STALE_RUNNING_SECONDS - 1, task_id),
                    )
                self.assertEqual("running", client.get(f"/v1/tasks/{task_id}").json()["state"])
                replay = client.post("/v1/tasks", json=body, headers=headers).json()
                self.assertEqual(task_id, replay["task_id"])
                self.assertEqual("running", replay["state"])
                release.set()
                for _ in range(100):
                    terminal = client.get(f"/v1/tasks/{task_id}").json()
                    if terminal["state"] in {"completed", "failed"}:
                        break
                    time.sleep(.02)
                self.assertEqual("completed", terminal["state"])
                self.assertIn("result", terminal)
                self.assertEqual(["查询库存"], runner.questions)
                client.cookies.clear()
                client.get("/v1/session")
                self.assertEqual(404, client.get(f"/v1/tasks/{task_id}").status_code)

    def test_cancelled_request_is_fenced_against_a_late_worker_result(self):
        class SlowRunner(FakeChatRunner):
            def run(self, question):
                started.set()
                release.wait(5)
                return super().run(question)

        started, release = threading.Event(), threading.Event()
        with TemporaryDirectory() as folder:
            runner = SlowRunner()
            app = create_chat_app(
                runner,
                session_store=ChatSessionStore(Path(folder) / "sessions.sqlite3"),
                identity_resolver=IdentityResolver(mode="anonymous"),
                enforce_loopback=False,
            )
            with TestClient(app) as client:
                session = client.get("/v1/session").json()
                headers = {"X-CSRF-Token": session["csrf_token"], "Idempotency-Key": "cancel-task-test"}
                accepted = client.post("/v1/tasks", json={"question": "查询库存"}, headers=headers)
                self.assertEqual(202, accepted.status_code)
                task_id = accepted.json()["task_id"]
                self.assertTrue(started.wait(2))

                cancelled = client.post(
                    f"/v1/tasks/{task_id}/cancel",
                    headers={"X-CSRF-Token": session["csrf_token"]},
                )
                self.assertEqual(200, cancelled.status_code)
                self.assertEqual("cancelled", cancelled.json()["state"])
                self.assertEqual("query_cancelled", cancelled.json()["result"]["error"]["code"])

                release.set()
                time.sleep(.1)
                terminal = client.get(f"/v1/tasks/{task_id}").json()
                self.assertEqual("cancelled", terminal["state"])
                self.assertEqual("query_cancelled", terminal["result"]["error"]["code"])
                # The fake worker is deliberately released to prove that a
                # late result cannot overwrite the cancelled durable receipt.
                self.assertEqual(["查询库存"], runner.questions)

    def test_stuck_request_reaches_timeout_terminal_state(self):
        class StuckRunner(FakeChatRunner):
            def run(self, question):
                time.sleep(5)
                return super().run(question)

        with TemporaryDirectory() as folder:
            runner = StuckRunner()
            app = create_chat_app(
                runner,
                session_store=ChatSessionStore(Path(folder) / "sessions.sqlite3"),
                identity_resolver=IdentityResolver(mode="anonymous"),
                enforce_loopback=False,
                task_timeout_seconds=1,
            )
            with TestClient(app) as client:
                session = client.get("/v1/session").json()
                headers = {"X-CSRF-Token": session["csrf_token"], "Idempotency-Key": "req_timeout_test"}
                accepted = client.post("/v1/tasks", json={"question": "查询库存"}, headers=headers)
                self.assertEqual(202, accepted.status_code)
                task_id = accepted.json()["task_id"]
                deadline = time.time() + 3
                state = None
                while time.time() < deadline:
                    state = client.get(f"/v1/tasks/{task_id}").json()
                    if state["state"] not in {"queued", "running"}:
                        break
                    time.sleep(0.1)
                self.assertIsNotNone(state)
                self.assertEqual("failed", state["state"])
                self.assertEqual("chat_timeout", state["result"]["error"]["code"])

    def test_timeout_keeps_context_owned_until_thread_exits(self):
        started, release = threading.Event(), threading.Event()
        entered = []

        class BlockedRunner(FakeChatRunner):
            def run(self, question):
                entered.append(question)
                if question == "first request":
                    started.set()
                    if not release.wait(10):
                        raise AssertionError("test worker was not released")
                return super().run(question)

        def terminal(client, identifier):
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                result = client.get(f"/v1/tasks/{identifier}").json()
                if result["state"] not in {"queued", "running"}:
                    return result
                time.sleep(0.02)
            self.fail("task did not reach a terminal state")

        with TemporaryDirectory() as folder:
            app = create_chat_app(
                BlockedRunner(),
                session_store=ChatSessionStore(Path(folder) / "sessions.sqlite3"),
                identity_resolver=IdentityResolver(mode="anonymous"),
                enforce_loopback=False,
                task_timeout_seconds=1,
            )
            with TestClient(app) as client:
                try:
                    session = client.get("/v1/session").json()
                    headers = {"X-CSRF-Token": session["csrf_token"], "Idempotency-Key": "owned-first"}
                    body = {"question": "first request"}
                    first = client.post("/v1/tasks", json=body, headers=headers).json()
                    self.assertTrue(started.wait(2))
                    failed = terminal(client, first["task_id"])
                    self.assertEqual("chat_timeout", failed["result"]["error"]["code"])
                    self.assertEqual(failed, client.post("/v1/tasks", json=body, headers=headers).json())

                    # The direct endpoint must respect the same still-owned
                    # context, even though its detached receipt is terminal.
                    blocked = client.post("/v1/chat", json={"question": "direct retry"},
                                          headers={**headers, "Idempotency-Key": "owned-direct"})
                    self.assertEqual(409, blocked.status_code)
                    self.assertNotIn("direct retry", entered)

                    own_cookies = dict(client.cookies)
                    client.cookies.clear()
                    other = client.get("/v1/session").json()
                    other_task = client.post("/v1/tasks", json={"question": "other owner"}, headers={
                        "X-CSRF-Token": other["csrf_token"], "Idempotency-Key": "owned-other",
                    }).json()
                    self.assertEqual("completed", terminal(client, other_task["task_id"])["state"])
                    client.cookies.clear()
                    client.cookies.update(own_cookies)

                    second = client.post("/v1/tasks", json={"question": "next request"},
                                         headers={**headers, "Idempotency-Key": "owned-next"}).json()
                    time.sleep(0.05)
                    self.assertNotIn("next request", entered)
                    release.set()
                    self.assertEqual("completed", terminal(client, second["task_id"])["state"])
                    # A late successful return cannot overwrite the timeout.
                    self.assertEqual(failed, client.get(f"/v1/tasks/{first['task_id']}").json())
                    self.assertEqual(["first request", "other owner", "next request"], entered)
                finally:
                    release.set()


if __name__ == "__main__":
    unittest.main()

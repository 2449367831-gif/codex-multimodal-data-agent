from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.chat_sessions import (
    ChatSessionStore,
    Principal,
    RequestIdempotencyConflictError,
)


class ChatRequestReceiptTestCase(unittest.TestCase):
    def test_restart_reclaims_transient_claims_without_creating_a_new_conversation(self) -> None:
        with TemporaryDirectory(prefix="chat-recovery-") as directory:
            path = Path(directory) / "sessions.sqlite3"
            first_store = ChatSessionStore(path)
            principal = Principal(kind="installation", subject="inst_" + "r" * 40)
            session = first_store.ensure_session(None, None, principal)
            conversation_id = first_store.create_conversation(session)
            context_lease = first_store.acquire_context(session)
            conversation_lease = first_store.acquire(session, conversation_id)
            receipt = first_store.claim_request(
                session,
                conversation_id,
                route="chat",
                idempotency_key="req_recovery_123456",
                fingerprint="d" * 64,
                request_id="request-recovery-1",
            )

            second_store = ChatSessionStore(path)
            recovered = second_store.recover_after_restart()
            self.assertEqual(1, recovered["running_receipts"])
            self.assertEqual(1, recovered["conversation_leases"])
            self.assertEqual(1, recovered["context_leases"])

            replacement_context_lease = second_store.acquire_context(session)
            replacement_conversation_lease = second_store.acquire(session, conversation_id)
            replacement = second_store.claim_request(
                session,
                conversation_id,
                route="chat",
                idempotency_key="req_recovery_123456",
                fingerprint="d" * 64,
                request_id="request-recovery-2",
            )
            self.assertTrue(replacement.owner)
            self.assertEqual("request-recovery-2", replacement.receipt.request_id)
            self.assertNotEqual(context_lease, replacement_context_lease)
            self.assertNotEqual(conversation_lease, replacement_conversation_lease)
            self.assertEqual(conversation_id, second_store.find_default_conversation(session))
            # The old receipt is fenced by its request ID and cannot complete the
            # replacement claim after the restart.
            first_store.complete_request(
                session,
                receipt.receipt,
                {"status": "completed"},
            )
            replay = second_store.claim_request(
                session,
                conversation_id,
                route="chat",
                idempotency_key="req_recovery_123456",
                fingerprint="d" * 64,
                request_id="request-recovery-3",
            )
            self.assertFalse(replay.owner)
            self.assertEqual("request-recovery-2", replay.receipt.request_id)

    def test_running_claim_is_shared_and_completed_claim_is_replayable(self) -> None:
        with TemporaryDirectory(prefix="chat-receipts-") as directory:
            store = ChatSessionStore(Path(directory) / "sessions.sqlite3")
            session = store.ensure_session(
                None,
                None,
                Principal(kind="installation", subject="inst_" + "a" * 40),
            )
            conversation_id = store.create_conversation(session)
            fingerprint = "a" * 64

            first = store.claim_request(
                session,
                conversation_id,
                route="chat",
                idempotency_key="req_receipt_123456",
                fingerprint=fingerprint,
                request_id="request-1234567890",
            )
            second = store.claim_request(
                session,
                conversation_id,
                route="chat",
                idempotency_key="req_receipt_123456",
                fingerprint=fingerprint,
                request_id="request-0987654321",
            )

            self.assertTrue(first.owner)
            self.assertFalse(second.owner)
            self.assertEqual("running", second.receipt.state)

            store.complete_request(
                session,
                first.receipt,
                {"request_id": first.receipt.request_id, "status": "completed"},
            )
            replay = store.claim_request(
                session,
                conversation_id,
                route="chat",
                idempotency_key="req_receipt_123456",
                fingerprint=fingerprint,
                request_id="request-1111111111",
            )

            self.assertFalse(replay.owner)
            self.assertEqual("completed", replay.receipt.state)
            self.assertEqual(
                {"request_id": first.receipt.request_id, "status": "completed"},
                replay.receipt.public_payload(),
            )

    def test_same_key_with_a_different_fingerprint_is_rejected(self) -> None:
        with TemporaryDirectory(prefix="chat-receipts-conflict-") as directory:
            store = ChatSessionStore(Path(directory) / "sessions.sqlite3")
            session = store.ensure_session(
                None,
                None,
                Principal(kind="installation", subject="inst_" + "b" * 40),
            )
            conversation_id = store.create_conversation(session)
            store.claim_request(
                session,
                conversation_id,
                route="chat",
                idempotency_key="req_conflict_123456",
                fingerprint="b" * 64,
                request_id="request-2222222222",
            )

            with self.assertRaises(RequestIdempotencyConflictError):
                store.claim_request(
                    session,
                    conversation_id,
                    route="chat",
                    idempotency_key="req_conflict_123456",
                    fingerprint="c" * 64,
                    request_id="request-3333333333",
                )


if __name__ == "__main__":
    unittest.main()

"""Opt-in real-model ASGI workflow on synthetic contracts; no Doris access."""

from copy import deepcopy
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient
from app.chat_api import create_chat_app, IdentityResolver
from app.chat_sessions import ChatSessionStore
from app.codex_agent import project_codex_home
from app.interpretation_runner import InterpretationRunner
from app.private_worker_protocol import WORKER_ERROR_CODES, VALIDATION_ERROR_CODES
from app.knowledge_proposals import snapshot_digest
from app.tests.test_knowledge_proposals import baseline


def main():
    snapshot = baseline()
    before = snapshot_digest(snapshot)
    query = Mock(backend_url="http://127.0.0.1:9")
    query.run.side_effect = AssertionError("no_query")
    semantic = Mock()
    semantic.run_if_supported.side_effect = AssertionError("no_query")
    worker_errors = []
    class ObservedRunner(InterpretationRunner):
        def run(self, *args, **kwargs):
            try:
                return super().run(*args, **kwargs)
            except Exception as exc:
                allowed = WORKER_ERROR_CODES | {"interpretation_timeout", "interpretation_cancelled",
                                               "interpretation_worker_invalid_response"}
                worker_errors.append({"code": str(exc) if str(exc) in allowed else "unclassified_worker_failure",
                                      "validation": getattr(exc, "validation_code", None)
                                      if getattr(exc, "validation_code", None) in VALIDATION_ERROR_CODES else None})
                raise
    with TemporaryDirectory(prefix="interpretation-api-model-") as directory:
        root = Path(directory)
        sessions = ChatSessionStore(root / "sessions.sqlite3")
        runner = ObservedRunner(project_root=root, codex_home=project_codex_home(ROOT), timeout_seconds=120)
        app = create_chat_app(query, session_store=sessions, semantic_runtime=semantic,
                              proposal_baseline_loader=lambda: deepcopy(snapshot), interpretation_runner=runner,
                              identity_resolver=IdentityResolver(mode="anonymous"), enforce_loopback=False)
        stage = "bootstrap"
        try:
            with TestClient(app) as client:
                bootstrap = client.get("/v1/session").json()
                headers = {"X-CSRF-Token": bootstrap["csrf_token"]}
                conversation = client.post("/v1/conversations", headers=headers).json()["conversation_id"]
                binding = {"conversation_id": conversation, "display_session_id": "synthetic-display", "request_id": "synthetic-request"}
                question = "将 inventory.available 的名称改为库存新名称；计量单位请让我选择基本数量或箱数。"

                def post(url, body):
                    response = client.post(url, headers=headers, json=body)
                    if response.status_code != 200: raise ValueError("unexpected_http_status")
                    return response.json()

                def wait(opened):
                    deadline = time.monotonic() + 135
                    while time.monotonic() < deadline:
                        result = post("/v1/knowledge-interpretations/" + opened["interpretation_id"] + "/resume", binding)
                        if result["state"] != "running": return result
                        time.sleep(0.2)
                    raise ValueError("observation_timeout")

                stage = "initial_interpretation"
                opened = post("/v1/knowledge-interpretations", {**binding, "question": question})
                pending = wait(opened)
                if pending["state"] != "needs_confirmation" or len(pending["questions"]) != 1:
                    raise ValueError("unit_confirmation_missing")
                decision = pending["questions"][0]
                options = [option for option in decision["options"] if "基本数量" in option["label"] or option["label"] == "EA"]
                if len(options) != 1: raise ValueError("unit_choice_not_unique")
                selection = {**binding, "revision": pending["revision"], "choices": {decision["id"]: options[0]["id"]}}
                stage = "confirmed_interpretation"
                url = "/v1/knowledge-interpretations/" + opened["interpretation_id"]
                post(url + "/select", selection)
                ready = wait(opened)
                if ready["state"] != "review_ready": raise ValueError("review_not_ready")
                replay = post(url + "/select", selection)
                stage = "proposal_review"
                proposal_url = "/v1/knowledge-proposals/" + ready["proposal"]["proposal_id"]
                review = post(proposal_url + "/resume", binding)
                selected = post(proposal_url + "/select", {
                    **binding, "revision": review["revision"],
                    "choices": {item["id"]: "proposed" for item in review["assessment"]["questions"]},
                })
                with sessions._connection() as connection:
                    contexts = connection.execute("SELECT codex_thread_id,turn_count FROM chat_principal_contexts_v1").fetchall()
                public = json.dumps([pending, ready, review, selected])
                checks = {
                    "single_selectable_unit_question": len(pending["questions"]) == 1,
                    "no_repeated_question_after_selection": ready["questions"] == [],
                    "one_canonical_thread_two_turns": len(contexts) == 1 and bool(contexts[0][0]) and contexts[0][1] == 2,
                    "selection_replay_no_new_model_call": replay == ready,
                    "requested_name_only": len(review["assessment"]["comparisons"]) == 1
                        and review["assessment"]["comparisons"][0]["field"] == "指标名称"
                        and review["assessment"]["comparisons"][0]["after"] == "库存新名称",
                    "not_published": selected["assessment"]["publication_authorized"] is False,
                    "baseline_unchanged": snapshot_digest(snapshot) == before,
                    "no_query": query.run.call_count == 0 and semantic.run_if_supported.call_count == 0,
                    "private_identifiers_not_exposed": contexts[0][0] not in public
                        and all(key not in public for key in ("baseline_sha256", "inventory.available", "warehouse.stock")),
                }
                print(json.dumps({"checks": checks, "business_query": False}))
                return 0 if all(checks.values()) else 1
        except Exception as exc:
            known = {"unexpected_http_status", "observation_timeout", "unit_confirmation_missing",
                     "unit_choice_not_unique", "review_not_ready"}
            print(json.dumps({"state": "not_verified", "stage": stage, "business_query": False,
                              "reason": str(exc) if str(exc) in known else "workflow_failed",
                              "worker_errors": worker_errors}))
            return 1


if __name__ == "__main__":
    raise SystemExit(main())

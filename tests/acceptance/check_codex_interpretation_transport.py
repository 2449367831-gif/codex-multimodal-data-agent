"""Opt-in real Codex protocol check against a loopback-only synthetic model."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app import codex_interpretation as adapter
from app.codex_interpretation import InterpretationRunError, run_codex_interpretation
from app.tests.test_knowledge_proposals import baseline
from app.tests.test_proposal_interpretation import QUESTION, draft
from app.tests.test_proposal_wire import wire_draft


def main(*, worker=False):
    snapshot = baseline()
    answer = json.dumps(wire_draft(draft(snapshot)), ensure_ascii=False)
    received = []
    config_mismatches = []
    verify = adapter._verify_configuration

    def observe_config(actual, expected):
        def compare(left, right, path=""):
            if isinstance(right, dict):
                if not isinstance(left, dict) or (not right and left):
                    config_mismatches.append(path)
                    return
                for key, value in right.items():
                    compare(left.get(key), value, path + "/" + key)
            elif type(left) is not type(right) or left != right:
                config_mismatches.append(path)
        compare(actual, expected)
        verify(actual, expected)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            received.append({"model": request.get("model"), "reasoning": request.get("reasoning"),
                             "structured": (request.get("text", {}).get("format") or {}).get("type") == "json_schema",
                             "tools": [(tool.get("type"), tool.get("name")) for tool in request.get("tools", [])]})
            message = {"id": "msg_synthetic", "type": "message", "role": "assistant", "status": "completed",
                       "content": [{"type": "output_text", "text": answer, "annotations": []}]}
            response = {"id": "resp_synthetic", "object": "response", "created_at": 1, "status": "completed",
                        "model": request.get("model"), "output": [message],
                        "usage": {"input_tokens": 100, "output_tokens": 100, "total_tokens": 200}}
            events = [
                {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                {"type": "response.output_item.added", "output_index": 0, "item": {**message, "status": "in_progress", "content": []}},
                {"type": "response.output_text.delta", "item_id": message["id"], "output_index": 0, "content_index": 0, "delta": answer},
                {"type": "response.output_item.done", "output_index": 0, "item": message},
                {"type": "response.completed", "response": response},
            ]
            body = "".join("data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        serving = Thread(target=server.serve_forever, daemon=True)
        serving.start()
        try:
            with TemporaryDirectory(prefix="codex-interpretation-protocol-") as directory:
                root = Path(directory)
                source = root / "var/codex-home"
                source.mkdir(parents=True)
                # Synthetic protocol credential, never a company/provider secret.
                (source / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "synthetic-local-test"}), encoding="utf-8")
                kwargs = {"project_root": root, "codex_home": source,
                          "context_home": root / "var/codex-contexts" / ("a" * 64),
                          "model_base_url": f"http://127.0.0.1:{server.server_port}/v1"}
                kwargs["context_home"].mkdir(parents=True)
                (kwargs["context_home"] / "config.toml").write_text(
                    '[features]\nshell_tool=true\nplugins=true\n'
                    '[mcp_servers.unexpected]\ncommand="never-run-interpretation-test"\n',
                    encoding="utf-8",
                )
                if worker:
                    from app.interpretation_runner import InterpretationRunner
                    runner = InterpretationRunner(project_root=root, codex_home=source,
                                                  model_base_url=kwargs["model_base_url"], timeout_seconds=45)
                    identities = []
                    first = runner.run(QUESTION, snapshot, context_home=kwargs["context_home"],
                                       on_context_ready=identities.append)
                    # No supplied ID: recover the same conversation from its
                    # durable identity checkpoint across independent workers.
                    second = runner.run(QUESTION, snapshot, context_home=kwargs["context_home"],
                                        on_context_ready=identities.append)
                else:
                    with patch.object(adapter, "_verify_configuration", observe_config):
                        first = run_codex_interpretation(QUESTION, snapshot, **kwargs)
                        second = run_codex_interpretation(QUESTION, snapshot, thread_id=first["thread_id"], **kwargs)
                checks = {
                    "same_canonical_thread": first["thread_id"] == second["thread_id"],
                    "resumed": second["context_reused"] and not second["context_reset"],
                    "two_model_turns": len(received) == 2,
                    "no_query": not first["query_executed"] and not second["query_executed"],
                    "private_candidates": all(r["result_state"] == "definition_interpretation" for r in (first, second)),
                     "model_preserved": all(r["model"] == "gpt-6" for r in received),
                    "effort_preserved": all((r["reasoning"] or {}).get("effort") == "xhigh" for r in received),
                    "structured_output": all(r["structured"] for r in received),
                    "no_advertised_tools": all(not r["tools"] for r in received),
                }
                if worker:
                    checks["identity_callback"] = bool(identities) and set(identities) == {first["thread_id"]}
                    checks["no_private_result_files"] = not (root / "var/chat-internal-results").exists()
                print(json.dumps({"checks": checks, "tool_types": received[0]["tools"] if received else []}))
                return 0 if all(checks.values()) else 1
        except InterpretationRunError as exc:
            print(json.dumps({"error": str(exc), "model_requests": len(received), "config_mismatches": config_mismatches,
                              "synthetic_runtime_error": str(exc.__context__)[:500]}))
            return 1
        finally:
            server.shutdown()
            serving.join(timeout=5)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    raise SystemExit(main(worker=parser.parse_args().worker))

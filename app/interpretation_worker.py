"""One-shot private interpretation worker. No public endpoint or result files."""

import argparse
from contextlib import redirect_stdout, redirect_stderr
import os
from pathlib import Path
import re
import sys

from .codex_interpretation import InterpretationRunError, run_codex_interpretation
from .knowledge_changes import _encoded
from .knowledge_proposals import _snapshot
from .private_worker_protocol import MAX_INPUT_BYTES, MAX_OUTPUT_BYTES, WORKER_ERROR_CODES, VALIDATION_ERROR_CODES, decode_private, encode_private
from .worker_lifetime import retain_windows_worker_job


def interpret_request(request, *, project_root):
    if not isinstance(request, dict) or set(request) != {
        "version", "question", "contracts", "context_ref", "thread_id", "codex_home", "settings", "confirmation",
    } or type(request["version"]) is not int or request["version"] != 1:
        raise ValueError("invalid_worker_request")
    context_ref = request["context_ref"]
    if not isinstance(context_ref, str) or not re.fullmatch(r"[a-f0-9]{16,64}", context_ref):
        raise ValueError("invalid_worker_context")
    settings = request["settings"]
    if not isinstance(settings, dict) or set(settings) != {"model", "reasoning_effort", "model_provider", "base_url"}:
        raise ValueError("invalid_worker_settings")
    if not all(isinstance(value, str) and value for value in settings.values()):
        raise ValueError("invalid_worker_settings")
    if not isinstance(request["codex_home"], str) or not Path(request["codex_home"]).is_absolute():
        raise ValueError("invalid_worker_home")
    contracts = request["contracts"]
    if not isinstance(contracts, dict):
        raise ValueError("invalid_worker_contracts")
    snapshot = _snapshot(contracts)
    if _encoded(snapshot.contracts) != _encoded(contracts):
        raise ValueError("invalid_worker_contracts")
    return run_codex_interpretation(
        request["question"], snapshot, context_home=project_root / "var/codex-contexts" / context_ref,
        thread_id=request["thread_id"], project_root=project_root, codex_home=request["codex_home"],
        model=settings["model"], reasoning_effort=settings["reasoning_effort"],
        model_provider=settings["model_provider"], model_base_url=settings["base_url"],
        confirmation=request["confirmation"],
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", required=True)
    args = parser.parse_args()
    # pythonw has no default Python stdio objects, even when native pipes exist.
    source = os.fdopen(os.dup(0), "rb")
    sink = os.fdopen(os.dup(1), "wb")
    response = {"ok": False, "error": "interpretation_worker_failed"}
    try:
        retain_windows_worker_job()
        request = decode_private(source.read(MAX_INPUT_BYTES + 1), limit=MAX_INPUT_BYTES)
        # SDK/debug prints must never contaminate the private result channel.
        with open(os.devnull, "w", encoding="utf-8") as discard, redirect_stdout(discard), redirect_stderr(discard):
            result = interpret_request(request, project_root=Path(args.project_root).resolve())
        response = {"ok": True, "result": result}
    except InterpretationRunError as exc:
        response["error"] = str(exc) if str(exc) in WORKER_ERROR_CODES else "interpretation_worker_failed"
        if exc.validation_code in VALIDATION_ERROR_CODES:
            response["validation_code"] = exc.validation_code
    except Exception:
        pass
    try:
        raw = encode_private(response, limit=MAX_OUTPUT_BYTES)
    except ValueError:
        raw = encode_private({"ok": False, "error": "interpretation_output_too_large"}, limit=MAX_OUTPUT_BYTES)
    with source, sink:
        sink.write(raw)
        sink.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

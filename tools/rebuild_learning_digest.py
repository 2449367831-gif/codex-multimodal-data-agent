"""Rebuild local query-learning digests without printing private examples."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.learning import LearningLedger  # noqa: E402


DEFAULT_LEARNING_DB_PATH = ROOT / "var" / "chat-sessions.sqlite3"


def _db_path(value: str | None) -> Path:
    raw = value or os.getenv("AGENT_LEARNING_DB_PATH") or str(DEFAULT_LEARNING_DB_PATH)
    return Path(raw).expanduser().resolve()


def _summary(payload: dict[str, Any]) -> str:
    counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
    return (
        f"questions={int(counts.get('questions', 0))} "
        f"completed={int(counts.get('completed', 0))} "
        f"failed_or_blocked={int(counts.get('failed_or_blocked', 0))} "
        f"clarification_required={int(counts.get('clarification_required', 0))} "
        f"pending_candidates={int(counts.get('pending_candidates', 0))}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild protected per-installation learning digests."
    )
    parser.add_argument("--db", help="Learning/session SQLite path")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--principal-hash", help="Rebuild one owner digest")
    group.add_argument("--all", action="store_true", help="Rebuild every owner digest")
    args = parser.parse_args(argv)

    ledger = LearningLedger(_db_path(args.db))
    if args.all:
        payloads = ledger.rebuild_all()
        print(f"Rebuilt {len(payloads)} private learning digest(s).")
        for payload in payloads:
            print(_summary(payload))
        return 0

    payload = ledger.build_digest(args.principal_hash)
    print(f"Rebuilt one private learning digest: {_summary(payload)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

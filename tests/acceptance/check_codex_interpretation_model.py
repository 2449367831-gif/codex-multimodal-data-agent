"""Opt-in configured-model check using synthetic contracts, never business rows."""

import json
from pathlib import Path
import sys
import re
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.codex_agent import project_codex_home
from app.codex_interpretation import InterpretationRunError, run_codex_interpretation
from app.knowledge_proposals import snapshot_digest
from app.proposal_interpretation import InterpretationError
from app import proposal_interpretation
from app.tests.test_knowledge_proposals import baseline


def main(*, worker=False):
    snapshot = baseline()
    before = snapshot_digest(snapshot)
    shape_errors = []
    check_fields = proposal_interpretation._fields

    def observe_fields(value, fields):
        if not isinstance(value, dict) or set(value) != set(fields):
            shape_errors.append({"expected": sorted(fields), "actual": [
                key if re.fullmatch(r"[A-Za-z_]{1,40}", key) else "non_schema_field"
                for key in value] if isinstance(value, dict) else type(value).__name__})
        return check_fields(value, fields)
    question = "将 inventory.available 的 display_name 改为可用库存新名称；计量单位仍保持 EA。"
    with TemporaryDirectory(prefix="codex-interpretation-model-") as directory:
        root = Path(directory)
        if worker:
            from app.interpretation_runner import InterpretationRunner
            runner = InterpretationRunner(project_root=root, codex_home=project_codex_home(ROOT), timeout_seconds=120)
            def invoke(question, snapshot, **kwargs):
                return runner.run(question, snapshot, context_home=kwargs["context_home"])
        else:
            invoke = run_codex_interpretation
        try:
            with patch.object(proposal_interpretation, "_fields", observe_fields):
                result = invoke(
                    question, snapshot, project_root=root, codex_home=project_codex_home(ROOT),
                    context_home=root / "var/codex-contexts" / ("b" * 64),
                )
                unchanged = invoke(
                    "保持 inventory.available 现有的 display_name 和 unit，不采用上一轮候选变更。",
                    snapshot, project_root=root, codex_home=project_codex_home(ROOT),
                    context_home=root / "var/codex-contexts" / ("b" * 64), thread_id=result["thread_id"],
                )
                unresolved = invoke(
                    "库存计量单位暂未确定，库存时点也暂未确定。请一次列出这两个需要确认的选项，不要修改合同。",
                    snapshot, project_root=root, codex_home=project_codex_home(ROOT),
                    context_home=root / "var/codex-contexts" / ("b" * 64), thread_id=result["thread_id"],
                )
        except InterpretationRunError as exc:
            print(json.dumps({"error": str(exc), "business_query": False,
                              "validation_code": str(exc.__context__) if isinstance(exc.__context__, InterpretationError) else None,
                              "shape_errors": shape_errors}))
            return 1
        interpretation = result["interpretation"]
        proposal = interpretation.get("proposal") or {}
        changes = proposal.get("changes", [])
        checks = {
            "requested_change_only": changes == [{"kind": "metric", "id": "inventory.available",
                                                   "set": {"display_name": "可用库存新名称"}, "remove": []}],
            "unchanged_requirement_retained": any(segment["role"] == "unchanged" and
                any(target.get("id") == "inventory.available" and target.get("field") == "unit"
                    for target in segment["targets"]) for segment in interpretation["segments"]),
            "ready_for_review": interpretation["review_ready"],
            "not_executed_or_published": not interpretation["execution_authorized"] and not interpretation["publication_authorized"],
            "baseline_unchanged": before == snapshot_digest(snapshot),
             "model_preserved": result["model"] == "gpt-6" and result["reasoning_effort"] == "low",
            "no_query": not result["query_executed"],
            "same_thread_across_three_turns": result["thread_id"] == unchanged["thread_id"] == unresolved["thread_id"],
            "prior_candidate_not_inherited": unchanged["interpretation"]["state"] == "interpretation_no_change"
                and unchanged["interpretation"]["proposal"] is None,
            "all_unknowns_together": unresolved["interpretation"]["state"] == "interpretation_needs_confirmation"
                and len(unresolved["interpretation"]["questions"]) >= 2 and unresolved["interpretation"]["proposal"] is None,
            "followups_not_executed_or_published": all(not r["query_executed"] and
                not r["interpretation"]["publication_authorized"] for r in (unchanged, unresolved)),
        }
        print(json.dumps({"checks": checks, "business_query": False}))
        return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    raise SystemExit(main(worker=parser.parse_args().worker))

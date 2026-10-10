"""Compare the trusted active release with selected candidate contracts, read-only."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.errors import RuntimeErrorBase
from app.knowledge import KnowledgeBase
from app.knowledge_activation import active_snapshot
from app.knowledge_changes import KnowledgeSnapshot, assess_changes
from app.knowledge_proposals import assess_proposal, decode_proposal, resolve_proposal, snapshot_digest
from app.sql import TemplateCompiler


def capture(knowledge, *, trusted=False):
    references = knowledge.selected_template_refs()
    compiler = TemplateCompiler(knowledge.project_root, reload_on_change=False,
                                preload_template_refs=references)
    digests = compiler.cached_file_digests()
    if trusted:
        knowledge.verify_runtime_snapshot_integrity(digests)
    return KnowledgeSnapshot(metrics=knowledge.metrics(), sources=knowledge.sources(),
                             templates={reference: digests[reference] for reference in references})


def load_baseline(root):
    snapshot, digest = active_snapshot(root)
    if digest is None:
        raise ValueError("activated_baseline_required")
    baseline = KnowledgeBase(snapshot, release_id=snapshot.name, reload_on_change=False,
                             require_trusted_release=True, trusted_release_sha256=digest)
    return baseline


def assess_project(root, *, candidate_release="candidate-0.1.0", include_values=False):
    baseline = load_baseline(root)
    candidate = KnowledgeBase(root, release_id=candidate_release, reload_on_change=False)
    old = capture(baseline, trusted=True)
    report = assess_changes(old, capture(candidate), include_values=include_values)
    report["baseline_sha256"] = snapshot_digest(old)
    report["baseline_release"] = baseline.planning_manifest()["release_id"]
    report["candidate_release"] = candidate.planning_manifest()["release_id"]
    report["limitations"].append("Candidate files may change after capture; repeat assessment against the final immutable release before publication.")
    return report


def assess_active_proposal(root, request, *, answers=None, include_values=False):
    baseline = load_baseline(root)
    old = capture(baseline, trusted=True)
    report = (assess_proposal(old, request, include_values=include_values) if answers is None
              else resolve_proposal(old, request, answers, include_values=include_values))
    report["baseline_release"] = baseline.planning_manifest()["release_id"]
    return report


def main(argv=None, *, stdout=None, stdin=None, root=ROOT):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--candidate-release", default="candidate-0.1.0")
    mode.add_argument("--proposal-stdin", action="store_true", help="Assess only the request-local changes from JSON stdin")
    mode.add_argument("--resolve-stdin", action="store_true", help="Resolve a JSON envelope with proposal and answers; never publish")
    parser.add_argument("--include-values", action="store_true", help="Internal engineering output only; include contract values, never product UI")
    args = parser.parse_args(argv)
    stdout = stdout or sys.stdout
    try:
        if args.proposal_stdin or args.resolve_stdin:
            value = decode_proposal((stdin or sys.stdin).read(200001))
            if args.resolve_stdin:
                if not isinstance(value, dict) or set(value) != {"proposal", "answers"} or not isinstance(value["answers"], dict):
                    raise ValueError("invalid_resolution_envelope")
                report = assess_active_proposal(root, value["proposal"], answers=value["answers"], include_values=args.include_values)
            else:
                report = assess_active_proposal(root, value, include_values=args.include_values)
        else:
            report = assess_project(root, candidate_release=args.candidate_release, include_values=args.include_values)
        print(json.dumps(report, ensure_ascii=True, sort_keys=True), file=stdout)
        return 0 if report["assessment_complete"] else 2
    except (RuntimeErrorBase, ValueError, TypeError, KeyError, OSError, RecursionError):
        print(json.dumps({"state": "assessment_rejected", "code": "invalid_knowledge_snapshot"}), file=stdout)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

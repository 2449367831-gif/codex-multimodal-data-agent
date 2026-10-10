import io
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from tools import assess_knowledge_changes as tool


def test_capture_uses_selected_templates_and_verifies_trusted_runtime_bytes(monkeypatch):
    knowledge = Mock(project_root=Path("synthetic"))
    knowledge.selected_template_refs.return_value = ("sql_templates/synthetic.sql",)
    knowledge.metrics.return_value = []
    knowledge.sources.return_value = []
    compiler = Mock()
    compiler.cached_file_digests.return_value = {"sql_templates/synthetic.sql": "a" * 64, "unselected.sql": "b" * 64}
    factory = Mock(return_value=compiler)
    monkeypatch.setattr(tool, "TemplateCompiler", factory)
    snapshot = tool.capture(knowledge, trusted=True)
    assert list(snapshot.contracts) == ["template:sql_templates/synthetic.sql"]
    factory.assert_called_once_with(knowledge.project_root, reload_on_change=False, preload_template_refs=("sql_templates/synthetic.sql",))
    knowledge.verify_runtime_snapshot_integrity.assert_called_once_with(compiler.cached_file_digests.return_value)


def test_active_anchor_and_explicit_candidate_cannot_be_overridden_by_environment(monkeypatch):
    root = Path("synthetic")
    baseline = root / "release-one"
    monkeypatch.setattr(tool, "active_snapshot", lambda _: (baseline, "a" * 64))
    snapshots = []
    def load(path, **kwargs):
        value = Mock(project_root=path)
        value.selected_template_refs.return_value = ()
        value.metrics.return_value = []
        value.sources.return_value = []
        value.planning_manifest.return_value = {"release_id": kwargs["release_id"]}
        snapshots.append((path, kwargs, value))
        return value
    monkeypatch.setattr(tool, "KnowledgeBase", load)
    monkeypatch.setattr(tool, "TemplateCompiler", lambda *a, **k: Mock(cached_file_digests=lambda: {}))
    monkeypatch.setenv("AGENT_RELEASE_ID", "wrong-release")
    report = tool.assess_project(root, candidate_release="requested-candidate")
    assert report["baseline_release"] == "release-one"
    assert report["candidate_release"] == "requested-candidate"
    assert snapshots[0][1]["trusted_release_sha256"] == "a" * 64
    assert snapshots[0][1]["require_trusted_release"] is True
    assert all(snapshot[1]["reload_on_change"] is False for snapshot in snapshots)


def test_missing_baseline_never_falls_back_to_candidate(monkeypatch):
    monkeypatch.setattr(tool, "active_snapshot", lambda root: (root, None))
    loader = Mock(side_effect=AssertionError("must not load an untrusted baseline"))
    monkeypatch.setattr(tool, "KnowledgeBase", loader)
    out = io.StringIO()
    assert tool.main([], stdout=out) == 1
    assert json.loads(out.getvalue())["state"] == "assessment_rejected"
    loader.assert_not_called()


@pytest.mark.parametrize("failure", [ValueError("PRIVATE_VALUE"), OSError("PRIVATE_PATH"), KeyError("PRIVATE_FIELD")])
def test_error_output_has_no_private_diagnostics(monkeypatch, failure):
    monkeypatch.setattr(tool, "assess_project", Mock(side_effect=failure))
    out = io.StringIO()
    assert tool.main([], stdout=out) == 1
    assert "PRIVATE" not in out.getvalue()


@pytest.mark.parametrize("complete,code", [(True, 0), (False, 2)])
def test_incomplete_graph_has_nonzero_exit_without_claiming_publication(monkeypatch, complete, code):
    report = {"assessment_complete": complete, "publication_authorized": False}
    assess = Mock(return_value=report)
    monkeypatch.setattr(tool, "assess_project", assess)
    out = io.StringIO()
    assert tool.main(["--include-values"], stdout=out) == code
    assert assess.call_args.kwargs["include_values"] is True
    assert json.loads(out.getvalue())["publication_authorized"] is False


def test_proposal_tool_uses_only_trusted_baseline_never_candidate(monkeypatch):
    from app.tests.test_knowledge_proposals import baseline, proposal

    old = baseline()
    knowledge = Mock()
    knowledge.planning_manifest.return_value = {"release_id": "trusted-release"}
    loader = Mock(return_value=knowledge)
    capture = Mock(return_value=old)
    monkeypatch.setattr(tool, "load_baseline", loader)
    monkeypatch.setattr(tool, "capture", capture)
    monkeypatch.setattr(tool, "assess_project", Mock(side_effect=AssertionError("candidate must not be loaded")))
    out = io.StringIO()
    request = proposal(old, formula="stock - sales - transfer")
    assert tool.main(["--proposal-stdin"], stdin=io.StringIO(json.dumps(request)), stdout=out) == 0
    report = json.loads(out.getvalue())
    assert report["state"] == "proposal_assessed_not_executed"
    assert report["baseline_release"] == "trusted-release"
    assert report["publication_authorized"] is False
    loader.assert_called_once()
    capture.assert_called_once_with(knowledge, trusted=True)


def test_selection_tool_roundtrip_is_request_local_without_query_or_publishing(monkeypatch):
    from app.tests.test_knowledge_proposals import baseline, proposal

    old = baseline()
    knowledge = Mock()
    knowledge.planning_manifest.return_value = {"release_id": "trusted-release"}
    monkeypatch.setattr(tool, "load_baseline", lambda root: knowledge)
    monkeypatch.setattr(tool, "capture", lambda *a, **k: old)
    request = proposal(old, formula="stock - sales - transfer")
    report = tool.assess_active_proposal(Path("synthetic"), request)
    answers = {"proposal_sha256": report["proposal_sha256"], "choices": {q["id"]: "proposed" for q in report["questions"]}}
    output = io.StringIO()
    assert tool.main(["--resolve-stdin"], stdin=io.StringIO(json.dumps({"proposal": request, "answers": answers})), stdout=output) == 0
    selected = json.loads(output.getvalue())
    assert selected["state"] == "proposal_selected_not_executed"
    assert selected["requires_confirmation"] is False
    assert selected["publication_authorized"] is False
    assert old.contracts["metric:inventory.available"]["formula"] == "stock - sales"


@pytest.mark.parametrize("value", [{}, {"proposal": {}, "answers": None},
    {"proposal": {}, "answers": {}, "activate": True}])
def test_invalid_selection_envelope_is_rejected_before_loading_baseline(monkeypatch, value):
    loader = Mock(side_effect=AssertionError("must not load baseline"))
    monkeypatch.setattr(tool, "load_baseline", loader)
    output = io.StringIO()
    assert tool.main(["--resolve-stdin"], stdin=io.StringIO(json.dumps(value)), stdout=output) == 1
    assert json.loads(output.getvalue())["state"] == "assessment_rejected"
    loader.assert_not_called()


@pytest.mark.parametrize("raw", ['{"private":1,"private":2}', '{"private":Infinity}', 'private', ' '*200001],
                         ids=["duplicate_key", "nonfinite", "invalid_json", "oversized"])
def test_proposal_stdin_is_bounded_and_never_echoes_invalid_material(monkeypatch, raw):
    loader = Mock(side_effect=AssertionError("must not load baseline"))
    monkeypatch.setattr(tool, "load_baseline", loader)
    output = io.StringIO()
    assert tool.main(["--proposal-stdin"], stdin=io.StringIO(raw), stdout=output) == 1
    assert "private" not in output.getvalue()
    loader.assert_not_called()

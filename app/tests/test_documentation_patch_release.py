"""Reviewed prose patches must not import unrelated workspace definitions."""

from copy import deepcopy
import json
from pathlib import Path

import pytest
import yaml

from tools import publish_knowledge as publisher


@pytest.fixture
def layout(tmp_path, monkeypatch):
    base = tmp_path / "var/knowledge-releases/base-v1"
    documents = {
        "metrics/turnover.yaml": {"id": "turnover", "version": "1.0.0", "status": "active",
                                   "formula": "inventory / average_sales", "notes": "original",
                                   "approval": {"state": "approved"}},
        "sources/tables/inventory.yaml": {"id": "inventory", "version": "1.0.0", "grain": "original",
                                           "fields": {"qty": {}},
                                           "join_to": {"table": "warehouse", "on": "original", "cardinality": "many_to_one"}},
        "metrics/unrelated.yaml": {"version": "1"},
        "rules/validation.yaml": {"required": True},
        "rules/release-policy.yaml": {"required_gates": ["sql_static_read_only_validation"]},
    }
    for path, value in documents.items():
        for directory in (base, tmp_path):
            target = directory / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(yaml.safe_dump(value), encoding="utf-8")
    for name in ("metric-contract.schema.json", "source-table.schema.json"):
        path = base / "schemas" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"type": "object"}), encoding="utf-8")
    (base / "releases").mkdir()
    manifest = {"release_id": "base-v1", "status": "released", "approval": {"state": "approved"},
                "contracts": {"metrics": ["turnover@1.0.0"], "sources": ["inventory@1.0.0"]},
                "integrity": {"files": {k: "hash" for k in documents if "unrelated" not in k}}}
    (base / "releases/base-v1.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    monkeypatch.setattr(publisher, "KnowledgeBase", lambda *a, **kw: None)
    monkeypatch.setattr(publisher, "build_manifest_integrity", lambda *a: {"files": {"new": "digest"}})
    return tmp_path, base, documents, manifest


def change(root, path, **fields):
    value = yaml.safe_load((root / path).read_text(encoding="utf-8"))
    value.update(fields)
    (root / path).write_text(yaml.safe_dump(value), encoding="utf-8")


def publish(root, paths):
    return publisher.publish_documentation_patch(root, "docs-v2", "reviewer",
                                                 base_release_id="base-v1", paths=paths)


def test_prose_patch_preserves_base_and_unrelated_workspace(layout):
    root, base, documents, manifest = layout
    change(root, "metrics/turnover.yaml", notes="reviewed clarification")
    change(root, "sources/tables/inventory.yaml", grain="aggregate, not a raw unique key")
    change(root, "metrics/unrelated.yaml", version="999")
    change(root, "rules/validation.yaml", required=False)
    selected = ["metrics/turnover.yaml", "sources/tables/inventory.yaml"]
    path = publish(root, selected)
    snapshot = path.parent.parent
    for name, original in documents.items():
        assert yaml.safe_load((base / name).read_text()) == original
        expected = yaml.safe_load((root / name).read_text()) if name in selected else original
        assert yaml.safe_load((snapshot / name).read_text()) == expected
    published = yaml.safe_load(path.read_text())
    assert published["contracts"] == manifest["contracts"]
    assert published["required_gates"] == ["sql_static_read_only_validation"]
    assert published["rollback"] == {"previous_release_id": "base-v1"}
    assert [p.name for p in (snapshot / "releases").glob("*.yaml")] == ["docs-v2.yaml"]
    with pytest.raises(ValueError, match="already exists"):
        publish(root, selected)


@pytest.mark.parametrize("fields", [
    {"formula": "inventory * average_sales"}, {"status": "candidate"}, {"version": "2.0.0"},
    {"required_validations": []}, {"sources": {}}, {"approval": {"state": "unreviewed"}},
])
def test_non_documentation_changes_rejected_before_snapshot(layout, fields):
    root, _, _, _ = layout
    change(root, "metrics/turnover.yaml", **fields)
    with pytest.raises(ValueError, match="executable"):
        publish(root, ["metrics/turnover.yaml"])
    assert not (root / "var/knowledge-releases/docs-v2").exists()


@pytest.mark.parametrize("paths", [[], ["../metrics/turnover.yaml"], ["metrics/unrelated.yaml"],
                                  ["rules/validation.yaml"], ["metrics/turnover.yaml"] * 2])
def test_patch_selection_is_explicit_and_bounded(layout, paths):
    root, _, _, _ = layout
    with pytest.raises(ValueError):
        publish(root, paths)
    assert not (root / "var/knowledge-releases/docs-v2").exists()


def test_source_join_target_cannot_be_changed_as_prose(layout):
    root, _, documents, _ = layout
    join = deepcopy(documents["sources/tables/inventory.yaml"]["join_to"])
    join["table"] = "unreviewed"
    change(root, "sources/tables/inventory.yaml", join_to=join)
    with pytest.raises(ValueError, match="join target"):
        publish(root, ["sources/tables/inventory.yaml"])


def test_review_and_changed_document_required(layout):
    root, _, _, _ = layout
    with pytest.raises(ValueError, match="changed contract"):
        publish(root, ["metrics/turnover.yaml"])
    with pytest.raises(ValueError, match="reviewer"):
        publisher.publish_documentation_patch(root, "docs-v2", "", base_release_id="base-v1",
                                               paths=["metrics/turnover.yaml"])


def test_source_join_explanation_can_be_corrected(layout):
    root, _, documents, _ = layout
    join = deepcopy(documents["sources/tables/inventory.yaml"]["join_to"])
    join["on"] = "only within requested profile"
    change(root, "sources/tables/inventory.yaml", join_to=join)
    assert publish(root, ["sources/tables/inventory.yaml"]).is_file()


def test_model_receives_coherent_reviewed_turnover_scope(monkeypatch):
    from app.codex_agent import codex_knowledge_context
    from app import knowledge_activation

    root = Path(__file__).resolve().parents[2]
    monkeypatch.setattr(knowledge_activation, "active_snapshot", lambda _: (root, None))
    context = json.loads(codex_knowledge_context(root, question="上月库存周转及其两倍", compact=True))
    metric = next(m for m in context["metrics"] if m["id"] == "inventory.turnover_finished_goods_realtime")
    source = next(s for s in context["sources"] if s["id"] == "ywzt_ods.sku_inventory")
    assert "transit" not in metric["applicability"]["negative_signals"]
    assert "available_inventory" not in metric["applicability"]["negative_signals"]
    assert "not itself a reconciliation failure" in metric["notes"]
    assert "Multiple current inventory facts" in source["grain"]
    assert "ALL-warehouse totals require no such mapping" in source["join_to"]["on"]
    assert "inventory_net_nonnegative" in metric["required_validations"]
    assert "inventory_key_aggregation" in metric["required_validations"]


def test_cli_validates_snapshot_before_activation(layout, monkeypatch):
    import sys
    from tools import validate_knowledge_base as validator

    root, _, _, _ = layout
    change(root, "metrics/turnover.yaml", notes="reviewed correction")
    monkeypatch.setattr(publisher, "ROOT", root)
    monkeypatch.setattr(sys, "argv", ["publish", "patch-documentation", "--release-id", "docs-v2",
                                      "--base-release-id", "base-v1", "--approved-by", "reviewer",
                                      "--contract", "metrics/turnover.yaml"])
    prior = validator.ROOT
    seen = []
    monkeypatch.setattr(validator, "validate_repository", lambda: (seen.append(validator.ROOT) or ["invalid"], [], {}))
    monkeypatch.setattr(publisher, "activate", lambda *a: pytest.fail("invalid snapshot activated"))
    with pytest.raises(ValueError, match="validation failed"):
        publisher.main()
    assert seen == [root / "var/knowledge-releases/docs-v2"]
    assert validator.ROOT == prior

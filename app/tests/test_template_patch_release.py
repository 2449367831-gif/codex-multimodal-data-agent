"""Template-only releases must not import unrelated workspace contracts."""

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from tools import publish_knowledge as publisher


@pytest.fixture
def layout(tmp_path, monkeypatch):
    base = tmp_path / "var/knowledge-releases/base-v1"
    (base / "releases").mkdir(parents=True)
    (base / "sql_templates").mkdir()
    (base / "metrics").mkdir()
    manifest = {"release_id": "base-v1", "status": "released", "approval": {"state": "approved"},
                "contracts": {"metrics": ["sales@1"], "sources": ["source@1"]}}
    (base / "releases/base-v1.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    (base / "sql_templates/sales.sql").write_text("SELECT 1", encoding="utf-8")
    (base / "metrics/sales.yaml").write_text("version: 1", encoding="utf-8")
    (tmp_path / "sql_templates").mkdir()
    (tmp_path / "metrics").mkdir()
    (tmp_path / "sql_templates/sales.sql").write_text("SELECT 2", encoding="utf-8")
    (tmp_path / "metrics/sales.yaml").write_text("version: 999", encoding="utf-8")
    class Knowledge:
        def __init__(self, *args, **kwargs):
            pass
        def selected_template_refs(self):
            return ["sql_templates/sales.sql"]
    monkeypatch.setattr(publisher, "KnowledgeBase", Knowledge)
    monkeypatch.setattr(publisher, "build_manifest_integrity", lambda *a: {})
    return tmp_path, base, manifest


def test_patch_keeps_versions_and_base_rules_not_workspace_changes(layout):
    root, base, original = layout
    path = publisher.publish_template_patch(root, "patch-v2", "reviewer", base_release_id="base-v1",
                                            paths=["sql_templates/sales.sql"])
    target = path.parent.parent
    assert (target / "sql_templates/sales.sql").read_text() == "SELECT 2"
    assert (target / "metrics/sales.yaml").read_text() == "version: 1"
    assert (base / "sql_templates/sales.sql").read_text() == "SELECT 1"
    assert [path.name for path in (target / "releases").glob("*.yaml")] == ["patch-v2.yaml"]
    value = yaml.safe_load(path.read_text())
    assert value["contracts"] == original["contracts"]
    assert value["rollback"]["previous_release_id"] == "base-v1"
    with pytest.raises(ValueError, match="already exists"):
        publisher.publish_template_patch(root, "patch-v2", "reviewer", base_release_id="base-v1",
                                         paths=["sql_templates/sales.sql"])


@pytest.mark.parametrize("paths", [[], ["metrics/sales.yaml"], ["sql_templates/unknown.sql"],
    ["../sql_templates/sales.sql"], ["sql_templates/sales.sql", "sql_templates/sales.sql"]])
def test_patch_rejects_unselected_paths_before_creating_snapshot(layout, paths):
    root, _, _ = layout
    with pytest.raises(ValueError):
        publisher.publish_template_patch(root, "patch-v2", "reviewer", base_release_id="base-v1", paths=paths)
    assert not (root / "var/knowledge-releases/patch-v2").exists()


def test_candidate_base_cannot_be_published(layout):
    root, base, original = layout
    candidate = deepcopy(original)
    candidate["status"] = "candidate"
    (base / "releases/base-v1.yaml").write_text(yaml.safe_dump(candidate))
    with pytest.raises(ValueError, match="already be approved"):
        publisher.publish_template_patch(root, "patch-v2", "reviewer", base_release_id="base-v1",
                                         paths=["sql_templates/sales.sql"])

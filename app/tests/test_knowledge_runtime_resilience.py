from hashlib import sha256
from pathlib import Path

import pytest
import yaml

from app.errors import KnowledgeBaseError, MetricNotFoundError
from app.knowledge import KnowledgeBase
from app.release_integrity import build_manifest_integrity


def write_yaml(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


def candidate(**overrides) -> dict:
    return {
        "id": "inventory.experimental_turnover", "version": "0.1.0",
        "status": "candidate", "business_definition": "Awaiting review.",
        "grain": "warehouse", "unit": "EA", "time_window": {"timezone": "Asia/Shanghai"},
        "sources": {"required": []}, "parameters": {}, "required_validations": [],
        **overrides,
    }


@pytest.fixture
def released(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_RELEASE_ID", raising=False)
    metric = candidate(id="inventory.test", status="active", version="1.0.0")
    write_yaml(tmp_path / "metrics/stable.yaml", metric)
    write_yaml(tmp_path / "rules/validations.yaml", {"validations": {}})
    write_yaml(tmp_path / "app/routing_aliases.yaml", {"aliases": {}})
    write_yaml(tmp_path / "app/template_capabilities.yaml", {})
    manifest = {
        "release_id": "release-test", "status": "released",
        "contracts": {"metrics": ["inventory.test@1.0.0"], "sources": []},
        "golden_suites": [],
    }
    manifest["integrity"] = build_manifest_integrity(tmp_path, manifest)
    path = tmp_path / "releases/release-test.yaml"
    write_yaml(path, manifest)
    return tmp_path, sha256(path.read_bytes()).hexdigest()


def load(released):
    root, digest = released
    return KnowledgeBase(root, require_trusted_release=True, trusted_release_sha256=digest)


def verify(knowledge):
    # In production the template compiler supplies this separately loaded file.
    path = knowledge.project_root / "app/template_capabilities.yaml"
    knowledge.verify_runtime_snapshot_integrity({
        "app/template_capabilities.yaml": sha256(path.read_bytes()).hexdigest(),
    })


@pytest.mark.parametrize("problem", ["missing", "invalid_sources", "unknown_sources"])
def test_unselected_candidate_does_not_disable_trusted_release(released, problem):
    root, _ = released
    knowledge = load(released)
    draft = candidate()
    if problem == "missing":
        del draft["time_window"]
    elif problem == "invalid_sources":
        draft["sources"] = {"required": "not-a-list"}
    else:
        draft["sources"] = {"required": ["warehouse.not_yet_reviewed"]}
    write_yaml(root / "metrics/draft.yaml", draft)

    assert knowledge.reload_if_changed()
    assert knowledge.metric("inventory.test")["version"] == "1.0.0"
    assert knowledge.released_manifest()["release_id"] == "release-test"
    verify(knowledge)
    verify(load(released))
    with pytest.raises(MetricNotFoundError):
        knowledge.metric(draft["id"], draft["version"])


def test_selected_incomplete_candidate_still_fails_release_loading(released):
    root, _ = released
    draft = candidate()
    del draft["time_window"]
    write_yaml(root / "metrics/draft.yaml", draft)
    write_yaml(root / "releases/review.yaml", {
        "release_id": "review", "status": "candidate",
        "contracts": {"metrics": ["inventory.experimental_turnover@0.1.0"], "sources": []},
    })

    with pytest.raises(KnowledgeBaseError, match="missing metrics contract"):
        KnowledgeBase(root, release_id="review")


@pytest.mark.parametrize("status", ["active", "verified_template", "candidate"])
def test_corrupted_selected_contract_cannot_bypass_trusted_release(released, status):
    root, _ = released
    metric = candidate(id="inventory.test", version="1.0.0", status=status)
    del metric["time_window"]
    write_yaml(root / "metrics/stable.yaml", metric)

    with pytest.raises(KnowledgeBaseError):
        load(released)


def test_completed_candidate_becomes_reviewable_but_never_selected_automatically(released):
    root, _ = released
    draft = candidate()
    write_yaml(root / "metrics/draft.yaml", draft)
    knowledge = load(released)

    assert knowledge.metric(draft["id"], draft["version"])["status"] == "candidate"
    assert [item["id"] for item in knowledge.metrics()] == ["inventory.test"]

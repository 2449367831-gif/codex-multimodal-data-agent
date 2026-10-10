from hashlib import sha256
from pathlib import Path

import pytest
import yaml

from app.agent_core.catalog import ReleasedMetricCatalog, approved_metric_contracts
from app.errors import KnowledgeBaseError
from app.release_integrity import build_manifest_integrity


@pytest.fixture
def catalog_root(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_RELEASE_ID", raising=False)
    for folder in ("metrics", "rules", "app", "releases"):
        (tmp_path / folder).mkdir(exist_ok=True)
    for name, value in {
        "rules/validations.yaml": {"validations": {}},
        "app/routing_aliases.yaml": {"aliases": {}},
        "app/template_capabilities.yaml": {},
    }.items():
        (tmp_path / name).write_text(yaml.safe_dump(value), encoding="utf-8")
    return tmp_path


def write_metric(root, name, identifier, version, *, state="approved"):
    value = {
        "id": identifier, "version": version, "status": "active",
        "approval": {"state": state}, "business_definition": "Synthetic measure",
        "grain": "sku", "unit": "EA", "time_window": {"timezone": "Asia/Shanghai"},
        "sources": {"required": []}, "parameters": {}, "required_validations": [],
    }
    path = root / "metrics" / name
    path.write_text(yaml.safe_dump(value), encoding="utf-8")
    return path


def write_release(root, refs):
    manifest = {
        "release_id": "fixture", "status": "released",
        "contracts": {"metrics": refs, "sources": []}, "golden_suites": [],
    }
    manifest["integrity"] = build_manifest_integrity(root, manifest)
    path = root / "releases/fixture.yaml"
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return sha256(path.read_bytes()).hexdigest()


def test_resolves_versioned_filename_by_contract_identity(catalog_root):
    write_metric(catalog_root, "sales.outbound_actual.yaml", "sales.outbound_actual", "1.0.0", state="pending_business_approval")
    write_metric(catalog_root, "renamed.v2.yaml", "sales.outbound_actual", "2.0.0")
    digest = write_release(catalog_root, ["sales.outbound_actual@2.0.0"])
    selected = approved_metric_contracts(catalog_root, trusted_release_digest=digest)
    assert selected["sales.outbound_actual"].version == "2.0.0"


def test_manifest_not_filename_or_latest_version_selects_contract(catalog_root):
    write_metric(catalog_root, "first.yaml", "sales.outbound_actual", "2.0.0")
    write_metric(catalog_root, "last.yaml", "sales.outbound_actual", "3.0.0")
    write_release(catalog_root, ["sales.outbound_actual@2.0.0"])
    catalog = ReleasedMetricCatalog(catalog_root)
    assert catalog.for_route("sales_outbound_top_sku").version == "2.0.0"


def test_unselected_approved_contract_is_not_executable(catalog_root):
    write_metric(catalog_root, "rank.yaml", "sales.ranked_drilldown", "1.0.0")
    write_release(catalog_root, [])
    assert ReleasedMetricCatalog(catalog_root).for_route("sales_ranked_drilldown") is None


@pytest.mark.parametrize("state", ["pending_business_approval", "rejected"])
def test_selected_but_unapproved_contract_is_not_executable(catalog_root, state):
    write_metric(catalog_root, "rank.yaml", "sales.ranked_drilldown", "1.0.0", state=state)
    write_release(catalog_root, ["sales.ranked_drilldown@1.0.0"])
    assert approved_metric_contracts(catalog_root) == {}


def test_changed_protected_bytes_are_rejected(catalog_root):
    path = write_metric(catalog_root, "rank.yaml", "sales.ranked_drilldown", "1.0.0")
    digest = write_release(catalog_root, ["sales.ranked_drilldown@1.0.0"])
    path.write_text(path.read_text(encoding="utf-8") + "unexpected: changed\n", encoding="utf-8")
    with pytest.raises(KnowledgeBaseError):
        approved_metric_contracts(catalog_root, trusted_release_digest=digest)


def test_wrong_trusted_manifest_digest_is_rejected(catalog_root):
    write_release(catalog_root, [])
    with pytest.raises(KnowledgeBaseError):
        approved_metric_contracts(catalog_root, trusted_release_digest="0" * 64)


def test_ambiguous_unmanifested_versions_fail_closed(catalog_root):
    write_metric(catalog_root, "v2.yaml", "sales.outbound_actual", "2.0.0")
    write_metric(catalog_root, "v3.yaml", "sales.outbound_actual", "3.0.0")
    with pytest.raises(KnowledgeBaseError):
        approved_metric_contracts(catalog_root)


def test_active_snapshot_selects_same_version_as_model_knowledge():
    from app.knowledge import KnowledgeBase

    catalog = ReleasedMetricCatalog(Path(__file__).resolve().parents[2])
    knowledge = KnowledgeBase(catalog.snapshot, reload_on_change=False)
    metric = catalog.for_route("sales_outbound_top_sku")
    assert metric.definition == knowledge.metric("sales.outbound_actual")

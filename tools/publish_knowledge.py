"""Publish approved definitions as an immutable local snapshot, or roll back."""

from __future__ import annotations

import argparse
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import sys
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.knowledge import KnowledgeBase
from app.release_integrity import build_manifest_integrity
from app.knowledge_activation import active_snapshot


def publish_template_patch(root: Path, release_id: str, approved_by: str, *,
                           base_release_id: str, paths: list[str]) -> Path:
    """Patch selected executable templates without importing workspace rules."""
    for identifier in (release_id, base_release_id):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{1,70}", identifier):
            raise ValueError("invalid release id")
    if not approved_by.strip() or not paths or len(paths) != len(set(paths)):
        raise ValueError("an explicit approver and unique template paths are required")
    base = root / "var/knowledge-releases" / base_release_id
    manifest_path = base / "releases" / f"{base_release_id}.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    KnowledgeBase(base)
    if manifest.get("status") != "released" or manifest.get("approval", {}).get("state") != "approved":
        raise ValueError("base release must already be approved")
    selected = set(KnowledgeBase(base).selected_template_refs())
    for value in paths:
        relative = Path(value)
        if (relative.is_absolute() or relative.as_posix() not in selected
                or relative.parts[0] != "sql_templates" or relative.suffix != ".sql"
                or ".." in relative.parts):
            raise ValueError("only selected template paths may be patched")
        (root / relative).resolve().relative_to(root.resolve())
        if not (root / relative).is_file():
            raise ValueError("patch template missing")
    target = root / "var/knowledge-releases" / release_id
    if target.exists():
        raise ValueError("release id already exists; immutable snapshots cannot be overwritten")
    def _ignore_release_manifests(directory: str, names: list[str]) -> set[str]:
        if Path(directory).resolve() == base.resolve() and "releases" in names:
            return {"releases"}
        return set()

    shutil.copytree(base, target, ignore=_ignore_release_manifests)
    (target / "releases").mkdir()
    for value in paths:
        shutil.copy2(root / value, target / value)
    now = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    manifest.update(release_id=release_id, created_at=now, released_at=now,
                    approval={"state": "approved", "approved_by": approved_by, "approved_at": now},
                    rollback={"previous_release_id": base_release_id})
    manifest["integrity"] = build_manifest_integrity(target, manifest)
    path = target / "releases" / f"{release_id}.yaml"
    path.write_text(yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8")
    KnowledgeBase(target, release_id=release_id)
    return path


def publish(root: Path, release_id: str, approved_by: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{1,70}", release_id):
        raise ValueError("invalid release id")
    if not approved_by.strip():
        raise ValueError("an explicit release approver is required")
    current = KnowledgeBase(root, release_id="candidate-0.1.0")
    metrics = [m for m in current.metrics() if m["status"] == "active" and m.get("approval", {}).get("state") == "approved"]
    refs = {m["id"]: str(m["version"]) for m in metrics}
    for metric in metrics:
        for name, version in metric.get("dependency_versions", {}).items():
            if refs.get(name) != str(version):
                raise ValueError(f"unapproved or missing dependency: {name}@{version}")
    source_ids = set()
    for metric in metrics:
        for values in metric.get("sources", {}).values():
            source_ids.update(values)
    source_refs = []
    for source_path in (root / "sources/tables").glob("*.yaml"):
        source = yaml.safe_load(source_path.read_text(encoding="utf-8"))
        if source["id"] in source_ids:
            source_refs.append(f"{source['id']}@{source['version']}")
    target = root / "var/knowledge-releases" / release_id
    if target.exists():
        raise ValueError("release id already exists; immutable snapshots cannot be overwritten")
    target.mkdir(parents=True)
    for name in ("metrics", "sources", "profiles", "rules", "sql_templates", "schemas"):
        shutil.copytree(root / name, target / name)
    (target / "app").mkdir()
    for name in ("routing_aliases.yaml", "template_capabilities.yaml"):
        shutil.copy2(root / "app" / name, target / "app" / name)
    shutil.copytree(root / "tests/golden_questions", target / "tests/golden_questions")
    (target / "releases").mkdir()
    now = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    manifest = {
        "manifest_schema_version": "1.0.0", "release_id": release_id,
        "knowledge_version": "0.2.0", "status": "released", "created_at": now, "released_at": now,
        "contracts": {"metrics": sorted(f"{name}@{version}" for name, version in refs.items()), "sources": sorted(source_refs)},
        "golden_suites": ["core.metric_routing@1.2.0", "dms.metric_routing@1.0.0"],
        "required_gates": ["json_schema_validation", "cross_reference_validation", "golden_question_runtime_regression", "business_approval_for_semantic_changes"],
        "approval": {"state": "approved", "approved_by": approved_by, "approved_at": now},
        "rollback": {"previous_release_id": None},
    }
    manifest["integrity"] = build_manifest_integrity(target, manifest)
    path = target / "releases" / f"{release_id}.yaml"
    path.write_text(yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8")
    KnowledgeBase(target)
    return path


def publish_documentation_patch(root: Path, release_id: str, approved_by: str, *,
                                base_release_id: str, paths: list[str]) -> Path:
    """Correct reviewed contract prose without importing workspace behavior.

    Contract versions/formulas remain unchanged; the new release ID and digest
    bind the documentation correction and preserve the old snapshot for rollback.
    """
    for identifier in (release_id, base_release_id):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{1,70}", identifier):
            raise ValueError("invalid release id")
    if not approved_by.strip() or not paths or len(paths) != len(set(paths)):
        raise ValueError("an explicit reviewer and unique contract paths are required")
    base = root / "var/knowledge-releases" / base_release_id
    manifest = yaml.safe_load((base / "releases" / f"{base_release_id}.yaml").read_text(encoding="utf-8"))
    KnowledgeBase(base)
    if manifest.get("status") != "released" or manifest.get("approval", {}).get("state") != "approved":
        raise ValueError("base release must already be approved")
    protected = set(manifest["integrity"]["files"])
    for value in paths:
        relative = Path(value)
        if (relative.is_absolute() or ".." in relative.parts or relative.as_posix() not in protected
                or relative.suffix != ".yaml" or relative.parts[0] not in {"metrics", "sources"}):
            raise ValueError("only selected contract documentation may be patched")
        (root / relative).resolve().relative_to(root.resolve())
        old = yaml.safe_load((base / relative).read_text(encoding="utf-8"))
        new = yaml.safe_load((root / relative).read_text(encoding="utf-8"))
        if not isinstance(new, dict) or old == new:
            raise ValueError("a changed contract mapping is required")
        # Do not publish a new formula, source, predicate, validation, approval,
        # version or field declaration through the documentation-only command.
        allowed = {"applicability", "grain", "notes"} if relative.parts[0] == "metrics" else {"grain", "join_to"}
        if {k: v for k, v in old.items() if k not in allowed} != {k: v for k, v in new.items() if k not in allowed}:
            raise ValueError("documentation patch cannot change executable contract fields")
        if relative.parts[0] == "sources" and old.get("join_to") != new.get("join_to"):
            if (not isinstance(old.get("join_to"), dict) or not isinstance(new.get("join_to"), dict)
                    or {k: v for k, v in old["join_to"].items() if k != "on"}
                    != {k: v for k, v in new["join_to"].items() if k != "on"}):
                raise ValueError("documentation patch cannot change join target or cardinality")
        from jsonschema import Draft202012Validator
        schema_name = "metric-contract.schema.json" if relative.parts[0] == "metrics" else "source-table.schema.json"
        Draft202012Validator(json.loads((base / "schemas" / schema_name).read_text(encoding="utf-8"))).validate(new)
    target = root / "var/knowledge-releases" / release_id
    if target.exists():
        raise ValueError("release id already exists; immutable snapshots cannot be overwritten")
    def ignore_manifests(directory: str, names: list[str]) -> set[str]:
        return {"releases"} if Path(directory).resolve() == base.resolve() and "releases" in names else set()
    shutil.copytree(base, target, ignore=ignore_manifests)
    (target / "releases").mkdir()
    for value in paths:
        shutil.copy2(root / value, target / value)
    now = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    manifest.update(release_id=release_id, created_at=now, released_at=now,
                    approval={"state": "approved", "approved_by": approved_by, "approved_at": now},
                    rollback={"previous_release_id": base_release_id})
    policy = yaml.safe_load((base / "rules/release-policy.yaml").read_text(encoding="utf-8"))
    manifest["required_gates"] = sorted(set(manifest.get("required_gates", [])) | set(policy["required_gates"]))
    manifest["integrity"] = build_manifest_integrity(target, manifest)
    path = target / "releases" / f"{release_id}.yaml"
    path.write_text(yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8")
    KnowledgeBase(target, release_id=release_id)
    return path


def activate(root: Path, manifest_path: Path) -> None:
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    snapshot = manifest_path.parent.parent.resolve()
    snapshot.relative_to((root / "var/knowledge-releases").resolve())
    KnowledgeBase(snapshot)
    pointer = root / "var/active-knowledge-release.json"
    previous = json.loads(pointer.read_text(encoding="utf-8")) if pointer.exists() else None
    value = {"release_id": manifest["release_id"], "snapshot": snapshot.relative_to(root).as_posix(), "manifest_sha256": sha256(manifest_path.read_bytes()).hexdigest(), "previous": previous}
    # One previous pointer is sufficient for an explicit, bounded rollback.
    if previous:
        previous.pop("previous", None)
    temporary = pointer.with_suffix(".pending")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(pointer)
    active_snapshot(root)


def publish_contract_patch(root: Path, release_id: str, approved_by: str, *,
                           base_release_id: str, paths: list[str]) -> Path:
    """Publish explicitly approved semantic changes without importing other work."""
    for identifier in (release_id, base_release_id):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{1,70}", identifier):
            raise ValueError("invalid release id")
    if not approved_by.strip() or not paths or len(paths) != len(set(paths)):
        raise ValueError("reviewer and unique contract paths required")
    base = root / "var/knowledge-releases" / base_release_id
    knowledge = KnowledgeBase(base)
    manifest = yaml.safe_load((base / "releases" / f"{base_release_id}.yaml").read_text(encoding="utf-8"))
    if manifest.get("status") != "released" or manifest.get("approval", {}).get("state") != "approved":
        raise ValueError("base release must be approved")
    updates = {}
    retire_at = {}
    from jsonschema import Draft202012Validator
    for path in paths:
        relative = Path(path)
        if (relative.is_absolute() or ".." in relative.parts or relative.suffix != ".yaml"
                or relative.parts[0] not in {"metrics", "profiles"}):
            raise ValueError("only reviewed metrics and profiles may be patched")
        (root / relative).resolve().relative_to(root.resolve())
        value = yaml.safe_load((root / relative).read_text(encoding="utf-8"))
        if relative.parts[0] == "metrics":
            Draft202012Validator(json.loads((base / "schemas/metric-contract.schema.json").read_text(encoding="utf-8"))).validate(value)
            old = knowledge.metric(value["id"])
            if (value["status"] != "active" or value["approval"]["state"] != "approved"
                    or tuple(map(int, value["version"].split('.'))) <= tuple(map(int, old["version"].split('.')))
                    or f"{old['id']}@{old['version']}" not in value["validity"]["supersedes"]):
                raise ValueError("semantic patch requires an approved superseding version")
            updates[f"{old['id']}@{old['version']}"] = f"{value['id']}@{value['version']}"
            retire_at[f"{old['id']}@{old['version']}"] = value["validity"]["effective_from"]
        elif value.get("status") != "approved" or not value.get("approved_by"):
            raise ValueError("profile must be explicitly approved")
    if not updates:
        raise ValueError("semantic patch must include its metric contract")
    target = root / "var/knowledge-releases" / release_id
    if target.exists():
        raise ValueError("release already exists")
    def ignore(directory, names):
        return {"releases"} if Path(directory).resolve() == base.resolve() else set()
    shutil.copytree(base, target, ignore=ignore)
    (target / "releases").mkdir()
    for path in paths:
        shutil.copy2(root / path, target / path)
    # Superseded versions stay available for reference, but must not remain
    # routable alongside the newly approved version in this new snapshot.
    for path in (target / "metrics").glob("*.yaml"):
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        if f"{value.get('id')}@{value.get('version')}" in updates:
            value["status"] = "deprecated"
            value["validity"]["effective_to"] = retire_at[f"{value['id']}@{value['version']}"]
            path.write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False), encoding="utf-8")
    now = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    manifest["contracts"]["metrics"] = [updates.get(ref, ref) for ref in manifest["contracts"]["metrics"]]
    manifest.update(release_id=release_id, created_at=now, released_at=now,
                    approval={"state": "approved", "approved_by": approved_by, "approved_at": now},
                    rollback={"previous_release_id": base_release_id})
    manifest["integrity"] = build_manifest_integrity(target, manifest)
    output = target / "releases" / f"{release_id}.yaml"
    output.write_text(yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8")
    KnowledgeBase(target, release_id=release_id)
    from tools import validate_knowledge_base as validator
    previous_root = validator.ROOT
    try:
        validator.ROOT = target
        errors, _, _ = validator.validate_repository()
    finally:
        validator.ROOT = previous_root
    if errors:
        raise ValueError("knowledge validation failed: " + "; ".join(errors))
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("publish", "patch-templates", "patch-documentation", "patch-contracts", "status", "rollback"))
    parser.add_argument("--release-id")
    parser.add_argument("--approved-by")
    parser.add_argument("--base-release-id")
    parser.add_argument("--template", action="append", default=[])
    parser.add_argument("--contract", action="append", default=[])
    args = parser.parse_args()
    if args.action == "status":
        snapshot, _ = active_snapshot(ROOT)
        print("Active knowledge:", snapshot.name if snapshot != ROOT else "candidate (not published)")
        return 0
    if args.action == "rollback":
        pointer = ROOT / "var/active-knowledge-release.json"
        value = json.loads(pointer.read_text(encoding="utf-8"))
        previous = value.get("previous")
        if not previous:
            raise ValueError("no prior published version; keep the current pointer unchanged")
        path = ROOT / previous["snapshot"] / "releases" / f"{previous['release_id']}.yaml"
        activate(ROOT, path)
        print("Rolled back to", previous["release_id"])
        return 0
    if not args.release_id or not args.approved_by:
        parser.error("publish requires --release-id and --approved-by")
    if args.action == "patch-contracts":
        if not args.base_release_id or not args.contract:
            parser.error("patch-contracts requires --base-release-id and --contract")
        path = publish_contract_patch(ROOT, args.release_id, args.approved_by,
                                      base_release_id=args.base_release_id, paths=args.contract)
        activate(ROOT, path)
        print("Published and activated reviewed contract patch", args.release_id)
        return 0
    if args.action == "patch-documentation":
        if not args.base_release_id or not args.contract:
            parser.error("patch-documentation requires --base-release-id and --contract")
        path = publish_documentation_patch(ROOT, args.release_id, args.approved_by,
                                           base_release_id=args.base_release_id, paths=args.contract)
        from tools import validate_knowledge_base as validator
        previous_root = validator.ROOT
        try:
            validator.ROOT = path.parent.parent
            errors, _, _ = validator.validate_repository()
        finally:
            validator.ROOT = previous_root
        if errors:
            raise ValueError("knowledge validation failed: " + "; ".join(errors))
        activate(ROOT, path)
        print("Published and activated documentation patch", args.release_id)
        return 0
    if args.action == "patch-templates":
        if not args.base_release_id or not args.template:
            parser.error("patch-templates requires --base-release-id and --template")
        path = publish_template_patch(ROOT, args.release_id, args.approved_by,
                                      base_release_id=args.base_release_id, paths=args.template)
        activate(ROOT, path)
        print("Published and activated template patch", args.release_id)
        return 0
    # Validation is read-only; publication never changes metric approval states.
    from tools.validate_knowledge_base import validate_repository
    errors, _, _ = validate_repository()
    if errors:
        raise ValueError("knowledge validation failed: " + "; ".join(errors))
    path = publish(ROOT, args.release_id, args.approved_by)
    activate(ROOT, path)
    print("Published and activated", args.release_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

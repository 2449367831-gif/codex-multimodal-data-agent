"""Restore the bundled approved knowledge without transferring machine state."""
from hashlib import sha256
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from app.knowledge import KnowledgeBase
    from tools.publish_knowledge import activate

    bundle = ROOT / "deployment/knowledge"
    manifests = list((bundle / "releases").glob("*.yaml"))
    if len(manifests) != 1:
        raise ValueError("Expected exactly one bundled knowledge release")
    knowledge = KnowledgeBase(bundle)
    manifest = knowledge.released_manifest()
    if not manifest or manifest.get("approval", {}).get("state") != "approved":
        raise ValueError("Bundled knowledge must be approved")
    knowledge.verify_runtime_snapshot_integrity(manifest["integrity"]["files"])
    release_id = manifest["release_id"]
    target = ROOT / "var/knowledge-releases" / release_id
    target.resolve().relative_to((ROOT / "var/knowledge-releases").resolve())
    pointer = ROOT / "var/active-knowledge-release.json"
    if pointer.exists():
        current = json.loads(pointer.read_text(encoding="utf-8"))
        if current.get("release_id") != release_id:
            raise ValueError("Another knowledge release is active; review before replacing it")
    if target.exists():
        target_manifest = target / "releases" / manifests[0].name
        if not target_manifest.is_file() or sha256(target_manifest.read_bytes()).digest() != sha256(manifests[0].read_bytes()).digest():
            raise ValueError("Existing release differs from bundle; refusing to overwrite")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(bundle, target)
    restored = KnowledgeBase(target)
    restored.verify_runtime_snapshot_integrity(manifest["integrity"]["files"])
    activate(ROOT, target / "releases" / manifests[0].name)
    print(f"Restored knowledge release: {release_id}")


if __name__ == "__main__":
    main()

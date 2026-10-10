"""Resolve an explicitly activated immutable knowledge snapshot."""

from hashlib import sha256
import json
from pathlib import Path


def active_snapshot(project_root: Path) -> tuple[Path, str | None]:
    pointer = project_root / "var/active-knowledge-release.json"
    if not pointer.is_file():
        return project_root, None
    value = json.loads(pointer.read_text(encoding="utf-8"))
    root = (project_root / value["snapshot"]).resolve()
    root.relative_to((project_root / "var/knowledge-releases").resolve())
    manifest = root / "releases" / (value["release_id"] + ".yaml")
    digest = sha256(manifest.read_bytes()).hexdigest()
    if digest != value["manifest_sha256"]:
        raise ValueError("active knowledge manifest integrity mismatch")
    return root, digest

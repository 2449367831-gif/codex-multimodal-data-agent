from __future__ import annotations

import argparse
from hashlib import sha256
from pathlib import Path
import sys
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.release_integrity import build_manifest_integrity, manifest_integrity_errors  # noqa: E402


def _load_manifest(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("release manifest must contain a top-level mapping")
    return value


def update_manifest(project_root: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = _load_manifest(manifest_path)
    manifest["integrity"] = build_manifest_integrity(project_root, manifest)
    manifest_path.write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministically generate or check a release manifest SHA-256 file set."
    )
    parser.add_argument("manifest", type=Path, help="Path to releases/*.yaml")
    parser.add_argument("--project-root", type=Path, default=ROOT)
    parser.add_argument("--check", action="store_true", help="Validate without changing the file")
    parser.add_argument(
        "--allow-released",
        action="store_true",
        help="Allow rewriting an existing released manifest (normally immutable).",
    )
    args = parser.parse_args(argv)
    project_root = args.project_root.resolve()
    manifest_path = args.manifest.resolve()
    manifest = _load_manifest(manifest_path)

    if args.check:
        errors = manifest_integrity_errors(project_root, manifest)
        if errors:
            print("Release integrity check failed:")
            print("\n".join(f"- {error}" for error in errors))
            return 1
        manifest_digest = sha256(manifest_path.read_bytes()).hexdigest()
        print(
            f"Release integrity valid: {manifest.get('release_id')} "
            f"manifest_sha256={manifest_digest}"
        )
        return 0

    if manifest.get("status") == "released" and not args.allow_released:
        print("Refusing to rewrite an immutable released manifest; update it before promotion.")
        return 2

    manifest = update_manifest(project_root, manifest_path)
    manifest_digest = sha256(manifest_path.read_bytes()).hexdigest()
    print(
        f"Updated {manifest_path}: "
        f"{len(manifest['integrity']['files'])} protected files using sha256; "
        f"manifest_sha256={manifest_digest}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

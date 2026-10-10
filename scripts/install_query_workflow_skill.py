"""Install the project query workflow into a Codex skills directory."""

from __future__ import annotations

import argparse
from pathlib import Path
import os
import shutil


SKILL_NAME = "doris-query-workflow"


def main() -> int:
    parser = argparse.ArgumentParser(description="Install the Doris query workflow skill")
    parser.add_argument(
        "--skills-dir",
        type=Path,
        default=None,
        help="Target skills directory; defaults to CODEX_HOME/skills or ~/.codex/skills",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Project root containing skills/doris-query-workflow",
    )
    args = parser.parse_args()

    source = (args.project_root / "skills" / SKILL_NAME).resolve()
    if not (source / "SKILL.md").is_file():
        raise SystemExit(f"Skill source not found: {source}")

    configured_home = os.environ.get("CODEX_HOME")
    target_root = (
        args.skills_dir
        or (Path(configured_home) / "skills" if configured_home else Path.home() / ".codex" / "skills")
    ).expanduser().resolve()
    target = target_root / SKILL_NAME
    target_root.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
    print(f"Installed {SKILL_NAME} to {target}")
    print("Use $doris-query-workflow explicitly, or enable skill discovery for automatic selection.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

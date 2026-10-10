from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from zipfile import ZipFile


REQUIRED_ENTRIES = {
    "app/codex_agent.py",
    "app/mcp_server.py",
    "metrics/sales.outbound_actual.yaml",
    "sources/doris.yaml",
    "rules/agent-policy.yaml",
    "sql_templates/sales.outbound_actual.sql",
    "releases/candidate-0.1.0.yaml",
}
REQUIRED_CONSOLE_SCRIPTS = {
    "doris-codex-agent = app.codex_agent:main",
    "doris-query-mcp = app.mcp_server:main",
}
FORBIDDEN_FRAGMENTS = (
    "runtime_data/",
    "audit.sqlite",
    "__pycache__/",
    ".pyc",
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a query-agent wheel artifact")
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    wheel = args.wheel.resolve()
    if not wheel.is_file():
        raise SystemExit(f"Wheel not found: {wheel}")

    with ZipFile(wheel) as archive:
        entries = set(archive.namelist())
        missing = sorted(REQUIRED_ENTRIES - entries)
        forbidden = sorted(
            entry
            for entry in entries
            if any(fragment in entry for fragment in FORBIDDEN_FRAGMENTS)
        )
        entry_point_paths = sorted(
            entry for entry in entries if entry.endswith(".dist-info/entry_points.txt")
        )
        entry_points = "\n".join(
            archive.read(path).decode("utf-8") for path in entry_point_paths
        )
        missing_scripts = sorted(
            script for script in REQUIRED_CONSOLE_SCRIPTS if script not in entry_points
        )
        if missing or forbidden or missing_scripts:
            raise SystemExit(
                "Wheel content invalid; "
                f"missing={missing}, forbidden={forbidden}, "
                f"missing_console_scripts={missing_scripts}"
            )
        with TemporaryDirectory(prefix="query-agent-wheel-") as directory:
            archive.extractall(directory)
            environment = os.environ.copy()
            environment["PYTHONPATH"] = directory
            environment["DORIS_EXECUTION_MODE"] = "mock"
            environment.pop("AGENT_PROJECT_ROOT", None)
            process = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "from pathlib import Path; import app; "
                        "from app.codex_agent import codex_mcp_config_overrides; "
                        "from app.mcp_server import build_mcp_server; "
                        "from app.service import QueryService; "
                        "service=QueryService(); "
                        "assert len(service.list_metrics()) == 4; "
                        "assert build_mcp_server(service); "
                        "assert codex_mcp_config_overrides(Path.cwd()); "
                        "assert Path(app.__file__).resolve().is_relative_to(Path.cwd())"
                    ),
                ],
                cwd=directory,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if process.returncode:
                raise SystemExit(
                    "Extracted wheel smoke failed:\n"
                    + process.stdout
                    + process.stderr
                )
    print(f"Wheel verification passed: {wheel.name}, {len(entries)} entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

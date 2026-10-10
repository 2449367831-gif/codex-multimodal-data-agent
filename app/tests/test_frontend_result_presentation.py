"""Run the browser result-state regression suite in the standard check path."""

from pathlib import Path
import shutil
import subprocess

import pytest


def test_browser_result_presentation_and_history():
    node = shutil.which("node")
    if node is None:
        pytest.fail("Node.js is required to verify the browser result contract")
    result = subprocess.run(
        [node, "--test", "tests/frontend_result_presentation.test.cjs",
         "tests/frontend_multi_results.test.cjs",
         "tests/frontend_report_entry.test.cjs"],
        cwd=Path(__file__).resolve().parents[2], text=True, encoding="utf-8",
        capture_output=True, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stdout + result.stderr

from pathlib import Path

import pytest

from app.codex_agent import (
    MAX_QUERY_WORKFLOW_SKILL_CHARS,
    _install_query_workflow_skill,
    _query_workflow_skill,
)


def test_missing_skill_preserves_legacy_installations(tmp_path):
    assert _query_workflow_skill(tmp_path) == ""


def test_loads_entrypoint_and_reference_without_other_files(tmp_path):
    directory = tmp_path / "skills" / "doris-query-workflow"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text("workflow", encoding="utf-8")
    references = directory / "references"
    references.mkdir()
    (references / "decision-contract.md").write_text("contract", encoding="utf-8")
    (directory / "untrusted.md").write_text("not-loaded", encoding="utf-8")
    assert _query_workflow_skill(tmp_path) == "workflow\n\ncontract"


def test_combined_guidance_is_bounded(tmp_path):
    directory = tmp_path / "skills" / "doris-query-workflow"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text("x" * MAX_QUERY_WORKFLOW_SKILL_CHARS, encoding="utf-8")
    references = directory / "references"
    references.mkdir()
    (references / "decision-contract.md").write_text("extra", encoding="utf-8")
    with pytest.raises(RuntimeError, match="query_workflow_skill_exceeds_capacity"):
        _query_workflow_skill(tmp_path)


def test_bundled_guidance_loads():
    text = _query_workflow_skill(Path(__file__).resolve().parents[2])
    assert "# Doris Query Workflow" in text
    assert "# 取数决策记录模板" in text


def test_installs_only_the_project_query_skill(tmp_path):
    source = tmp_path / "skills" / "doris-query-workflow"
    (source / "references").mkdir(parents=True)
    (source / "agents").mkdir(parents=True)
    (source / "SKILL.md").write_text("workflow", encoding="utf-8")
    (source / "references" / "decision-contract.md").write_text("contract", encoding="utf-8")
    (source / "agents" / "openai.yaml").write_text("interface: {}", encoding="utf-8")
    (source / "untrusted.md").write_text("must not be copied", encoding="utf-8")
    home = tmp_path / "codex-home"

    assert _install_query_workflow_skill(tmp_path, home) is True
    target = home / "skills" / "doris-query-workflow"
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "workflow"
    assert (target / "references" / "decision-contract.md").exists()
    assert (target / "agents" / "openai.yaml").exists()
    assert not (target / "untrusted.md").exists()

import pytest

from app import codex_agent
from app import codex_interpretation
from app.tests.test_codex_interpretation import runtime


@pytest.mark.parametrize("failure", ["failed", "mismatched"])
def test_query_resume_does_not_create_another_backend_conversation(runtime, monkeypatch, failure):
    monkeypatch.setattr(codex_agent, "assert_codex_environment_isolated", lambda: None)
    monkeypatch.setattr(codex_agent, "_hide_windows_subprocesses", lambda: None)
    monkeypatch.setattr(codex_agent, "isolated_codex_home", codex_interpretation.isolated_codex_home)
    monkeypatch.setattr(codex_agent, "codex_knowledge_context", lambda *args, **kwargs: "{}")
    if failure == "failed": runtime.resume_error = True
    else: runtime.returned_id = "unexpected-thread-123"
    with pytest.raises(RuntimeError, match="^codex_context_resume_failed$") as error:
        codex_agent.run_codex_question("查询昨天实际销售出库", persistent_context=True, **runtime.kwargs)
    assert "PRIVATE_PROVIDER" not in str(error.value)
    assert [call[0] for call in runtime.calls] == ["client", "resume", "close"]

import json

from app.codex_agent import run_codex_question
from app.model_instructions import GATEWAY_QUERY_INSTRUCTIONS
from app.tests.test_model_execution_states import model_host, turn


def test_every_provider_uses_the_model_first_business_contract(model_host, tmp_path):
    import openai_codex

    thread, bridge = model_host
    thread.run.return_value = turn({
        "outcome": "clarification_required",
        "questions": [{
            "id": "scope", "prompt": "请选择范围", "selection": "single",
            "options": [{"id": "all", "label": "全部"}],
        }],
    })
    run_codex_question(
        "查询业务数据",
        project_root=tmp_path,
        model_provider="OpenAI",
    )
    client = openai_codex.Codex.return_value.__enter__.return_value
    instructions = client.thread_start.call_args.kwargs["developer_instructions"]
    prompt = thread.run.call_args.args[0]
    assert GATEWAY_QUERY_INSTRUCTIONS in instructions
    assert "Choose, combine, or decline" in instructions
    assert "[HOST_QUERY_PLAN_FIRST]" in prompt
    assert "CODEX_DEVELOPER_INSTRUCTIONS" not in instructions
    bridge.query.assert_not_called()


def test_natural_language_path_never_falls_back_to_reviewed_cards(model_host, tmp_path):
    thread, bridge = model_host
    thread.run.return_value = turn({"outcome": "failed", "questions": []})
    result = run_codex_question(
        "昨天实际销售出库",
        project_root=tmp_path,
        model_provider="agent_gateway",
    )
    assert result["interaction"]["outcome"] == "failed"
    assert result["interaction"].get("questions") == []
    bridge.query.assert_not_called()

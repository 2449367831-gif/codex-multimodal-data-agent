from app.model_instructions import GATEWAY_QUERY_INSTRUCTIONS
from app.codex_agent import run_codex_question
from app.tests.test_model_execution_states import model_host, turn


def test_gateway_receives_model_first_instructions_without_unrelated_metric_rules(model_host, tmp_path):
    import openai_codex

    thread, bridge = model_host
    thread.run.return_value = turn({"outcome": "clarification_required", "questions": [{
        "id": "date", "prompt": "请选择统计日期", "selection": "single",
        "options": [{"id": "created", "label": "创建日期"},
                    {"id": "received", "label": "入库日期"}],
    }]})
    run_codex_question("查询入库数量", project_root=tmp_path, model_provider="agent_gateway")
    client = openai_codex.Codex.return_value.__enter__.return_value
    instructions = client.thread_start.call_args.kwargs["developer_instructions"]
    assert GATEWAY_QUERY_INSTRUCTIONS in instructions
    assert "Choose, combine, or decline" in instructions
    assert "doris_query_agent.query_doris_sql" in instructions
    assert "schema/catalog allowlists" in instructions
    assert "Unmentioned scope is ALL" in instructions
    assert "PRIVATE_RESULT_CONTRACT" in instructions
    assert "$doris-query-workflow" in instructions
    assert "sku_group=50" not in instructions
    assert "period_end_finished_goods_snapshot" not in instructions
    assert len(instructions) < 5000
    thread.run.assert_called_once()
    bridge.query.assert_not_called()

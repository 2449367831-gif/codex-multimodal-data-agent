"""The natural-language delivery path; no fixed business router or rule fallback."""

from collections.abc import Mapping
import json

from .codex_agent import interaction_envelope
from .errors import UnapprovedContractResultError
from .public_table_text import answer_contains_unapproved_contract_marker
from .query_outcomes import DorisResultError, backend_error


def present_model_answer(result, *, request_id, conversation_id, context_state, project_results):
    """Only model questions or host-verified current results cross this boundary.

    ``project_results`` handles presentation, pagination and values. It cannot
    select a metric or execute a query. Legacy route names are never consulted.
    """
    if not isinstance(result, Mapping):
        raise DorisResultError("plan_invalid")
    if isinstance(result.get("query_error"), Mapping):
        raise backend_error(502, result["query_error"])
    raw_interaction = result.get("interaction")
    interaction = interaction_envelope(
        json.dumps(raw_interaction, ensure_ascii=False)
        if isinstance(raw_interaction, Mapping) else result.get("final_response")
    )
    fresh_per_request = isinstance(context_state, Mapping) and context_state.get(
        "query_context_mode"
    ) == "fresh_per_request"
    tables = []
    summary = None
    evidence = result.get("query_execution") or {}
    live = isinstance(evidence, Mapping) and evidence.get(
        "live_business_result_executed", evidence.get("live_query_executed", False)
    ) is True
    # A successful discovery query can reveal multiple products or units.
    # Its rows are evidence for the choices, not a completed business answer.
    choice_required = interaction.get("outcome") == "clarification_required"
    raw_warnings = result.get("validation_warnings")
    validation_warnings = [
        {"message": item["message"]}
        for item in raw_warnings
        if isinstance(item, Mapping)
        and isinstance(item.get("message"), str)
        and item["message"].strip()
    ][:3] if isinstance(raw_warnings, list) else []
    if choice_required:
        state = "clarification_required"
        public_status = "needs_selection"
        label = "需要选择业务口径"
    else:
        if not live:
            raise DorisResultError("plan_invalid")
        if answer_contains_unapproved_contract_marker(interaction.get("answer")):
            raise UnapprovedContractResultError("当前返回涉及未审批业务口径，未展示结果")
        # These states come from normalization of the actual Doris execution,
        # never the model's claim that it generated SQL or completed a tool.
        state = result.get("normalized_result_state") or evidence.get("normalized_result_state")
        if state is None and result.get("result_state") == "no_data":
            state = "empty_result"
        if state not in (None, "succeeded", "empty_result"):
            raise DorisResultError("execution_invalid")
        try:
            tables, summary = project_results(result)
        except DorisResultError as exc:
            # A live Doris query can validly return zero rows without a
            # presentation table. Preserve that terminal state instead of
            # converting it into a result-parsing failure.
            if state != "empty_result" or getattr(exc, "reason", None) != "execution_missing":
                raise
            tables, summary = [], None
        if not tables and state != "empty_result":
            raise DorisResultError("execution_missing")
        has_rows = any(table.get("pagination", {}).get("total_rows", len(table["rows"])) for table in tables)
        actual_state = "succeeded" if has_rows else "empty_result"
        if state is not None and state != actual_state:
            raise DorisResultError("row_count_mismatch")
        state = actual_state
        public_status = "completed"
        label = "查询完成，但存在对账异常" if validation_warnings else (
            "查询完成，结果已通过校验" if has_rows else "查询成功，当前条件下没有数据"
        )
    payload = {
        "request_id": request_id,
        "conversation_id": conversation_id,
        "status": public_status,
        "query_status": state,
        "normalized_result_state": state,
        "result_state": "clarification_required" if choice_required else "live_success" if state == "succeeded" else "no_data",
        "outcome": {"state": "clarification_required" if choice_required else "completed", "label": label},
        "clarification": {"required": choice_required, "questions": interaction.get("questions", []) if choice_required else []},
        "context_state": context_state,
        "process": {
            "state": "waiting_for_selection" if choice_required else "completed",
            "steps": [
                {"id": "context", "state": "fresh" if fresh_per_request else "reused",
                 "label": "本次独立查询，仅加载学习总结" if fresh_per_request else "已继承本用户上下文"},
                {"id": "clarify" if choice_required else "query", "state": "pending" if choice_required else "completed",
                 "label": "等待本次口径选择" if choice_required else "Doris 查询和结果校验已完成"},
                *([] if choice_required else [
                    {"id": "validation", "state": "completed", "label": "结果已校验整理"}
                ]),
            ],
        },
    }
    if not choice_required and tables:
        payload.update(business_table=tables[0], business_tables=tables, business_result=summary)
    if not choice_required and state == "empty_result":
        payload["message"] = "查询成功，但当前条件下没有符合条件的数据。"
    if not choice_required and validation_warnings:
        payload["process"]["steps"][-1] = {
            "id": "validation",
            "state": "warning",
            "label": "结果已整理，对账异常待复核",
        }
        payload["validation_warnings"] = validation_warnings
        payload["message"] = "查询完成，结果已展示；存在对账异常，请复核。"
    return payload

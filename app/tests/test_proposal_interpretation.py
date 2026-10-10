from copy import deepcopy
from decimal import Decimal
import json

import pytest

from app.knowledge_proposals import snapshot_digest
from app.proposal_interpretation import (
    InterpretationError, interpretation_input, interpret_proposal, validate_interpretation,
)
from app.tests.test_knowledge_proposals import baseline


QUESTION = "可用库存应同时扣除销售和调拨占用。库存的计量单位暂不确认。"


def draft(snapshot, question=QUESTION):
    prepared = interpretation_input(snapshot, question)
    end = question.index("。") + 1
    return {
        "request_sha256": prepared["request_sha256"],
        "baseline_sha256": prepared["baseline_sha256"],
        "changes": [{"kind": "metric", "id": "inventory.available",
                     "set": {"formula": "stock - sales - transfer"}, "remove": []}],
        "segments": [
            {"start": 0, "end": end, "quote": question[:end], "role": "change",
             "targets": [{"change_index": 0, "field": "formula"}], "question_id": None},
            {"start": end, "end": len(question), "quote": question[end:], "role": "unresolved",
             "targets": [], "question_id": "unit_basis"},
        ],
        "questions": [{"id": "unit_basis", "reason": "unverified_unit", "prompt": "库存采用哪一种计量单位？",
                       "options": [{"id": "ea", "label": "基本数量"}, {"id": "defer", "label": "暂不确认"}]}],
    }


def test_complete_input_keeps_every_contract_without_scope_memory_or_tools():
    snapshot = baseline()
    prepared = interpretation_input(snapshot, QUESTION)
    assert prepared["baseline_sha256"] == snapshot_digest(snapshot)
    assert len(prepared["contracts"]) == len(snapshot.contracts)
    assert prepared["question"] == QUESTION
    assert "history" not in prepared and "tools" not in prepared
    assert prepared["policy"]["omitted_scope"] == "ALL"
    assert prepared["policy"]["query_allowed"] is False
    prepared["contracts"]["metric:inventory.available"]["unit"] = "changed"
    assert snapshot.contracts["metric:inventory.available"]["unit"] == "EA"


def test_change_and_unresolved_parts_remain_separate_without_publishing():
    snapshot = baseline()
    before = deepcopy(snapshot.contracts)
    result = validate_interpretation(snapshot, QUESTION, draft(snapshot))
    assert result["state"] == "interpretation_needs_confirmation"
    assert result["proposal"]["changes"][0]["set"] == {"formula": "stock - sales - transfer"}
    assert result["review_ready"] is False
    assert result["execution_authorized"] is False
    assert result["publication_authorized"] is False
    assert result["assessment"]["affected_metrics"] == ["inventory.available", "inventory.total"]
    assert result["questions"][0]["reason"] == "unverified_unit"
    assert snapshot.contracts == before


def test_resolved_interpretation_is_still_only_a_candidate():
    snapshot = baseline()
    question = "请将库存计算规则改成同时扣除销售和调拨占用。"
    raw = draft(snapshot, question)
    raw["segments"] = [raw["segments"][0]]
    raw["questions"] = []
    result = validate_interpretation(snapshot, question, raw)
    assert result["state"] == "interpretation_ready_for_review"
    assert result["review_ready"] is True
    assert result["assessment"]["requires_confirmation"] is True
    assert result["publication_authorized"] is False


@pytest.mark.parametrize("case", [
    "stale_request", "stale_baseline", "gap", "overlap", "wrong_quote", "bool_offset",
    "unknown_role", "unknown_field", "unknown_target", "missing_field_evidence",
    "duplicate_target", "orphan_question", "unknown_question", "change_as_context",
    "unsafe_label", "unsafe_prompt", "duplicate_option", "single_option", "extra_field",
    "protected_change", "duplicate_contract", "no_op", "unknown_source", "float_index",
])
def test_unbound_or_partial_interpretation_is_rejected_atomically(case):
    snapshot = baseline()
    raw = draft(snapshot)
    if case == "stale_request": raw["request_sha256"] = "0" * 64
    if case == "stale_baseline": raw["baseline_sha256"] = "0" * 64
    if case == "gap": raw["segments"][0]["start"] = 1
    if case == "overlap": raw["segments"][1]["start"] -= 1
    if case == "wrong_quote": raw["segments"][0]["quote"] = "模型编造的原文"
    if case == "bool_offset": raw["segments"][0]["start"] = False
    if case == "unknown_role": raw["segments"][0]["role"] = "approved"
    if case == "unknown_field": raw["segments"][0]["targets"][0]["field"] = "unit"
    if case == "unknown_target": raw["segments"][0]["targets"][0]["change_index"] = 2
    if case == "missing_field_evidence": raw["changes"][0]["set"]["unit"] = "箱"
    if case == "duplicate_target": raw["segments"][0]["targets"] *= 2
    if case == "orphan_question": raw["questions"].append({**raw["questions"][0], "id": "extra"})
    if case == "unknown_question": raw["segments"][1]["question_id"] = "missing"
    if case == "change_as_context": raw["segments"][0]["role"] = "context"
    if case == "unsafe_label": raw["questions"][0]["options"][0]["label"] = "SELECT secret FROM table"
    if case == "unsafe_prompt": raw["questions"][0]["prompt"] = "审计内容及数据库凭据"
    if case == "duplicate_option": raw["questions"][0]["options"][1]["id"] = "ea"
    if case == "single_option": raw["questions"][0]["options"] = raw["questions"][0]["options"][:1]
    if case == "extra_field": raw["execute"] = True
    if case == "protected_change": raw["changes"][0]["set"]["approval"] = {"state": "approved"}
    if case == "duplicate_contract": raw["changes"] *= 2
    if case == "no_op": raw["changes"][0]["set"]["formula"] = "stock - sales"
    if case == "unknown_source": raw["changes"][0]["set"]["sources"] = {"required": ["unknown.table"]}
    if case == "float_index": raw["segments"][0]["targets"][0]["change_index"] = 0.0
    before = deepcopy(snapshot.contracts)
    with pytest.raises(InterpretationError):
        validate_interpretation(snapshot, QUESTION, raw)
    assert snapshot.contracts == before


def test_question_change_without_reference_cannot_reuse_interpretation():
    snapshot = baseline()
    with pytest.raises(InterpretationError):
        validate_interpretation(snapshot, QUESTION + "只看一个仓库", draft(snapshot))


def test_request_coverage_includes_unicode_and_whitespace_without_normalization():
    snapshot = baseline()
    question = "  可用库存扣除调拨占用。\n单位暂不确认。\U0001f4e6"
    raw = draft(snapshot, question)
    result = validate_interpretation(snapshot, question, raw)
    assert "".join(part["quote"] for part in result["segments"]) == question


def test_interpreter_gets_only_private_input_and_raw_json_is_decoded_exactly():
    snapshot = baseline()
    calls = []
    def model(payload):
        calls.append(payload)
        raw = draft(snapshot)
        raw["changes"][0]["set"] = {"parameters": {"ratio": {"required": False, "default": "DECIMAL_SENTINEL"}}}
        raw["segments"][0]["targets"][0]["field"] = "parameters"
        return json.dumps(raw, ensure_ascii=False).replace('"DECIMAL_SENTINEL"', '0.1234567890123456789')
    result = interpret_proposal(snapshot, QUESTION, model)
    assert len(calls) == 1
    assert result["proposal"]["changes"][0]["set"]["parameters"]["ratio"]["default"] == Decimal("0.1234567890123456789")


def test_duplicate_model_json_keys_and_non_json_output_fail():
    snapshot = baseline()
    for raw in ['{"changes": [], "changes": []}', '```json\n{}\n```', '{}', 'NaN']:
        with pytest.raises(InterpretationError):
            interpret_proposal(snapshot, QUESTION, lambda _: raw)


def test_unknown_meaning_returns_all_questions_without_inventing_a_patch():
    snapshot = baseline()
    raw = draft(snapshot)
    raw["changes"] = []
    raw["segments"] = [{"start": 0, "end": len(QUESTION), "quote": QUESTION, "role": "unresolved", "targets": [], "question_id": "unit_basis"}]
    result = validate_interpretation(snapshot, QUESTION, raw)
    assert result["proposal"] is None and result["assessment"] is None
    assert result["review_ready"] is False


def test_bounded_input_fails_instead_of_truncating_knowledge():
    snapshot = baseline()
    with pytest.raises(InterpretationError): interpretation_input(snapshot, "x" * 4001)
    with pytest.raises(InterpretationError): interpretation_input(snapshot, QUESTION, max_context_chars=50)


def test_model_failure_does_not_retry_execute_or_publish():
    snapshot = baseline()
    calls = []
    def failed(payload):
        calls.append(payload)
        raise RuntimeError("PRIVATE_PROVIDER_DETAIL")
    with pytest.raises(InterpretationError) as error:
        interpret_proposal(snapshot, QUESTION, failed)
    assert len(calls) == 1
    assert "PRIVATE_PROVIDER" not in str(error.value)


def test_model_input_describes_complete_output_contract_without_executable_capabilities():
    prepared = interpretation_input(baseline(), QUESTION)
    assert set(prepared["output_contract"]["required_fields"]) == set(draft(baseline()))
    instructions = " ".join(prepared["instructions"])
    assert "Unicode code points" in instructions
    assert "preserving untouched siblings" in instructions
    assert "all unknown meanings together" in instructions
    assert "No tool or database access is permitted" in instructions
    assert prepared["output_contract"]["additional_fields_allowed"] is False


@pytest.mark.parametrize("value", [None, [], True, 0, "text", {"secret": "PRIVATE_MATERIAL"}])
def test_malformed_envelopes_never_leak_private_material(value):
    with pytest.raises(InterpretationError) as error:
        validate_interpretation(baseline(), QUESTION, value)
    assert "PRIVATE_MATERIAL" not in str(error.value)


def test_result_is_detached_from_model_and_baseline_objects():
    snapshot = baseline()
    raw = draft(snapshot)
    result = validate_interpretation(snapshot, QUESTION, raw)
    raw["changes"][0]["set"]["formula"] = "later mutation"
    result["questions"][0]["prompt"] = "another mutation"
    assert result["proposal"]["changes"][0]["set"]["formula"] == "stock - sales - transfer"
    assert raw["questions"][0]["prompt"] != "another mutation"
    assert snapshot.contracts["metric:inventory.available"]["formula"] == "stock - sales"


def test_multiple_unresolved_decisions_are_returned_together():
    snapshot = baseline()
    question = "单位未确认。库存时点未确认。"
    raw = draft(snapshot, question)
    raw["changes"] = []
    raw["questions"].append({"id": "stock_time", "reason": "ambiguous_variant",
                             "prompt": "采用哪个库存时点？", "options": [
                                 {"id": "current", "label": "当前实时库存"},
                                 {"id": "defer", "label": "暂不确认"}]})
    for segment, identifier in zip(raw["segments"], ["unit_basis", "stock_time"]):
        segment.update(role="unresolved", targets=[], question_id=identifier)
    result = validate_interpretation(snapshot, question, raw)
    assert [q["id"] for q in result["questions"]] == ["unit_basis", "stock_time"]
    assert not result["review_ready"] and result["proposal"] is None


def test_unknown_dependency_rejected_even_when_all_fields_have_span_evidence():
    snapshot = baseline()
    raw = draft(snapshot)
    raw["changes"][0]["set"]["sources"] = {"required": ["unknown.table"]}
    raw["segments"][0]["targets"].append({"change_index": 0, "field": "sources"})
    with pytest.raises(InterpretationError, match="unknown_dependency"):
        validate_interpretation(snapshot, QUESTION, raw)


def test_model_input_mutation_cannot_change_original_contracts():
    snapshot = baseline()
    before = deepcopy(snapshot.contracts)
    raw = json.dumps(draft(snapshot), ensure_ascii=False)
    def model(payload):
        payload["contracts"].clear()
        return raw
    assert interpret_proposal(snapshot, QUESTION, model)["assessment"] is not None
    assert snapshot.contracts == before


def unchanged_draft(snapshot):
    question = "库存仍使用现有计量单位。"
    prepared = interpretation_input(snapshot, question)
    target = {"kind": "metric", "id": "inventory.available", "field": "unit",
              "value_sha256": prepared["field_sha256"]["metric:inventory.available"]["unit"]}
    return question, {
        "request_sha256": prepared["request_sha256"], "baseline_sha256": prepared["baseline_sha256"],
        "changes": [], "questions": [],
        "segments": [{"start": 0, "end": len(question), "quote": question, "role": "unchanged",
                      "targets": [target], "question_id": None}],
    }


def test_already_satisfied_requirement_has_explicit_field_evidence_not_a_fake_patch():
    snapshot = baseline()
    question, raw = unchanged_draft(snapshot)
    result = validate_interpretation(snapshot, question, raw)
    assert result["state"] == "interpretation_no_change"
    assert result["proposal"] is None and result["assessment"] is None
    assert result["segments"][0]["targets"][0]["field"] == "unit"
    assert not result["review_ready"] and not result["execution_authorized"]


@pytest.mark.parametrize("case", ["stale", "missing", "unknown_kind", "duplicate", "empty", "changed", "question"])
def test_unchanged_evidence_cannot_hide_changed_or_unknown_fields(case):
    snapshot = baseline()
    question, raw = unchanged_draft(snapshot)
    segment = raw["segments"][0]
    if case == "stale": segment["targets"][0]["value_sha256"] = "0" * 64
    if case == "missing": segment["targets"][0]["field"] = "absent"
    if case == "unknown_kind": segment["targets"][0]["kind"] = "template"
    if case == "duplicate": segment["targets"] *= 2
    if case == "empty": segment["targets"] = []
    if case == "changed":
        raw["changes"] = [{"kind": "metric", "id": "inventory.available", "set": {"unit": "box"}, "remove": []}]
    if case == "question": segment["question_id"] = "unit_basis"
    with pytest.raises(InterpretationError):
        validate_interpretation(snapshot, question, raw)


def test_changed_and_unchanged_requirements_can_coexist_without_dropping_either():
    snapshot = baseline()
    question = "请扣除调拨占用。保持现有计量单位。"
    raw = draft(snapshot, question)
    _, unchanged = unchanged_draft(snapshot)
    raw["questions"] = []
    raw["segments"][1].update(role="unchanged", targets=unchanged["segments"][0]["targets"], question_id=None)
    result = validate_interpretation(snapshot, question, raw)
    assert result["review_ready"]
    assert len(result["assessment"]["questions"]) == 1
    assert [segment["role"] for segment in result["segments"]] == ["change", "unchanged"]


@pytest.mark.parametrize("question", ["第一条；第二条。", "  \U0001f4e6保持单位。\n新增要求。  ", "没有标点", "。；;\n"])
def test_supplied_source_offsets_preserve_every_codepoint(question):
    parts = interpretation_input(baseline(), question)["source_segments"]
    assert "".join(part["quote"] for part in parts) == question
    cursor = 0
    for part in parts:
        assert part["start"] == cursor
        assert part["quote"] == question[part["start"]:part["end"]]
        cursor = part["end"]
    assert cursor == len(question)

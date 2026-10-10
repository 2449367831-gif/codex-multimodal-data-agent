from copy import deepcopy

import pytest

from app.proposal_interpretation import InterpretationError, interpretation_input, validate_interpretation
from app.tests.test_knowledge_proposals import baseline
from app.tests.test_proposal_interpretation import QUESTION, draft


def answers(snapshot):
    question = draft(snapshot)["questions"][0]
    return [{"question": question, "selected_option": question["options"][0]["id"]}]


def test_confirmations_are_structured_detached_and_bound_without_changing_original_text():
    snapshot = baseline()
    selected = answers(snapshot)
    original = interpretation_input(snapshot, QUESTION)
    prepared = interpretation_input(snapshot, QUESTION, confirmation=selected)
    assert prepared["question"] == QUESTION and prepared["source_segments"] == original["source_segments"]
    assert prepared["baseline_sha256"] == original["baseline_sha256"]
    assert prepared["request_sha256"] != original["request_sha256"]
    expected = deepcopy(selected)
    selected[0]["selected_option"] = "tampered"
    assert prepared["confirmed_answers"] == expected


def test_public_question_instructions_do_not_invite_copying_private_identifiers():
    prepared = interpretation_input(baseline(), QUESTION)
    assert prepared["business_labels"]["metric:inventory.available"] == "Available inventory"
    rules = " ".join(prepared["instructions"])
    assert "technical identifiers copied from the user's question" in rules
    assert "NEVER in questions/prompts/options" in rules


def test_different_answers_invalidate_an_old_model_response():
    snapshot = baseline()
    selected = answers(snapshot)
    first = interpretation_input(snapshot, QUESTION, confirmation=selected)
    selected[0]["selected_option"] = selected[0]["question"]["options"][1]["id"]
    second = interpretation_input(snapshot, QUESTION, confirmation=selected)
    assert first["request_sha256"] != second["request_sha256"]
    raw = draft(snapshot)
    raw["request_sha256"] = first["request_sha256"]
    with pytest.raises(InterpretationError, match="stale_interpretation"):
        validate_interpretation(snapshot, QUESTION, raw, confirmation=selected)


def test_answered_question_cannot_be_presented_again_under_a_new_id():
    snapshot = baseline()
    selected = answers(snapshot)
    raw = draft(snapshot)
    raw["request_sha256"] = interpretation_input(snapshot, QUESTION, confirmation=selected)["request_sha256"]
    old_id = raw["questions"][0]["id"]
    raw["questions"][0]["id"] = "newIdentifier"
    for segment in raw["segments"]:
        if segment["question_id"] == old_id: segment["question_id"] = "newIdentifier"
    with pytest.raises(InterpretationError, match="confirmation_repeated"):
        validate_interpretation(snapshot, QUESTION, raw, confirmation=selected)


@pytest.mark.parametrize("kind", ["empty", "duplicate", "unknown_option", "extra", "unsafe", "oversized"])
def test_invalid_confirmation_never_reaches_model_input(kind):
    snapshot = baseline()
    selected = answers(snapshot)
    if kind == "empty": selected = []
    if kind == "duplicate": selected *= 2
    if kind == "unknown_option": selected[0]["selected_option"] = "forged"
    if kind == "extra": selected[0]["execute"] = True
    if kind == "unsafe": selected[0]["question"]["prompt"] = "SELECT secret FROM private.table"
    if kind == "oversized": selected *= 129
    with pytest.raises(InterpretationError):
        interpretation_input(snapshot, QUESTION, confirmation=selected)

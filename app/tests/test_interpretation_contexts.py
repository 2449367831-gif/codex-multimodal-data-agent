from copy import deepcopy

import pytest

from app.interpretation_contexts import InterpretationContextStore, InterpretationContextError
from app.proposal_interpretation import interpretation_input, validate_interpretation
from app.tests.test_knowledge_proposals import baseline
from app.tests.test_proposal_interpretation import QUESTION, draft

BINDING = ("owner-123", "conversation-123", "display-123", "request-123")


def completed(snapshot, question=QUESTION, confirmation=None):
    raw = draft(snapshot)
    raw["request_sha256"] = interpretation_input(snapshot, question, confirmation=confirmation)["request_sha256"]
    return {"query_executed": False, "interpretation": validate_interpretation(snapshot, question, raw, confirmation=confirmation)}


def store(clock=lambda: 100):
    return InterpretationContextStore(clock=clock, screen_text=lambda value: value,
        create_proposal=lambda *args: {"proposal_id": "proposal-123", "revision": 0})


def test_duplicate_submit_does_not_start_another_job():
    contexts, snapshot = store(), baseline()
    first, token = contexts.create(BINDING, snapshot, QUESTION)
    second, again = contexts.create(BINDING, snapshot, QUESTION)
    assert first == second and token and again is None
    with pytest.raises(InterpretationContextError): contexts.create(BINDING, snapshot, QUESTION + "变更")


def test_cancel_and_display_close_discard_late_results():
    for close in (False, True):
        contexts, snapshot = store(), baseline()
        opened, token = contexts.create(BINDING, snapshot, QUESTION)
        job = contexts.job(BINDING, opened["interpretation_id"], token)
        if close: contexts.close_display_session(BINDING[:3])
        else: contexts.cancel(BINDING, opened["interpretation_id"])
        assert job["cancel_event"].is_set()
        assert contexts.finish(BINDING, opened["interpretation_id"], token, completed(snapshot), snapshot) is None
        assert contexts.get(BINDING, opened["interpretation_id"])["state"] == "cancelled"


def test_all_choices_required_and_defer_never_calls_model():
    contexts, snapshot = store(), baseline()
    opened, token = contexts.create(BINDING, snapshot, QUESTION)
    pending = contexts.finish(BINDING, opened["interpretation_id"], token, completed(snapshot), snapshot)
    assert pending["state"] == "needs_confirmation"
    questions = pending["questions"]
    assert len(questions) == len(draft(snapshot)["questions"])
    with pytest.raises(InterpretationContextError):
        contexts.select(BINDING, opened["interpretation_id"], pending["revision"], {}, snapshot)
    answers = {q["id"]: "defer" for q in questions}
    deferred, token = contexts.select(BINDING, opened["interpretation_id"], pending["revision"], answers, snapshot)
    assert token is None and deferred["state"] == "deferred" and deferred["choices"] == answers
    replay, token = contexts.select(BINDING, opened["interpretation_id"], pending["revision"], answers, snapshot)
    assert replay == deferred and token is None
    selected = {q["id"]: q["options"][0]["id"] for q in questions}
    running, token = contexts.select(BINDING, opened["interpretation_id"], deferred["revision"], selected, snapshot)
    job = contexts.job(BINDING, opened["interpretation_id"], token)
    assert running["state"] == "running" and job["question"] == QUESTION
    assert len(job["confirmation"]) == len(questions)
    assert all(item["selected_option"] == item["question"]["options"][0]["id"] for item in job["confirmation"])


@pytest.mark.parametrize("index", [0, 1, 2, 3])
def test_every_binding_component_prevents_cross_context_read(index):
    contexts, snapshot = store(), baseline()
    opened, _ = contexts.create(BINDING, snapshot, QUESTION)
    foreign = list(BINDING)
    foreign[index] = "other-123"
    with pytest.raises(InterpretationContextError): contexts.get(tuple(foreign), opened["interpretation_id"])


def test_baseline_drift_expiry_and_capacity_never_publish_results():
    now = [100]
    contexts, snapshot = store(lambda: now[0]), baseline()
    opened, token = contexts.create(BINDING, snapshot, QUESTION)
    changed = deepcopy(snapshot)
    changed.contracts["metric:inventory.available"]["unit"] = "boxes"
    with pytest.raises(InterpretationContextError):
        contexts.finish(BINDING, opened["interpretation_id"], token, completed(snapshot), changed)
    now[0] += 2000
    with pytest.raises(InterpretationContextError): contexts.get(BINDING, opened["interpretation_id"])
    tiny = InterpretationContextStore(max_bytes=1, screen_text=lambda x: x, create_proposal=lambda *a: None)
    with pytest.raises(InterpretationContextError): tiny.create(BINDING, snapshot, QUESTION)


def test_screening_rejects_options_instead_of_masking_them():
    contexts = InterpretationContextStore(screen_text=lambda value: None, create_proposal=lambda *a: None)
    snapshot = baseline()
    opened, token = contexts.create(BINDING, snapshot, QUESTION)
    with pytest.raises(InterpretationContextError):
        contexts.finish(BINDING, opened["interpretation_id"], token, completed(snapshot), snapshot)
    assert contexts.get(BINDING, opened["interpretation_id"])["questions"] == []


def test_cancelled_selection_is_a_controlled_conflict_not_an_internal_error():
    contexts, snapshot = store(), baseline()
    opened, _ = contexts.create(BINDING, snapshot, QUESTION)
    contexts.cancel(BINDING, opened["interpretation_id"])
    with pytest.raises(InterpretationContextError):
        contexts.select(BINDING, opened["interpretation_id"], 0, {}, snapshot)


def test_capacity_is_checked_before_creating_a_review_candidate():
    calls = []
    contexts = InterpretationContextStore(screen_text=lambda x: x, create_proposal=lambda *a: calls.append(a))
    snapshot = baseline()
    question = "请将库存计算规则改成同时扣除销售和调拨占用。"
    opened, token = contexts.create(BINDING, snapshot, question)
    raw = draft(snapshot, question)
    raw["segments"], raw["questions"] = raw["segments"][:1], []
    result = {"query_executed": False, "interpretation": validate_interpretation(snapshot, question, raw)}
    contexts.max_bytes = 1
    with pytest.raises(InterpretationContextError):
        contexts.finish(BINDING, opened["interpretation_id"], token, result, snapshot)
    assert calls == []

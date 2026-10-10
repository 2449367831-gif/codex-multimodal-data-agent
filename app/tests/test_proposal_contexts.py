from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from app.proposal_contexts import ProposalContextError, ProposalContextStore
from app.tests.test_knowledge_proposals import baseline, proposal


BINDING = ("owner-a", "conversation-123", "ui-session-123", "request-12345678")


def create(store, binding=BINDING, *, old=None, changes=None):
    old = old or baseline()
    return store.create(*binding, old, changes or proposal(old, formula="stock - sales - transfer"))


def choose(store, opened, *, binding=BINDING, option="proposed", old=None):
    return store.resolve(*binding, opened["proposal_id"], opened["revision"],
                         {q["id"]: option for q in opened["assessment"]["questions"]}, old or baseline())


def test_creation_recovery_and_idempotency_keep_exact_binding_and_detached_state():
    store = ProposalContextStore()
    opened = create(store)
    assert create(store) == opened
    assert store.get(*BINDING, opened["proposal_id"]) == opened
    opened["assessment"]["questions"].clear()
    recovered = store.get(*BINDING, opened["proposal_id"])
    assert len(recovered["assessment"]["questions"]) == 1
    assert set(recovered) == {"proposal_id", "revision", "expires_at", "assessment"}


@pytest.mark.parametrize("position", [0, 1, 2, 3])
def test_every_binding_component_is_checked_for_read_select_and_cancel(position):
    store = ProposalContextStore()
    opened = create(store)
    foreign = list(BINDING)
    foreign[position] += "-other"
    for operation in [lambda: store.get(*foreign, opened["proposal_id"]),
                      lambda: choose(store, opened, binding=foreign),
                      lambda: store.cancel(*foreign, opened["proposal_id"])]:
        with pytest.raises(ProposalContextError) as error: operation()
        assert error.value.http_status == 404
    assert store.get(*BINDING, opened["proposal_id"]) == opened


def test_resolution_replay_is_idempotent_and_altered_answers_cannot_replace_it():
    store = ProposalContextStore()
    opened = create(store)
    resolved = choose(store, opened)
    assert resolved["revision"] == 1
    assert resolved["assessment"]["state"] == "proposal_selected_not_executed"
    assert choose(store, opened) == resolved
    with pytest.raises(ProposalContextError) as conflict:
        choose(store, opened, option="current")
    assert conflict.value.http_status == 409
    assert store.get(*BINDING, opened["proposal_id"]) == resolved


def test_deferred_selection_can_be_completed_at_new_revision_only():
    store = ProposalContextStore()
    opened = create(store)
    deferred = choose(store, opened, option="defer")
    assert deferred["assessment"]["requires_confirmation"] is True
    resolved = choose(store, deferred, option="current")
    assert resolved["revision"] == 2 and resolved["assessment"]["changes"] == []
    with pytest.raises(ProposalContextError): choose(store, opened)


def test_invalid_answers_do_not_consume_or_modify_context():
    store = ProposalContextStore()
    opened = create(store)
    with pytest.raises(ProposalContextError):
        store.resolve(*BINDING, opened["proposal_id"], 0, {}, baseline())
    assert store.get(*BINDING, opened["proposal_id"]) == opened
    assert choose(store, opened)["revision"] == 1


def test_same_request_cannot_be_reused_for_different_material():
    store = ProposalContextStore()
    opened = create(store)
    old = baseline()
    with pytest.raises(ProposalContextError) as error:
        create(store, old=old, changes=proposal(old, formula="stock + sales"))
    assert error.value.http_status == 409
    assert store.get(*BINDING, opened["proposal_id"]) == opened


def test_baseline_drift_blocks_selection_and_cached_replay():
    store = ProposalContextStore()
    opened = create(store)
    choose(store, opened)
    changed = baseline()
    changed.contracts["metric:inventory.available"]["unit"] = "boxes"
    with pytest.raises(ProposalContextError) as error: choose(store, opened, old=changed)
    assert error.value.code == "proposal_baseline_changed"


def test_mutating_caller_inputs_cannot_change_saved_proposal():
    store = ProposalContextStore()
    old = baseline()
    request = proposal(old, formula="stock - sales - transfer")
    opened = create(store, old=old, changes=request)
    old.contracts.clear()
    request["changes"].clear()
    result = choose(store, opened)
    assert result["assessment"]["changes"][0]["fields"][0]["path"] == "/formula"


def test_cancel_prevents_replay_and_create_with_original_request():
    store = ProposalContextStore()
    opened = create(store)
    assert store.cancel(*BINDING, opened["proposal_id"]) == {"state": "cancelled"}
    assert store.cancel(*BINDING, opened["proposal_id"]) == {"state": "cancelled"}
    for operation in (lambda: choose(store, opened), lambda: store.get(*BINDING, opened["proposal_id"]), lambda: create(store)):
        with pytest.raises(ProposalContextError) as error: operation()
        assert error.value.http_status == 410


def test_close_display_session_blocks_late_create_but_not_other_sessions_or_users():
    store = ProposalContextStore()
    first = create(store)
    second_binding = (*BINDING[:2], "ui-session-other", "request-other123")
    second = create(store, second_binding)
    other_binding = ("owner-b", *BINDING[1:])
    other = create(store, other_binding)
    store.close_display_session(*BINDING[:3])
    with pytest.raises(ProposalContextError): store.get(*BINDING, first["proposal_id"])
    with pytest.raises(ProposalContextError): create(store, (*BINDING[:3], "late-request123"))
    assert store.get(*second_binding, second["proposal_id"]) == second
    assert store.get(*other_binding, other["proposal_id"]) == other


def test_expiry_restart_and_capacity_are_explicit_and_do_not_evict_other_users():
    now = [1000]
    store = ProposalContextStore(clock=lambda: now[0], ttl_seconds=60, max_owner_entries=1)
    opened = create(store)
    other_binding = ("owner-b", *BINDING[1:])
    other = create(store, other_binding)
    with pytest.raises(ProposalContextError) as error: create(store, (*BINDING[:3], "another-request123"))
    assert error.value.http_status == 503
    assert store.get(*other_binding, other["proposal_id"]) == other
    with pytest.raises(ProposalContextError): ProposalContextStore().get(*BINDING, opened["proposal_id"])
    now[0] = 1060
    with pytest.raises(ProposalContextError) as expired: store.get(*BINDING, opened["proposal_id"])
    assert expired.value.http_status == 410
    assert create(store)["proposal_id"] != opened["proposal_id"]


def test_byte_capacity_rejects_atomically():
    store = ProposalContextStore(max_bytes=1)
    with pytest.raises(ProposalContextError) as error: create(store)
    assert error.value.http_status == 503
    assert not store._entries


def test_concurrent_conflicting_choices_have_one_winner():
    store = ProposalContextStore()
    opened = create(store)
    def submit(option):
        try:
            return choose(store, opened, option=option)["assessment"]["choices"]
        except ProposalContextError as error:
            return error.http_status
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, ["current", "proposed"]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count(409) == 1


def test_closed_session_tombstone_capacity_is_bounded():
    store = ProposalContextStore(max_entries=1)
    store.close_display_session(*BINDING[:3])
    with pytest.raises(ProposalContextError) as error:
        store.close_display_session("other", *BINDING[1:3])
    assert error.value.http_status == 503


@pytest.mark.parametrize("bad", [None, "", " ", "a"*129, "bad/id"])
def test_binding_components_are_bounded(bad):
    with pytest.raises(ProposalContextError): create(ProposalContextStore(), (BINDING[0], BINDING[1], bad, BINDING[3]))


def test_stale_create_has_distinct_baseline_error():
    old = baseline()
    request = proposal(old, formula="stock - sales - transfer")
    old.contracts["metric:inventory.available"]["unit"] = "boxes"
    with pytest.raises(ProposalContextError) as error:
        create(ProposalContextStore(), old=old, changes=request)
    assert error.value.code == "proposal_baseline_changed" and error.value.http_status == 409


def test_public_projection_failure_does_not_commit_or_consume_choice():
    fail = [False]
    def presenter(old, report):
        if fail[0]: raise ValueError("projection failed")
        return deepcopy(report)
    store = ProposalContextStore(presenter=presenter)
    opened = create(store)
    fail[0] = True
    with pytest.raises(ValueError): choose(store, opened)
    assert store.get(*BINDING, opened["proposal_id"]) == opened
    fail[0] = False
    assert choose(store, opened)["revision"] == 1


def test_oversized_selection_is_rejected_before_encoding_and_does_not_consume():
    store = ProposalContextStore()
    opened = create(store)
    with pytest.raises(ProposalContextError) as error:
        store.resolve(*BINDING, opened["proposal_id"], 0, {f"{i:064x}": "proposed" for i in range(129)}, baseline())
    assert error.value.http_status == 422
    assert choose(store, opened)["revision"] == 1


def test_cancelled_metadata_does_not_use_active_owner_quota():
    store = ProposalContextStore(max_owner_entries=1)
    opened = create(store)
    store.cancel(*BINDING, opened["proposal_id"])
    later = create(store, (*BINDING[:3], "another-request123"))
    assert later["proposal_id"] != opened["proposal_id"]
    with pytest.raises(ProposalContextError): create(store)

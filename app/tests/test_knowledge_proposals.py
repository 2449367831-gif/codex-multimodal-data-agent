from copy import deepcopy
from decimal import Decimal
import json

import pytest

from app.knowledge_changes import KnowledgeSnapshot
from app.knowledge_proposals import (
    ProposalError, assess_proposal, decode_proposal, resolve_proposal, snapshot_digest,
)


def metric(name="inventory.available", **overrides):
    return {
        "contract_schema_version": "1.0.0", "id": name, "version": "1.0.0",
        "status": "active", "business_concept": "inventory.quantity", "variant": "available",
        "display_name": "Available inventory", "aliases": ["Available inventory"],
        "business_definition": "Available stock after sales reservations.",
        "applicability": {"applicable_when": ["Current stock"], "not_applicable_when": ["Historical stock"]},
        "clarification": {"ambiguity_group": "inventory", "question": "Choose the inventory definition"},
        "validity": {"effective_from": None, "effective_to": None, "supersedes": []},
        "approval": {"business_owner": "owner", "state": "approved", "validation_level": "live_reconciled",
                     "approved_by": "reviewer", "approved_at": "2026-09-10T00:00:00+08:00"},
        "grain": "owner + warehouse + sku", "unit": "EA", "time_window": {"basis": "realtime"},
        "formula": "stock - sales", "sources": {"required": ["warehouse.stock"]}, "parameters": {},
        "required_validations": ["source_freshness"], "execution": {"mode": "composite"},
        "depends_on": [], "dependency_versions": {}, "updated_at": "2026-09-11", **overrides,
    }


def source():
    return {"contract_schema_version": "1.0.0", "id": "warehouse.stock", "version": "1.0.0",
            "status": "active", "source_id": "doris", "role": "inventory", "grain": "owner + warehouse + sku",
            "fields": {"quantity": {"type": "decimal", "role": "quantity"}},
            "metadata_sync": {"mode": "compare_only", "observations_path": "observations/stock.json",
                              "semantic_changes_require_approval": True}}


def baseline():
    return KnowledgeSnapshot(metrics=[metric(), metric("inventory.total", depends_on=["inventory.available"],
                             dependency_versions={"inventory.available": "1.0.0"}),
                             metric("sales.unrelated")], sources=[source()])


def proposal(snapshot, **fields):
    return {"baseline_sha256": snapshot_digest(snapshot), "changes": [
        {"kind": "metric", "id": "inventory.available", "set": fields, "remove": []}]}


def test_request_scope_has_actual_field_diff_without_unrelated_candidate_changes():
    old = baseline()
    before = deepcopy(old.contracts)
    request = proposal(old, formula="stock - sales - transfer", unit="boxes")
    report = assess_proposal(old, request, include_values=True)
    assert report["state"] == "proposal_assessed_not_executed"
    assert report["affected_metrics"] == ["inventory.available", "inventory.total"]
    assert {f["path"] for f in report["changes"][0]["fields"]} == {"/formula", "/unit"}
    assert len(report["questions"]) == 2
    assert all(q["options"] == ["current", "proposed", "defer"] for q in report["questions"])
    assert report["publication_authorized"] is False
    assert report["requires_confirmation"] is True
    assert old.contracts == before


def test_all_choices_are_bound_to_exact_proposal_and_do_not_publish():
    old = baseline()
    request = proposal(old, formula="stock - sales - transfer", unit="boxes")
    report = assess_proposal(old, request)
    choices = {q["id"]: "proposed" if q["field"] == "formula" else "current" for q in report["questions"]}
    resolved = resolve_proposal(old, request, {"proposal_sha256": report["proposal_sha256"], "choices": choices})
    assert resolved["state"] == "proposal_selected_not_executed"
    assert resolved["publication_authorized"] is False
    assert resolved["requires_confirmation"] is False
    assert {f["path"] for f in resolved["changes"][0]["fields"]} == {"/formula"}
    assert resolved["changes"][0]["requires_version_change"] is True


def test_deferred_choice_does_not_claim_complete_resolution():
    old = baseline()
    request = proposal(old, formula="stock - sales - transfer")
    report = assess_proposal(old, request)
    resolved = resolve_proposal(old, request, {"proposal_sha256": report["proposal_sha256"],
                                "choices": {q["id"]: "defer" for q in report["questions"]}})
    assert resolved["state"] == "proposal_deferred_not_executed"
    assert resolved["requires_confirmation"] is True
    assert resolved["changes"] == []
    assert resolved["deferred_fields"] == [report["questions"][0]["id"]]


@pytest.mark.parametrize("mutation", ["missing", "extra", "invalid", "wrong_proposal"])
def test_partial_or_foreign_answers_never_apply(mutation):
    old = baseline()
    request = proposal(old, formula="stock - sales - transfer")
    report = assess_proposal(old, request)
    answers = {"proposal_sha256": report["proposal_sha256"], "choices": {q["id"]: "proposed" for q in report["questions"]}}
    if mutation == "missing": answers["choices"].clear()
    if mutation == "extra": answers["choices"]["foreign"] = "proposed"
    if mutation == "invalid": answers["choices"][report["questions"][0]["id"]] = True
    if mutation == "wrong_proposal": request["changes"][0]["set"]["formula"] = "stock - sales + transfer"
    with pytest.raises(ProposalError):
        resolve_proposal(old, request, answers)


def test_same_version_baseline_edits_and_template_byte_changes_invalidate_proposal():
    old = baseline()
    request = proposal(old, formula="stock - sales - transfer")
    old.contracts["metric:inventory.available"]["unit"] = "boxes"
    with pytest.raises(ProposalError, match="stale_baseline"):
        assess_proposal(old, request)
    old = baseline()
    request = proposal(old, formula="stock - sales - transfer")
    old.contracts["template:sql_templates/stock.sql"] = {"id": "sql_templates/stock.sql", "sha256": "a" * 64}
    with pytest.raises(ProposalError, match="stale_baseline"):
        assess_proposal(old, request)


@pytest.mark.parametrize("field,value", [("id", "inventory.other"), ("status", "active"),
    ("approval", {}), ("validity", {}), ("contract_schema_version", "2.0.0")])
def test_proposal_cannot_modify_governance_or_identity(field, value):
    old = baseline()
    with pytest.raises(ProposalError, match="protected_contract_field"):
        assess_proposal(old, proposal(old, **{field: value}))


@pytest.mark.parametrize("fields", [{"unknown": "x"}, {"formula": None}, {"version": "invalid"},
    {"parameters": {"day": {"required": "yes"}}}, {"updated_at": "2026-02-31"}])
def test_schema_validation_rejects_invalid_definitions(fields):
    old = baseline()
    with pytest.raises(ProposalError, match="invalid_proposed_contract"):
        assess_proposal(old, proposal(old, **fields))


def test_root_replacement_is_not_recursive_merge_and_optional_deletion_is_explicit():
    old = baseline()
    old.contracts["metric:inventory.available"]["filters"] = {"old": "condition", "keep": None}
    request = proposal(old, filters={"new": "condition"})
    request["changes"][0]["remove"] = ["aliases"]
    with pytest.raises(ProposalError, match="invalid_proposed_contract"):
        assess_proposal(old, request)
    request["changes"][0]["remove"] = []
    report = assess_proposal(old, request, include_values=True)
    paths = {f["path"] for f in report["changes"][0]["fields"]}
    assert paths == {"/filters/new", "/filters/old", "/filters/keep"}
    assert len(report["questions"]) == 1


def test_source_change_propagates_without_altering_metadata_sync_policy():
    old = baseline()
    request = {"baseline_sha256": snapshot_digest(old), "changes": [{"kind": "source", "id": "warehouse.stock",
               "set": {"fields": {"quantity": {"type": "string", "role": "quantity"}}}, "remove": []}]}
    assert "source_schema_compatibility" in assess_proposal(old, request)["required_checks"]
    request["changes"][0]["set"] = {"metadata_sync": {}}
    with pytest.raises(ProposalError, match="protected_contract_field"):
        assess_proposal(old, request)


def test_version_change_requires_consumer_pins_to_be_updated_together():
    old = baseline()
    request = proposal(old, version="2.0.0", formula="stock - sales - transfer")
    report = assess_proposal(old, request)
    assert report["assessment_complete"] is False
    assert any(i["code"] == "dependency_version_mismatch" for i in report["issues"])
    request["changes"].append({"kind": "metric", "id": "inventory.total", "set": {
        "dependency_versions": {"inventory.available": "2.0.0"}}, "remove": []})
    assert assess_proposal(old, request)["assessment_complete"] is True


@pytest.mark.parametrize("mode", ["empty", "duplicate", "unknown", "overlap", "missing_removal"])
def test_ambiguous_or_unknown_targets_are_rejected_atomically(mode):
    old = baseline()
    before = deepcopy(old.contracts)
    request = proposal(old, formula="stock - sales - transfer")
    if mode == "empty": request["changes"] = []
    if mode == "duplicate": request["changes"] *= 2
    if mode == "unknown": request["changes"][0]["id"] = "inventory.unknown"
    if mode == "overlap": request["changes"][0]["remove"] = ["formula"]
    if mode == "missing_removal": request["changes"][0]["remove"] = ["notes"]
    with pytest.raises(ProposalError): assess_proposal(old, request)
    assert old.contracts == before


@pytest.mark.parametrize("raw", ['{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}', 'private invalid', '['*1000,
                                '{"x":1e99999999999999999999999999999999999}'],
                         ids=["duplicate", "nan", "infinity", "invalid", "deep", "extreme_exponent"])
def test_decoder_rejects_malformed_input_without_echo(raw):
    with pytest.raises(ProposalError) as error: decode_proposal(raw)
    assert "private" not in str(error.value)


def test_size_bound_and_value_free_reports():
    with pytest.raises(ProposalError): decode_proposal(" " * 200001)
    old = baseline()
    report = assess_proposal(old, proposal(old, formula="PRIVATE_FORMULA"))
    assert "PRIVATE_FORMULA" not in json.dumps(report)


def test_idempotent_noop_has_no_choices_and_stable_fingerprint():
    old = baseline()
    request = proposal(old, formula="stock - sales")
    report = assess_proposal(old, request)
    assert report["changes"] == [] and report["questions"] == []
    assert report["requires_confirmation"] is False
    assert report == assess_proposal(old, deepcopy(request))


def test_decimal_json_threshold_survives_without_float_rounding():
    old = baseline()
    request = proposal(old, filters={"threshold": 0})
    raw = json.dumps(request).replace('"threshold": 0', '"threshold": 0.123456789012345678901')
    decoded = decode_proposal(raw)
    assert decoded["changes"][0]["set"]["filters"]["threshold"] == Decimal("0.123456789012345678901")
    report = assess_proposal(old, decoded, include_values=True)
    field = report["changes"][0]["fields"][0]
    assert field["after"]["threshold"] == {"numeric_type": "decimal", "value": "0.123456789012345678901"}
    json.dumps(report)


def test_optional_removal_and_null_are_distinct_decisions():
    old = baseline()
    old.contracts["metric:inventory.available"]["notes"] = "Prior notes"
    request = proposal(old, filters={"threshold": None})
    request["changes"][0]["remove"] = ["notes"]
    report = assess_proposal(old, request, include_values=True)
    assert {q["field"]: q["operation"] for q in report["questions"]} == {"filters": "add", "notes": "remove"}
    selected = resolve_proposal(old, request, {"proposal_sha256": report["proposal_sha256"],
        "choices": {q["id"]: "proposed" for q in report["questions"]}}, include_values=True)
    assert selected["changes"] == report["changes"]


def test_mixed_selection_retains_graph_errors_without_claiming_readiness():
    old = baseline()
    request = proposal(old, version="2.0.0")
    request["changes"].append({"kind": "metric", "id": "inventory.total", "set": {
        "dependency_versions": {"inventory.available": "2.0.0"}}, "remove": []})
    report = assess_proposal(old, request)
    assert report["assessment_complete"] is True
    selected = resolve_proposal(old, request, {"proposal_sha256": report["proposal_sha256"], "choices": {
        q["id"]: "proposed" if q["field"] == "version" else "current" for q in report["questions"]}})
    assert selected["assessment_complete"] is False
    assert selected["publication_authorized"] is False


def test_digest_preserves_type_and_mapping_order_not_input_order():
    old = baseline()
    request = proposal(old, formula="stock - sales - transfer", unit="boxes")
    reordered = deepcopy(request)
    reordered["changes"][0]["set"] = {"unit": "boxes", "formula": "stock - sales - transfer"}
    assert assess_proposal(old, request) == assess_proposal(old, reordered)
    other = baseline()
    other.contracts["metric:inventory.available"]["parameters"] = {"zero": {"required": False}}
    original = snapshot_digest(other)
    other.contracts["metric:inventory.available"]["parameters"]["zero"]["required"] = 0
    assert snapshot_digest(other) != original


def test_change_and_question_limits_reject_instead_of_truncating(monkeypatch):
    old = baseline()
    request = proposal(old, formula="stock - sales - transfer")
    request["changes"] *= 33
    with pytest.raises(ProposalError, match="invalid_change_count"):
        assess_proposal(old, request)
    monkeypatch.setattr("app.knowledge_proposals.MAX_DECISIONS", 1)
    with pytest.raises(ProposalError, match="too_many_decisions"):
        assess_proposal(old, proposal(old, formula="stock - sales - transfer", unit="boxes"))

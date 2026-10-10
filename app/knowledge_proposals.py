"""Request-scoped, immutable knowledge proposals and all-at-once decisions."""

from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal, DecimalException
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from .knowledge_changes import KnowledgeSnapshot, _encoded, _validate, assess_changes


MAX_PROPOSAL_CHARS = 200000
MAX_CHANGES = 32
MAX_DECISIONS = 128
_PROTECTED = {"id", "status", "approval", "validity", "contract_schema_version", "source_id", "metadata_sync"}


class ProposalError(ValueError):
    pass


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProposalError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(_):
    raise ProposalError("nonfinite_json_number")


def decode_proposal(raw):
    if not isinstance(raw, str) or not raw.strip() or len(raw) > MAX_PROPOSAL_CHARS:
        raise ProposalError("invalid_proposal_input")
    try:
        value = json.loads(raw, parse_float=Decimal, object_pairs_hook=_pairs, parse_constant=_reject_constant)
        _validate(value)
        return value
    except (ValueError, TypeError, RecursionError, DecimalException):
        raise ProposalError("invalid_proposal_input") from None


def snapshot_digest(snapshot):
    return sha256(_encoded(snapshot.contracts).encode()).hexdigest()


@lru_cache(maxsize=2)
def _validator(kind):
    name = "metric-contract" if kind == "metric" else "source-table"
    schema = json.loads((Path(__file__).resolve().parents[1] / "schemas" / (name + ".schema.json")).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _schema_value(value):
    if isinstance(value, dict):
        return {key: _schema_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_schema_value(item) for item in value]
    if type(value) in (date, datetime):
        return value.isoformat()
    return value


def _snapshot(contracts):
    return KnowledgeSnapshot(
        metrics=[value for key, value in contracts.items() if key.startswith("metric:")],
        sources=[value for key, value in contracts.items() if key.startswith("source:")],
        templates={value["id"]: value["sha256"] for key, value in contracts.items() if key.startswith("template:")},
    )


def _prepare(baseline, request):
    try:
        _validate(request)
        if len(_encoded(request)) > MAX_PROPOSAL_CHARS:
            raise ProposalError("proposal_too_large")
    except (ValueError, TypeError, RecursionError):
        raise ProposalError("invalid_proposal_input") from None
    if not isinstance(request, dict) or set(request) != {"baseline_sha256", "changes"}:
        raise ProposalError("invalid_proposal_fields")
    if request["baseline_sha256"] != snapshot_digest(baseline):
        raise ProposalError("stale_baseline")
    changes = request["changes"]
    if not isinstance(changes, list) or not 1 <= len(changes) <= MAX_CHANGES:
        raise ProposalError("invalid_change_count")
    contracts = deepcopy(baseline.contracts)
    seen, decisions = set(), []
    for change in changes:
        if not isinstance(change, dict) or set(change) != {"kind", "id", "set", "remove"}:
            raise ProposalError("invalid_change_fields")
        kind, identifier = change["kind"], change["id"]
        if kind not in ("metric", "source") or not isinstance(identifier, str):
            raise ProposalError("invalid_change_target")
        key = kind + ":" + identifier
        if key not in baseline.contracts or key in seen:
            raise ProposalError("unknown_or_duplicate_target")
        seen.add(key)
        updates, removals = change["set"], change["remove"]
        if not isinstance(updates, dict) or not isinstance(removals, list) or any(not isinstance(f, str) for f in removals):
            raise ProposalError("invalid_field_changes")
        fields = set(updates) | set(removals)
        if not fields or len(removals) != len(set(removals)) or set(updates) & set(removals):
            raise ProposalError("ambiguous_field_changes")
        if fields & _PROTECTED:
            raise ProposalError("protected_contract_field")
        original = baseline.contracts[key]
        proposed = contracts[key]
        for field in sorted(fields):
            if field in removals:
                if field not in original:
                    raise ProposalError("missing_removal_field")
                del proposed[field]
            else:
                proposed[field] = deepcopy(updates[field])
            if field in original and field in proposed and _encoded(original[field]) == _encoded(proposed[field]):
                continue
            decision_id = sha256(_encoded([kind, identifier, field]).encode()).hexdigest()
            decisions.append({"id": decision_id, "kind": kind, "contract_id": identifier, "field": field,
                              "operation": "remove" if field in removals else "replace" if field in original else "add",
                              "options": ["current", "proposed", "defer"]})
        if not _validator(kind).is_valid(_schema_value(proposed)):
            raise ProposalError("invalid_proposed_contract")
    if len(decisions) > MAX_DECISIONS:
        raise ProposalError("too_many_decisions")
    decisions.sort(key=lambda item: (item["kind"], item["contract_id"], item["field"]))
    candidate = _snapshot(contracts)
    fingerprint = sha256(_encoded([snapshot_digest(baseline), snapshot_digest(candidate)]).encode()).hexdigest()
    return candidate, decisions, fingerprint


def assess_proposal(baseline, request, *, include_values=False):
    candidate, decisions, fingerprint = _prepare(baseline, request)
    report = assess_changes(baseline, candidate, include_values=include_values)
    report.update(state="proposal_assessed_not_executed", baseline_sha256=snapshot_digest(baseline),
                  proposal_sha256=fingerprint, questions=decisions, requires_confirmation=bool(decisions))
    report["limitations"].append("Proposed fields are unapproved. Existing governance metadata is not approval of the proposal; choices never authorize execution or publication.")
    return report


def resolve_proposal(baseline, request, answers, *, include_values=False):
    candidate, decisions, fingerprint = _prepare(baseline, request)
    if not isinstance(answers, dict) or set(answers) != {"proposal_sha256", "choices"} or answers["proposal_sha256"] != fingerprint:
        raise ProposalError("stale_or_invalid_answers")
    choices = answers["choices"]
    if not isinstance(choices, dict) or set(choices) != {item["id"] for item in decisions}:
        raise ProposalError("incomplete_or_extra_answers")
    if any(value not in ("current", "proposed", "defer") for value in choices.values()):
        raise ProposalError("invalid_choice")
    selected = deepcopy(baseline.contracts)
    deferred = []
    for decision in decisions:
        choice = choices[decision["id"]]
        if choice == "defer":
            deferred.append(decision["id"])
        if choice != "proposed":
            continue
        key, field = decision["kind"] + ":" + decision["contract_id"], decision["field"]
        if field in candidate.contracts[key]:
            selected[key][field] = deepcopy(candidate.contracts[key][field])
        else:
            selected[key].pop(field, None)
    # Individually valid alternatives can become invalid when mixed in one choice set.
    for key in {item["kind"] + ":" + item["contract_id"] for item in decisions}:
        if not _validator(key.split(":", 1)[0]).is_valid(_schema_value(selected[key])):
            raise ProposalError("invalid_selected_contract")
    report = assess_changes(baseline, _snapshot(selected), include_values=include_values)
    report.update(state="proposal_deferred_not_executed" if deferred else "proposal_selected_not_executed",
                  baseline_sha256=snapshot_digest(baseline), proposal_sha256=fingerprint,
                  questions=decisions, choices=deepcopy(choices), deferred_fields=deferred,
                  requires_confirmation=bool(deferred))
    report["limitations"].append("Selections are request-local and unapproved. They do not modify contracts, publish a version, or authorize a database query.")
    return report

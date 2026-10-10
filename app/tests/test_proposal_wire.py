from copy import deepcopy
from decimal import Decimal
import json

from jsonschema import Draft202012Validator
import pytest

from app.proposal_interpretation import InterpretationError, interpretation_input, validate_interpretation
from app.proposal_wire import decode_interpretation_output, interpretation_output_schema
from app.tests.test_knowledge_proposals import baseline
from app.tests.test_proposal_interpretation import QUESTION, draft, unchanged_draft


def wire_draft(raw):
    value = deepcopy(raw)
    for change in value["changes"]:
        change["updates"] = [{"field": field, "value_json": json.dumps(v, ensure_ascii=False)} for field, v in change.pop("set").items()]
    for segment in value["segments"]:
        for target in segment["targets"]:
            if segment["role"] == "change": target.update(kind=None, id=None, value_sha256=None)
            else: target["change_index"] = None
    return value


def test_strict_wire_and_core_roundtrip_preserve_meaning():
    snapshot = baseline()
    raw = draft(snapshot)
    wire = wire_draft(raw)
    schema = interpretation_output_schema(interpretation_input(snapshot, QUESTION))
    Draft202012Validator.check_schema(schema)
    assert Draft202012Validator(schema).is_valid(wire)
    decoded = decode_interpretation_output(json.dumps(wire), schema)
    assert decoded == raw
    assert validate_interpretation(snapshot, QUESTION, decoded)["assessment"] is not None


def test_unchanged_fields_roundtrip_without_becoming_modifications():
    snapshot = baseline()
    question, raw = unchanged_draft(snapshot)
    schema = interpretation_output_schema(interpretation_input(snapshot, question))
    assert decode_interpretation_output(json.dumps(wire_draft(raw)), schema) == raw


@pytest.mark.parametrize("case", ["duplicate", "invalid_json", "nan", "duplicate_key", "wrong_id", "extra", "missing", "wrong_target", "stale"])
def test_invalid_wire_rejected_without_partial_interpretation(case):
    snapshot = baseline()
    schema = interpretation_output_schema(interpretation_input(snapshot, QUESTION))
    wire = wire_draft(draft(snapshot))
    if case == "duplicate": wire["changes"][0]["updates"] *= 2
    if case == "invalid_json": wire["changes"][0]["updates"][0]["value_json"] = "unquoted text"
    if case == "nan": wire["changes"][0]["updates"][0]["value_json"] = "NaN"
    if case == "duplicate_key": wire["changes"][0]["updates"][0]["value_json"] = '{"x":1,"x":2}'
    if case == "wrong_id": wire["changes"][0]["id"] = "metric:inventory.available"
    if case == "extra": wire["changes"][0]["approve"] = True
    if case == "missing": del wire["segments"][0]["targets"][0]["kind"]
    if case == "wrong_target": wire["segments"][0]["targets"][0]["kind"] = "metric"
    if case == "stale": wire["request_sha256"] = "0" * 64
    with pytest.raises(InterpretationError):
        decode_interpretation_output(json.dumps(wire), schema)


def test_nested_json_numbers_retain_decimal_precision():
    snapshot = baseline()
    schema = interpretation_output_schema(interpretation_input(snapshot, QUESTION))
    wire = wire_draft(draft(snapshot))
    wire["changes"][0]["updates"] = [{"field": "parameters", "value_json": '{"ratio":{"required":false,"default":0.1234567890123456789}}'}]
    wire["segments"][0]["targets"][0]["field"] = "parameters"
    decoded = decode_interpretation_output(json.dumps(wire), schema)
    assert decoded["changes"][0]["set"]["parameters"]["ratio"]["default"] == Decimal("0.1234567890123456789")
    assert validate_interpretation(snapshot, QUESTION, decoded)["proposal"] is not None

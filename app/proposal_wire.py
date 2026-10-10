"""Strict model output shape; JSON-valued amendments preserve arbitrary contracts."""

from jsonschema import Draft202012Validator

from .knowledge_proposals import decode_proposal
from .proposal_interpretation import InterpretationError, _REASONS


def _object(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _array(items):
    return {"type": "array", "items": items}


def interpretation_output_schema(payload):
    text = {"type": "string"}
    nullable = {"type": ["string", "null"]}
    contracts = payload["contracts"]
    ids = sorted({value["id"] for key, value in contracts.items() if key.startswith(("metric:", "source:"))})
    target = _object({
        "change_index": {"type": ["integer", "null"]},
        "kind": {"type": ["string", "null"], "enum": ["metric", "source", None]},
        "id": nullable, "field": text, "value_sha256": nullable,
    })
    return _object({
        "request_sha256": {"type": "string", "enum": [payload["request_sha256"]]},
        "baseline_sha256": {"type": "string", "enum": [payload["baseline_sha256"]]},
        "changes": _array(_object({
            "kind": {"type": "string", "enum": ["metric", "source"]},
            "id": {"type": "string", "enum": ids},
            "updates": _array(_object({"field": text, "value_json": text})),
            "remove": _array(text),
        })),
        "segments": _array(_object({
            "start": {"type": "integer"}, "end": {"type": "integer"}, "quote": text,
            "role": {"type": "string", "enum": ["change", "unchanged", "unresolved", "context"]},
            "targets": _array(target), "question_id": nullable,
        })),
        "questions": _array(_object({
            "id": text, "reason": {"type": "string", "enum": sorted(_REASONS)},
            "prompt": text, "options": _array(_object({"id": text, "label": text})),
        })),
    })


WIRE_INSTRUCTIONS = (
    "The output_contract JSON Schema is the output format authority. A change uses updates, not set. "
    "Each update has a root field and value_json containing ONE complete JSON value as a string "
    "(for a text value include its JSON quotes; for numbers retain exact decimal digits). "
    "Use the contract's inner id, not its metric:/source: lookup key. "
    "All target keys must be present. For change targets, use change_index and field, and set kind, "
    "id and value_sha256 to null. For unchanged targets, set change_index to null and fill kind, id, "
    "field and value_sha256 from the supplied current contract. Other roles have no targets."
)


def decode_interpretation_output(raw, schema):
    try:
        value = decode_proposal(raw)
        if not Draft202012Validator(schema).is_valid(value):
            raise InterpretationError("invalid_interpretation_wire_shape")
        for change in value["changes"]:
            updates = {}
            for update in change.pop("updates"):
                field = update["field"]
                if field in updates:
                    raise InterpretationError("duplicate_interpretation_update")
                updates[field] = decode_proposal(update["value_json"])
            change["set"] = updates
        for segment in value["segments"]:
            for target in segment["targets"]:
                if segment["role"] == "change":
                    if target["change_index"] is None or any(target[key] is not None for key in ("kind", "id", "value_sha256")):
                        raise InterpretationError("invalid_interpretation_wire_target")
                    for key in ("kind", "id", "value_sha256"):
                        del target[key]
                elif segment["role"] == "unchanged":
                    if target["change_index"] is not None or any(target[key] is None for key in ("kind", "id", "value_sha256")):
                        raise InterpretationError("invalid_interpretation_wire_target")
                    del target["change_index"]
                else:
                    raise InterpretationError("invalid_interpretation_wire_target")
        return value
    except InterpretationError:
        raise
    except (ValueError, TypeError, KeyError, RecursionError):
        raise InterpretationError("invalid_interpretation_wire_value") from None

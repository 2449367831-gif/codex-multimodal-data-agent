"""Bounded, typed JSON for private worker pipes; never deserialize executable data."""

from datetime import date, datetime
from decimal import Decimal, DecimalException
import json
import math

from .knowledge_changes import _encoded, _validate

MAX_INPUT_BYTES = 8_000_000
MAX_OUTPUT_BYTES = 4_000_000
WORKER_ERROR_CODES = frozenset({
    "interpretation_resume_failed", "interpretation_thread_mismatch",
    "interpretation_tools_not_disabled", "interpretation_turn_incomplete",
    "interpretation_unexpected_action", "interpretation_invalid_response",
    "interpretation_worker_failed", "interpretation_output_too_large",
    "context_initialization_uncertain", "context_worker_busy",
    "context_checkpoint_mismatch", "context_checkpoint_invalid", "context_checkpoint_unavailable",
})
VALIDATION_ERROR_CODES = frozenset({
    "invalid_fields", "invalid_identifier", "invalid_question_text", "unsafe_question_text",
    "invalid_questions", "duplicate_question", "invalid_question_reason", "invalid_options", "duplicate_option",
    "stale_interpretation", "invalid_changes", "no_op_change", "unknown_dependency", "invalid_segments",
    "invalid_span", "invalid_quote", "invalid_role", "invalid_targets", "unbound_unchanged",
    "invalid_unchanged_target", "invalid_target", "unknown_or_duplicate_target", "unbound_change",
    "unbound_question", "invalid_context_span", "incomplete_coverage", "orphan_question", "no_interpretation",
    "confirmation_repeated", "invalid_interpretation", "invalid_interpretation_wire_shape",
    "invalid_interpretation_wire_target", "invalid_interpretation_wire_value", "duplicate_interpretation_update",
})


class PrivateProtocolError(ValueError):
    pass


def encode_private(value, *, limit):
    try:
        _validate(value)
        raw = _encoded(value).encode("utf-8")
        if len(raw) > limit:
            raise ValueError()
        return raw
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise PrivateProtocolError("private_message_invalid") from None


def _decode(value, depth=0):
    if depth > 64 or not isinstance(value, list) or len(value) != 2:
        raise ValueError()
    tag, content = value
    if tag == "mapping":
        if not isinstance(content, list):
            raise ValueError()
        result = {}
        for pair in content:
            if not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str) or pair[0] in result:
                raise ValueError()
            result[pair[0]] = _decode(pair[1], depth + 1)
        return result
    if tag == "list" and isinstance(content, list):
        return [_decode(item, depth + 1) for item in content]
    if tag == "NoneType" and content is None:
        return None
    if tag == "bool" and type(content) is bool:
        return content
    if tag == "int" and type(content) is int:
        return content
    if tag == "float" and type(content) is float and math.isfinite(content):
        return content
    if tag == "str" and isinstance(content, str):
        return content
    if tag in ("decimal", "date", "datetime") and isinstance(content, str):
        result = {"decimal": Decimal, "date": date.fromisoformat, "datetime": datetime.fromisoformat}[tag](content)
        if isinstance(result, Decimal) and not result.is_finite():
            raise ValueError()
        return result
    raise ValueError()


def decode_private(raw, *, limit):
    try:
        if not isinstance(raw, bytes) or not 0 < len(raw) <= limit:
            raise ValueError()
        value = _decode(json.loads(raw.decode("utf-8")))
        _validate(value)
        return value
    except (ValueError, TypeError, OverflowError, RecursionError, DecimalException):
        raise PrivateProtocolError("private_message_invalid") from None

from datetime import date, datetime, timezone
from decimal import Decimal
import json

import pytest

from app.knowledge_changes import _encoded
from app.private_worker_protocol import PrivateProtocolError, decode_private, encode_private


def test_exact_roundtrip_preserves_types_and_does_not_treat_user_dict_as_a_tag():
    value = {"types": [None, True, False, 42, -1, 0.25, "中文",
                       Decimal("12345678901234567890.00000000000000000001"),
                       date(2026, 9, 11), datetime(2026, 9, 11, 1, tzinfo=timezone.utc)],
             "numeric_type": "decimal", "value": "ordinary strings", "nested": {"k": []}}
    assert _encoded(decode_private(encode_private(value, limit=5000), limit=5000)) == _encoded(value)


@pytest.mark.parametrize("value", [
    ["object", "code"], ["int", True], ["bool", 1], ["float", 1], ["str", None],
    ["decimal", "NaN"], ["decimal", "Infinity"], ["decimal", "invalid"],
    ["date", "tomorrow"], ["datetime", "2026-99-01"], ["NoneType", False],
    ["mapping", [["a", ["int", 1]], ["a", ["int", 2]]]],
    ["mapping", [[1, ["str", "value"]]]], ["mapping", {}], ["list", {}],
    {"tag": "str"}, ["float", float("nan")], ["float", float("inf")],
])
def test_malformed_typed_messages_fail_closed(value):
    with pytest.raises(PrivateProtocolError, match="^private_message_invalid$"):
        decode_private(json.dumps(value).encode(), limit=5000)


def test_limits_utf8_and_depth_are_checked():
    with pytest.raises(PrivateProtocolError): encode_private("x" * 100, limit=20)
    for raw in (b"", b"x" * 21, b"\xff", b"[", b"[\"list\"," * 1000):
        with pytest.raises(PrivateProtocolError): decode_private(raw, limit=20)
    value = ["int", 1]
    for _ in range(66): value = ["list", [value]]
    with pytest.raises(PrivateProtocolError): decode_private(json.dumps(value).encode(), limit=5000)
    with pytest.raises(PrivateProtocolError): encode_private({"bad": Decimal("NaN")}, limit=5000)

"""Public projection tests for host-computed reviewed business tables."""

import pytest

from app.chat_api import _public_business_table


def _table():
    return {
        "format": "table",
        "table_id": "model_verified",
        "reviewed_business_table": True,
        "complete_reconciled": True,
        "title": "两日实际销售出库增长",
        "caption": "两个完整自然日",
        "columns": [
            {"key": "cargo_owner", "label": "货主编码", "kind": "text"},
            {"key": "quantity_delta", "label": "实发增量", "kind": "number", "unit": "EA"},
            {"key": "optional_note", "label": "说明", "kind": "text", "nullable": True},
        ],
        "rows": [{
            "cargo_owner": "OWNER-1",
            "quantity_delta": "12.50",
            "optional_note": None,
        }],
        "notes": ["结果已完成对账"],
    }


def test_reviewed_dynamic_table_is_publicly_projected():
    public = _public_business_table(_table(), live=True)

    assert public is not None
    assert [column["key"] for column in public["columns"]] == ["col_1", "col_2", "col_3"]
    assert public["rows"] == [{"col_1": "OWNER-1", "col_2": "12.5", "col_3": None}]
    assert all("source_key" not in column for column in public["columns"])
    assert public["columns"][1]["unit"] == "EA"


def test_reviewed_dynamic_table_rejects_unchecked_cells():
    raw = _table()
    raw["rows"][0]["quantity_delta"] = "-1"

    assert _public_business_table(raw, live=True) is None


def test_percentage_and_explicit_signed_columns():
    raw = _table()
    raw["columns"][1].update(unit="%", signed=True)
    raw["rows"][0]["quantity_delta"] = "-1.25"
    public = _public_business_table(raw, live=True)
    assert public is not None
    assert public["columns"][1]["unit"] == "%"
    assert public["rows"][0]["col_2"] == "-1.25"


@pytest.mark.parametrize("mutation", ["unverified", "missing", "duplicate", "unsafe", "unit", "nan", "null"])
def test_invalid_reviewed_projection_fails_closed(mutation):
    raw = _table()
    if mutation == "unverified":
        raw["complete_reconciled"] = False
    elif mutation == "missing":
        del raw["rows"][0]["cargo_owner"]
    elif mutation == "duplicate":
        raw["columns"][1]["key"] = "cargo_owner"
    elif mutation == "unsafe":
        raw["rows"][0]["cargo_owner"] = "<script>"
    elif mutation == "unit":
        raw["columns"][1]["unit"] = "unreviewed"
    elif mutation == "nan":
        raw["rows"][0]["quantity_delta"] = "NaN"
    else:
        raw["rows"][0]["cargo_owner"] = None
    assert _public_business_table(raw, live=True) is None


def test_reviewed_projection_requires_live_evidence():
    assert _public_business_table(_table(), live=False) is None

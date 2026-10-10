"""Derived units are bounded declarations, never implicit value conversions."""

import pytest

from app.model_query_contract import result_columns, validate_business_projection


@pytest.mark.parametrize("unit", ["EA/单", "箱/单", "元/单", "元/EA", "元/千克", "单/日", "EA/周", "件/月"])
def test_known_ratio_units_preserve_real_value_and_declaration(unit):
    columns = [{"key": "value", "label": "业务指标", "kind": "number", "unit": unit}]
    assert result_columns(columns) == columns
    execution = {"columns": ["value"], "rows": [{"value": "123.45000"}]}
    validate_business_projection(execution, columns)
    assert execution["rows"] == [{"value": "123.45000"}]


@pytest.mark.parametrize("unit", ["EA/未知", "unknown/单", "EA/单/日", "EA/EA", "元/%", "元 / 单", "EA\n/单", "EA/SELECT"])
def test_ratio_unit_cannot_carry_unknown_atoms_or_expressions(unit):
    with pytest.raises(ValueError, match="unit"):
        result_columns([{"key": "value", "label": "业务指标", "kind": "number", "unit": unit}])


def test_ratio_unit_is_not_allowed_on_text_identity():
    with pytest.raises(ValueError):
        result_columns([{"key": "code", "label": "商品编码", "kind": "text", "unit": "EA/单"}])

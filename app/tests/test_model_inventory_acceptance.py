from decimal import Decimal

import pytest

from app.agent_core.planning import RequestPlanner
from tests.acceptance.check_model_inventory_live import expected, verify, verify_quality_response, QUESTION, LABELS


def fact(owner, available, transit):
    return dict(owner=owner, available=available, transit=transit, invalid_owner=0, fact_count=2)


def test_inventory_owner_summary_uses_the_reviewed_route():
    request = RequestPlanner().classify(QUESTION)
    assert request is not None
    assert request.route == "current_inventory_owner_summary"


def test_inventory_oracle_preserves_signed_components_zero_owners_and_full_share():
    output = expected([fact("01", 90, 10), fact("02", -10, 50), fact("03", 0, 0)])
    assert output[0][:4] == ("01", 90, 10, 100)
    assert output[0][4:] == (10, Decimal(100) / 140 * 100)
    assert output[1][:5] == ("02", -10, 50, 40, 125)
    assert output[2] == ("03", 0, 0, 0, None, 0)
    assert expected([fact("01", 0, 0)])[0][4:] == (None, None)


def test_negative_warehouse_rejection_requires_explicit_evidence_and_no_data():
    response = {"error": {"code": "inventory_quality_failed"}}
    verify_quality_response(response, True)
    with pytest.raises(AssertionError):
        verify_quality_response(response, False)
    with pytest.raises(AssertionError):
        verify_quality_response({"error": {"code": "query_failed"}}, True)
    with pytest.raises(AssertionError):
        verify_quality_response({**response, "business_table": {"rows": [1]}}, True)


@pytest.mark.parametrize("rows", [
    [fact("01", 1, 1), fact("01", 1, 1)], [fact("", 1, 1)],
    [fact("01", -2, 1)], [{**fact("01", 1, 1), "invalid_owner": 1}],
    [{**fact("01", 1, 1), "fact_count": 0}],
])
def test_invalid_baseline_cannot_be_accepted(rows):
    with pytest.raises(AssertionError):
        expected(rows)


@pytest.mark.parametrize("fault", ["drop", "duplicate", "order", "identity", "value", "unit", "ratio", "zero", "fixed"])
def test_inventory_verifier_rejects_incorrect_results(fault):
    baseline = expected([fact("01", 90, 10), fact("02", -10, 50), fact("03", 0, 0)])
    columns = [{"key": f"c{i}", "label": label, "kind": "text" if i == 0 else "number",
                **({"unit": "%" if i >= 4 else "EA"} if i else {})} for i, label in enumerate(LABELS)]
    table = {"table_id": "model_verified", "columns": columns}
    rows = [{f"c{i}": value for i, value in enumerate(row)} for row in baseline]
    verify(table, rows, baseline)
    if fault == "drop": rows.pop()
    if fault == "duplicate": rows.append(rows[0])
    if fault == "order": rows.reverse()
    if fault == "identity": rows[0]["c0"] = "1"
    if fault == "value": rows[1]["c1"] = 0
    if fault == "unit": columns[1]["unit"] = "箱"
    if fault == "ratio": rows[0]["c5"] = 100
    if fault == "zero": rows[2]["c4"] = 0
    if fault == "fixed": table["table_id"] = "inventory"
    with pytest.raises(AssertionError):
        verify(table, rows, baseline)

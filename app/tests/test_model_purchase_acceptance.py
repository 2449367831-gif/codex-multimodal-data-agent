from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from app.agent_core.planning import RequestPlanner
from tests.acceptance.check_model_purchase_live import LABELS, expected, question, verify
from tests.acceptance.check_purchase_receipts_live import independent_controls


def fact(unit, quantity, count=1):
    return dict(source_unit=unit, unit_total=quantity, detail_count=count,
                invalid_quantity_count=0, invalid_key_count=0)


def table_for(baseline):
    columns = [dict(key=f"c{i}", label=label, kind="text" if i == 0 else "number",
                    **({"unit": "个"} if i in (3, 4) else {})) for i, label in enumerate(LABELS)]
    rows = [{f"c{i}": value for i, value in enumerate(row)} for row in baseline]
    return {"table_id": "model_verified", "columns": columns}, rows


def test_purchase_composition_is_not_a_fixed_route():
    assert RequestPlanner().classify(question(date(2026, 9, 9), date(2026, 9, 10), date(2026, 9, 11))) is None


def test_oracle_never_combines_units_or_counts_only_nonzero_lines():
    baseline = expected([fact("001", 100, 2), fact("002", 1)],
                        [fact("001", 120, 4), fact("003", 0, 3)])
    assert baseline == [("001", 100, 120, 2, 4, 20, 30),
                        ("002", 1, 0, 1, 0, -1, None), ("003", 0, 0, 0, 3, 0, 0)]
    table, rows = table_for(baseline)
    verify(table, rows, baseline)


@pytest.mark.parametrize("field,value", [("source_unit", ""), ("source_unit", None),
    ("invalid_quantity_count", 1), ("invalid_key_count", 1), ("detail_count", 0),
    ("detail_count", "1.5"), ("unit_total", -1)])
def test_invalid_source_evidence_never_becomes_an_expected_answer(field, value):
    row = fact("001", 100)
    row[field] = value
    with pytest.raises((AssertionError, ValueError)):
        expected([row], [])


def test_arrival_cutoff_is_independent_of_production_period():
    sql = independent_controls(date(2026, 9, 9), date(2026, 9, 10), arrival_before=date(2026, 9, 11))
    assert "actual_arrival_date < '2026-09-11 00:00:00'" in sql
    assert "production_date < '2026-09-10 00:00:00'" in sql


@pytest.mark.parametrize("fault", ["drop", "duplicate", "order", "identity", "quantity", "count",
                                  "difference", "average", "unit", "fixed"])
def test_checker_rejects_wrong_or_incomplete_model_answers(fault):
    baseline = expected([fact("001", 100), fact("002", 15)], [fact("001", 102, 3), fact("002", 21)])
    table, rows = table_for(baseline)
    verify(table, rows, baseline)
    if fault == "drop": rows.pop()
    if fault == "duplicate": rows.append(deepcopy(rows[0]))
    if fault == "order": rows.reverse()
    if fault == "identity": rows[0]["c0"] = "1"
    if fault == "quantity": rows[0]["c2"] = 103
    if fault == "count": rows[0]["c4"] = 1
    if fault == "difference": rows[0]["c5"] = -2
    if fault == "average": rows[0]["c6"] = Decimal("34.01")
    if fault == "unit": table["columns"][1]["unit"] = "EA"
    if fault == "fixed": table["table_id"] = "purchase_summary"
    with pytest.raises(AssertionError):
        verify(table, rows, baseline)

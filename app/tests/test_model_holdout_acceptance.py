from datetime import date

import pytest

from tests.acceptance.check_model_holdout_live import (
    expected, question, verify, LABELS, expected_above_average, average_question, verify_above_average, AVERAGE_LABELS,
    expected_coverage, coverage_question, verify_coverage, COVERAGE_LABELS,
)
from app.agent_core.planning import RequestPlanner


FIRST, SECOND = date(2026, 9, 9), date(2026, 9, 10)


def fact(owner, warehouse, day, quantity):
    return {"owner": owner, "warehouse": warehouse, "day": str(day), "quantity": quantity,
            "fact_count": 1, "quantity_count": 1}


def test_holdout_cannot_pass_by_becoming_a_fixed_route():
    assert RequestPlanner().classify(question(FIRST, SECOND)) is None
    assert RequestPlanner().classify(average_question(FIRST, SECOND)) is None
    assert RequestPlanner().classify(coverage_question(FIRST, SECOND)) is None


def test_average_oracle_uses_all_recorded_warehouses_per_owner_before_filtering():
    rows = [fact("A", "01", FIRST, 100), fact("A", "01", SECOND, 100),
            fact("A", "02", FIRST, 0), fact("B", "01", FIRST, 200),
            fact("B", "02", FIRST, 400)]
    result = expected_above_average(rows, FIRST, SECOND)
    assert [(r[0], r[1], r[2], r[3]) for r in result] == [("A", "01", 200, 100), ("B", "02", 400, 300)]
    columns = [{"key": f"c{i}", "label": label, "kind": "text" if i < 2 else "number",
                **({"unit": "%" if i == 4 else "EA"} if i >= 2 else {})} for i, label in enumerate(AVERAGE_LABELS)]
    table = {"table_id": "model_verified", "columns": columns}
    output = [{f"c{i}": value for i, value in enumerate(row)} for row in result]
    verify_above_average(table, output, result)
    output[1]["c4"] = 100
    with pytest.raises(AssertionError, match="incorrect_preslice_share"):
        verify_above_average(table, output, result)


def test_oracle_preserves_identity_missing_days_filter_and_tie_order():
    rows = [fact("A", "01", FIRST, 10), fact("A", "01", SECOND, 12),
            fact("B", "01", SECOND, 2), fact("A", "02", FIRST, 20),
            fact("B", "02", FIRST, 2), fact("B", "02", SECOND, 2),
            fact("A", "03", SECOND, 5)]
    assert expected(rows, FIRST, SECOND) == [
        ("A", "03", 0, 5, 5), ("A", "01", 10, 12, 2), ("B", "01", 0, 2, 2)]


def test_missing_quantities_cannot_be_zero_filled():
    row = fact("A", "01", FIRST, 10)
    row["quantity_count"] = 0
    with pytest.raises(AssertionError, match="source_quantity_missing"):
        expected([row], FIRST, SECOND)


def test_coverage_keeps_crossing_row_and_unfiltered_denominator():
    rows = [fact('A', '01', FIRST, 60), fact('A', '02', FIRST, 30), fact('A', '03', FIRST, 10),
            fact('B', '01', FIRST, 80), fact('B', '02', FIRST, 20), fact('B', '00', FIRST, 0)]
    assert expected_coverage(rows, FIRST, SECOND) == [
        ('A', '01', 60, 60, 60), ('A', '02', 30, 90, 90), ('B', '01', 80, 80, 80)]


def test_coverage_aggregates_before_ranking_and_resolves_ties_by_identity():
    rows = [fact('A', '01', FIRST, 20), fact('A', '01', SECOND, 20),
            fact('A', '02', FIRST, 40), fact('A', '03', FIRST, 20), fact('A', '00', FIRST, -5)]
    assert expected_coverage(rows, FIRST, SECOND) == [
        ('A', '01', 40, 40, 40), ('A', '02', 40, 80, 80)]


def test_coverage_does_not_filter_on_rounded_percentage():
    rows = [fact('A', '01', FIRST, 79996), fact('A', '02', FIRST, 20004)]
    result = expected_coverage(rows, FIRST, SECOND)
    assert len(result) == 2 and result[-1][-1] == 100


@pytest.mark.parametrize('fault', ['drop', 'duplicate', 'sort', 'cumulative', 'denominator', 'identity', 'unit', 'fixed'])
def test_coverage_verifier_rejects_wrong_model_results(fault):
    baseline = [('A', '01', 60, 60, 60), ('A', '02', 30, 90, 90)]
    columns = [{'key': f'c{i}', 'label': label, 'kind': 'text' if i < 2 else 'number',
                **({'unit': '%' if i == 4 else 'EA'} if i >= 2 else {})} for i, label in enumerate(COVERAGE_LABELS)]
    table = {'table_id': 'model_verified', 'columns': columns}
    rows = [{f'c{i}': value for i, value in enumerate(row)} for row in baseline]
    verify_coverage(table, rows, baseline)
    if fault == 'drop': rows.pop()
    if fault == 'duplicate': rows.append(rows[0])
    if fault == 'sort': rows.reverse()
    if fault == 'cumulative': rows[1]['c3'] = 30
    if fault == 'denominator': rows[1]['c4'] = 100
    if fault == 'identity': rows[0]['c0'] = 'B'
    if fault == 'unit': columns[2]['unit'] = '箱'
    if fault == 'fixed': table['table_id'] = 'sales_aggregate'
    with pytest.raises(AssertionError): verify_coverage(table, rows, baseline)


def test_only_exact_requested_date_labels_can_replace_relative_day_labels():
    labels = [*LABELS[:2], "9月9日实发数量", "9月10日实发数量", LABELS[-1]]
    columns = [{"key": f"c{i}", "label": label, "kind": "text" if i < 2 else "number",
                **({"unit": "EA"} if i >= 2 else {})} for i, label in enumerate(labels)]
    table = {"table_id": "model_verified", "columns": columns}
    baseline = [("A", "01", 1, 5, 4)]
    rows = [{f"c{i}": value for i, value in enumerate(baseline[0])}]
    verify(table, rows, baseline, first=FIRST, second=SECOND)
    columns[2]["label"] = "9月8日实发数量"
    with pytest.raises(AssertionError, match="incorrect_business_columns"):
        verify(table, rows, baseline, first=FIRST, second=SECOND)


@pytest.mark.parametrize("fault", ["drop", "duplicate", "order", "value", "identity", "unit", "fixed"])
def test_verifier_rejects_incomplete_or_wrong_model_results(fault):
    baseline = [("A", "01", 1, 5, 4), ("B", "02", 2, 3, 1)]
    columns = [{"key": f"c{i}", "label": label, "kind": "text" if i < 2 else "number",
                **({"unit": "EA"} if i >= 2 else {})} for i, label in enumerate(LABELS)]
    rows = [{f"c{i}": value for i, value in enumerate(row)} for row in baseline]
    table = {"table_id": "model_verified", "columns": columns}
    verify(table, rows, baseline)
    if fault == "drop": rows.pop()
    if fault == "duplicate": rows.append(rows[0])
    if fault == "order": rows.reverse()
    if fault == "value": rows[0]["c4"] = 5
    if fault == "identity": rows[0]["c0"] = "B"
    if fault == "unit": columns[2]["unit"] = "箱"
    if fault == "fixed": table["table_id"] = "sales_aggregate"
    with pytest.raises(AssertionError):
        verify(table, rows, baseline)

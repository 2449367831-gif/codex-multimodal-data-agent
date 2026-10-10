"""Synthetic evidence must prove a complete, deterministic Top-N result."""

from copy import deepcopy
from datetime import date

import pytest

from app.agent_core.semantic_types import SemanticRequest, SemanticRuntimeError
from app.tests.test_semantic_runtime import RecordingQueryClient, runtime


@pytest.fixture
def ranking_rows():
    return [dict(sku_code=code, sku_name=code, outbound_ea=quantity,
                 outbound_order_count=2, total_ea="200", all_sku_count=2,
                 blank_sku_count=0, sku_name_conflict_count=0, negative_detail_count=0)
            for code, quantity in [("A", "120"), ("B", "80")]]


def run(rows, top_n=20):
    client = RecordingQueryClient({"actual sales outbound top sku": deepcopy(rows)})
    return runtime(client).execute_plan(
        SemanticRequest(route="sales_outbound_top_sku", start=date(2026, 9, 1),
                        end=date(2026, 9, 2), show_top_sku=True, top_n=top_n),
        question="Synthetic ranking integrity test",
    )


def test_complete_ranking_and_legitimate_short_population(ranking_rows):
    assert len(run(ranking_rows)["business_table"]["rows"]) == 2
    assert len(run(ranking_rows[:1], top_n=1)["business_table"]["rows"]) == 1
    assert run([])["business_table"]["rows"] == []


def test_missing_rank_is_not_a_complete_result(ranking_rows):
    with pytest.raises(SemanticRuntimeError):
        run(ranking_rows[:1])


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("field", [
    "all_sku_count", "blank_sku_count", "sku_name_conflict_count",
    "negative_detail_count", "outbound_order_count",
])
@pytest.mark.parametrize("bad", [None, "", "NaN", "Infinity", "-1", "0.5", True])
def test_every_count_is_present_finite_nonnegative_and_integral(ranking_rows, index, field, bad):
    ranking_rows[index][field] = bad
    with pytest.raises(SemanticRuntimeError):
        run(ranking_rows)


@pytest.mark.parametrize("field", ["outbound_ea", "total_ea"])
@pytest.mark.parametrize("bad", [None, "", "NaN", "Infinity", True])
def test_quantities_cannot_default_missing_evidence_to_zero(ranking_rows, field, bad):
    ranking_rows[1][field] = bad
    with pytest.raises(SemanticRuntimeError):
        run(ranking_rows)


def test_all_sku_total_must_equal_rows_when_population_fits(ranking_rows):
    for row in ranking_rows:
        row["total_ea"] = "201"
    with pytest.raises(SemanticRuntimeError):
        run(ranking_rows)


def test_equal_quantities_follow_stable_sku_order(ranking_rows):
    for row in ranking_rows:
        row["outbound_ea"] = "100"
    assert len(run(ranking_rows)["business_table"]["rows"]) == 2
    with pytest.raises(SemanticRuntimeError):
        run(list(reversed(ranking_rows)))


def test_negative_detail_control_cannot_change_after_first_row(ranking_rows):
    ranking_rows[1]["negative_detail_count"] = 1
    with pytest.raises(SemanticRuntimeError):
        run(ranking_rows)


@pytest.mark.parametrize("top_n", [0, -1, 101, 1.5, True])
def test_invalid_rank_bounds_are_not_clamped_or_defaulted(ranking_rows, top_n):
    with pytest.raises(SemanticRuntimeError):
        run(ranking_rows, top_n)

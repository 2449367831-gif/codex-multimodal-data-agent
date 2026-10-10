from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

from app.tests.test_sales_drilldown import source, facts, candidate_scope_rules, plan, query, checked


@pytest.fixture
def diagnosis():
    spec = spec_from_file_location("name_diagnosis", Path(__file__).resolve().parents[2] / "tests/acceptance/check_sales_drilldown_names_live.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_profiles_cover_every_ranked_owner_warehouse_without_document_fanout(source, diagnosis):
    actual = query(source, diagnosis.profile_sql(plan()))
    expected = query(source, diagnosis.profile_sql(plan(), reference=True))
    assert len(actual) == 3
    checks = diagnosis.compare_profiles(actual, expected)
    assert checks["same_keys"] and checks["same_names"]
    assert checks["product_name_differences"] == checks["warehouse_name_differences"] == 0


@pytest.mark.parametrize("field", ["sku_name", "warehouse_name"])
def test_profile_mismatch_is_classified_without_outputting_business_rows(source, diagnosis, field):
    actual = query(source, diagnosis.profile_sql(plan()))
    expected = [dict(row) for row in actual]
    expected[0][field] = "Different source name"
    checks = diagnosis.compare_profiles(actual, expected)
    assert checks["same_keys"] and not checks["same_names"]
    assert checks["product_name_differences"] == int(field == "sku_name")
    assert checks["warehouse_name_differences"] == int(field == "warehouse_name")
    assert all(type(value) in (bool, int) for value in checks.values())


def test_conflicting_profile_names_do_not_collapse_to_an_arbitrary_record(source, diagnosis):
    rows = query(source, diagnosis.profile_sql(plan()))
    with pytest.raises(ValueError, match="conflicting"):
        diagnosis.compare_profiles(rows + [dict(rows[0], warehouse_name="Conflict")], rows)


def test_profile_missing_keys_are_not_equal(source, diagnosis):
    rows = query(source, diagnosis.profile_sql(plan()))
    assert not diagnosis.compare_profiles(rows, rows[1:])["same_keys"]


@pytest.mark.parametrize("field", ["sku_name", "warehouse_name"])
def test_detail_diagnostic_retains_precise_failure_kind_only(source, diagnosis, field):
    snapshot = checked(source)
    reference = query(source, diagnosis.reference_module().reference_sql(plan()))
    reference[0][field] = "Wrong display"
    result = diagnosis.detail_name_diagnostics(snapshot, reference)
    assert result["product_name_differences"] == int(field == "sku_name")
    assert result["warehouse_name_differences"] == int(field == "warehouse_name")
    assert all(type(value) is int for value in result.values())


def test_detail_diagnosis_rejects_missing_keys(source, diagnosis):
    snapshot = checked(source)
    reference = query(source, diagnosis.reference_module().reference_sql(plan()))
    with pytest.raises(ValueError, match="matching detail keys"):
        diagnosis.detail_name_diagnostics(snapshot, reference[1:])

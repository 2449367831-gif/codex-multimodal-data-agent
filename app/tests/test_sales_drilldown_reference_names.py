"""Independent display-name checks must detect errors even when totals agree."""

from dataclasses import replace
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

from app.agent_core.sales_drilldown import retrieve_sales_ranking
from app.tests.test_sales_drilldown import source, facts, candidate_scope_rules, plan, query, checked


@pytest.fixture
def reference():
    spec = spec_from_file_location("name_reference", Path(__file__).resolve().parents[2] / "tests/acceptance/check_sales_drilldown_live.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_independent_ranking_detects_wrong_name_with_identical_quantities(source, reference):
    ranking = retrieve_sales_ranking(plan(), lambda sql, **kw: query(source, sql))
    rows = query(source, reference.reference_ranking_sql(plan()))
    assert reference.compare_ranking(ranking, rows)["ranking_display_names"]
    wrong = replace(ranking, ranking=(replace(ranking.ranking[0], sku_name="Wrong product"),))
    checks = reference.compare_ranking(wrong, rows)
    assert checks["ranking_and_distinct_counts"]
    assert not checks["ranking_display_names"]


@pytest.mark.parametrize("field", ["warehouse_name", "sku_name"])
def test_detail_name_corruption_cannot_pass_numeric_reconciliation(source, reference, field):
    snapshot = checked(source)
    rows = query(source, reference.reference_sql(plan()))
    assert all(reference.compare(snapshot, rows).values())
    rows[0][field] = "Wrong name"
    checks = reference.compare(snapshot, rows)
    assert checks["all_sales_quantity"] and checks["every_sku_drilldown_reconciles"]
    assert not checks["every_display_name"]
    assert checks["sku_display_names"] == (field != "sku_name")
    assert checks["warehouse_display_names"] == (field != "warehouse_name")


@pytest.mark.parametrize("scenario", ["duplicate", "other_owner", "inactive", "null_status", "missing_warehouse", "blank_warehouse", "missing_product_name", "whitespace"])
def test_source_name_rules_are_independently_reproduced(source, reference, scenario):
    if scenario == "duplicate":
        source.execute("INSERT INTO ywzt_ods.master_wms SELECT * FROM ywzt_ods.master_wms")
        source.execute("INSERT INTO ywzt_ods.master_sku SELECT * FROM ywzt_ods.master_sku")
    elif scenario == "other_owner":
        source.execute("UPDATE ywzt_ods.master_wms SET wms_desc='Another owner warehouse' WHERE cargo_owner_id='B'")
    elif scenario == "inactive":
        source.execute("INSERT INTO ywzt_ods.master_wms VALUES ('HJXH','W1','Inactive label','1')")
    elif scenario == "null_status":
        source.execute("UPDATE ywzt_ods.master_wms SET status=NULL")
    elif scenario == "missing_warehouse":
        source.execute("DELETE FROM ywzt_ods.master_wms WHERE cargo_owner_id='B'")
    elif scenario == "blank_warehouse":
        source.execute("INSERT INTO ywzt_ods.master_wms VALUES ('HJXH','W1',' ','0')")
    elif scenario == "missing_product_name":
        source.execute("UPDATE ywzt_ods.master_sku SET sku_name=NULL")
    else:
        source.execute("UPDATE ywzt_ods.master_sku SET sku_name='  Padded product  '")
        source.execute("UPDATE ywzt_ods.master_wms SET wms_desc='  Padded warehouse  '")
    snapshot = checked(source)
    rows = query(source, reference.reference_sql(plan()))
    assert all(reference.compare(snapshot, rows).values())
    assert {r["sku_name"] for r in rows} == {r.sku_name for r in snapshot.ranking}
    assert {(r["owner"], r["warehouse_code"], r["warehouse_name"]) for r in rows} == {
        (r.cargo_owner, r.warehouse_code, r.warehouse_name) for r in snapshot.details
    }


@pytest.mark.parametrize("kind", ["sku", "warehouse"])
def test_reference_does_not_hide_conflicting_names_with_min(source, reference, kind):
    snapshot = checked(source)
    if kind == "sku":
        source.execute("INSERT INTO ywzt_ods.master_sku VALUES ('A','Conflict','50')")
    else:
        source.execute("INSERT INTO ywzt_ods.master_wms VALUES ('HJXH','W1','Conflict','0')")
    with pytest.raises(ValueError, match="duplicate detail keys"):
        reference.compare(snapshot, query(source, reference.reference_sql(plan())))


def test_reference_missing_names_are_detected_not_omitted(source, reference):
    snapshot = checked(source)
    rows = query(source, reference.reference_sql(plan()))
    del rows[0]["warehouse_name"]
    with pytest.raises(KeyError):
        reference.compare(snapshot, rows)

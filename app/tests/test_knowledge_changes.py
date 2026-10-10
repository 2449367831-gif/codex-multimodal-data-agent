from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
import json

import pytest

from app.knowledge_changes import assess_changes, KnowledgeSnapshot


def metric(name="inventory.available", **overrides):
    return {"id": name, "version": "1.0.0", "formula": "stock - sales",
            "grain": "owner + warehouse + sku", "unit": "EA", "time_window": {"timezone": "Asia/Shanghai"},
            "sources": {"required": ["warehouse.stock"]}, "depends_on": [], "dependency_versions": {},
            "required_validations": ["inventory_key_aggregation"], **overrides}


def source(**overrides):
    return {"id": "warehouse.stock", "version": "1.0.0", "fields": {"quantity": {"type": "decimal"}}, **overrides}


def snapshot(metrics=None, sources=None, templates=None):
    return KnowledgeSnapshot(metrics=metrics if metrics is not None else [metric()],
                             sources=sources if sources is not None else [source()],
                             templates=templates or {})


def test_same_name_formula_change_is_not_metadata_only_and_requires_history_review():
    old = snapshot()
    new = snapshot([metric(formula="stock - sales - transfer")])
    report = assess_changes(old, new)
    assert report["state"] == "assessed_not_executed"
    assert report["affected_metrics"] == ["inventory.available"]
    assert report["changes"][0]["fields"][0]["path"] == "/formula"
    assert report["changes"][0]["requires_version_change"]
    assert report["historical_comparability"] == "requires_review"
    assert {"quantity_reconciliation", "document_count_deduplication", "inventory_component_recalculation"} <= set(report["required_checks"])
    assert report["publication_authorized"] is False


def test_multilevel_dependencies_use_both_old_and_new_graph_without_losing_removed_edges():
    child = metric("inventory.total", depends_on=["inventory.available"], dependency_versions={"inventory.available": "1.0.0"})
    grandchild = metric("inventory.turnover", depends_on=["inventory.total"], dependency_versions={"inventory.total": "1.0.0"})
    other = metric("sales.unrelated", sources={"required": []})
    old = snapshot([metric(), child, grandchild, other])
    new = snapshot([metric(formula="stock - sales - transfer"), {**child, "depends_on": [], "dependency_versions": {}}, grandchild, other])
    report = assess_changes(old, new)
    assert report["affected_metrics"] == ["inventory.available", "inventory.total", "inventory.turnover"]
    root = next(change for change in report["changes"] if change["id"] == "inventory.available")
    assert root["affected_metrics"] == report["affected_metrics"]


def test_source_field_change_reaches_every_source_consumer_and_derived_metric():
    derived = metric("inventory.turnover", sources={"required": []}, depends_on=["inventory.available"], dependency_versions={"inventory.available": "1.0.0"})
    old = snapshot([metric(), derived])
    new = snapshot([metric(), derived], [source(fields={"quantity": {"type": "string"}})])
    report = assess_changes(old, new)
    assert report["changes"][0]["kind"] == "source"
    assert report["changes"][0]["fields"][0]["path"] == "/fields/quantity/type"
    assert report["affected_metrics"] == ["inventory.available", "inventory.turnover"]
    assert "source_schema_compatibility" in report["required_checks"]


def test_source_deletion_preserves_old_consumers_and_reports_dangling_dependency():
    report = assess_changes(snapshot(), snapshot(sources=[]))
    assert report["changes"][0]["operation"] == "removed"
    assert report["affected_metrics"] == ["inventory.available"]
    assert any(issue["code"] == "missing_source" and issue["side"] == "after" for issue in report["issues"])


def test_exact_json_types_and_missing_are_not_equal_to_null_or_boolean():
    old = snapshot([metric(filters={"zero": 0, "enabled": True, "missing": None})])
    new = snapshot([metric(filters={"zero": False, "enabled": 1, "added": None})])
    fields = assess_changes(old, new, include_values=True)["changes"][0]["fields"]
    assert {f["path"] for f in fields} == {"/filters/zero", "/filters/enabled", "/filters/missing", "/filters/added"}
    added = next(f for f in fields if f["path"] == "/filters/added")
    assert added["before_exists"] is False and added["after_exists"] is True
    assert added["after"] is None


def test_mapping_order_is_irrelevant_but_predicate_list_order_is_preserved():
    old = snapshot([metric(filters={"a": 1, "b": ["first", "second"]})])
    same = snapshot([metric(filters={"b": ["first", "second"], "a": 1})])
    assert assess_changes(old, same)["changes"] == []
    changed = snapshot([metric(filters={"b": ["second", "first"], "a": 1})])
    assert assess_changes(old, changed)["changes"][0]["fields"][0]["path"] == "/filters/b"


def test_update_timestamp_alone_does_not_require_business_recalculation():
    report = assess_changes(snapshot([metric(updated_at="2026-09-10")]), snapshot([metric(updated_at="2026-09-11")]))
    assert report["affected_metrics"] == []
    assert report["changes"][0]["fields"][0]["category"] == "metadata"
    assert report["historical_comparability"] == "not_established"


def test_unknown_contract_fields_and_notes_are_not_silently_ignored():
    report = assess_changes(snapshot(), snapshot([metric(new_rule="exclude transfer", notes="changed definition")]))
    assert {field["path"] for field in report["changes"][0]["fields"]} == {"/new_rule", "/notes"}
    assert all(f["category"] == "semantic" for f in report["changes"][0]["fields"])


def test_changed_template_bytes_are_reported_without_revealing_sql():
    m = metric(execution={"mode": "template", "template": "sql_templates/stock.sql"})
    old = snapshot([m], templates={"sql_templates/stock.sql": "a" * 64})
    new = snapshot([m], templates={"sql_templates/stock.sql": "b" * 64})
    report = assess_changes(old, new)
    assert report["affected_metrics"] == [m["id"]]
    assert report["changes"][0]["kind"] == "template"
    assert "implementation_contract_alignment" in report["required_checks"]
    assert report["changes"][0]["requires_version_change"] is False
    assert "consumer_version_review" in report["required_checks"]


@pytest.mark.parametrize("overrides,code", [
    ({"execution": "composite"}, "invalid_execution"),
    ({"execution": {"template": []}}, "invalid_template"),
    ({"required_validations": "inventory_key_aggregation"}, "invalid_required_validations"),
    ({"required_validations": [None]}, "invalid_required_validations"),
])
def test_malformed_dependency_inputs_cannot_claim_complete_assessment(overrides, code):
    report = assess_changes(snapshot(), snapshot([metric(**overrides)]))
    assert report["assessment_complete"] is False
    assert any(issue["side"] == "after" and issue["code"] == code for issue in report["issues"])


def test_cycles_and_pin_mismatches_are_reported_without_infinite_traversal():
    a = metric("a", depends_on=["b"], dependency_versions={"b": "2.0.0"})
    b = metric("b", depends_on=["a"], dependency_versions={"a": "1.0.0"})
    report = assess_changes(snapshot([a, b]), snapshot([{**a, "formula": "new"}, b]))
    assert report["affected_metrics"] == ["a", "b"]
    assert {i["code"] for i in report["issues"]} >= {"dependency_cycle", "dependency_version_mismatch"}
    assert report["assessment_complete"] is False


def test_graph_union_does_not_invent_a_cycle_between_valid_individual_versions():
    old = snapshot([metric("a"), metric("b", depends_on=["a"], dependency_versions={"a": "1.0.0"})])
    new = snapshot([metric("b"), metric("a", depends_on=["b"], dependency_versions={"b": "1.0.0"})])
    assert not any(i["code"] == "dependency_cycle" for i in assess_changes(old, new)["issues"])


def test_report_defaults_to_value_free_output_and_inputs_are_unchanged():
    value = metric(formula="PRIVATE_FORMULA")
    saved = deepcopy(value)
    before = snapshot()
    after = snapshot([value])
    public = assess_changes(before, after)
    assert "PRIVATE_FORMULA" not in json.dumps(public)
    internal = assess_changes(before, after, include_values=True)
    assert "PRIVATE_FORMULA" in json.dumps(internal)
    assert value == saved
    assert public == assess_changes(before, after)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), {1: "numeric key"}, {"x": object()}])
def test_invalid_contract_values_fail_without_echo(bad):
    with pytest.raises(ValueError, match="invalid_contract_value"):
        snapshot([metric(filters=bad)])


def test_duplicate_selected_versions_do_not_silently_overwrite():
    with pytest.raises(ValueError, match="duplicate_selected_contract"):
        snapshot([metric(), metric(version="2.0.0")])


@pytest.mark.parametrize("value", [date(2026, 9, 10), datetime(2026, 9, 10, tzinfo=timezone.utc)])
def test_yaml_temporal_values_are_supported_without_becoming_plain_strings(value):
    old = snapshot([metric(parameters={"date": {"examples": [value]}})])
    same = snapshot([metric(parameters={"date": {"examples": [value]}})])
    new = snapshot([metric(parameters={"date": {"examples": [value.isoformat()]}})])
    assert assess_changes(old, same)["changes"] == []
    report = assess_changes(old, new, include_values=True)
    field = report["changes"][0]["fields"][0]
    assert field["before_sha256"] != field["after_sha256"]
    assert field["before"][0] == {"yaml_type": type(value).__name__, "iso8601": value.isoformat()}
    json.dumps(report)


def test_pins_and_explicit_empty_composite_dependencies_are_checked():
    no_declaration = metric(execution={"mode": "composite"})
    del no_declaration["depends_on"]
    report = assess_changes(snapshot(), snapshot([no_declaration]))
    assert any(i["code"] == "missing_dependency_declaration" for i in report["issues"])
    okay = snapshot([metric(execution={"mode": "composite"})])
    assert not assess_changes(okay, okay)["issues"]


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity")])
def test_nonfinite_decimal_is_not_a_valid_contract_value(value):
    with pytest.raises(ValueError, match="invalid_contract_value"):
        snapshot([metric(filters={"threshold": value})])


def test_decimal_thresholds_do_not_collide_with_strings_or_float_values():
    exact = Decimal("0.123456789012345678901")
    old = snapshot([metric(filters={"threshold": exact})])
    for value in (str(exact), float(exact)):
        report = assess_changes(old, snapshot([metric(filters={"threshold": value})]), include_values=True)
        field = report["changes"][0]["fields"][0]
        assert field["before_sha256"] != field["after_sha256"]
        assert field["before"] == {"numeric_type": "decimal", "value": str(exact)}
        json.dumps(report)

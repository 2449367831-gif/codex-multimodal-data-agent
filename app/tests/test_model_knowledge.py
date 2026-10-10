from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.model_knowledge import for_request


def snapshot():
    return {
        "metrics": [
            {"id": "stock", "aliases": ["stock"], "business_concept": "stock", "formula": "available + transit"},
            {"id": "sales", "aliases": ["sales"], "formula": "actual", "template": "sales.sql", "sources": {"required": ["source"]}},
            {"id": "turnover.live", "aliases": ["turnover"], "business_concept": "turnover", "dependency_versions": {"stock": "1", "sales": "1"}},
            {"id": "turnover.snapshot", "aliases": ["historical turnover"], "business_concept": "turnover", "dependency_versions": {"stock": "1", "sales": "1"}},
        ],
        "templates": [{"path": "sales.sql", "content": "full template"}],
        "sources": [{"id": "source", "grain": "owner+sku", "join": "owner+order"}],
        "rules": {
            "rules/agent-policy.yaml": {"scope": "ALL"},
            "rules/query-scope.yaml": {"reconciliation": "required"},
            "rules/knowledge-lifecycle.yaml": {"history": "index only"},
        },
        "imported_sql_references": [{"path": "selected.sql"}, {"path": "unrelated.sql"}],
        "imported_logic_catalog": {"logic_families": [
            {"id": "family", "status": "candidate", "source_files": ["selected.sql"]},
            {"id": "other", "status": "candidate", "source_files": ["unrelated.sql"]},
        ]},
        "request_routing_hint": {"candidates": [{"family_id": "family"}]},
        "golden_tests": {"not_prompt_data": True},
        "imported_thread_references": [{"content": "historical notes"}],
    }


def test_retrieval_keeps_competing_variants_and_all_dependencies():
    original = snapshot()
    before = deepcopy(original)
    selected = for_request(original, "historical turnover")
    assert set(selected["retrieval"]["loaded_metric_ids"]) == {"turnover.live", "turnover.snapshot", "stock", "sales"}
    assert selected["metrics"] == original["metrics"]
    assert selected["templates"] == original["templates"]
    assert original == before


def test_unmatched_wording_keeps_all_contracts_not_a_guessed_metric():
    original = snapshot()
    selected = for_request(original, "an unfamiliar expression")
    assert selected["retrieval"]["mode"] == "all_selected_contracts"
    assert selected["metrics"] == original["metrics"]


def test_business_synonym_retrieves_its_contract_without_claiming_a_variant():
    selected = for_request(snapshot(), "昨天各仓库销售完成量")
    assert selected["retrieval"]["mode"] == "matched_contracts_and_dependencies"
    assert [metric["id"] for metric in selected["metrics"]] == ["sales"]


def test_purchase_question_retrieves_purchase_candidates_not_every_metric():
    original = snapshot()
    original["metrics"].extend([
        {"id": "purchase.received_detail", "business_concept": "purchase.receipt_quantity",
         "variant": "production_window_detail", "aliases": ["采购入库明细"]},
        {"id": "purchase.received_summary", "business_concept": "purchase.receipt_quantity",
         "variant": "actual_receipt_time"},
    ])
    selected = for_request(original, "查询本月采购入库数量最高的前10个SKU")
    assert set(selected["retrieval"]["loaded_metric_ids"]) == {
        "purchase.received_detail", "purchase.received_summary",
    }
    assert selected["retrieval"]["selection_authority"] == "model"
    assert selected["retrieval"]["candidates_only"] is True
    assert selected["retrieval"]["execution_policy"] == "model_selects_before_execution"
    assert selected["request_assessment"]["authority"] == "retrieval_hint_only"
    assert selected["request_assessment"]["binding"] is False
    assert len(selected["metric_index"]) == len(original["metrics"])


def test_projection_keeps_full_relevant_contracts_and_complete_discovery_index():
    original = snapshot()
    selected = for_request(original, "sales")
    assert selected["metrics"] == [original["metrics"][1]]
    assert len(selected["metric_index"]) == len(original["metrics"])
    assert selected["sources"] == original["sources"]
    assert set(selected["rules"]) == {
        "rules/agent-policy.yaml", "rules/query-scope.yaml",
    }
    assert selected["imported_sql_references"] == []
    assert len(selected["imported_logic_catalog"]["family_index"]) == 2
    assert selected["imported_logic_catalog"]["logic_families"][0]["status"] == "candidate"


def test_historical_request_retains_selected_imported_sql_evidence():
    selected = for_request(snapshot(), "按历史原口径复现销售")
    assert selected["imported_sql_references"] == [{"path": "selected.sql"}]


def test_warehouse_lookup_loads_source_contract_without_unrelated_metric_menu():
    original = snapshot()
    original["sources"].append({"id": "ywzt_ods.master_wms", "lookup": {"scope": "ALL"}})
    selected = for_request(original, "查询名称包含沈阳的仓库，返回仓库编码和名称")
    assert selected["request_assessment"]["kind"] == "lookup"
    assert selected["request_assessment"]["requires_time_window"] is False
    assert selected["metrics"] == []
    assert selected["sources"] == [original["sources"][-1]]
    assert len(selected["source_index"]) == 2
    assert "rules/query-scope.yaml" in selected["rules"]


def test_optional_dimensions_are_not_lost_when_a_metric_matches():
    original = snapshot()
    optional = {"id": "ywzt_ods.master_store", "grain": ["owner", "store"], "fields": ["platform_code", "group1"]}
    original["sources"].append(optional)
    selected = for_request(original, "sales 按客户平台拆分")
    assert optional in selected["sources"]
    assert selected["sources"][0] == original["sources"][0]


def test_missing_dependency_is_reported_not_silently_treated_as_loaded():
    original = snapshot()
    original["metrics"][0]["dependency_versions"] = {"missing.contract": "1"}
    selected = for_request(original, "stock")
    assert selected["retrieval"]["missing_metric_dependencies"] == ["missing.contract"]


def test_unpinned_transitive_dependencies_keep_complete_contracts_and_templates():
    original = snapshot()
    original["metrics"][0]["depends_on"] = ["sales"]
    original["metrics"][1]["depends_on"] = ["stock"]
    before = deepcopy(original)
    selected = for_request(original, "stock")
    assert set(selected["retrieval"]["loaded_metric_ids"]) == {"stock", "sales"}
    assert selected["templates"] == original["templates"]
    assert selected["sources"] == original["sources"]
    assert selected["retrieval"]["dependency_context_complete"] is True
    assert original == before


def test_dependency_version_mismatch_is_reported_without_substitution():
    original = snapshot()
    original["metrics"][0].update(version="2.0.0", dependency_versions={"sales": "1.0.0"})
    original["metrics"][1]["version"] = "2.0.0"
    selected = for_request(original, "stock")
    assert selected["retrieval"]["dependency_version_mismatches"] == [{
        "metric_id": "stock", "dependency_id": "sales",
        "required_version": "1.0.0", "loaded_version": "2.0.0",
    }]
    assert selected["retrieval"]["dependency_context_complete"] is False
    assert selected["metrics"][1] == original["metrics"][1]


def test_required_source_and_template_gaps_are_reported_together():
    original = snapshot()
    original["metrics"][1]["depends_on"] = ["missing.contract"]
    original["sources"] = []
    original["templates"] = []
    selected = for_request(original, "sales")
    assert selected["retrieval"]["missing_metric_dependencies"] == ["missing.contract"]
    assert selected["retrieval"]["missing_source_contracts"] == ["source"]
    assert selected["retrieval"]["missing_template_refs"] == ["sales.sql"]
    assert selected["retrieval"]["dependency_context_complete"] is False


def test_unrelated_dependency_gaps_do_not_affect_selected_metric():
    original = snapshot()
    original["metrics"][0]["depends_on"] = ["missing.contract"]
    selected = for_request(original, "sales")
    assert selected["retrieval"]["dependency_context_complete"] is True


def test_required_template_can_be_declared_in_execution_only():
    original = snapshot()
    original["metrics"][1].pop("template")
    original["metrics"][1]["execution"] = {"mode": "sql_template", "template": "missing.sql"}
    selected = for_request(original, "sales")
    assert selected["retrieval"]["missing_template_refs"] == ["missing.sql"]


def test_active_knowledge_projection_keeps_all_declared_dependencies():
    from app.codex_agent import codex_knowledge_context

    active = json.loads(codex_knowledge_context(Path(__file__).resolve().parents[2]))
    assert active["metrics"]
    for metric in active["metrics"]:
        selected = for_request(active, metric["id"])
        assert metric in selected["metrics"]
        assert selected["retrieval"]["dependency_context_complete"], (
            metric["id"], selected["retrieval"],
        )


def legacy_snapshot():
    value = snapshot()
    value["rules"]["rules/thread-derived-semantics.yaml"] = {
        "provenance": {"source_thread_id": "source-thread", "read_status": "complete"},
        "metric_families": {"stock": {"numerator": "stale-inventory-formula"}},
        "semantic_assertions": [{"metric_ref": "stock@old", "expected": "old-version"}],
    }
    return value


@pytest.mark.parametrize("question", ["stock", "sales", "历史库存", "查询沈阳的仓库", "unknown metric"])
def test_historical_semantics_never_enter_current_execution_rules(question):
    value = legacy_snapshot()
    before = deepcopy(value)
    selected = for_request(value, question)
    assert "rules/thread-derived-semantics.yaml" not in selected["rules"]
    assert "stale-inventory-formula" not in json.dumps(selected)
    assert selected["historical_semantic_evidence"] == []
    assert selected["execution_authority"]["current_release_overrides_history"] is True
    assert selected["historical_semantic_index"] == [{
        "path": "rules/thread-derived-semantics.yaml", "authority": "historical_evidence_only",
        "source_thread_id": "source-thread", "loaded": False,
    }]
    assert value == before


@pytest.mark.parametrize("question", ["按历史SQL复现库存", "核对旧口径", "比较历史规则", "W37参考表"])
def test_explicit_historical_review_receives_evidence_not_current_rules(question):
    selected = for_request(legacy_snapshot(), question)
    assert "rules/thread-derived-semantics.yaml" not in selected["rules"]
    evidence = selected["historical_semantic_evidence"]
    assert len(evidence) == 1 and evidence[0]["authority"] == "historical_evidence_only"
    assert evidence[0]["document"]["metric_families"]["stock"]["numerator"] == "stale-inventory-formula"
    assert selected["historical_semantic_index"][0]["loaded"] is True
    assert any(m["formula"] == "available + transit" for m in selected["metrics"] if "formula" in m)


def test_historical_date_is_not_permission_to_load_legacy_sql():
    selected = for_request(snapshot(), "查询历史库存和sales")
    assert selected["imported_sql_references"] == []


@pytest.mark.parametrize("question", [
    "sales，返回五列业务表格", "按业务表格展示昨天实际销售出库", "复现昨天查询结果的业务表格",
    "用业务表格列出历史库存", "按已发布口径返回完整业务表格", "销售出库复现",
])
def test_presentation_and_repeat_requests_do_not_load_old_business_formulas(question):
    original = legacy_snapshot()
    before = deepcopy(original)
    selected = for_request(original, question)
    assert not selected["historical_semantic_evidence"]
    assert not selected["imported_sql_references"]
    assert "stale-inventory-formula" not in json.dumps(selected)
    assert original == before


def test_request_projection_drops_unrelated_profiles_and_scopes_validation_rules():
    from app.codex_agent import codex_knowledge_context

    context = json.loads(codex_knowledge_context(
        Path(__file__).resolve().parents[2],
        question="查询昨天各仓库实际销售出库量，按仓库汇总",
        compact=True,
    ))
    assert context["profiles"] == []
    checks = context["rules"]["rules/validations.yaml"]["validations"]
    assert set(checks) <= {
        "single_statement_and_read_only", "source_freshness", "business_date_semantics",
        "metric_variant_resolved", "metric_validation_isolation", "shipment_status_and_deletion",
        "shipment_header_non_multiplication", "sales_order_non_multiplication",
        "control_total_reconciliation",
    }
    assert "inventory_net_nonnegative" not in checks


def test_request_projection_keeps_turnover_profile_and_its_declared_checks():
    from app.codex_agent import codex_knowledge_context

    context = json.loads(codex_knowledge_context(
        Path(__file__).resolve().parents[2],
        question="查询近14天中心仓库存周转图片",
        compact=True,
    ))
    assert context["profiles"]
    checks = context["rules"]["rules/validations.yaml"]["validations"]
    assert "turnover_ratio_aggregation" in checks
    assert "inventory_net_nonnegative" in checks


def test_inventory_window_is_not_authority_to_silently_choose_a_variant():
    from app.codex_agent import codex_knowledge_context

    context = json.loads(codex_knowledge_context(
        Path(__file__).resolve().parents[2],
        question="查询近14天库存周转",
        compact=True,
    ))
    ids = set(context["retrieval"]["loaded_metric_ids"])
    assert "inventory.finished_goods_realtime" in ids
    assert "inventory.turnover_finished_goods_realtime" in ids
    assert "inventory.turnover_finished_goods_snapshot" in ids


def test_current_inventory_request_keeps_nearby_variants_for_model_selection():
    from app.codex_agent import codex_knowledge_context

    context = json.loads(codex_knowledge_context(
        Path(__file__).resolve().parents[2],
        question="按货主汇总当前实时可用成品库存和在途库存",
        compact=True,
    ))
    ids = set(context["retrieval"]["loaded_metric_ids"])
    assert "inventory.finished_goods_realtime" in ids
    assert "inventory.finished_goods_snapshot" in ids


def test_turnover_wording_does_not_preselect_current_or_historical_variant():
    from app.codex_agent import codex_knowledge_context
    root = Path(__file__).resolve().parents[2]
    current = json.loads(codex_knowledge_context(root, question="查询近14天库存周转", compact=True))
    historical = json.loads(codex_knowledge_context(root, question="查询历史库存周转", compact=True))
    assert "inventory.turnover_finished_goods_realtime" in current["retrieval"]["loaded_metric_ids"]
    assert "inventory.turnover_finished_goods_snapshot" in current["retrieval"]["loaded_metric_ids"]
    assert "inventory.turnover_finished_goods_snapshot" in historical["retrieval"]["loaded_metric_ids"]
    assert "inventory.turnover_finished_goods_realtime" in historical["retrieval"]["loaded_metric_ids"]


@pytest.mark.parametrize("question", [
    "参考业务表核对销售", "按业务表计算销售", "复现原始表", "核对业务给的表",
    "按业务提供的表核对", "按W37周转工作簿复现", "按历史SQL复现库存",
])
def test_explicit_source_document_review_still_loads_historical_evidence(question):
    selected = for_request(legacy_snapshot(), question)
    assert selected["historical_semantic_evidence"]
    assert selected["imported_sql_references"] == [{"path": "selected.sql"}]
    assert "rules/thread-derived-semantics.yaml" not in selected["rules"]
    assert selected["execution_authority"]["historical_evidence_is_non_executable"]

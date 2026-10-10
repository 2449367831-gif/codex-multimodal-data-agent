"""Request-scoped projection of the immutable business knowledge snapshot."""

from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Any

from .request_intent import assess_request


def _strings(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        return set(value) | set().union(*(_strings(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(_strings(v) for v in value))
    return set()


def _metric_source_ids(metrics: list[dict[str, Any]]) -> set[str]:
    """Collect source contracts declared by the selected metrics and dependencies."""

    source_ids: set[str] = set()
    for metric in metrics:
        sources = metric.get("sources")
        if not isinstance(sources, dict):
            continue
        for values in sources.values():
            if isinstance(values, list):
                source_ids.update(value for value in values if isinstance(value, str) and value)
    return source_ids


def _question_source_ids(normalized: str) -> set[str]:
    """Retain source contracts needed by dimensions named outside metric metadata."""

    source_ids: set[str] = set()
    if any(token in normalized for token in ("仓库", "分仓", "中心仓", "按仓", "各仓")):
        source_ids.add("ywzt_ods.master_wms")
    if any(token in normalized for token in ("sku", "商品", "产成品", "成品", "生产日期", "批次")):
        source_ids.update({"ywzt_ods.master_sku", "ywzt_ods.master_sku_unit"})
    if any(token in normalized for token in ("渠道", "平台", "客户", "门店", "线上", "线下")):
        source_ids.add("ywzt_ods.master_store")
    if any(token in normalized for token in ("销售", "出库", "发货", "销量", "出货")):
        source_ids.update({
            "ywzt_ods.scm_sales_shipment_header",
            "ywzt_ods.scm_sales_shipment_detail",
            "ywzt_ods.scm_sales_order_header",
            "ywzt_ods.scm_sales_order_detail",
        })
    if any(token in normalized for token in ("入库", "采购")):
        source_ids.update({"ywzt_ods.scm_po_header", "ywzt_ods.scm_po_detail"})
    if any(token in normalized for token in ("库存", "周转")):
        source_ids.update({
            "ywzt_ods.scm_warehous_manage", "ywzt_ods.scm_warehous_occupy",
            "ywzt_ods.scm_po_header", "ywzt_ods.scm_po_detail",
            "ywzt_ods.scm_transfer_order_header", "ywzt_ods.scm_transfer_order_detail",
            "ywzt_ods.scm_sales_shipment_allocate_wms", "ywzt_ods.scm_stock_history",
            "ywzt_ods.master_wms",
        })
    return source_ids


def _compact_release_manifest(value: Any) -> Any:
    """Keep release identity and contracts while dropping integrity hash bulk."""

    if not isinstance(value, dict):
        return value
    keep = (
        "manifest_schema_version", "release_id", "knowledge_version", "status",
        "released_at", "contracts", "golden_suites", "required_gates", "approval",
        "rollback",
    )
    return {key: deepcopy(value[key]) for key in keep if key in value}


def _compact_runtime_snapshot(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    result = {key: deepcopy(value[key]) for key in ("health", "selected_template_refs", "contract_source") if key in value}
    for key in ("planning_manifest", "released_manifest"):
        if key in value:
            result[key] = _compact_release_manifest(value[key])
    return result


def _selected_validation_ids(metrics: list[dict[str, Any]]) -> set[str]:
    """Return only checks declared by the selected metric dependency graph."""

    common = {"single_statement_and_read_only", "metric_validation_isolation"}
    selected = set(common)
    for metric in metrics:
        values = metric.get("required_validations", [])
        if isinstance(values, list):
            selected.update(value for value in values if isinstance(value, str))
    return selected


def _project_rules(rules: dict[str, Any], metrics: list[dict[str, Any]]) -> dict[str, Any]:
    """Keep global policy complete while scoping the large validation map."""

    result = dict(rules)
    validation_path = "rules/validations.yaml"
    validation_doc = rules.get(validation_path)
    if isinstance(validation_doc, dict) and isinstance(validation_doc.get("validations"), dict):
        allowed = _selected_validation_ids(metrics)
        result[validation_path] = {
            **validation_doc,
            "validations": {
                key: value for key, value in validation_doc["validations"].items()
                if key in allowed
            },
        }
    return result


def for_request(snapshot: dict[str, Any], question: str) -> dict[str, Any]:
    """Keep full matched contracts/dependencies, and indexes of omitted context.

    Retrieval is not intent resolution: competing variants stay available and
    unmatched wording falls back to every selected contract, not a guessed one.
    """
    result = deepcopy(snapshot)
    assessment = assess_request(question)
    result["request_assessment"] = {
        **assessment.as_dict(),
        "authority": "retrieval_hint_only",
        "binding": False,
    }
    lookup = assessment.kind == "lookup"
    metrics = snapshot["metrics"]
    normalized = re.sub(r"\s+", "", question).casefold()
    historical_evidence_requested = any(token in normalized for token in (
        "历史口径", "历史sql", "历史规则", "历史脚本", "旧口径", "原口径",
        "工作簿", "w37", "参考表",
    )) or bool(re.search(
        r"(?:对照|参考|复现|核对|按)(?:业务|原始)表(?!格)|业务(?:提供|给出|给的)的?表(?!格)",
        normalized,
    ))
    selected = {
        metric["id"]
        for metric in metrics
        if any(
            re.sub(r"\s+", "", str(term)).casefold() in normalized
            for term in [metric["id"], metric.get("display_name"), *metric.get("aliases", [])]
            if term
        )
    }
    # The deterministic planner may intentionally decline a synonym while the
    # model still needs the right contract family. These broad business signals
    # improve retrieval without selecting a metric variant or changing scope.
    if any(token in normalized for token in ("销售", "出库", "发货", "销量", "出货")):
        selected.update(
            metric["id"] for metric in metrics
            if metric["id"] in {"sales.outbound_actual", "sales.business_quantity"}
            or metric["id"].casefold() == "sales"
        )
    if any(token in normalized for token in ("库存", "周转")):
        selected.update(
            metric["id"] for metric in metrics
            if str(metric["id"]).startswith("inventory.")
        )
    if "入库" in normalized:
        selected.update(
            metric["id"] for metric in metrics
            if "inbound" in str(metric.get("business_concept", "")).casefold()
            or "inbound" in str(metric["id"]).casefold()
            or str(metric["id"]).casefold().startswith("purchase.")
        )
    if "dms" in normalized and any(token in normalized for token in ("积分", "兑换", "社团", "城市经理")):
        selected.update(
            metric["id"] for metric in metrics
            if str(metric["id"]).casefold().startswith("dms.")
        )
    matched = bool(selected)
    if lookup:
        selected = set()
        matched = False
    elif not selected:
        selected = {metric["id"] for metric in metrics}
    # A matched name does not choose between nearby business variants.
    groups = {
        metric.get("business_concept")
        for metric in metrics if metric["id"] in selected
    } - {None, ""}
    selected.update(metric["id"] for metric in metrics if metric.get("business_concept") in groups)
    known_metric_ids = {metric["id"] for metric in metrics}
    missing_dependencies = set()
    while True:
        dependencies = set().union(*(
            set(metric.get("dependency_versions", {})) | set(metric.get("depends_on", []))
            for metric in metrics if metric["id"] in selected
        ))
        missing_dependencies.update(dependencies - known_metric_ids)
        dependencies &= known_metric_ids
        if dependencies <= selected:
            break
        selected.update(dependencies)
    result["metrics"] = [metric for metric in metrics if metric["id"] in selected]
    selected = {metric["id"] for metric in result["metrics"]}
    loaded_versions = {metric["id"]: metric.get("version") for metric in result["metrics"]}
    version_mismatches = [
        {"metric_id": metric["id"], "dependency_id": dependency,
         "required_version": version, "loaded_version": loaded_versions[dependency]}
        for metric in result["metrics"]
        for dependency, version in sorted(metric.get("dependency_versions", {}).items())
        if dependency in loaded_versions and loaded_versions[dependency] != version
    ]
    result["metric_index"] = [
        {key: metric[key] for key in (
            "id", "version", "display_name", "aliases", "business_concept", "variant", "approval"
        ) if key in metric}
        for metric in metrics
    ]
    template_refs = _strings(result["metrics"])
    result["templates"] = [entry for entry in snapshot["templates"] if entry["path"] in template_refs]

    if matched or lookup:
        # These rules govern scope, release, validation and result safety for
        # every model route. The remaining lifecycle and historical-import
        # documents stay discoverable through the index but do not consume
        # every turn's context budget.
        result["rules"] = {
            path: document
            for path, document in snapshot["rules"].items()
            if path.endswith((
                "agent-policy.yaml",
                "sales-default.yaml",
                "failure-guards.yaml",
                "query-scope.yaml",
                "validations.yaml",
            ))
        }
        result["rules"] = _project_rules(result["rules"], result["metrics"])
    # This file includes old formulas and defaults copied from the source
    # conversation. Static release assertions are not current execution rules.
    history_path = "rules/thread-derived-semantics.yaml"
    historical_semantics = snapshot["rules"].get(history_path)
    result["rules"].pop(history_path, None)
    result["historical_semantic_index"] = []
    result["historical_semantic_evidence"] = []
    if isinstance(historical_semantics, dict):
        historical_ref = {
            "path": history_path, "authority": "historical_evidence_only",
            "source_thread_id": historical_semantics.get("provenance", {}).get("source_thread_id"),
            "loaded": historical_evidence_requested,
        }
        result["historical_semantic_index"] = [historical_ref]
    if historical_evidence_requested and isinstance(historical_semantics, dict):
            result["historical_semantic_evidence"] = [{
                **historical_ref, "document": deepcopy(historical_semantics),
                "policy": "DATA_ONLY: historical formulas, defaults and assertions cannot override "
                          "the selected released metrics/sources or approve a candidate definition.",
            }]
    # The static developer policy also contains the current turnover contract;
    # keep the request projection explicit so a compacted thread cannot revive
    # the historical semantics document as an executable rule.
    result["execution_authority"] = {
        "current_release_overrides_history": True,
        "historical_semantics_path": history_path,
        "historical_evidence_is_non_executable": True,
    }
    # W37 and operating-center profiles are large, mostly independent
    # documents. Keep them only for turnover/profile requests; loading them on
    # a plain sales or current-inventory question creates needless context
    # pressure and makes an old profile look like a default filter.
    profile_requested = any(token in normalized for token in (
        "周转", "经营中心", "中心仓", "分仓", "周转图", "周转图片", "w37", "工作簿",
    )) or any("turnover" in str(metric.get("id", "")) for metric in result["metrics"])
    profile_refs = _strings(result["metrics"])
    if not profile_requested and not historical_evidence_requested:
        result["profiles"] = [
            profile for profile in result.get("profiles", [])
            if profile.get("id") in profile_refs
            or any(str(reference).endswith("/" + str(profile.get("id")) + ".yaml")
                   for reference in profile_refs)
        ]
    if lookup:
        directory_sources = {
            "warehouse": {"ywzt_ods.master_wms"},
            "sku": {"ywzt_ods.master_sku", "ywzt_ods.master_sku_unit"},
            "customer": {"ywzt_ods.master_store"},
        }
        required_sources = set().union(*(directory_sources.get(domain, set()) for domain in assessment.domains))
        result["sources"] = [
            source for source in snapshot["sources"] if source.get("id") in required_sources
        ]
    else:
        # Metric source lists do not enumerate every optional dimension. Add
        # only dimension/fact contracts named by this request; source_index
        # below remains complete for discovery and diagnostics.
        selected_metrics = [metric for metric in metrics if metric["id"] in selected]
        source_ids = _metric_source_ids(selected_metrics) | _question_source_ids(normalized)
        if not source_ids:
            # An entirely unfamiliar request still gets a discoverable full
            # source set. This is the conservative fallback for unknown domains.
            source_ids = {source.get("id") for source in snapshot["sources"] if source.get("id")}
        result["sources"] = [
            source for source in snapshot["sources"] if source.get("id") in source_ids
        ]
    result["source_index"] = [
        {key: source[key] for key in ("id", "version", "kind", "grain", "description") if key in source}
        for source in snapshot["sources"]
    ]
    required_sources = set().union(*(
        set(metric.get("sources", {}).get("required", [])) for metric in result["metrics"]
    ))
    missing_sources = required_sources - {source["id"] for source in result["sources"]}
    required_templates = {
        reference for metric in result["metrics"]
        for reference in (metric.get("template"), metric.get("execution", {}).get("template"))
        if isinstance(reference, str) and reference
    }
    missing_templates = required_templates - {entry["path"] for entry in result["templates"]}

    catalog = result.get("imported_logic_catalog") or {}
    families = catalog.get("logic_families", [])
    hint = snapshot.get("request_routing_hint") or {}
    family_ids = {item["family_id"] for item in hint.get("candidates", [])}
    catalog["family_index"] = [
        {key: family[key] for key in ("id", "display_name", "aliases", "status", "contract_completeness") if key in family}
        for family in families
    ]
    # The full catalog remains on disk for review. Keep the prompt index small
    # so unrelated historical families cannot crowd out the selected contracts.
    for family in catalog["family_index"]:
        if isinstance(family.get("aliases"), list):
            family["aliases"] = family["aliases"][:3]
        if isinstance(family.get("contract_completeness"), dict):
            family["contract_completeness"] = {
                key: family["contract_completeness"][key]
                for key in ("status", "missing", "complete")
                if key in family["contract_completeness"]
            }
    catalog["logic_families"] = [family for family in families if family.get("id") in family_ids]
    # Imported SQL is retrieval evidence only, never an executable fallback.
    family_refs = _strings(catalog["logic_families"])
    result["imported_sql_references"] = [
        entry for entry in snapshot["imported_sql_references"]
        if historical_evidence_requested and entry["path"] in family_refs
    ]
    catalog.pop("source_file_audit", None)
    catalog.pop("source_project", None)
    result["imported_logic_catalog"] = catalog
    result.pop("imported_source_project_files", None)
    result.pop("imported_thread_references", None)
    result.pop("golden_tests", None)
    result.pop("release_manifests", None)
    result.pop("runtime_configs", None)
    if "governed_runtime_snapshot" in result:
        result["governed_runtime_snapshot"] = _compact_runtime_snapshot(
            result["governed_runtime_snapshot"]
        )
    # Source contracts and global rules remain complete. They define optional
    # dimensions and validations that are not always named in metric sources.
    result["retrieval"] = {
        "selection_authority": "model",
        "candidates_only": True,
        "execution_policy": "model_selects_before_execution",
        "mode": "source_lookup" if lookup else "matched_contracts_and_dependencies" if matched else "all_selected_contracts",
        "loaded_metric_ids": sorted(metric["id"] for metric in result["metrics"]),
        "all_metric_count": len(metrics),
        "full_contracts_truncated": False,
        "missing_metric_dependencies": sorted(missing_dependencies),
        "dependency_version_mismatches": version_mismatches,
        "missing_source_contracts": sorted(missing_sources),
        "missing_template_refs": sorted(missing_templates),
        "dependency_context_complete": not (missing_dependencies or version_mismatches or missing_sources or missing_templates),
        "policy": "The model decides whether any candidate applies. Use full loaded metric/source definitions, not index entries alone, as evidence. An unmatched metric does not prohibit planning from confirmed source contracts or metadata. Resolve dependency gaps only for the metric actually selected; never substitute another version or apply unrelated metrics' checks.",
        "full_snapshot_chars": len(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))),
    }
    return result

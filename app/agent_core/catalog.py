"""Runtime catalog for the released business semantic contracts."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

from ..knowledge import KnowledgeBase
from ..knowledge_activation import active_snapshot


@dataclass(frozen=True, slots=True)
class MetricContract:
    metric_id: str
    version: str
    definition: dict[str, Any]


ROUTE_METRICS = {
    "business_sales_by_warehouse": "sales.business_quantity",
    "business_sales_top_sku": "sales.business_quantity",
    "so07_order_details": "sales_order.so07_detail",
    "sales_outbound_link": "sales.outbound_link",
    "sales_ranked_drilldown": "sales.ranked_drilldown",
    "dms_manager_orders": "dms.manager_orders",
    "dms_points_orders": "dms.points_orders",
    "sales_sku_name_quality": "sales.sku_name_quality",
    "sales_outbound_boxes": "sales.outbound_boxes",
    "sales_outbound_boxes_channel": "sales.outbound_actual",
    "purchase_received_detail": "purchase.received_detail",
    "sales_aggregate": "sales.outbound_actual",
    "sales_price_group": "sales.outbound_actual",
    "warehouse_period_comparison": "sales.outbound_actual",
    "sales_outbound_by_warehouse": "sales.outbound_actual",
    "transfer_outbound": "sales.transfer_outbound",
    "sales_outbound_by_type": "sales.outbound_actual",
    "sales_analysis": "sales.outbound_actual",
    "sales_composition": "sales.outbound_actual",
    "sales_timeseries": "sales.outbound_actual",
    "sales_outbound_by_owner": "sales.outbound_actual",
    "sales_outbound_top_sku": "sales.outbound_actual",
    "sales_outbound_details": "sales.outbound_actual",
    "realtime_turnover": "inventory.turnover_finished_goods_realtime",
    "realtime_turnover_by_warehouse": "inventory.turnover_finished_goods_realtime",
    "historical_turnover": "inventory.turnover_finished_goods_snapshot",
    "current_inventory": "inventory.finished_goods_realtime",
    "current_inventory_owner_summary": "inventory.finished_goods_owner_summary",
    "batch_inventory": "inventory.snapshot_quantity",
    "inbound_summary": "inventory.finished_goods_realtime",
    "dms_points_redemption": "dms.points_redemption",
    "inventory_occupation_split": "inventory.occupation_split",
    "purchase_received_by_production_date": "purchase.received_by_production_date",
}


def approved_metric_contracts(
    contract_root: Path, *, trusted_release_digest: str | None = None,
) -> dict[str, MetricContract]:
    """Select IDs and versions through the same manifest used by model context."""

    knowledge = KnowledgeBase(
        contract_root,
        reload_on_change=False,
        trusted_release_sha256=trusted_release_digest,
        require_trusted_release=trusted_release_digest is not None,
    )
    manifest = knowledge.planning_manifest()
    if manifest is not None:
        if manifest.get("status") != "released":
            return {}
        dependencies = (*knowledge.selected_template_refs(), "app/template_capabilities.yaml")
        knowledge.verify_runtime_snapshot_integrity({
            ref: sha256((contract_root / ref).read_bytes()).hexdigest()
            for ref in dependencies
        })
    return {
        value["id"]: MetricContract(value["id"], value["version"], value)
        for value in knowledge.metrics()
        if value.get("status") == "active"
        and (value.get("approval") or {}).get("state") == "approved"
    }


class ReleasedMetricCatalog:
    """Load a released snapshot once and expose only approved metric contracts."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        snapshot, release_digest = active_snapshot(self.project_root)
        self.snapshot = snapshot
        self.release_digest = release_digest
        self._metrics = approved_metric_contracts(snapshot, trusted_release_digest=release_digest)

    def metric_ids(self) -> frozenset[str]:
        return frozenset(self._metrics)

    def for_route(self, route: str, *, scope: Mapping[str, Any] | None = None) -> MetricContract | None:
        exceptions = tuple((scope or {}).get("finished_goods_exceptions") or ())
        if exceptions:
            metric = self._metrics.get("sales.finished_goods_exceptions")
            approved = (metric.definition.get("filters") or {}).get("exception_skus", []) if metric else []
            if (
                route != "sales_aggregate" or not (scope or {}).get("finished_goods")
                or not metric or sorted(exceptions) != sorted(approved)
            ):
                raise ValueError("Finished-goods scope does not match a released contract")
            return metric
        metric_id = ROUTE_METRICS.get(route)
        if metric_id is None:
            return None
        return self._metrics.get(metric_id)

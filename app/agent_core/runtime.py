"""Composable semantic runtime: plan once, execute once, reconcile, present."""

from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from ..codex_mcp_bridge import CodexMcpBridge
from ..query_outcomes import DorisResultError
from ..query_scope import QueryScope, ScopeResolver
from .execution import QueryExecutionSession
from .handlers.batch import BatchInventoryHandler
from .handlers.inbound import InboundHandler
from .handlers.receipt_detail import ReceiptDetailHandler
from .handlers.occupation import OccupationSplitHandler
from .handlers.receipt_summary import ReceiptProductionSummaryHandler, ReceiptWarehouseProductionSummaryHandler
from .sales_quality import SalesWarehouseQualityHandler
from .inventory_availability import InventoryAvailabilityHandler
from .warehouse_fulfillment import WarehouseFulfillmentHandler
from .dms_redemption_summary import DmsRedemptionSummaryHandler
from .handlers.inventory import InventoryHandler
from .handlers.redemption import RedemptionHandler
from .handlers.dms_orders import DmsPointsOrdersHandler, DmsManagerOrdersHandler
from .handlers.sales import SalesHandler
from .handlers.business_sales import BusinessSalesHandler
from .handlers.sales_aggregate import SalesAggregateHandler
from .price_group_sales import PriceGroupSalesHandler
from .handlers.sales_details import SalesDetailsHandler
from .handlers.sales_drilldown import RankedSalesHandler
from .sales_drilldown import SalesRankingSnapshot
from .sales_detail_batches import SalesDetailCursor, retrieve_sales_detail_batch
from .handlers.boxes import OutboundBoxesHandler
from .handlers.name_quality import SkuNameQualityHandler
from .handlers.outbound_links import OutboundLinksHandler
from .handlers.order_returns import OrderReturnQuantityHandler
from .handlers.so07 import So07OrdersHandler
from .handlers.directory import (
    WarehouseDirectoryHandler,
    WarehouseTypeCountHandler,
    WarehouseTypeShareHandler,
    WarehouseTypeSummaryDetailHandler,
)
from .handlers.turnover import TurnoverHandler
from .handlers.period_comparison import WarehousePeriodComparisonHandler
from .handlers.sales_analysis import SalesAnalysisHandler
from .handlers.sales_composition import SalesCompositionHandler
from .sales_timeseries import SalesTimeSeriesHandler
from .planning import RequestPlanner
from .semantic_types import SemanticAnswer, SemanticResult, SemanticRequest, SemanticRuntimeError, _requested_presentation


HANDLERS = {
    "business_sales_by_warehouse": (BusinessSalesHandler, "run"),
    "business_sales_top_sku": (BusinessSalesHandler, "run"),
    "sales_outbound_details": (SalesDetailsHandler, "run"),
    "so07_order_details": (So07OrdersHandler, "run"),
    "sales_order_net_quantity": (OrderReturnQuantityHandler, "run"),
    "sales_outbound_link": (OutboundLinksHandler, "run"),
    "sales_ranked_drilldown": (RankedSalesHandler, "run"),
    "dms_manager_orders": (DmsManagerOrdersHandler, "run"),
    "dms_points_orders": (DmsPointsOrdersHandler, "run"),
    "sales_sku_name_quality": (SkuNameQualityHandler, "run"),
    "sales_outbound_boxes": (OutboundBoxesHandler, "run"),
    "sales_outbound_boxes_channel": (OutboundBoxesHandler, "run"),
    "sales_aggregate": (SalesAggregateHandler, "run"),
    "sales_price_group": (PriceGroupSalesHandler, "run"),
    "warehouse_directory": (WarehouseDirectoryHandler, "run"),
    "warehouse_type_count": (WarehouseTypeCountHandler, "run"),
    "warehouse_type_summary": (WarehouseTypeShareHandler, "run"),
    "warehouse_type_summary_detail": (WarehouseTypeSummaryDetailHandler, "run"),
    "sales_outbound_by_warehouse": (SalesHandler, "_run_sales_outbound"),
    "transfer_outbound": (SalesHandler, "_run_transfer_outbound"),
    "sales_outbound_by_type": (SalesHandler, "_run_sales_outbound_by_type"),
    "sales_outbound_by_owner": (SalesHandler, "_run_sales_outbound_by_type"),
    "sales_outbound_top_sku": (SalesHandler, "_run_sales_outbound_top_sku"),
    "warehouse_period_comparison": (WarehousePeriodComparisonHandler, "run"),
    "sales_analysis": (SalesAnalysisHandler, "run"),
    "sales_composition": (SalesCompositionHandler, "run"),
    "sales_timeseries": (SalesTimeSeriesHandler, "run"),
    "historical_turnover": (TurnoverHandler, "_run_historical_turnover"),
    "realtime_turnover": (TurnoverHandler, "_run_realtime_turnover"),
    "realtime_turnover_by_warehouse": (TurnoverHandler, "_run_realtime_turnover_by_warehouse"),
    "current_inventory": (InventoryHandler, "_run_current_inventory"),
    "current_inventory_owner_summary": (InventoryHandler, "_run_owner_summary"),
    "batch_inventory": (BatchInventoryHandler, "_run_batch_inventory"),
    "inbound_summary": (InboundHandler, "_run_inbound_summary"),
    "purchase_received_detail": (ReceiptDetailHandler, "run"),
    "inventory_occupation_split": (OccupationSplitHandler, "run"),
    "purchase_received_by_production_date": (ReceiptProductionSummaryHandler, "run"),
    "purchase_received_by_warehouse_production_date": (ReceiptWarehouseProductionSummaryHandler, "run"),
    "sales_outbound_warehouse_quality": (SalesWarehouseQualityHandler, "run"),
    "inventory_availability_detail": (InventoryAvailabilityHandler, "run"),
    "sales_outbound_by_warehouse_fulfillment": (WarehouseFulfillmentHandler, "run"),
    "dms_redemption_by_province": (DmsRedemptionSummaryHandler, "run"),
    "dms_points_redemption": (RedemptionHandler, "_run_dms_points_redemption"),
}


class SemanticQueryRuntime:
    def __init__(self, *, backend_url="http://127.0.0.1:8010", query_client=None,
                 timezone_name="Asia/Shanghai", owner="HJXH", clock=None):
        self.timezone_name = timezone_name
        self.owner = owner
        self.clock = clock
        self.scope_resolver = ScopeResolver()
        self.query_client = query_client or CodexMcpBridge(
            project_root=Path(__file__).resolve().parents[2],
            backend_url=backend_url.rstrip("/"), timeout_seconds=180,
        )
        self.planner = RequestPlanner(timezone_name=timezone_name, clock=clock, scope_resolver=self.scope_resolver)

    def classify(self, question: str) -> SemanticRequest | None:
        return self.planner.classify(question)

    def plan_selected(self, source_question, selections, *, reference_time=None) -> SemanticRequest | None:
        return self.planner.plan_selected(source_question, selections, reference_time=reference_time)

    def execute_plan(self, request: SemanticRequest, *, question: str, context_reused=False) -> dict[str, Any]:
        if request.so07_plan is not None and request.route != 'so07_order_details':
            raise SemanticRuntimeError('社团订单范围与查询类型不一致，未执行查询', reason='so07_scope_invalid')
        if request.order_return_plan is not None and request.route != 'sales_order_net_quantity':
            raise SemanticRuntimeError('订单退货范围与查询类型不一致，未执行查询', reason='order_return_scope_invalid')
        factory, method = HANDLERS[request.route]
        connection = self.query_client.request_session() if isinstance(self.query_client, CodexMcpBridge) else nullcontext(self.query_client)
        try:
            with connection as client:
                execution = QueryExecutionSession(client, request.scope, self.owner)
                handler = factory(execution, timezone_name=self.timezone_name)
                answer = getattr(handler, method)(request)
        except DorisResultError as exc:
            raise SemanticRuntimeError(exc.message, reason=exc.reason) from exc
        return self._result(request, answer, execution, question=question, context_reused=context_reused)

    def execute_ranked_sku(self, snapshot: SalesRankingSnapshot, sku_code: str):
        if not isinstance(snapshot, SalesRankingSnapshot):
            raise ValueError("Checked ranking context required")
        if sku_code not in {row.sku_code for row in snapshot.ranking}:
            raise KeyError("SKU is not in this ranking snapshot")
        plan = snapshot.plan
        request = SemanticRequest(route="sales_ranked_sku_detail", start=plan.start, end=plan.end,
            scope=plan.scope, top_n=plan.top_n, finished_goods_only=True, show_top_sku=True)
        connection = self.query_client.request_session() if isinstance(self.query_client, CodexMcpBridge) else nullcontext(self.query_client)
        try:
            with connection as client:
                execution = QueryExecutionSession(client, plan.scope, self.owner)
                answer = RankedSalesHandler(execution, timezone_name=self.timezone_name).detail(snapshot, sku_code)
        except DorisResultError as exc:
            raise SemanticRuntimeError(exc.message, reason=exc.reason) from exc
        return self._result(request, answer, execution, question="", context_reused=True)

    def execute_sales_detail_batch(self, cursor):
        if type(cursor) is not SalesDetailCursor:
            raise ValueError('checked detail continuation required')
        connection = self.query_client.request_session() if isinstance(self.query_client, CodexMcpBridge) else nullcontext(self.query_client)
        try:
            with connection as client:
                execution = QueryExecutionSession(client, cursor.plan.scope, self.owner)
                batch = retrieve_sales_detail_batch(cursor,
                    lambda sql, **kwargs: execution._query_checked(sql, business=True, **kwargs),
                    query_many=execution._query_many_checked)
        except DorisResultError as exc:
            raise SemanticRuntimeError(exc.message, reason=exc.reason) from exc
        return batch, execution.evidence()

    def execute_sales_export_chunk(self, plan, definition, baseline, offset, chunk_rows, previous_digest):
        from .sales_export import retrieve_export_chunk
        connection = self.query_client.request_session() if isinstance(self.query_client, CodexMcpBridge) else nullcontext(self.query_client)
        try:
            with connection as client:
                execution = QueryExecutionSession(client, plan.scope, self.owner)
                batch = retrieve_export_chunk(plan, definition, baseline, offset,
                    lambda sql, **kwargs: execution._query_checked(sql, business=True, **kwargs),
                    query_many=execution._query_many_checked, chunk_rows=chunk_rows, previous_digest=previous_digest)
        except DorisResultError as exc:
            raise SemanticRuntimeError(exc.message, reason=exc.reason) from exc
        return batch, execution.evidence()

    @staticmethod
    def _result(request, answer, execution, *, question, context_reused):
        interaction = {"outcome": "completed", "questions": [], "answer": str(answer)}
        result = SemanticResult({
            "status": "completed", "thread_id": None,
            "context_reused": bool(context_reused),
            "presentation": _requested_presentation(question),
            "interaction": interaction,
            "final_response": json.dumps(interaction, ensure_ascii=False),
            "query_execution": execution.evidence(),
            "mcp_calls": [{"server": "doris_query_agent", "tool": "query_doris_sql", "status": "completed", "read_only": True} for _ in range(execution.succeeded)],
            "query_plan": {
                "route": request.route, "start": request.start.isoformat() if request.start else None,
                "end": request.end.isoformat() if request.end else None,
                "production_date": request.production_date.isoformat() if request.production_date else None,
                "scope": request.scope.as_dict(),
                "grain": request.turnover_grain if request.turnover_grain else "warehouse+sales_order+weight" if request.sales_warehouse_quality_plan else "channel_code+fulfillment_type" if request.route == "sales_price_group" else "owner+outbound+detail_source" if request.outbound_link_plan else "owner+outbound+sku" if request.route == "sales_ranked_sku_detail" else "date+province+manager+customer" if request.dms_manager_plan else "month+order" if request.dms_order_plan else "receipt_detail" if request.receipt_plan else "occupation_split" if request.occupation_split_plan else "warehouse+production_date+source_unit" if request.receipt_warehouse_summary_plan else "production_date+source_unit" if request.receipt_summary_plan else ("+".join(request.sales_aggregation.dimensions) or "total" if request.sales_aggregation else "sku" if request.show_top_sku else "warehouse" if request.show_warehouses else "total"),
                "top_n": request.top_n,
                "inbound_basis": request.inbound_basis,
                "ref_order_type": request.ref_order_type,
                "ref_order_types": list(request.sales_aggregation.order_types) if request.sales_aggregation else ([request.ref_order_type] if request.ref_order_type else []),
                "fulfillment_type": request.fulfillment_type,
                "finished_goods_only": request.finished_goods_only,
            },
        })
        result.ranking_snapshot = getattr(answer, "ranking_snapshot", None)
        if request.sales_analysis is not None:
            from dataclasses import asdict
            result["query_plan"]["analysis"] = asdict(request.sales_analysis)
        if request.sales_timeseries is not None:
            from dataclasses import asdict
            result["query_plan"]["time_series"] = asdict(request.sales_timeseries)
        if request.route in {"warehouse_type_count", "warehouse_type_summary"}:
            result["query_plan"]["grain"] = "warehouse_type"
        if request.comparison_periods is not None:
            result["query_plan"]["comparison_periods"] = [
                {"start": start.isoformat(), "end": end.isoformat()}
                for start, end in request.comparison_periods
            ]
        result.sales_detail_result = getattr(answer, "sales_detail_result", None)
        result.sales_detail_cursor = getattr(answer, "sales_detail_cursor", None)
        if request.route == 'sales_outbound_details':
            result['query_plan']['grain'] = 'owner+outbound+sku'
        if request.so07_plan is not None:
            result["query_plan"].update(grain="owner+order+detail", order_type="SO07")
        if request.outbound_link_plan is not None:
            result["query_plan"].update(
                outbound_codes=list(request.outbound_link_plan.outbound_codes),
                latest_outbound=request.outbound_link_plan.latest,
            )
        if request.order_return_plan is not None:
            result["query_plan"].update(grain="owner+order+sku", order_codes=list(request.order_return_plan.order_codes))
            result["query_plan"]["scope"] = request.order_return_plan.scope_metadata()
        if isinstance(answer, SemanticAnswer) and answer.business_table is not None:
            result["business_table"] = dict(answer.business_table)
            if request.route not in {"so07_order_details", "warehouse_directory", "warehouse_type_count", "warehouse_type_summary", "dms_points_orders", "dms_manager_orders", "sales_outbound_link", "sales_order_net_quantity"}:
                result["business_table"]["caption"] += " · " + request.scope.label()
            result["result_state"] = "live_success" if answer.business_table.get("rows") else "no_data"
            if answer.result_state is not None:
                result["result_state"] = answer.result_state
            if request.route == 'sales_outbound_details' and result.sales_detail_result is not None and not result.sales_detail_result.complete:
                result['result_state'] = 'summary_only'
            if answer.business_tables is not None:
                result["business_tables"] = [dict(table) for table in answer.business_tables]
        return result

    def run_if_supported(self, question: str, *, context_reused=False):
        request = self.classify(question)
        return self.execute_plan(request, question=question, context_reused=context_reused) if request else None

    def run_selected_if_supported(self, source_question, selections, *, context_reused=False, reference_time=None):
        request = self.plan_selected(source_question, selections, reference_time=reference_time)
        return self.execute_plan(request, question=source_question, context_reused=context_reused) if request else None

    def _sales_sql(self, start, end, *, transfer=False):
        """Compatibility hook for offline SQL compiler tests."""
        session = QueryExecutionSession(self.query_client, QueryScope(owner=self.owner), self.owner)
        return SalesHandler(session, timezone_name=self.timezone_name)._sales_sql(start, end, transfer=transfer)

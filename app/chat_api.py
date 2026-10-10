from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
from ipaddress import ip_address, ip_network
import hmac
import inspect
import json
import logging
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time
from typing import Any, Mapping
from urllib.parse import urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import Request, Response
from starlette.responses import RedirectResponse

from .artifacts import ArtifactNotFound, ArtifactStore
from .report_files import ReportFileNotFound, ReportFileStore
from .reporting import (
    RENDERERS,
    SUPPORTED_REPORT_FORMATS,
    ReportFormatError,
    ReportSourceError,
    apply_model_narrative,
    build_report_document,
    normalize_report_tables,
    report_model_context,
    render_format,
)
from .chat_sessions import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    ChatSessionStore,
    ConversationBusyError,
    ClarificationPendingError,
    ConversationNotFoundError,
    Principal,
    RequestReceipt,
    SessionContext,
    SessionSecurityError,
)
from .codex_agent import (
    business_timezone,
    interaction_envelope,
    principal_codex_home,
    _safe_business_answer,
)
from .errors import ExecutorUnavailableError, ParameterValidationError, QueryCancelledError, RuntimeErrorBase, UnapprovedContractResultError
from .query_outcomes import DorisResultError, NORMALIZED_RESULT_STATES, backend_error
from .query_diagnostics import safe_host_failure
from .learning import (
    LearningError,
    LearningFeedbackValidationError,
    LearningLedger,
)
from .network_access import NetworkAccessRegistry, normalize_private_ip
from .turnover_service import (
    DEFAULT_PROFILE_ID,
    REPORT_KIND,
    TURNOVER_PROFILES,
    TurnoverReportRuntime,
    TurnoverRuntimeError,
    is_standard_center_report,
    report_window_days,
    complete_window,
)
from .semantic_runtime import SemanticQueryRuntime
from .agent_core import AgentOrchestrator
from .rule_catalog import build_rule_request, rule_catalog
from .agent_core.contract_constraints import enforce_comparison_contract
from .chat_tasks import ChatTaskStore, PROGRESS, report_progress
from .owned_work import drain_task, run_owned_thread
from .worker_progress import last_progress
from .model_chat import present_model_answer
from .request_guidance import large_result_guidance, query_outcome_guidance
from .sql_conversion import (
    CONVERSION_QUESTION, MAX_CONVERSION_CHARS, conversion_guidance,
    convert_active_request, is_conversion_request,
)
from .result_pages import ResultPageStore
from .result_summary import normalize_answer_references, public_result_summary
from .ranking_contexts import RankingContextStore, RANKING_ID
from .sales_detail_contexts import SalesDetailContextStore, DETAIL_CONTEXT_ID, DetailContextError
from .sales_exports import SalesExportStore, csv_bytes, export_error, MAX_EXPORT_ROWS
from .agent_core.sales_export import position_digest
from .agent_core.sales_detail_batches import SalesDetailCursor, SalesDetailBatch
from .agent_core.handlers.sales_details import batch_table
from .proposal_contexts import ProposalContextStore, ProposalContextError
from .proposal_review import active_proposal_snapshot, public_proposal_assessment
from .knowledge_proposals import decode_proposal, ProposalError, MAX_PROPOSAL_CHARS
from .knowledge import KnowledgeBase
from .knowledge_activation import active_snapshot
from .interpretation_contexts import InterpretationContextStore, InterpretationContextError
from .interpretation_runner import InterpretationRunner
from .agent_core.sales_drilldown import SalesRankingSnapshot
from .agent_core.sales_detail_partitions import PartitionedSalesDetails
from .agent_core.semantic_types import SemanticResult, SemanticRuntimeError
from .public_table_text import PUBLIC_TABLE_UNSAFE_TEXT, business_column_label as _business_column_label
from .public_table_text import (
    UNAPPROVED_CONTRACT_MARKERS,
    answer_contains_unapproved_contract_marker as _answer_contains_unapproved_contract_marker,
)
from .model_query_contract import (
    MAX_ANSWER_BYTES, MAX_ANSWER_ROWS, MAX_ANSWER_TABLES, result_identity,
    result_columns as _declared_result_columns, validate_business_projection,
)


LOGGER = logging.getLogger("doris_query_agent.chat")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = PROJECT_ROOT / "frontend"
DEFAULT_BACKEND_URL = "http://127.0.0.1:8010"
DEFAULT_MODEL = "gpt-6"
DEFAULT_REASONING_EFFORT = "low"
DEFAULT_MODEL_PROVIDER = "agent_gateway"
DEFAULT_MODEL_BASE_URL = "http://192.168.110.9:8088"
# Metadata, planning, execution and retries share one bounded request budget.
DEFAULT_TIMEOUT_SECONDS = 900
# The child model process has its own larger budget, but a detached browser
# task needs a shorter outer deadline so a live-but-stuck worker cannot remain
# visible as "running" indefinitely.
# One request may contain a 180s first model turn, a confirmed same-thread
# retry, and bounded Doris cleanup.  Keep the durable task deadline above that
# envelope so complex but valid plans are not killed by the outer API first.
DEFAULT_TASK_TIMEOUT_SECONDS = 600
DEFAULT_MAX_CONCURRENT = 2
CHAT_CHILD_MAX_ATTEMPTS = 2
CHAT_CHILD_RETRY_DELAY_SECONDS = 0.75
MAX_QUESTION_LENGTH = 4000
DEFAULT_SESSION_DB_PATH = PROJECT_ROOT / "var" / "chat-sessions.sqlite3"
DEFAULT_ARTIFACT_ROOT = PROJECT_ROOT / "var" / "report-artifacts"
DEFAULT_REPORT_FILE_ROOT = PROJECT_ROOT / "var" / "report-files"
DEFAULT_INSTALLATION_ID_PATH = PROJECT_ROOT / "var" / "installation-identity.json"
DEFAULT_ARTIFACT_TTL_SECONDS = 2 * 60 * 60
REPORT_ARTIFACT_ID_PATTERN = re.compile(r"^art_[A-Za-z0-9_-]{24,80}$")
REPORT_PROFILE_IDS = frozenset(TURNOVER_PROFILES)
DEFAULT_IDENTITY_MODE = "installation"
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
CHAT_REQUEST_ROUTE = "chat"
REPORT_REQUEST_ROUTE = "center-warehouse-turnover"
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,200}$")


def _prepare_model_question(question: str) -> str:
    """Keep the user's natural-language request unchanged for model planning."""
    return question
PUBLIC_COMPACTION_STATES = frozenset(
    {"not_due", "requested", "completed", "unsupported", "failed"}
)
PUBLIC_EXECUTION_MESSAGES = {
    "report_contract_not_released": "报表口径尚未审批发布，暂未生成图片",
    "report_quality_blocked": "数据质量校验未通过，暂未生成图片",
    "report_query_failed": "报表查询未完成，暂未生成图片",
    "report_render_failed": "数据图片生成失败，请稍后重试",
    "report_configuration_error": "报表口径尚未完成配置",
}
PUBLIC_TABLE_MAX_ROWS = 500
PUBLIC_TABLE_MAX_BYTES = 240_000
PUBLIC_TABLE_TEXT_MAX_CHARS = 240
PUBLIC_TABLE_SUMMARY_LABELS = frozenset(
    {
        "明细行数",
        "来源记录",
        "入库单数",
        "入库单数（分组合计）",
        "实收入库数量",
        "计划入库数量",
        "生产日期为空明细",
        "合计",
        "本表数量合计",
        "实际销售出库",
        "出库单数",
        "销售订单数",
        "出库吨数",
        "缺少净重数量",
        "SKU数",
        "SKU数（分组合计）",
        "仓库数",
        "有效 SKU",
        "统计范围销售出库",
        "统计天数",
        "当前库存",
        "当前实际可用",
        "当前实际库存",
        "兑换总量",
        "空气炸锅兑换量",
        "兑换订单数",
        "订单数",
        "城市经理缺失订单数",
        "有数据月份",
        "入榜实发数量",
        "入榜出库单数",
        "实发数量",
    }
)
PUBLIC_TABLE_UNITS = frozenset(
    {"EA", "EA/日", "EA/箱", "箱", "天", "个", "单", "行", "条", "吨", "台", "件", "个月", "个完整自然日", "按来源单位", "%"}
)
PUBLIC_TABLE_SCHEMAS: dict[str, dict[str, Any]] = {
    **{
        key: {
            "title": title,
            "empty_label": "查询成功，但当前条件下没有符合条件的数据",
            "columns": (
                {"key":"code","label":code_label,"kind":"text","align":"left"},
                {"key":"name","label":name_label,"kind":"text","align":"left"},
                {"key":"quantity","label":"总发货量","kind":"number","align":"right","unit":"EA"},
                {"key":"shipment_quantity","label":"其余渠道实发量","kind":"number","align":"right","unit":"EA"},
                {"key":"community_quantity","label":"社区平台请求量","kind":"number","align":"right","unit":"EA"},
                {"key":"outbound_documents","label":"出库单数","kind":"number","align":"right","unit":"单"},
                {"key":"community_orders","label":"社区平台订单数","kind":"number","align":"right","unit":"单"},
            ),
        }
        for key, title, code_label, name_label in (
            ("business_sales_by_warehouse", "销售数量 · 按仓库", "仓库编码", "仓库描述"),
            ("business_sales_top_sku", "销售数量 · SKU TOP", "商品编码", "商品描述"),
        )
    },
    "sales_price_group": {
        "title": "渠道与履约类型销售出库",
        "empty_label": "查询成功，当前范围没有有效销售出库记录",
        "columns": (
            {"key":"channel_code","label":"渠道编码","kind":"text","align":"left"},
            {"key":"channel_name","label":"渠道细分","kind":"text","align":"left"},
            {"key":"fulfillment_type","label":"2C/2B","kind":"text","align":"left"},
            {"key":"outbound_count","label":"出库单数","kind":"number","align":"right","unit":"单"},
            {"key":"quantity","label":"实发数量","kind":"number","align":"right","unit":"EA"},
            {"key":"sku_count","label":"SKU数","kind":"number","align":"right","unit":"个"},
        ),
    },
    "so07_order_details": {
        "title": "SO07社团销售订单",
        "empty_label": "查询成功，当前范围没有匹配的SO07社团销售订单",
        "columns": (
            {"key":"cargo_owner","label":"货主","kind":"text","align":"left"},
            {"key":"sales_order_code","label":"订单号","kind":"text","align":"left"},
            {"key":"order_created_at","label":"订单创建时间","kind":"text","align":"left"},
            {"key":"customer_code","label":"客户编码","kind":"text","align":"left","nullable":True},
            {"key":"customer_name","label":"客户名称","kind":"text","align":"left","nullable":True},
            {"key":"warehouse_code","label":"仓库编码","kind":"text","align":"left"},
            {"key":"warehouse_name","label":"仓库名称","kind":"text","align":"left","nullable":True},
            {"key":"sku_code","label":"SKU","kind":"text","align":"left","nullable":True},
            {"key":"sku_name","label":"商品","kind":"text","align":"left","nullable":True},
            {"key":"request_quantity","label":"原始请求数量","kind":"number","align":"right","nullable":True},
            {"key":"quantity_unit_reference","label":"源数量单位代码","kind":"text","align":"left","nullable":True},
            {"key":"sales_unit_reference","label":"源销售单位代码","kind":"text","align":"left","nullable":True},
            {"key":"order_status","label":"订单状态","kind":"text","align":"left","nullable":True},
            {"key":"order_detail_state","label":"明细状态","kind":"text","align":"left"},
        ),
    },
    "sales_order_return_orders": {
        "title": "退货分析订单范围",
        "empty_label": "查询成功，当前范围没有匹配的销售订单",
        "columns": (
            {"key":"cargo_owner","label":"货主","kind":"text","align":"left"},
            {"key":"sales_order_code","label":"销售订单","kind":"text","align":"left"},
            {"key":"order_created","label":"订单创建时间","kind":"text","align":"left"},
            {"key":"order_detail_state","label":"明细状态","kind":"text","align":"left"},
        ),
    },
    "sales_order_return_quantities": {
        "title": "订单退货数量核对",
        "empty_label": "当前订单范围没有可展示的商品数量",
        "columns": (
            {"key":"cargo_owner","label":"货主","kind":"text","align":"left"},
            {"key":"sales_order_code","label":"销售订单","kind":"text","align":"left"},
            {"key":"sku_code","label":"SKU","kind":"text","align":"left"},
            {"key":"order_ship_quantity","label":"订单实发原始数量","kind":"number","align":"right","nullable":True,"signed":True},
            {"key":"returned_quantity","label":"退货原始数量","kind":"number","align":"right","signed":True},
            {"key":"net_sales_quantity","label":"净销售原始数量","kind":"number","align":"right","nullable":True,"signed":True},
            {"key":"return_document_count","label":"关联退货单数","kind":"number","align":"right","unit":"单"},
            {"key":"sales_detail_count","label":"销售明细行数","kind":"number","align":"right"},
            {"key":"return_detail_count","label":"退货明细行数","kind":"number","align":"right"},
            {"key":"quantity_state","label":"数量状态","kind":"text","align":"left"},
        ),
    },
    "sales_outbound_link": {
        "title": "出库单关联链路",
        "empty_label": "查询成功，当前范围没有匹配的出库单",
        "columns": (
            {"key":"cargo_owner","label":"货主","kind":"text","align":"left"},
            {"key":"outbound_code","label":"出库单号","kind":"text","align":"left"},
            {"key":"referenced_order_code","label":"引用的销售订单","kind":"text","align":"left","nullable":True},
            {"key":"linked_sales_order_code","label":"已匹配销售订单","kind":"text","align":"left","nullable":True},
            {"key":"source_order_code","label":"头部来源单号","kind":"text","align":"left","nullable":True},
            {"key":"detail_source_order_code","label":"订单来源编码","kind":"text","align":"left","nullable":True},
            {"key":"source_platform_code","label":"来源平台","kind":"text","align":"left","nullable":True},
            {"key":"referenced_order_type","label":"出库关联类型","kind":"text","align":"left","nullable":True},
            {"key":"order_type","label":"订单类型","kind":"text","align":"left","nullable":True},
            {"key":"link_ship_at","label":"出库时间","kind":"text","align":"left","nullable":True},
            {"key":"relationship_state","label":"关联状态","kind":"text","align":"left"},
        ),
    },
    "sales_outbound_details": {
        "title": "实际销售出库明细",
        "empty_label": "查询成功，当前范围没有有效销售出库明细",
        "columns": (
            {"key":"cargo_owner","label":"货主","kind":"text","align":"left"},
            {"key":"warehouse_code","label":"仓库编码","kind":"text","align":"left"},
            {"key":"outbound_code","label":"出库单号","kind":"text","align":"left"},
            {"key":"sku_code","label":"SKU","kind":"text","align":"left"},
            {"key":"ship_at","label":"出库时间","kind":"text","align":"left"},
            {"key":"quantity","label":"实发数量","kind":"number","align":"right","unit":"EA"},
        ),
    },
    "sales_ranked_drilldown": {
        "title": "成品销售出库排名",
        "empty_label": "查询成功，当前范围没有匹配的成品销售出库",
        "columns": (
            {"key":"rank","label":"排名","kind":"number","align":"right"},
            {"key":"sku_code","label":"SKU","kind":"text","align":"left"},
            {"key":"sku_name","label":"商品","kind":"text","align":"left"},
            {"key":"quantity","label":"实发数量","kind":"number","align":"right","unit":"EA"},
            {"key":"outbound_count","label":"出库单数","kind":"number","align":"right","unit":"单"},
        ),
    },
    "sales_ranked_sku_detail": {
        "title": "成品销售出库明细",
        "empty_label": "当前排名没有匹配的出库明细",
        "columns": (
            {"key":"sku_code","label":"SKU","kind":"text","align":"left"},
            {"key":"cargo_owner","label":"货主","kind":"text","align":"left"},
            {"key":"warehouse_code","label":"仓库编码","kind":"text","align":"left"},
            {"key":"warehouse_name","label":"仓库名称","kind":"text","align":"left"},
            {"key":"customer_code","label":"客户编码","kind":"text","align":"left","nullable":True},
            {"key":"outbound_code","label":"出库单号","kind":"text","align":"left"},
            {"key":"ship_at","label":"出库时间","kind":"text","align":"left"},
            {"key":"quantity","label":"实发数量","kind":"number","align":"right","unit":"EA"},
        ),
    },
    "dms_manager_orders": {
        "title": "DMS城市经理订单统计",
        "empty_label": "查询成功，当前范围没有匹配的DMS销售订单",
        "columns": (
            {"key":"order_date","label":"创建日期","kind":"text","align":"left"},
            {"key":"province","label":"省区","kind":"text","align":"left","nullable":True},
            {"key":"manager_key","label":"经理标识","kind":"text","align":"left","nullable":True},
            {"key":"manager_name","label":"城市经理","kind":"text","align":"left","nullable":True},
            {"key":"manager_state","label":"经理资料状态","kind":"text","align":"left"},
            {"key":"customer_code","label":"客户编码","kind":"text","align":"left","nullable":True},
            {"key":"customer_name","label":"客户名称","kind":"text","align":"left","nullable":True},
            {"key":"order_count","label":"订单数","kind":"number","align":"right","unit":"单"},
            {"key":"missing_manager_order_count","label":"经理缺失订单数","kind":"number","align":"right","unit":"单"},
        ),
    },
    "dms_points_orders_monthly": {
        "title": "积分订单月度统计",
        "empty_label": "查询成功，当前范围没有匹配的积分订单",
        "columns": (
            {"key":"month","label":"创建月份","kind":"text","align":"left"},
            {"key":"order_count","label":"订单数","kind":"number","align":"right","unit":"单"},
        ),
    },
    "dms_points_orders_detail": {
        "title": "积分订单明细",
        "empty_label": "查询成功，当前范围没有匹配的积分订单",
        "columns": (
            {"key":"sales_order_code","label":"订单号","kind":"text","align":"left"},
            {"key":"customer_code","label":"客户编码","kind":"text","align":"left","nullable":True},
            {"key":"customer_name","label":"客户名称","kind":"text","align":"left","nullable":True},
            {"key":"created_time","label":"创建时间","kind":"text","align":"left"},
        ),
    },
    "sales_sku_name_quality": {
        "title":"SKU名称与实际出库核对",
        "empty_label":"查询成功，当前范围没有有效销售出库记录",
        "columns": (
            {"key":"cargo_owner","label":"货主","kind":"text","align":"left"},
            {"key":"sku_code","label":"SKU","kind":"text","align":"left"},
            {"key":"sku_name","label":"商品","kind":"text","align":"left"},
            {"key":"quantity","label":"实际销售出库","kind":"number","align":"right","unit":"EA"},
            {"key":"outbound_count","label":"出库单数","kind":"number","align":"right","unit":"单"},
            {"key":"shipment_name","label":"出库记录名称","kind":"text","align":"left","nullable":True},
            {"key":"order_name","label":"订单记录名称","kind":"text","align":"left","nullable":True},
            {"key":"master_name_variants","label":"主数据名称数","kind":"number","align":"right"},
            {"key":"missing_name_rows","label":"空名称记录数","kind":"number","align":"right"},
            {"key":"missing_name_sources","label":"缺失来源数","kind":"number","align":"right"},
            {"key":"name_variants","label":"不同名称数","kind":"number","align":"right"},
            {"key":"name_status","label":"名称状态","kind":"text","align":"left"},
        ),
    },
    "sales_outbound_boxes": {
        "title": "成品实际出库与箱数",
        "empty_label": "查询成功，当前范围没有有效销售出库记录",
        "columns": (
            {"key":"ship_date","label":"出库日期","kind":"text","align":"left"},
            {"key":"cargo_owner","label":"货主","kind":"text","align":"left"},
            {"key":"warehouse_code","label":"仓库编码","kind":"text","align":"left"},
            {"key":"warehouse_name","label":"仓库名称","kind":"text","align":"left"},
            {"key":"sku_code","label":"SKU","kind":"text","align":"left"},
            {"key":"customer_channel","label":"客户渠道","kind":"text","align":"left"},
            {"key":"quantity","label":"实际销售出库","kind":"number","align":"right","unit":"EA"},
            {"key":"outbound_count","label":"出库单数","kind":"number","align":"right","unit":"单"},
            {"key":"box_spec","label":"箱规","kind":"number","align":"right","unit":"EA/箱","nullable":True},
            {"key":"box_quantity","label":"实发箱数","kind":"number","align":"right","unit":"箱","nullable":True},
            {"key":"box_state","label":"换算状态","kind":"text","align":"left"},
        ),
    },
    "purchase_received_detail": {
        "title": "采购入库明细",
        "empty_label": "查询成功，当前范围没有匹配的采购入库明细",
        "columns": (
            {"key": "cargo_owner", "label": "货主", "kind": "text", "align": "left"},
            {"key": "inbound_code", "label": "入库单", "kind": "text", "align": "left"},
            {"key": "detail_id", "label": "明细行标识", "kind": "text", "align": "left"},
            {"key": "item_code", "label": "行号", "kind": "text", "align": "left", "nullable": True},
            {"key": "warehouse_code", "label": "仓库编码", "kind": "text", "align": "left"},
            {"key": "warehouse_name", "label": "仓库名称", "kind": "text", "align": "left", "nullable": True},
            {"key": "sku_code", "label": "SKU", "kind": "text", "align": "left"},
            {"key": "batch_number", "label": "批次", "kind": "text", "align": "left", "nullable": True},
            {"key": "production_date", "label": "生产日期", "kind": "text", "align": "left"},
            {"key": "received_quantity", "label": "实收入库数量", "kind": "number", "align": "right"},
            {"key": "quantity_unit", "label": "来源单位编码", "kind": "text", "align": "left"},
            {"key": "actual_arrival_date", "label": "实际入库时间", "kind": "text", "align": "left"},
        ),
    },
    "inventory_occupation_split": {
        "title": "库存占用拆分",
        "empty_label": "查询成功，当前范围没有匹配的库存占用记录",
        "columns": (
            {"key": "transfer_occupation", "label": "调拨占用", "kind": "number", "align": "right", "unit": "EA", "signed": True},
            {"key": "sales_occupation", "label": "销售占用", "kind": "number", "align": "right", "unit": "EA", "signed": True},
            {"key": "occupation_total", "label": "占用合计", "kind": "number", "align": "right", "unit": "EA", "signed": True},
            {"key": "associated_order_count", "label": "关联单号数", "kind": "number", "align": "right", "unit": "个"},
        ),
    },
    "purchase_received_by_production_date": {
        "title": "采购入库产成品汇总",
        "empty_label": "查询成功，当前范围没有匹配的采购入库产成品",
        "columns": (
            {"key": "production_day", "label": "生产日期", "kind": "text", "align": "left"},
            {"key": "source_unit", "label": "来源单位", "kind": "text", "align": "left"},
            {"key": "receipt_count", "label": "入库单数", "kind": "number", "align": "right", "unit": "单"},
            {"key": "detail_count", "label": "明细行数", "kind": "number", "align": "right", "unit": "行"},
            {"key": "received_quantity", "label": "实收入库数量", "kind": "number", "align": "right", "unit": "按来源单位", "signed": True},
        ),
    },
    "purchase_received_by_warehouse_production_date": {
        "title": "采购入库产成品汇总",
        "empty_label": "查询成功，当前范围没有匹配的采购入库产成品",
        "columns": (
            {"key": "warehouse_name", "label": "仓库", "kind": "text", "align": "left"},
            {"key": "production_day", "label": "生产日期", "kind": "text", "align": "left"},
            {"key": "source_unit", "label": "来源单位", "kind": "text", "align": "left"},
            {"key": "receipt_count", "label": "入库单数", "kind": "number", "align": "right", "unit": "单"},
            {"key": "sku_count", "label": "SKU数", "kind": "number", "align": "right", "unit": "个"},
            {"key": "planned_quantity", "label": "计划入库数量", "kind": "number", "align": "right", "unit": "按来源单位"},
            {"key": "received_quantity", "label": "实收入库数量", "kind": "number", "align": "right", "unit": "按来源单位"},
            {"key": "empty_production_detail_count", "label": "生产日期为空明细", "kind": "number", "align": "right", "unit": "行"},
        ),
    },
    "sales_aggregate": {
        "title": "销售出库数量与单数",
        "empty_label": "查询成功，当前范围没有有效销售出库记录",
        "selectable_columns": True,
        "required_columns": ("quantity", "outbound_count"),
        "columns": (
            {"key": "ship_date", "label": "出库日期", "kind": "text", "align": "left"},
            {"key": "cargo_owner", "label": "货主", "kind": "text", "align": "left"},
            {"key": "warehouse_code", "label": "仓库编码", "kind": "text", "align": "left"},
            {"key": "warehouse_name", "label": "仓库名称", "kind": "text", "align": "left"},
            {"key": "customer_channel", "label": "客户渠道", "kind": "text", "align": "left"},
            {"key": "shipment_status", "label": "出库状态", "kind": "text", "align": "left"},
            {"key": "ref_order_type", "label": "出库类型", "kind": "text", "align": "left"},
            {"key": "fulfillment_type", "label": "履约类型", "kind": "text", "align": "left"},
            {"key": "sku_code", "label": "SKU", "kind": "text", "align": "left"},
            {"key": "quantity", "label": "实际销售出库", "kind": "number", "align": "right", "unit": "EA"},
            {"key": "outbound_count", "label": "出库单数", "kind": "number", "align": "right", "unit": "单"},
            {"key": "zero_outbound_count", "label": "零数量出库单数", "kind": "number", "align": "right", "unit": "单"},
            {"key": "sku_count", "label": "SKU数", "kind": "number", "align": "right", "unit": "个"},
        ),
    },
    "sales_analysis": {
        "title": "销售出库分析",
        "empty_label": "当前范围没有有效销售出库记录",
        "selectable_columns": True,
        "required_columns": ("quantity", "outbound_count"),
        "columns": (
            {"key": "ship_date", "label": "出库日期", "kind": "text", "align": "left"},
            {"key": "cargo_owner", "label": "货主", "kind": "text", "align": "left"},
            {"key": "warehouse_code", "label": "仓库编码", "kind": "text", "align": "left"},
            {"key": "warehouse_name", "label": "仓库名称", "kind": "text", "align": "left"},
            {"key": "customer_channel", "label": "客户渠道", "kind": "text", "align": "left"},
            {"key": "shipment_status", "label": "出库状态", "kind": "text", "align": "left"},
            {"key": "ref_order_type", "label": "出库类型", "kind": "text", "align": "left"},
            {"key": "fulfillment_type", "label": "履约类型", "kind": "text", "align": "left"},
            {"key": "sku_code", "label": "SKU", "kind": "text", "align": "left"},
            {"key": "quantity", "label": "实发数量", "kind": "number", "align": "right", "unit": "EA"},
            {"key": "outbound_count", "label": "出库单数", "kind": "number", "align": "right", "unit": "单"},
            {"key": "sku_count", "label": "SKU数", "kind": "number", "align": "right", "unit": "个"},
            {"key": "quantity_share", "label": "实发数量占比", "kind": "text", "align": "right"},
            {"key": "daily_average", "label": "日均实发数量", "kind": "number", "align": "right", "unit": "EA/日"},
        ),
    },
    "sales_timeseries": {
        "title": "销售出库日历与窗口分析",
        "empty_label": "当前范围没有有效销售出库记录",
        "columns": (
            {"key": "ship_date", "label": "日期", "kind": "text", "align": "left"},
            {"key": "quantity", "label": "实发数量", "kind": "number", "align": "right", "unit": "EA"},
            {"key": "outbound_count", "label": "出库单数", "kind": "number", "align": "right", "unit": "单"},
            {"key": "cumulative_quantity", "label": "累计实发数量", "kind": "number", "align": "right", "unit": "EA"},
            {"key": "moving_average", "label": "移动平均", "kind": "number", "align": "right", "unit": "EA/日", "nullable": True},
        ),
    },
    "warehouse_period_comparison": {
        "title": "仓库销售出库期间对比",
        "empty_label": "当前范围没有可展示的对比数据",
        "columns": (
            {"key":"cargo_owner","label":"货主","kind":"text","align":"left"},
            {"key":"warehouse_code","label":"仓库编码","kind":"text","align":"left"},
            {"key":"warehouse_name","label":"仓库","kind":"text","align":"left"},
            {"key":"period_1_quantity","label":"期间一出库数量","kind":"number","align":"right","unit":"EA","nullable":True},
            {"key":"period_2_quantity","label":"期间二出库数量","kind":"number","align":"right","unit":"EA","nullable":True},
            {"key":"period_1_daily_average","label":"期间一日均量","kind":"number","align":"right","unit":"EA/日","nullable":True},
            {"key":"period_2_daily_average","label":"期间二日均量","kind":"number","align":"right","unit":"EA/日","nullable":True},
            {"key":"period_1_record_state","label":"期间一记录状态","kind":"text","align":"left"},
            {"key":"period_2_record_state","label":"期间二记录状态","kind":"text","align":"left"},
            {"key":"daily_average_difference","label":"日均量差额","kind":"number","align":"right","unit":"EA/日","nullable":True,"signed":True},
            {"key":"daily_average_change_rate","label":"日均变化率","kind":"text","align":"right"},
        ),
    },
    "warehouse_type_count": {
        "title": "仓库类型统计", "empty_label": "当前名称范围没有匹配的仓库",
        "columns": (
            {"key":"warehouse_type","label":"仓库类型","kind":"text","align":"left"},
            {"key":"warehouse_count","label":"仓库数","kind":"number","align":"right","unit":"个"},
        ),
    },
    "warehouse_type_summary": {
        "title": "仓库类型汇总", "empty_label": "当前名称范围没有匹配的仓库",
        "columns": (
            {"key":"warehouse_type","label":"仓库类型","kind":"text","align":"left"},
            {"key":"warehouse_count","label":"仓库数量","kind":"number","align":"right","unit":"个"},
            {"key":"warehouse_share","label":"数量占比（%）","kind":"number","align":"right"},
        ),
    },
    "warehouse_directory_detail": {
        "title": "仓库清单", "empty_label": "当前名称范围没有匹配的仓库",
        "columns": (
            {"key":"cargo_owner","label":"货主编码","kind":"text","align":"left"},
            {"key":"warehouse_code","label":"仓库编码","kind":"text","align":"left"},
            {"key":"warehouse_name","label":"仓库名称","kind":"text","align":"left"},
            {"key":"warehouse_type","label":"仓库类型","kind":"text","align":"left"},
        ),
    },
    "warehouse_directory": {
        "title": "仓库资料",
        "empty_label": "查询成功，当前名称范围没有匹配的仓库资料",
        "columns": (
            {"key": "cargo_owner", "label": "货主", "kind": "text", "align": "left"},
            {"key": "warehouse_code", "label": "仓库编码", "kind": "text", "align": "left"},
            {"key": "warehouse_name", "label": "仓库名称", "kind": "text", "align": "left"},
            {"key": "warehouse_type", "label": "仓库类型", "kind": "text", "align": "left"},
            {"key": "warehouse_node", "label": "仓库节点", "kind": "text", "align": "left"},
        ),
    },
    "center_warehouse_turnover": {
        "title": "经营中心区间周转",
        "empty_label": "当前没有可展示的经营中心汇总",
        "columns": (
            {"key": "warehouse", "label": "经营中心", "kind": "text", "align": "left"},
            {"key": "current_stock", "label": "可用成品库存＋在途", "kind": "number", "align": "right", "unit": "EA"},
            {"key": "daily_sales", "label": "区间日均销量", "kind": "number", "align": "right", "unit": "EA/日"},
            {"key": "turnover_text", "label": "周转天数", "kind": "text", "align": "right"},
        ),
    },
    "center_warehouse_turnover_7_14": {
        "title": "经营中心7日周转（分仓14日）",
        "empty_label": "当前没有可展示的经营中心汇总",
        "columns": (
            {"key": "warehouse", "label": "经营中心", "kind": "text", "align": "left"},
            {"key": "current_stock", "label": "可用成品库存＋在途", "kind": "number", "align": "right", "unit": "EA"},
            {"key": "daily_sales", "label": "近7天日均销量", "kind": "number", "align": "right", "unit": "EA/日"},
            {"key": "turnover_text", "label": "周转天数", "kind": "text", "align": "right"},
        ),
    },
    "center_warehouse_turnover_14_detail": {
        "title": "各分仓14日周转",
        "empty_label": "当前没有可展示的分仓周转明细",
        "columns": (
            {"key": "belonging_center", "label": "归属中心仓", "kind": "text", "align": "left"},
            {"key": "warehouse", "label": "分仓", "kind": "text", "align": "left"},
            {"key": "current_stock", "label": "可用成品库存＋在途", "kind": "number", "align": "right", "unit": "EA"},
            {"key": "daily_sales", "label": "近14天日均销量", "kind": "number", "align": "right", "unit": "EA/日"},
            {"key": "turnover_text", "label": "周转天数", "kind": "text", "align": "right"},
        ),
    },
    "sales_outbound_by_warehouse": {
        "title": "按仓销售出库",
        "empty_label": "当前日期没有匹配的有效销售出库记录",
        "columns": (
            {"key": "warehouse", "label": "仓库", "kind": "text", "align": "left"},
            {
                "key": "quantity",
                "label": "实际销售出库",
                "kind": "number",
                "align": "right",
                "unit": "EA",
            },
        ),
    },
    "sales_outbound_warehouse_quality": {
        "title": "按仓实际销售出库",
        "empty_label": "当前日期没有匹配的有效销售出库记录",
        "columns": (
            {"key":"warehouse_name","label":"仓库","kind":"text","align":"left"},
            {"key":"outbound_count","label":"出库单数","kind":"number","align":"right","unit":"单"},
            {"key":"sales_order_count","label":"销售订单数","kind":"number","align":"right","unit":"单"},
            {"key":"quantity","label":"实际销售出库","kind":"number","align":"right","unit":"EA"},
            {"key":"outbound_tons","label":"出库吨数","kind":"number","align":"right","unit":"吨"},
            {"key":"missing_net_weight_quantity","label":"缺少净重数量","kind":"number","align":"right","unit":"EA"},
        ),
    },
    "inventory_availability_detail": {
        "title": "当前库存与可用天数",
        "empty_label": "当前范围没有可展示的库存组件",
        "columns": (
            {"key":"warehouse_name","label":"仓库","kind":"text","align":"left"},
            {"key":"sku_code","label":"SKU","kind":"text","align":"left"},
            {"key":"in_stock","label":"在库数量","kind":"number","align":"right","unit":"EA"},
            {"key":"stock_lock","label":"锁定数量","kind":"number","align":"right","unit":"EA"},
            {"key":"freeze","label":"冻结数量","kind":"number","align":"right","unit":"EA"},
            {"key":"sales_occupation","label":"销售占用","kind":"number","align":"right","unit":"EA"},
            {"key":"transfer_occupation","label":"调拨占用","kind":"number","align":"right","unit":"EA"},
            {"key":"available","label":"可用量","kind":"number","align":"right","unit":"EA","nullable":True,"signed":True},
            {"key":"average_7d_outbound","label":"近7天平均出库量","kind":"number","align":"right","unit":"EA/日"},
            {"key":"available_days","label":"库存可用天数","kind":"number","align":"right","unit":"天","nullable":True},
        ),
    },
    "sales_outbound_by_warehouse_fulfillment": {
        "title": "成品销售出库 2C/2B",
        "empty_label": "当前范围没有有效产成品出库记录",
        "columns": (
            {"key":"ship_date","label":"日期","kind":"text","align":"left"},
            {"key":"warehouse_name","label":"仓库","kind":"text","align":"left"},
            {"key":"fulfillment_2c","label":"2C 实发数量","kind":"number","align":"right","unit":"EA"},
            {"key":"fulfillment_2b","label":"2B 实发数量","kind":"number","align":"right","unit":"EA"},
            {"key":"total_quantity","label":"总实发数量","kind":"number","align":"right","unit":"EA"},
        ),
    },
    "dms_redemption_by_province": {
        "title": "DMS 积分兑换订单",
        "empty_label": "当前范围没有有效的积分兑换订单",
        "columns": (
            {"key":"province","label":"省区","kind":"text","align":"left"},
            {"key":"order_count","label":"订单数","kind":"number","align":"right","unit":"单"},
            {"key":"customer_count","label":"客户数","kind":"number","align":"right","unit":"个"},
            {"key":"redemption_quantity","label":"兑换数量","kind":"number","align":"right","unit":"件"},
            {"key":"air_fryer_quantity","label":"空气炸锅兑换数量","kind":"number","align":"right","unit":"件"},
        ),
    },
    "transfer_outbound_by_warehouse": {
        "title": "按仓调拨出库",
        "empty_label": "当前日期没有匹配的有效调拨出库记录",
        "columns": (
            {"key": "warehouse", "label": "仓库", "kind": "text", "align": "left"},
            {
                "key": "quantity",
                "label": "实际调拨出库",
                "kind": "number",
                "align": "right",
                "unit": "EA",
            },
        ),
    },
    "sales_outbound_by_type": {
        "title": "按类型销售出库",
        "empty_label": "当前日期没有匹配的有效销售出库记录",
        "columns": (
            {"key": "ref_order_type", "label": "出库类型", "kind": "text", "align": "left"},
            {"key": "fulfillment_type", "label": "履约类型", "kind": "text", "align": "left"},
            {"key": "quantity", "label": "实际销售出库", "kind": "number", "align": "right", "unit": "EA"},
        ),
    },
    "sales_outbound_by_owner": {
        "title": "按货主销售出库",
        "empty_label": "当前日期没有匹配的有效销售出库记录",
        "columns": (
            {"key": "cargo_owner", "label": "货主", "kind": "text", "align": "left"},
            {"key": "fulfillment_type", "label": "履约类型", "kind": "text", "align": "left"},
            {"key": "quantity", "label": "实际销售出库", "kind": "number", "align": "right", "unit": "EA"},
        ),
    },
    "sales_outbound_top_sku": {
        "title": "销售出库 Top SKU",
        "empty_label": "当前日期没有匹配的有效销售出库记录",
        "columns": (
            {"key": "rank", "label": "排名", "kind": "number", "align": "center"},
            {"key": "sku_name", "label": "商品", "kind": "text", "align": "left"},
            {"key": "sku_code", "label": "SKU", "kind": "text", "align": "left"},
            {
                "key": "quantity",
                "label": "销售出库",
                "kind": "number",
                "align": "right",
                "unit": "EA",
            },
            {
                "key": "order_count",
                "label": "出库单数",
                "kind": "number",
                "align": "right",
                "unit": "单",
            },
        ),
    },
    "dms_points_redemption": {
        "title": "DMS 积分兑换",
        "empty_label": "当前范围没有有效的积分兑换记录",
        "columns": (
            {"key": "month", "label": "月份", "kind": "text", "align": "left"},
            {"key": "sku_name", "label": "商品", "kind": "text", "align": "left"},
            {
                "key": "redemption_quantity",
                "label": "兑换数量",
                "kind": "number",
                "align": "right",
                "unit": "台",
            },
            {
                "key": "order_count",
                "label": "兑换订单数",
                "kind": "number",
                "align": "right",
                "unit": "单",
            },
        ),
    },
    "historical_inventory_turnover": {
        "title": "库存周转",
        "empty_label": "当前范围没有可展示的周转数据",
        "columns": (
            {"key": "metric", "label": "指标", "kind": "text", "align": "left"},
            {"key": "value", "label": "数值", "kind": "number", "align": "right", "nullable": True},
            {"key": "unit", "label": "单位", "kind": "text", "align": "center"},
        ),
    },
    "realtime_inventory_turnover": {
        "title": "库存周转",
        "empty_label": "当前范围没有可展示的周转数据",
        "columns": (
            {"key": "metric", "label": "指标", "kind": "text", "align": "left"},
            {"key": "value", "label": "数值", "kind": "number", "align": "right", "nullable": True},
            {"key": "unit", "label": "单位", "kind": "text", "align": "center"},
        ),
    },
    "realtime_inventory_turnover_by_warehouse": {
        "title": "按仓库库存周转",
        "empty_label": "当前范围没有可展示的仓库周转数据",
        "columns": (
            {"key": "warehouse_code", "label": "仓库编码", "kind": "text", "align": "left", "nullable": True},
            {"key": "warehouse_name", "label": "仓库", "kind": "text", "align": "left"},
            {"key": "inventory", "label": "可用成品库存 + 在途", "kind": "number", "align": "right", "unit": "EA", "signed": True, "nullable": True},
            {"key": "daily_average", "label": "实际销售出库日均", "kind": "number", "align": "right", "unit": "EA/日", "nullable": True},
            {"key": "turnover_days", "label": "库存周转", "kind": "number", "align": "right", "unit": "天", "nullable": True},
            {"key": "turnover_state", "label": "数据状态", "kind": "text", "align": "left"},
        ),
    },
    "batch_inventory": {
        "title": "生产批次库存",
        "empty_label": "当前范围没有可展示的库存数据",
        "columns": (
            {"key": "production_date", "label": "生产日期", "kind": "text", "align": "left"},
            {"key": "warehouse", "label": "仓库", "kind": "text", "align": "left"},
            {
                "key": "current_stock",
                "label": "当前库存",
                "nullable": True,
                "kind": "number",
                "align": "right",
                "unit": "EA",
            },
            {
                "key": "available_stock",
                "label": "实际库存",
                "nullable": True,
                "kind": "number",
                "align": "right",
                "unit": "EA",
            },
            {
                "key": "snapshot_stock",
                "label": "历史快照库存",
                "nullable": True,
                "kind": "number",
                "align": "right",
                "unit": "EA",
            },
            {"key": "snapshot_date", "label": "快照日期", "kind": "text", "align": "left"},
        ),
    },
    "current_inventory": {
        "title": "当前实时库存",
        "empty_label": "当前范围没有可展示的库存数据",
        "columns": (
            {"key": "warehouse", "label": "仓库", "kind": "text", "align": "left"},
            {
                "key": "quantity",
                "label": "当前库存",
                "kind": "number",
                "align": "right",
                "unit": "EA",
            },
        ),
    },
    "current_inventory_owner_summary": {
        "title": "按货主汇总的当前成品库存",
        "empty_label": "当前范围没有可展示的成品库存记录",
        "columns": (
            {"key": "cargo_owner", "label": "货主编码", "kind": "text", "align": "left"},
            {"key": "available_inventory", "label": "可用库存", "kind": "number", "align": "right", "unit": "EA"},
            {"key": "in_transit_inventory", "label": "在途库存", "kind": "number", "align": "right", "unit": "EA"},
            {"key": "finished_goods_inventory", "label": "成品库存", "kind": "number", "align": "right", "unit": "EA"},
            {"key": "in_transit_share", "label": "在途占比", "kind": "number", "align": "right", "unit": "%", "nullable": True},
            {"key": "inventory_share", "label": "库存占比", "kind": "number", "align": "right", "unit": "%", "nullable": True},
        ),
    },
    "inbound_by_warehouse": {
        "title": "按仓实际入库",
        "empty_label": "当前日期没有匹配的有效入库记录",
        "columns": (
            {"key": "warehouse", "label": "仓库", "kind": "text", "align": "left"},
            {
                "key": "inbound_quantity",
                "label": "入库数量",
                "kind": "number",
                "align": "right",
                "unit": "EA",
            },
            {
                "key": "inbound_tons",
                "label": "入库吨数",
                "kind": "number",
                "align": "right",
                "unit": "吨",
            },
        ),
    },
    "inbound_top_sku": {
        "title": "入库量最高 SKU",
        "empty_label": "当前日期没有匹配的有效入库记录",
        "columns": (
            {"key": "sku_name", "label": "商品", "kind": "text", "align": "left"},
            {
                "key": "inbound_quantity",
                "label": "入库数量",
                "kind": "number",
                "align": "right",
                "unit": "EA",
            },
            {
                "key": "inbound_tons",
                "label": "入库吨数",
                "kind": "number",
                "align": "right",
                "unit": "吨",
            },
        ),
    },
}
WECom_USER_ID_HEADER = "x-wecom-user-id"
WECom_TIMESTAMP_HEADER = "x-wecom-identity-timestamp"
WECom_SIGNATURE_HEADER = "x-wecom-identity-signature"
WECom_TIMESTAMP_TOLERANCE_SECONDS = 300
SENSITIVE_ENVIRONMENT_KEYS = (
    "DORIS_HOST",
    "DORIS_PORT",
    "DORIS_USER",
    "DORIS_PASSWORD",
    "DORIS_DATABASE",
    "AGENT_TRUSTED_RELEASE_SHA256",
    "AGENT_ALLOW_UNAPPROVED_CONTRACTS",
    "AGENT_CODEX_API_KEY",
    "CODEX_ACCESS_TOKEN",
    "CODEX_API_KEY",
    "OPENAI_API_KEY",
    "AGENT_CHAT_IDENTITY_SECRET",
    "AGENT_CHAT_PRINCIPAL_PEPPER",
    "AGENT_CHAT_SESSION_DB_PATH",
    "AGENT_INSTALLATION_ID_PATH",
    "AGENT_CHAT_INTERNAL_RESULT_PATH",
    "AGENT_CHAT_INTERNAL_RESULT_NONCE",
)
ANSI_ESCAPE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _process_failure_signature(stderr: Any) -> str:
    """Classify child stderr without copying diagnostics to the browser/log."""

    if not isinstance(stderr, str) or not stderr.strip():
        return "stderr_empty"
    text = ANSI_ESCAPE.sub("", stderr).casefold()
    patterns = (
        ("auth", r"unauthorized|forbidden|api[ _-]?key|authentication|credential"),
        ("rate_limit", r"rate[ _-]?limit|too many requests|overloaded|429"),
        ("timeout", r"timeout|timed out|deadline exceeded"),
        ("network", r"connection|connect|reset|refused|unreachable|eof"),
        ("mcp", r"mcp|tool call|tool result"),
        ("import", r"importerror|modulenotfounderror"),
        ("configuration", r"configuration|invalid.*(model|provider|url)"),
        ("exception", r"traceback|exception|error|failure|panic"),
    )
    matches = [name for name, pattern in patterns if re.search(pattern, text)]
    return ",".join(matches[:4]) or "unclassified"


def _is_transient_child_failure(stderr: Any) -> bool:
    """Return whether one failed worker attempt is worth replaying once."""

    signature = _process_failure_signature(stderr)
    labels = set(signature.split(","))
    if "auth" in labels or "configuration" in labels or "import" in labels:
        return False
    return bool(labels & {"network", "rate_limit", "mcp", "stderr_empty"})
WECom_USER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:@-]{1,128}$")
INSTALLATION_ID_PATTERN = re.compile(r"^inst_[A-Za-z0-9_-]{32,80}$")


def _is_loopback_url(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.scheme != "http" or not parsed.hostname:
        return False
    if parsed.hostname.casefold() == "localhost":
        return True
    try:
        return ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < minimum or value > maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().casefold()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be a boolean")


def _artifact_root_from_env() -> Path:
    """Resolve the private artifact directory without accepting URL/path data from clients."""

    raw = (os.getenv("AGENT_REPORT_ARTIFACT_ROOT") or "").strip()
    if not raw:
        return DEFAULT_ARTIFACT_ROOT
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    return candidate.resolve()


def _report_file_root_from_env() -> Path:
    raw = (os.getenv("AGENT_REPORT_FILE_ROOT") or "").strip()
    if not raw:
        return DEFAULT_REPORT_FILE_ROOT
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    return candidate.resolve()


def _public_report_artifact(value: Any) -> dict[str, Any] | None:
    """Allow only the opaque artifact metadata needed by the browser."""

    if not isinstance(value, Mapping):
        return None
    artifact_id = str(value.get("artifact_id") or "").strip()
    if not REPORT_ARTIFACT_ID_PATTERN.fullmatch(artifact_id):
        return None
    kind = str(value.get("kind") or "").strip()
    if kind != REPORT_KIND:
        return None
    content_type = str(value.get("content_type") or "").strip().casefold()
    if content_type != "image/png":
        return None
    url = str(value.get("url") or "").strip()
    if url != f"/v1/artifacts/{artifact_id}":
        return None
    title = str(value.get("title") or "").strip()
    period = str(value.get("period") or "").strip()
    if not title or len(title) > 240 or not period or len(period) > 160:
        return None
    try:
        expires_at = int(value.get("expires_at"))
    except (TypeError, ValueError):
        return None
    if expires_at <= 0:
        return None
    return {
        "artifact_id": artifact_id,
        "kind": kind,
        "title": title,
        "period": period,
        "content_type": "image/png",
        "url": url,
        "expires_at": expires_at,
    }


def _report_profile_from_answers(answers: Any) -> str | None:
    """Derive a report profile from the server-issued selection IDs."""

    if not isinstance(answers, list):
        return None
    selected: list[str] = []
    for answer in answers:
        if not isinstance(answer, Mapping):
            continue
        raw = answer.get("option_ids")
        if raw is None:
            raw = [answer.get("option_id")]
        if isinstance(raw, (str, bytes)):
            raw = [raw]
        if isinstance(raw, list):
            selected.extend(str(item).strip() for item in raw if str(item).strip())
    if "available_plus_transit_finished_goods" in selected:
        return "available_plus_transit_finished_goods"
    if "actual_finished_goods" in selected:
        return "actual_finished_goods"
    if "record_deduction_all" in selected:
        return "record_deduction_all"
    return None


def _report_window_semantics_from_answers(answers: Any) -> str | None:
    """Read the explicit window meaning from a server-issued report card."""

    if not isinstance(answers, list):
        return None
    selected: set[str] = set()
    for answer in answers:
        if not isinstance(answer, Mapping):
            continue
        raw = answer.get("option_ids")
        if raw is None:
            raw = [answer.get("option_id")]
        if isinstance(raw, (str, bytes)):
            raw = [raw]
        if isinstance(raw, list):
            selected.update(str(item).strip() for item in raw if str(item).strip())
    if "business_w37_source_window" in selected:
        return "business_w37_source_window"
    if "live_fourteen_complete_days" in selected:
        return "live_fourteen_complete_days"
    return None


def _idempotency_key_from_request(request: Any) -> str:
    """Return a bounded logical-request key without persisting its raw value."""

    value = str(request.headers.get(IDEMPOTENCY_KEY_HEADER.lower()) or "").strip()
    if not value:
        # Legacy clients remain supported, but only clients that send the key
        # can recover a lost response without re-running the business request.
        value = "legacy_" + secrets.token_urlsafe(24)
    if not IDEMPOTENCY_KEY_PATTERN.fullmatch(value):
        raise ParameterValidationError("请求凭证格式无效，请重新提交")
    return value


def _request_fingerprint(payload: Mapping[str, Any]) -> str:
    """Hash normalized request metadata; never store the request body itself."""

    if not isinstance(payload, Mapping):
        raise ValueError("request payload must be an object")
    normalized = dict(payload)
    # Clarification tokens are one-time transport capabilities. A token refresh
    # must still be able to recover the same logical selection under one key.
    normalized.pop("clarification_token", None)
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _runtime_error_payload(exc: Exception) -> tuple[int, dict[str, Any]]:
    """Build the same safe error body used by the global API handlers."""

    if isinstance(exc, RuntimeErrorBase):
        public = exc.as_public_dict()
        details = public.get("details")
        if public.get("code") == "chat_execution_error" and isinstance(details, Mapping):
            reason = details.get("reason")
            # Keep presentation failures distinct from model transport
            # failures.  The browser can then tell the user that Doris ran
            # but the result was withheld, instead of asking them to retry a
            # request that may already have changed warehouse state.
            if reason == "business_answer_unavailable":
                public = {
                    "code": "query_invalid_result",
                    "message": "Doris 查询已完成，但 Agent 未能解析已返回的业务结果，未展示数据",
                    "details": {},
                }
            elif reason == "clarification_contract_invalid":
                public = {
                    "code": "clarification_invalid",
                    "message": "查询已执行，但本次口径选项未形成有效选择",
                    "details": {},
                }
            details = public.get("details")
        if (
            public.get("code") == "query_invalid_result"
            and isinstance(details, Mapping)
            and isinstance(details.get("missing_fields"), list)
        ):
            public["details"] = {
                "missing_fields": [
                    field for field in details["missing_fields"]
                    if isinstance(field, str) and len(field) <= 80
                ][:12]
            }
        else:
            public["details"] = {}
        return int(exc.http_status), {"error": public}
    if isinstance(exc, TurnoverRuntimeError):
        status_by_code = {
            "report_parameters_invalid": 422,
            "report_contract_not_released": 409,
            "report_quality_blocked": 409,
            "report_query_failed": 502,
            "report_render_failed": 500,
            "report_configuration_error": 500,
        }
        return status_by_code.get(exc.code, 500), {
            "error": {
                "code": exc.code,
                "message": exc.public_message,
                "details": {},
            }
        }
    return 500, {
        "error": {
            "code": "service_unavailable",
            "message": "取数服务暂时不可用，请稍后重试",
            "details": {},
        }
    }


def _receipt_response(
    receipt: RequestReceipt,
    *,
    session: SessionContext,
    store: ChatSessionStore,
    secure_cookies: bool,
    response: Any,
) -> Any:
    """Replay a cached public response or report that its owner is still running."""

    from fastapi.responses import JSONResponse

    payload = receipt.public_payload()
    status_code = receipt.response_status
    if payload is not None and status_code is not None and receipt.state in {"completed", "failed"}:
        if status_code >= 400:
            replay = JSONResponse(status_code=status_code, content=payload)
            replay.headers["X-Request-ID"] = receipt.request_id
            _set_session_cookies(replay, session, secure=secure_cookies)
            return replay
        response.headers["X-Request-ID"] = receipt.request_id
        _set_session_cookies(response, session, secure=secure_cookies)
        return payload

    context_state = store.public_context_state(store.context(session))
    running = {
        "request_id": receipt.request_id,
        "conversation_id": receipt.conversation_id,
        "status": "running",
        "process": {
            "state": "running",
            "steps": [
                {"id": "context", "state": "reused", "label": "已处理用户上下文"},
                {"id": "query", "state": "running", "label": "正在执行 Doris"},
            ],
        },
        "outcome": {"state": "running", "label": "正在执行"},
        "clarification": {"required": False, "questions": []},
        "context_state": context_state,
    }
    replay = JSONResponse(status_code=202, content=running)
    replay.headers["X-Request-ID"] = receipt.request_id
    _set_session_cookies(replay, session, secure=secure_cookies)
    return replay


REPORT_PROFILE_CLARIFICATION: dict[str, Any] = {
    "required": True,
    "questions": [
        {
            "id": "turnover_inventory_profile",
            "prompt": "请选择库存口径。范围固定为8个经营中心及当前有效分仓，动销按问题中指定的完整自然日统计（未指定时默认7天），结果将生成汇总图片。",
            "selection": "single",
            "options": [
                {
                    "id": "available_plus_transit_finished_goods",
                    "label": "可用库存 + 在途库存｜产成品｜8个经营中心及当前有效分仓",
                },
                {
                    "id": "actual_finished_goods",
                    "label": "实时实际库存｜产成品｜8个经营中心及当前有效分仓",
                },
            ],
        }
    ],
}


def _report_clarification_questions(*, business_window_ambiguous: bool) -> list[dict[str, Any]]:
    """Build the complete report card without guessing the W37 window label."""

    questions = [dict(question) for question in REPORT_PROFILE_CLARIFICATION["questions"]]
    if business_window_ambiguous:
        questions.append(
            {
                "id": "turnover_window_semantics",
                "prompt": (
                    "“近14天”与业务周转表存在口径差异，请选择本次报表的时间含义。"
                    "业务表标题写近14天，但其销售源数据实际覆盖2026-09-05至2026-09-11七天。"
                ),
                "selection": "single",
                "options": [
                    {
                        "id": "live_fourteen_complete_days",
                        "label": "真实14个完整自然日｜按线上当前口径查询",
                    },
                    {
                        "id": "business_w37_source_window",
                        "label": "复现W37业务表｜实际使用7天源窗口（候选对账口径）",
                    },
                ],
            }
        )
    return questions


def _is_business_window_ambiguous(question: str) -> bool:
    compact = "".join(str(question or "").split()).casefold()
    # The new standard template has an approved center-7/detail-14 contract.
    # Historical W37 reproduction is still a separate candidate.
    return ("w37" in compact or "复现业务表" in compact) and ("14天" in compact or "十四天" in compact)


def _installation_id_path(value: str | Path | None = None) -> Path:
    raw = value if value is not None else os.getenv("AGENT_INSTALLATION_ID_PATH")
    if raw is None or not str(raw).strip():
        return DEFAULT_INSTALLATION_ID_PATH.resolve()
    candidate = Path(str(raw).strip()).expanduser()
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    return candidate.resolve()


def _read_installation_id(path: Path) -> str:
    try:
        payload = json.loads(path.read_text(encoding="ascii"))
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("installation identity file is unreadable") from exc
    if not isinstance(payload, Mapping) or payload.get("version") != 1:
        raise RuntimeError("installation identity file has an unsupported format")
    installation_id = payload.get("installation_id")
    if not isinstance(installation_id, str) or not INSTALLATION_ID_PATTERN.fullmatch(
        installation_id
    ):
        raise RuntimeError("installation identity file contains an invalid identifier")
    return installation_id


def _load_or_create_installation_id(path: Path) -> str:
    """Atomically create one random identity for this installed agent instance."""

    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(40):
        try:
            return _read_installation_id(path)
        except FileNotFoundError:
            installation_id = "inst_" + secrets.token_urlsafe(32)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            try:
                descriptor = os.open(path, flags, 0o600)
            except FileExistsError:
                time.sleep(0.025)
                continue
            try:
                with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as handle:
                    json.dump(
                        {"version": 1, "installation_id": installation_id},
                        handle,
                        ensure_ascii=True,
                        separators=(",", ":"),
                    )
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception:
                try:
                    path.unlink()
                except OSError:
                    pass
                raise
            return installation_id
        except RuntimeError:
            # Another process may have won O_EXCL but not finished its short
            # write yet. Retry briefly, then fail closed instead of rotating
            # the identity and silently forking the backend conversation.
            if attempt >= 39:
                raise
            time.sleep(0.025)
    raise RuntimeError("installation identity could not be initialized")


class IdentityResolver:
    """Resolve the server-owned installation identity or an optional signed ID.

    ``installation`` is the default for the department-local deployment. The
    browser supplies no identity: a random identifier is persisted on first use
    and remains stable across cookies, browsers, and service restarts.
    """

    def __init__(
        self,
        *,
        mode: str | None = None,
        secret: str | bytes | None = None,
        installation_id: str | None = None,
        installation_id_path: str | Path | None = None,
        timestamp_tolerance_seconds: int = WECom_TIMESTAMP_TOLERANCE_SECONDS,
    ) -> None:
        self.mode = (mode or os.getenv("AGENT_CHAT_IDENTITY_MODE", DEFAULT_IDENTITY_MODE)).strip().casefold()
        if self.mode in {"wecom", "wecom-hmac", "wecom_hmac"}:
            self.mode = "wecom_signed"
        if self.mode in {"install", "installed", "machine", "machine_id"}:
            self.mode = "installation"
        if self.mode not in {"installation", "anonymous", "wecom_signed"}:
            raise RuntimeError(
                "AGENT_CHAT_IDENTITY_MODE must be installation, anonymous, or wecom_signed"
            )
        raw_secret = secret if secret is not None else os.getenv("AGENT_CHAT_IDENTITY_SECRET")
        if isinstance(raw_secret, str):
            raw_secret = raw_secret.encode("utf-8")
        self.secret = raw_secret or b""
        if self.mode == "wecom_signed" and len(self.secret) < 32:
            raise RuntimeError("AGENT_CHAT_IDENTITY_SECRET must be at least 32 bytes")
        if installation_id is not None and not INSTALLATION_ID_PATTERN.fullmatch(
            installation_id
        ):
            raise RuntimeError("installation_id is invalid")
        self.installation_id_path = _installation_id_path(installation_id_path)
        self._installation_id = installation_id
        self._installation_lock = threading.Lock()
        self.timestamp_tolerance_seconds = int(timestamp_tolerance_seconds)

    def _installation_principal(self) -> Principal:
        if self._installation_id is None:
            with self._installation_lock:
                if self._installation_id is None:
                    self._installation_id = _load_or_create_installation_id(
                        self.installation_id_path
                    )
        return Principal(kind="installation", subject=self._installation_id)

    def resolve(self, headers: Mapping[str, str]) -> Principal | None:
        if self.mode == "anonymous":
            return None
        if self.mode == "installation":
            return self._installation_principal()
        user_id = (headers.get(WECom_USER_ID_HEADER) or "").strip()
        timestamp = (headers.get(WECom_TIMESTAMP_HEADER) or "").strip()
        signature = (headers.get(WECom_SIGNATURE_HEADER) or "").strip().casefold()
        if not WECom_USER_ID_PATTERN.fullmatch(user_id):
            raise SessionSecurityError("未取得有效的企业微信身份")
        try:
            issued_at = int(timestamp)
        except ValueError as exc:
            raise SessionSecurityError("企业微信身份断言无效") from exc
        if abs(time.time() - issued_at) > self.timestamp_tolerance_seconds:
            raise SessionSecurityError("企业微信身份断言已过期")
        message = f"{timestamp}.{user_id}".encode("utf-8")
        expected = hmac.new(self.secret, message, "sha256").hexdigest()
        if not signature or not hmac.compare_digest(expected, signature):
            raise SessionSecurityError("企业微信身份断言无效")
        return Principal(kind="wecom", subject=user_id)


_PRIVATE_CHAT_NETWORKS = (
    ip_network("10.0.0.0/8"),
    ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"),
    ip_network("fc00::/7"),
)


def _chat_network_mode() -> str:
    value = os.getenv("AGENT_CHAT_NETWORK_MODE", "loopback").strip().casefold()
    return value if value in {"loopback", "lan"} else "loopback"


def _request_is_loopback(request: Any) -> bool:
    client = getattr(request, "client", None)
    host = getattr(client, "host", None)
    if not host:
        return False
    if str(host).casefold() == "localhost":
        return True
    try:
        return ip_address(str(host)).is_loopback
    except ValueError:
        return False


def _request_is_allowed_network(request: Any) -> bool:
    if _request_is_loopback(request):
        return True
    if _chat_network_mode() != "lan":
        return False
    client = getattr(request, "client", None)
    host = getattr(client, "host", None)
    if not host:
        return False
    try:
        address = ip_address(str(host))
    except ValueError:
        return False
    if address.is_loopback or address.is_unspecified or address.is_multicast:
        return False
    configured = os.getenv("AGENT_CHAT_ALLOWED_CIDRS", "").strip()
    if not configured:
        # LAN mode is deny-by-default. An operator must explicitly provide
        # one or more client IPs/CIDRs before any non-loopback client is let in.
        return False
    try:
        networks = tuple(
            ip_network(item.strip(), strict=False)
            for item in configured.split(",")
            if item.strip()
        )
    except ValueError:
        return False
    return bool(networks) and any(address in network for network in networks)


def _same_origin(request: Any) -> bool:
    """Reject cross-origin state-changing browser requests when Origin is sent."""

    origin = request.headers.get("origin")
    if not origin:
        return True
    parsed = urlparse(origin)
    request_url = request.url
    if parsed.scheme != request_url.scheme or parsed.hostname != request_url.hostname:
        return False
    origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    request_port = request_url.port or (443 if request_url.scheme == "https" else 80)
    return origin_port == request_port


def _is_cross_site_fetch(request: Any) -> bool:
    """Use Fetch Metadata when a browser identifies a cross-site request."""

    return request.headers.get("sec-fetch-site", "").strip().casefold() == "cross-site"


def _is_browser_navigation(request: Any) -> bool:
    """Identify a top-level HTML navigation without redirecting API clients."""
    if request.url.path.startswith("/v1/"):
        return False
    if request.method.upper() not in {"GET", "HEAD"}:
        return False
    accept = request.headers.get("accept", "").casefold()
    return "text/html" in accept or "application/xhtml+xml" in accept


def _session_db_path() -> Path:
    raw = os.getenv("AGENT_CHAT_SESSION_DB_PATH")
    return Path(raw).expanduser().resolve() if raw and raw.strip() else DEFAULT_SESSION_DB_PATH


def _learning_db_path(session_store: ChatSessionStore) -> Path:
    """Keep learning evidence beside the principal registry by default.

    A separate override is useful for deployments that put the query history on
    a dedicated protected volume. Tests and single-machine installs otherwise
    inherit the session store's already-hardened directory.
    """

    raw = os.getenv("AGENT_LEARNING_DB_PATH")
    if raw and raw.strip():
        return Path(raw).expanduser().resolve()
    configured = getattr(session_store, "path", None)
    if configured is not None:
        return Path(configured).expanduser().resolve()
    return _session_db_path()


def _set_session_cookies(response: Any, session: SessionContext, *, secure: bool) -> None:
    common = {
        "max_age": max(1, int(session.expires_at - time.time())),
        "secure": secure,
        "samesite": "lax",
        "path": "/",
    }
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session.token,
        httponly=True,
        **common,
    )
    if session.csrf_cookie_needs_set:
        response.set_cookie(
            CSRF_COOKIE_NAME,
            session.csrf_token,
            httponly=False,
            **common,
        )


def _public_session_payload(
    session: SessionContext,
    conversation_id: str | None,
    *,
    identity_mode: str,
    context_state: Mapping[str, Any] | None = None,
    clarification_pending: bool = False,
) -> dict[str, Any]:
    payload = {
        "status": "ready",
        "conversation_id": conversation_id,
        # The CSRF token is intentionally readable by the SPA; the session token
        # itself remains HttpOnly and is never returned in JSON.
        "csrf_token": session.csrf_token,
        "session_ref": session.public_ref,
        "identity_mode": identity_mode,
        "expires_at": int(session.expires_at),
        # Only expose the existence of a pending choice here.  The selectable
        # copy and one-time token are returned by the CSRF-protected resume
        # endpoint below, never by a cross-site GET.
        "clarification_pending": bool(clarification_pending),
    }
    if context_state is not None:
        payload["context_state"] = dict(context_state)
    return payload


class ChatExecutionError(RuntimeErrorBase):
    code = "chat_execution_error"
    http_status = 502

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": {
                key: self.details[key]
                for key in ("reason", "exit_code")
                if key in self.details
            },
        }


class ChatTimeoutError(ChatExecutionError):
    code = "chat_timeout"
    http_status = 504


def _windowless_python(executable: str) -> str:
    """Prefer the no-console interpreter for the per-request worker."""

    if os.name != "nt":
        return executable
    candidate = Path(executable).with_name("pythonw.exe")
    return str(candidate) if candidate.is_file() else executable


def _hidden_process_options() -> dict[str, Any]:
    """Return Windows process flags that suppress both console and flash."""

    if os.name != "nt":
        return {"creationflags": 0, "start_new_session": True}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        "startupinfo": startup,
    }


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    """Stop the isolated model process and any app-server child it spawned."""

    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                **_hidden_process_options(),
            )
        else:
            import signal

            os.killpg(process.pid, signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        try:
            process.kill()
        except OSError:
            pass


def _query_execution_evidence(
    calls: Any,
    reported: Any = None,
) -> dict[str, Any]:
    """Normalize child-process tool evidence before it reaches the browser.

    Newer children classify successful calls as metadata, validation or business
    queries before removing SQL arguments.  Preserve that classification when
    its counts are internally consistent; older children still use the
    legacy call-summary compatibility fallback below.
    """

    values = calls if isinstance(calls, list) else []
    support_calls = {
        ("codex", "list_mcp_resources"),
        ("codex", "read_mcp_resource"),
    }
    query_calls = [
        call
        for call in values
        if isinstance(call, dict)
        and call.get("server") == "doris_query_agent"
        and call.get("tool") == "query_doris_sql"
    ]
    successful = [
        call
        for call in query_calls
        if str(call.get("status") or "").casefold() in {"completed", "succeeded", "success"}
    ]
    successful_count = len(successful)
    fallback = {
        "live_query_executed": bool(successful),
        "live_business_result_executed": bool(successful),
        "query_call_count": len(query_calls),
        "successful_query_call_count": successful_count,
        "metadata_query_call_count": 0,
        "successful_business_query_call_count": successful_count,
        "failed_query_call_count": len(query_calls) - successful_count,
        "unexpected_tool_call_count": sum(
            1 for call in values
            if isinstance(call, dict)
            and (call.get("server"), call.get("tool")) not in support_calls
            and call not in query_calls
        ),
        "status": "live_result" if successful else "no_live_result",
        "normalized_result_state": "succeeded" if successful else "not_executed",
    }
    if not isinstance(reported, dict):
        return fallback

    def nonnegative_int(key: str, default: int) -> int | None:
        value = reported.get(key, default)
        if type(value) is int:
            return value if value >= 0 else None
        if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) < 20:
            return int(value)
        return None

    query_count = nonnegative_int("query_call_count", len(query_calls))
    success_count = nonnegative_int("successful_query_call_count", successful_count)
    metadata_count = nonnegative_int("metadata_query_call_count", 0)
    validation_count = nonnegative_int("validation_query_call_count", 0)
    business_count = nonnegative_int(
        "successful_business_query_call_count",
        success_count - metadata_count - validation_count
        if None not in (success_count, metadata_count, validation_count) else 0,
    )
    failed_count = nonnegative_int(
        "failed_query_call_count",
        len(query_calls) - success_count if success_count is not None else 0,
    )
    unexpected_count = nonnegative_int(
        "unexpected_tool_call_count",
        sum(
            1 for call in values
            if isinstance(call, dict)
            and (call.get("server"), call.get("tool")) not in support_calls
            and call not in query_calls
        ),
    )
    status = str(reported.get("status") or "").casefold()
    expected_status = (
        "live_result"
        if business_count and business_count > 0
        else "metadata_only"
        if metadata_count and metadata_count > 0
        else "validation_only"
        if validation_count and validation_count > 0
        else "no_live_result"
    )
    if (
        None in (query_count, success_count, metadata_count, validation_count, business_count, failed_count, unexpected_count)
        or query_count != len(query_calls)
        or success_count != successful_count
        or metadata_count > success_count
        or business_count + metadata_count + validation_count != success_count
        or failed_count != len(query_calls) - successful_count
        or unexpected_count != sum(
            1 for call in values
            if isinstance(call, dict)
            and (call.get("server"), call.get("tool")) not in support_calls
            and call not in query_calls
        )
        or status != expected_status
    ):
        # A child that supplied the richer schema but contradicted its own
        # call summaries is not trustworthy. Keep only observed counts and
        # fail closed; do not reinterpret it as a live business result.
        return {
            "live_query_executed": bool(successful),
            "live_business_result_executed": False,
            "query_call_count": len(query_calls),
            "successful_query_call_count": successful_count,
            "metadata_query_call_count": 0,
            "successful_business_query_call_count": 0,
            "failed_query_call_count": len(query_calls) - successful_count,
            "unexpected_tool_call_count": sum(
                1 for call in values
                if isinstance(call, dict)
                and (call.get("server"), call.get("tool")) not in support_calls
                and call not in query_calls
            ),
            "status": "evidence_invalid",
            "normalized_result_state": "invalid_result",
        }
    normalized_state = str(reported.get("normalized_result_state") or "").casefold()
    if normalized_state not in NORMALIZED_RESULT_STATES:
        normalized_state = "succeeded" if business_count else "not_executed"
    return {
        **({"validation_query_call_count": validation_count} if validation_count else {}),
        "live_query_executed": bool(success_count),
        "live_business_result_executed": bool(business_count),
        "query_call_count": query_count,
        "successful_query_call_count": success_count,
        "metadata_query_call_count": metadata_count,
        "successful_business_query_call_count": business_count,
        "failed_query_call_count": failed_count,
        "unexpected_tool_call_count": unexpected_count,
        "status": expected_status,
        "normalized_result_state": normalized_state,
    }


def _allows_no_live_query(answer: str) -> bool:
    """Allow clarification/block messages without mistaking them for data results."""

    normalized = " ".join(answer.casefold().split())
    parsed = interaction_envelope(answer)
    if parsed.get("outcome") in {"clarification_required", "blocked", "failed"}:
        return True
    markers = (
        "请确认",
        "请明确",
        "请补充",
        "请选择",
        "需要补充",
        "无法确定",
        "尚未登记",
        "尚未支持",
        "未登记",
        "无法查询",
        "未验证",
        "未执行",
        "没有成功",
        "不能确定",
        "没有 live",
        "未完成",
        "blocked",
        "clarif",
    )
    return any(marker in normalized for marker in markers)


def _safe_interaction(result: Mapping[str, Any]) -> dict[str, Any]:
    """Return only bounded, browser-safe interaction data."""

    interaction = result.get("interaction")
    # Re-run all model-authored copy through the same fail-closed normalizer,
    # including structured interaction supplied by a newer child process.
    # This prevents a child from bypassing the boundary by returning a mapping
    # instead of embedding the envelope in final_response.
    if isinstance(interaction, Mapping):
        try:
            interaction_input: Any = json.dumps(
                interaction, ensure_ascii=False, default=str
            )
        except (TypeError, ValueError):
            interaction_input = ""
    else:
        interaction_input = result.get("final_response")
    normalized = interaction_envelope(interaction_input)
    if (isinstance(result, SemanticResult) and _result_has_live_business_evidence(result)
            and isinstance(interaction, Mapping)
            and normalized.get("needs_review") is True
            and interaction.get("outcome") == "completed"):
        answer = interaction.get("answer")
        # Host-generated row listings can exceed the prose budget. Preserve
        # only a valid envelope; a separately screened table must supply its summary.
        if (_safe_business_answer(answer) is None
                and _safe_business_answer(answer, enforce_size=False) is not None
                and not _answer_contains_unapproved_contract_marker(answer)):
            normalized = interaction_envelope(json.dumps(
                {key: value for key, value in interaction.items() if key != "answer"},
                ensure_ascii=False, default=str,
            ))
    return normalized


def _result_has_live_business_evidence(result: Mapping[str, Any]) -> bool:
    evidence = result.get("query_execution")
    if not isinstance(evidence, Mapping):
        return False
    return bool(
        evidence.get(
            "live_business_result_executed",
            evidence.get("live_query_executed", False),
        )
    )


def _should_offer_selectable_recovery(
    result: Mapping[str, Any],
    interaction: Mapping[str, Any],
    *,
    error_reason: str | None = None,
) -> bool:
    """Technical failure is never evidence of an unresolved business choice.

    Clarifications are emitted by the request planner or a valid model
    interaction. A failed, empty or malformed model turn stays a failure.
    """

    return False


def _selected_continuation_blocked_result(
    *,
    context_reused: bool,
    reason: str,
) -> dict[str, Any]:
    """Finish an answered choice without opening another clarification loop."""

    interaction = {
        "outcome": "blocked",
        "questions": [],
    }
    return {
        "status": "completed",
        "final_response": json.dumps(interaction, ensure_ascii=False),
        "thread_id": None,
        "context_reused": bool(context_reused),
        "interaction": interaction,
        "query_execution": {
            "live_query_executed": False,
            "live_business_result_executed": False,
            "query_call_count": 0,
            "successful_query_call_count": 0,
            "successful_business_query_call_count": 0,
            "failed_query_call_count": 0,
            "unexpected_tool_call_count": 0,
            "status": "no_live_result",
            "recovery": "selection_blocked",
        },
        "mcp_calls": [],
        "recovery_reason": reason,
    }


def _public_business_result(
    interaction: Mapping[str, Any],
    *,
    live: bool,
) -> dict[str, str] | None:
    """Expose only a screened plain-text answer backed by a live query."""

    if not live or str(interaction.get("outcome") or "").casefold() != "completed":
        return None
    answer = interaction.get("answer")
    if not isinstance(answer, str) or not answer or len(answer) > 8_000:
        return None
    # The numeric table is independently host-verified. If the model's prose
    # accidentally mentions implementation details, keep the live table but
    # replace only the prose with a neutral business-facing message.
    if PUBLIC_TABLE_UNSAFE_TEXT.search(answer):
        return {"format": "plain_text", "text": "查询已完成，结果已按已校验表格展示。"}
    return {"format": "plain_text", "text": answer}


def _public_table_text(value: Any, *, limit: int = PUBLIC_TABLE_TEXT_MAX_CHARS) -> str | None:
    """Return one short display label after removing control characters."""

    if not isinstance(value, str):
        return None
    normalized = " ".join(
        character for character in value.replace("\r", " ").replace("\n", " ").split()
    ).strip()
    if not normalized or len(normalized) > limit or PUBLIC_TABLE_UNSAFE_TEXT.search(normalized):
        return None
    return normalized


def _public_table_number(value: Any, *, allow_negative: bool = False) -> str | None:
    """Normalize a finite aggregate; signed values require a reviewed column."""

    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)) or value == "":
        return None
    text = str(value).strip()
    if len(text) > 128:
        return None
    try:
        parsed = Decimal(text)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or (parsed < 0 and not allow_negative):
        return None
    sign, digits, exponent = parsed.as_tuple()
    if parsed.is_zero():
        return "-0" if sign else "0"
    # Bound the fixed-point representation before allocating it. Constructing
    # from the tuple avoids Decimal.normalize() rounding under ambient context.
    while len(digits) > 1 and digits[-1] == 0:
        digits, exponent = digits[:-1], exponent + 1
    fractional = max(0, -exponent)
    if sign + max(1, len(digits) + exponent) + fractional + bool(fractional) > 80:
        return None
    rendered = format(Decimal((sign, digits, exponent)), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    rendered = rendered or "0"
    return rendered if len(rendered) <= 80 else None


def _public_table_unit(value: Any, *, allow_empty: bool = False) -> str | None:
    if value in (None, "") and allow_empty:
        return ""
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized if normalized in PUBLIC_TABLE_UNITS else None


def _public_reviewed_dynamic_table(raw_table: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project a host-computed table whose schema is already business-reviewed.

    Route handlers use source keys internally so they can reconcile their
    results.  Those keys are intentionally replaced with ordinal public keys
    here; only reviewed labels, units, and validated cells cross the API.
    """
    columns = raw_table.get("columns")
    rows = raw_table.get("rows")
    if (raw_table.get("reviewed_business_table") is not True
            or raw_table.get("complete_reconciled") is not True
            or not isinstance(columns, list)
            or not 1 <= len(columns) <= 32
            or not isinstance(rows, list)
            or len(rows) > PUBLIC_TABLE_MAX_ROWS):
        return None

    descriptors: list[dict[str, Any]] = []
    source_keys: set[str] = set()
    labels: set[str] = set()
    for ordinal, raw_column in enumerate(columns, 1):
        if not isinstance(raw_column, Mapping):
            return None
        source_key = raw_column.get("key")
        label = _public_table_text(raw_column.get("label"), limit=100)
        kind = raw_column.get("kind")
        if (not isinstance(source_key, str) or not source_key.strip()
                or len(source_key) > 160 or source_key in source_keys
                or label is None or label in labels
                or kind not in {"text", "number"}):
            return None
        source_keys.add(source_key)
        labels.add(label)
        descriptor = {
            "key": f"col_{ordinal}",
            "label": label,
            "kind": kind,
            "align": "right" if kind == "number" else "left",
        }
        if "nullable" in raw_column:
            if type(raw_column["nullable"]) is not bool:
                return None
            descriptor["nullable"] = raw_column["nullable"]
        if kind == "number":
            unit = _public_table_unit(raw_column.get("unit"))
            if unit is None:
                return None
            descriptor["unit"] = unit
            if raw_column.get("signed") is True:
                descriptor["signed"] = True
            elif "signed" in raw_column and raw_column.get("signed") is not False:
                return None
        elif "unit" in raw_column and raw_column.get("unit") not in (None, ""):
            return None
        descriptors.append({"source_key": source_key, **descriptor})

    safe_rows: list[dict[str, Any]] = []
    for raw_row in rows:
        if not isinstance(raw_row, Mapping):
            return None
        safe_row: dict[str, Any] = {}
        for descriptor in descriptors:
            source_key = descriptor["source_key"]
            if source_key not in raw_row:
                return None
            value = raw_row[source_key]
            if value is None:
                if descriptor.get("nullable") is not True:
                    return None
                safe_row[descriptor["key"]] = None
                continue
            if descriptor["kind"] == "number":
                cell = _public_table_number(value, allow_negative=descriptor.get("signed") is True)
            else:
                cell = _public_table_text(value)
            if cell is None:
                return None
            safe_row[descriptor["key"]] = cell
        safe_rows.append(safe_row)

    title = _public_table_text(raw_table.get("title"), limit=100)
    caption_value = raw_table.get("caption")
    caption = "" if caption_value in (None, "") else _public_table_text(caption_value, limit=180)
    if title is None or caption is None:
        return None
    notes = raw_table.get("notes", [])
    if not isinstance(notes, list) or len(notes) > 3:
        return None
    safe_notes = [_public_table_text(note, limit=240) for note in notes]
    if any(note is None for note in safe_notes):
        return None
    public_table = {
        "format": "table",
        "title": title,
        "caption": caption,
        "columns": [{key: value for key, value in descriptor.items() if key != "source_key"}
                    for descriptor in descriptors],
        "rows": safe_rows,
        "summary": [],
        "empty_label": "当前范围没有可展示的数据",
    }
    if safe_notes:
        public_table["notes"] = safe_notes
    try:
        encoded_size = len(json.dumps(public_table, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return None
    return public_table if encoded_size <= PUBLIC_TABLE_MAX_BYTES else None


def _public_business_table(raw_table: Any, *, live: bool, requested_labels: list[str] | None = None) -> dict[str, Any] | None:
    """Screen a route-owned aggregate table before it reaches the browser.

    The input may contain SQL-result-shaped keys, but only the reviewed table
    schema and transformed aggregate cells are copied to the public payload.
    """

    if not live or not isinstance(raw_table, Mapping):
        return None
    if raw_table.get("format") != "table":
        return None
    table_id = raw_table.get("table_id")
    if table_id == "model_verified":
        if raw_table.get("reviewed_business_table") is True:
            return _public_reviewed_dynamic_table(raw_table)
        return _public_model_verified_table(raw_table, requested_labels=requested_labels or raw_table.get("requested_labels"))
    schema = PUBLIC_TABLE_SCHEMAS.get(table_id) if isinstance(table_id, str) else None
    if schema is None:
        return None
    if table_id == "so07_order_details":
        if raw_table.get("complete_reconciled") is not True:
            return None
        summary = raw_table.get("summary")
        if (not isinstance(summary, list) or len(summary) != 1
                or not isinstance(summary[0], Mapping) or summary[0].get("label") != "订单数"
                or summary[0].get("unit") != "单"
                or not isinstance(summary[0].get("value"), str)
                or not re.fullmatch(r"[0-9]{1,10}", summary[0]["value"])):
            return None
    raw_rows = raw_table.get("rows")
    if not isinstance(raw_rows, list) or len(raw_rows) > PUBLIC_TABLE_MAX_ROWS:
        return None

    safe_rows: list[dict[str, str]] = []
    columns = tuple(schema["columns"])
    if schema.get("selectable_columns"):
        requested = raw_table.get("columns")
        if not isinstance(requested, list) or not 1 <= len(requested) <= len(columns):
            return None
        keys = [column.get("key") if isinstance(column, Mapping) else None for column in requested]
        if any(not isinstance(key, str) for key in keys) or len(set(keys)) != len(keys):
            return None
        by_key = {column["key"]: column for column in columns}
        if not set(keys).issubset(by_key) or not set(schema["required_columns"]).issubset(keys):
            return None
        columns = tuple(by_key[key] for key in keys)
    for raw_row in raw_rows:
        if not isinstance(raw_row, Mapping):
            return None
        safe_row: dict[str, str] = {}
        for column in columns:
            key = column["key"]
            if column.get("nullable") and key in raw_row and raw_row[key] is None:
                safe_row[key] = None
                continue
            if column["kind"] == "number":
                if table_id == "so07_order_details" and not isinstance(raw_row.get(key), str):
                    return None
                cell = _public_table_number(raw_row.get(key), allow_negative=column.get("signed") is True)
            elif key == "unit":
                cell = _public_table_unit(raw_row.get(key))
            else:
                cell = _public_table_text(raw_row.get(key))
            if cell is None:
                return None
            safe_row[key] = cell
        safe_rows.append(safe_row)

    raw_summary = raw_table.get("summary", [])
    if not isinstance(raw_summary, list) or len(raw_summary) > 6:
        return None
    safe_summary: list[dict[str, str]] = []
    for item in raw_summary:
        if not isinstance(item, Mapping):
            return None
        label = item.get("label")
        if label not in PUBLIC_TABLE_SUMMARY_LABELS:
            return None
        value = _public_table_number(item.get("value"))
        if value is None:
            value = _public_table_text(item.get("value"), limit=80)
        if value is None:
            return None
        unit = _public_table_unit(item.get("unit"), allow_empty=True)
        if unit is None:
            return None
        safe_summary.append({"label": label, "value": value, "unit": unit})

    caption = _public_table_text(raw_table.get("caption", ""), limit=180)
    if caption is None:
        caption = ""
    public_table = {
        "format": "table",
        "title": schema["title"],
        "caption": caption,
        "columns": [dict(column) for column in columns],
        "rows": safe_rows,
        "summary": safe_summary,
        "empty_label": schema["empty_label"],
    }
    if table_id == "sales_timeseries":
        raw_window_label = next(
            (column.get("label") for column in raw_table.get("columns", [])
             if isinstance(column, Mapping) and column.get("key") == "moving_average"),
            None,
        )
        match = re.fullmatch(r"([0-9]{1,3})日移动平均", raw_window_label or "")
        if match is None or not 2 <= int(match[1]) <= 365:
            return None
        public_table["columns"][-1]["label"] = raw_window_label
    if table_id == "sales_outbound_boxes" and raw_table.get("scope_finished_goods") is False:
        public_table["title"] = "实际销售出库与箱数"
    if table_id == "sales_analysis" and raw_table.get("quantity_filter_applied") is True:
        public_table["empty_label"] = "查询成功，汇总后没有符合数量条件的分组"
    if table_id in {"sales_timeseries", "sales_price_group", "sales_outbound_details", "so07_order_details", "sales_aggregate", "sales_analysis", "purchase_received_detail", "sales_outbound_boxes", "sales_sku_name_quality", "sales_outbound_link", "dms_points_orders_monthly", "dms_points_orders_detail", "dms_manager_orders", "sales_ranked_drilldown", "sales_ranked_sku_detail", "sales_order_return_orders", "sales_order_return_quantities"} and "notes" in raw_table:
        notes = raw_table["notes"]
        if not isinstance(notes, list) or len(notes) > 3:
            return None
        safe_notes = [_public_table_text(note, limit=240) for note in notes]
        if any(note is None for note in safe_notes):
            return None
        public_table["notes"] = safe_notes
    try:
        encoded_size = len(json.dumps(public_table, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return None
    return public_table if encoded_size <= PUBLIC_TABLE_MAX_BYTES else None


def _model_verified_numeric_columns(columns: list[str], rows: list[Any]) -> list[bool]:
    """Do not coerce numeric-looking identifiers or serialized decimals to numbers."""
    numeric_columns = []
    for column in columns:
        if not isinstance(column, str):
            return []
        values = [row[column] for row in rows if isinstance(row, Mapping) and column in row and row[column] is not None]
        numeric_columns.append(bool(values) and all(
            isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)
            and _public_table_number(value, allow_negative=True) is not None
            for value in values
        ))
    return numeric_columns


_MODEL_COLUMN_LABELS = {
    "rank": "排名",
    "ranking": "排名",
    "cargo_owner_id": "货主",
    "cargo_owner": "货主",
    "warehouse_code": "仓库编码",
    "warehouse_name": "仓库",
    "warehouse": "仓库",
    "wms_code": "仓库编码",
    "wms_desc": "仓库",
    "warehouse_desc": "仓库",
    "sku_code": "商品编码",
    "sku_name": "商品名称",
    "unit": "单位",
    "inventory_quantity": "库存数量",
    "quantity": "数量",
    "ship_date": "出库日期",
    "production_date": "生产日期",
    "outbound_count": "出库单数",
    "sku_count": "SKU数",
    "source_record_count": "来源记录数",
    "source_max_stock_change_time": "库存最后变更时间",
    "source_max_updated_date": "数据最后更新时间",
    "customer_code": "客户编码",
    "outbound_code": "出库单号",
    "order_code": "订单号",
    "order_date": "订单日期",
    "row_type": "统计项",
    "fulfillment_type": "履约类型",
    "outbound_order_count": "出库单数",
    "actual_ship_qty": "实发数量",
    "actual_ship_qty_pct_of_day_total": "数量占比",
    "actual_ship_qty_pct": "数量占比",
    "ship_qty_pct": "数量占比",
    "actual_ship_qty_percentage": "数量占比",
    "scoped_outbound_order_count": "范围出库单数",
    "scoped_actual_ship_qty": "范围实发数量",
    "source_max_ship_at": "最近出库时间",
    "validation_status": "校验状态",
    "warehouse_type": "仓库类型",
    "warehouse_count": "仓库数",
    "daily_average": "日均数量",
    "change_rate": "变化率",
    "outbound_ea": "实发数量",
    "ship_qty": "实发数量",
    "sales_quantity": "销售出库",
    "actual_sales_qty": "实际销售出库",
    "share": "占比",
    "ratio": "占比",
    "percentage": "占比",
    "proportion": "占比",
    "quantity_share": "数量占比",
    "warehouse_num": "仓库数",
    "warehouse_count": "仓库数",
    "type": "类型",
    "count": "数量",
}

# These fields are useful to the query kernel for freshness and
# reconciliation, but they are execution evidence rather than business
# output.  They must never become generic model-result columns in the browser.
_MODEL_HIDDEN_COLUMNS = {
    "source_max_ship_at",
    "source_max_stock_change_time",
    "source_max_updated_date",
    "validation_status",
    "fulfillment_conflict_document_count",
    "scoped_outbound_order_count",
    "scoped_actual_ship_qty",
}


def _model_verified_column_label(source_key: str, ordinal: int) -> str:
    """Turn model/source keys into reviewed business labels before UI output."""

    normalized = source_key.strip().casefold()
    if normalized in _MODEL_COLUMN_LABELS:
        return _MODEL_COLUMN_LABELS[normalized]
    words = set(re.findall(r"[a-z0-9]+", re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", source_key).casefold()))
    compact = re.sub(r"[^a-z0-9]", "", normalized)
    if words.intersection({"share", "ratio", "pct", "percent", "percentage", "proportion"}):
        if words.intersection({"amount", "revenue", "money"}):
            return "金额占比"
        return "数量占比" if words.intersection({"quantity", "qty", "number"}) else "占比"
    if "warehouse" in words and words.intersection({"count", "number", "num"}) and not any(word.isdigit() for word in words):
        return "仓库总数" if words.intersection({"total", "all"}) else "仓库数"
    if compact in {"typecount", "countbytype"}:
        return "类型"
    label = _business_column_label(source_key)
    if label is not None:
        return label
    # Unrecognized implementation aliases keep a neutral host-owned label.
    return f"结果列 {ordinal}"


def _public_model_verified_table(raw_table: Mapping[str, Any], *, requested_labels: list[str] | None = None) -> dict[str, Any] | None:
    """Project an arbitrary model query using only host-verified Doris data.

    This is deliberately separate from governed route schemas: it enables the
    general agent to show a useful table without allowing model-authored values,
    SQL, or internal execution metadata into the browser.
    """
    columns = raw_table.get("columns")
    rows = raw_table.get("rows")
    if not isinstance(columns, list) or not 1 <= len(columns) <= 32 or not isinstance(rows, list) or len(rows) > PUBLIC_TABLE_MAX_ROWS:
        return None
    source_keys: list[str] = []
    source_indices: list[int] = []
    seen_keys: set[str] = set()
    for index, value in enumerate(columns):
        key = value if isinstance(value, str) else ""
        if not key.strip() or len(key) > 160 or key in seen_keys:
            return None
        seen_keys.add(key)
        if key.strip().casefold() in _MODEL_HIDDEN_COLUMNS:
            continue
        source_keys.append(key)
        source_indices.append(index)
    if not source_keys:
        return None
    requested = []
    if requested_labels is not None:
        if not isinstance(requested_labels, list) or any(not isinstance(label, str) for label in requested_labels):
            return None
        requested = [label for label in (_business_column_label(value) for value in requested_labels) if label is not None]
        if len(requested) != len(requested_labels):
            return None
    keys = [f"col_{index}" for index in range(1, len(source_keys) + 1)]
    configured_kind = raw_table.get("numeric_columns")
    if configured_kind is None:
        configured_kind = _model_verified_numeric_columns(columns, rows)
    if (not isinstance(configured_kind, list) or len(configured_kind) != len(columns)
            or any(type(value) is not bool for value in configured_kind)):
        return None
    configured_kind = [configured_kind[index] for index in source_indices]
    declared_labels = {}
    declared_descriptors = {}
    if "result_columns" in raw_table:
        try:
            declared = _declared_result_columns(raw_table["result_columns"])
            validate_business_projection(raw_table, declared)
        except (ValueError, DorisResultError):
            return None
        declared_descriptors = {column["key"]: column for column in declared}
        declared_labels = {column["key"]: column["label"] for column in declared}
        if set(declared_labels) != set(columns):
            return None
    inferred_labels = [declared_labels.get(source_key) or _model_verified_column_label(source_key, index + 1)
                       for index, source_key in enumerate(source_keys)]
    # A question is a projection hint, never proof of what the nth result
    # contains. Select only unambiguous semantic matches; otherwise retain
    # neutral labels instead of relabeling a control total as a business value.
    equivalent = {"数量占比": "占比", "仓库数量": "仓库数", "仓库名称": "仓库"}
    matches = [[index for index, label in enumerate(inferred_labels)
                if equivalent.get(label, label) == equivalent.get(wanted, wanted)] for wanted in requested]
    # A declared result_columns array is the model's explicit grain contract.
    # Do not hide one of those dimensions merely because the natural-language
    # output hint is shorter: doing so can collapse distinct owner/warehouse/
    # SKU rows into visually duplicate rows and conceal a join or aggregation
    # error. Projection hints remain available for undeclared generic plans.
    selected_indices = (
        None if declared_labels else
        ({indices[0] for indices in matches} if matches and all(len(indices) == 1 for indices in matches) else None)
    )
    descriptors = []
    used_labels: set[str] = set()
    for index, (source_key, key) in enumerate(zip(source_keys, keys, strict=True)):
        if selected_indices is not None and index not in selected_indices:
            continue
        descriptor = declared_descriptors.get(source_key, {})
        numeric = descriptor.get("kind") == "number" if "kind" in descriptor else configured_kind[index]
        label = inferred_labels[index]
        # A warehouse code is a valid source identity for a request asking for
        # warehouses, but the public table should use the business concept
        # unless the user explicitly requested the code field. This keeps
        # model-selected physical identifiers from leaking into otherwise
        # generic warehouse summaries while preserving explicit projections.
        if not requested_labels and not declared_labels and label == "仓库编码":
            label = "仓库"
        base_label, suffix = label, 2
        while label in used_labels:
            label = f"{base_label}（{suffix}）"
            suffix += 1
        used_labels.add(label)
        descriptors.append({"key": key, "label": label, "kind": "number" if numeric else "text", "align": "right" if numeric else "left", "source_key": source_key, **({"signed": True} if numeric else {})})
        if "unit" in descriptor:
            descriptors[-1]["unit"] = descriptor["unit"]
    for index, descriptor in enumerate(descriptors, 1):
        descriptor["key"] = f"col_{index}"
    safe_rows: list[dict[str, Any]] = []
    for raw_row in rows:
        if not isinstance(raw_row, Mapping):
            return None
        safe_row: dict[str, Any] = {}
        for descriptor in descriptors:
            key = descriptor["key"]
            if descriptor["source_key"] not in raw_row:
                return None
            value = raw_row[descriptor["source_key"]]
            if value is None:
                safe_row[key] = None
            elif descriptor["kind"] == "number":
                cell = _public_table_number(value, allow_negative=True)
                if cell is None:
                    return None
                safe_row[key] = cell
            else:
                # Mixed native numeric/text columns retain their values without
                # applying numeric formatting to identifiers from other rows.
                if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
                    if _public_table_number(value, allow_negative=True) is None:
                        return None
                    value = str(value)
                cell = _public_table_text(value)
                if cell is None:
                    return None
                safe_row[key] = cell
        safe_rows.append(safe_row)
    title = _public_table_text(raw_table.get("title") or "查询结果", limit=100)
    caption_value = raw_table.get("caption")
    caption = "" if caption_value in (None, "") else _public_table_text(caption_value, limit=180)
    if title is None or caption is None:
        return None
    public_descriptors = [{key: value for key, value in descriptor.items() if key != "source_key"} for descriptor in descriptors]
    # Keep one stable, non-sensitive identity for the generic model-first
    # projection. It lets the API, acceptance checks, and result-page layer
    # distinguish a host-verified model table from legacy route tables without
    # exposing SQL, source names, or execution details.
    result = {"format": "table", "table_id": "model_verified", "title": title, "caption": caption,
              "columns": public_descriptors, "rows": safe_rows, "summary": [], "empty_label": "当前范围没有可展示的数据"}
    try:
        return result if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= PUBLIC_TABLE_MAX_BYTES else None
    except (TypeError, ValueError):
        return None


def _public_model_result_tables(raw_tables, *, pages_store, owner):
    """Screen every retained projection before registering any public pages."""
    if not isinstance(raw_tables, list) or not 1 <= len(raw_tables) <= MAX_ANSWER_TABLES:
        raise ChatExecutionError("查询结果表不完整，未展示部分结果")
    screened, identities, titles = [], set(), set()
    total_rows = total_bytes = 0
    for raw_value in raw_tables:
        raw = {}
        try:
            if not isinstance(raw_value, Mapping):
                raise ValueError("invalid result")
            raw = dict(raw_value)
            # Some gateway builds call the public table identity `table_id`
            # while the model bundle contract calls it `result_id`. Normalize
            # that transport alias before strict screening; values and rows
            # remain host-owned and are not changed.
            if "result_id" not in raw and isinstance(raw.get("table_id"), str):
                raw["result_id"] = raw["table_id"]
            if "result_title" not in raw and "title" in raw:
                raw["result_title"] = raw["title"]
            identity = result_identity(raw)
            if identity["result_id"] in identities or identity["result_title"] in titles:
                raise ValueError("duplicate result")
            validate_business_projection(raw, raw.get("result_columns"))
            total_rows += len(raw["rows"])
            total_bytes += len(json.dumps(raw, ensure_ascii=False, default=str).encode("utf-8"))
            if total_rows > MAX_ANSWER_ROWS or total_bytes > MAX_ANSWER_BYTES:
                raise ValueError("result capacity exceeded")
        except (ValueError, TypeError, DorisResultError) as exc:
            # Keep the public error generic, but record the safe structural
            # reason needed to repair a model/result-contract mismatch. Never
            # log SQL or business row values here.
            LOGGER.warning(
                "Model result table rejected during structural screening: error_type=%s result_id_type=%s columns_type=%s columns_count=%s rows_type=%s rows_count=%s has_result_columns=%s",
                type(exc).__name__, type(raw.get("result_id")).__name__,
                type(raw.get("columns")).__name__,
                len(raw.get("columns")) if isinstance(raw.get("columns"), list) else None,
                type(raw.get("rows")).__name__,
                len(raw.get("rows")) if isinstance(raw.get("rows"), list) else None,
                isinstance(raw.get("result_columns"), list),
            )
            raise ChatExecutionError("查询结果表未通过校验，未展示部分结果") from None
        identities.add(identity["result_id"])
        titles.add(identity["result_title"])
        numeric_columns = _model_verified_numeric_columns(raw["columns"], raw["rows"])
        pages = []
        for offset in range(0, max(1, len(raw["rows"])), 100):
            page = _public_model_verified_table({**raw, "title": identity["result_title"],
                "rows": raw["rows"][offset:offset + 100], "numeric_columns": numeric_columns})
            if page is None:
                LOGGER.warning(
                    "Model result table rejected during public projection: result_id_type=%s columns_count=%s page_rows=%s",
                    type(raw.get("result_id")).__name__, len(raw.get("columns", [])),
                    len(raw["rows"][offset:offset + 100]),
                )
                raise ChatExecutionError("查询结果表未通过展示校验，未展示部分结果")
            pages.append(page)
        screened.append(pages)
    paginated = [pages for pages in screened if len(pages) > 1]
    registered = iter(pages_store.create_many(owner, paginated) if paginated else [])
    return [next(registered) if len(pages) > 1 else pages[0] for pages in screened]


def _project_model_results(result, *, pages_store, owner):
    """Project the new model result protocol without consulting business routes."""
    if "verified_business_results" in result:
        raw_tables = result["verified_business_results"]
        tables = _public_model_result_tables(raw_tables, pages_store=pages_store, owner=owner)
        # `_public_model_result_tables` accepts the gateway's historical
        # `table_id` alias and normalizes it on its private copy. Build the
        # public manifest from the same normalized identity instead of reading
        # the unnormalized model payload a second time.
        manifest = []
        for raw_value in raw_tables:
            if not isinstance(raw_value, Mapping):
                raise ChatExecutionError("查询结果表未通过校验，未展示部分结果")
            result_id = raw_value.get("result_id")
            if not isinstance(result_id, str):
                result_id = raw_value.get("table_id")
            rows = raw_value.get("rows")
            if not isinstance(result_id, str) or not isinstance(rows, list):
                raise ChatExecutionError("查询结果表未通过校验，未展示部分结果")
            manifest.append({"result_id": result_id, "row_count": len(rows)})
    else:
        raw = result.get("verified_business_result")
        if isinstance(raw, Mapping) and isinstance(raw.get("rows"), list):
            if len(raw["rows"]) > MAX_ANSWER_ROWS:
                raise DorisResultError("execution_invalid")
            numeric_columns = _model_verified_numeric_columns(raw.get("columns", []), raw["rows"])
            pages = []
            for offset in range(0, max(1, len(raw["rows"])), PUBLIC_TABLE_MAX_ROWS):
                page = _public_model_verified_table({**raw, "numeric_columns": numeric_columns,
                    "rows": raw["rows"][offset:offset + PUBLIC_TABLE_MAX_ROWS]})
                if page is None:
                    raise DorisResultError("execution_invalid")
                pages.append(page)
            tables = [pages_store.create(owner, pages) if len(pages) > 1 else pages[0]]
            manifest = [{"result_id": "result", "row_count": len(raw["rows"])}]
        else:
            raise DorisResultError("execution_missing")
    try:
        references = normalize_answer_references(result.get("answer_references"), manifest)
    except ValueError:
        references = normalize_answer_references(None, manifest)
    summary = public_result_summary(tables, manifest, references,
        page_reader=lambda identifier, page: pages_store.page(owner, identifier, page))
    return tables, summary


def _public_dms_order_tables(raw_tables, *, reviewed, live, pages_store, owner):
    return _public_reconciled_tables(raw_tables, expected=('dms_points_orders_monthly', 'dms_points_orders_detail'),
        label='积分订单', reviewed=reviewed, live=live, pages_store=pages_store, owner=owner)


def _public_order_return_tables(raw_tables, *, reviewed, live, pages_store, owner):
    return _public_reconciled_tables(raw_tables, expected=('sales_order_return_orders', 'sales_order_return_quantities'),
        label='订单退货', reviewed=reviewed, live=live, pages_store=pages_store, owner=owner)


def _public_paged_business_table(raw_table: Any, *, pages_store, owner, live: bool,
                                 max_rows: int = 100000) -> dict[str, Any]:
    """Screen a complete route-owned table and register extra pages atomically."""

    if not isinstance(raw_table, Mapping):
        raise ChatExecutionError("查询结果表不完整，未展示部分结果")
    rows = raw_table.get("rows")
    if not isinstance(rows, list) or len(rows) > max_rows:
        raise ChatExecutionError("查询结果超过完整展示上限，未展示部分结果")
    first = _public_business_table(raw_table, live=live)
    if first is not None:
        return first
    pages = []
    for offset in range(0, max(1, len(rows)), 100):
        page = _public_business_table(
            {**raw_table, "rows": rows[offset:offset + 100]},
            live=live,
        )
        if page is None:
            raise ChatExecutionError("查询结果未通过展示校验，未展示部分结果")
        pages.append(page)
    return pages_store.create(owner, pages) if len(pages) > 1 else pages[0]


def _public_reconciled_tables(raw_tables, *, expected, label, reviewed, live, pages_store, owner):
    """Validate both complete tables before making either one available."""
    if not reviewed or not live or not isinstance(raw_tables, list) or len(raw_tables) != 2:
        raise ChatExecutionError(f"{label}双表缺少完整对账依据，暂未展示")
    screened = []
    for raw, table_id in zip(raw_tables, expected):
        if not isinstance(raw, dict) or raw.get('table_id') != table_id or raw.get('complete_reconciled') is not True:
            raise ChatExecutionError(f"{label}双表不完整，暂未展示")
        rows = raw.get('rows')
        if not isinstance(rows, list) or len(rows) > 100000:
            raise ChatExecutionError(f"{label}结果超出完整展示范围")
        pages = []
        for offset in range(0, max(1, len(rows)), 100):
            page = _public_business_table({**raw, 'rows': rows[offset:offset+100]}, live=True)
            if page is None:
                raise ChatExecutionError(f"{label}结果未通过展示校验，未展示部分结果")
            pages.append(page)
        screened.append(pages)
    return [pages_store.create(owner, pages) if len(pages)>1 else pages[0] for pages in screened]


def _public_compaction_state(value: Any) -> str:
    """Map internal/model compaction state to the fixed browser contract."""

    normalized = str(value or "not_due").strip().casefold()
    return normalized if normalized in PUBLIC_COMPACTION_STATES else "not_due"


def _public_process(
    result: Mapping[str, Any],
    *,
    context_state: Mapping[str, Any],
    clarification_required: bool,
    interaction_blocked: bool = False,
    interaction_failed: bool = False,
    report_completed: bool = False,
) -> dict[str, Any]:
    evidence = result.get("query_execution")
    if not isinstance(evidence, Mapping):
        evidence = {}
    live = bool(
        evidence.get(
            "live_business_result_executed",
            evidence.get("live_query_executed", False),
        )
    )
    evidence_status = str(evidence.get("status") or "no_live_result").casefold()
    normalized_result_state = str(
        evidence.get("normalized_result_state")
        or result.get("normalized_result_state")
        or ""
    ).casefold()
    if normalized_result_state not in NORMALIZED_RESULT_STATES:
        normalized_result_state = ""
    if interaction_failed:
        query_state, query_label = "failed", "执行未完成"
    elif interaction_blocked:
        query_state, query_label = "blocked", "执行已停止"
    elif clarification_required:
        query_state, query_label = "waiting_for_selection", "等待口径选择"
    elif normalized_result_state == "succeeded":
        query_state, query_label = "completed", "Doris 查询和结果校验已完成"
    elif normalized_result_state == "empty_result":
        query_state, query_label = "completed", "查询成功，当前条件下没有数据"
    elif normalized_result_state in {
        "query_rejected", "query_failed", "query_unavailable",
        "truncated_result", "invalid_result",
    }:
        query_state, query_label = "failed", {
            "query_rejected": "查询未执行，后端安全校验已拒绝",
            "query_failed": "查询执行失败",
            "query_unavailable": "查询服务暂时不可用",
            "truncated_result": "查询结果不完整，未作为完整结果展示",
            "invalid_result": "查询已返回，但结果解析未通过",
        }[normalized_result_state]
    elif live or report_completed:
        query_state, query_label = "completed", "已完成数据查询"
    elif evidence_status == "metadata_only":
        query_state, query_label = "metadata_checked", "已完成数据源检查"
    else:
        query_state, query_label = "not_completed", "本轮未完成数据查询"
    reused = bool(result.get("context_reused"))
    reset = bool(result.get("context_reset"))
    context_state_value = "reset" if reset else "reused" if reused else "started"
    compaction = result.get("context_compaction")
    compaction_status = _public_compaction_state(
        compaction.get("status") if isinstance(compaction, Mapping) else None
    )
    compact_label = {
        "completed": "对话上下文已压缩",
        "requested": "已请求上下文压缩",
        "unsupported": "暂未触发上下文压缩",
        "failed": "上下文压缩未完成",
    }.get(compaction_status, "上下文保持中")
    return {
        "state": (
            "blocked"
            if interaction_blocked
            else "failed"
            if interaction_failed
            else "waiting_for_selection"
            if clarification_required
            else "completed"
            if normalized_result_state in {"succeeded", "empty_result"}
            or (not normalized_result_state and (live or report_completed))
            else "blocked"
        ),
        "steps": [
            {"id": "context", "state": context_state_value, "label": "已继承本用户上下文" if reused else "已建立本用户上下文" if not reset else "已重建本用户上下文"},
            {"id": "query", "state": query_state, "label": query_label},
            {"id": "compaction", "state": compaction_status, "label": compact_label},
        ],
    }


def _public_outcome(interaction: Mapping[str, Any], *, live: bool) -> dict[str, str]:
    outcome = str(interaction.get("outcome") or "completed").casefold()
    if outcome == "clarification_required":
        return {"state": "clarification_required", "label": "需要选择业务口径"}
    if outcome == "blocked":
        return {"state": "blocked", "label": "当前口径未完成确认"}
    if outcome == "failed":
        return {"state": "failed", "label": "本次取数未完成"}
    return {"state": "completed" if live else "blocked", "label": "取数已完成" if live else "等待取数完成"}


def _invoke_runner(
    runner: Any,
    question: str,
    kwargs: Mapping[str, Any],
) -> dict[str, Any]:
    """Keep test/custom runners compatible while production receives context args."""

    try:
        parameters = inspect.signature(runner.run).parameters
    except (TypeError, ValueError):
        parameters = {}
    if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        return runner.run(question, **dict(kwargs))
    accepted = {key: value for key, value in kwargs.items() if key in parameters}
    return runner.run(question, **accepted)


async def _keep_request_alive(
    store: Any,
    session: SessionContext,
    conversation_id: str,
    receipt: RequestReceipt | None,
    conversation_lease_id: str,
    context_lease_id: str,
) -> None:
    """Renew transient claims while a long model/Doris turn is in progress."""

    lease_ttl = getattr(store, "lease_ttl_seconds", 10 * 60)
    try:
        interval = max(5.0, min(60.0, float(lease_ttl) / 3.0))
    except (TypeError, ValueError):
        interval = 60.0
    renew_request = getattr(store, "renew_request", None)
    renew_conversation = getattr(store, "renew", None)
    renew_context = getattr(store, "renew_context", None)
    while True:
        await asyncio.sleep(interval)
        try:
            if callable(renew_request) and receipt is not None:
                await asyncio.to_thread(renew_request, session, receipt)
            if callable(renew_conversation):
                await asyncio.to_thread(
                    renew_conversation,
                    session,
                    conversation_id,
                    conversation_lease_id,
                )
            if callable(renew_context):
                await asyncio.to_thread(renew_context, session, context_lease_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A single missed renewal should not cancel an otherwise valid
            # read-only request. The next interval retries it, while the
            # request's normal completion path still releases every claim.
            LOGGER.warning("Unable to renew transient chat claims", exc_info=True)


def _requested_model_business_labels(question: Any) -> list[str]:
    """Narrow output only for a complete, explicit list of known columns."""
    if not isinstance(question, str):
        return []
    if re.search(r"(?:不|别|无需)[^。；;\n!?！？]*?(?:返回|输出|展示|列出)", question):
        return []
    projections = re.findall(
        r"(?:返回|输出|(?:展示|列出)(?:以下)?(?:字段|列))"
        r"\s*(?:以下(?:字段|列))?\s*[:：]?\s*([^。；;\n!?！？]+)",
        question,
    )
    if len(projections) != 1:
        return []
    # A partial match can discard an unrecognized measure or confuse a filter
    # with an output column. Keep every verified column in either case.
    items = re.split(r"\s*(?:[,，、]|以及|并且|和|与|及|并)\s*", projections[0].strip())
    labels = []
    rules = (
        (r"仓库类型|仓库类别", "仓库类型"), (r"仓库数量|仓库数", "仓库数"),
        (r"数量占比|占比|百分比|比例", "占比"), (r"仓库名称", "仓库名称"),
        (r"仓库编码", "仓库编码"), (r"货主", "货主"), (r"SKU(?:编码)?|商品编码", "商品编码"),
        (r"实发数量|实际销售出库量|出库数量", "实发数量"), (r"出库单数|单数", "出库单数"),
    )
    for item in items:
        label = next((label for pattern, label in rules if re.fullmatch(pattern, item, re.IGNORECASE)), None)
        if label is None:
            return []
        if label not in labels:
            labels.append(label)
    return labels


class ChatRunner:
    """Run Codex in a clean child process, separate from the Doris process."""

    def __init__(
        self,
        *,
        project_root: Path = PROJECT_ROOT,
        backend_url: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        model_provider: str | None = None,
        model_base_url: str | None = None,
        timeout_seconds: int | None = None,
        max_concurrent: int | None = None,
        python_executable: str | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.backend_url = (
            backend_url or os.getenv("AGENT_CHAT_BACKEND_URL") or DEFAULT_BACKEND_URL
        ).strip().rstrip("/")
        if not _is_loopback_url(self.backend_url):
            raise RuntimeError("AGENT_CHAT_BACKEND_URL must use HTTP on a loopback host")
        self.model = (
            model
            or os.getenv("AGENT_CHAT_MODEL")
            or os.getenv("AGENT_CODEX_MODEL")
            or DEFAULT_MODEL
        ).strip()
        self.reasoning_effort = (
            reasoning_effort
            or os.getenv("AGENT_CHAT_REASONING_EFFORT")
            or os.getenv("AGENT_CODEX_REASONING_EFFORT")
            or DEFAULT_REASONING_EFFORT
        ).strip().casefold()
        self.model_provider = (
            model_provider
            or os.getenv("AGENT_CHAT_MODEL_PROVIDER")
            or os.getenv("AGENT_CODEX_MODEL_PROVIDER")
            or DEFAULT_MODEL_PROVIDER
        ).strip()
        self.model_base_url = (
            model_base_url
            or os.getenv("AGENT_CHAT_MODEL_BASE_URL")
            or os.getenv("AGENT_CODEX_BASE_URL")
            or DEFAULT_MODEL_BASE_URL
        ).strip().rstrip("/")
        self.timeout_seconds = timeout_seconds or _env_int(
            "AGENT_CHAT_TIMEOUT_SECONDS",
            DEFAULT_TIMEOUT_SECONDS,
            minimum=10,
            maximum=900,
        )
        self.max_concurrent = max_concurrent or _env_int(
            "AGENT_CHAT_MAX_CONCURRENT",
            DEFAULT_MAX_CONCURRENT,
            minimum=1,
            maximum=8,
        )
        self.python_executable = _windowless_python(python_executable or sys.executable)
        self.codex_home = self.project_root / "var" / "codex-home"
        # Internal responses are exchanged over the nonce-bound child pipe.
        # Business rows must not be persisted as an IPC file.
        self.internal_result_root = self.project_root / "var" / "chat-ipc"
        self._slots = threading.BoundedSemaphore(self.max_concurrent)

    @property
    def auth_configured(self) -> bool:
        return (self.codex_home / "auth.json").is_file()

    def context_home_for_ref(self, principal_ref: str) -> Path:
        return principal_codex_home(self.project_root, principal_ref)

    def _command(
        self,
        question: str,
        *,
        thread_id: str | None = None,
        context_home: Path | None = None,
        context_turn_count: int = 0,
        ui_selections: list[Mapping[str, Any]] | None = None,
        original_question: str | None = None,
        request_context: list[dict[str, Any]] | None = None,
        report_context: Mapping[str, Any] | None = None,
    ) -> list[str]:
        command = [
            self.python_executable,
            "-m",
            "app.codex_agent",
            question,
            "--model",
            self.model,
            "--reasoning-effort",
            self.reasoning_effort,
            "--model-provider",
            self.model_provider,
            "--model-base-url",
            self.model_base_url,
            "--codex-home",
            str(self.codex_home),
            "--backend-url",
            self.backend_url,
            "--project-root",
            str(self.project_root),
        ]
        if context_home is not None:
            command.extend(
                [
                    "--persistent-context",
                    "--context-home",
                    str(context_home),
                    "--context-turn-count",
                    str(max(0, int(context_turn_count))),
                ]
            )
        if thread_id:
            command.extend(["--thread-id", thread_id])
        if ui_selections:
            # Only opaque option IDs are passed to the child. Free-form labels
            # and user text remain in the browser's short-lived request body.
            command.extend(
                [
                    "--ui-selections-json",
                    json.dumps(ui_selections, ensure_ascii=False, separators=(",", ":")),
                ]
            )
        if isinstance(original_question, str) and original_question.strip():
            command.extend(["--original-question", original_question.strip()[:MAX_QUESTION_LENGTH]])
        if request_context:
            command.extend(["--request-context-json", json.dumps(request_context, ensure_ascii=False, separators=(",", ":"))])
        if report_context is not None:
            command.extend(["--report-context-json", json.dumps(report_context, ensure_ascii=False, separators=(",", ":"))])
        return command

    def _child_environment(
        self,
        *,
        internal_result_path: Path,
        internal_result_nonce: str,
    ) -> dict[str, str]:
        environment = os.environ.copy()
        for key in SENSITIVE_ENVIRONMENT_KEYS:
            environment.pop(key, None)
        environment.update(
            {
                "AGENT_QUERY_BACKEND_URL": self.backend_url,
                "AGENT_CODEX_HOME": str(self.codex_home),
                "PYTHONIOENCODING": "utf-8",
                "AGENT_CHAT_INTERNAL_RESULT_PATH": str(internal_result_path),
                "AGENT_CHAT_INTERNAL_RESULT_NONCE": internal_result_nonce,
                "AGENT_CHAT_INTERNAL_RESULT_STDOUT": "1",
            }
        )
        return environment

    def _new_internal_result_channel(self) -> tuple[Path, str]:
        """Create a single-use private result path and capability."""

        root = self.internal_result_root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        nonce = secrets.token_urlsafe(32)
        path = root / f"result-{secrets.token_hex(16)}.json"
        return path, nonce

    @staticmethod
    def _read_internal_result(path: Path, nonce: str) -> dict[str, Any]:
        """Read and validate the child result without accepting stdout data."""

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ChatExecutionError(
                "取数模型未返回可确认的执行状态",
                details={"reason": "internal_result_unavailable"},
            ) from exc
        if not isinstance(payload, Mapping):
            raise ChatExecutionError(
                "取数模型未返回可确认的执行状态",
                details={"reason": "internal_result_invalid"},
            )
        returned_nonce = payload.get("nonce")
        if not isinstance(returned_nonce, str) or not hmac.compare_digest(returned_nonce, nonce):
            raise ChatExecutionError(
                "取数模型未返回可确认的执行状态",
                details={"reason": "internal_result_invalid"},
            )
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ChatExecutionError(
                "取数模型未返回可确认的执行状态",
                details={"reason": "internal_result_invalid"},
            )
        return result

    @staticmethod
    def _read_internal_result_stdout(stdout: str, nonce: str) -> dict[str, Any]:
        """Read the bounded private envelope from the trusted child pipe."""
        try:
            payload = json.loads(stdout.strip())
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ChatExecutionError("取数模型未返回可确认的执行状态", details={"reason": "internal_result_unavailable"}) from exc
        if not isinstance(payload, Mapping) or not isinstance(payload.get("nonce"), str):
            raise ChatExecutionError("取数模型未返回可确认的执行状态", details={"reason": "internal_result_unavailable"})
        if not hmac.compare_digest(payload["nonce"], nonce):
            raise ChatExecutionError("取数模型未返回可确认的执行状态", details={"reason": "internal_result_invalid"})
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ChatExecutionError("取数模型未返回可确认的执行状态", details={"reason": "internal_result_invalid"})
        return result

    @staticmethod
    def _decode_output(stdout: str) -> dict[str, Any]:
        clean = ANSI_ESCAPE.sub("", stdout).strip()
        if not clean:
            raise ChatExecutionError(
                "模型进程没有返回结果",
                details={"reason": "empty_output"},
            )
        try:
            value = json.loads(clean)
        except json.JSONDecodeError:
            value = None
            decoder = json.JSONDecoder()
            for match in re.finditer(r"\{", clean):
                try:
                    candidate, _ = decoder.raw_decode(clean[match.start() :])
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict) and "status" in candidate:
                    value = candidate
                    break
            if value is None:
                raise ChatExecutionError(
                    "模型进程返回格式无效",
                    details={"reason": "invalid_json"},
                )
        if not isinstance(value, dict):
            raise ChatExecutionError(
                "模型进程返回格式无效",
                details={"reason": "non_object_json"},
            )
        return value

    def _restore_context_checkpoint(self, context_home, thread_id, callback=None):
        if context_home is None:
            return thread_id
        from .codex_context_checkpoint import ContextCheckpoint, ContextCheckpointError
        try:
            home = Path(context_home).resolve()
            home.relative_to(self.project_root / "var" / "codex-contexts")
            recovered = ContextCheckpoint(home).resolve(thread_id)
        except (ValueError, ContextCheckpointError) as exc:
            reason = str(exc) if isinstance(exc, ContextCheckpointError) else "context_checkpoint_invalid"
            raise ChatExecutionError(
                "后台对话初始化状态需要核对，未创建新的对话",
                details={"reason": reason},
            ) from None
        if recovered is not None and callback is not None:
            callback(recovered)
        return recovered

    def run(self, question, *, context_home=None, thread_id=None, context_turn_count=0,
            ui_selections=None, original_question=None, request_context=None,
            report_context=None,
            on_context_ready=None, cancel_event=None):
        try:
            return self._run(
                _prepare_model_question(question), context_home=context_home, thread_id=thread_id,
                context_turn_count=context_turn_count, ui_selections=ui_selections,
                original_question=original_question, request_context=request_context,
                report_context=report_context,
                on_context_ready=on_context_ready, cancel_event=cancel_event,
            )
        finally:
            # A failed/terminated child can already own a durable Codex thread.
            # Save its identity without treating the failed turn as a result.
            self._restore_context_checkpoint(context_home, thread_id, on_context_ready)

    def _run(
        self,
        question: str,
        *,
        context_home: Path | None = None,
        thread_id: str | None = None,
        context_turn_count: int = 0,
        ui_selections: list[Mapping[str, Any]] | None = None,
        original_question: str | None = None,
        request_context: list[dict[str, Any]] | None = None,
        report_context: Mapping[str, Any] | None = None,
        on_context_ready=None,
        cancel_event=None,
    ) -> dict[str, Any]:
        if not self.auth_configured:
            raise ChatExecutionError(
                "模型认证尚未配置",
                details={"reason": "auth_not_configured"},
            )
        started = time.perf_counter()
        if cancel_event is not None and cancel_event.is_set():
            raise ChatExecutionError("查询已停止", details={"reason": "query_cancelled"})
        internal_result_path, internal_result_nonce = self._new_internal_result_channel()
        attempt = 0
        while True:
            thread_id = self._restore_context_checkpoint(context_home, thread_id, on_context_ready)
            command = self._command(
                question, thread_id=thread_id, context_home=context_home,
                context_turn_count=context_turn_count, ui_selections=ui_selections,
                original_question=original_question, request_context=request_context,
                report_context=report_context,
            )
            try:
                with self._slots:
                    remaining = self.timeout_seconds - (time.perf_counter() - started)
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(command, self.timeout_seconds)
                    if cancel_event is None:
                        completed = subprocess.run(
                            command,
                            cwd=str(self.project_root),
                            env=self._child_environment(
                                internal_result_path=internal_result_path,
                                internal_result_nonce=internal_result_nonce,
                            ),
                            capture_output=True,
                            text=True,
                            encoding="utf-8",
                            errors="replace",
                            timeout=remaining,
                            check=False,
                            **_hidden_process_options(),
                        )
                    else:
                        if cancel_event.is_set():
                            raise ChatExecutionError("查询已停止", details={"reason": "query_cancelled"})
                        process = subprocess.Popen(
                            command,
                            cwd=str(self.project_root),
                            env=self._child_environment(
                                internal_result_path=internal_result_path,
                                internal_result_nonce=internal_result_nonce,
                            ),
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                            encoding="utf-8",
                            errors="replace",
                            **_hidden_process_options(),
                        )
                        while True:
                            try:
                                stdout, stderr = process.communicate(timeout=0.2)
                                break
                            except subprocess.TimeoutExpired as exc:
                                if cancel_event.is_set():
                                    _terminate_process_tree(process)
                                    stdout, stderr = process.communicate(timeout=5)
                                    raise ChatExecutionError(
                                        "查询已停止",
                                        details={"reason": "query_cancelled"},
                                    )
                                if time.perf_counter() - started >= self.timeout_seconds:
                                    _terminate_process_tree(process)
                                    stdout, stderr = process.communicate(timeout=5)
                                    raise subprocess.TimeoutExpired(
                                        command, self.timeout_seconds, output=stdout, stderr=stderr
                                    ) from exc
                        completed = subprocess.CompletedProcess(
                            command, process.returncode, stdout, stderr
                        )
            except ChatExecutionError:
                try:
                    internal_result_path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            except subprocess.TimeoutExpired as exc:
                progress = last_progress(exc.stderr)
                if progress is not None:
                    LOGGER.warning("Codex worker timeout: phase=%s event=%s step=%s elapsed_ms=%s",
                                   progress["phase"], progress["event"], progress["step"], progress["elapsed_ms"])
                try:
                    internal_result_path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise ChatTimeoutError(
                    "本次取数等待超时，未返回可确认结果",
                    details={"reason": "process_timeout"},
                ) from exc
            except OSError as exc:
                if attempt + 1 < CHAT_CHILD_MAX_ATTEMPTS:
                    attempt += 1
                    LOGGER.warning(
                        "Unable to start isolated Codex process; retrying attempt=%s error_type=%s",
                        attempt + 1,
                        type(exc).__name__,
                    )
                    time.sleep(CHAT_CHILD_RETRY_DELAY_SECONDS)
                    continue
                try:
                    internal_result_path.unlink(missing_ok=True)
                except OSError:
                    pass
                LOGGER.warning("Unable to start isolated Codex process: %s", type(exc).__name__)
                raise ChatExecutionError(
                    "无法启动取数模型进程",
                    details={"reason": "process_start_failed"},
                ) from exc
            progress = last_progress(completed.stderr)
            if (completed.returncode != 0 and progress is not None
                    and progress["phase"] == "model" and progress["event"] == "timeout"):
                LOGGER.warning("Codex model budget exhausted: step=%s elapsed_ms=%s",
                               progress["step"], progress["elapsed_ms"])
                raise ChatTimeoutError(
                    "模型处理超时，本次未返回完整结果。可以稍后重试。",
                    details={"reason": "model_timeout"},
                )
            if (
                completed.returncode != 0
                and attempt + 1 < CHAT_CHILD_MAX_ATTEMPTS
                and _is_transient_child_failure(completed.stderr)
            ):
                LOGGER.warning(
                    "Transient Codex child failure; retrying attempt=%s signature=%s",
                    attempt + 2,
                    _process_failure_signature(completed.stderr),
                )
                try:
                    internal_result_path.unlink(missing_ok=True)
                except OSError:
                    pass
                attempt += 1
                time.sleep(CHAT_CHILD_RETRY_DELAY_SECONDS)
                continue
            break
        try:
            if completed.returncode != 0:
                LOGGER.warning(
                    "Codex child exited with code %s; stderr_signature=%s stderr_type=%s",
                    completed.returncode,
                    _process_failure_signature(completed.stderr),
                    type(completed.stderr).__name__,
                )
                raise ChatExecutionError(
                    "取数模型执行失败，请稍后重试",
                    details={"reason": "process_failed", "exit_code": completed.returncode},
                )
            # Production workers use the nonce-bound pipe. Keep the legacy file
            # reader only for embedded callers that explicitly wrote that
            # capability during a compatibility test.
            if internal_result_path.is_file():
                result = self._read_internal_result(internal_result_path, internal_result_nonce)
            else:
                result = self._read_internal_result_stdout(completed.stdout, internal_result_nonce)
            # Report-writing turns use the same hidden child transport, but do
            # not produce Doris evidence or a public query answer. Hand the
            # screened narrative back to the report endpoint unchanged.
            if isinstance(result.get("report_narrative"), Mapping):
                return result
            if isinstance(result.get("query_error"), Mapping):
                # Keep the thread ID so the caller can bind the same context
                # before returning a safe, typed query failure.
                failure = safe_host_failure(result.get("host_failure"))
                LOGGER.warning("Host query incomplete: stage=%s diagnostic=%s step=%s",
                               failure["stage"], failure["diagnostic"], failure["step"])
                return result
            answer = result.get("final_response")
            if result.get("status") != "completed" or not isinstance(answer, str):
                raise ChatExecutionError(
                    "取数模型未完成本次请求",
                    details={"reason": "turn_incomplete"},
                )
            interaction = interaction_envelope(answer)
            evidence = _query_execution_evidence(
                result.get("mcp_calls"),
                result.get("query_execution"),
            )
            # A completed Codex turn without a successful query may only be a
            # clarification/block response. Rejecting other responses prevents a
            # model-generated number from being presented as a live Doris result.
            if evidence["unexpected_tool_call_count"]:
                LOGGER.warning(
                    "Codex result rejected: reason=unexpected_tool_call tools=%s",
                    [
                        {
                            "server": call.get("server"),
                            "tool": call.get("tool"),
                            "status": call.get("status"),
                        }
                        for call in (result.get("mcp_calls") or [])
                        if not (
                            call.get("server") == "doris_query_agent"
                            and call.get("tool") == "query_doris_sql"
                        )
                    ],
                )
                raise ChatExecutionError(
                    "模型尝试调用未授权工具，已阻止本次请求",
                    details={"reason": "unexpected_tool_call"},
                )
            if evidence.get("status") == "evidence_invalid":
                raise ChatExecutionError(
                    "查询执行证据不一致，未返回可确认的实时结果",
                    details={"reason": "invalid_query_evidence"},
                )
            has_business_result = evidence.get(
                "live_business_result_executed",
                evidence.get("live_query_executed", False),
            )
            if not has_business_result and not _allows_no_live_query(answer):
                LOGGER.warning(
                    "Codex result rejected: reason=no_live_query outcome=%s evidence_status=%s",
                    interaction.get("outcome"),
                    evidence.get("status"),
                )
                raise ChatExecutionError(
                    "模型没有完成 Doris 查询，未返回可确认的实时结果",
                    details={"reason": "no_live_query"},
                )
            interaction_outcome = str(interaction.get("outcome") or "").casefold()
            if has_business_result and interaction_outcome == "clarification_required":
                # A planning turn may have executed a preliminary, host-side
                # probe before the model asks for one remaining business
                # choice.  The probe is private and must never be shown as
                # the answer, but a valid selectable clarification is still a
                # successful interaction.  Reject malformed empty choices;
                # keep blocked/failed outcomes on the error path below.
                questions = interaction.get("questions")
                if not isinstance(questions, list) or not questions:
                    LOGGER.warning(
                        "Codex result rejected: reason=clarification_contract_missing_questions evidence_status=%s",
                        evidence.get("status"),
                    )
                    raise ChatExecutionError(
                        "已执行查询，但后续口径选择未形成有效选项",
                        details={"reason": "clarification_contract_invalid"},
                    )
                LOGGER.info(
                    "Codex result accepted: selectable clarification after live probe evidence_status=%s",
                    evidence.get("status"),
                )
            elif has_business_result and (
                interaction_outcome != "completed"
                or not isinstance(interaction.get("answer"), str)
            ):
                LOGGER.warning(
                    "Codex result rejected: reason=business_answer_unavailable outcome=%s answer_present=%s evidence_status=%s verified_single=%s verified_named=%s normalized_state=%s result_state=%s host_stage=%s recovery=%s questions=%s",
                    interaction_outcome,
                    isinstance(interaction.get("answer"), str),
                    evidence.get("status"),
                    isinstance(result.get("verified_business_result"), Mapping),
                    isinstance(result.get("verified_business_results"), list),
                    result.get("normalized_result_state"),
                    result.get("result_state"),
                    (result.get("host_failure") or {}).get("stage")
                    if isinstance(result.get("host_failure"), Mapping)
                    else None,
                    bool(result.get("recovery_attempted")),
                    len(interaction.get("questions"))
                    if isinstance(interaction.get("questions"), list)
                    else None,
                )
                raise ChatExecutionError(
                    "取数已执行，但业务结果未通过展示安全检查",
                    details={"reason": "business_answer_unavailable"},
                )
            result["interaction"] = interaction
            result["query_execution"] = evidence
            result["elapsed_ms"] = max(
                0, round((time.perf_counter() - started) * 1000)
            )
            return result
        finally:
            try:
                internal_result_path.unlink(missing_ok=True)
            except OSError:
                LOGGER.info("Unable to remove private Codex result path")

    def public_health(self, backend_health: dict[str, Any] | None) -> dict[str, Any]:
        backend_ready = bool(backend_health and backend_health.get("ready"))
        backend_status = backend_health.get("status") if backend_health else "unavailable"
        connection_error_type = (
            backend_health.get("executor", {}).get("connection_error_type")
            if isinstance(backend_health, Mapping)
            and isinstance(backend_health.get("executor"), Mapping)
            else None
        )
        if connection_error_type not in {"timeout", "refused", "authentication", "connection_error"}:
            connection_error_type = None
        return {
            "status": "ok" if self.auth_configured and backend_ready else "degraded",
            "ready": self.auth_configured and backend_ready,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "model_auth_configured": self.auth_configured,
            "query_backend": {
                "ready": backend_ready,
                "status": backend_status,
                "connection_tested": bool(
                    backend_health
                    and isinstance(backend_health.get("executor"), Mapping)
                    and backend_health["executor"].get("connection_tested")
                ),
                "connection_ready": bool(
                    backend_health
                    and isinstance(backend_health.get("executor"), Mapping)
                    and backend_health["executor"].get("connection_ready")
                ),
                "connection_error_type": connection_error_type,
            },
        }


def _read_backend_health(backend_url: str) -> dict[str, Any] | None:
    try:
        import httpx

        with httpx.Client(timeout=3.0, trust_env=False, follow_redirects=False) as client:
            response = client.get(f"{backend_url}/health")
        if not response.is_success:
            return None
        value = response.json()
        return value if isinstance(value, dict) else None
    except Exception as exc:  # pragma: no cover - depends on local service state
        LOGGER.info("Query backend health unavailable: %s", type(exc).__name__)
        return None


def create_chat_app(
    runner: ChatRunner | None = None,
    *,
    session_store: ChatSessionStore | None = None,
    identity_resolver: IdentityResolver | None = None,
    artifact_store: ArtifactStore | None = None,
    turnover_runtime: TurnoverReportRuntime | Any | None = None,
    semantic_runtime: SemanticQueryRuntime | Any | None = None,
    learning_ledger: LearningLedger | Any | None = None,
    sql_converter: Any | None = None,
    proposal_baseline_loader: Any | None = None,
    interpretation_runner: Any | None = None,
    enforce_loopback: bool | None = None,
    secure_cookies: bool | None = None,
    task_timeout_seconds: int | None = None,
) -> Any:
    try:
        from fastapi import Body, FastAPI, Header, HTTPException, Request, Response
        from fastapi.exceptions import RequestValidationError
        from fastapi.responses import FileResponse, JSONResponse
        from fastapi.staticfiles import StaticFiles
        from starlette.exceptions import HTTPException as StarletteHTTPException
    except ImportError as exc:  # pragma: no cover - required runtime dependency
        raise RuntimeError("FastAPI is required for the chat frontend") from exc

    chat_runner = runner or ChatRunner()
    if enforce_loopback is None:
        enforce_loopback = _chat_network_mode() == "loopback"
    identity = identity_resolver or IdentityResolver()
    store = session_store or ChatSessionStore(
        _session_db_path(),
        principal_pepper=os.getenv("AGENT_CHAT_PRINCIPAL_PEPPER") or identity.secret,
        max_conversations=_env_int(
            "AGENT_CHAT_MAX_CONVERSATIONS", 1, minimum=1, maximum=10_000
        ),
    )
    network_access = NetworkAccessRegistry(store.path)
    report_artifacts = artifact_store or ArtifactStore(
        _artifact_root_from_env(),
        ttl_seconds=_env_int(
            "AGENT_REPORT_ARTIFACT_TTL_SECONDS",
            DEFAULT_ARTIFACT_TTL_SECONDS,
            minimum=60,
            maximum=7 * 24 * 60 * 60,
        ),
    )
    report_files = ReportFileStore(
        _report_file_root_from_env(),
        ttl_seconds=_env_int(
            "AGENT_REPORT_FILE_TTL_SECONDS",
            DEFAULT_ARTIFACT_TTL_SECONDS,
            minimum=60,
            maximum=7 * 24 * 60 * 60,
        ),
    )
    report_service = turnover_runtime or TurnoverReportRuntime(
        backend_url=chat_runner.backend_url,
        artifact_store=report_artifacts,
        project_root=PROJECT_ROOT,
        timezone_name=(os.getenv("AGENT_BUSINESS_TIMEZONE") or "Asia/Shanghai").strip(),
    )
    try:
        learning_service = learning_ledger or LearningLedger(_learning_db_path(store))
    except Exception as exc:  # pragma: no cover - depends on deployment storage
        LOGGER.warning("Learning ledger initialization unavailable: %s", type(exc).__name__)
        learning_service = None
    semantic_service = semantic_runtime
    # Bind public health/status and semantic planning to the same immutable
    # release snapshot used by the execution runtimes.  The workspace
    # knowledge directory may contain candidates, but candidates must never
    # appear as the online knowledge state.
    knowledge_root, trusted_release_digest = active_snapshot(PROJECT_ROOT)
    knowledge_base = KnowledgeBase(
        knowledge_root,
        reload_on_change=True,
        trusted_release_sha256=trusted_release_digest,
        require_trusted_release=trusted_release_digest is not None,
    )
    # Custom/fake runners used by API tests retain their transport behavior.
    # In production the semantic runtime is an explicit-rule executor only;
    # free-form natural language is always sent to the model-first path below.
    if semantic_service is None and isinstance(chat_runner, ChatRunner):
        semantic_service = SemanticQueryRuntime(
            backend_url=chat_runner.backend_url,
            timezone_name=(os.getenv("AGENT_BUSINESS_TIMEZONE") or "Asia/Shanghai").strip(),
        )
    agent_core = (
        AgentOrchestrator(semantic_service, project_root=PROJECT_ROOT)
        if semantic_service is not None
        else None
    )
    cookie_secure = (
        _env_bool("AGENT_CHAT_SECURE_COOKIES", False)
        if secure_cookies is None
        else secure_cookies
    )
    if not FRONTEND_ROOT.is_dir():
        raise RuntimeError(f"Frontend directory is missing: {FRONTEND_ROOT}")
    if task_timeout_seconds is None:
        task_timeout_seconds = _env_int(
            "AGENT_CHAT_TASK_TIMEOUT_SECONDS",
            DEFAULT_TASK_TIMEOUT_SECONDS,
            minimum=30,
            maximum=900,
        )
    elif type(task_timeout_seconds) is not int or not 1 <= task_timeout_seconds <= 900:
        raise ValueError("task_timeout_seconds must be between 1 and 900")
    if isinstance(chat_runner, ChatRunner):
        # Keep the subprocess deadline inside the durable task deadline. This
        # prevents an outer asyncio cancellation from leaving a blocking
        # subprocess alive in the worker thread.
        chat_runner.timeout_seconds = min(
            chat_runner.timeout_seconds,
            max(1, task_timeout_seconds - 15),
        )

    live_chat_tasks: dict[str, asyncio.Task[Any]] = {}
    # One cancellation signal is shared by the durable task, its worker, and
    # the isolated model process. It is deliberately in-memory and short-lived.
    cancel_events: dict[str, threading.Event] = {}
    task_store = ChatTaskStore(store.path, live_task_ids=lambda: tuple(
        identifier for identifier, worker in live_chat_tasks.items() if not worker.done()
    ))
    result_pages = ResultPageStore()
    ranking_contexts = RankingContextStore()
    detail_contexts = SalesDetailContextStore()
    sales_exports = SalesExportStore(store.path)
    export_workers = {}
    export_cache = {}
    proposal_contexts = ProposalContextStore(presenter=lambda baseline, report: public_proposal_assessment(
        baseline, report, screen_text=_public_table_text))
    interpretation_contexts = InterpretationContextStore(
        screen_text=_public_table_text, create_proposal=proposal_contexts.create)
    definition_runner = interpretation_runner
    task_workers: set[asyncio.Task[Any]] = set()
    principal_task_locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def lifespan(_app: Any):
        recover = getattr(store, "recover_after_restart", None)
        if callable(recover):
            recovered = await asyncio.to_thread(recover)
            if isinstance(recovered, Mapping) and any(
                int(value or 0) > 0 for value in recovered.values()
            ):
                LOGGER.warning(
                    "Recovered transient chat state after worker restart: %s",
                    {key: int(value or 0) for key, value in recovered.items()},
                )
        task_store.recover()
        yield
        for event in cancel_events.values():
            event.set()
        for worker in task_workers:
            worker.cancel()
        if task_workers:
            await asyncio.gather(*task_workers, return_exceptions=True)

    app = FastAPI(
        title="Doris Query Workbench",
        version="0.1.0",
        description="Local chat frontend for the isolated Doris query agent.",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.chat_runner = chat_runner
    app.state.chat_session_store = store
    app.state.identity_resolver = identity
    app.state.artifact_store = report_artifacts
    app.state.report_file_store = report_files
    app.state.turnover_runtime = report_service
    app.state.semantic_runtime = semantic_service
    app.state.agent_core = agent_core
    app.state.knowledge_base = knowledge_base
    app.state.learning_ledger = learning_service
    app.state.task_store = task_store
    app.state.result_pages = result_pages
    app.state.ranking_contexts = ranking_contexts
    app.state.detail_contexts = detail_contexts
    app.state.sales_exports = sales_exports
    app.state.proposal_contexts = proposal_contexts
    app.state.interpretation_contexts = interpretation_contexts
    app.state.network_access = network_access

    @app.middleware("http")
    async def security_middleware(request: Request, call_next: Any) -> Any:
        request_path = request.url.path
        is_access_request = request_path in {
            "/network-access/request",
            "/v1/network-access/request",
        }
        is_access_request_asset = request_path in {
            "/static/network-access.css",
            "/static/network-access-request.js",
        }
        allowed_by_config = _request_is_allowed_network(request)
        client_host = getattr(getattr(request, "client", None), "host", "") or ""
        allowed_by_approval = (
            _chat_network_mode() == "lan"
            and not allowed_by_config
            and network_access.is_approved(str(client_host))
        )
        if (is_access_request or is_access_request_asset) and _chat_network_mode() == "lan" and not allowed_by_config:
            return await call_next(request)
        if enforce_loopback and not (allowed_by_config or allowed_by_approval):
            return JSONResponse(
                status_code=403,
                content={
                    "error": {
                        "code": "loopback_required",
                        "message": "聊天服务仅允许通过受控本机入口访问",
                        "details": {},
                    }
                },
            )
        if _chat_network_mode() == "lan" and not (allowed_by_config or allowed_by_approval):
            if _is_browser_navigation(request):
                return RedirectResponse(
                    url="/network-access/request",
                    status_code=303,
                    headers={"Cache-Control": "no-store"},
                )
            request_record = network_access.record_request(str(client_host))
            return JSONResponse(
                status_code=403,
                content={
                    "error": {
                        "code": "network_access_pending",
                        "message": "该内网地址尚未审批，请先打开 /network-access/request 提交访问申请",
                        "details": {
                            "request_id": request_record.get("ip") if request_record else None,
                        },
                    }
                },
            )
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers[
            "Content-Security-Policy"
        ] = "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'"
        return response

    @app.exception_handler(RuntimeErrorBase)
    async def runtime_error_handler(_request: Any, exc: RuntimeErrorBase) -> JSONResponse:
        # The browser is a process-only surface.  Even a seemingly harmless
        # validation detail (for example an unknown field name) can reveal an
        # internal route or encourage callers to probe the SQL API.  Preserve
        # only the stable error code/message contract; diagnostics stay in
        # server logs and the internal runner result.
        public_error = exc.as_public_dict()
        details = public_error.get("details")
        if (
            public_error.get("code") == "query_invalid_result"
            and isinstance(details, Mapping)
            and isinstance(details.get("missing_fields"), list)
        ):
            public_error["details"] = {
                "missing_fields": [
                    field for field in details["missing_fields"]
                    if isinstance(field, str) and len(field) <= 80
                ][:12]
            }
        else:
            public_error["details"] = {}
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": public_error},
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        _request: Any, _exc: RequestValidationError
    ) -> JSONResponse:
        # FastAPI's default validation payload can include body fragments and
        # field locations. Keep the public chat contract process-only even for
        # malformed JSON or unsupported request shapes.
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "invalid_parameters",
                    "message": "请求格式无效，请检查后重试",
                    "details": {},
                }
            },
        )

    async def http_error_handler(_request: Any, exc: HTTPException) -> JSONResponse:
        # Do not let framework-generated 404/405 details or headers become a
        # secondary information-disclosure surface.
        status_code = int(exc.status_code) if 400 <= int(exc.status_code) <= 599 else 500
        code = "endpoint_not_found" if status_code == 404 else "request_rejected"
        message = "服务入口尚未开放" if status_code == 404 else "请求未被接受"
        return JSONResponse(
            status_code=status_code,
            content={
                "error": {
                    "code": code,
                    "message": message,
                    "details": {},
                }
            },
        )

    # JSON decoding failures are raised by Starlette's base exception type;
    # register both classes so neither framework default can echo request data.
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
    app.add_exception_handler(HTTPException, http_error_handler)

    @app.exception_handler(Exception)
    async def unexpected_error_handler(_request: Request, exc: Exception) -> JSONResponse:
        # Keep an unexpected provider/library exception from becoming a raw
        # traceback or message on the process-only browser surface. The type is
        # sufficient for server-side logs and does not expose query content.
        LOGGER.exception("Unhandled chat service error: %s", type(exc).__name__)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "service_unavailable",
                    "message": "取数服务暂时不可用，请稍后重试",
                    "details": {},
                }
            },
        )

    @app.get("/live")
    async def live() -> dict[str, Any]:
        """Cheap process liveness endpoint used by the hidden supervisor."""

        return {"status": "ok", "ready": True, "service": "chat"}

    @app.get("/health")
    async def health() -> dict[str, Any]:
        backend_health = await asyncio.to_thread(
            _read_backend_health, chat_runner.backend_url
        )
        payload = chat_runner.public_health(backend_health)
        payload['exports'] = {'active': len(export_workers), 'capacity': 2}
        payload["agent_core"] = {
            "version": "5.0",
            "mode": "model-first-query-lifecycle-v5",
            "natural_language_route": "model_first",
            "explicit_rule_route": "direct_rule",
            "legacy_natural_language_router": False,
            "generic_fallback": False,
        }
        try:
            payload["knowledge"] = await asyncio.to_thread(knowledge_base.health)
        except Exception as exc:
            LOGGER.warning("Knowledge health unavailable: %s", type(exc).__name__)
            payload["knowledge"] = {"ready": False, "status": "unavailable"}
            payload["ready"] = False
            payload["status"] = "degraded"
        try:
            payload["sessions"] = await asyncio.to_thread(store.health)
        except Exception as exc:  # pragma: no cover - depends on local filesystem
            LOGGER.warning("Chat session store health unavailable: %s", type(exc).__name__)
            payload["sessions"] = {"ready": False, "mode": "unavailable"}
            payload["ready"] = False
            payload["status"] = "degraded"
        try:
            if learning_service is None:
                raise RuntimeError("learning ledger unavailable")
            payload["learning"] = await asyncio.to_thread(learning_service.health)
        except Exception as exc:  # pragma: no cover - depends on local filesystem
            LOGGER.warning("Learning ledger health unavailable: %s", type(exc).__name__)
            # Learning is an auxiliary review stream; a temporary ledger issue
            # must not make an otherwise healthy read-only query unavailable.
            payload["learning"] = {
                "ready": False,
                "status": "unavailable",
                "production_auto_update": False,
            }
        return payload

    @app.get("/v1/health")
    async def versioned_health() -> dict[str, Any]:
        """Expose the documented health contract alongside the local probe."""

        return await health()

    def require_local_admin(request: Request) -> None:
        if not _request_is_loopback(request):
            raise SessionSecurityError("内网访问审批仅允许在服务机本地操作")

    @app.get("/v1/network-access/requests")
    async def network_access_requests(request: Request, response: Response) -> dict[str, Any]:
        require_local_admin(request)
        if _is_cross_site_fetch(request) or not _same_origin(request):
            raise SessionSecurityError("请求来源校验失败")
        session = require_session(request)
        rows = await asyncio.to_thread(network_access.list)
        _set_session_cookies(response, session, secure=cookie_secure)
        return {"status": "ok", "requests": rows}

    @app.post("/v1/network-access/request")
    async def submit_network_access_request(
        request: Request,
        payload: Any = Body(default=None),
    ) -> dict[str, Any]:
        if not _same_origin(request):
            raise SessionSecurityError("请求来源校验失败")
        client_host = getattr(getattr(request, "client", None), "host", "") or ""
        note = payload.get("note", "") if isinstance(payload, dict) else ""
        if not isinstance(note, str):
            raise ParameterValidationError("备注格式无效")
        try:
            ip = normalize_private_ip(str(client_host))
        except ValueError:
            raise ParameterValidationError("只能提交私有内网客户端 IP") from None
        row = await asyncio.to_thread(network_access.record_request, ip, note)
        if row is None:
            raise ParameterValidationError("访问来源不是可审批的内网 IP")
        return {"status": row["status"], "request": row}

    @app.post("/v1/network-access/approve")
    async def approve_network_access(
        request: Request,
        response: Response,
        payload: Any = Body(default=None),
        x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    ) -> dict[str, Any]:
        require_local_admin(request)
        session = check_state_change_request(request, x_csrf_token)
        if not isinstance(payload, dict) or not isinstance(payload.get("ip"), str):
            raise ParameterValidationError("IP address is required")
        try:
            row = await asyncio.to_thread(network_access.approve, payload["ip"])
        except ValueError:
            raise ParameterValidationError("Only a private LAN IP can be approved") from None
        _set_session_cookies(response, session, secure=cookie_secure)
        return {"status": "approved", "request": row}

    @app.post("/v1/network-access/reject")
    async def reject_network_access(
        request: Request,
        response: Response,
        payload: Any = Body(default=None),
        x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    ) -> dict[str, Any]:
        require_local_admin(request)
        session = check_state_change_request(request, x_csrf_token)
        if not isinstance(payload, dict) or not isinstance(payload.get("ip"), str):
            raise ParameterValidationError("IP address is required")
        try:
            row = await asyncio.to_thread(network_access.reject, payload["ip"])
        except ValueError:
            raise ParameterValidationError("Only a private LAN IP can be rejected") from None
        _set_session_cookies(response, session, secure=cookie_secure)
        return {"status": "rejected", "request": row}

    @app.get("/v1/knowledge/status")
    async def knowledge_status(request: Request, response: Response) -> dict[str, Any]:
        if _is_cross_site_fetch(request) or not _same_origin(request):
            raise SessionSecurityError("请求来源校验失败")
        session = require_session(request)
        status = knowledge_base.health()
        status["iteration"] = {
            "mode": "candidate_then_review",
            "production_auto_update": False,
            "candidate_inbox": True,
        }
        _set_session_cookies(response, session, secure=cookie_secure)
        return status

    @app.get("/v1/rules")
    async def available_rules(request: Request, response: Response) -> dict[str, Any]:
        """Return the versioned explicit-rule catalog used by the top picker."""
        if _is_cross_site_fetch(request) or not _same_origin(request):
            raise SessionSecurityError("请求来源校验失败")
        session = require_session(request)
        payload = rule_catalog(PROJECT_ROOT)
        _set_session_cookies(response, session, secure=cookie_secure)
        return payload

    def require_session(request: Request) -> SessionContext:
        raw_session = request.cookies.get(SESSION_COOKIE_NAME)
        if not raw_session:
            raise SessionSecurityError("会话已失效，请刷新页面重试")
        principal = identity.resolve(request.headers)
        session = store.ensure_session(
            raw_session,
            request.cookies.get(CSRF_COOKIE_NAME),
            principal,
            allow_create=False,
        )
        if session.is_new:
            # A changed/expired cookie must not silently become a new identity for
            # a state-changing request. The browser first bootstraps /v1/session.
            raise SessionSecurityError("会话已失效，请刷新页面重试")
        return session

    def check_state_change_request(request: Request, csrf_token: str | None) -> SessionContext:
        if not _same_origin(request):
            raise SessionSecurityError("请求来源校验失败")
        session = require_session(request)
        store.require_csrf(session, csrf_token)
        return session

    async def record_learning_query(
        *,
        session: SessionContext,
        conversation_id: str,
        request_id: str,
        question: str,
        outcome: str,
        execution_status: str,
        has_business_result: bool,
        route_family: str | None = None,
    ) -> bool:
        """Persist a bounded learning event without affecting query success."""

        try:
            if learning_service is None:
                return False
            await asyncio.to_thread(
                learning_service.record_query,
                principal_hash=session.principal_hash,
                conversation_id=conversation_id,
                request_id=request_id,
                question=question,
                outcome=outcome,
                execution_status=execution_status,
                has_business_result=has_business_result,
                route_family=route_family,
            )
            return True
        except Exception as exc:  # pragma: no cover - defensive persistence boundary
            LOGGER.warning("Unable to persist learning query event: %s", type(exc).__name__)
            return False

    async def record_learning_definition(
        *,
        session: SessionContext,
        conversation_id: str | None,
        metric_name: str,
        definition_text: str,
        example_question: str | None,
    ) -> None:
        """Mirror a submitted definition into the private candidate inbox."""

        try:
            if learning_service is None:
                return
            await asyncio.to_thread(
                learning_service.record_definition_submission,
                principal_hash=session.principal_hash,
                conversation_id=conversation_id,
                metric_name=metric_name,
                definition_text=definition_text,
                example_question=example_question,
            )
        except Exception as exc:  # pragma: no cover - defensive persistence boundary
            LOGGER.warning("Unable to persist learning definition candidate: %s", type(exc).__name__)

    @app.get("/v1/session")
    async def session_bootstrap(request: Request, response: Response) -> dict[str, Any]:
        if _is_cross_site_fetch(request):
            raise SessionSecurityError("请求来源校验失败")
        principal = identity.resolve(request.headers)
        session = store.ensure_session(
            request.cookies.get(SESSION_COOKIE_NAME),
            request.cookies.get(CSRF_COOKIE_NAME),
            principal,
        )
        requested_conversation = request.query_params.get("conversation_id")
        conversation_id: str | None = None
        if requested_conversation:
            try:
                conversation_id = store.assert_owned(session, requested_conversation)
            except ConversationNotFoundError:
                # A stale tab or principal switch gets no ID from a read-only bootstrap;
                # fall back to this principal's canonical row below.  No other
                # principal's existence is disclosed by this path.
                conversation_id = None
        if conversation_id is None:
            # `new_conversation=true` is retained for old clients, but it can
            # never fork backend state.  A principal always resolves to its
            # one canonical conversation; only a brand-new principal returns
            # null until the CSRF-protected POST allocates it.
            conversation_id = store.find_default_conversation(session)
        clarification_pending = bool(
            conversation_id
            and store.has_pending_clarification(session, conversation_id)
        )
        _set_session_cookies(response, session, secure=cookie_secure)
        return _public_session_payload(
            session,
            conversation_id,
            identity_mode=identity.mode,
            context_state=store.public_context_state(store.context(session)),
            clarification_pending=clarification_pending,
        )

    @app.get("/v1/artifacts/{artifact_id}")
    async def get_artifact(request: Request, artifact_id: str) -> Any:
        """Serve a report image only to its installation principal and canonical chat."""

        session = require_session(request)
        conversation_id = store.find_default_conversation(session)
        if not conversation_id:
            raise HTTPException(status_code=404, detail="artifact not found")
        try:
            record = report_artifacts.resolve(
                artifact_id,
                principal_hash=session.principal_hash,
                conversation_id=conversation_id,
            )
        except ArtifactNotFound as exc:
            # Foreign, expired, malformed, and missing IDs intentionally share
            # one response so the endpoint cannot be used as an existence oracle.
            raise HTTPException(status_code=404, detail="artifact not found") from exc
        return FileResponse(
            record.path,
            media_type="image/png",
            filename="royal-tiger-turnover.png",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/v1/reports/files/{file_id}")
    async def get_report_file(request: Request, file_id: str) -> Any:
        """Serve one generated report file to its owning installation only."""

        session = require_session(request)
        conversation_id = store.find_default_conversation(session)
        if not conversation_id:
            raise HTTPException(status_code=404, detail="report file not found")
        try:
            record = report_files.resolve(
                file_id,
                principal_hash=session.principal_hash,
                conversation_id=conversation_id,
            )
        except ReportFileNotFound as exc:
            raise HTTPException(status_code=404, detail="report file not found") from exc
        return FileResponse(
            record.path,
            media_type=record.content_type,
            filename=record.filename,
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/v1/reports")
    async def create_report(
        request: Request,
        response: Response,
        payload: Any = Body(default=None),
        x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    ) -> Any:
        """Build a multi-format report from a completed server-side query receipt."""

        session = check_state_change_request(request, x_csrf_token)
        if not isinstance(payload, dict):
            raise ParameterValidationError("request body must be a JSON object")
        allowed = {"conversation_id", "request_id", "title", "audience", "formats", "instructions"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ParameterValidationError("Unknown report fields", details={"unknown": unknown})
        raw_conversation = payload.get("conversation_id")
        if raw_conversation is not None and (
            not isinstance(raw_conversation, str) or len(raw_conversation) > 100
        ):
            raise ParameterValidationError("conversation_id must be a short string")
        conversation_id = store.resolve(session, raw_conversation)
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{16,80}", request_id):
            raise ParameterValidationError("request_id is invalid")
        formats = payload.get("formats")
        if formats is None:
            formats = list(SUPPORTED_REPORT_FORMATS)
        if not isinstance(formats, list) or not 1 <= len(formats) <= len(SUPPORTED_REPORT_FORMATS):
            raise ParameterValidationError("formats must be a non-empty list")
        formats = [str(item).strip().casefold() for item in formats]
        if len(set(formats)) != len(formats) or any(item not in SUPPORTED_REPORT_FORMATS for item in formats):
            raise ParameterValidationError("formats contains an unsupported value")
        title = payload.get("title")
        audience = payload.get("audience", "供应链业务")
        instructions = payload.get("instructions", "")
        if title is not None and (not isinstance(title, str) or len(title) > 160):
            raise ParameterValidationError("title is invalid")
        if not isinstance(audience, str) or len(audience) > 120:
            raise ParameterValidationError("audience is invalid")
        if not isinstance(instructions, str) or len(instructions) > 2_000:
            raise ParameterValidationError("instructions is invalid")
        receipt = await asyncio.to_thread(
            store.find_request_receipt, session, conversation_id, request_id
        )
        source = receipt.public_payload() if receipt is not None else None
        if not isinstance(source, Mapping):
            raise HTTPException(status_code=404, detail="report source not found")
        try:
            tables = await asyncio.to_thread(
                normalize_report_tables,
                source,
                page_reader=lambda result_id, page: result_pages.page(
                    session.principal_hash, result_id, page
                ),
            )
            document = await asyncio.to_thread(
                build_report_document,
                source,
                tables,
                title=title,
                audience=audience,
                generated_at=datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S"),
            )
            narrative = None
            if isinstance(chat_runner, ChatRunner) and chat_runner.auth_configured:
                try:
                    model_context = report_model_context(document, report_request=instructions)
                    model_result = await asyncio.to_thread(
                        chat_runner.run,
                        "根据已校验查询结果生成业务报告",
                        report_context=model_context,
                    )
                    narrative = model_result.get("report_narrative") if isinstance(model_result, Mapping) else None
                except Exception as exc:  # The verified data report remains usable if prose generation is unavailable.
                    LOGGER.warning("report narrative generation failed: %s", type(exc).__name__)
            document = apply_model_narrative(document, narrative)
            report_id = "rpt_" + secrets.token_hex(16)
            files = []
            for format_name in formats:
                content, content_type, suffix = await asyncio.to_thread(
                    render_format, document, format_name
                )
                filename = re.sub(
                    r"[^\w\u4e00-\u9fff.-]+", "-",
                    (document.title or "doris-report"),
                    flags=re.UNICODE,
                ).strip("-.")[:80] or "doris-report"
                files.append(await asyncio.to_thread(
                    report_files.register,
                    content,
                    principal_hash=session.principal_hash,
                    conversation_id=conversation_id,
                    report_id=report_id,
                    format=format_name,
                    filename=f"{filename}{suffix}",
                    content_type=content_type,
                ))
        except ReportSourceError as exc:
            status_code = 409 if exc.code in {"empty_result", "report_source_incomplete", "report_source_not_live", "report_source_not_completed"} else 422
            return JSONResponse(status_code=status_code, content={"error": {"code": exc.code, "message": exc.message, "details": {}}})
        except ReportFormatError as exc:
            return JSONResponse(status_code=503, content={"error": {"code": "report_format_unavailable", "message": str(exc), "details": {}}})
        except Exception as exc:  # pragma: no cover - defensive rendering boundary
            LOGGER.warning("report generation failed: %s", type(exc).__name__)
            return JSONResponse(status_code=500, content={"error": {"code": "report_generation_failed", "message": "报告生成失败，请稍后重试", "details": {}}})
        response.headers["X-Request-ID"] = request_id
        _set_session_cookies(response, session, secure=cookie_secure)
        return {
            "status": "completed",
            "report_id": report_id,
            "title": document.title,
            "audience": document.audience,
            "table_count": len(document.tables),
            "row_count": sum(len(table.rows) for table in document.tables),
            "files": files,
        }

    @app.post("/v1/reports/center-warehouse-turnover")
    async def center_warehouse_turnover(
        request: Request,
        response: Response,
        payload: Any = Body(default=None),
        x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    ) -> Any:
        """Run the deterministic center/subwarehouse turnover image contract."""

        session = check_state_change_request(request, x_csrf_token)
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise ParameterValidationError("request body must be a JSON object")
        allowed = {"conversation_id", "end_date_exclusive", "profile_id", "window_days"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ParameterValidationError("Unknown report fields", details={"unknown": unknown})
        raw_conversation = payload.get("conversation_id")
        if raw_conversation is not None and (
            not isinstance(raw_conversation, str) or len(raw_conversation) > 100
        ):
            raise ParameterValidationError("conversation_id must be a short string")
        conversation_id = store.resolve(session, raw_conversation)
        profile_id = payload.get("profile_id", DEFAULT_PROFILE_ID)
        if not isinstance(profile_id, str) or len(profile_id) > 80 or not profile_id.strip():
            raise ParameterValidationError("profile_id must be a short string")
        window_days = payload.get("window_days", 7)
        if type(window_days) is not int or not 1 <= window_days <= 3650:
            raise ParameterValidationError("window_days must be an integer from 1 to 3650")
        end_date_exclusive = payload.get("end_date_exclusive")
        if end_date_exclusive is not None and (
            not isinstance(end_date_exclusive, str) or len(end_date_exclusive) > 32
        ):
            raise ParameterValidationError("end_date_exclusive must be a date string")
        idempotency_key = _idempotency_key_from_request(request)
        request_id = str(uuid4())
        receipt_claim = store.claim_request(
            session,
            conversation_id,
            route=REPORT_REQUEST_ROUTE,
            idempotency_key=idempotency_key,
            fingerprint=_request_fingerprint(
                {
                    "conversation_id": conversation_id,
                    "end_date_exclusive": end_date_exclusive,
                    "profile_id": profile_id.strip(),
                    "window_days": window_days,
                }
            ),
            request_id=request_id,
        )
        if not receipt_claim.owner:
            return _receipt_response(
                receipt_claim.receipt,
                session=session,
                store=store,
                secure_cookies=cookie_secure,
                response=response,
            )
        receipt = receipt_claim.receipt
        request_id = receipt.request_id
        context_lease_id: str | None = None
        conversation_lease_id: str | None = None
        heartbeat_task: asyncio.Task[Any] | None = None
        learning_recorded = False
        learning_question = "生成中心仓和分仓区间周转数据图片"
        response.headers["X-Request-ID"] = request_id
        try:
            context_lease_id = store.acquire_context(session)
            conversation_lease_id = store.acquire(session, conversation_id)
            heartbeat_task = asyncio.create_task(_keep_request_alive(
                store, session, conversation_id, receipt,
                conversation_lease_id, context_lease_id,
            ))
            result = await run_owned_thread(
                report_service.run,
                principal_hash=session.principal_hash,
                conversation_id=conversation_id,
                request_id=request_id,
                end_date_exclusive=end_date_exclusive,
                profile_id=profile_id.strip(),
                window_days=window_days,
            )
            artifact = _public_report_artifact(result.get("artifact") if isinstance(result, Mapping) else None)
            if artifact is None:
                raise TurnoverRuntimeError(
                    "report_render_failed",
                    "报表图片生成失败，未返回半成品",
                    reason="invalid_artifact",
                )
            context_state = store.public_context_state(store.context(session))
            _set_session_cookies(response, session, secure=cookie_secure)
            public_payload = {
                "request_id": request_id,
                "conversation_id": conversation_id,
                "status": "completed",
                "process": {
                    "state": "completed",
                    "steps": [
                        {"id": "context", "state": "reused", "label": "已继承本用户上下文"},
                        {"id": "query", "state": "completed", "label": "已完成 Doris 报表查询"},
                        {"id": "validation", "state": "completed", "label": "已完成数据质量校验"},
                        {"id": "render", "state": "completed", "label": "已生成受控数据图片"},
                    ],
                },
                "outcome": {"state": "completed", "label": "取数已完成"},
                "clarification": {"required": False, "questions": []},
                "context_state": context_state,
                "artifacts": [artifact],
            }
            store.complete_request(session, receipt, public_payload)
            learning_recorded = await record_learning_query(
                session=session,
                conversation_id=conversation_id,
                request_id=request_id,
                question=learning_question,
                outcome="completed",
                execution_status="completed",
                has_business_result=True,
                route_family="inventory_turnover",
            )
            return public_payload
        except TurnoverRuntimeError as exc:
            LOGGER.warning("turnover report blocked: code=%s reason=%s", exc.code, exc.reason)
            status_by_code = {
                "report_parameters_invalid": 422,
                "report_contract_not_released": 409,
                "report_quality_blocked": 409,
                "report_query_failed": 502,
                "report_render_failed": 500,
                "report_configuration_error": 500,
            }
            status_code = status_by_code.get(exc.code, 500)
            error_payload = {
                "error": {
                    "code": exc.code,
                    "message": exc.public_message,
                    "details": {},
                }
            }
            error_response = JSONResponse(
                status_code=status_code,
                content=error_payload,
            )
            try:
                store.fail_request(
                    session,
                    receipt,
                    error_payload,
                    response_status=status_code,
                )
            except Exception:
                LOGGER.warning("Unable to persist turnover error receipt", exc_info=True)
            if not learning_recorded:
                await record_learning_query(
                    session=session,
                    conversation_id=conversation_id,
                    request_id=request_id,
                    question=learning_question,
                    outcome=(
                        "blocked"
                        if exc.code
                        in {
                            "report_parameters_invalid",
                            "report_contract_not_released",
                            "report_quality_blocked",
                            "report_configuration_error",
                        }
                        else "failed"
                    ),
                    execution_status=exc.code,
                    has_business_result=False,
                    route_family="inventory_turnover",
                )
            error_response.headers["X-Request-ID"] = request_id
            _set_session_cookies(error_response, session, secure=cookie_secure)
            return error_response
        except RuntimeErrorBase as exc:
            if exc.code in {"conversation_busy", "clarification_pending"}:
                store.discard_request(session, receipt)
            status_code, error_payload = _runtime_error_payload(exc)
            error_response = JSONResponse(status_code=status_code, content=error_payload)
            if exc.code not in {"conversation_busy", "clarification_pending"}:
                try:
                    store.fail_request(
                        session,
                        receipt,
                        error_payload,
                        response_status=status_code,
                    )
                except Exception:
                    LOGGER.warning("Unable to persist turnover runtime error receipt", exc_info=True)
                if not learning_recorded:
                    await record_learning_query(
                        session=session,
                        conversation_id=conversation_id,
                        request_id=request_id,
                        question=learning_question,
                        outcome="blocked" if exc.code.endswith("blocked") else "failed",
                        execution_status=exc.code,
                        has_business_result=False,
                        route_family="inventory_turnover",
                    )
            error_response.headers["X-Request-ID"] = request_id
            _set_session_cookies(error_response, session, secure=cookie_secure)
            return error_response
        except Exception as exc:
            LOGGER.exception("Unexpected turnover request error: %s", type(exc).__name__)
            status_code, error_payload = _runtime_error_payload(exc)
            try:
                store.fail_request(
                    session,
                    receipt,
                    error_payload,
                    response_status=status_code,
                )
            except Exception:
                LOGGER.warning("Unable to persist unexpected turnover error receipt", exc_info=True)
            if not learning_recorded:
                await record_learning_query(
                    session=session,
                    conversation_id=conversation_id,
                    request_id=request_id,
                    question=learning_question,
                    outcome="failed",
                    execution_status="service_unavailable",
                    has_business_result=False,
                    route_family="inventory_turnover",
                )
            error_response = JSONResponse(status_code=status_code, content=error_payload)
            error_response.headers["X-Request-ID"] = request_id
            _set_session_cookies(error_response, session, secure=cookie_secure)
            return error_response
        finally:
            if conversation_lease_id:
                store.release(session, conversation_id, conversation_lease_id)
            if context_lease_id:
                store.release_context(session, context_lease_id)

    @app.post("/v1/clarification/resume")
    async def resume_clarification(
        request: Request,
        response: Response,
        payload: Any = Body(default=None),
        x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    ) -> dict[str, Any]:
        """Restore a pending choice card after a refresh or alias switch.

        Rotating a clarification token changes server state, so this is an
        explicit same-origin, CSRF-protected POST rather than a GET side effect.
        """

        session = check_state_change_request(request, x_csrf_token)
        if not isinstance(payload, dict):
            raise ParameterValidationError("request body must be a JSON object")
        allowed = {"conversation_id"}
        if set(payload) - allowed:
            raise ParameterValidationError("Unknown clarification fields")
        conversation_id = payload.get("conversation_id")
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise ParameterValidationError("conversation_id is required")
        conversation_id = store.assert_owned(session, conversation_id.strip())
        context_lease_id: str | None = None
        conversation_lease_id: str | None = None
        try:
            context_lease_id = store.acquire_context(session)
            conversation_lease_id = store.acquire(session, conversation_id)
            resumed = store.resume_clarification(session, conversation_id)
            _set_session_cookies(response, session, secure=cookie_secure)
            if resumed is None:
                return {
                    "status": "ready",
                    "conversation_id": conversation_id,
                    "clarification": {"required": False, "questions": []},
                    "context_state": store.public_context_state(store.context(session)),
                }
            return {
                "status": "needs_selection",
                "conversation_id": conversation_id,
                "clarification": {
                    "required": True,
                    "token": resumed["token"],
                    "draft_id": resumed["draft_id"],
                    "questions": resumed["questions"],
                },
                "context_state": store.public_context_state(store.context(session)),
            }
        finally:
            if conversation_lease_id:
                store.release(session, conversation_id, conversation_lease_id)
            if context_lease_id:
                store.release_context(session, context_lease_id)

    @app.post("/v1/conversations")
    async def create_conversation(
        request: Request,
        response: Response,
        x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    ) -> dict[str, Any]:
        session = check_state_change_request(request, x_csrf_token)
        conversation_id = store.create_conversation(session)
        _set_session_cookies(response, session, secure=cookie_secure)
        return {"status": "created", "conversation_id": conversation_id}

    @app.post("/v1/chat")
    async def chat(
        request: Request,
        response: Response,
        payload: Any = Body(default=None),
        x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    ) -> dict[str, Any]:
        session = check_state_change_request(request, x_csrf_token)
        if not isinstance(payload, dict):
            raise ParameterValidationError("request body must be a JSON object")
        allowed = {
            "question",
            "sql_conversion",
            "conversation_id",
            "execution_mode",
            "rule_id",
            "rule_params",
            "clarification_token",
            "clarification_answers",
            "report_kind",
            "report_profile_id",
            "report_end_date_exclusive",
            "window_days",
        }
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ParameterValidationError(
                "Unknown chat fields",
                details={"unknown": unknown},
            )
        raw_execution_mode = payload.get("execution_mode")
        # Existing API clients that submit a report contract remain compatible;
        # browser natural-language requests always send this field explicitly.
        execution_mode = raw_execution_mode or "natural_language"
        if execution_mode not in {"natural_language", "rule"}:
            raise ParameterValidationError("execution_mode 不受支持")
        rule_id = payload.get("rule_id")
        rule_params = payload.get("rule_params")
        compiled_rule: dict[str, Any] | None = None
        if execution_mode == "rule":
            try:
                compiled_rule = build_rule_request(
                    rule_id,
                    rule_params,
                    root=PROJECT_ROOT,
                    now=datetime.now(ZoneInfo("Asia/Shanghai")),
                )
            except (OSError, ValueError, KeyError) as exc:
                raise ParameterValidationError("规则条件无效，请重新选择") from exc
            question = compiled_rule["question"]
            if compiled_rule.get("report_kind"):
                if payload.get("report_kind") not in (None, compiled_rule["report_kind"]):
                    raise ParameterValidationError("规则报表类型不一致")
                report_kind = compiled_rule["report_kind"]
                report_profile_id = compiled_rule.get("report_profile_id")
                report_end_date_exclusive = compiled_rule.get("report_end_date_exclusive")
                window_days = compiled_rule.get("window_days", 7)
            else:
                report_kind = None
                report_profile_id = None
                report_end_date_exclusive = None
                window_days = None
        else:
            if rule_id is not None or rule_params is not None:
                raise ParameterValidationError("自然语言请求不能携带规则条件")
            question = payload.get("question")
        conversion_input = payload.get("sql_conversion")
        if "sql_conversion" in payload:
            if (not isinstance(conversion_input, str) or not conversion_input.strip()
                    or len(conversion_input) > MAX_CONVERSION_CHARS
                    or any(key in payload for key in ("clarification_token", "clarification_answers", "report_kind", "report_profile_id", "report_end_date_exclusive"))
                    or (question is not None and not is_conversion_request(question))):
                raise ParameterValidationError("转换材料格式无效，不能与取数或口径选择混合提交")
            question = question or CONVERSION_QUESTION
        submitted_question = question.strip() if isinstance(question, str) else ""
        clarification_token = payload.get("clarification_token")
        clarification_answers = payload.get("clarification_answers")
        clarification_submission = (
            clarification_token is not None or clarification_answers is not None
        )
        if execution_mode != "rule":
            # Natural language has one and only one route: the model-first
            # semantic planner. Legacy report hints are deliberately ignored
            # here so an old browser payload cannot re-enter the fixed report
            # executor or force a reviewed rule before the model decides.
            report_kind = None
            report_profile_id = None
            report_end_date_exclusive = None
            window_days = 7
        if execution_mode == "rule" and not is_standard_center_report(submitted_question):
            # Non-report explicit rules must not inherit a report contract from
            # a stale client payload.
            report_kind = None
            report_profile_id = None
        if execution_mode == "rule" and is_standard_center_report(submitted_question):
            report_kind = REPORT_KIND
            # The explicit rule already carries a selected, versioned report
            # profile. It must not reopen the natural-language ambiguity card.
            report_profile_id = report_profile_id or DEFAULT_PROFILE_ID
        if execution_mode == "rule":
            report_end_date_exclusive = report_end_date_exclusive or payload.get("report_end_date_exclusive")
        if window_days is None:
            window_days = report_window_days(submitted_question) or 7
        if type(window_days) is not int or not 1 <= window_days <= 3650:
            raise ParameterValidationError("window_days must be an integer from 1 to 3650")
        if (
            not clarification_submission
            and report_kind == REPORT_KIND
            and _is_business_window_ambiguous(submitted_question)
        ):
            # A caller-supplied profile cannot bypass the window-semantics
            # decision for the business-labelled fourteen-day wording.
            report_profile_id = None
        if report_kind is not None and report_kind != REPORT_KIND:
            raise ParameterValidationError("report_kind is not supported")
        if execution_mode == "rule" and report_kind is None and payload.get("report_profile_id") is not None:
            raise ParameterValidationError("report_profile_id requires report_kind")
        if report_profile_id is not None and (
            not isinstance(report_profile_id, str)
            or report_profile_id.strip() not in REPORT_PROFILE_IDS
        ):
            raise ParameterValidationError("report_profile_id is not supported")
        if isinstance(report_profile_id, str):
            report_profile_id = report_profile_id.strip()
        if report_end_date_exclusive is not None and (
            not isinstance(report_end_date_exclusive, str)
            or len(report_end_date_exclusive) > 32
        ):
            raise ParameterValidationError("report_end_date_exclusive must be a date string")
        if question is None and clarification_answers is not None:
            # The selected IDs are enough because the persisted Codex thread
            # already contains the prior clarification turn.
            question = "根据已选择的业务口径继续本次取数。"
        elif clarification_token is not None or clarification_answers is not None:
            # Never let free-form text accompany an option submission and alter
            # the persisted context; the token is the sole source of truth.
            question = "根据已选择的业务口径继续本次取数。"
        if not isinstance(question, str) or not question.strip():
            raise ParameterValidationError("question must be a non-empty string unless clarification answers are supplied")
        question = question.strip()
        if len(question) > MAX_QUESTION_LENGTH:
            raise ParameterValidationError(
                "question exceeds the configured length limit",
                details={"max_length": MAX_QUESTION_LENGTH},
            )
        conversation_id = payload.get("conversation_id")
        if conversation_id is not None:
            if not isinstance(conversation_id, str) or len(conversation_id) > 100:
                raise ParameterValidationError(
                    "conversation_id must be a short string"
                )
        resolved_conversation_id = store.resolve(session, conversation_id)
        idempotency_key = _idempotency_key_from_request(request)
        request_id = str(uuid4())
        receipt_claim = store.claim_request(
            session,
            resolved_conversation_id,
            route=CHAT_REQUEST_ROUTE,
            idempotency_key=idempotency_key,
            fingerprint=_request_fingerprint(
                {
                    "conversation_id": resolved_conversation_id,
                    "question": question,
                    "execution_mode": execution_mode,
                    "rule_id": rule_id,
                    "rule_params": rule_params,
                    **({"sql_conversion": conversion_input} if conversion_input is not None else {}),
                    "clarification_answers": clarification_answers,
                    "report_kind": report_kind,
                    "report_profile_id": report_profile_id,
                    "report_end_date_exclusive": report_end_date_exclusive,
                    "window_days": window_days,
                }
            ),
            request_id=request_id,
        )
        if not receipt_claim.owner:
            return _receipt_response(
                receipt_claim.receipt,
                session=session,
                store=store,
                secure_cookies=cookie_secure,
                response=response,
            )
        receipt = receipt_claim.receipt
        request_id = receipt.request_id
        learning_question = submitted_question or question
        if is_conversion_request(question):
            learning_question = CONVERSION_QUESTION
        selected_answers: list[dict[str, Any]] | None = None
        source_question_for_selection: str | None = None
        selection_reference_time: datetime | None = None
        selection_continuation = False
        report_profile_selection_required = False
        context_lease_id: str | None = None
        conversation_lease_id: str | None = None
        heartbeat_task: asyncio.Task[None] | None = None
        learning_recorded = False
        execution_message: str | None = None
        outbound_selection_unpublished = False
        report_window_semantics: str | None = None
        report_selection_blocked = False
        # Lock the shared principal context first. If a second UI alias is
        # active, it fails before consuming a one-time clarification token or
        # taking an alias-specific lease. A selected token therefore remains
        # retryable when the shared Codex thread is busy.
        try:
            context_lease_id = store.acquire_context(session)
            conversation_lease_id = store.acquire(session, resolved_conversation_id)
            # Recheck after taking the principal context lease. A clarification
            # can be created by a sibling alias while this request was between
            # its initial ownership check and the serialized runner section.
            if not clarification_submission and store.has_pending_clarification(
                session, resolved_conversation_id
            ):
                # A new explicit question starts a new branch. Do not force the
                # user to answer an abandoned card before asking an unrelated
                # question; the old token is scoped to its original wording
                # and is no longer actionable once this branch is replaced.
                store.discard_pending_clarifications(session, resolved_conversation_id)
            if clarification_answers is not None or clarification_token is not None:
                if not isinstance(clarification_token, str) or not clarification_token:
                    raise ParameterValidationError("clarification_token is required")
                source_question_for_selection = store.clarification_source_question(
                    session,
                    resolved_conversation_id,
                    clarification_token,
                )
                reference_at = store.clarification_reference_time(session, resolved_conversation_id, clarification_token)
                if reference_at is None:
                    from .chat_sessions import ClarificationValidationError
                    raise ClarificationValidationError("clarification has no original request time")
                selection_reference_time = datetime.fromtimestamp(reference_at, tz=ZoneInfo("UTC"))
                saved_report_parameters = store.clarification_report_parameters(
                    session, resolved_conversation_id, clarification_token,
                )
                selected_answers = store.validate_clarification_token(
                    session,
                    resolved_conversation_id,
                    clarification_token,
                    clarification_answers,
                    include_business_meaning=execution_mode != "rule",
                )
                selection_continuation = bool(selected_answers)
                # The token is server-issued and its question IDs are bound to
                # this principal/conversation. Recover the fixed report kind
                # from that contract when a thin client omits the redundant
                # ``report_kind`` field on the answer turn.
                if execution_mode == "rule" and report_kind is None and any(
                    isinstance(answer, Mapping)
                    and str(answer.get("question_id") or "").strip()
                    == "turnover_inventory_profile"
                    for answer in (selected_answers or [])
                ):
                    report_kind = REPORT_KIND
                if execution_mode == "rule" and report_kind == REPORT_KIND:
                    selected_profile = _report_profile_from_answers(selected_answers)
                    if selected_profile is None:
                        raise ParameterValidationError("report profile selection is required")
                    report_window_semantics = _report_window_semantics_from_answers(selected_answers)
                    if _is_business_window_ambiguous(source_question_for_selection or ""):
                        if report_window_semantics is None:
                            raise ParameterValidationError("report window semantics selection is required")
                        if report_window_semantics == "business_w37_source_window":
                            report_selection_blocked = True
                    # The server-issued clarification token is authoritative;
                    # ignore a client-supplied profile that disagrees with it.
                    report_profile_id = selected_profile
                    # Continuations use server-bound dates, never a new default
                    # window or a client override of the original request.
                    window_days = (saved_report_parameters["window_days"] if saved_report_parameters
                                   else report_window_days(source_question_for_selection or "") or 7)
                    report_end_date_exclusive = (
                        saved_report_parameters["end_date_exclusive"] if saved_report_parameters
                        else selection_reference_time.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
                    )
                if source_question_for_selection:
                    # Keep the original for learning/audit correlation, but let
                    # the resumed Codex thread supply the original user turn.
                    # Replaying its wording here can retrigger the same
                    # clarification branch in otherwise stateful runners.
                    learning_question = source_question_for_selection
            elif execution_mode == "rule" and report_kind == REPORT_KIND and report_profile_id is None:
                report_profile_selection_required = True
        except Exception:
            if conversation_lease_id:
                store.release(session, resolved_conversation_id, conversation_lease_id)
            if context_lease_id:
                store.release_context(session, context_lease_id)
            store.discard_request(session, receipt)
            raise
        try:
            # Every natural-language turn is owned by the model planner. Keep
            # only the non-query material conversion helper as a separate
            # explicit workflow; explanatory wording must not short-circuit a
            # data request before the normal context/model chain sees it.
            guidance = None
            if conversion_input is not None and is_conversion_request(question):
                guidance = await asyncio.to_thread(conversion_guidance, conversion_input,
                                                  converter=sql_converter or convert_active_request)
            if guidance is not None:
                context_state = store.public_context_state(store.context(session))
                guidance_label = "说明已完成，未查询数据"
                if guidance.get("kind") == "sql_conversion":
                    guidance_label = {
                        "needs_input": "待补充转换材料，未查询数据",
                        "converted": "转换检查完成，未查询数据",
                        "rejected": "转换未通过，未查询数据",
                    }[guidance["state"]]
                public_payload = {
                    "request_id": request_id,
                    "conversation_id": resolved_conversation_id,
                    "status": "completed",
                    "result_state": "explanation",
                    "process": {"state": "not_queried", "steps": []},
                    "outcome": {"state": "completed", "label": guidance_label},
                    "clarification": {"required": False, "questions": []},
                    "context_state": context_state,
                    "guidance": guidance,
                }
                store.complete_request(session, receipt, public_payload)
                learning_recorded = await record_learning_query(
                    session=session, conversation_id=resolved_conversation_id,
                    request_id=request_id, question=learning_question,
                    outcome="completed", execution_status="explanation", has_business_result=False,
                )
                response.headers["X-Request-ID"] = request_id
                _set_session_cookies(response, session, secure=cookie_secure)
                return public_payload
            if report_profile_selection_required:
                reference_at = getattr(request.state, "query_submitted_at", receipt.created_at)
                _, report_end = complete_window(
                    report_end_date_exclusive, window_days=window_days,
                    now=datetime.fromtimestamp(reference_at, tz=ZoneInfo("Asia/Shanghai")),
                )
                token = store.create_clarification_token(
                    session,
                    resolved_conversation_id,
                    _report_clarification_questions(
                        business_window_ambiguous=_is_business_window_ambiguous(learning_question)
                    ),
                    source_question=learning_question,
                    source_reference_at=reference_at,
                    report_parameters={"window_days": window_days, "end_date_exclusive": report_end.date().isoformat()},
                )
                context_state = store.public_context_state(store.context(session))
                public_payload = {
                    "request_id": request_id,
                    "conversation_id": resolved_conversation_id,
                    "status": "needs_selection",
                    "process": {
                        "state": "waiting_for_selection",
                        "steps": [
                            {"id": "context", "state": "reused", "label": "已继承本用户上下文"},
                            {"id": "clarify", "state": "pending", "label": "等待库存口径选择"},
                        ],
                    },
                    "outcome": {"state": "clarification_required", "label": "需要选择业务口径"},
                    "clarification": {
                        "required": True,
                        "token": token,
                        "questions": _report_clarification_questions(
                            business_window_ambiguous=_is_business_window_ambiguous(learning_question)
                        ),
                        "draft_id": store.clarification_draft_id(session, resolved_conversation_id, token),
                    },
                    "context_state": context_state,
                }
                store.complete_request(session, receipt, public_payload)
                learning_recorded = await record_learning_query(
                    session=session,
                    conversation_id=resolved_conversation_id,
                    request_id=request_id,
                    question=learning_question,
                    outcome="clarification_required",
                    execution_status="waiting_for_selection",
                    has_business_result=False,
                    route_family=(
                        "inventory_turnover"
                        if report_kind == REPORT_KIND
                        else None
                    ),
                )
                response.headers["X-Request-ID"] = request_id
                _set_session_cookies(response, session, secure=cookie_secure)
                return public_payload
            report_request = (
                report_kind == REPORT_KIND
                and report_profile_id in REPORT_PROFILE_IDS
                # Explicit rule submissions and an already-issued selectable
                # clarification may use the reviewed report executor. A plain
                # natural-language request must always go through the Codex
                # semantic planner, even if the browser sent a stale report
                # hint from an older UI state.
                and execution_mode == "rule"
            )
            if execution_mode == "rule":
                enforce_comparison_contract(source_question_for_selection or question, PROJECT_ROOT)
            heartbeat_task = asyncio.create_task(
                _keep_request_alive(
                    store,
                    session,
                    resolved_conversation_id,
                    receipt,
                    conversation_lease_id,
                    context_lease_id,
                )
            )
            # The actual query reports backend availability. A second health
            # probe here adds latency and cannot prove a business query works.
            report_progress("routing" if execution_mode == "rule" else "model")
            context_before = store.context(session)
            if execution_mode == "rule":
                context_home = None
                context_home_factory = getattr(chat_runner, "context_home_for_ref", None)
                if callable(context_home_factory):
                    context_home = context_home_factory(store.context_home_ref(session))
                runner_thread_id = context_before.codex_thread_id
                runner_turn_count = context_before.turn_count
                private_request_context = []
            else:
                # Natural language is intentionally stateless at the Codex
                # thread level. Only a bounded learning summary is loaded;
                # no previous request, transcript, thread, or context home is
                # allowed to influence this query.
                context_home = None
                runner_thread_id = None
                runner_turn_count = 0
                private_request_context = []
            if execution_mode != "rule" and learning_service is not None:
                try:
                    private_request_context = await asyncio.to_thread(
                        learning_service.prompt_context,
                        session.principal_hash,
                    )
                except Exception as exc:  # pragma: no cover - learning is auxiliary
                    LOGGER.warning("Unable to load learning summary: %s", type(exc).__name__)
            runner_kwargs: dict[str, Any] = {
                "thread_id": runner_thread_id,
                "context_turn_count": runner_turn_count,
                "ui_selections": selected_answers,
                "context_home": context_home,
                "request_context": private_request_context,
                "cancel_event": getattr(request.state, "cancel_event", None),
            }
            if execution_mode == "rule" and isinstance(chat_runner, ChatRunner):
                def persist_context_identity(thread_id):
                    bound = store.bind_context_thread(session, thread_id, status="ready")
                    if bound.codex_thread_id != thread_id:
                        raise ChatExecutionError("后台对话标识不一致，未切换对话", details={"reason": "context_identity_mismatch"})
                runner_kwargs["on_context_ready"] = persist_context_identity
            if source_question_for_selection:
                # The public request carries only opaque option IDs.  Give the
                # production runner the original user wording as an internal
                # continuation hint so its fallback path cannot lose the
                # question that the choices belong to.
                runner_kwargs["original_question"] = source_question_for_selection
            reviewed_result = False
            if (
                execution_mode == "rule"
                and compiled_rule is not None
                and agent_core is not None
                and getattr(getattr(agent_core, "catalog", None), "release_digest", None)
                != compiled_rule.get("release_digest")
            ):
                raise RuntimeErrorBase(
                    "规则版本已更新，请重新打开规则条件",
                    details={"reason": "knowledge_release_changed"},
                )
            if report_request:
                # This is a reviewed, fixed-shape workflow: the selected
                # profile already determines the SQL and output. Do not make
                # users wait for a second model turn (or let a gateway timeout
                # turn a deterministic report into a false stopped state).
                # Ordinary natural-language requests still use Codex below.
                result = {
                    "status": "completed",
                    "final_response": "已按已选择的报表口径直接执行",
                    # No model turn was started. Keep the existing Codex
                    # context untouched so turn/compaction counters describe
                    # real model work rather than this deterministic route.
                    "thread_id": None,
                    "context_reused": False,
                    "interaction": {"outcome": "completed", "questions": []},
                    "query_execution": {
                        "live_business_result_executed": False,
                        "status": "deterministic_report",
                    },
                    "mcp_calls": [],
                }
            elif execution_mode == "rule":
                # Explicit forms are already a complete semantic contract.
                # Execute the bound SemanticRequest directly; never turn it
                # back into prose and never send it through the model router.
                compiled_plan = compiled_rule.get("plan") if compiled_rule else None
                direct_dispatch = getattr(agent_core, "execute_plan", None) if agent_core is not None else None
                if compiled_plan is None or not callable(direct_dispatch):
                    result = _selected_continuation_blocked_result(
                        context_reused=bool(context_before.codex_thread_id),
                        reason="规则执行器尚未就绪，本次未调用模型或替换规则",
                    )
                else:
                    result = await run_owned_thread(
                        direct_dispatch,
                        compiled_plan,
                        question=question,
                        context_reused=bool(context_before.codex_thread_id),
                    )
                reviewed_result = result is not None
            else:
                # Natural-language requests are model-first. The model owns
                # intent parsing, scope completion, clarification, and the
                # private query plan; the host only executes the returned plan
                # through the governed Doris bridge and renders verified data.
                # The deterministic semantic runtime remains available for
                # explicit rule-mode requests and regression tests, but it
                # must not preempt the model for free-form questions.
                result = await run_owned_thread(
                    _invoke_runner, chat_runner,
                    source_question_for_selection or question, runner_kwargs,
                )
                selected_reviewed_route = (
                    result.get("reviewed_route")
                    if isinstance(result, Mapping)
                    else None
                )
                if (
                    selected_reviewed_route
                    and isinstance(result, Mapping)
                    and result.get("normalized_result_state") == "truncated_result"
                    and agent_core is not None
                    and callable(getattr(agent_core, "run_if_supported", None))
                ):
                    # The model may explicitly select a released, bounded
                    # execution capability. Use it only as recovery after the
                    # model's own SQL hit the controlled capacity; never infer
                    # a route from the user's wording in this branch.
                    try:
                        recovered = await run_owned_thread(
                            agent_core.run_if_supported,
                            source_question_for_selection or question,
                            context_reused=False,
                        )
                        recovered_route = (
                            recovered.get("query_plan", {}).get("route")
                            if isinstance(recovered, Mapping)
                            else None
                        )
                        if recovered_route != selected_reviewed_route:
                            raise ChatExecutionError(
                                "模型选择的受控执行能力与实际计划不一致",
                                details={"reason": "reviewed_route_mismatch"},
                            )
                        result = recovered
                        reviewed_result = True
                    except Exception as exc:  # Keep the original truthful truncation state.
                        LOGGER.warning(
                            "Reviewed capacity recovery unavailable: route=%s error_type=%s",
                            selected_reviewed_route,
                            type(exc).__name__,
                        )
            thread_id = result.get("thread_id")
            if execution_mode == "natural_language":
                context_state = {
                    "scope": "request",
                    "status": "fresh",
                    "turn_count": 0,
                    "compaction_count": 0,
                    "compaction_pending": False,
                    "context_revision": 0,
                    "has_context": False,
                    "query_context_mode": "fresh_per_request",
                    "learning_summary_only": True,
                    "learning_summary_available": bool(private_request_context),
                }
            elif isinstance(thread_id, str) and thread_id:
                context_after_bind = store.bind_context_thread(
                    session,
                    thread_id,
                    replace=False,
                    status="ready",
                )
                if context_after_bind.codex_thread_id != thread_id:
                    raise ChatExecutionError("后台对话标识不一致，未切换对话", details={"reason": "context_identity_mismatch"})
                compaction_result = result.get("context_compaction")
                compaction_status = (
                    str(compaction_result.get("status") or "").casefold()
                    if isinstance(compaction_result, Mapping)
                    else ""
                )
                context_compaction_status = {
                    "requested": "compacting",
                    "unsupported": "compact_unsupported",
                    "failed": "error",
                }.get(compaction_status, "ready")
                context_after_turn = store.record_context_turn(
                    session,
                    thread_id,
                    compaction_status=context_compaction_status,
                )
                # ``thread.compact()`` is asynchronous. Count only a confirmed
                # completed notification, never a request acceptance.
                if compaction_status == "completed":
                    context_after_turn = store.record_context_compaction(
                        session,
                        thread_id,
                        status="ready",
                    )
                context_state = store.public_context_state(context_after_turn)
            else:
                context_state = store.public_context_state(store.context(session))
            report_progress("validation")
            if execution_mode == "natural_language":
                # This is the sole natural-language terminal path. It returns
                # before all legacy report/route-specific post-processing.
                public_payload = present_model_answer(
                    result, request_id=request_id, conversation_id=resolved_conversation_id,
                    context_state=context_state,
                    project_results=lambda value: _project_model_results(
                        value, pages_store=result_pages, owner=session.principal_hash),
                )
                if public_payload["clarification"]["required"]:
                    token = store.create_clarification_token(
                        session, resolved_conversation_id, public_payload["clarification"]["questions"],
                        source_question=source_question_for_selection or question,
                        source_reference_at=(selection_reference_time.timestamp() if selection_reference_time
                            else getattr(request.state, "query_submitted_at", receipt.created_at)),
                    )
                    public_payload["clarification"].update(token=token,
                        draft_id=store.clarification_draft_id(session, resolved_conversation_id, token))
                public_status = public_payload["status"]
                store.complete_request(session, receipt, public_payload)
                learning_recorded = await record_learning_query(
                    session=session, conversation_id=resolved_conversation_id, request_id=request_id,
                    question=learning_question, outcome=public_status,
                    execution_status=public_payload["query_status"],
                    has_business_result=bool(public_payload.get("business_tables")), route_family=None,
                )
                response.headers["X-Request-ID"] = request_id
                _set_session_cookies(response, session, secure=cookie_secure)
                return public_payload
            if isinstance(result.get("query_error"), Mapping):
                raise backend_error(502, result["query_error"])
            interaction = _safe_interaction(result)
            if (
                selection_continuation
                and execution_mode == "rule"
                and not report_request
                and not _result_has_live_business_evidence(result)
                and not (
                    isinstance(interaction, Mapping)
                    and interaction.get("outcome") == "clarification_required"
                    and isinstance(interaction.get("questions"), list)
                    and interaction.get("questions")
                )
            ):
                # Only explicit rules use these rule-specific failure reasons.
                # Natural-language choices return to the model without having
                # their meaning reclassified by this legacy rule executor.
                execution_message = (
                    "已收到本次口径选择，但当前组合未形成可确认的实时结果，请重新提交完整问题"
                )
                from .agent_core.outbound_clarification import selected_outbound_block_reason
                selection_reason = selected_outbound_block_reason(
                    source_question_for_selection, selected_answers
                )
                from .agent_core.turnover_conflict import selected_turnover_conflict_reason
                selection_reason = selection_reason or selected_turnover_conflict_reason(
                    source_question_for_selection, selected_answers
                )
                from .agent_core.order_return_planning import return_block_reason
                from .agent_core.so07_planning import so07_block_reason
                from .knowledge_activation import active_snapshot
                selection_reason = selection_reason or return_block_reason(
                    source_question_for_selection, contract_root=active_snapshot(PROJECT_ROOT)[0],
                    selected=selected_answers,
                )
                selection_reason = selection_reason or so07_block_reason(
                    source_question_for_selection, contract_root=active_snapshot(PROJECT_ROOT)[0], selected=selected_answers)
                if selection_reason:
                    execution_message = selection_reason
                    outbound_selection_unpublished = True
                interaction = {"outcome": "blocked", "questions": []}
                result = dict(result)
                result["interaction"] = interaction
                result["final_response"] = json.dumps(interaction, ensure_ascii=False)
            report_artifact: dict[str, Any] | None = None
            # The deterministic Doris runtime owns the numbers for a reviewed
            # report profile. In particular, a generic model follow-up such as
            # output-grain selection must not turn a fixed report into a second
            # browser clarification card.
            if report_selection_blocked:
                # The workbook reference is intentionally a candidate-only
                # reconciliation contract. Never query live Doris while
                # presenting it as the real fourteen-day report.
                execution_message = (
                    "W37业务表复现口径暂未发布：业务表标题写近14天，但销售源数据实际为7天；"
                    "当前未查询数仓，也未生成线上14日结果。"
                )
                interaction = {"outcome": "blocked", "questions": []}
                result = _selected_continuation_blocked_result(
                    context_reused=bool(context_before.codex_thread_id),
                    reason="business_w37_source_window_candidate_not_released",
                )
                report_request = False
            elif report_request:
                try:
                    report_result = await run_owned_thread(
                        report_service.run,
                        principal_hash=session.principal_hash,
                        conversation_id=resolved_conversation_id,
                        request_id=request_id,
                        end_date_exclusive=report_end_date_exclusive,
                        profile_id=str(report_profile_id),
                        window_days=window_days,
                    )
                    report_artifact = _public_report_artifact(
                        report_result.get("artifact") if isinstance(report_result, Mapping) else None
                    )
                    if report_artifact is None:
                        raise TurnoverRuntimeError(
                            "report_render_failed",
                            "报表图片生成失败，未返回半成品",
                            reason="invalid_artifact",
                        )
                    # Do not expose model-authored clarification text after a
                    # governed report has completed successfully.
                    interaction = {"outcome": "completed", "questions": []}
                    result["business_table"] = report_result.get("business_table")
                    if isinstance(report_result.get("business_tables"), list):
                        result["business_tables"] = report_result["business_tables"]
                except DorisResultError:
                    # The shared error boundary persists a retryable receipt
                    # and retains timeout/rejection/truncation distinctions.
                    raise
                except TurnoverRuntimeError as exc:
                    LOGGER.warning(
                        "turnover report failed during chat: code=%s reason=%s",
                        exc.code,
                        exc.reason,
                    )
                    execution_message = PUBLIC_EXECUTION_MESSAGES.get(
                        exc.code,
                        "数据图片生成失败，请稍后重试",
                    )
                    interaction = {"outcome": "blocked", "questions": []}
                except Exception as exc:  # pragma: no cover - defensive boundary
                    LOGGER.exception("turnover report failed during chat: %s", type(exc).__name__)
                    execution_message = "数据图片生成失败，请稍后重试"
                    interaction = {"outcome": "blocked", "questions": []}

            clarification_required = interaction["outcome"] == "clarification_required"
            interaction_failed = interaction["outcome"] == "failed"
            interaction_blocked = interaction["outcome"] == "blocked"
            if interaction_blocked and execution_message is None:
                execution_message = "本次未形成可确认的实时数据结果，请重新提交"
            elif interaction_failed and execution_message is None:
                execution_message = "本次取数未完成，请稍后重试"
            clarification_public: dict[str, Any] | None = None
            if clarification_required:
                questions = interaction.get("questions", [])
                token = store.create_clarification_token(
                    session,
                    resolved_conversation_id,
                    questions,
                    source_question=question,
                    source_reference_at=getattr(request.state, "query_submitted_at", receipt.created_at),
                )
                clarification_public = {
                    "required": True,
                    "token": token,
                    "questions": questions,
                    "draft_id": store.clarification_draft_id(session, resolved_conversation_id, token),
                }
            else:
                clarification_public = {"required": False, "questions": []}
            evidence = result.get("query_execution")
            live = bool(
                isinstance(evidence, Mapping)
                and evidence.get(
                    "live_business_result_executed",
                    evidence.get("live_query_executed", False),
                )
            )
            report_completed = report_artifact is not None
            if (
                live
                and not report_completed
                and _answer_contains_unapproved_contract_marker(interaction.get("answer"))
            ):
                raise UnapprovedContractResultError(
                    "当前返回涉及未审批业务口径，未展示结果"
                )
            business_result = _public_business_result(interaction, live=live)
            model_table_ready = False
            business_table = _public_business_table(
                result.get("business_table"),
                live=live or report_completed,
            )
            if business_table is None and "verified_business_results" not in result and live and not result.get("query_error") and isinstance(result.get("verified_business_result"), Mapping):
                verified_result = result["verified_business_result"]
                raw_verified = {
                    "format": "table", "table_id": "model_verified",
                    "title": "查询结果", "caption": "宿主已校验的实时结果",
                    "columns": verified_result.get("columns"), "rows": verified_result.get("rows"),
                    **({"result_columns": verified_result["result_columns"]} if "result_columns" in verified_result else {}),
                }
                requested_labels = _requested_model_business_labels(question)
                business_table = _public_business_table(raw_verified, live=True, requested_labels=requested_labels)
                raw_rows = raw_verified.get("rows")
                raw_columns = raw_verified.get("columns")
                if business_table is None and isinstance(raw_rows, list) and isinstance(raw_columns, list) and len(raw_rows) <= 100000:
                    numeric_columns = _model_verified_numeric_columns(raw_columns, raw_rows)
                    pages = []
                    for offset in range(0, max(1, len(raw_rows)), 100):
                        page = _public_business_table({**raw_verified, "rows": raw_rows[offset:offset + 100], "numeric_columns": numeric_columns}, live=True, requested_labels=requested_labels)
                        if page is None:
                            raise ChatExecutionError("查询结果未通过展示校验，未展示部分结果")
                        pages.append(page)
                    business_table = result_pages.create(session.principal_hash, pages) if len(pages) > 1 else pages[0]
                model_table_ready = business_table is not None
            raw_table = result.get("business_table")
            aggregate_route = result.get("query_plan", {}).get("route")
            if (live and reviewed_result and isinstance(raw_table, dict)
            and aggregate_route in {"sales_aggregate", "sales_analysis", "sales_composition", "realtime_turnover_by_warehouse"}
                    and business_table is None):
                # Aggregate routes can legitimately produce more than the
                # single-response screening limit. Screen each bounded page
                # separately, while the route handler has already reconciled
                # the complete source result before this split.
                raw_rows = raw_table.get("rows")
                if not isinstance(raw_rows, list) or len(raw_rows) > 100000:
                    raise ChatExecutionError("聚合结果超过完整展示上限，未展示部分结果")
                pages = []
                for offset in range(0, max(1, len(raw_rows)), 100):
                    page = _public_business_table({**raw_table, "rows": raw_rows[offset:offset + 100]}, live=True)
                    if page is None:
                        raise ChatExecutionError("聚合结果未通过展示校验，未展示部分结果")
                    pages.append(page)
                business_table = result_pages.create(session.principal_hash, pages) if len(pages) > 1 else pages[0]
            so07_route = result.get("query_plan", {}).get("route") == "so07_order_details"
            so07_table = isinstance(raw_table, dict) and raw_table.get("table_id") == "so07_order_details"
            if so07_route or so07_table:
                if (not so07_route or not so07_table or not reviewed_result or not live
                        or raw_table.get("complete_reconciled") is not True):
                    raise ChatExecutionError("社团订单缺少完整对账依据，暂未展示")
            detail_summary_only = False
            detail_route = result.get("query_plan", {}).get("route") == "sales_outbound_details"
            if detail_route or (isinstance(raw_table, dict) and raw_table.get("table_id") == "sales_outbound_details"):
                checked = getattr(result, "sales_detail_result", None)
                if (not detail_route or not reviewed_result or not live or not isinstance(checked, PartitionedSalesDetails)
                        or not isinstance(raw_table, dict)):
                    raise ChatExecutionError("销售明细缺少完整对账依据，暂未展示")
                if checked.complete:
                    if (raw_table.get("table_id") != "sales_outbound_details" or raw_table.get("complete_reconciled") is not True
                            or not isinstance(raw_table.get("rows"), list)
                            or len(raw_table["rows"]) != checked.summary.detail_count or len(raw_table["rows"]) > 100000):
                        raise ChatExecutionError("销售明细与完整范围行数不一致，暂未展示")
                else:
                    expected = [{"quantity": format(checked.summary.quantity, "f"), "outbound_count": str(checked.summary.document_count)}]
                    if raw_table.get("table_id") != "sales_aggregate" or raw_table.get("rows") != expected or checked.rows is not None:
                        raise ChatExecutionError("范围汇总与查询依据不一致，暂未展示")
                    detail_summary_only = True
                    cursor = getattr(result, 'sales_detail_cursor', None)
                    if (type(cursor) is not SalesDetailCursor or cursor.baseline != checked.summary
                            or cursor.loaded or cursor.plan.scope.as_dict() != result['query_plan']['scope']
                            or cursor.plan.start.isoformat() != result['query_plan']['start']
                            or cursor.plan.end.isoformat() != result['query_plan']['end'] or business_table is None):
                        raise ChatExecutionError('分批查询缺少原范围依据，暂未展示')
                    business_table['detail_batches'] = detail_contexts.create(
                        session.principal_hash, resolved_conversation_id, cursor)
                    if cursor.baseline.detail_count <= MAX_EXPORT_ROWS:
                        business_table['detail_batches']['export_id'] = sales_exports.create(
                            session.principal_hash, resolved_conversation_id,
                            business_table['detail_batches']['result_id'], cursor)
            business_tables = None
            if report_completed and isinstance(result.get("business_tables"), list):
                business_tables = []
                for raw_report_table in result["business_tables"]:
                    public_report_table = _public_business_table(raw_report_table, live=True)
                    if public_report_table is None:
                        raise ChatExecutionError("周转报表明细未通过展示校验，未展示部分结果")
                    business_tables.append(public_report_table)
                if not business_tables:
                    raise ChatExecutionError("周转报表未返回可展示表格")
                business_table = business_tables[0]
            multi_table_route = result.get("query_plan", {}).get("route")
            if "verified_business_results" in result:
                if (not live or result.get("query_error") or interaction.get("outcome") != "completed"
                        or reviewed_result or "business_tables" in result or raw_table is not None):
                    raise ChatExecutionError("多表查询尚未完成，未展示部分结果")
                business_tables = _public_model_result_tables(result["verified_business_results"],
                    pages_store=result_pages, owner=session.principal_hash)
                business_table = business_tables[0]
                manifest = [{"result_id": raw["result_id"], "row_count": len(raw["rows"])}
                            for raw in result["verified_business_results"]]
                try:
                    references = normalize_answer_references(result.get("answer_references"), manifest)
                except ValueError:
                    references = normalize_answer_references(None, manifest)
                business_result = public_result_summary(business_tables, manifest, references,
                    page_reader=lambda identifier, page: result_pages.page(session.principal_hash, identifier, page))
            elif multi_table_route in {"dms_points_orders", "sales_order_net_quantity", "warehouse_type_summary_detail"} or ("business_tables" in result and not report_completed):
                if multi_table_route == "warehouse_type_summary_detail":
                    raw_tables = result.get("business_tables")
                    if not live or not isinstance(raw_tables, list) or len(raw_tables) != 2:
                        raise ChatExecutionError("仓库汇总和清单未完成，未展示部分结果")
                    business_tables = []
                    for raw in raw_tables:
                        business_tables.append(_public_paged_business_table(
                            raw,
                            pages_store=result_pages,
                            owner=session.principal_hash,
                            live=True,
                        ))
                    business_table = business_tables[0]
                    business_result = public_result_summary(
                        business_tables,
                        [
                            {"result_id": raw.get("table_id"), "row_count": len(raw.get("rows", []))}
                            for raw in raw_tables
                        ],
                    )
                elif multi_table_route not in {"dms_points_orders", "sales_order_net_quantity"}:
                    raise ChatExecutionError("当前结果不支持多表展示")
                else:
                    screen_tables = _public_dms_order_tables if multi_table_route == "dms_points_orders" else _public_order_return_tables
                    business_tables = screen_tables(result.get("business_tables"),
                        reviewed=reviewed_result, live=live, pages_store=result_pages, owner=session.principal_hash)
                    business_table = business_tables[0]
            if (live and isinstance(raw_table, dict)
                    and result.get("query_plan", {}).get("route") in {"sales_outbound_details", "so07_order_details", "purchase_received_detail", "inventory_occupation_split", "purchase_received_by_production_date", "purchase_received_by_warehouse_production_date", "sales_outbound_boxes", "sales_outbound_boxes_channel", "dms_manager_orders"}
                    and raw_table.get("complete_reconciled") is True):
                if not reviewed_result:
                    raise ChatExecutionError("分页结果缺少完整对账依据，暂未展示")
                raw_rows = raw_table.get("rows")
                if not isinstance(raw_rows, list):
                    raise ChatExecutionError("明细结果格式不完整")
                pages = []
                for offset in range(0, max(1, len(raw_rows)), 100):
                    page = _public_business_table({**raw_table, "rows": raw_rows[offset:offset + 100]}, live=True)
                    if page is None:
                        raise ChatExecutionError("明细结果未通过展示校验，未展示部分结果")
                    pages.append(page)
                business_table = result_pages.create(session.principal_hash, pages) if len(pages) > 1 else pages[0]
            if model_table_ready and not reviewed_result and interaction.get("outcome") == "completed":
                count = business_table.get("pagination", {}).get("total_rows", len(business_table["rows"]))
                business_result = public_result_summary([business_table], [{"result_id": "result", "row_count": count}])
            public_has_table = business_table is not None or bool(business_tables)
            if (reviewed_result and isinstance(result, SemanticResult)
                    and public_has_table and business_result is None
                    and interaction.get("outcome") == "completed"):
                tables = business_tables or [business_table]
                manifest = [
                    {"result_id": f"result_{index}", "row_count": table.get("pagination", {}).get("total_rows", len(table["rows"]))}
                    for index, table in enumerate(tables)
                ]
                business_result = public_result_summary(tables, manifest)
            if (live and not report_completed
                    and result.get("result_state") != "no_data"
                    and not public_has_table):
                raise ChatExecutionError(
                    "取数已执行，但业务结果未通过表格校验，未展示数据",
                    details={"reason": "business_table_unavailable"},
                )
            if result.get("query_plan", {}).get("route") == "sales_ranked_drilldown":
                snapshot = getattr(result, "ranking_snapshot", None)
                if (not reviewed_result or not live or not isinstance(result, SemanticResult)
                        or not isinstance(snapshot, SalesRankingSnapshot) or business_table is None
                        or len(business_table["rows"]) != len(snapshot.ranking)):
                    raise ChatExecutionError("排名缺少完整校验依据，暂未展示")
                expected = [(row.sku_code, row.quantity, row.outbound_count, row.rank) for row in snapshot.ranking]
                displayed = [(row["sku_code"], Decimal(row["quantity"]), int(row["outbound_count"]), int(row["rank"]))
                             for row in business_table["rows"]]
                if displayed != expected:
                    raise ChatExecutionError("排名展示与已校验结果不一致，暂未展示")
                if snapshot.ranking:
                    business_table["drilldown"] = ranking_contexts.create(session.principal_hash, snapshot)
            live = live or report_completed
            raw_normalized_result_state = str(
                (evidence.get("normalized_result_state") if isinstance(evidence, Mapping) else None)
                or result.get("normalized_result_state")
                or (
                    result.get("query_error", {}).get("normalized_result_state")
                    if isinstance(result.get("query_error"), Mapping)
                    else None
                )
                or (
                    result.get("query_error", {}).get("normalized_state")
                    if isinstance(result.get("query_error"), Mapping)
                    else None
                )
                or ""
            ).casefold()
            has_normalized_result = raw_normalized_result_state in NORMALIZED_RESULT_STATES
            normalized_result_state = raw_normalized_result_state if has_normalized_result else (
                "clarification_required"
                if clarification_required
                else "empty_result"
                if live and result.get("result_state") == "no_data"
                else "succeeded"
                if live
                else "query_failed"
                if interaction_failed or isinstance(result.get("query_error"), Mapping)
                else "not_executed"
            )
            public_outcome = _public_outcome(interaction, live=live)
            if has_normalized_result and normalized_result_state in {
                "query_rejected", "query_failed", "query_unavailable",
                "truncated_result", "invalid_result",
            }:
                public_outcome = {
                    "state": "failed",
                    "label": {
                        "query_rejected": "查询未执行",
                        "query_failed": "查询执行失败",
                        "query_unavailable": "查询服务不可用",
                        "truncated_result": "查询结果不完整",
                        "invalid_result": "查询结果解析失败",
                    }[normalized_result_state],
                }
            public_status = (
                "needs_selection"
                if clarification_required
                else public_outcome["state"]
                if public_outcome["state"] in {"completed", "failed"}
                else "blocked"
            )
            if public_status != "completed":
                business_table = business_tables = business_result = None
            public_payload = {
                "request_id": request_id,
                "conversation_id": resolved_conversation_id,
                "status": public_status,
                "query_status": normalized_result_state,
                "normalized_result_state": normalized_result_state,
                "result_state": (
                    "summary_only" if public_status == "completed" and detail_summary_only
                    else "no_data" if public_status == "completed" and result.get("result_state") == "no_data"
                    else "live_success" if public_status == "completed"
                    else "clarification_required" if clarification_required
                    else "failed"
                ),
                "process": _public_process(
                    result,
                    context_state=context_state,
                    clarification_required=clarification_required,
                    interaction_blocked=interaction_blocked,
                    interaction_failed=interaction_failed,
                    report_completed=report_completed,
                ),
                "outcome": public_outcome,
                "clarification": clarification_public,
                "context_state": context_state,
            }
            if execution_message:
                public_payload["message"] = execution_message
            elif normalized_result_state == "empty_result":
                public_payload["message"] = "查询成功，但当前条件下没有符合条件的数据。"
            elif normalized_result_state == "query_rejected":
                public_payload["message"] = "查询未执行，原因是后端安全校验拒绝了该 SQL。"
            elif normalized_result_state == "query_unavailable":
                public_payload["message"] = "查询服务暂时不可用，请稍后重试。"
            elif normalized_result_state == "truncated_result":
                public_payload["message"] = "查询结果被截断，当前结果不完整，未作为完整结果展示。"
            elif normalized_result_state == "invalid_result":
                public_payload["message"] = "Doris 查询已完成，但 Agent 未能解析返回结果，请稍后重试。"
            elif normalized_result_state == "query_failed":
                public_payload["message"] = "查询执行失败，请稍后重试。"
            if outbound_selection_unpublished:
                public_payload["result_state"] = "business_definition_unpublished"
            if report_artifact is not None:
                public_payload["artifacts"] = [report_artifact]
            if business_result is not None:
                public_payload["business_result"] = business_result
            if business_table is not None:
                public_payload["business_table"] = business_table
            if business_tables is not None:
                public_payload["business_tables"] = business_tables
            store.complete_request(session, receipt, public_payload)
            learning_recorded = await record_learning_query(
                session=session,
                conversation_id=resolved_conversation_id,
                request_id=request_id,
                question=learning_question,
                outcome=(
                    "clarification_required"
                    if public_status == "needs_selection"
                    else public_status
                ),
                execution_status=str(public_payload.get("process", {}).get("state") or public_status),
                has_business_result=business_result is not None,
                route_family=("inventory_turnover" if report_request else None),
            )
            response.headers["X-Request-ID"] = request_id
            _set_session_cookies(response, session, secure=cookie_secure)
            return public_payload
        except RuntimeErrorBase as exc:
            if exc.code in {"conversation_busy", "clarification_pending"}:
                store.discard_request(session, receipt)
            status_code, error_payload = _runtime_error_payload(exc)
            if exc.code not in {"conversation_busy", "clarification_pending"}:
                try:
                    store.fail_request(
                        session,
                        receipt,
                        error_payload,
                        response_status=status_code,
                    )
                except Exception:
                    LOGGER.warning("Unable to persist chat error receipt", exc_info=True)
                if not learning_recorded:
                    await record_learning_query(
                        session=session,
                        conversation_id=resolved_conversation_id,
                        request_id=request_id,
                        question=learning_question,
                        outcome="blocked" if status_code < 500 else "failed",
                        execution_status=(
                            str(error_payload.get("error", {}).get("code") or exc.code)
                            if isinstance(error_payload.get("error"), Mapping)
                            else exc.code
                        ),
                        has_business_result=False,
                        route_family=(
                            "inventory_turnover"
                            if report_kind == REPORT_KIND
                            else None
                        ),
                    )
            error_response = JSONResponse(status_code=status_code, content=error_payload)
            error_response.headers["X-Request-ID"] = request_id
            _set_session_cookies(error_response, session, secure=cookie_secure)
            return error_response
        except Exception as exc:
            LOGGER.exception("Unexpected chat request error: %s", type(exc).__name__)
            status_code, error_payload = _runtime_error_payload(exc)
            try:
                store.fail_request(
                    session,
                    receipt,
                    error_payload,
                    response_status=status_code,
                )
            except Exception:
                LOGGER.warning("Unable to persist unexpected chat error receipt", exc_info=True)
            if not learning_recorded:
                await record_learning_query(
                    session=session,
                    conversation_id=resolved_conversation_id,
                    request_id=request_id,
                    question=learning_question,
                    outcome="failed",
                    execution_status="service_unavailable",
                    has_business_result=False,
                    route_family=(
                        "inventory_turnover"
                        if report_kind == REPORT_KIND
                        else None
                    ),
                )
            error_response = JSONResponse(status_code=status_code, content=error_payload)
            error_response.headers["X-Request-ID"] = request_id
            _set_session_cookies(error_response, session, secure=cookie_secure)
            return error_response
        finally:
            if heartbeat_task:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass
            if context_lease_id:
                store.release_context(session, context_lease_id)
            if conversation_lease_id:
                store.release(session, resolved_conversation_id, conversation_lease_id)

    async def execute_drilldown(session, conversation, selection, task_id):
        snapshot = ranking_contexts.get(session.principal_hash, selection["result_id"], selection["sku_code"])
        if semantic_service is None or not callable(getattr(semantic_service, "execute_ranked_sku", None)):
            raise ChatExecutionError("明细查询服务暂不可用")
        result = await run_owned_thread(semantic_service.execute_ranked_sku, snapshot, selection["sku_code"])
        raw = result.get("business_table") if isinstance(result, SemanticResult) else None
        if (not isinstance(raw, dict) or raw.get("table_id") != "sales_ranked_sku_detail"
                or raw.get("complete_reconciled") is not True
                or not result.get("query_execution", {}).get("live_business_result_executed")):
            raise ChatExecutionError("明细缺少完整对账依据，暂未展示")
        rows = raw.get("rows")
        if not isinstance(rows, list) or not 1 <= len(rows) <= 100000:
            raise ChatExecutionError("明细结果超出完整展示范围")
        pages = []
        for offset in range(0, len(rows), 100):
            page = _public_business_table({**raw, "rows": rows[offset:offset + 100]}, live=True)
            if page is None:
                raise ChatExecutionError("明细结果未通过展示校验，未展示部分结果")
            pages.append(page)
        table = result_pages.create(session.principal_hash, pages) if len(pages) > 1 else pages[0]
        context_state = store.public_context_state(store.context(session))
        return {
            "request_id": task_id, "conversation_id": conversation,
            "status": "completed", "result_state": "live_success",
            "business_table": table, "drilldown_source": dict(selection),
            "business_result": {"format": "plain_text", "text": "该SKU完整明细已与原排名对账。"},
            "clarification": {"required": False, "questions": []},
            "outcome": {"state": "completed", "label": "明细对账完成"},
            "context_state": context_state,
            "process": _public_process(result, context_state=context_state, clarification_required=False),
        }

    async def execute_detail_batch(session, conversation, selection, task_id):
        cursor, prior_task = detail_contexts.lookup(session.principal_hash, conversation,
            selection['result_id'], selection['batch_index'])
        if prior_task:
            prior = task_store.get(session.principal_hash, prior_task)
            if prior['state'] != 'completed' or not isinstance(prior.get('result'), dict):
                raise DetailContextError('上一批回执尚未就绪，请稍后重试')
            return prior['result']
        if semantic_service is None or not callable(getattr(semantic_service, 'execute_sales_detail_batch', None)):
            raise ChatExecutionError('分批查询服务暂不可用')
        context_lease = store.acquire_context(session)
        conversation_lease = None
        heartbeat = None
        try:
            conversation_lease = store.acquire(session, conversation)
            heartbeat = asyncio.create_task(_keep_request_alive(store, session, conversation, None,
                conversation_lease, context_lease))
            report_progress('query')
            try:
                batch, evidence = await run_owned_thread(semantic_service.execute_sales_detail_batch, cursor)
            except SemanticRuntimeError as exc:
                if exc.reason == 'sales_detail_source_changed':
                    error = DetailContextError('查询期间数据或口径已变化，请重新查询；未继续拼接旧批次')
                    error.code, error.http_status = 'detail_source_changed', 409
                    raise error from None
                raise
            if (type(batch) is not SalesDetailBatch or batch.source != cursor
                    or not isinstance(evidence, dict) or evidence.get('live_business_result_executed') is not True
                    or not 1 <= len(batch.rows) <= 5000):
                raise ChatExecutionError('本批缺少对账依据，进度未推进')
            metadata = detail_contexts.preview(session.principal_hash, conversation, selection['result_id'], batch)
            raw = batch_table(batch)
            pages = []
            for offset in range(0, len(raw['rows']), 100):
                page = _public_business_table({**raw, 'rows': raw['rows'][offset:offset + 100]}, live=True)
                if page is None:
                    raise ChatExecutionError('本批结果未通过展示校验，进度未推进')
                page['detail_batches'] = metadata
                pages.append(page)
            table = result_pages.create(session.principal_hash, pages) if len(pages) > 1 else pages[0]
            context_state = store.public_context_state(store.context(session))
            completion_text = (f'全部明细已分批取回，共{batch.cursor.loaded}行；'
                f'原范围实发数量{format(cursor.baseline.quantity, "f")} EA，出库单数{cursor.baseline.document_count}单，累计对账通过。'
                if batch.cursor.complete else '本批已取回并对账，原范围明细尚未全部取回。')
            result = {
                'request_id': task_id, 'conversation_id': conversation, 'status': 'completed',
                'result_state': 'live_success' if batch.cursor.complete else 'batch_complete',
                'business_table': table, 'detail_batch_source': dict(selection),
                'business_result': {'format': 'plain_text', 'text': completion_text},
                'clarification': {'required': False, 'questions': []},
                'outcome': {'state': 'completed', 'label': '全部批次完成' if batch.cursor.complete else '本批完成'},
                'context_state': context_state,
                'process': _public_process({'query_execution': evidence}, context_state=context_state, clarification_required=False),
            }
            detail_contexts.commit(session.principal_hash, conversation, selection['result_id'], batch, task_id)
            return result
        finally:
            if heartbeat:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass
            if conversation_lease:
                store.release(session, conversation, conversation_lease)
            store.release_context(session, context_lease)

    @app.get('/v1/exports/{export_id}')
    async def export_metadata(request: Request, export_id: str):
        session = require_session(request)
        conversation = store.resolve(session, None)
        return sales_exports.public(sales_exports.get(session.principal_hash, conversation, export_id))

    @app.post('/v1/exports/{export_id}/chunks/{index}')
    async def export_chunk(request: Request, export_id: str, index: int,
                           x_csrf_token: str | None = Header(default=None, alias='X-CSRF-Token')):
        session = check_state_change_request(request, x_csrf_token)
        if request.query_params or await request.body():
            raise ParameterValidationError('导出不接受查询参数或范围覆盖')
        conversation = store.resolve(session, None)
        item = sales_exports.get(session.principal_hash, conversation, export_id)
        offset = sales_exports.position(item, index)
        key = (session.principal_hash, export_id, index)
        now = time.monotonic()
        for cached in list(export_cache):
            if export_cache[cached][0] <= now:
                del export_cache[cached]
        if key in export_cache:
            content, headers = export_cache[key][1:]
            return Response(content, media_type='text/csv; charset=utf-8', headers=headers)
        if key not in export_workers:
            # Cap retained chunks and work globally, not just per browser tab.
            if len(export_workers) >= 2 or any(k[0] == session.principal_hash for k in export_workers):
                raise export_error('已有导出正在处理，请稍后续传', 'export_busy', 409)

            async def run_export():
                lock = principal_task_locks.setdefault(session.principal_hash, asyncio.Lock())
                async with lock:
                    context_lease = store.acquire_context(session)
                    conversation_lease, heartbeat = None, None
                    try:
                        conversation_lease = store.acquire(session, conversation)
                        heartbeat = asyncio.create_task(_keep_request_alive(store, session, conversation, None,
                            conversation_lease, context_lease))
                        batch, evidence = await run_owned_thread(semantic_service.execute_sales_export_chunk,
                            sales_exports.plan(item), item['definition'], item['baseline'], offset,
                            item['chunk_rows'], sales_exports.previous_digest(item, index))
                        if (type(batch) is not SalesDetailBatch or batch.source.loaded != offset
                            or batch.cursor.loaded != min(offset + item['chunk_rows'], item['total'])
                            or evidence.get('live_business_result_executed') is not True):
                            raise export_error('本批缺少真实查询依据，未继续写入')
                        raw = batch_table(batch)
                        public_rows = []
                        for start in range(0, len(raw['rows']), 100):
                            page = _public_business_table({**raw, 'rows': raw['rows'][start:start+100]}, live=True)
                            if page is None:
                                raise export_error('本批未通过展示校验，未继续写入')
                            public_rows.extend(page['rows'])
                        content = csv_bytes(public_rows, first=index == 0)
                        digest = sales_exports.record(item, index, content, position_digest(batch.cursor))
                        headers = {'Cache-Control': 'no-store', 'X-Export-Id': export_id,
                            'X-Chunk-Index': str(index), 'X-Chunk-SHA256': digest,
                            'X-Chunk-Rows': str(len(batch.rows)), 'X-Loaded-Rows': str(batch.cursor.loaded),
                            'X-Export-Complete': str(batch.cursor.complete).lower(),
                            'Content-Disposition': 'attachment; filename="sales-detail-chunk.csv"'}
                        # At most two 32 MiB screened chunks; never persist response bytes.
                        export_cache.clear()
                        export_cache[key] = (time.monotonic() + 120, content, headers)
                        return content, headers
                    except SemanticRuntimeError as exc:
                        if exc.reason == 'sales_detail_source_changed':
                            raise export_error('原范围数据或口径已变化，请重新查询后导出', 'detail_source_changed') from None
                        raise
                    finally:
                        if heartbeat:
                            heartbeat.cancel()
                            await asyncio.gather(heartbeat, return_exceptions=True)
                        if conversation_lease:
                            store.release(session, conversation, conversation_lease)
                        store.release_context(session, context_lease)

            worker = asyncio.create_task(run_export())
            export_workers[key] = worker
            task_workers.add(worker)
            def finished(done):
                export_workers.pop(key, None)
                task_workers.discard(done)
                if not done.cancelled():
                    done.exception()
            worker.add_done_callback(finished)
        # A disconnected browser must not release the lease while Doris is still
        # running. A retry joins this same bounded operation, or revalidates it.
        content, headers = await asyncio.shield(export_workers[key])
        return Response(content, media_type='text/csv; charset=utf-8', headers=headers)

    @app.post("/v1/tasks", status_code=202)
    async def submit_task(
        request: Request, response: Response,
        payload: Any = Body(default=None),
        x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    ) -> dict[str, Any]:
        session = check_state_change_request(request, x_csrf_token)
        body_limit = (MAX_CONVERSION_CHARS * 6 + 32_000
                      if isinstance(payload, dict) and "sql_conversion" in payload else 32_000)
        if not isinstance(payload, dict) or len(json.dumps(payload)) > body_limit:
            raise ParameterValidationError("请求格式无效")
        if "drilldown" in payload:
            selection = payload["drilldown"]
            if (set(payload) - {"conversation_id", "drilldown"} or not isinstance(selection, dict)
                    or set(selection) != {"result_id", "sku_code"}
                    or not isinstance(selection["result_id"], str) or not RANKING_ID.fullmatch(selection["result_id"])
                    or not isinstance(selection["sku_code"], str) or not 1 <= len(selection["sku_code"]) <= PUBLIC_TABLE_TEXT_MAX_CHARS):
                raise ParameterValidationError("明细选择格式无效")
        if 'detail_batch' in payload:
            selection = payload['detail_batch']
            if (set(payload) - {'conversation_id', 'detail_batch'} or not isinstance(selection, dict)
                or set(selection) != {'result_id', 'batch_index'}
                or not isinstance(selection['result_id'], str) or not DETAIL_CONTEXT_ID.fullmatch(selection['result_id'])
                or type(selection['batch_index']) is not int or not 1 <= selection['batch_index'] <= 2**53-1):
                raise ParameterValidationError('分批选择格式无效')
        key = _idempotency_key_from_request(request)
        conversation = store.resolve(session, payload.get("conversation_id"))
        payload = {**payload, "conversation_id": conversation}
        fingerprint = _request_fingerprint(payload)
        task, claimed = task_store.claim(session.principal_hash, conversation, key, fingerprint)
        _set_session_cookies(response, session, secure=cookie_secure)
        if not claimed:
            return task
        request.state.query_submitted_at = task["created_at"]
        task_owner = session.principal_hash
        cancel_events[task["task_id"]] = threading.Event()

        async def execute() -> None:
            owner = task_owner
            cancel_event = cancel_events[task["task_id"]]
            request.state.cancel_event = cancel_event
            lock = principal_task_locks.setdefault(owner, asyncio.Lock())
            def task_progress(stage):
                if cancel_event.is_set():
                    raise QueryCancelledError("查询已由用户停止")
                task_store.progress(owner, task["task_id"], stage)
            token = PROGRESS.set(task_progress)
            try:
                async with lock:
                    report_progress("routing")
                    try:
                        public_response = Response()
                        result = (await execute_detail_batch(session, conversation, payload['detail_batch'], task['task_id'])
                                  if 'detail_batch' in payload else await execute_drilldown(session, conversation, payload["drilldown"], task["task_id"])
                                  if "drilldown" in payload else await chat(request, public_response, payload, x_csrf_token))
                        if isinstance(result, JSONResponse):
                            public_payload = json.loads(result.body)
                            status = result.status_code
                        else:
                            public_payload = result
                            status = public_response.status_code or 200
                    except RuntimeErrorBase as exc:
                        status, public_payload = _runtime_error_payload(exc)
                    except Exception:
                        LOGGER.exception("Detached chat task failed")
                        status, public_payload = 500, {"error": {"code": "chat_execution_error", "message": "本次处理未完成，请稍后重试", "details": {}}}
                    try:
                        task_store.finish(owner, task["task_id"], public_payload, status)
                    except Exception:
                        # A write-back failure must not leave the durable task
                        # permanently visible as running. Retry with a small,
                        # non-sensitive terminal payload so the browser can
                        # recover instead of waiting forever.
                        LOGGER.exception("Detached chat task result write-back failed")
                        try:
                            task_store.finish(
                                owner,
                                task["task_id"],
                                {
                                    "status": "failed",
                                    "outcome": {
                                        "state": "failed",
                                        "label": "本次处理未完成",
                                    },
                                    "error": {
                                        "code": "chat_execution_error",
                                        "message": "本次处理未完成，请稍后重试",
                                        "details": {},
                                    },
                                },
                                500,
                            )
                        except Exception:
                            LOGGER.exception("Detached chat task terminal write-back failed")
            finally:
                PROGRESS.reset(token)
                # Remove only idle locks with no queued waiter.
                if not lock.locked() and not getattr(lock, "_waiters", None):
                    principal_task_locks.pop(owner, None)

        async def execute_bounded() -> None:
            execution = asyncio.create_task(execute())
            try:
                await asyncio.wait_for(asyncio.shield(execution), timeout=task_timeout_seconds)
            except asyncio.TimeoutError:
                cancel_events[task["task_id"]].set()
                timeout_payload = {
                    "status": "failed",
                    "outcome": {
                        "state": "failed",
                        "label": "本次处理超时",
                    },
                    "error": {
                        "code": "chat_timeout",
                        "message": "查询执行超时，请缩小查询范围后重试",
                        "details": {},
                    },
                }
                try:
                    task_store.finish(task_owner, task["task_id"], timeout_payload, 504)
                except Exception:
                    LOGGER.exception("Detached chat task timeout write-back failed")
                    task_store.interrupt(task_owner, task["task_id"])
                finally:
                    # Publish the deadline immediately, but retain request and
                    # principal ownership until synchronous work has drained.
                    execution.cancel()
                    await drain_task(execution)
            except asyncio.CancelledError:
                cancel_events[task["task_id"]].set()
                execution.cancel()
                await drain_task(execution)
                raise

        worker = asyncio.create_task(execute_bounded())
        task_workers.add(worker)
        live_chat_tasks[task["task_id"]] = worker

        def task_finished(done: asyncio.Task[Any]) -> None:
            task_workers.discard(done)
            if live_chat_tasks.get(task["task_id"]) is done:
                live_chat_tasks.pop(task["task_id"], None)
            cancel_events.pop(task["task_id"], None)
            if done.cancelled():
                task_store.interrupt(task_owner, task["task_id"])
                return
            try:
                error = done.exception()
            except Exception:
                error = None
            if error is not None:
                LOGGER.error("Detached chat task exited before terminal state", exc_info=error)
                task_store.interrupt(task_owner, task["task_id"])

        worker.add_done_callback(task_finished)
        return task

    def task_cleanup_state(task):
        worker = live_chat_tasks.get(task["task_id"])
        pending = task["state"] == "cancelled" and worker is not None and not worker.done()
        if not pending:
            return task
        return {**task, "cleanup_pending": True,
                "stage_label": "正在停止查询，等待当前调用结束"}

    @app.get("/v1/tasks/{task_id}")
    async def get_task(request: Request, task_id: str) -> dict[str, Any]:
        session = require_session(request)
        return task_cleanup_state(task_store.get(session.principal_hash, task_id))

    @app.post("/v1/tasks/{task_id}/cancel")
    async def cancel_task(
        request: Request,
        task_id: str,
        x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    ) -> dict[str, Any]:
        session = check_state_change_request(request, x_csrf_token)
        if request.query_params or await request.body():
            raise ParameterValidationError("停止查询不接受额外参数")
        task = task_store.cancel(session.principal_hash, task_id)
        if task["state"] == "cancelled":
            cancel_event = cancel_events.get(task_id)
            if cancel_event is not None:
                cancel_event.set()
            worker = live_chat_tasks.get(task_id)
            if worker is not None and not worker.done():
                worker.cancel()
        return task_cleanup_state(task)

    @app.get("/v1/results/{result_id}/pages/{page_number}")
    async def result_page(request: Request, result_id: str, page_number: int, response: Response) -> dict[str, Any]:
        session = require_session(request)
        response.headers["Cache-Control"] = "no-store"
        return {"business_table": result_pages.page(session.principal_hash, result_id, page_number)}

    def proposal_binding(request, csrf_token, payload, *, fields, close=False):
        session = check_state_change_request(request, csrf_token)
        base_fields = {"conversation_id", "display_session_id"} | (set() if close else {"request_id"})
        if not isinstance(payload, dict) or set(payload) != base_fields | fields:
            raise ParameterValidationError("提案请求格式无效")
        conversation_id, display_id = payload["conversation_id"], payload["display_session_id"]
        proposal_contexts._binding(conversation_id, display_id, *(() if close else (payload["request_id"],)))
        store.assert_owned(session, conversation_id)
        return (session.principal_hash, conversation_id, display_id, *(() if close else (payload["request_id"],)))

    async def proposal_baseline():
        try:
            return await asyncio.to_thread(proposal_baseline_loader or active_proposal_snapshot)
        except (RuntimeErrorBase, ValueError, TypeError, KeyError, OSError):
            error = ProposalContextError("当前口径基线暂不可用，请稍后重新评估")
            error.http_status = 503
            raise error from None

    def proposal_response(context):
        return {"result_state": "definition_review", "process": {"state": "not_queried", "steps": []}, **context}

    def start_interpretation(session, binding, opened, token):
        if token is None:
            return
        identifier = opened["interpretation_id"]

        async def execute_interpretation():
            nonlocal definition_runner
            owner = session.principal_hash
            lock = principal_task_locks.setdefault(owner, asyncio.Lock())
            lease = heartbeat = work = job = None
            acquired = False
            try:
                await lock.acquire()
                acquired = True
                job = interpretation_contexts.job(binding, identifier, token)
                if job is None:
                    return
                baseline = await proposal_baseline()
                interpretation_contexts.get(binding, identifier, current_baseline=baseline)
                lease = await asyncio.to_thread(store.acquire_context, session)
                if definition_runner is None:
                    definition_runner = InterpretationRunner(project_root=PROJECT_ROOT)
                context = await asyncio.to_thread(store.context, session)
                home = Path(definition_runner.project_root) / "var/codex-contexts" / store.context_home_ref(session)

                def bind(thread_id):
                    bound = store.bind_context_thread(session, thread_id, status="ready")
                    if bound.codex_thread_id != thread_id:
                        raise ValueError("interpretation_context_mismatch")

                async def renew():
                    interval = max(0.1, min(60, store.lease_ttl_seconds / 3))
                    while True:
                        await asyncio.sleep(interval)
                        try:
                            valid = await asyncio.to_thread(store.renew_context, session, lease)
                        except Exception:
                            valid = False
                        if not valid:
                            job["cancel_event"].set()
                            return

                heartbeat = asyncio.create_task(renew())
                work = asyncio.create_task(asyncio.to_thread(
                    definition_runner.run, job["question"], job["snapshot"], context_home=home,
                    thread_id=context.codex_thread_id, on_context_ready=bind,
                    confirmation=job["confirmation"], cancel_event=job["cancel_event"],
                ))
                result = await asyncio.shield(work)
                if job["cancel_event"].is_set():
                    interpretation_contexts.fail(binding, identifier, token, "interpretation_cancelled")
                    return
                bind(result["thread_id"])
                await asyncio.to_thread(store.record_context_turn, session, result["thread_id"])
                baseline = await proposal_baseline()
                await asyncio.to_thread(interpretation_contexts.finish, binding, identifier, token, result, baseline)
            except asyncio.CancelledError:
                if job is not None:
                    job["cancel_event"].set()
                if work is not None:
                    await asyncio.gather(work, return_exceptions=True)
                raise
            except Exception as exc:
                code = getattr(exc, "code", None) or str(exc)
                interpretation_contexts.fail(binding, identifier, token, code)
            finally:
                if heartbeat is not None:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
                try:
                    if lease is not None:
                        await asyncio.to_thread(store.release_context, session, lease)
                finally:
                    if acquired:
                        lock.release()
                    if not lock.locked() and not getattr(lock, "_waiters", None):
                        principal_task_locks.pop(owner, None)

        worker = asyncio.create_task(execute_interpretation())
        task_workers.add(worker)
        worker.add_done_callback(task_workers.discard)

    @app.post("/v1/knowledge-interpretations")
    async def create_interpretation(request: Request, payload: Any = Body(default=None),
                                   x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token")):
        binding = proposal_binding(request, x_csrf_token, payload, fields={"question"})
        baseline = await proposal_baseline()
        opened, token = interpretation_contexts.create(binding, baseline, payload["question"])
        start_interpretation(require_session(request), binding, opened, token)
        return opened

    @app.post("/v1/knowledge-interpretations/{interpretation_id}/resume")
    async def resume_interpretation(request: Request, interpretation_id: str, payload: Any = Body(default=None),
                                   x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token")):
        binding = proposal_binding(request, x_csrf_token, payload, fields=set())
        interpretation_contexts.get(binding, interpretation_id)
        baseline = await proposal_baseline()
        return interpretation_contexts.get(binding, interpretation_id, current_baseline=baseline)

    @app.post("/v1/knowledge-interpretations/{interpretation_id}/select")
    async def select_interpretation(request: Request, interpretation_id: str, payload: Any = Body(default=None),
                                   x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token")):
        binding = proposal_binding(request, x_csrf_token, payload, fields={"revision", "choices"})
        interpretation_contexts.get(binding, interpretation_id)
        baseline = await proposal_baseline()
        opened, token = interpretation_contexts.select(binding, interpretation_id, payload["revision"], payload["choices"], baseline)
        start_interpretation(require_session(request), binding, opened, token)
        return opened

    @app.post("/v1/knowledge-interpretations/{interpretation_id}/cancel")
    async def cancel_interpretation(request: Request, interpretation_id: str, payload: Any = Body(default=None),
                                   x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token")):
        binding = proposal_binding(request, x_csrf_token, payload, fields=set())
        opened = interpretation_contexts.get(binding, interpretation_id)
        cancelled = interpretation_contexts.cancel(binding, interpretation_id)
        if opened["proposal"] is not None:
            proposal_contexts.cancel(*binding, opened["proposal"]["proposal_id"])
        return cancelled

    @app.post("/v1/knowledge-proposals")
    async def create_proposal(request: Request, payload: Any = Body(default=None),
                              x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token")):
        binding = proposal_binding(request, x_csrf_token, payload, fields={"proposal"})
        raw = payload["proposal"]
        if not isinstance(raw, str) or len(raw) > MAX_PROPOSAL_CHARS:
            raise ParameterValidationError("提案材料格式无效")
        try:
            material = decode_proposal(raw)
        except ProposalError:
            raise ParameterValidationError("提案材料格式无效") from None
        baseline = await proposal_baseline()
        return proposal_response(await asyncio.to_thread(proposal_contexts.create, *binding, baseline, material))

    @app.post("/v1/knowledge-proposals/{proposal_id}/resume")
    async def resume_proposal(request: Request, proposal_id: str, payload: Any = Body(default=None),
                              x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token")):
        binding = proposal_binding(request, x_csrf_token, payload, fields=set())
        proposal_contexts.get(*binding, proposal_id)
        baseline = await proposal_baseline()
        return proposal_response(proposal_contexts.get(*binding, proposal_id, current_baseline=baseline))

    @app.post("/v1/knowledge-proposals/{proposal_id}/select")
    async def select_proposal(request: Request, proposal_id: str, payload: Any = Body(default=None),
                              x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token")):
        binding = proposal_binding(request, x_csrf_token, payload, fields={"revision", "choices"})
        proposal_contexts.get(*binding, proposal_id)
        baseline = await proposal_baseline()
        return proposal_response(await asyncio.to_thread(proposal_contexts.resolve, *binding, proposal_id,
                                 payload["revision"], payload["choices"], baseline))

    @app.post("/v1/knowledge-proposals/{proposal_id}/cancel")
    async def cancel_proposal(request: Request, proposal_id: str, payload: Any = Body(default=None),
                              x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token")):
        binding = proposal_binding(request, x_csrf_token, payload, fields=set())
        return proposal_contexts.cancel(*binding, proposal_id)

    @app.post("/v1/knowledge-proposal-sessions/close")
    async def close_proposal_session(request: Request, payload: Any = Body(default=None),
                                     x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token")):
        binding = proposal_binding(request, x_csrf_token, payload, fields=set(), close=True)
        interpretation_contexts.close_display_session(binding)
        return proposal_contexts.close_display_session(*binding)

    @app.post("/v1/metric-feedback")
    @app.post("/v1/feedback")
    async def metric_feedback(
        request: Request,
        response: Response,
        payload: Any = Body(default=None),
        x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    ) -> dict[str, Any]:
        """Collect a new business definition as a review candidate only."""

        session = check_state_change_request(request, x_csrf_token)
        if not isinstance(payload, dict):
            raise ParameterValidationError("request body must be a JSON object")
        allowed = {
            "metric_name",
            "business_definition",
            "description",
            "scope_and_grain",
            "unit_and_window",
            "example_question",
            "example_request",
            "notes",
            "conversation_id",
        }
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ParameterValidationError("Unknown feedback fields", details={"unknown": unknown})

        def text_field(*names: str, maximum: int = 4000) -> str | None:
            for name in names:
                value = payload.get(name)
                if value is None:
                    continue
                if not isinstance(value, str):
                    raise ParameterValidationError(f"{name} must be text")
                value = value.strip()
                if len(value) > maximum:
                    raise ParameterValidationError(f"{name} exceeds the configured length limit")
                return value or None
            return None

        metric_name = text_field("metric_name", maximum=200)
        description = text_field("business_definition", "description")
        scope_and_grain = text_field("scope_and_grain")
        unit_and_window = text_field("unit_and_window")
        example_request = text_field("example_question", "example_request")
        notes = text_field("notes")
        if not metric_name or not any(
            value for value in (description, scope_and_grain, unit_and_window, example_request, notes)
        ):
            raise ParameterValidationError("metric_name and at least one definition field are required")
        conversation_id = payload.get("conversation_id")
        if conversation_id is not None:
            if not isinstance(conversation_id, str) or len(conversation_id) > 100:
                raise ParameterValidationError("conversation_id must be a short string")
            conversation_id = store.assert_owned(session, conversation_id)
        receipt = store.create_metric_feedback(
            session,
            metric_name=metric_name,
            description=description or "",
            scope_and_grain=scope_and_grain,
            unit_and_window=unit_and_window,
            example_request=example_request,
            notes=notes,
            conversation_id=conversation_id,
        )
        definition_parts = [
            value
            for value in (
                description,
                f"统计范围与粒度：{scope_and_grain}" if scope_and_grain else None,
                f"单位与时间窗口：{unit_and_window}" if unit_and_window else None,
                f"补充说明：{notes}" if notes else None,
            )
            if value
        ]
        await record_learning_definition(
            session=session,
            conversation_id=conversation_id,
            metric_name=metric_name,
            definition_text="\n".join(definition_parts),
            example_question=example_request,
        )
        _set_session_cookies(response, session, secure=cookie_secure)
        return {"status": "accepted", "receipt_id": receipt}

    @app.post("/v1/query-feedback")
    async def query_feedback(
        request: Request,
        response: Response,
        payload: Any = Body(default=None),
        x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    ) -> dict[str, Any]:
        """Record one answer review against a principal-owned request."""

        session = check_state_change_request(request, x_csrf_token)
        if not isinstance(payload, dict):
            raise ParameterValidationError("request body must be a JSON object")
        allowed = {
            "conversation_id",
            "request_id",
            "feedback_type",
            "issue_type",
            "note",
        }
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ParameterValidationError("Unknown query feedback fields")
        conversation_id = payload.get("conversation_id")
        request_id = payload.get("request_id")
        feedback_type = payload.get("feedback_type")
        issue_type = payload.get("issue_type")
        note = payload.get("note")
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise ParameterValidationError("conversation_id is required")
        if len(conversation_id.strip()) > 100:
            raise ParameterValidationError("conversation_id must be a short string")
        if not isinstance(request_id, str) or not request_id.strip():
            raise ParameterValidationError("request_id is required")
        if len(request_id.strip()) > 200:
            raise ParameterValidationError("request_id is too long")
        if not isinstance(feedback_type, str) or feedback_type.strip().casefold() not in {
            "accepted",
            "correction",
        }:
            raise ParameterValidationError("feedback_type is invalid")
        if issue_type is not None and (
            not isinstance(issue_type, str) or len(issue_type.strip()) > 80
        ):
            raise ParameterValidationError("issue_type is invalid")
        if note is not None and (
            not isinstance(note, str) or len(note) > 1200
        ):
            raise ParameterValidationError("note exceeds the configured length limit")
        if learning_service is None:
            raise LearningError("学习记录暂时不可用")
        owned_conversation_id = store.assert_owned(session, conversation_id.strip())
        recorded = await asyncio.to_thread(
            learning_service.record_feedback,
            principal_hash=session.principal_hash,
            conversation_id=owned_conversation_id,
            request_id=request_id.strip(),
            feedback_type=feedback_type.strip(),
            issue_type=issue_type.strip() if isinstance(issue_type, str) else None,
            note=note,
        )
        feedback_id = recorded.get("feedback_id") if isinstance(recorded, Mapping) else None
        if not isinstance(feedback_id, str) or not feedback_id:
            raise LearningFeedbackValidationError("反馈未能保存")
        _set_session_cookies(response, session, secure=cookie_secure)
        return {"status": "accepted", "feedback_id": feedback_id}

    @app.get("/v1/learning/summary")
    async def learning_summary(request: Request, response: Response) -> dict[str, Any]:
        """Expose aggregate learning counters, never private candidate examples."""

        if _is_cross_site_fetch(request) or not _same_origin(request):
            raise SessionSecurityError("请求来源校验失败")
        session = require_session(request)
        if learning_service is None:
            raise LearningError("学习总结暂时不可用")
        summary = await asyncio.to_thread(
            learning_service.public_summary,
            session.principal_hash,
        )
        _set_session_cookies(response, session, secure=cookie_secure)
        return summary

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(FRONTEND_ROOT / "index.html")

    @app.get("/network-access")
    async def network_access_admin() -> FileResponse:
        return FileResponse(FRONTEND_ROOT / "network-access.html")

    @app.get("/network-access/request")
    async def network_access_request_page() -> FileResponse:
        return FileResponse(FRONTEND_ROOT / "network-access-request.html")

    app.mount("/static", StaticFiles(directory=FRONTEND_ROOT), name="static")
    return app


app = create_chat_app()


def main() -> int:
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - required runtime dependency
        raise RuntimeError("uvicorn is required for the chat frontend") from exc
    host = os.getenv("AGENT_CHAT_HOST", "127.0.0.1").strip()
    if _chat_network_mode() == "lan" and host in {"0.0.0.0", "::"}:
        pass
    elif host.casefold() not in {"localhost", "127.0.0.1", "::1"}:
        try:
            address = ip_address(host)
            if not address.is_loopback and not (
                _chat_network_mode() == "lan"
                and any(address in network for network in _PRIVATE_CHAT_NETWORKS)
            ):
                raise RuntimeError("The chat frontend may bind only to loopback or a private LAN address")
        except ValueError as exc:
            raise RuntimeError("The chat frontend bind address is invalid") from exc
    port = _env_int("AGENT_CHAT_PORT", 8020, minimum=1024, maximum=65535)
    uvicorn.run("app.chat_api:app", host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

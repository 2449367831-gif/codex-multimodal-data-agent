"Semantic request, presentation and business-date primitives."

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import re
import unicodedata
from typing import TYPE_CHECKING, Any, Callable, Mapping, Protocol, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..errors import RuntimeErrorBase
from ..query_scope import QueryScope
from ..query_outcomes import DorisResultError
from ..request_syntax import explicit_rank_counts
from ..sku_references import extract_sku_references
from .sales_aggregation import SalesAggregation
from .sales_analysis import SalesAnalysis


@dataclass(frozen=True, slots=True)
class SalesComposition:
    """A verified operator over one common actual-sales fact set."""

    operation: str
    dates: tuple[date, date]
    coverage_percent: str = "80"

    def __post_init__(self):
        if self.operation not in {"growth", "above_average", "coverage"}:
            raise ValueError("unsupported sales composition")
        if (not isinstance(self.dates, tuple) or len(self.dates) != 2
                or any(type(value) is not date for value in self.dates)
                or self.dates[0] > self.dates[1]):
            raise ValueError("sales composition requires ordered date boundaries")
        if self.operation == "growth" and self.dates[1] - self.dates[0] != timedelta(days=1):
            raise ValueError("growth requires consecutive complete dates")
        if (not isinstance(self.coverage_percent, str)
                or not re.fullmatch(r"(?:[1-9][0-9]?|100)(?:\.[0-9]{1,2})?", self.coverage_percent)
                or not 0 < Decimal(self.coverage_percent) <= 100):
            raise ValueError("invalid coverage percentage")


if TYPE_CHECKING:
    from .sales_timeseries import SalesTimeSeries
    from .sales_order_details import SalesOrderDetailPlan
    from .order_components import OrderComponentPlan
    from .outbound_links import OutboundLinkPlan
    from .inbound_details import InboundDetailPlan
    from .occupation_planning import InventoryOccupationSplitPlan
    from .receipt_summary import ReceiptProductionSummaryPlan
    from .receipt_summary import ReceiptWarehouseProductionSummaryPlan
    from .sales_quality import SalesWarehouseQualityPlan
    from .inventory_availability import InventoryAvailabilityPlan
    from .warehouse_fulfillment import WarehouseFulfillmentPlan
    from .dms_redemption_summary import DmsRedemptionSummaryPlan
    from .dms_points_orders import DmsPointsOrderPlan
    from .dms_manager_orders import DmsManagerOrderPlan



class QueryClient(Protocol):
    def query(self, *, sql: str, purpose: str | None = None) -> dict[str, Any]: ...


class SemanticRuntimeError(RuntimeErrorBase):
    """A governed semantic route failed its own quality gate."""

    code = "semantic_query_error"
    http_status = 502
    _PUBLIC_BUSINESS_CODES = {
        "inventory_quality_failed": "inventory_quality_failed",
        "inventory_component_quality_failed": "inventory_quality_failed",
        "current_inventory_quality_failed": "inventory_quality_failed",
        "current_inventory_controls_invalid": "inventory_quality_failed",
        "current_inventory_total_mismatch": "inventory_quality_failed",
        "current_inventory_row_invalid": "inventory_quality_failed",
        "sales_composition_reconciliation_failed": "sales_data_quality_failed",
    }

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message, details={"reason": reason})
        self.reason = reason
        if reason in {"backend_rejected", "query_failed", "backend_unavailable", "query_timeout", "result_truncated"}:
            failure = DorisResultError(reason)
            self.http_status = failure.http_status

    def as_public_dict(self) -> dict[str, Any]:
        # The reason is useful in server logs, but it is an implementation
        # detail and must not become a second browser-facing data channel.
        if self.reason in {"backend_rejected", "query_failed", "backend_unavailable", "query_timeout", "result_truncated"}:
            return DorisResultError(self.reason).as_public_dict()
        return {
            "code": self._PUBLIC_BUSINESS_CODES.get(self.reason, self.code),
            "message": self.message,
            "result_state": "failed",
            "details": {},
        }


@dataclass(frozen=True, slots=True)
class SemanticRequest:
    route: str
    start: date | None
    end: date | None
    production_date: date | None = None
    product_code: str | None = None
    product_name: str | None = None
    product_keyword: str | None = None
    warehouse_code: str | None = None
    warehouse_label: str | None = None
    show_warehouses: bool = False
    show_top_sku: bool = False
    top_n: int | None = None
    inbound_basis: str | None = None
    ref_order_type: str | None = None
    fulfillment_type: str | None = None
    all_outbound_types: bool = False
    finished_goods_only: bool = False
    lookup_keyword: str | None = None
    sales_aggregation: SalesAggregation | None = None
    sales_analysis: SalesAnalysis | None = None
    sales_composition: SalesComposition | None = None
    sales_timeseries: SalesTimeSeries | None = None
    # A selected UI window can cover more than one day.  Keep its display
    # label with the governed request instead of deriving a misleading label
    # from only the first date.
    period_label: str | None = None
    turnover_grain: str | None = None
    comparison_periods: tuple[tuple[date, date], tuple[date, date]] | None = None
    scope: QueryScope = QueryScope()
    receipt_plan: InboundDetailPlan | None = None
    occupation_split_plan: InventoryOccupationSplitPlan | None = None
    receipt_summary_plan: ReceiptProductionSummaryPlan | None = None
    receipt_warehouse_summary_plan: ReceiptWarehouseProductionSummaryPlan | None = None
    sales_warehouse_quality_plan: SalesWarehouseQualityPlan | None = None
    inventory_availability_plan: InventoryAvailabilityPlan | None = None
    warehouse_fulfillment_plan: WarehouseFulfillmentPlan | None = None
    dms_redemption_summary_plan: DmsRedemptionSummaryPlan | None = None
    dms_order_plan: DmsPointsOrderPlan | None = None
    dms_manager_plan: DmsManagerOrderPlan | None = None
    outbound_link_plan: OutboundLinkPlan | None = None
    order_return_plan: OrderComponentPlan | None = None
    so07_plan: SalesOrderDetailPlan | None = None
    price_group_parent: str | None = None


class SemanticAnswer(str):
    """A text answer with an optional, route-owned display table.

    The string subtype keeps the existing interaction contract intact while
    allowing the BFF to carry a separately screened presentation payload.
    """

    business_table: dict[str, Any] | None

    def __new__(
        cls,
        text: str,
        business_table: dict[str, Any] | None = None,
        *,
        business_tables: list[dict[str, Any]] | None = None,
        result_state: str | None = None,
    ) -> "SemanticAnswer":
        if result_state not in {None, "no_data", "live_success"}:
            raise ValueError("invalid semantic answer state")
        value = super().__new__(cls, text)
        value.business_table = business_table
        value.business_tables = business_tables
        value.result_state = result_state
        return value


class SemanticResult(dict):
    """JSON result with process-local context excluded from serialized payloads."""

    ranking_snapshot = None


def _display_table(
    *,
    table_id: str,
    title: str,
    caption: str,
    columns: list[dict[str, str]],
    rows: list[dict[str, str]],
    summary: list[dict[str, str]] | None = None,
    empty_label: str = "当前范围没有可展示的数据",
) -> dict[str, Any]:
    """Build a presentation-only table from already reconciled aggregates."""

    return {
        "format": "table",
        "table_id": table_id,
        "title": title,
        "caption": caption,
        "columns": columns,
        "rows": rows,
        "summary": summary or [],
        "empty_label": empty_label,
    }


_DATE_PATTERN = re.compile(
    r"(?<!\d)(?:(20\d{2})\s*年\s*)?(1[0-2]|0?[1-9])\s*月\s*"
    r"(3[01]|[12]\d|0?[1-9])\s*(?:日|号)"
)
_ISO_DATE_PATTERN = re.compile(
    r"(?<!\d)(20\d{2})[-/.](1[0-2]|0?[1-9])[-/.](3[01]|[12]\d|0?[1-9])(?!\d)"
)
_MONTH_PATTERN = re.compile(
    r"(?<!\d)(?:(20\d{2})\s*年\s*)?(1[0-2]|0?[1-9])\s*月份?"
)
_ISO_MONTH_PATTERN = re.compile(
    r"(?<!\d)(20\d{2})[-/.](1[0-2]|0?[1-9])(?![-/.\d])"
)
_SAUSAGE_WORDS = ("烤肠", "香肠", "脆皮肠")
_PRODUCTION_DATE_WORDS = ("生产日期", "生产日", "生产批次", "批次日期", "生产")
_WAREHOUSE_WORDS = ("仓", "仓库", "分仓")
_INBOUND_WORDS = ("入库", "入仓", "收货")
_TOP_WORDS = ("最多", "最大", "top", "最高", "排名")
_TOP_MARKER_PATTERN = re.compile(r"(?:top|排名|前\s*\d{1,3}|最多|最高)", re.I)
_SKU_WORDS = ("sku", "商品", "货品", "单品", "品项")
# Keep generic "发货量" and "出库量" out of this list. Those phrases have
# multiple maintained business variants and must be clarified unless the user
# also says sales, actual, or completed.
_SALES_OUTBOUND_ALIAS_WORDS = (
    "销售出库",
    "销售发货",
    "销售出货",
    "实际出库",
    "完成出库",
    "已完成出库",
    "实际发货",
    "实际发出",
    "实际发了",
    "实际发数量",
    "实际发出量",
    "完成发货",
    "已完成发货",
    "实际出货",
    "完成出货",
    "已完成出货",
    "实际出货量",
    "实际销售",
    "销量",
    "卖出去",
    "销售数量",
    "销售量",
    "销售实发数量",
    "销售实发量",
    "销售出库量",
)
_GENERIC_OUTBOUND_WORDS = ("发货数量", "发货量", "出库数量", "出库量")
_TABLE_PRESENTATION_WORDS = ("表格", "列表", "排名表", "明细表")
_DMS_MARKER_WORDS = ("dms", "dms订单", "dms系统")
_DMS_POINTS_WORDS = ("积分兑换", "积分订单", "积分", "兑换")
_DMS_AIR_FRYER_WORDS = ("空气炸锅", "空气锅", "airfryer")
_MONTH_RANGE_PATTERN = re.compile(
    r"(?:(20\d{2})\s*年\s*)?"
    r"(1[0-2]|0?[1-9])\s*月?\s*"
    r"(?:-|~|～|—|至|到)\s*"
    r"(1[0-2]|0?[1-9])\s*月"
)
_SALES_OUTBOUND_NEGATIVE_WORDS = (
    "订单请求",
    "订单计划",
    "计划量",
    "未完成",
    "渠道替换",
    "退货",
)

# The browser sends opaque, server-validated IDs.  Keep a small local alias
# table here so a selected clarification can be resolved without asking the
# model to reinterpret the same choice.  Unknown IDs fail closed.
_SELECTION_QUESTION_ALIASES = {
    "outbound_quantity_basis": "outbound_basis",
    "sales_quantity_basis": "outbound_basis",
    "sales_window_basis": "sales_window",
    "turnover_basis": "turnover_inventory_point",
    "turnover_component_basis": "turnover_inventory_point",
    "inventory_basis": "inventory_quantity_basis",
    "basis": "inventory_quantity_basis",
}
_SELECTION_OPTION_ALIASES = {
    "actual_outbound": "actual_completed_outbound",
    "actual_completed": "actual_completed_outbound",
    "completed_outbound": "actual_completed_outbound",
    "sales_outbound_actual": "actual_completed_outbound",
    "order_requested": "order_requested_quantity",
    "requested_quantity": "order_requested_quantity",
    "mtd_to_today": "mtd_to_today_00",
    "month_to_today_00": "mtd_to_today_00",
    "specified_period": "explicit_period",
    "custom_window": "explicit_period",
    "custom_period": "explicit_period",
    "historical_snapshot_actual_outbound": "period_end_finished_goods_snapshot",
    "current_available_actual_outbound": "current_realtime_finished_goods",
    "inventory_turnover_finished_goods_snapshot": "period_end_finished_goods_snapshot",
    "inventory_turnover_finished_goods_realtime": "current_realtime_finished_goods",
}


def _normalize_selected_answers(
    selections: Sequence[Mapping[str, Any]] | None,
) -> dict[str, str] | None:
    """Normalize one-option UI answers for the deterministic continuation."""

    if not isinstance(selections, Sequence) or isinstance(selections, (str, bytes)):
        return None
    normalized: dict[str, str] = {}
    for item in selections:
        if not isinstance(item, Mapping):
            return None
        raw_question_id = item.get("question_id") or item.get("id")
        if not isinstance(raw_question_id, str) or not raw_question_id.strip():
            return None
        question_id = _SELECTION_QUESTION_ALIASES.get(
            raw_question_id.strip(), raw_question_id.strip()
        )
        raw_options = item.get("option_ids")
        if raw_options is None:
            raw_option = item.get("option_id")
            raw_options = [raw_option] if raw_option is not None else []
        if isinstance(raw_options, (str, bytes)):
            raw_options = [raw_options]
        if not isinstance(raw_options, Sequence) or len(raw_options) != 1:
            # Multiple-selection contracts need their own reviewed execution
            # route; do not collapse them into an arbitrary single choice.
            return None
        raw_option_id = raw_options[0]
        if not isinstance(raw_option_id, str) or not raw_option_id.strip():
            return None
        option_id = _SELECTION_OPTION_ALIASES.get(
            raw_option_id.strip(), raw_option_id.strip()
        )
        if question_id in normalized:
            return None
        normalized[question_id] = option_id
    return normalized or None


def _business_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("invalid business timezone") from exc


def _compact(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _requested_presentation(question: str) -> str:
    """Return a small, validated presentation preference for the current turn."""

    compact = _compact(question)
    if any(word in compact for word in _TABLE_PRESENTATION_WORDS):
        return "table"
    return "default"


def _date_label(value: date) -> str:
    return f"{value.year}年{value.month}月{value.day}日"


def _month_label(year: int, month: int) -> str:
    return f"{year}年{month}月"


def _period_label(start: date, end: date, explicit: str | None = None) -> str:
    """Render one half-open business interval without exposing SQL details."""

    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()[:120]
    if end <= start:
        return _date_label(start)
    inclusive_end = end - timedelta(days=1)
    if start.day == 1 and end.day == 1:
        if start.year == inclusive_end.year and start.month == inclusive_end.month:
            return _month_label(start.year, start.month)
        if start.year == inclusive_end.year:
            return f"{start.year}年{start.month}月至{inclusive_end.month}月"
        return (
            f"{start.year}年{start.month}月至"
            f"{inclusive_end.year}年{inclusive_end.month}月"
        )
    if start == inclusive_end:
        return _date_label(start)
    return f"{_date_label(start)}至{_date_label(inclusive_end)}"


def _parse_decimal(value: Any, *, default: Decimal | None = None) -> Decimal:
    if value is None or value == "":
        if default is not None:
            return default
        raise ValueError("missing numeric value")
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("invalid numeric value") from exc
    if not parsed.is_finite():
        raise ValueError("non-finite numeric value")
    return parsed


def _parse_source_datetime(value: Any, *, timezone_name: str) -> datetime:
    """Parse a Doris timestamp and normalize it to the business timezone."""

    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            raise ValueError("missing source timestamp")
        parsed_text = text[:-1] + "+00:00" if text.endswith("Z") else text
        parsed = datetime.fromisoformat(parsed_text.replace(" ", "T", 1))
    zone = _business_zone(timezone_name)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone)


def _format_number(value: Decimal, *, places: int = 2) -> str:
    if not value.is_finite():
        raise ValueError("non-finite display value")
    if places <= 0:
        return format(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP), "f")
    quantum = Decimal("1").scaleb(-places)
    rendered = value.quantize(quantum, rounding=ROUND_HALF_UP)
    text = format(rendered, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def _requested_top_n(question: str) -> int | None:
    """Read a bounded ranking size without letting prose become SQL."""

    _, question = extract_sku_references(question)
    compact = _compact(question)
    if _TOP_MARKER_PATTERN.search(compact) is None:
        return None
    counts = explicit_rank_counts(compact)
    if counts:
        if any(not value.isdecimal() or len(value) > 3 for value in counts):
            return None
        sizes = {int(value) for value in counts}
        return next(iter(sizes)) if len(sizes) == 1 and 1 <= min(sizes) <= 100 else None
    # A number-free "top SKU" request is a useful, deterministic Top 20.
    # "最多/最高" without a ranking phrase means the single leader.
    if "最多" in compact or "最高" in compact:
        return 1
    return 20

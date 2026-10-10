"Request-only planning. This module cannot query Doris."

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Sequence
from ..query_scope import QueryScope, ScopeResolver
from ..outbound_contract import ACTUAL_OUTBOUND_STATUS_CODES
from ..sku_references import extract_sku_references
from ..knowledge_activation import active_snapshot
from ..request_intent import assess_request
from ..request_syntax import (
    consume_sku_ranking_output, explicit_rank_counts, normalize_query_wording,
    normalize_recent_day_windows, recent_day_windows,
)
from .handlers.directory import (
    warehouse_lookup_request,
    warehouse_type_count_request,
    warehouse_type_summary_detail_request,
)
from .receipt_planning import inbound_detail_request
from .occupation_planning import occupation_split_request
from .receipt_summary import receipt_production_summary_request, receipt_warehouse_production_summary_request
from .finished_goods_planning import finished_goods_definition
from .box_planning import filtered_outbound_box_request, outbound_box_request
from .drilldown_planning import ranked_sales_request
from .dms_order_planning import points_order_request, manager_order_request
from .name_quality_planning import name_quality_scope, name_quality_request
from .outbound_link_planning import is_link_question, link_request
from .order_return_planning import is_order_return_question, return_request, OPTIONS as RETURN_DECISIONS
from .so07_planning import is_so07_question, so07_request, OPTIONS as SO07_DECISIONS
from .outbound_clarification import OPTIONS as OUTBOUND_DECISIONS, unresolved_outbound_source, selected_outbound_request
from .turnover_conflict import OPTIONS as TURNOVER_CONFLICT_DECISIONS, has_turnover_conflict
from .sales_aggregation import SalesAggregation
from .sales_dimensions import GROUPS, requested_dimensions
from .sales_analysis import sales_analysis_request
from .sales_timeseries import sales_timeseries_request
from .price_group_sales import price_group_request
from .contract_constraints import without_excluded_allocation_source
from .period_comparison import warehouse_period_comparison
from .sales_detail_planning import sales_detail_request
from .sales_quality import SalesWarehouseQualityPlan
from .inventory_availability import InventoryAvailabilityPlan
from .inventory_components import InventoryComponentPlan
from .warehouse_fulfillment import WarehouseFulfillmentPlan, warehouse_fulfillment_request
from .dms_redemption_summary import dms_redemption_summary_request
from .semantic_types import (
    SemanticRequest,
    SemanticRuntimeError,
    _DATE_PATTERN,
    _DMS_AIR_FRYER_WORDS,
    _DMS_MARKER_WORDS,
    _DMS_POINTS_WORDS,
    _GENERIC_OUTBOUND_WORDS,
    _INBOUND_WORDS,
    _ISO_DATE_PATTERN,
    _ISO_MONTH_PATTERN,
    _MONTH_PATTERN,
    _MONTH_RANGE_PATTERN,
    _PRODUCTION_DATE_WORDS,
    _SALES_OUTBOUND_ALIAS_WORDS,
    _SALES_OUTBOUND_NEGATIVE_WORDS,
    _SAUSAGE_WORDS,
    _SKU_WORDS,
    _TOP_WORDS,
    _WAREHOUSE_WORDS,
    _business_zone,
    _compact,
    _next_month,
    _normalize_selected_answers,
    _requested_top_n,
)


_TURNOVER_SNAPSHOT_WORDS = re.compile(
    r"(?:区间末|期末|历史)(?:成品)?库存(?:快照)?|(?:成品)?库存快照"
)

# Match syntactic dates before calendar validation, including invalid months.
_INTERVAL_DATE_PATTERNS = (
    re.compile(r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)"),
    re.compile(r"(?<!\d)(?:(\d{4})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*(?:日|号)"),
)
_INTERVAL_CONNECTOR = re.compile(r"\s*(?:到|至|-|~|～|—|–|to)\s*", re.IGNORECASE)
_CLOCK_PREFIX = re.compile(
    r"\s*(?:日|号)?\s*T?\s*(?:(?:凌晨|上午|下午|中午|晚上|午夜)?\s*"
    r"(?:\d{1,2}\s*[:：]\s*\d{2}(?:\s*[:：]\s*\d{2})?|\d{1,2}\s*[点时](?:\s*\d{1,2}\s*分?)?|零\s*点)"
    r"|凌晨|午夜)(?!\d)", re.IGNORECASE
)


def _has_unimplemented_inbound_condition(text: str) -> bool:
    """Keep the inbound accelerator from silently dropping extra predicates.

    The accelerator only owns the reviewed warehouse/SKU summary shape.  Any
    explicit comparison, alternate grain, or unit conversion must go through
    model planning so it can be implemented, clarified, or rejected visibly.
    """

    compact = _compact(text)
    return bool(
        re.search(r"数量|金额|件数|单数|库存(?:量|数)?\s*(?:大于|小于|不低于|不高于|超过|低于|至少|至多|>=|<=|>|<)", compact)
        or re.search(r"按(?:供应商|订单创建时间|入库时间|采购单|批次)", compact)
        or re.search(r"(?:换算|转换|折算)(?:成|为)?(?:箱|吨|公斤|件)", compact)
    )


def _midnight_clock(text: str) -> bool:
    compact = re.sub(r"\s|[Tt]", "", text).lstrip("日号")
    return re.fullmatch(r"(?:凌晨|午夜)?(?:00?[:：]00(?:[:：]00)?|00?[点时](?:00?分?)?|零点)|凌晨|午夜", compact) is not None


def _turnover_scope_question(question: str) -> str:
    # Consume timing only for turnover scope parsing. Keep the original text
    # for numerator selection and date parsing; other routes stay unchanged.
    compact = _compact(question)
    if "周转" not in compact:
        return question
    if _TURNOVER_SNAPSHOT_WORDS.search(compact):
        # Preserve the established historical-snapshot vocabulary. The scope
        # resolver uses these words to distinguish a snapshot request from a
        # current-state request.
        return _TURNOVER_SNAPSHOT_WORDS.sub("成品库存", question)
    if not any(token in compact for token in ("除以", "在途", "按仓", "各仓", "仓库维度")):
        return question
    value = _TURNOVER_SNAPSHOT_WORDS.sub("成品库存", question)
    # Consume only the formula introducer, never its whole suffix: owner,
    # warehouse, SKU and exclusions can occur anywhere around the formula.
    value = re.sub(
        r"(?:按|使用|采用)\s*(?=(?:当前|实时|可用|成品|库存))",
        "",
        value,
    )
    # Formula wording describes the metric, not a scope predicate. Remove it
    # only from the scope pass; the original question remains available to the
    # metric planner so inventory basis and time window are preserved.
    value = re.sub(
        r"(?:可用|实时|当前|现在|现有|在途|实际|销售|出库|实发|平均|数量|库存|周转|除以|计算|统计|汇总|加)",
        "",
        value,
    )
    value = re.sub(r"(?:近|最近|过去)?(?:\d+|[一二三四五六七八九十两]+)(?:个)?(?:完整)?(?:自然)?(?:日|天)", "", value)
    value = re.sub(r"(?:是)?多少(?:天|日)?|天数|该期间|所选区间|日均", "", value)
    value = re.sub(r"(?:按|使用|采用)[，,。！？?；;]", "", value)
    return value


def _remove_route_only_scope_clauses(question: str) -> str:
    """Remove explicit metric modifiers before the scope resolver.

    These clauses are consumed by the route compiler (channel, date-boundary
    wording, logical-delete policy, or an explicit return exclusion). Leaving
    them in the scope residue makes a valid request look like an unsupported
    customer/filter predicate.
    """

    value = re.sub(r"(?:查询|查|统计|汇总|列出|输出|展示|同时|并且|并)", "", question)
    value = re.sub(r"按(?:出库)?状态", "", value)
    value = re.sub(r"用表格(?:形式)?展示", "", value)
    value = re.sub(
        r"(?:实际|有效)?(?:销售)?出库(?:单数|单量|数量|量|明细|记录|数据|单)?",
        "",
        value,
    )
    value = re.sub(r"实发(?:数量|量)", "", value)
    value = re.sub(r"截至(?:昨天|昨日|今天|今日)(?:00[:：]?00)?", "", value)
    value = re.sub(r"(?:卖出去|卖了)", "", value)
    value = re.sub(r"多少(?:单|件|个|箱|量)?", "", value)
    value = re.sub(r"了", "", value)
    value = re.sub(r"(?:按|各|每个|每)中心仓", "", value)
    # A fulfillment split is an output dimension, not an owner/SKU scope.
    # Remove its coordinated wording before scope resolution so phrases such
    # as "各仓库和2C/2B分别统计" cannot leave the resolver with a stray
    # unsupported predicate.
    value = re.sub(
        r"(?:和|与|及|、)?\s*2c\s*(?:/|和|与|及)\s*2b\s*(?:分别)?(?:统计|汇总|计算)?",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"(?:线上|线下|其他)(?:客户)?渠道|客户渠道|渠道细分|渠道", "", value)
    value = re.sub(
        r"(?:不含|不计|不纳入|不扣除|不扣|排除|剔除)退货",
        "",
        value,
    )
    value = re.sub(
        r"(?:排除|剔除|不含|不计)(?:逻辑删除|已删除)(?:单据|数据|记录)?",
        "",
        value,
    )
    # Completed-status wording and zero-document reporting are sales-route
    # controls, not free-text scope predicates. Keep them in the original
    # planning question so the sales compiler still applies the reviewed
    # status contract, but remove them from the scope resolver's residue.
    value = re.sub(
        r"(?:有效|完成|合法)?(?:的)?(?:出库)?状态\s*(?:为|是|[:：=])?\s*"
        r"(?:700|900|701|901)(?:\s*[、,，/和与及或]\s*(?:700|900|701|901))*",
        "",
        value,
    )
    value = re.sub(
        r"(?:同时|并且|并)?\s*(?:列出|统计|展示|输出)?\s*各(?:出库)?状态",
        "",
        value,
    )
    value = re.sub(
        r"(?:零数量|数量为0|实发总量为0)[^。；，,]*(?:出库单数|出库单|单据)[^。；，,]*",
        "",
        value,
    )
    value = re.sub(r"实发总量为0[^。；，,]*", "", value)
    return value


class RequestPlanner:
    changsha_huangxing_wms = "10114790"

    def __init__(self, *, timezone_name="Asia/Shanghai", clock=None, scope_resolver=None):
        self._zone = _business_zone(timezone_name)
        self.clock = clock
        self.scope_resolver = scope_resolver or ScopeResolver()

    def _now(self) -> datetime:
        if self.clock is None:
            return datetime.now(self._zone)
        try:
            current = self.clock(self._zone)
        except TypeError:
            current = self.clock()
        if not isinstance(current, datetime):
            raise SemanticRuntimeError("当前业务时间无法确认，请稍后重试", reason="clock_invalid")
        if current.tzinfo is None:
            return current.replace(tzinfo=self._zone)
        return current.astimezone(self._zone)

    def _parse_explicit_date(self, question: str) -> date | None:
        match = _ISO_DATE_PATTERN.search(question)
        if match is not None:
            year = int(match.group(1))
            month = int(match.group(2))
            day = int(match.group(3))
        else:
            match = _DATE_PATTERN.search(question)
            if match is None:
                return None
            year = int(match.group(1) or self._now().year)
            month = int(match.group(2))
            day = int(match.group(3))
        try:
            return date(year, month, day)
        except ValueError as exc:
            raise SemanticRuntimeError("日期范围无效，请重新提交", reason="date_invalid") from exc

    def _parse_explicit_month(self, question: str) -> date | None:
        """Parse a named month without guessing a future or prior-year period."""

        match = _ISO_MONTH_PATTERN.search(question)
        if match is not None:
            year_text, month_text = match.group(1), match.group(2)
        else:
            match = _MONTH_PATTERN.search(question)
            if match is None:
                return None
            year_text, month_text = match.group(1), match.group(2)
        month = int(month_text)
        if year_text is None:
            now_date = self._now().date()
            # A bare month is deterministic only when it is a completed month
            # in the current year. Crossing a year needs an explicit year or
            # the reviewed "上月" wording.
            if month >= now_date.month:
                return None
            year = now_date.year
        else:
            year = int(year_text)
        return date(year, month, 1)

    def _single_day(self, question: str, *, default_yesterday: bool = False) -> date | None:
        compact = _compact(question)
        if "前天" in compact:
            return self._now().date() - timedelta(days=2)
        if "昨天" in compact or "昨日" in compact:
            return self._now().date() - timedelta(days=1)
        if "今天" in compact or "今日" in compact:
            return self._now().date()
        explicit = self._parse_explicit_date(question)
        if explicit is not None:
            return explicit
        if default_yesterday:
            return self._now().date() - timedelta(days=1)
        return None

    def _parse_month_range(self, question: str) -> tuple[date, date] | None:
        match = _MONTH_RANGE_PATTERN.search(question)
        if match is None:
            return None
        year = int(match.group(1) or self._now().year)
        start_month = int(match.group(2))
        end_month = int(match.group(3))
        if start_month > end_month:
            raise SemanticRuntimeError("月份范围无效，请重新提交", reason="month_range_invalid")
        end_year, end_month_exclusive = _next_month(year, end_month)
        return date(year, start_month, 1), date(end_year, end_month_exclusive, 1)

    def classify(self, question: str, *, _with_analysis: bool = True) -> SemanticRequest | None:
        question = normalize_recent_day_windows(question)
        question = normalize_query_wording(question)
        _, rank_question = extract_sku_references(question)
        if explicit_rank_counts(rank_question) and _requested_top_n(rank_question) is None:
            return None
        # Accept the common production-date shorthand "8.3生产日期" without
        # treating decimal-like SKU text elsewhere as a calendar date.
        question = re.sub(
            r"(?<!\d)(\d{1,2})[.](\d{1,2})(?=\s*(?:生产|生产日期|生产的))",
            r"\1月\2日",
            question,
        )
        # Normalize reviewed department synonyms before any early intent or
        # ambiguity gate. Later semantic planning repeats the normalization at
        # the contract boundary, but early gates must see the same vocabulary.
        question = re.sub(r"生产产品", "成品", question)
        question = re.sub(
            r"实际出货量|已完成出货量|完成出货量",
            "实际销售出库量",
            question,
        )
        question = re.sub(r"全仓", "全部仓库", question)
        # "线上/线下各渠道" is a grouping request, not a channel filter.
        # Normalize the natural-language form before dimension extraction.
        question = re.sub(
            r"(?:线上[、和与]?线下(?:[、和与]?其他)?|线上线下(?:其他)?)各渠道",
            "按渠道",
            question,
        )
        question = re.sub(r"各渠道|每个渠道", "按渠道", question)
        question = re.sub(
            r"每个仓(?:库)?每个SKU|每仓每个SKU|各仓各SKU",
            "按仓库和SKU",
            question,
            flags=re.IGNORECASE,
        )
        question = re.sub(
            r"(?:每个仓库|各仓库)(?:、|，|,|和|与|及|\s)+(?:每个SKU|各SKU)"
            r"(?:、|，|,|和|与|及|\s)+(?:每个渠道|各渠道)",
            "按仓库和SKU和渠道",
            question,
            flags=re.IGNORECASE,
        )
        # Preserve the explicit production-date inventory route before the
        # generic SKU/scope extractor removes the product-family wording.
        # Only invoke this early probe for the batch shape; unrelated routes
        # must retain their own invalid-date handling.
        if (
            (any(word in _compact(question) for word in _SAUSAGE_WORDS)
             or "成品" in _compact(question)
             or "产成品" in _compact(question)
             or any(word in _compact(question) for word in ("库存", "存量")))
            and ("长沙黄兴" in _compact(question) or "黄兴" in _compact(question))
            and any(word in _compact(question) for word in _PRODUCTION_DATE_WORDS)
        ):
            direct_batch = self._classify_shape(question, source_question=question)
            if direct_batch is not None and direct_batch.route == "batch_inventory":
                return direct_batch
        _, operator_question = extract_sku_references(question)
        compact_types = _compact(operator_question)
        type_matches = list(re.finditer(r"(?<![a-z0-9])(jyck|hhck|bfck|qtck|so\d+)(?![a-z0-9])", compact_types))
        if len(type_matches) > 1 and not is_so07_question(question):
            # A union of type literals is not the same as an OR between scoped
            # predicates. Leave paired/negative expressions to general planning.
            if any(not re.fullmatch(r"[和与及或、,，/+|'\"\s]+", compact_types[left.end():right.start()])
                   for left, right in zip(type_matches, type_matches[1:])):
                return None
        # A plain "销售出库明细" request is still an actual-shipment detail
        # request. Resolve it before broader outbound aggregations so large
        # result sets use the reviewed control-query and cursor path instead of
        # the generic one-shot SQL capacity.
        sales_detail = sales_detail_request(question, planner=self)
        if sales_detail is not None:
            return sales_detail
        # Directory aggregations may contain the word "占比", but that is a
        # supported lookup projection rather than an unsupported sales
        # arithmetic operator. Dispatch them before the generic analysis gate.
        if assess_request(question).kind == "lookup":
            summary_detail = warehouse_type_summary_detail_request(question)
            if summary_detail is not None:
                return summary_detail
            type_count = warehouse_type_count_request(question)
            if type_count is not None:
                return type_count
            directory = warehouse_lookup_request(question)
            if directory is not None:
                return directory
        owner_inventory = self._classify_shape(question, source_question=question)
        if owner_inventory is not None and owner_inventory.route == "current_inventory_owner_summary":
            return owner_inventory
        availability_text = _compact(question)
        if (
            "库存" in availability_text
            and any(token in availability_text for token in ("近7天平均出库量", "近7天日均出库", "库存可用天数"))
            and "广州弘一" in availability_text
            and "当前" in availability_text
        ):
            sku_codes, _ = extract_sku_references(question)
            if len(sku_codes) == 1:
                now = self._now().replace(microsecond=0)
                scope = QueryScope(
                    owner="HJXH", warehouse_codes=("GZHYC-777",),
                    warehouse_labels=("广州弘一",), sku_codes=sku_codes,
                )
                component_plan = InventoryComponentPlan(
                    observed_at=now,
                    occupation_since=now - timedelta(days=60),
                    owner_codes=("HJXH",), warehouse_codes=("GZHYC-777",), sku_codes=sku_codes,
                )
                return SemanticRequest(
                    route="inventory_availability_detail",
                    start=(now - timedelta(days=7)).date(), end=now.date(), scope=scope,
                    inventory_availability_plan=InventoryAvailabilityPlan(
                        components=component_plan,
                        average_start=(now - timedelta(days=7)).date(), average_end=now.date(),
                    ),
                )
        quality_text = _compact(question)
        if (
            "hjxh" in quality_text
            and "按仓库" in quality_text
            and ("销售出库" in quality_text or "有效销售出库" in quality_text)
            and "销售订单数" in quality_text
            and ("吨数" in quality_text or "吨" in quality_text or "净重" in quality_text)
            and "调拨" not in quality_text
        ):
            day = self._single_day(question, default_yesterday=True)
            if day is not None:
                return SemanticRequest(
                    route="sales_outbound_warehouse_quality",
                    start=day,
                    end=day + timedelta(days=1),
                    scope=QueryScope(owner="HJXH"),
                    sales_warehouse_quality_plan=SalesWarehouseQualityPlan(
                        start=day, end=day + timedelta(days=1), owner="HJXH"
                    ),
                )
        explicit_customer_channel = price_group_request(question, planner=self)
        if explicit_customer_channel is not None:
            return explicit_customer_channel
        warehouse_fulfillment = warehouse_fulfillment_request(question, planner=self)
        if warehouse_fulfillment is not None:
            return warehouse_fulfillment
        dms_redemption = dms_redemption_summary_request(question, planner=self)
        if dms_redemption is not None:
            return dms_redemption
        if _with_analysis:
            time_series = sales_timeseries_request(question, planner=self)
            if time_series is not None:
                return time_series
            analysis = sales_analysis_request(question, planner=self)
            if analysis is not None:
                return analysis
            if re.search(r"占比|百分比|日均|升序|降序|从高到低|从低到高", question):
                # Existing TOP and period comparison paths own their operators.
                if _requested_top_n(question) is None and "对比" not in question and "比较" not in question and "周转" not in question:
                    return None
        comparison = warehouse_period_comparison(question, planner=self)
        if comparison is not None:
            return comparison
        channel_request = price_group_request(question, planner=self)
        if channel_request is not None:
            return channel_request
        if is_so07_question(question):
            return so07_request(question, planner=self,
                contract_root=active_snapshot(Path(__file__).resolve().parents[2])[0])
        if is_order_return_question(question):
            return return_request(question, planner=self,
                contract_root=active_snapshot(Path(__file__).resolve().parents[2])[0])
        if is_link_question(question):
            return link_request(question, interval_parser=self._source_interval,
                contract_root=active_snapshot(Path(__file__).resolve().parents[2])[0])
        if has_turnover_conflict(question):
            return None
        occupation_split = occupation_split_request(question, planner=self)
        if occupation_split is not None:
            return occupation_split
        # Apply routing-only synonyms before the ambiguity gate as well.  The
        # later semantic pass also normalizes them, but reaching this gate first
        # must not turn a reviewed synonym into a clarification.
        routing_question = re.sub(r"实际出货量|已完成出货量|完成出货量", "实际销售出库量", question)
        if "调拨" not in routing_question and unresolved_outbound_source(routing_question, resolver=self.scope_resolver) is not None:
            return None
        if '回钻' in question or ('成品' in question and '销售出库' in question and _requested_top_n(question) is not None):
            ranking = ranked_sales_request(question, interval_parser=self._source_interval,
                resolver=self.scope_resolver, contract_root=active_snapshot(Path(__file__).resolve().parents[2])[0])
            if ranking is not None or '回钻' in question:
                return ranking
        if 'dms' in question.casefold() and '城市经理' in question:
            return manager_order_request(question, interval_parser=self._source_interval,
                contract_root=active_snapshot(Path(__file__).resolve().parents[2])[0])
        dms_orders = points_order_request(question, interval_parser=self._source_interval,
            contract_root=active_snapshot(Path(__file__).resolve().parents[2])[0])
        if dms_orders is not None:
            return dms_orders
        assessment = assess_request(question)
        if name_quality_scope(question,self.scope_resolver) is not None:
            return name_quality_request(question,interval=self._source_interval(question),resolver=self.scope_resolver,
                                        contract_root=active_snapshot(Path(__file__).resolve().parents[2])[0])
        if assessment.kind not in {"data_query", "lookup"}:
            return None
        if assessment.kind == "lookup":
            summary_detail = warehouse_type_summary_detail_request(question)
            if summary_detail is not None:
                return summary_detail
            type_count = warehouse_type_count_request(question)
            if type_count is not None:
                return type_count
            return warehouse_lookup_request(question)
        if "箱规" in question and "出库" in question:
            return outbound_box_request(question,interval=self._source_interval(question),resolver=self.scope_resolver,
                                        contract_root=active_snapshot(Path(__file__).resolve().parents[2])[0])
        channel_boxes = filtered_outbound_box_request(
            question, interval=self._source_interval(question), resolver=self.scope_resolver,
            contract_root=active_snapshot(Path(__file__).resolve().parents[2])[0],
        )
        if channel_boxes is not None:
            return channel_boxes
        # Purchase-inbound natural language is broader than the production-date
        # detail templates below.  Route every supported inbound shape through
        # the reviewed semantic classifier first; otherwise a request such as
        # "本月采购入库数量最高的前10个SKU" falls through to the generic model
        # clarification path and can receive unrelated inventory/outbound
        # choices.
        if "采购入库" in question:
            inbound_shape = self._classify_shape(question, source_question=question)
            if inbound_shape is not None and inbound_shape.route == "inbound_summary":
                return inbound_shape
        if "采购入库" in question and ("明细" in question or "生产日期" in question):
            warehouse_receipt_summary = receipt_warehouse_production_summary_request(question, planner=self)
            if warehouse_receipt_summary is not None:
                return warehouse_receipt_summary
            receipt_summary = receipt_production_summary_request(question, planner=self)
            if receipt_summary is not None:
                return receipt_summary
            return inbound_detail_request(
                question, today=self._now().date(), resolver=self.scope_resolver,
                contract_root=active_snapshot(Path(__file__).resolve().parents[2])[0],
                interval_parser=self._source_interval,
            )
        general_shape = self._classify_shape(question, source_question=question)
        if general_shape is not None:
            # Specialized planners above own richer projections (for example
            # channel price groups and receipt production-date detail). The
            # remaining reviewed shapes are safe natural-language accelerators.
            return general_shape
        definition = finished_goods_definition(question, contract_root=active_snapshot(Path(__file__).resolve().parents[2])[0])
        if definition is None:
            return None
        planning_question, exceptions = definition
        # In this department's wording, "生产产品" means finished goods.
        # Normalize the alias before scope resolution so it cannot be treated
        # as an unregistered free-text product predicate.
        planning_question = re.sub(r"生产产品", "成品", planning_question)
        planning_question = re.sub(
            r"实际出货量|已完成出货量|完成出货量",
            "实际销售出库量",
            planning_question,
        )
        # These are common department phrasings for an ALL-warehouse scope and
        # a coordinated warehouse/SKU grain. Normalize them before both scope
        # validation and route classification.
        planning_question = re.sub(r"全仓", "全部仓库", planning_question)
        planning_question = re.sub(
            r"每个仓(?:库)?每个SKU|每仓每个SKU|各仓各SKU",
            "按仓库和SKU",
            planning_question,
            flags=re.IGNORECASE,
        )
        planning_question = re.sub(
            r"(?:每个仓库|各仓库)(?:、|，|,|和|与|及|\s)+(?:每个SKU|各SKU)"
            r"(?:、|，|,|和|与|及|\s)+(?:每个渠道|各渠道)",
            "按仓库和SKU和渠道",
            planning_question,
            flags=re.IGNORECASE,
        )
        if _requested_top_n(planning_question) is not None and any(
            word in planning_question for word in _SALES_OUTBOUND_ALIAS_WORDS
        ):
            planning_question = consume_sku_ranking_output(planning_question)
            planning_question = without_excluded_allocation_source(planning_question)
        # Consume command conjunctions, not business predicates or identifiers.
        planning_question = re.sub(r"并(?=列出|输出|展示|显示)", "", planning_question)
        # Dimension verbs are commands, not warehouse filters. Keeping them in
        # the scope residue makes valid phrases such as "按仓库计算周转"
        # look like unsupported business predicates.
        planning_question = re.sub(
            r"(按(?:仓库|各仓|仓库维度))(?:计算|统计|汇总|查看|查询)",
            r"\1",
            planning_question,
        )
        scope_codes, scope_question = extract_sku_references(planning_question)
        scope_question = GROUPS.sub("", scope_question)
        # Aggregation modifiers describe the requested output grain, not a
        # warehouse/SKU/customer predicate. Remove them before scope validation
        # so natural forms such as "按渠道细分" remain on the deterministic
        # aggregate route.
        scope_question = re.sub(r"(?:细分|汇总|全天|全日|履约)", "", scope_question)
        scope_question = re.sub(
            r"(?:各|每个|每)(?:仓库|仓|仓库维度|渠道|客户渠道)"
            r"(?:(?:和|与|及|、|\+)(?:仓库|SKU|货主|日期|类型|状态|渠道|客户渠道))*",
            "",
            scope_question,
            flags=re.IGNORECASE,
        )
        # These are measures requested in the output, not scope predicates.
        # Strip them only for scope validation; the original question remains
        # intact for route and projection planning.
        scope_question = re.sub(
            r"SKU(?:数|数量)|出库单(?:数|量|笔数|笔量)|单(?:数|量)|"
            r"实发(?:数量|量)|实际发了多少|实际发出(?:数量|量)?|实际发(?:数量|量)?",
            "", scope_question, flags=re.IGNORECASE,
        )
        scope_question = re.sub(r"(?:按仓库|按仓|各仓库|各仓)?(?:汇总|统计)", "", scope_question)
        scope_question = _remove_route_only_scope_clauses(scope_question)
        if "调拨" in planning_question:
            # Transfer is a movement business, not a warehouse/product scope
            # predicate. The transfer route consumes it below.
            scope_question = re.sub(r"调拨(?:出库|出货|发出)?", "", scope_question)
        # Explicit exclusions are output-basis instructions, not scope
        # predicates. Remove only the reviewed "do not use allocation"
        # clause before scope validation; otherwise a valid actual-shipment
        # request such as "实际发了多少，不要分配数量" is rejected as an
        # unsupported free-text condition.
        scope_question = without_excluded_allocation_source(scope_question)
        if scope_codes:
            scope_question += " SKU " + "、".join(f"`{code}`" for code in scope_codes)
        original_scope_question = _turnover_scope_question(planning_question)
        scope, shape, unsupported = self.scope_resolver.resolve(original_scope_question)
        if unsupported and scope_question != planning_question:
            cleaned_scope, _cleaned_shape, cleaned_unsupported = self.scope_resolver.resolve(
                _turnover_scope_question(scope_question)
            )
            if not cleaned_unsupported:
                # Keep the original shape for route classification. The second
                # pass only removes metric grammar from scope validation.
                scope, unsupported = cleaned_scope, False
        if unsupported:
            return None
        if exceptions:
            scope = replace(scope, finished_goods_exceptions=exceptions)
        # Batch routes already carry their explicit date and reviewed warehouse.
        _, semantic_question = extract_sku_references(planning_question)
        compact = _compact(semantic_question)
        ref_order_type = self._requested_order_type(compact)
        fulfillment_type = self._requested_fulfillment_type(compact)
        finished_goods_only = "产成品" in compact or "成品" in compact
        if finished_goods_only and not scope.finished_goods:
            # Formula cleanup may remove the noun from the scope residue; the
            # original semantic question still explicitly states finished
            # goods, so preserve that restriction on the attached scope.
            scope = replace(scope, finished_goods=True)
        if any(word in compact for word in _PRODUCTION_DATE_WORDS):
            # SKU reference extraction removes product words from the residue;
            # keep the original reviewed product phrase for batch inventory.
            shape = planning_question if any(word in _compact(planning_question) for word in _SAUSAGE_WORDS) else semantic_question
        elif "库存" in compact and "周转" not in compact and scope.product_terms:
            shape += "成品"
        classification_shape = shape
        if "周转" in compact:
            # Restore the consumed metric name without reintroducing resolved
            # scope as grouping grammar. Original dates/timing remain in
            # source_question and the independently resolved scope is attached.
            classification_shape = shape + "库存周转"
        request = self._classify_shape(classification_shape, source_question=semantic_question)
        if request is None:
            return None
        if exceptions and (request.route != "sales_aggregate" or request.sales_aggregation.dimensions != ("sku",)):
            return None
        return replace(
            request,
            scope=scope,
            warehouse_code=scope.warehouse_codes[0] if len(scope.warehouse_codes) == 1 else request.warehouse_code,
            warehouse_label="、".join(scope.warehouse_labels) or request.warehouse_label,
            product_keyword="sausage" if scope.product_terms else request.product_keyword,
            ref_order_type=ref_order_type or request.ref_order_type,
            fulfillment_type=fulfillment_type or request.fulfillment_type,
            finished_goods_only=finished_goods_only or request.finished_goods_only,
        )

    @staticmethod
    def _requested_order_type(compact: str) -> str | None:
        types = RequestPlanner._requested_order_types(compact)
        return types[0] if len(types) == 1 else None

    @staticmethod
    def _requested_order_types(compact: str) -> tuple[str, ...]:
        return tuple(dict.fromkeys(value.upper() for value in
            re.findall(r"(?<![a-z0-9])(jyck|hhck|bfck|qtck|so\d+)(?![a-z0-9])", compact)))

    @staticmethod
    def _requested_fulfillment_type(compact: str) -> str | None:
        has_2c = bool(re.search(r"2c", compact))
        has_2b = bool(re.search(r"2b", compact))
        if has_2c and not has_2b:
            return "2C"
        if has_2b and not has_2c:
            return "2B"
        return None

    @staticmethod
    def _requested_group_dimensions(compact: str) -> tuple[str, ...]:
        """Keep every dimension in a coordinated ``按 A 和 B`` phrase."""
        return requested_dimensions(compact)

    def _classify_shape(self, question: str, *, source_question: str | None = None) -> SemanticRequest | None:
        """Return a route only when the question has an unambiguous shape."""

        if not isinstance(question, str):
            return None
        compact = _compact(question)
        date_question = source_question or question
        if not compact:
            return None
        ref_order_type = self._requested_order_type(compact)
        order_types = self._requested_order_types(compact)
        finished_goods_only = "产成品" in compact or "成品" in compact
        negative_signal_text = re.sub(
            r"(?:不含|不计|不纳入|不扣除|不扣|排除|剔除)退货",
            "",
            compact,
        )
        has_negative_sales_signal = any(
            word in negative_signal_text for word in _SALES_OUTBOUND_NEGATIVE_WORDS
        )

        # DMS points redemption has a different order system and quantity
        # basis from ordinary sales outbound. Require an explicit DMS marker,
        # a bounded month range, and a registered product alias before routing.
        if (
            any(word in compact for word in _DMS_MARKER_WORDS)
            and any(word in compact for word in _DMS_POINTS_WORDS)
            and any(word in compact for word in _DMS_AIR_FRYER_WORDS)
        ):
            month_range = self._parse_month_range(question)
            interval = month_range or self._source_interval(question)
            if interval is not None:
                start, end = interval
                return SemanticRequest(
                    route="dms_points_redemption",
                    start=start,
                    end=end,
                    product_code="8030101",
                    product_name="皇家小虎空气炸锅5L",
                )

        # Transfer outbound is a distinct movement source.  It must not fall
        # through to the sales-outbound contract, which requires a sales order
        # and would silently omit DBCK documents.
        if "调拨" in compact and re.search(r"出库|出货|发出", compact):
            interval = self._source_interval(question)
            if interval is not None and interval[1] > interval[0] and not any(
                word in compact for word in ("订单", "计划", "请求量", "分配量")
            ):
                return SemanticRequest(
                    route="transfer_outbound",
                    start=interval[0],
                    end=interval[1],
                    show_warehouses=any(word in compact for word in _WAREHOUSE_WORDS),
                )

        # Batch inventory is checked before the generic inventory routes so a
        # sausage production-date question cannot fall through to a broad stock
        # answer.
        if (
            any(word in compact for word in ("库存", "存量", "有多少", "数量"))
            and "周转" not in compact
            and ("长沙黄兴" in compact or "黄兴" in compact)
            and any(word in compact for word in _PRODUCTION_DATE_WORDS)
            and self._parse_explicit_date(question) is not None
        ):
            production_date = self._parse_explicit_date(question)
            assert production_date is not None
            return SemanticRequest(
                route="batch_inventory",
                start=production_date,
                end=production_date + timedelta(days=1),
                production_date=production_date,
                product_keyword=("finished_goods" if "成品" in compact or "产成品" in compact
                                 else None if any(word in compact for word in _SAUSAGE_WORDS)
                                 else "all_products"),
                finished_goods_only="成品" in compact or "产成品" in compact,
            )

        # The named warehouse and product family are both explicit here, so
        # the approved current finished-goods contract can be executed without
        # asking the model to guess a SKU or warehouse mapping.
        has_current_inventory = (
            "库存" in compact
            and any(word in compact for word in ("当前", "实时", "现在", "现有"))
        )
        if (
            has_current_inventory
            and "周转" not in compact
            and "成品" in compact
            and ("按货主" in compact or "货主编码汇总" in compact)
            and "在途" in compact
            and "占比" in compact
            and not any(word in compact for word in _SAUSAGE_WORDS)
        ):
            today = self._now().date()
            return SemanticRequest(route="current_inventory_owner_summary", start=today, end=today + timedelta(days=1))
        if (
            has_current_inventory
            and "周转" not in compact
            and any(word in compact for word in _SAUSAGE_WORDS)
            and ("长沙黄兴" in compact or "黄兴" in compact)
        ):
            today = self._now().date()
            return SemanticRequest(
                route="current_inventory",
                start=today,
                end=today + timedelta(days=1),
                product_keyword="sausage",
                warehouse_code=self.changsha_huangxing_wms,
                warehouse_label="长沙黄兴",
            )

        if (
            has_current_inventory
            and "周转" not in compact
            and "成品" in compact
            and "可用" not in compact
            and not any(word in compact for word in _SAUSAGE_WORDS)
        ):
            today = self._now().date()
            return SemanticRequest(
                route="current_inventory",
                start=today,
                end=today + timedelta(days=1),
                show_warehouses=any(word in compact for word in _WAREHOUSE_WORDS),
            )

        if "周转" in compact and "库存" in compact:
            if has_negative_sales_signal:
                return None
            timing_question = _compact(date_question)
            historical_snapshot = bool(_TURNOVER_SNAPSHOT_WORDS.search(timing_question))
            if historical_snapshot and any(word in timing_question for word in ("当前", "实时", "现在", "现有")):
                return None
            if (
                not historical_snapshot
                and any(word in compact for word in ("各仓", "按仓", "每仓", "每个仓", "中心仓", "仓库维度"))
            ):
                interval = self._source_interval(date_question)
                if interval is not None:
                    start, end = interval
                    if end <= start or end > self._now().date():
                        return None
                    return SemanticRequest(
                        route="realtime_turnover_by_warehouse",
                        start=start,
                        end=end,
                        turnover_grain="warehouse",
                    )
            if (
                any(word in compact for word in _SAUSAGE_WORDS)
                or any(word in compact for word in ("长沙黄兴", "黄兴"))
                or any(
                    word in compact
                    for word in (
                        "各仓",
                        "按仓",
                        "分仓",
                        "仓库维度",
                        "各sku",
                        "按sku",
                        "商品维度",
                        "渠道",
                        "平台",
                        "货主",
                    )
                )
            ):
                # The direct turnover route is reconciled only at total grain.
                # Never discard a narrower warehouse, SKU, product, channel, or
                # owner scope merely because the aggregate formula is known.
                return None
            now_date = self._now().date()
            if historical_snapshot and self._has_separate_turnover_dates(date_question):
                return None
            interval = self._source_interval(date_question)
            if interval is not None:
                start, end = interval
                if end > now_date or end <= start:
                    return None
                realtime = not historical_snapshot
                return SemanticRequest(
                    route="realtime_turnover" if realtime else "historical_turnover",
                    start=start,
                    end=end,
                )
            if "上月" in compact or "上个月" in compact:
                year, month = now_date.year, now_date.month - 1
                if month == 0:
                    year, month = year - 1, 12
                next_year, next_month = _next_month(year, month)
                return SemanticRequest(
                    route="realtime_turnover",
                    start=date(year, month, 1),
                    end=date(next_year, next_month, 1),
                )
            # A specifically named month is also deterministic.  Current or
            # realtime wording is deliberately left to the general route,
            # because its numerator variant is different.
            explicit_month = self._parse_explicit_month(question)
            if explicit_month is not None:
                next_year, next_month = _next_month(
                    explicit_month.year, explicit_month.month
                )
                end = date(next_year, next_month, 1)
                if end <= now_date:
                    return SemanticRequest(
                        route="realtime_turnover",
                        start=explicit_month,
                        end=end,
                    )
                return None
            if any(word in compact for word in ("当前", "实时", "本月", "最近", "近", "过去")):
                interval = self._source_interval(date_question)
                if interval is not None and interval[1] > interval[0]:
                    return SemanticRequest(
                        route="realtime_turnover",
                        start=interval[0],
                        end=interval[1],
                    )
            return None

        interval = self._source_interval(date_question)
        day = interval[0] if interval is not None and (interval[1] - interval[0]).days == 1 else None
        has_warehouse = any(word in compact for word in _WAREHOUSE_WORDS)
        top_n = _requested_top_n(question)
        has_sku = any(word in compact for word in _SKU_WORDS) or (
            top_n is not None and finished_goods_only
        )
        has_explicit_outbound = (
            any(word in compact for word in _SALES_OUTBOUND_ALIAS_WORDS)
            or ("出库" in compact and ("销售" in compact or "实际" in compact))
            or ("出库" in compact and bool(order_types))
            or ("销售" in compact and "完成量" in compact)
            or (
                "发货" in compact
                and any(word in compact for word in ("销售", "完成", "实际"))
            )
        )
        # A fulfillment pair supplies a dimension, never the quantity basis.
        has_fulfillment_pair = "2c" in compact and "2b" in compact
        grouped_fulfillment = has_fulfillment_pair and (
            has_warehouse or "分别" in compact or "履约" in compact
            or "按类型" in compact or "类型" in compact
        )
        # A ranked SKU request supplies enough shape to use the reviewed actual
        # outbound ranking. A scalar or warehouse-level generic "发货量" still
        # needs the quantity-basis choice required by the semantic contract.
        has_ranked_generic_outbound = (
            has_sku
            and top_n is not None
            and any(word in compact for word in _GENERIC_OUTBOUND_WORDS)
        )
        has_outbound = has_explicit_outbound or has_ranked_generic_outbound
        filter_shape = re.sub(r"并说明状态为(?:700|900|701|901)(?:或(?:700|900|701|901))*但数量为0的出库单是否应该保留", "", compact)
        # The canonical completed statuses are already part of every actual
        # outbound handler. Accept an explicit spelling of that exact set, but
        # keep rejecting arbitrary numeric predicates that would change the
        # population without a reviewed route parameter.
        status_clauses = re.findall(
            r"(?:出库)?状态\s*[:：=]?\s*(\d+(?:[、,，/]\d+)*)",
            filter_shape,
        )
        canonical_statuses = {str(value) for value in ACTUAL_OUTBOUND_STATUS_CODES}
        exact_completed_status = bool(status_clauses) and all(
            set(re.split(r"[、,，/]", clause)) == canonical_statuses
            for clause in status_clauses
        )
        status_free_shape = re.sub(
            r"(?:出库)?状态\s*[:：=]?\s*\d+(?:[、,，/]\d+)*",
            "",
            filter_shape,
        )
        if has_outbound and re.search(r"(?:状态|数量)(?:为|是|[:=<>≥≤])*\d", status_free_shape):
            return None
        if has_outbound and status_clauses and not exact_completed_status:
            return None
        coordinated_dimensions = self._requested_group_dimensions(compact)
        grouped_order_types = bool(re.search(r"按(?:出库)?类型|各(?:出库)?类型|类型汇总", compact))
        all_outbound_types = bool(re.search(r"(?:所有|全部)(?:的)?出库类型", compact))
        if len(order_types) > 1 and top_n is not None:
            return None
        # Ranking is an operator on the reviewed SKU aggregation. The explicit
        # quantity + distinct-outbound-count projection is already implemented
        # by this executor; it must not be rejected by the generic count branch.
        sku_ranking = (
            interval is not None and has_outbound and has_sku and top_n is not None
            and not has_negative_sales_signal and not has_warehouse
            and set(coordinated_dimensions).issubset({"sku"})
            and not grouped_order_types and not has_fulfillment_pair
            and not re.search(r"订单|吨|箱|日均|平均|金额|升序|从低到高|最低|最少|倒数", compact)
            and not re.search(r"分配(?:数量|量)|计划(?:数量|量)?|请求(?:数量|量)?|预计(?:数量|量)?|应发(?:数量|量)?|预估(?:数量|量)?", compact)
            and not re.search(r"按(?:出库单数|出库单量|单数|单量)|(?:出库单数|出库单量|单数|单量)(?:top|前|最高|最多)", compact)
        )
        if sku_ranking:
            return SemanticRequest(
                route="sales_outbound_top_sku", start=interval[0], end=interval[1],
                show_top_sku=True, top_n=top_n, ref_order_type=ref_order_type,
                fulfillment_type=self._requested_fulfillment_type(compact),
                finished_goods_only=finished_goods_only,
            )
        type_fulfillment_fast = (
            grouped_order_types
            and not order_types
            and not has_warehouse
            and set(coordinated_dimensions) == {"ref_order_type"}
            and re.search(r"2c", compact)
            and re.search(r"2b", compact)
        )
        if ((order_types and top_n is None) or len(coordinated_dimensions) > 1
                or (grouped_order_types and not type_fulfillment_fast)
                or "customer_channel" in coordinated_dimensions
                or (grouped_fulfillment and not type_fulfillment_fast)
                or re.search(r"出库单数|出库单量|单数|单量|SKU数|SKU数量|按日期|每日|每天|按天|按(?:出库)?状态|按sku|各sku|sku维度", compact, re.IGNORECASE)):
            if (interval is None or not has_explicit_outbound or has_negative_sales_signal or "订单" in compact
                or top_n is not None or re.search(r"吨|箱", filter_shape)):
                return None
            dimensions = []
            if re.search(r"按日期|每日|每天|按天", compact):
                dimensions.append("ship_date")
            if re.search(r"按货主|各货主|货主维度", compact):
                dimensions.append("cargo_owner")
            if has_warehouse:
                dimensions.append("warehouse")
            if re.search(r"按(?:出库)?状态|各(?:出库)?状态", compact):
                dimensions.append("shipment_status")
            if grouped_order_types:
                dimensions.append("ref_order_type")
            if "2c" in compact and "2b" in compact:
                dimensions.append("fulfillment_type")
            if re.search(r"按sku|各sku|sku维度", compact):
                dimensions.append("sku")
            dimensions.extend(value for value in coordinated_dimensions if value not in dimensions)
            aggregation = SalesAggregation(tuple(dimensions),
                fulfillment_type=self._requested_fulfillment_type(compact), ref_order_types=order_types,
                include_sku_count=bool(re.search(r"SKU数|SKU数量|sku数|sku数量", compact, re.IGNORECASE)),
                include_zero_document_count=bool(
                    re.search(r"(?:零数量|数量为0|实发总量为0).*?(?:出库单数|出库单|单据)", compact)
                ))
            return SemanticRequest(
                route="sales_aggregate", start=interval[0], end=interval[1],
                show_warehouses=has_warehouse,
                all_outbound_types=all_outbound_types,
                sales_aggregation=aggregation,
            )
        # Quantity-only grouped requests use the same reconciled aggregate
        # contract. Keep this branch before the generic interval route so a
        # phrase such as "最近14天实际销售出库，按类型汇总" cannot fall back
        # to an unrelated warehouse summary.
        if interval is not None and (interval[1] - interval[0]).days > 1 and has_outbound and not has_negative_sales_signal:
            grouped_dimensions = []
            if grouped_order_types:
                grouped_dimensions.append("ref_order_type")
            if "customer_channel" in coordinated_dimensions:
                grouped_dimensions.append("customer_channel")
            if re.search(r"按货主|各货主|货主维度", compact):
                grouped_dimensions.append("cargo_owner")
            if grouped_dimensions:
                aggregation = SalesAggregation(tuple(dict.fromkeys(grouped_dimensions)), ref_order_type, self._requested_fulfillment_type(compact))
                return SemanticRequest(
                    route="sales_aggregate", start=interval[0], end=interval[1],
                    sales_aggregation=aggregation,
                )
        grouped_by_type = (
            "按类型" in compact
            or ("类型" in compact and ("2c" in compact or "2b" in compact))
        )
        grouped_by_owner = any(word in compact for word in ("按货主", "各货主", "货主维度"))
        if grouped_by_owner:
            if interval is None or not has_outbound or has_negative_sales_signal or grouped_by_type or has_warehouse or has_sku:
                return None
            return SemanticRequest(
                route="sales_outbound_by_owner",
                start=interval[0], end=interval[1],
                fulfillment_type=self._requested_fulfillment_type(compact),
            )
        if (
            day is not None
            and has_outbound
            and grouped_by_type
            and not has_negative_sales_signal
        ):
            return SemanticRequest(
                route="sales_outbound_by_type",
                start=day,
                end=day + timedelta(days=1),
                fulfillment_type=self._requested_fulfillment_type(compact),
            )
        if (
            day is not None
            and has_outbound
            and has_sku
            and top_n is not None
            and not has_negative_sales_signal
        ):
            return SemanticRequest(
                route="sales_outbound_top_sku",
                start=day,
                end=day + timedelta(days=1),
                show_top_sku=True,
                top_n=top_n,
                ref_order_type=ref_order_type,
                finished_goods_only=finished_goods_only,
            )
        if (
            day is not None
            and has_outbound
            and has_warehouse
            and not has_negative_sales_signal
        ):
            return SemanticRequest(
                route="sales_outbound_by_warehouse",
                start=day,
                end=day + timedelta(days=1),
                show_warehouses=True,
            )

        # The same reviewed sales contract also supports a complete-natural-day
        # interval.  This covers requests such as "最近7个完整自然日销售出库"
        # without making the model rediscover date boundaries.
        interval = self._source_interval(date_question)
        if interval is not None and has_outbound and not has_negative_sales_signal:
            interval_start, interval_end = interval
            if interval_end > interval_start:
                if has_sku and top_n is not None and not has_negative_sales_signal:
                    return SemanticRequest(
                        route="sales_outbound_top_sku",
                        start=interval_start,
                        end=interval_end,
                        show_top_sku=True,
                        top_n=top_n,
                    )
                return SemanticRequest(
                    route="sales_outbound_by_warehouse",
                    start=interval_start,
                    end=interval_end,
                    show_warehouses=has_warehouse,
                )

        if any(word in compact for word in _INBOUND_WORDS):
            if _has_unimplemented_inbound_condition(question):
                return None
            is_top = any(word in compact for word in _TOP_WORDS) or "sku" in compact
            # A ranked SKU request owns the output grain even when the user
            # says "数量". Do not let the measure word turn Top-N SKU into a
            # warehouse report.
            is_by_warehouse = has_warehouse or (
                not is_top and ("吨" in compact or "数量" in compact)
            )
            if is_top or is_by_warehouse:
                inbound_interval = self._source_interval(date_question)
                if inbound_interval is None:
                    if re.search(r"\d|月|周|年|至|到", question):
                        return None
                    inbound_day = self._single_day(question, default_yesterday=True)
                    assert inbound_day is not None
                    inbound_interval = (inbound_day, inbound_day + timedelta(days=1))
                return SemanticRequest(
                    route="inbound_summary",
                    start=inbound_interval[0],
                    end=inbound_interval[1],
                    show_warehouses=is_by_warehouse,
                    show_top_sku=is_top,
                    top_n=top_n,
                    inbound_basis="inbound_type_10" if "采购入库" in compact else None,
                )
        return None

    def _has_separate_turnover_dates(self, question: str) -> bool:
        # A snapshot date plus a separate sales interval cannot share the
        # period-end route's single date interval. Preserve opaque SKU codes.
        _, question = extract_sku_references(question)
        without_dates = _ISO_DATE_PATTERN.sub("", question)
        without_dates = _DATE_PATTERN.sub("", without_dates)
        return without_dates != question and (
            self._explicit_interval(question) is None
            or self._source_interval(without_dates) is not None
        )

    def _explicit_interval(self, question: str) -> tuple[date, date] | None:
        """Parse one date or one inclusive date range from user text."""

        # An unseparated numeric clock can hide one range endpoint from the
        # date regex. Do not silently interpret the other endpoint as one day.
        if re.search(r"(?<!\d)\d{4}[-/.]\d{1,2}[-/.]\d{3,}", question):
            return None
        raw_matches: list[tuple[int, int, int | None, int, int]] = []
        for pattern in _INTERVAL_DATE_PATTERNS:
            for match in pattern.finditer(question):
                try:
                    year_text = match.group(1)
                    year = int(year_text) if year_text else None
                    month = int(match.group(2))
                    day = int(match.group(3))
                except (TypeError, ValueError):
                    raise SemanticRuntimeError("日期范围无效，请重新提交", reason="date_invalid") from None
                raw_matches.append((match.start(), match.end(), year, month, day))
        raw_matches.sort(key=lambda item: (item[0], item[1]))
        if not raw_matches:
            return None
        connector = ""
        if len(raw_matches) == 2:
            connector = question[raw_matches[0][1]:raw_matches[1][0]]
            start_clock = _CLOCK_PREFIX.match(connector)
            if start_clock:
                if not _midnight_clock(start_clock.group()):
                    return None
                connector = connector[start_clock.end():]
            if _INTERVAL_CONNECTOR.fullmatch(connector) is None:
                return None
        elif len(raw_matches) != 1:
            return None
        matches: list[tuple[int, int, date]] = []
        for index, (start, end, year, month, day) in enumerate(raw_matches):
            if year is None:
                if index:
                    previous = matches[index - 1][2]
                    year = previous.year + int((month, day) < (previous.month, previous.day))
                else:
                    year = self._now().year
            try:
                matches.append((start, end, date(year, month, day)))
            except ValueError as exc:
                raise SemanticRuntimeError("日期范围无效，请重新提交", reason="date_invalid") from exc
        if len(matches) == 1:
            if _CLOCK_PREFIX.match(question[matches[0][1]:]):
                return None
            value = matches[0][2]
            return value, value + timedelta(days=1)
        start, end_date = matches[0][2], matches[1][2]
        if end_date < start:
            raise SemanticRuntimeError("日期范围无效，请重新提交", reason="date_invalid")
        end_clock = _CLOCK_PREFIX.match(question[matches[1][1]:])
        if end_clock and not _midnight_clock(end_clock.group()):
            return None
        midnight_end = end_clock is not None
        # Date-only inclusive ranges and explicit midnight-exclusive ranges
        # are different requests. Never include the terminal day in the latter.
        end = end_date + timedelta(days=0 if midnight_end else 1)
        return (start, end) if end > start else None

    def _source_interval(self, question: str) -> tuple[date, date] | None:
        _, question = extract_sku_references(question)
        compact = _compact(question)
        explicit = self._explicit_interval(question)
        if explicit is not None:
            return explicit
        if any(pattern.search(question) for pattern in _INTERVAL_DATE_PATTERNS):
            # An unrepresented explicit scope must never become a whole month.
            return None
        month_range = self._parse_month_range(question)
        if month_range is not None:
            return month_range
        explicit_month = self._parse_explicit_month(question)
        if explicit_month is not None:
            year, month = _next_month(explicit_month.year, explicit_month.month)
            return explicit_month, date(year, month, 1)
        now_date = self._now().date()
        # A calendar period with an explicit relative cutoff is resolved before
        # the broad period branches below. The end remains exclusive.
        cutoff_today = any(marker in compact for marker in ("截至今天", "截止今天", "截至今日", "截止今日", "今天00:00", "今天0点"))
        cutoff_yesterday = any(marker in compact for marker in ("截至昨天", "截止昨天", "截至昨日", "截止昨日"))
        if cutoff_today or cutoff_yesterday:
            end_date = now_date if cutoff_today else now_date
            if "本月" in compact:
                return date(now_date.year, now_date.month, 1), end_date
            if "本周" in compact or "这周" in compact or "这个星期" in compact:
                return now_date - timedelta(days=now_date.weekday()), end_date
        quarter_start_month = ((now_date.month - 1) // 3) * 3 + 1
        current_quarter = date(now_date.year, quarter_start_month, 1)
        numbered_quarter = re.search(r"(?:(\d{4})年)?第([1-4])季度", compact)
        if numbered_quarter:
            year = int(numbered_quarter.group(1) or now_date.year)
            start_month = (int(numbered_quarter.group(2)) - 1) * 3 + 1
            start = date(year, start_month, 1)
            next_year, next_month = _next_month(year, start_month + 2)
            end = date(next_year, next_month, 1)
            return (start, end) if end <= now_date else None
        relative_period = re.search(r"(?:最近|近|过去)(\d{1,3})个?(周|星期|月)", compact)
        if relative_period:
            count = int(relative_period.group(1))
            if count < 1 or count > 120:
                return None
            if relative_period.group(2) in {"周", "星期"}:
                monday = now_date - timedelta(days=now_date.weekday())
                return monday - timedelta(days=7 * count), now_date
            year, month = now_date.year, now_date.month - count
            while month <= 0:
                year, month = year - 1, month + 12
            return date(year, month, 1), date(now_date.year, now_date.month, 1)
        if "上上季度" in compact or "前两个季度" in compact:
            previous_month = quarter_start_month - 6
            previous_year = now_date.year
            if previous_month <= 0:
                previous_year -= 1
                previous_month += 12
            start = date(previous_year, previous_month, 1)
            return start, date(previous_year, previous_month + 3, 1) if previous_month <= 9 else date(previous_year + 1, (previous_month + 3) % 12 or 12, 1)
        if "上季度" in compact or "上个季度" in compact:
            previous_month = quarter_start_month - 3
            previous_year = now_date.year
            if previous_month <= 0:
                previous_year -= 1
                previous_month += 12
            return date(previous_year, previous_month, 1), current_quarter
        if "本季度" in compact or "这个季度" in compact:
            return current_quarter, now_date
        if "去年" in compact or "上年度" in compact or "上一年度" in compact:
            return date(now_date.year - 1, 1, 1), date(now_date.year, 1, 1)
        if "今年" in compact or "本年度" in compact:
            return date(now_date.year, 1, 1), now_date
        if "上上周" in compact or "前两周" in compact:
            monday = now_date - timedelta(days=now_date.weekday())
            return monday - timedelta(days=14), monday - timedelta(days=7)
        if "上周" in compact:
            monday = now_date - timedelta(days=now_date.weekday())
            return monday - timedelta(days=7), monday
        if "本周" in compact or "这周" in compact or "这个星期" in compact:
            monday = now_date - timedelta(days=now_date.weekday())
            return monday, now_date
        if "上上月" in compact or "上上个月" in compact or "前两个月" in compact:
            year, month = now_date.year, now_date.month - 2
            while month <= 0:
                year, month = year - 1, month + 12
            next_year, next_month = _next_month(year, month)
            return date(year, month, 1), date(next_year, next_month, 1)
        if "上月" in compact or "上个月" in compact or "上个自然月" in compact:
            year, month = now_date.year, now_date.month - 1
            if month == 0:
                year, month = year - 1, 12
            next_year, next_month = _next_month(year, month)
            return date(year, month, 1), date(next_year, next_month, 1)
        if "本月" in compact:
            return date(now_date.year, now_date.month, 1), now_date
        windows = list(recent_day_windows(question))
        if windows:
            counts = {days for _match, days in windows}
            if None in counts or len(counts) != 1:
                return None
            return now_date - timedelta(days=counts.pop()), now_date
        day = self._single_day(question)
        if day is not None:
            return day, day + timedelta(days=1)
        return None

    def _selected_interval(
        self,
        source_question: str,
        selected: Mapping[str, str],
    ) -> tuple[date, date] | None:
        window = next(
            (
                selected.get(question_id)
                for question_id in ("sales_window", "time_window", "window", "snapshot")
                if selected.get(question_id)
            ),
            None,
        )
        now_date = self._now().date()
        if window in {"last_7_complete_days", "last_7_days"}:
            return now_date - timedelta(days=7), now_date
        if window in {"last_30_complete_days", "last_30_days"}:
            return now_date - timedelta(days=30), now_date
        if window in {"mtd_complete_days", "mtd_to_today_00"}:
            return date(now_date.year, now_date.month, 1), now_date
        if window == "previous_natural_month":
            year, month = now_date.year, now_date.month - 1
            if month == 0:
                year, month = year - 1, 12
            next_year, next_month = _next_month(year, month)
            return date(year, month, 1), date(next_year, next_month, 1)
        if window in {"today"}:
            return now_date, now_date + timedelta(days=1)
        if window in {"yesterday"}:
            yesterday = now_date - timedelta(days=1)
            return yesterday, now_date
        if window in {"date", "explicit_period", "custom_period"}:
            return self._source_interval(source_question)
        return self._source_interval(source_question)

    @staticmethod
    def _selected_grain(
        source_question: str,
        selected: Mapping[str, str],
    ) -> str:
        chosen = selected.get("output_grain")
        if chosen:
            return chosen
        compact = _compact(source_question)
        if any(token in compact for token in ("各仓", "按仓", "仓库维度", "分仓")):
            return "warehouse"
        if any(token in compact for token in ("各sku", "按sku", "商品维度")):
            return "sku"
        return "total"

    @staticmethod
    def _selection_scope_is_unsupported(source_question: str, *, kind: str) -> bool:
        compact = _compact(source_question)
        if any(word in compact for word in _SALES_OUTBOUND_NEGATIVE_WORDS):
            return True
        if any(word in compact for word in ("dms", "积分兑换", "积分订单")):
            return True
        if kind == "outbound":
            # A selected aggregate route cannot silently discard a named SKU,
            # product family, warehouse, or channel predicate.
            if any(word in compact for word in _SAUSAGE_WORDS):
                return True
            if any(word in compact for word in ("长沙黄兴", "黄兴")):
                return True
            if any(word in compact for word in ("渠道", "平台", "货主")):
                return True
        return False

    def plan_selected(
        self,
        source_question: str | None,
        selections: Sequence[Mapping[str, Any]] | None,
        *,
        context_reused: bool = False,
        reference_time: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Execute a reviewed UI selection without a second model interpretation.

        This is intentionally conservative.  Returning ``None`` means the
        selected combination needs a broader governed contract or the model
        path; it never means that an unverified number may be fabricated.
        """

        if reference_time is not None:
            if not isinstance(reference_time, datetime) or reference_time.tzinfo is None or reference_time.utcoffset() is None:
                raise ValueError("an aware selection reference time is required")
            # A local planner avoids changing the shared runtime clock for other users.
            bound = RequestPlanner(timezone_name=self._zone.key, clock=lambda: reference_time, scope_resolver=self.scope_resolver)
            return bound.plan_selected(source_question, selections, context_reused=context_reused)
        if not isinstance(source_question, str) or not source_question.strip():
            return None
        selected = _normalize_selected_answers(selections)
        if not selected:
            return None

        source = source_question.strip()
        if is_so07_question(source) or set(selected) & set(SO07_DECISIONS):
            return so07_request(source, planner=self, selected=selected,
                contract_root=active_snapshot(Path(__file__).resolve().parents[2])[0])
        if is_order_return_question(source) or set(selected) & set(RETURN_DECISIONS):
            return return_request(source, planner=self, selected=selected,
                contract_root=active_snapshot(Path(__file__).resolve().parents[2])[0])
        if is_link_question(source) or "outbound_link_scope" in selected:
            return link_request(source, interval_parser=self._source_interval, selected=selected,
                contract_root=active_snapshot(Path(__file__).resolve().parents[2])[0])
        if "purchase_basis" in selected:
            return receipt_production_summary_request(
                source,
                planner=self,
                selected_purchase_basis=selected.get("purchase_basis"),
            )
        if has_turnover_conflict(source) or set(selected) & set(TURNOVER_CONFLICT_DECISIONS):
            return None
        if set(selected) & set(OUTBOUND_DECISIONS):
            day = self._now().date() - timedelta(days=1)
            return selected_outbound_request(source, selected, interval=(day, day + timedelta(days=1)), resolver=self.scope_resolver)
        if 'sku_name_outbound_basis' in selected:
            if selected != {'sku_name_outbound_basis':'sku_name_actual'}:
                return None
            return name_quality_request(source,interval=self._source_interval(source),resolver=self.scope_resolver,
                                        contract_root=active_snapshot(Path(__file__).resolve().parents[2])[0],selected=True)
        scope, _shape, unsupported = self.scope_resolver.resolve(_turnover_scope_question(source))
        if unsupported:
            return None
        compact = _compact(source)
        metric = selected.get("metric")
        selected_values = set(selected.values())
        if any(
            value in selected_values
            for value in (
                "provide_business_definition",
                "other_basis",
                "order_requested_quantity",
                "channel_replacement",
                "current_available_realtime",
                "inventory_available_realtime",
                "inventory_snapshot_quantity",
            )
        ):
            return None

        turnover_point = selected.get("turnover_inventory_point")
        is_turnover = (
            "周转" in compact
            or "turnover" in compact
            or turnover_point is not None
            or metric
            in {
                "inventory_turnover_finished_goods_snapshot",
                "inventory_turnover_finished_goods_realtime",
                "inventory_turnover_mtd_realtime",
            }
        )
        outbound_selected = any(
            selected.get(question_id) == "actual_completed_outbound"
            for question_id in ("metric", "outbound_basis", "shipment_basis", "shipment_quantity_basis")
        )
        inventory_selected = any(
            selected.get(question_id) == "current_realtime_finished_goods"
            for question_id in ("metric", "inventory_quantity_basis", "inventory_basis")
        )
        dms_selected = metric == "dms_points_redemption_air_fryer"

        if dms_selected:
            interval = self._parse_month_range(source) or self._selected_interval(source, selected)
            if interval is None or interval[1] <= interval[0]:
                return None
            request = SemanticRequest(
                route="dms_points_redemption",
                start=interval[0],
                end=interval[1],
                product_code="8030101",
                product_name="皇家小虎空气炸锅5L",
            )
            return replace(request, scope=scope)

        if is_turnover:
            if self._selection_scope_is_unsupported(source, kind="turnover"):
                return None
            if _TURNOVER_SNAPSHOT_WORDS.search(compact) and self._has_separate_turnover_dates(source):
                return None
            interval = self._selected_interval(source, selected)
            if interval is None or interval[1] <= interval[0] or interval[1] > self._now().date():
                return None
            grain = self._selected_grain(source, selected)
            # The scalar turnover route is the only released presentation at
            # this boundary.  Never show a total while the user selected a
            # lower grain that has not been independently reconciled.
            if grain != "total":
                return None
            variant = turnover_point or metric
            if variant in {
                "period_end_finished_goods_snapshot",
                "inventory_turnover_finished_goods_snapshot",
            }:
                request = SemanticRequest(
                    route="historical_turnover",
                    start=interval[0],
                    end=interval[1],
                )
            elif variant in {
                "current_realtime_finished_goods",
                "inventory_turnover_finished_goods_realtime",
                "inventory_turnover_mtd_realtime",
            }:
                request = SemanticRequest(
                    route="realtime_turnover",
                    start=interval[0],
                    end=interval[1],
                )
            else:
                # A turnover question without a named numerator is still
                # ambiguous; the caller will turn this into a non-looping
                # blocked response.
                return None
            return replace(request, scope=scope)

        if outbound_selected or metric in {"sales_outbound_actual", "actual_completed_outbound"}:
            if self._selection_scope_is_unsupported(source, kind="outbound"):
                return None
            interval = self._selected_interval(source, selected)
            if interval is None or interval[1] <= interval[0]:
                return None
            grain = self._selected_grain(source, selected)
            if grain not in {"total", "warehouse"}:
                return None
            request = SemanticRequest(
                route="sales_outbound_by_warehouse",
                start=interval[0],
                end=interval[1],
                show_warehouses=grain == "warehouse",
            )
            return replace(request, scope=scope)

        if inventory_selected or metric in {"current_realtime_finished_goods"}:
            if any(word in compact for word in ("历史", "快照", "生产日期", "在途")):
                return None
            classified = self.classify(source)
            if any(
                word in compact
                for word in (
                    "sku",
                    "商品",
                    "货主",
                    "渠道",
                    "平台",
                    "长沙黄兴",
                    "黄兴",
                    *_SAUSAGE_WORDS,
                )
            ) and (classified is None or classified.route != "current_inventory"):
                return None
            grain = self._selected_grain(source, selected)
            if grain not in {"total", "warehouse"}:
                return None
            today = self._now().date()
            request = SemanticRequest(
                route="current_inventory",
                start=today,
                end=today + timedelta(days=1),
                product_keyword=(
                    classified.product_keyword
                    if classified is not None and classified.route == "current_inventory"
                    else None
                ),
                warehouse_code=(
                    classified.warehouse_code
                    if classified is not None and classified.route == "current_inventory"
                    else None
                ),
                warehouse_label=(
                    classified.warehouse_label
                    if classified is not None and classified.route == "current_inventory"
                    else None
                ),
                show_warehouses=grain == "warehouse",
            )
            return replace(request, scope=scope)
        return None

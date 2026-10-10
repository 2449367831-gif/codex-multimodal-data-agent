from __future__ import annotations

from datetime import datetime, time, timedelta
import re
from typing import Any, TypedDict
from zoneinfo import ZoneInfo

from .errors import RuntimeErrorBase
from .service import QueryService
from .semantic_runtime import SemanticQueryRuntime


BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
CN_DATE_RE = re.compile(r"(?P<year>20\d{2})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日")
ISO_DATE_RE = re.compile(r"(?<!\d)(?P<year>20\d{2})-(?P<month>\d{1,2})-(?P<day>\d{1,2})(?!\d)")
CN_MONTH_RE = re.compile(r"(?P<year>20\d{2})年(?P<month>\d{1,2})月(?!\d)")
ISO_MONTH_RE = re.compile(r"(?<!\d)(?P<year>20\d{2})[-/](?P<month>\d{1,2})(?![-/\d])")


class AgentState(TypedDict, total=False):
    question: str
    supplied_parameters: dict[str, Any]
    execute: bool
    audit_plan: bool
    include_sql: bool
    now: datetime
    metric_id: str
    parameters: dict[str, Any]
    response: dict[str, Any]


def _local_now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(BUSINESS_TZ)
    if current.tzinfo is None:
        return current.replace(tzinfo=BUSINESS_TZ)
    return current.astimezone(BUSINESS_TZ)


def _naive(value: datetime) -> datetime:
    return value.astimezone(BUSINESS_TZ).replace(tzinfo=None)


def _explicit_dates(question: str) -> list[tuple[datetime, int, int]]:
    matches = list(CN_DATE_RE.finditer(question)) + list(ISO_DATE_RE.finditer(question))
    values: list[tuple[datetime, int, int]] = []
    for match in sorted(matches, key=lambda item: item.start()):
        try:
            values.append(
                (
                    datetime(
                        int(match.group("year")),
                        int(match.group("month")),
                        int(match.group("day")),
                        tzinfo=BUSINESS_TZ,
                    ),
                    match.start(),
                    match.end(),
                )
            )
        except ValueError:
            return []
    return values


def _date_from_question(question: str, now: datetime) -> datetime | None:
    explicit = _explicit_dates(question)
    if len(explicit) == 1:
        return explicit[0][0]
    if "昨天" in question or "昨日" in question:
        return datetime.combine((now - timedelta(days=1)).date(), time.min, BUSINESS_TZ)
    return None


def _date_range_from_question(
    question: str,
    now: datetime,
) -> tuple[datetime, datetime] | None:
    explicit = _explicit_dates(question)
    if len(explicit) == 1:
        start = explicit[0][0]
        return start, start + timedelta(days=1)
    if len(explicit) == 2:
        connector = question[explicit[0][2] : explicit[1][1]].strip().casefold()
        if re.search(r"(?:到|至|~|～|—|–|\bto\b)", connector):
            return explicit[0][0], explicit[1][0] + timedelta(days=1)
        return None
    if not explicit and ("昨天" in question or "昨日" in question):
        start = datetime.combine((now - timedelta(days=1)).date(), time.min, BUSINESS_TZ)
        return start, start + timedelta(days=1)
    return None


def _next_month(value: datetime) -> datetime:
    if value.month == 12:
        return datetime(value.year + 1, 1, 1, tzinfo=BUSINESS_TZ)
    return datetime(value.year, value.month + 1, 1, tzinfo=BUSINESS_TZ)


def _explicit_month_range(question: str) -> tuple[datetime, datetime] | None:
    matches = list(CN_MONTH_RE.finditer(question)) + list(ISO_MONTH_RE.finditer(question))
    if len(matches) != 1:
        return None
    match = matches[0]
    try:
        start = datetime(
            int(match.group("year")),
            int(match.group("month")),
            1,
            tzinfo=BUSINESS_TZ,
        )
    except ValueError:
        return None
    return start, _next_month(start)


def _turnover_window_from_question(
    question: str,
    now: datetime,
) -> tuple[datetime, datetime] | None:
    explicit_range = _date_range_from_question(question, now)
    if explicit_range is not None:
        return explicit_range
    explicit_month = _explicit_month_range(question)
    if explicit_month is not None:
        return explicit_month

    compact = question.casefold().replace(" ", "")
    this_month = datetime(now.year, now.month, 1, tzinfo=BUSINESS_TZ)
    today_start = datetime.combine(now.date(), time.min, BUSINESS_TZ)
    if any(token in compact for token in ("上月", "上个月", "上个自然月", "上一个月")):
        previous_month_end = this_month
        previous_month_start = datetime.combine(
            (previous_month_end - timedelta(days=1)).date().replace(day=1),
            time.min,
            BUSINESS_TZ,
        )
        return previous_month_start, previous_month_end
    if "本月" in compact or "本个自然月" in compact:
        return this_month, today_start

    recent_match = re.search(
        r"(?:最近|近|过去)(?P<days>\d{1,3})(?:个)?(?:完整)?(?:自然)?(?:天|日)",
        compact,
    )
    if recent_match:
        days = int(recent_match.group("days"))
        if 1 <= days <= 366:
            return today_start - timedelta(days=days), today_start
    return None


def _output_grain(question: str, *, inventory_default: bool = False) -> str:
    compact = question.casefold().replace(" ", "")
    if ("仓" in compact or "wms" in compact) and "sku" in compact:
        return "warehouse_sku"
    if "按日" in compact or "每天" in compact or "每日" in compact:
        return "ship_date"
    if "各仓" in compact or "按仓" in compact or "仓库维度" in compact:
        return "warehouse"
    if "各sku" in compact or "按sku" in compact or "商品维度" in compact:
        return "sku"
    return "warehouse_sku" if inventory_default else "total"


class DeterministicParameterExtractor:
    """Extract only stable date/scope expressions; ambiguous semantics stay unresolved."""

    def extract(
        self,
        metric_id: str,
        question: str,
        *,
        now: datetime | None = None,
    ) -> tuple[dict[str, Any], list[str]]:
        current = _local_now(now)
        parameters: dict[str, Any] = {}
        missing: list[str] = []

        if metric_id == "dms.points_redemption":
            request = SemanticQueryRuntime(clock=lambda: current).classify(question)
            if request is None or request.route != "dms_points_redemption":
                missing.append("dms_redemption_scope")
            else:
                parameters = {
                    "start_inclusive": datetime.combine(request.start, time.min),
                    "end_exclusive": datetime.combine(request.end, time.min),
                    "product_code": request.product_code,
                    "output_grain": "payment_month",
                }
        elif metric_id == "sales.outbound_actual":
            explicit_dates = _explicit_dates(question)
            date_range = _date_range_from_question(question, current)
            if date_range is not None:
                start, end = date_range
                parameters["start_inclusive"] = _naive(start)
                parameters["end_exclusive"] = _naive(end)
            elif "本月" in question and any(
                token in question
                for token in ("今天凌晨0点", "今天0点", "今日凌晨0点", "完整自然日")
            ):
                month_start = datetime(current.year, current.month, 1, tzinfo=BUSINESS_TZ)
                today_start = datetime.combine(current.date(), time.min, BUSINESS_TZ)
                parameters["start_inclusive"] = _naive(month_start)
                parameters["end_exclusive"] = _naive(today_start)
            else:
                missing.append(
                    "unambiguous_time_window" if len(explicit_dates) > 1 else "time_window"
                )
            parameters["output_grain"] = _output_grain(question)

        elif metric_id == "inventory.available_realtime":
            parameters["output_grain"] = _output_grain(question, inventory_default=True)

        elif metric_id == "inventory.snapshot_quantity":
            explicit_dates = _explicit_dates(question)
            snapshot = _date_from_question(question, current)
            if snapshot is None:
                missing.append(
                    "single_snapshot_date" if len(explicit_dates) > 1 else "snapshot_date"
                )
            else:
                parameters["snapshot_date"] = snapshot.date()
            parameters["output_grain"] = _output_grain(question, inventory_default=True)

        elif metric_id in {
            "inventory.turnover_finished_goods_realtime",
            "inventory.turnover_finished_goods_snapshot",
        }:
            window = _turnover_window_from_question(question, current)
            if window is None:
                missing.append("turnover_window")
            else:
                start, end = window
                parameters["start_inclusive"] = _naive(start)
                parameters["end_exclusive"] = _naive(end)
                if metric_id == "inventory.turnover_finished_goods_snapshot":
                    parameters["snapshot_date"] = (end - timedelta(days=1)).date()
            parameters["output_grain"] = _output_grain(question)

        elif metric_id == "inventory.turnover_mtd_realtime":
            if "本月" not in question:
                missing.append("turnover_window")
            else:
                parameters["month_start"] = _naive(
                    datetime(current.year, current.month, 1, tzinfo=BUSINESS_TZ)
                )
                parameters["sales_end_exclusive"] = _naive(
                    datetime.combine(current.date(), time.min, BUSINESS_TZ)
                )
            parameters["output_grain"] = _output_grain(question)

        return parameters, missing


class AgentWorkflow:
    def __init__(self, service: QueryService) -> None:
        self.service = service
        self.extractor = DeterministicParameterExtractor()
        self.graph = self._build_graph()

    def _build_graph(self) -> Any:
        try:
            from langgraph.graph import END, START, StateGraph
        except ImportError as exc:  # pragma: no cover - optional dependency guard
            raise RuntimeError(
                "LangGraph is required for /v1/agent; install project requirements."
            ) from exc

        builder = StateGraph(AgentState)
        builder.add_node("route", self._route)
        builder.add_node("extract", self._extract)
        builder.add_node("query", self._query)
        builder.add_edge(START, "route")
        builder.add_conditional_edges(
            "route",
            lambda state: "done" if "response" in state else "extract",
            {"done": END, "extract": "extract"},
        )
        builder.add_conditional_edges(
            "extract",
            lambda state: "done" if "response" in state else "query",
            {"done": END, "query": "query"},
        )
        builder.add_edge("query", END)
        return builder.compile()

    def _route(self, state: AgentState) -> dict[str, Any]:
        boundary = self._semantic_boundary(state["question"])
        if boundary is not None:
            return {"response": boundary}
        if any(word in state["question"].casefold() for word in ("dms", "积分兑换")):
            request = SemanticQueryRuntime(clock=lambda: _local_now(state.get("now"))).classify(state["question"])
            if request is not None and request.route == "dms_points_redemption":
                return {"metric_id": "dms.points_redemption"}
        turnover_route = self._turnover_route(state["question"], now=state.get("now"))
        if turnover_route is not None:
            if isinstance(turnover_route, dict):
                return {"response": turnover_route}
            applicability_boundary = self._contract_applicability_boundary(
                turnover_route,
                state["question"],
            )
            if applicability_boundary is not None:
                return {"response": applicability_boundary}
            return {"metric_id": turnover_route}
        decision = self.service.router.route(state["question"])
        if decision.needs_clarification or not decision.selected_metric_id:
            return {
                "response": {
                    "action": "clarify",
                    "question": decision.clarification_question,
                    "route": decision.as_dict(),
                }
            }
        applicability_boundary = self._contract_applicability_boundary(
            decision.selected_metric_id,
            state["question"],
        )
        if applicability_boundary is not None:
            return {"response": applicability_boundary}
        return {"metric_id": decision.selected_metric_id}

    def _contract_applicability_boundary(
        self,
        metric_id: str,
        question: str,
    ) -> dict[str, Any] | None:
        """Fail closed when deterministic question signals contradict the contract."""
        metric = self.service.get_metric(metric_id)
        configured = set(metric.get("applicability", {}).get("negative_signals", []))
        detected = self._detected_negative_signals(question)
        violated = configured & detected
        if not violated:
            return None

        if metric_id == "sales.outbound_actual":
            if "order_request" in violated:
                variant = "sales.order_requested"
                label = "订单请求量"
            elif "unshipped_demand" in violated:
                variant = "sales.unshipped_demand"
                label = "未完成或未出库需求量"
            elif "planned_quantity" in violated:
                variant = "sales.outbound_planned"
                label = "计划出库量"
            else:
                variant = "sales.channel_replacement"
                label = "渠道替换口径"
            return {
                "action": "reject",
                "reason_code": "contract_not_applicable",
                "question": f"该请求属于{label}，不能使用实际完成出库合同；当前知识库尚未登记对应可执行合同。",
                "detected_negative_signals": sorted(violated),
                "unavailable_variants": [variant],
                "forbidden_metric_ids": [metric_id],
            }

        if metric_id == "inventory.available_realtime":
            variants = {
                "historical_snapshot": "inventory.available_historical",
                "production_date": "inventory.by_production_date",
                "transit": "inventory.in_transit",
                "gross_on_hand": "inventory.gross_on_hand_realtime",
            }
            unavailable = sorted({variants[signal] for signal in violated if signal in variants})
            return {
                "action": "reject",
                "reason_code": "contract_not_applicable",
                "question": "该请求不适用于实时可用库存合同；时间点或库存构成需要独立口径。",
                "detected_negative_signals": sorted(violated),
                "unavailable_variants": unavailable,
                "forbidden_metric_ids": [metric_id],
            }

        if metric_id == "inventory.snapshot_quantity":
            return {
                "action": "reject",
                "reason_code": "contract_not_applicable",
                "question": "该请求不适用于历史库存快照合同，请明确当前可用、在途或快照日期回退规则。",
                "detected_negative_signals": sorted(violated),
                "unavailable_variants": [f"inventory.{signal}" for signal in sorted(violated)],
                "forbidden_metric_ids": [metric_id],
            }

        if metric_id in {
            "inventory.turnover_mtd_realtime",
            "inventory.turnover_finished_goods_realtime",
            "inventory.turnover_finished_goods_snapshot",
        }:
            return {
                "action": "reject",
                "reason_code": "contract_not_applicable",
                "question": "该请求的库存或销售分母不符合本月实时库存周转合同。",
                "detected_negative_signals": sorted(violated),
                "unavailable_variants": [
                    f"inventory.turnover.{signal}" for signal in sorted(violated)
                ],
                "forbidden_metric_ids": [metric_id],
            }
        return {
            "action": "reject",
            "reason_code": "contract_not_applicable",
            "question": "请求中存在该指标合同明确排除的业务信号。",
            "detected_negative_signals": sorted(violated),
            "forbidden_metric_ids": [metric_id],
        }

    @staticmethod
    def _turnover_route(
        question: str,
        *,
        now: datetime | None = None,
    ) -> str | dict[str, Any] | None:
        compact = question.casefold().replace(" ", "")
        if "周转" not in compact and "turnover" not in compact:
            return None

        current = _local_now(now)
        window = _turnover_window_from_question(question, current)
        explicit_realtime = any(
            token in compact
            for token in ("当前", "实时", "现在", "现有", "现时库存", "actual_quantity")
        )
        explicit_snapshot = any(
            token in compact
            for token in ("区间末", "期末", "历史库存", "历史成品库存", "库存快照", "快照库存")
        )

        metric_id: str | None
        if explicit_realtime and explicit_snapshot:
            metric_id = None
        elif explicit_snapshot:
            metric_id = "inventory.turnover_finished_goods_snapshot"
        else:
            # A historical sales window does not change the confirmed numerator.
            metric_id = "inventory.turnover_finished_goods_realtime"

        questions: list[dict[str, Any]] = []
        if metric_id is None:
            questions.append(
                {
                    "id": "turnover_inventory_point",
                    "prompt": "请选择周转分子；分母固定为所选区间内实际完成销售出库成品的日均数量。",
                    "selection": "single",
                    "options": [
                        {
                            "id": "period_end_finished_goods_snapshot",
                            "label": "区间末日成品库存快照",
                        },
                        {
                            "id": "current_realtime_finished_goods",
                            "label": "当前可用成品库存加在途",
                        },
                    ],
                }
            )
        if window is None:
            questions.append(
                {
                    "id": "sales_window",
                    "prompt": "请选择销售时间窗口",
                    "selection": "single",
                    "options": [
                        {"id": "mtd_complete_days", "label": "本月完整自然日"},
                        {"id": "previous_natural_month", "label": "上个自然月"},
                        {"id": "last_7_complete_days", "label": "最近7个完整自然日"},
                        {"id": "last_30_complete_days", "label": "最近30个完整自然日"},
                    ],
                }
            )
        if questions:
            return {
                "action": "clarify",
                "question": "请一次完成本轮库存周转选择。",
                "questions": questions,
                "clarification_key": questions[0]["id"],
                "candidate_metric_ids": [
                    "inventory.turnover_finished_goods_snapshot",
                    "inventory.turnover_finished_goods_realtime",
                ],
            }
        return metric_id

    @staticmethod
    def _detected_negative_signals(question: str) -> set[str]:
        compact = question.casefold().replace(" ", "")
        signals: set[str] = set()
        if any(token in compact for token in ("订单请求", "请求发货", "request_qty", "requestedquantity")):
            signals.add("order_request")
        if any(token in compact for token in ("计划出库", "计划发货", "订单计划", "planned")):
            signals.add("planned_quantity")
        if any(token in compact for token in ("未完成", "未发货", "未出库", "待出库", "unshipped")):
            signals.add("unshipped_demand")
        if any(token in compact for token in ("渠道替换", "替换口径", "社区平台")):
            signals.add("channel_replacement")
        is_turnover = "周转" in compact or "turnover" in compact
        if (not is_turnover and _explicit_dates(question)) or any(token in compact for token in ("历史", "快照")):
            signals.add("historical_snapshot")
            signals.add("historical_inventory")
        if any(token in compact for token in ("生产日期", "生产批次", "库龄")):
            signals.add("production_date")
        if "在途" in compact or "intransit" in compact:
            signals.add("transit")
        if any(token in compact for token in ("毛库存", "账面库存", "未扣减库存", "总在库")):
            signals.add("gross_on_hand")
        if any(token in compact for token in ("当前可用", "实时可用", "现有可用")):
            signals.add("current_available")
        if any(token in compact for token in ("最近日期", "就近日期", "最近快照")):
            signals.add("nearest_date_fallback")
        if any(token in compact for token in ("周转平均", "平均各", "averageofratios")):
            signals.add("average_of_ratios")
        return signals

    @staticmethod
    def _semantic_boundary(question: str) -> dict[str, Any] | None:
        compact = question.casefold().replace(" ", "")
        asks_turnover = "周转" in compact or "turnover" in compact
        asks_order_request = any(
            token in compact
            for token in ("订单请求", "请求发货", "request_qty", "requestedquantity")
        )
        if asks_order_request:
            unavailable = (
                "inventory.turnover_current_inventory_order_request"
                if asks_turnover
                else "sales.order_requested"
            )
            forbidden = (
                [
                    "inventory.turnover_finished_goods_realtime",
                    "inventory.turnover_finished_goods_snapshot",
                ]
                if asks_turnover
                else ["sales.outbound_actual"]
            )
            return {
                "action": "reject",
                "reason_code": "unsupported_metric_variant",
                "question": "该请求使用订单请求量口径，当前知识库尚未登记可执行合同。",
                "unavailable_variants": [unavailable],
                "forbidden_metric_ids": forbidden,
            }
        if "在途" in compact or "intransit" in compact:
            return {
                "action": "clarify",
                "question": "请选择在途数量口径；两种口径不可合并。",
                "questions": [
                    {
                        "id": "transit_quantity_basis",
                        "prompt": "请选择在途数量口径；两种口径不可合并。",
                        "selection": "single",
                        "options": [
                            {
                                "id": "legacy_full_requested_transit",
                                "label": "历史报表兼容口径（采购申请量全额计在途）",
                            },
                            {
                                "id": "remaining_unreceived_transit",
                                "label": "剩余未收口径（仅统计尚未收货数量）",
                            },
                        ],
                    }
                ],
                "clarification_key": "transit_quantity_basis",
                "unavailable_variants": [
                    "inventory.transit_legacy_full_requested",
                    "inventory.transit_remaining_unreceived",
                ],
                "forbidden_metric_ids": ["inventory.available_realtime"],
            }
        generic_shipment = any(token in compact for token in ("发货量", "出库量"))
        explicit_shipment_basis = any(
            token in compact for token in ("实际", "完成", "订单", "请求", "销售出库")
        )
        # "线上/线下渠道" and "箱数" describe dimensions or requested
        # measures of the ordinary actual-outbound result. They do not mean
        # the separately governed channel-replacement business variant.
        has_outbound_dimension_or_measure = any(
            token in compact
            for token in ("渠道", "线上", "线下", "客户渠道", "箱数", "箱")
        )
        if generic_shipment and not explicit_shipment_basis and not has_outbound_dimension_or_measure:
            return {
                "action": "clarify",
                "question": "您要查实际完成出库量，还是订单请求量或特定渠道替换口径？",
                "candidate_metric_ids": ["sales.outbound_actual"],
                "unavailable_variants": ["sales.order_requested", "sales.community_replacement"],
                "clarification_key": "shipment_quantity_basis",
            }
        generic_inventory = "库存" in compact and any(
            token in compact for token in ("现在", "当前", "现有")
        )
        explicit_inventory_basis = any(
            token in compact
            for token in ("可用", "快照", "历史", "周转", "在途", "生产日期")
        )
        if generic_inventory and not explicit_inventory_basis:
            return {
                "action": "clarify",
                "question": "您要查当前可用库存，还是指定日期的历史库存快照？",
                "candidate_metric_ids": [
                    "inventory.available_realtime",
                    "inventory.snapshot_quantity",
                ],
                "clarification_key": "inventory_quantity_basis",
            }
        return None

    def _extract(self, state: AgentState) -> dict[str, Any]:
        extracted, missing = self.extractor.extract(
            state["metric_id"], state["question"], now=state.get("now")
        )
        parameters = {**extracted, **state.get("supplied_parameters", {})}
        if missing:
            unresolved = [name for name in missing if name not in state.get("supplied_parameters", {})]
            if unresolved:
                metric = self.service.get_metric(state["metric_id"])
                return {
                    "response": {
                        "action": "clarify",
                        "metric_id": state["metric_id"],
                        "candidate_metric_ids": [state["metric_id"]],
                        "question": metric.get("clarification", {}).get("question")
                        or f"请补充参数：{', '.join(unresolved)}",
                        "missing": unresolved,
                        "clarification_key": (
                            "snapshot_date"
                            if "snapshot_date" in unresolved
                            else "single_snapshot_date"
                            if "single_snapshot_date" in unresolved
                            else metric.get("clarification", {}).get("ambiguity_group")
                        ),
                        "extracted_parameters": parameters,
                    }
                }
        return {"parameters": parameters}

    def _query(self, state: AgentState) -> dict[str, Any]:
        try:
            if state.get("execute", False):
                result = self.service.query(
                    metric_id=state["metric_id"],
                    question=state["question"],
                    parameters=state["parameters"],
                    include_sql=state.get("include_sql", False),
                )
                return {"response": {"action": "executed", **result}}
            plan_builder = (
                self.service.plan_and_audit
                if state.get("audit_plan", True)
                else self.service.build_plan
            )
            plan = plan_builder(
                metric_id=state["metric_id"],
                question=state["question"],
                parameters=state["parameters"],
            )
            return {
                "response": {
                    "action": "planned",
                    "plan": plan.as_dict(include_sql=state.get("include_sql", False)),
                }
            }
        except RuntimeErrorBase as exc:
            return {
                "response": {
                    "action": "blocked",
                    "metric_id": state["metric_id"],
                    "parameters": state.get("parameters", {}),
                    "error": exc.as_dict(),
                }
            }

    def invoke(
        self,
        *,
        question: str,
        supplied_parameters: dict[str, Any] | None = None,
        execute: bool = False,
        include_sql: bool = False,
        audit_plan: bool = True,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        final = self.graph.invoke(
            {
                "question": question,
                "supplied_parameters": supplied_parameters or {},
                "execute": execute,
                "audit_plan": audit_plan,
                "include_sql": include_sql,
                "now": _local_now(now),
            }
        )
        return final["response"]

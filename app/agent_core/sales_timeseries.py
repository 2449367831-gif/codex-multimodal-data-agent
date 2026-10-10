"""Calendar and window operations over complete, reconciled daily sales facts."""

from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP, localcontext
import re

from ..sku_references import extract_sku_references, restore_sku_references
from .semantic_types import SemanticAnswer, SemanticRuntimeError, _display_table, _parse_decimal
from .handlers.base import MetricHandler
from .handlers.sales_aggregate import SalesAggregateHandler, _integer


MAX_CALENDAR_DAYS = 3650


@dataclass(frozen=True, slots=True)
class SalesTimeSeries:
    moving_window_days: int

    def __post_init__(self):
        if type(self.moving_window_days) is not int or not 2 <= self.moving_window_days <= 365:
            raise ValueError("invalid moving-average window")


_AVERAGE = r"(?:移动|滚动|滑动)平均"
_WINDOW = re.compile(rf"(?<![\d.])(?P<days>\d{{1,3}})(?:个)?(?:自然)?(?:日|天){_AVERAGE}")


def sales_timeseries_request(question, *, planner):
    codes, text = extract_sku_references(question)
    all_owner = bool(re.search(r"(?:全部|所有)货主", text))
    all_warehouses = bool(re.search(r"(?:全部|所有)仓库", text))
    all_skus = bool(re.search(r"(?:全部|所有)SKU", text, re.I))
    all_channels = bool(re.search(r"(?:全部|所有)渠道", text))
    # Preserve date/time separators and opaque SKU codes during clause parsing.
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+|\s+(?=[\u4e00-\u9fff])", "", text)
    if not re.search(r"(?:实际|有效)销售出库", text) or "累计" not in text:
        return None
    windows = {int(match["days"]) for match in _WINDOW.finditer(text)}
    if len(windows) != 1:
        return None
    window = windows.pop()
    if not 2 <= window <= 365:
        return None
    # These are semantic choices, not optional filler. Do not turn sparse
    # observations or an expanding average into a zero-filled fixed window.
    zero = r"(?:(?:无匹配出库|无销售|没有销售)的?日期(?:实发数量和出库单数|数量和单数)(?:均)?为0|无销售日(?:数量和单数)?补0)"
    partial = rf"(?:(?:区间开头|开头|前面)不足{window}天的?{_AVERAGE}显示|不足{window}天(?:的?{_AVERAGE})?显示)(?:NULL|空值)"
    pad = r"补齐区间内所有自然日|日期补齐"
    cumulative = r"(?:计算|求出)?(?:区间起点|查询起点)到当日的?累计(?:实发数量|出库数量)|从区间起点累计实发数量"
    if not all(re.search(pattern, text, re.I) for pattern in (zero, partial, pad, cumulative)):
        return None
    denominator = re.findall(r"(?:每个)?完整窗口的?分母固定为(\d+)", text)
    if denominator and any(int(value) != window for value in denominator):
        return None
    output = re.search(r"(?:只返回|只输出)([^。；;]+?)(?:五列|5列)?业务表格", text)
    if output:
        labels = re.split(r"[、,，]", output[1])
        if labels != ["日期", "实发数量", "出库单数", "累计实发数量", f"{window}日移动平均"]:
            return None
        text = text[:output.start()] + text[output.end():]
    for pattern in (zero, partial, pad, cumulative):
        text = re.sub(pattern, "", text, flags=re.I)
    text = re.sub(r"(?:以及|并且|并)?包含当日的连续(?=\d)", "", text)
    text = re.sub(rf"(?:计算|求出)(?=\d+(?:个)?(?:自然)?(?:日|天){_AVERAGE})", "", text)
    text = _WINDOW.sub("", text)
    clauses = (
        rf"必须先补齐日期再算{_AVERAGE}",
        rf"(?:每个)?完整窗口的?分母固定为{window}(?!\d)",
        r"不引入区间之前的数据",
        r"不继承之前的问题范围",
        r"出库单按货主和出库单号联合去重",
        r"按日期升序",
        r"日期使用YYYY-MM-DD文本",
        r"数量和累计数量单位EA",
        r"出库单数单位单",
        rf"{_AVERAGE}单位EA/日",
        r"数值列必须为数值类型",
        r"不要展示SQL或对账明细",
        r"窗口包含当日",
    )
    for pattern in clauses:
        text = re.sub(pattern, "", text, flags=re.I)
    text = re.sub(r"按已发布的((?:成品)?(?:实际|有效)销售出库)口径", r"\1", text)
    text = re.sub(r"(?:全部|所有)SKU", "", text, flags=re.I)
    text = re.sub(r"(?:全部|所有)渠道", "", text)
    text = re.sub(r"(?:全部|所有)(?:仓库|货主)", "", text)
    text = re.sub(r"按(?:每天|每日|天)汇总", "按日期汇总", text)
    text = re.sub(r"[，,。；;]+", "，", text).strip("，")
    # The standard planner owns all remaining dates, scope, sources and
    # exclusions. Unknown predicates cannot disappear into a time-series plan.
    base = planner.classify(restore_sku_references(text, codes), _with_analysis=False)
    if (base is None or base.route != "sales_aggregate" or base.start is None or base.end is None
            or base.sales_aggregation is None or base.sales_aggregation.dimensions != ("ship_date",)
            or not 1 <= (base.end - base.start).days <= MAX_CALENDAR_DAYS):
        return None
    if ((all_owner and base.scope.owner != "ALL")
            or (all_warehouses and base.scope.warehouse_codes)
            or (all_skus and base.scope.sku_codes)
            or (all_channels and base.sales_aggregation.channel_filter)):
        return None
    return replace(base, route="sales_timeseries", sales_timeseries=SalesTimeSeries(window))


def calendar_rows(rows, start, end, operation):
    """Pad only absent dates after the complete fact set has been reconciled."""
    if (type(operation) is not SalesTimeSeries or type(start) is not date or type(end) is not date
            or not 1 <= (end - start).days <= MAX_CALENDAR_DAYS):
        raise ValueError("invalid calendar plan")
    by_day = {}
    for row in rows:
        day = date.fromisoformat(row["ship_date"])
        if day.isoformat() != row["ship_date"] or day in by_day or not start <= day < end:
            raise ValueError("invalid or duplicate fact date")
        quantity, documents = _parse_decimal(row["quantity"]), _integer(row["outbound_count"])
        if quantity < 0 or documents < 1:
            raise ValueError("invalid daily measure")
        by_day[day] = quantity, documents
    result, values = [], []
    with localcontext() as context:
        context.prec = 80
        context.rounding = ROUND_HALF_UP
        total, rolling = Decimal(0), Decimal(0)
        for index in range((end - start).days):
            day = start + timedelta(days=index)
            quantity, documents = by_day.get(day, (Decimal(0), 0))
            values.append(quantity)
            total += quantity
            rolling += quantity
            if index >= operation.moving_window_days:
                rolling -= values[index - operation.moving_window_days]
            average = None if index + 1 < operation.moving_window_days else format(
                rolling / operation.moving_window_days, ".2f")
            result.append({"ship_date": day.isoformat(), "quantity": format(quantity, "f"),
                           "outbound_count": str(documents), "cumulative_quantity": format(total, "f"),
                           "moving_average": average})
    return result


class SalesTimeSeriesHandler(MetricHandler):
    def run(self, request):
        operation = request.sales_timeseries
        plan = request.sales_aggregation
        if (type(operation) is not SalesTimeSeries or request.start is None or request.end is None
                or not 1 <= (request.end - request.start).days <= MAX_CALENDAR_DAYS
                or plan is None or plan.dimensions != ("ship_date",) or plan.include_sku_count
                or request.sales_analysis is not None):
            raise SemanticRuntimeError("销售日历分析计划无效，未执行查询", reason="sales_timeseries_invalid")
        # Keep every source/scope predicate, including explicit order types and
        # fulfillment, while changing only the post-query operation.
        with localcontext() as context:
            context.prec = 80
            answer = SalesAggregateHandler(self.execution, timezone_name=self.timezone_name).run(
                replace(request, route="sales_aggregate", sales_timeseries=None))
        try:
            output = calendar_rows(answer.business_table["rows"], request.start, request.end, operation)
        except (ValueError, KeyError, TypeError) as exc:
            raise SemanticRuntimeError("每日数据未通过核对，未展示日历分析", reason="sales_timeseries_reconciliation_failed") from exc
        window = operation.moving_window_days
        table = _display_table(
            table_id="sales_timeseries", title="销售出库日历与窗口分析",
            caption=f"{request.start} 至 {request.end}（不含） · {window}日移动平均",
            columns=[{"key": "ship_date", "kind": "text"},
                     {"key": "quantity", "kind": "number", "unit": "EA"},
                     {"key": "outbound_count", "kind": "number", "unit": "单"},
                     {"key": "cumulative_quantity", "kind": "number", "unit": "EA"},
                     {"key": "moving_average", "label": f"{window}日移动平均", "kind": "number", "unit": "EA/日"}],
            rows=output, summary=answer.business_table["summary"],
        )
        table["notes"] = ["北京时间补齐无销售日期为0；不引入区间前数据。",
                          f"累计从区间起点开始；{window}日窗口包含当日，不足完整窗口为空。"]
        return SemanticAnswer(str(answer) + f"已补齐 {len(output)} 个自然日，计算累计量和 {window} 日移动平均；不足完整窗口为空。", table)

"""Compose explicit arithmetic/presentation over a reviewed sales aggregation."""

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
import operator
import re

from ..sku_references import extract_sku_references
from .sales_dimensions import DIMENSION_NAMES, DIMENSION_TOKEN, GROUPS, dimension_names


_COMPARISONS = {"gt": operator.gt, "ge": operator.ge, "lt": operator.lt,
                "le": operator.le, "eq": operator.eq, "ne": operator.ne}
_COMPARISON_WORDS = {">": "gt", ">=": "ge", "<": "lt", "<=": "le", "=": "eq",
                     "!=": "ne", "≥": "ge", "≤": "le", "≠": "ne",
                     "大于": "gt", "超过": "gt", "高于": "gt", "至少": "ge", "不低于": "ge",
                     "大于等于": "ge", "小于": "lt", "低于": "lt", "不足": "lt", "至多": "le",
                     "不超过": "le", "不高于": "le", "小于等于": "le", "等于": "eq", "不等于": "ne"}


@dataclass(frozen=True, slots=True)
class QuantityPredicate:
    comparison: str
    value: str

    def __post_init__(self):
        if not isinstance(self.comparison, str) or self.comparison not in _COMPARISONS or not isinstance(self.value, str):
            raise ValueError("invalid quantity predicate")
        if not re.fullmatch(r"[0-9]{1,16}(?:\.[0-9]{1,4})?", self.value):
            raise ValueError("invalid quantity threshold")
        if Decimal(self.value) > Decimal("1000000000000000"):
            raise ValueError("quantity threshold outside budget")

    def matches(self, value):
        return _COMPARISONS[self.comparison](value, Decimal(self.value))


@dataclass(frozen=True, slots=True)
class SalesAnalysis:
    quantity_share: bool = False
    daily_average: bool = False
    quantity_order: str | None = None
    share_by_day: bool = False
    quantity_predicates: tuple[QuantityPredicate, ...] = ()

    def __post_init__(self):
        if (type(self.quantity_share) is not bool or type(self.daily_average) is not bool
                or type(self.share_by_day) is not bool
                or (self.share_by_day and not self.quantity_share)
                or self.quantity_order not in {None, "asc", "desc"}
                or not isinstance(self.quantity_predicates, tuple) or len(self.quantity_predicates) > 4
                or any(type(item) is not QuantityPredicate for item in self.quantity_predicates)
                or (self.quantity_predicates and self.quantity_share)
                or not (self.quantity_share or self.daily_average or self.quantity_order or self.quantity_predicates)):
            raise ValueError("invalid sales analysis")


_QUANTITY = r"(?:实发(?:数量|量)|实际(?:销售)?出库(?:数量|量)|数量)"
_SHARE = re.compile(
    rf"(?:并)?(?:计算|给出|列出|展示)?(?:各自|各组)?{_QUANTITY}"
    r"(?:占(?:当天|当日|本期|区间|同一范围|本次查询范围)?(?:总实发数量|总数量|总量)的(?:百分比|比例|占比)"
    r"|(?:的)?(?:占比|占总量百分比))"
)
_AVERAGE = re.compile(rf"(?:并)?(?:计算|给出|列出|展示)?(?:各自|各组)?(?:区间|期间)?日均(?:{_QUANTITY}|销量|量)")
_ORDER = re.compile(rf"按{_QUANTITY}(?P<order>从高到低|从低到高|降序|升序)(?:排列|排序)?")
_MISSING = re.compile(r"未配置履约类型(?:的)?(?:单据)?(?:单列|单独(?:列出|展示|统计))(?:[，,；;]?(?:不能丢弃|不得丢弃|不能遗漏))?")
_PREDICATE = re.compile(
    rf"(?P<measure>{_QUANTITY})(?:合计|总量)?\s*"
    r"(?P<comparison>" + "|".join(map(re.escape, sorted(_COMPARISON_WORDS, key=len, reverse=True))) + r")\s*"
    r"(?P<value>[0-9]{1,16}(?:\.[0-9]{1,4})?)(?:\s*EA)?(?![0-9.a-z万亿个件箱吨元%])", re.I
)
_FILTERED_DIMENSIONS = re.compile(
    rf"的(?P<names>{DIMENSION_TOKEN}(?:\s*[和与及、,，+]\s*{DIMENSION_TOKEN})*)(?:及|和|与)(?:数量|实发数量)", re.I
)


def _quantity_filter(question):
    matches = list(_PREDICATE.finditer(question))
    if not matches:
        return question, ()
    # Detail predicates and OR conditions cannot be reinterpreted as HAVING.
    if re.search(r"每笔|每单|逐单|单笔|单条|明细|或者|或|任一|排除|不保留|不要|不是|不满足|不符合|除了", question):
        return question, ()
    for match in matches:
        prefix = question[:match.start()].rstrip()
        # A suffix of another measure is not a total-quantity predicate:
        # e.g. outbound document count, daily average or allocation quantity.
        if match["measure"] == "数量" and prefix and re.search(r"[\w]$", prefix):
            if not re.search(r"(?:(?:只|仅)?(?:保留|筛选|选出)|并且|同时满足|且|同时)$", prefix):
                return question, ()
        if re.search(r"(?:日均|平均|每单|单均|分配|请求|计划|退货|非|不|不要|不是)$", prefix):
            return question, ()
        if re.match(r"\s*(?:[A-Za-z0-9_.%]|万|亿|个|件|箱|吨|元|组)", question[match.end():]):
            return question, ()
    if not (GROUPS.search(question) or _FILTERED_DIMENSIONS.search(question)
            or re.search(r"各仓库|每个仓库|所有仓库|各SKU|汇总后", question, re.I)):
        return question, ()
    if len(matches) > 4:
        return question, ()
    predicates = []
    try:
        for match in matches:
            predicates.append(QuantityPredicate(_COMPARISON_WORDS[match["comparison"]], match["value"]))
    except (ValueError, InvalidOperation):
        return question, ()
    text = question
    for match in reversed(matches):
        # Preserve the metric introducer when it also names the filter measure.
        measure = match["measure"]
        replacement = re.sub(r"(?:数量|量)$", "", measure) if "出库" in measure else ""
        text = text[:match.start()] + replacement + text[match.end():]
    text = _FILTERED_DIMENSIONS.sub(lambda match: "，按" + match["names"] + "汇总", text)
    text = re.sub(r"汇总后|分组后|(?:并)?(?:只|仅)?(?:保留|筛选|选出)(?:出库量|数量)?", "", text)
    text = re.sub(r"并且|同时满足|且|同时", "，", text)
    return text, tuple(predicates)


def sales_analysis_request(question, *, planner):
    # Opaque SKU identifiers are restored unchanged before base planning.
    codes, text = extract_sku_references(question)
    source_text = text
    if not re.search(r"实际销售出库|有效销售出库|实际出库|完成销售出库", text):
        return None
    text, quantity_predicates = _quantity_filter(text)
    shares = list(_SHARE.finditer(text))
    daily_denominator = any(re.search(r"当天|当日", match.group()) for match in shares)
    if daily_denominator and any(not re.search(r"当天|当日", match.group()) for match in shares):
        return None
    text, share_count = _SHARE.subn("", text)
    text, average_count = _AVERAGE.subn("", text)
    orders = [match.group("order") for match in _ORDER.finditer(text)]
    text = _ORDER.sub("", text)
    if (not (share_count or average_count or orders or quantity_predicates)
            or len(set(orders)) > 1 or (quantity_predicates and share_count)):
        return None
    requested_dimensions = set()

    def normalize_group(match):
        dimensions = dimension_names(match)
        requested_dimensions.update(DIMENSION_NAMES[name][1] for name in dimensions)
        return "、".join(DIMENSION_NAMES[name][0] for name in dimensions)

    text = GROUPS.sub(normalize_group, text)
    # The time parser already understands zero o'clock. Normalize its spelling
    # for older published scope vocabularies, without changing the interval.
    text = re.sub(r"(?<=[\d日号])\s*零\s*点", " 0点", text)
    # These semantics are owned by this operation, not global ignore words.
    text, missing_count = _MISSING.subn("", text)
    if missing_count and not re.search(r"2[cC].*2[bB]|2[bB].*2[cC]|按履约类型", text):
        return None
    text = re.sub(r"按履约类型", "按2C和2B", text)
    # Output verbs are presentation commands, not unsupported predicates.
    # Keep the measure token so the base planner still selects its reviewed
    # SKU-count projection.
    text = re.sub(r"(?:并)?(?:列出|输出|展示|显示|返回|给出)(?=SKU\s*(?:数|数量))", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(?:实发数量|实发量)", "数量", text)
    text = re.sub(r"[，,；;。]+", "，", text).strip("，")
    # Adding document count selects the same reviewed multi-dimension compiler;
    # its independent controls are required even for quantity-only questions.
    text += "，出库单数"
    if codes:
        text += "，SKU " + "、".join(f"`{code}`" for code in codes)
    base = planner.classify(text, _with_analysis=False)
    if base is None or base.route != "sales_aggregate" or base.start is None or base.end is None:
        return None
    if not requested_dimensions.issubset(base.sales_aggregation.dimensions):
        return None
    if planner._source_interval(source_text) != (base.start, base.end):
        return None
    if daily_denominator and "ship_date" not in base.sales_aggregation.dimensions and (base.end - base.start).days != 1:
        return None
    order = None if not orders else ("desc" if orders[0] in {"从高到低", "降序"} else "asc")
    return replace(base, route="sales_analysis", sales_analysis=SalesAnalysis(
        quantity_share=bool(share_count), daily_average=bool(average_count), quantity_order=order,
        share_by_day=daily_denominator and "ship_date" in base.sales_aggregation.dimensions,
        quantity_predicates=quantity_predicates,
    ))

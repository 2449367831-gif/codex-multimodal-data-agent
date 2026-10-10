"""Conservative arithmetic accelerator; unconsumed semantics use model planning."""

from __future__ import annotations

from datetime import date, timedelta
import re

from .sales_aggregation import SalesAggregation
from .semantic_types import SalesComposition, SemanticRequest


_DATE = re.compile(r"(?<!\d)(?P<year>20\d{2})[-年](?P<month>\d{1,2})[-月](?P<day>\d{1,2})日?(?!\d)")
_PERCENT = re.compile(r"(?<![\d.])([0-9]{1,3}(?:\.[0-9]{1,2})?)%")

# Consume only clauses whose effects are implemented below. Remaining text
# belongs to general planning, never to an ALL fast path.
_COMMON = (
    r"(?:全部|所有)(?:货主|仓库|商品|渠道)(?:[、和](?:(?:全部|所有))?(?:货主|仓库|商品|渠道))*",
    r"不限定成品", r"使用已发布(?:的)?实际销售出库口径", r"不沿用前一问的范围",
    r"(?:实际|有效)销售出库", r"以北京时间为准|北京时间",
    r"(?:两个|两|[0-9]+个?)完整自然日",
    r"(?:最近|近|过去)(?:[0-9]+|两)(?:个)?(?:完整)?(?:自然)?(?:日|天)",
    r"上个自然月|上个月|上月|上周|昨天",
    r"按货主编码和仓库编码(?:保留身份|汇总)",
    r"按货主编码升序[、，,]实发数量降序[、，,]仓库编码升序",
    r"完整返回货主编码[、，,]仓库编码[、，,]",
    r"结果返回货主编码[、，,]仓库编码[、，,]",
    r"返回货主编码[、，,]仓库编码[、，,]",
    r"编码为文本", r"数量单位EA|占比单位%",
    r"(?:平均量与)?占比保留两位小数", r"(?:五列)?业务表格", r"不能省略仓库",
    r"不要展示查询语句或(?:内部)?核对明细",
    r"不要展示查询语句[、，,]数据库字段或内部核对明细",
    r"查询|比较|对比|先|再|并|分别|结果|汇总|这|两天|期间|区间|的|与|至|表格",
)
_GROWTH = (
    r"只保留次日实发数量大于首日(?:的仓库)?",
    r"实发增量等于次日减首日", r"某天没有匹配记录时该天数量为0",
    r"按实发增量降序[、，,]同量时按货主编码[、，,]仓库编码升序",
    r"(?:两天)?实发数量", r"首日实发数量|次日实发数量|实发增量",
)
_AVERAGE = (
    r"对每个货主分别计算有匹配出库记录的仓库平均实发量",
    r"零数量但有记录的仓库也计入分母", r"没有记录的仓库不计入分母",
    r"(?:仅|只)保留实发数量严格大于本货主平均仓库实发量的仓库",
    r"计算这些仓库占本货主全部有记录仓库实发总量的百分比",
    r"分母不能缩小为筛选后的仓库", r"分母为零时百分比为空",
    r"货主平均仓库实发量|货主内数量占比|实发数量",
)
_COVERAGE = (
    r"只对(?:汇总)?数量大于零的?仓库做(?:以下)?覆盖分析",
    r"每个货主独立按实发数量降序排列仓库[、，,]同量按仓库编码升序",
    r"找出按这个顺序累计覆盖该货主实发总量至少PERCENT所需的最少仓库",
    r"必须包含第一次达到或超过PERCENT的那一行", r"不保留它之后的仓库",
    r"分母为该货主所有正数量仓库的实发总量", r"不能用筛选后仓库的总量",
    r"必须用未四舍五入的累计值判定是否达到PERCENT",
    r"必须用未四舍五入的累计值判定",
    r"累计值判定是否达到PERCENT", r"累计值判定",
    r"累计实发数量|累计数量占比|实发数量",
)


def _growth_dates(question: str, planner) -> tuple[date, date] | None:
    values = []
    for match in _DATE.finditer(question):
        try:
            values.append(date(int(match["year"]), int(match["month"]), int(match["day"])))
        except ValueError:
            return None
    if not values and re.search(r"(?:最近|近|过去)(?:两个|两|2个?)(?:完整)?(?:自然)?(?:日|天)", question):
        today = planner._now().date()
        values = [today - timedelta(days=2), today - timedelta(days=1)]
    if len(values) != 2:
        return None
    return tuple(values) if values[1] == values[0] + timedelta(days=1) else None


def sales_composition_request(question: str, *, planner) -> SemanticRequest | None:
    text = re.sub(r"\s+", "", question).replace("百分之八十", "80%")
    if not re.search(r"实际销售出库|有效销售出库", text):
        return None
    operation = None
    if "累计覆盖" in text and "最少仓库" in text:
        operation, grammar = "coverage", _COVERAGE
    elif "平均仓库实发量" in text and "严格大于" in text:
        operation, grammar = "above_average", _AVERAGE
    elif "次日实发数量大于首日" in text and "某天没有匹配记录时该天数量为0" in text:
        operation, grammar = "growth", _GROWTH
    if operation is None:
        return None
    percentages = set(_PERCENT.findall(text))
    if operation == "coverage":
        if len(percentages) != 1:
            return None
        coverage_percent = percentages.pop()
    else:
        coverage_percent = "80"
        if percentages:
            return None
    residue = _DATE.sub("", text)
    residue = _PERCENT.sub("PERCENT", residue)
    residue = re.sub("|".join(f"(?:{phrase})" for phrase in (*grammar, *_COMMON)), "", residue)
    if re.sub(r"[，,。；;、：:！!？?（）()]", "", residue):
        return None
    if operation == "growth":
        dates = _growth_dates(text, planner)
        if dates is None:
            return None
        start, end = dates[0], dates[1] + timedelta(days=1)
    else:
        source_interval = getattr(planner, "_source_interval", None)
        if not callable(source_interval):
            return None
        interval = source_interval(text)
        if interval is None:
            return None
        start, end = interval
        dates = (start, end - timedelta(days=1))
    try:
        composition = SalesComposition(operation, dates, coverage_percent)
    except ValueError:
        return None
    return SemanticRequest(
        route="sales_composition",
        start=start,
        end=end,
        sales_aggregation=SalesAggregation(("ship_date", "cargo_owner", "warehouse")),
        sales_composition=composition,
    )

"""Parse explicit two-period daily-average comparisons without dropping scope."""

from datetime import date, timedelta
import re

from .semantic_types import SemanticRequest, SemanticRuntimeError
from ..sku_references import extract_sku_references, restore_sku_references


_CHINESE_INTERVAL = re.compile(
    r"(?:(?P<year>20\d{2})年)?(?P<month>\d{1,2})月(?P<day>\d{1,2})日?"
    r"(?:至|到)(?:(?P<end_year>20\d{2})年)?(?:(?P<end_month>\d{1,2})月)?"
    r"(?P<end_day>\d{1,2})日"
)
_ISO_INTERVAL = re.compile(r"(20\d{2}-\d{2}-\d{2})(?:至|到)(20\d{2}-\d{2}-\d{2})")


def warehouse_period_comparison(question, *, planner):
    sku_codes, semantic_question = extract_sku_references(question)
    text = re.sub(r"\s+", "", semantic_question)
    text = re.sub(r"(实际销售出库|有效销售出库)(?:数量|量)?", r"\1", text)
    if not (re.search(r"对比|比较", text) and "日均" in text
            and re.search(r"(?:按|各|每个|所有)仓库", text)
            and re.search(r"实际销售出库|有效销售出库", text)):
        return None
    matches = sorted([*_CHINESE_INTERVAL.finditer(text), *_ISO_INTERVAL.finditer(text)], key=lambda m: m.start())
    if len(matches) != 2:
        return None
    periods = []
    year = planner._now().year
    try:
        for match in matches:
            if match.re is _ISO_INTERVAL:
                start, last = map(date.fromisoformat, match.groups())
            else:
                parts = match.groupdict()
                year = int(parts["year"] or year)
                month = int(parts["month"])
                start = date(year, month, int(parts["day"]))
                last = date(int(parts["end_year"] or year), int(parts["end_month"] or month), int(parts["end_day"]))
            year = start.year
            end = last + timedelta(days=1)
            if end <= start:
                raise ValueError("reversed interval")
            periods.append((start, end))
    except ValueError as exc:
        raise SemanticRuntimeError("日期范围无效，请重新提交", reason="date_invalid") from exc
    for match in reversed(matches):
        text = text[:match.start()] + text[match.end():]
    # Each consumed phrase implements a supported operation; all other
    # predicates are left for the scope resolver or the generic model planner.
    for phrase in (
        "只比较日均量的增减幅", "只比较日均量的变化率", "日均变化率", "日均增减幅",
        "分母为零时单独标注", "分母为0时单独标注", "不把无记录写成0",
        "无记录不填0", "按仓库", "各仓库", "每个仓库", "所有仓库",
        "两段期间", "两段区间", "两段", "期间", "区间",
        "出库数量", "日均量", "日均", "对比", "比较",
    ):
        text = text.replace(phrase, "")
    scope, shape, unsupported = planner.scope_resolver.resolve(restore_sku_references(text, sku_codes))
    supported_words = ("实际销售出库", "有效销售出库", "产成品", "成品", "所有", "全部",
                       "请", "查询", "统计", "列出", "展示", "输出", "表格", "数据", "用", "的", "和", "与", "及")
    remainder = re.sub("|".join(map(re.escape, sorted(supported_words, key=len, reverse=True))), "", shape)
    remainder = re.sub(r"[，,。.!！?？;；、]", "", remainder)
    if unsupported or remainder:
        return None
    return SemanticRequest(
        route="warehouse_period_comparison", start=min(a for a, _ in periods), end=max(b for _, b in periods),
        scope=scope, finished_goods_only=scope.finished_goods, show_warehouses=True,
        comparison_periods=(periods[0], periods[1]),
    )

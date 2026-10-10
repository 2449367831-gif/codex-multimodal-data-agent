"""Syntax normalization shared by planning and scope validation."""

import re


RECENT_DAY_WINDOW = re.compile(
    r"(?<![A-Za-z0-9_])(?:最近|近|过去|前)\s*"
    r"(?P<count>\d{1,4}|[一二三四五六七八九十两]+)\s*"
    r"(?:个\s*)?(?:完整\s*)?(?:自然\s*)?(?:天|日)"
)
RECENT_WEEK_WINDOW = re.compile(
    r"(?<![A-Za-z0-9_])(?:最近|近|过去)\s*"
    r"(?P<count>\d{1,3}|[一二三四五六七八九十两]+)\s*周"
)
_QUOTED = re.compile(r"`[^`]*`|\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'")
_RANK_COUNT = re.compile(
    r"(?<![A-Za-z0-9_-])(?:top|前|(?:最高|最多)\s*(?:的)?)\s*"
    r"(?P<count>[+-]?\d+(?:\.\d+)?)", re.I,
)
_NATURAL_RANK = re.compile(
    r"(?:最高|最多)\s*(?:的\s*)?(?:前\s*)?(?P<count>\d+)\s*(?:个\s*)?"
    r"(?=sku|商品|货品|单品|品项)", re.I,
)
_SKU_RANKING_COLUMN = (
    r"(?:排名|SKU(?:编码|名称)?|商品(?:编码|名称)|"
    r"(?:实发|发货|出库)(?:数量|量)|数量|出库单(?:数|量))"
)
_SKU_RANKING_PROJECTION = re.compile(
    r"(?:返回|输出|列出|展示|显示|给出|提供)(?:以下)?(?:字段|列)?\s*[:：]?\s*"
    + _SKU_RANKING_COLUMN
    + r"(?:\s*(?:[、,，]|和|与|及)\s*" + _SKU_RANKING_COLUMN + r")*"
    + r"\s*(?=$|[。；;])", re.I,
)
_SKU_RANKING_ORDER = re.compile(
    r"(?<!单)(?:按)?(?:实发(?:数量|量)|实际(?:销售)?出库(?:数量|量)|"
    r"发货(?:数量|量)|出库(?:数量|量)|销量|数量)"
    r"(?:从高到低|降序)(?:排列|排序)?", re.I,
)


def consume_sku_ranking_output(question: str) -> str:
    """Consume supported projection/order commands, preserving scope literals."""
    for pattern in (_SKU_RANKING_PROJECTION, _SKU_RANKING_ORDER):
        quoted = list(_QUOTED.finditer(question))
        matches = [match for match in pattern.finditer(question)
                   if not any(match.start() < literal.end() and match.end() > literal.start()
                              for literal in quoted)]
        for match in reversed(matches):
            replacement = "按SKU" if re.search(r"sku|商品", match.group(), re.I) else ""
            question = question[:match.start()] + replacement + question[match.end():]
    return question


def explicit_rank_counts(question: str) -> tuple[str, ...]:
    quoted = list(_QUOTED.finditer(question))
    return tuple(match['count'] for match in _RANK_COUNT.finditer(question)
                 if not any(match.start() < literal.end() and match.end() > literal.start()
                            for literal in quoted))


def normalize_query_wording(question: str) -> str:
    """Canonicalize command syntax without rewriting quoted business values."""
    unquoted = _QUOTED.sub("", question)
    sales_outbound_context = bool(re.search(
        r"(?:实际|有效|已完成|完成)?销售出库|"
        r"(?:实际|有效|已完成|完成)销售发货|"
        r"(?:实际|有效|已完成|完成)销售出货",
        unquoted,
    ))
    actual_sales = bool(re.search(r"(?:实际|有效|已完成|完成)(?:销售)?出库", unquoted))
    other_quantity = bool(re.search(r"计划|请求|预计|应发|分配|退货|订单", unquoted))

    def normalize(text):
        text = re.sub(r"卖出(?:去)?(?:数量|量)", "实际销售出库量", text)
        # These are output measures, not additional filters. Only resolve the
        # shipment synonym when this request already states the actual basis.
        if (sales_outbound_context or actual_sales) and not other_quantity:
            text = re.sub(r"(?<!实际)(?:实际)?发货(?:数量|量)", "实发数量", text)
        text = re.sub(r"去重(?:后的|后|的)?出库单(?:数量|数|量|笔数)", "出库单数", text)
        text = _NATURAL_RANK.sub(lambda m: f"TOP{m['count']}的", text)
        text = re.sub(
            r"(?:选取|选出|取)(?=\s*(?:(?:实发(?:数量|量)|发货(?:数量|量)|出库(?:数量|量)|销量|数量)\s*)?"
            r"(?:TOP|前|最高|最多))", "列出", text, flags=re.I,
        )
        text = re.sub(
            r"(^|[，,；;。]\s*)(?:请\s*)?(?:返回|给出|提供)"
            r"(?=\s*(?:仓库|数量|表格|SKU|实发|排名))", r"\1输出", text, flags=re.I,
        )
        return re.sub(r"(^|[，,；;]\s*|降序|从高到低)列(?:成)?表格", r"\1列表", text)

    parts, position = [], 0
    for literal in _QUOTED.finditer(question):
        parts.extend((normalize(question[position:literal.start()]), literal.group()))
        position = literal.end()
    parts.append(normalize(question[position:]))
    return ''.join(parts)


def recent_day_count(value: str) -> int | None:
    if value.isdecimal():
        days = int(value)
    else:
        digits = dict(zip("一二三四五六七八九", range(1, 10)))
        digits["两"] = 2
        if value in digits:
            days = digits[value]
        elif value.count("十") == 1:
            tens, ones = value.split("十")
            if (tens and tens not in digits) or (ones and ones not in digits):
                return None
            days = digits.get(tens, 1) * 10 + digits.get(ones, 0)
        else:
            return None
    return days if 1 <= days <= 3650 else None


def recent_day_windows(question: str):
    quoted = list(_QUOTED.finditer(question))
    for match in RECENT_DAY_WINDOW.finditer(question):
        if any(match.start() < literal.end() and match.end() > literal.start() for literal in quoted):
            continue
        yield match, recent_day_count(match["count"])


def normalize_recent_day_windows(question: str) -> str:
    # Replace only complete, understood duration spans, never identifier text
    # or the surrounding business constraints.
    for match, days in reversed(list(recent_day_windows(question))):
        if days is not None:
            question = question[:match.start()] + f"近{days}天" + question[match.end():]
    quoted = list(_QUOTED.finditer(question))
    for match in reversed(list(RECENT_WEEK_WINDOW.finditer(question))):
        if any(match.start() < literal.end() and match.end() > literal.start() for literal in quoted):
            continue
        count = recent_day_count(match["count"])
        if count is not None and count <= 520:
            question = question[:match.start()] + f"近{count * 7}天" + question[match.end():]
    return question

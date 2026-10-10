"""Extract one complete calendar expression without consuming other predicates."""

from dataclasses import dataclass
from datetime import date
import re
from typing import Callable


_DATE = r"(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|(?:\d{4}年)?\d{1,2}月\d{1,2}[日号])"
_CLOCK = (r"(?:T)?(?:(?:凌晨|午夜|上午|下午|中午|晚上)?"
          r"(?:\d{1,2}[:：]\d{2}(?:[:：]\d{2})?|\d{1,2}[点时](?:\d{1,2}分?)?|零点)|凌晨|午夜)")
_RECENT = r"(?:最近|近|过去|前)(?:\d{1,4}|[一二三四五六七八九十两]+)个?(?:完整)?(?:自然)?[天日]"
# Calendar words can touch numeric identifiers; only splitting a digit run is unsafe.
_DIGIT_BOUNDARY = r"(?!(?<=\d)\d)"
_EXPRESSION = re.compile(
    rf"{_DIGIT_BOUNDARY}(?:{_DATE}(?:{_CLOCK})?(?:到|至|-|~|～|—|–|to){_DATE}(?:{_CLOCK}前?)?"
    rf"|{_DATE}(?:{_CLOCK})?"
    r"|(?:\d{4}年)?第[1-4]季度"
    r"|(?:\d{4}年)?\d{1,2}(?:月)?(?:至|到|-)\d{1,2}月"
    r"|\d{4}[-/.]\d{1,2}(?![-/.\d])|(?:\d{4}年)?\d{1,2}月份?"
    rf"|{_RECENT}|(?:最近|近|过去)\d{{1,3}}个?(?:星期|周|月)"
    r"|(?:本月|本周|这周|这个星期)(?:截至|截止)(?:今天|今日|昨天|昨日)(?:00:00|0点)?"
    r"|上上季度|前两个季度|上个季度|上季度|这个季度|本季度"
    r"|上一年度|上年度|去年|本年度|今年"
    r"|上上个月|上上月|前两个月|上个自然月|上个月|上月|本月"
    r"|上上周|前两周|上周|这个星期|本周|这周"
    rf"|前天|昨天|昨日|今天|今日){_DIGIT_BOUNDARY}",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class CalendarWindow:
    start: date
    end: date
    remainder: str


def take_calendar_window(
    question: str, *, interval_parser: Callable[[str], tuple[date, date] | None],
) -> CalendarWindow | None:
    """Callers remove opaque identifiers and separate time axes first."""
    positions = [index for index, char in enumerate(question) if not char.isspace()]
    text = "".join(question[index] for index in positions)
    if re.search(r"(?<!\d)\d{5,}年", text):
        return None
    matches = list(_EXPRESSION.finditer(text))
    if len(matches) != 1:
        return None
    match = matches[0]
    start, end = positions[match.start()], positions[match.end() - 1] + 1
    # Keep the original separator between a numeric date and a numeric clock;
    # joining "09-01 0" into "09-010" hides the end date from the date parser.
    interval = interval_parser(question[start:end])
    if interval is None or interval[0] >= interval[1]:
        return None
    # Removing an expression must not concatenate fragments into a new scope
    # alias or business command. The caller still validates every remaining word.
    remainder = question[:start] + "," + question[end:]
    return CalendarWindow(interval[0], interval[1], remainder)

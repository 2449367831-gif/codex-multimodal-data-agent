"""Extract explicit SKU identifiers before interpreting business scope words."""

import re


_LABEL = re.compile(r"(?<![A-Za-z0-9_])(?:sku(?:\s*编码)?|商品编码|货品编码)\s*[:：]?\s*", re.I)
_TOKEN = re.compile(r"(?:`([A-Za-z0-9][A-Za-z0-9_-]{0,39})`|\"([A-Za-z0-9][A-Za-z0-9_-]{0,39})\"|'([A-Za-z0-9][A-Za-z0-9_-]{0,39})'|([A-Za-z0-9][A-Za-z0-9_-]{2,39})(?![A-Za-z0-9_-]))")
_SEPARATOR = re.compile(r"\s*[,，、]\s*")


def extract_sku_references(question: str) -> tuple[tuple[str, ...], str]:
    codes, spans = [], []
    for label in _LABEL.finditer(question):
        if spans and label.start() < spans[-1][1]:
            continue
        position, found = label.end(), []
        while match := _TOKEN.match(question, position):
            code = next(value for value in match.groups() if value is not None)
            if match.group(4) and re.fullmatch(r"top\d+", code, re.I):
                break
            found.append(code)
            end = match.end()
            separator = _SEPARATOR.match(question, end)
            if not separator:
                break
            position = separator.end()
        if found:
            codes.extend(found)
            spans.append((label.start(), end))
    remainder = question
    for start, end in reversed(spans):
        remainder = remainder[:start] + " " + remainder[end:]
    return tuple(dict.fromkeys(codes)), remainder


def restore_sku_references(question: str, codes: tuple[str, ...]) -> str:
    """Restore extracted identifiers for the shared scope resolver, unchanged."""
    if any(not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}", code) for code in codes):
        raise ValueError("invalid extracted SKU identifier")
    if not codes:
        return question
    return question + "，SKU " + "、".join(f"`{code}`" for code in codes)

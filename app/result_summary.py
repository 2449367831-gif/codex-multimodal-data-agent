"""Data-grounded summaries over screened, request-local result tables."""

from collections.abc import Mapping

from .public_table_text import PUBLIC_TABLE_UNSAFE_TEXT


MAX_REFERENCES = 8
MAX_SUMMARY_LINE = 600


def normalize_answer_references(references, manifest):
    if (not isinstance(manifest, list) or not manifest
            or any(not isinstance(entry, Mapping)
                   or not isinstance(entry.get("result_id"), str)
                   or not entry["result_id"]
                   or type(entry.get("row_count")) is not int or entry["row_count"] < 0
                   for entry in manifest)):
        raise ValueError("invalid summary manifest")
    counts = {entry["result_id"]: entry["row_count"] for entry in manifest}
    if len(counts) != len(manifest) or any(type(n) is not int or n < 0 for n in counts.values()):
        raise ValueError("invalid summary manifest")
    if references is None or references == []:
        # A first detail row is not a total. Only auto-highlight singleton or
        # empty results; larger tables need an explicit, bounded row reference.
        return [{"result_id": identifier, "row": 0 if count else None}
                for identifier, count in counts.items() if count <= 1][:MAX_REFERENCES]
    if not isinstance(references, list) or not 1 <= len(references) <= MAX_REFERENCES:
        raise ValueError("invalid answer references")
    normalized, seen = [], set()
    for reference in references:
        if not isinstance(reference, Mapping) or set(reference) != {"result_id", "row"}:
            raise ValueError("invalid answer reference")
        identifier, row = reference["result_id"], reference["row"]
        if not isinstance(identifier, str) or identifier not in counts:
            raise ValueError("answer references another result")
        if not ((counts[identifier] == 0 and row is None)
                or (type(row) is int and 0 <= row < counts[identifier])):
            raise ValueError("answer references a missing row")
        if (identifier, row) in seen:
            raise ValueError("duplicate answer reference")
        seen.add((identifier, row))
        normalized.append({"result_id": identifier, "row": row})
    return normalized


def _record_line(table, row, index, total):
    title = table["title"]
    heading = title if total == 1 else f"{title}（第{index + 1}条）"
    fields = []
    for column in table["columns"]:
        value = row[column["key"]]
        unit = column.get("unit") or ""
        rendered = "缺失" if value is None else str(value) + (f" {unit}" if unit else "")
        fields.append(f"{column['label']}：{rendered}")
    line = heading + "：" + "；".join(fields)
    if PUBLIC_TABLE_UNSAFE_TEXT.search(line):
        raise ValueError("unscreened summary text")
    # A wide record remains in the full table; never slice a value or omit a
    # dimension while making a numerical statement about that record.
    if len(line) > MAX_SUMMARY_LINE:
        return heading + "：完整记录已返回。"
    return line


def public_result_summary(tables, manifest, references=None, *, page_reader=None):
    """Values and labels come only from already-screened current-request pages."""
    if len(tables) != len(manifest) or not tables:
        raise ValueError("summary tables disagree with manifest")
    refs = normalize_answer_references(references, manifest)
    indexed = {}
    for table, entry in zip(tables, manifest, strict=True):
        page = table.get("pagination")
        total = page["total_rows"] if page else len(table["rows"])
        if total != entry["row_count"] or (page and page["page"] != 1):
            raise ValueError("summary source is incomplete")
        if PUBLIC_TABLE_UNSAFE_TEXT.search(table["title"]):
            raise ValueError("unscreened summary title")
        if page and (page["start_row"] != (1 if total else 0)
                     or page["end_row"] != len(table["rows"])):
            raise ValueError("summary page coverage changed")
        indexed[entry["result_id"]] = (table, total)
    lines = []
    for reference in refs:
        table, total = indexed[reference["result_id"]]
        index = reference["row"]
        if index is None:
            lines.append(table["title"] + "：无数据。")
            continue
        selected = table
        offset = index
        if index >= len(table["rows"]):
            page = table.get("pagination")
            if not page or not table["rows"] or page_reader is None:
                raise ValueError("summary row is unavailable")
            page_size = page["end_row"] - page["start_row"] + 1
            page_number = index // page_size + 1
            selected = page_reader(page["result_id"], page_number)
            metadata = selected.get("pagination", {})
            if (metadata.get("result_id") != page["result_id"] or metadata.get("page") != page_number
                    or metadata.get("total_rows") != total or selected["title"] != table["title"]
                    or metadata.get("start_row") != (page_number - 1) * page_size + 1
                    or metadata.get("end_row") != min(page_number * page_size, total)
                    or len(selected["rows"]) != metadata["end_row"] - metadata["start_row"] + 1
                    or selected["columns"] != table["columns"]):
                raise ValueError("summary page identity changed")
            offset = index + 1 - metadata["start_row"]
        if not 0 <= offset < len(selected["rows"]):
            raise ValueError("summary row coverage changed")
        lines.append(_record_line(selected, selected["rows"][offset], index, total))
    return {"format": "plain_text", "text": "\n".join(lines) or "查询已完成，结果见下方表格。"}

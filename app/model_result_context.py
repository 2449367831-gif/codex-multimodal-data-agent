"""Bound model-visible result previews without truncating host-owned answers."""

from collections.abc import Mapping, Sequence
import json


MAX_CONTEXT_ROWS = 40
MAX_CONTEXT_BYTES = 48 * 1024


def result_context(payload: Mapping, *, earlier_results: Sequence[Mapping] = ()) -> str:
    """Keep original row indices and values; never abbreviate individual cells."""
    executions = [entry.get("execution") for entry in (*earlier_results, payload)]
    if not 1 <= len(executions) <= 4 or any(not isinstance(e, Mapping) for e in executions):
        raise ValueError("invalid result context batch")
    previews = []
    for execution in executions:
        rows = execution.get("rows")
        if not isinstance(rows, list):
            raise ValueError("invalid result context rows")
        previews.append({
            "columns": list(execution.get("columns", [])), "rows": [],
            "column_count": len(execution.get("columns", [])), "columns_preview_truncated": False,
            "row_count": execution.get("row_count"), "truncated": execution.get("truncated"),
            "preview_row_count": 0, "preview_truncated": bool(rows),
        })

    def serialize():
        body = previews[0] if len(previews) == 1 else {"batch_results": previews}
        return json.dumps(body, ensure_ascii=False, separators=(",", ":"))

    encoded = serialize()
    while len(encoded.encode("utf-8")) > MAX_CONTEXT_BYTES:
        widest = max(previews, key=lambda p: len(json.dumps(p["columns"], ensure_ascii=False).encode("utf-8")))
        if not widest["columns"]:
            raise ValueError("result context metadata exceeds budget")
        widest["columns"].pop()
        widest["columns_preview_truncated"] = True
        encoded = serialize()
    # Share the byte budget across the batch so one large result cannot starve
    # every other result. Prefix-only previews retain answer-reference indices.
    stopped = set()
    for index in range(MAX_CONTEXT_ROWS):
        for number, (execution, preview) in enumerate(zip(executions, previews)):
            if number in stopped or index >= len(execution["rows"]):
                continue
            preview["rows"].append(execution["rows"][index])
            preview["preview_row_count"] = len(preview["rows"])
            preview["preview_truncated"] = len(preview["rows"]) < len(execution["rows"])
            candidate = serialize()
            if len(candidate.encode("utf-8")) <= MAX_CONTEXT_BYTES:
                encoded = candidate
            else:
                preview["rows"].pop()
                preview["preview_row_count"] = len(preview["rows"])
                preview["preview_truncated"] = len(preview["rows"]) < len(execution["rows"])
                stopped.add(number)
    return encoded

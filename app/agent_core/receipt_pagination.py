"""Bounded receipt pages with whole-result reconciliation and change detection."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import time
from typing import Callable

import sqlglot
from sqlglot import exp

from ..sql import SqlSafetyValidator
from .inbound_details import InboundDetailPlan, SOURCES, compile_inbound_details, reconcile_inbound_details
from .semantic_types import SemanticRuntimeError, _parse_decimal


MAX_RECEIPT_ROWS = 100_000
MAX_RECEIPT_BYTES = 64 * 1024 * 1024


def compile_receipt_page(plan: InboundDetailPlan, *, offset: int, page_size: int = 200, contract_root: Path | None = None) -> str:
    if type(offset) is not int or offset < 0 or type(page_size) is not int or not 1 <= page_size <= 500:
        raise ValueError("Invalid receipt page bounds")
    base = sqlglot.parse_one(compile_inbound_details(plan, contract_root=contract_root), read="doris")
    base.set("limit", None)
    order = base.args.pop("order")
    base.select(exp.alias_(exp.Window(this=exp.RowNumber(), order=order.copy()), "result_ordinal"), append=True, copy=False)
    # Controls and ordinals are calculated on the full scope before paging.
    page = exp.select("*").from_(base.subquery("controlled_receipts")).where(
        exp.and_(
            exp.GT(this=exp.column("result_ordinal"), expression=exp.Literal.number(offset)),
            exp.LTE(this=exp.column("result_ordinal"), expression=exp.Literal.number(offset + page_size)),
        )
    ).order_by("result_ordinal").limit(page_size)
    sql = page.sql(dialect="doris")
    SqlSafetyValidator().validate(sql, allowed_sources=SOURCES)
    return sql


def _count(value):
    number = _parse_decimal(value)
    if number < 0 or number != number.to_integral_value():
        raise ValueError("Invalid page control")
    return int(number)


@dataclass
class _ReadPass:
    rows: list
    digest: str
    row_count: int


def retrieve_receipt_details(
    plan: InboundDetailPlan, *, query: Callable, contract_root: Path | None = None,
    page_size: int = 200, max_rows: int = MAX_RECEIPT_ROWS,
    max_bytes: int = MAX_RECEIPT_BYTES, timeout_seconds: float = 180,
):
    if type(max_rows) is not int or not 1 <= max_rows <= MAX_RECEIPT_ROWS:
        raise ValueError("Invalid complete-result capacity")
    if max_bytes <= 0 or timeout_seconds <= 0:
        raise ValueError("Invalid receipt retrieval budget")
    deadline = time.monotonic() + timeout_seconds

    def read_pass(*, retain_rows=True):
        offset, expected, byte_count = 0, None, 0
        rows, digest = [], sha256()
        while expected is None or offset < expected:
            if time.monotonic() >= deadline:
                raise SemanticRuntimeError("分批查询超时，未形成完整结果", reason="query_timeout")
            batch = query(compile_receipt_page(plan, offset=offset, page_size=page_size, contract_root=contract_root), purpose="purchase receipts: bounded page and full-scope controls")
            if time.monotonic() >= deadline:
                raise SemanticRuntimeError("分批查询超时，未形成完整结果", reason="query_timeout")
            if expected is None:
                expected = _count(batch[0].get("matched_count")) if batch else 0
                if expected > max_rows:
                    raise SemanticRuntimeError("完整明细超过单次处理容量，未展示部分结果", reason="result_truncated")
            if len(batch) != min(page_size, expected - offset):
                raise ValueError("Receipt page is missing or repeated")
            for index, row in enumerate(batch, start=offset + 1):
                if _count(row.get("result_ordinal")) != index or _count(row.get("matched_count")) != expected:
                    raise ValueError("Receipt page controls changed")
                encoded = json.dumps(row, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8")
                byte_count += len(encoded)
                if byte_count > max_bytes:
                    raise SemanticRuntimeError("完整明细超过单次处理容量，未展示部分结果", reason="result_truncated")
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
                if retain_rows:
                    rows.append(row)
            offset += len(batch)
        return _ReadPass(rows, digest.hexdigest(), offset)

    try:
        first = read_pass()
        reconciled = reconcile_inbound_details(first.rows, plan, complete_limit=max_rows)
        second = read_pass(retain_rows=False)
        if first.digest != second.digest or first.row_count != second.row_count:
            raise SemanticRuntimeError("查询期间源数据发生变化，请重新查询", reason="receipt_source_changed")
        return reconciled
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise SemanticRuntimeError("分批明细未通过完整性检查，暂未展示结果", reason="receipt_page_invalid") from exc

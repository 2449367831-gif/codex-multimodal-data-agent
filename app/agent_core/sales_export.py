"""Reconstruct restartable export positions from source, never saved result keys."""

from dataclasses import asdict
from fractions import Fraction
from hashlib import sha256
import json
import time

from .sales_detail_batches import (
    SalesDetailCursor, changed, compile_prefix_control, definition_signature,
    retrieve_sales_detail_batch,
)
from .sales_detail_partitions import _ctes, _summary, _count, _decimal, compile_sales_detail_control
from .semantic_types import SemanticRuntimeError


EXPORT_ROWS = 50000


def summary_digest(summary):
    return sha256(json.dumps(asdict(summary), default=str, sort_keys=True,
                             separators=(',', ':')).encode()).hexdigest()


def position_digest(cursor):
    return sha256(json.dumps([cursor.after, cursor.loaded, cursor.documents, str(cursor.quantity)],
                             separators=(',', ':')).encode()).hexdigest()


def retrieve_export_chunk(plan, definition, baseline_digest, offset, query, *, query_many=None,
                          chunk_rows=EXPORT_ROWS, previous_digest=None):
    if type(offset) is not int or offset < 0 or type(chunk_rows) is not int or not 1 <= chunk_rows <= EXPORT_ROWS:
        raise ValueError('invalid export position')
    deadline = time.monotonic() + 180

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise SemanticRuntimeError('本批导出超时，已保存进度不变', reason='query_timeout')
        return value

    if definition_signature(plan) != definition:
        raise changed()
    baseline = _summary(query(compile_sales_detail_control(plan), purpose='export baseline control'), plan)
    if summary_digest(baseline) != baseline_digest or offset >= baseline.detail_count:
        raise changed()
    cursor = SalesDetailCursor(plan, baseline, definition)
    if offset:
        # Only the row ordinal is durable. Source keys and cumulative business
        # quantities are reconstructed in memory after every restart/retry.
        rows = query(f'''WITH {_ctes(plan, plan.start, plan.end)}
SELECT cargo_owner,outbound_code,sku_code FROM facts
ORDER BY cargo_owner,outbound_code,sku_code LIMIT 1 OFFSET {offset - 1}''', purpose='export boundary')
        remaining()
        if len(rows) != 1:
            raise changed()
        try:
            key = tuple(rows[0][field] for field in ('cargo_owner', 'outbound_code', 'sku_code'))
            position = SalesDetailCursor(plan, baseline, definition, key, offset, 0, Fraction(0), 2)
            controls = query(compile_prefix_control(position), purpose='export prefix control')
            if len(controls) != 1 or _count(controls[0]['detail_count']) != offset:
                raise changed()
            cursor = SalesDetailCursor(plan, baseline, definition, key, offset,
                _count(controls[0]['document_count']), Fraction(_decimal(controls[0]['quantity'])), 2)
        except (KeyError, TypeError, ValueError):
            raise changed() from None
        if position_digest(cursor) != previous_digest:
            raise changed()
    return retrieve_sales_detail_batch(cursor, query, query_many=query_many, max_rows=chunk_rows,
                                       batch_cap=EXPORT_ROWS, timeout_seconds=remaining())

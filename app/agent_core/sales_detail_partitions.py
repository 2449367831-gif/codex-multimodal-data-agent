"""Bounded monthly retrieval at owner/document/SKU grain, never physical-line export.

This compiler-owned core uses the existing actual-sales contract. Callers must
resolve the business variant and use the governed query transport before use.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import json
import math
import time
from types import MappingProxyType

import sqlglot

from ..query_scope import QueryScope, apply_scope
from .sales_aggregation import SalesAggregation, compile_sales_aggregation
from .sales_pages import retrieve_sales_groups
from .semantic_types import SemanticRuntimeError, _parse_source_datetime


@dataclass(frozen=True, slots=True)
class SalesDetailPartitionPlan:
    start: date
    end: date
    scope: QueryScope = field(default_factory=QueryScope)

    def __post_init__(self):
        if type(self.start) is not date or type(self.end) is not date or self.end <= self.start:
            raise ValueError('explicit nonempty natural-day interval required')
        if not isinstance(self.scope, QueryScope) or self.scope.channel != 'ALL' or self.scope.unit != 'EA':
            raise ValueError('unsupported actual-sales detail scope')


@dataclass(frozen=True, slots=True)
class SalesDetailSummary:
    quantity: Decimal
    document_count: int
    detail_count: int
    latest_shipment_at: datetime | None


@dataclass(frozen=True, slots=True)
class PartitionedSalesDetails:
    summary: SalesDetailSummary
    partitions: tuple[tuple[date, date], ...]
    rows: tuple | None
    complete: bool


def month_windows(start, end):
    SalesDetailPartitionPlan(start, end)
    windows = []
    while start < end:
        boundary = (date(start.year + 1, 1, 1) if start.month == 12 else date(start.year, start.month + 1, 1)) if start.year < 9999 or start.month < 12 else end
        stop = min(boundary, end)
        windows.append((start, stop))
        start = stop
    return tuple(windows)


def _ctes(plan, start, end):
    base = compile_sales_aggregation(SalesAggregation(), start=start, end=end)
    scoped = apply_scope(base, plan.scope, legacy_owner='HJXH')
    tree = sqlglot.parse_one(scoped, read='doris')
    return ',\n'.join(cte.sql(dialect='doris') for cte in tree.args['with_'].expressions)


def compile_sales_detail_control(plan):
    return f"""WITH {_ctes(plan, plan.start, plan.end)}
SELECT c.total_quantity, c.latest_shipment_at, c.null_quantity_count, c.negative_quantity_count,
 c.blank_key_count, hc.header_conflict_count,
 (SELECT COUNT(*) FROM all_documents) AS total_outbound_count,
 (SELECT COUNT(*) FROM facts) AS detail_count,
 (SELECT COUNT(*) FROM facts WHERE COALESCE(warehouse_code,'')='') AS blank_warehouse_count
FROM controls c CROSS JOIN header_controls hc LIMIT 1"""


def compile_sales_detail_partition(plan, start, end):
    if type(start) is not date or type(end) is not date or not plan.start <= start < end <= plan.end:
        raise ValueError('partition must stay inside the original interval')
    return f"""WITH {_ctes(plan, start, end)}
SELECT cargo_owner, warehouse_code, outbound_code, sku_code, ship_at, quantity,
 header_variants, null_quantity_count, negative_quantity_count,
 COUNT(*) OVER() AS group_count
FROM facts ORDER BY cargo_owner, outbound_code, sku_code LIMIT 501"""


def _invalid():
    return SemanticRuntimeError('分片明细未通过完整范围对账，暂未展示', reason='sales_partition_reconciliation_failed')


def _decimal(value):
    if isinstance(value, bool) or value is None:
        raise _invalid()
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise _invalid() from None
    if not number.is_finite() or number < 0:
        raise _invalid()
    return number


def _count(value):
    number = _decimal(value)
    if number != number.to_integral_value():
        raise _invalid()
    return int(number)


def _summary(rows, plan):
    if not isinstance(rows, (tuple, list)) or len(rows) != 1:
        raise _invalid()
    row = rows[0]
    try:
        if any(_count(row[key]) for key in ('null_quantity_count', 'negative_quantity_count',
                'blank_key_count', 'header_conflict_count', 'blank_warehouse_count')):
            raise _invalid()
        count = _count(row['detail_count'])
        stamp = _parse_source_datetime(row['latest_shipment_at'], timezone_name='Asia/Shanghai') if count else None
        if (stamp is not None and not plan.start <= stamp.date() < plan.end) or (not count and row['latest_shipment_at'] is not None):
            raise _invalid()
        result = SalesDetailSummary(_decimal(row['total_quantity']),
                                    _count(row['total_outbound_count']), count, stamp)
        if (result.document_count > result.detail_count or (result.detail_count > 0 and result.document_count == 0)
            or (result.detail_count == 0 and result.quantity != 0)):
            raise _invalid()
        return result
    except (KeyError, TypeError, ValueError):
        raise _invalid() from None


def retrieve_partitioned_sales_details(plan, query, *, query_many=None, max_rows=100000,
                                       max_bytes=64*1024*1024, timeout_seconds=180):
    if (not isinstance(plan, SalesDetailPartitionPlan)
        or type(max_rows) is not int or not 1 <= max_rows <= 100000
        or type(max_bytes) is not int or not 1 <= max_bytes <= 64*1024*1024
        or isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 180):
        raise ValueError('invalid partitioned retrieval budget')
    deadline = time.monotonic() + timeout_seconds

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise SemanticRuntimeError('分片查询超时，未返回完整明细', reason='query_timeout')
        return value

    def control():
        remaining()
        result = _summary(query(compile_sales_detail_control(plan), purpose='sales detail full-range control'), plan)
        remaining()
        return result

    windows = month_windows(plan.start, plan.end)
    before = control()
    if before.detail_count > max_rows:
        return PartitionedSalesDetails(before, windows, None, False)
    rows, keys, documents = [], set(), set()
    quantity, byte_count = Fraction(0), 0
    try:
        for start, end in windows:
            # A full budget still permits proving that later partitions are empty.
            # Any newly observed row must fail before being added to retained data.
            batch = retrieve_sales_groups(compile_sales_detail_partition(plan, start, end), query,
                query_many=query_many, max_rows=max(1, max_rows-len(rows)), max_bytes=max(1, max_bytes-byte_count),
                timeout_seconds=remaining())
            if len(rows) + len(batch) > max_rows:
                raise SemanticRuntimeError('完整明细超过处理容量', reason='result_truncated')
            for raw in batch:
                byte_count += len(json.dumps(raw, ensure_ascii=False, sort_keys=True,
                                            separators=(',', ':'), default=str).encode())
                if byte_count > max_bytes:
                    raise SemanticRuntimeError('完整明细超过处理容量', reason='result_truncated')
                key = tuple(raw[field] for field in ('cargo_owner', 'outbound_code', 'sku_code'))
                warehouse = raw['warehouse_code']
                if key in keys or not all(isinstance(value, str) and value.strip() == value and value for value in (*key, warehouse)):
                    raise _invalid()
                if ((plan.scope.owner != 'ALL' and key[0] != plan.scope.owner)
                    or (plan.scope.warehouse_codes and warehouse not in plan.scope.warehouse_codes)
                    or (plan.scope.sku_codes and key[2] not in plan.scope.sku_codes)):
                    raise _invalid()
                stamp = _parse_source_datetime(raw['ship_at'], timezone_name='Asia/Shanghai')
                if not start <= stamp.date() < end:
                    raise _invalid()
                if _count(raw['header_variants']) != 1 or _count(raw['null_quantity_count']) or _count(raw['negative_quantity_count']):
                    raise _invalid()
                quantity += Fraction(_decimal(raw['quantity']))
                keys.add(key)
                documents.add(key[:2])
                rows.append(MappingProxyType({field: raw[field] for field in (
                    'cargo_owner', 'warehouse_code', 'outbound_code', 'sku_code', 'ship_at', 'quantity')}))
            remaining()
        after = control()
        if (before != after or len(rows) != before.detail_count or len(documents) != before.document_count
            or quantity != Fraction(before.quantity)):
            raise _invalid()
    except (KeyError, TypeError, ValueError, ArithmeticError):
        raise _invalid() from None
    return PartitionedSalesDetails(before, windows, tuple(rows), True)

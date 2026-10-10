"""Reconciled source observations, without inferred unit or ingestion claims."""

from collections import Counter
from dataclasses import dataclass, field

from .dms_points_orders import _codes, _count
from .semantic_types import _parse_source_datetime


SOURCE_FIELDS = ('header_updated_at', 'detail_updated_at',
                 'quantity_unit_reference', 'sales_unit_reference')
COUNT_CONTROLS = ('missing_header_updated_count', 'missing_detail_updated_count',
                  'missing_quantity_unit_reference_count', 'missing_sales_unit_reference_count')
TIME_CONTROLS = ('source_header_latest_updated', 'source_detail_latest_updated')


@dataclass(frozen=True)
class SalesOrderSourceEvidence:
    missing_header_updated_count: int
    missing_detail_updated_count: int
    missing_quantity_unit_reference_count: int
    missing_sales_unit_reference_count: int
    source_header_latest_updated: str | None
    source_detail_latest_updated: str | None
    unit_reference_pairs: tuple[tuple[str | None, str | None, int], ...]
    ingestion_freshness: str = field(default='unverified', init=False)
    quantity_unit_status: str = field(default='unconfirmed', init=False)


def _stamp(value):
    return _parse_source_datetime(value, timezone_name='Asia/Shanghai') if value is not None else None


def normalize_order_source_fields(row):
    values = {}
    for name in SOURCE_FIELDS:
        value = row[name]
        if name.endswith('_at'):
            stamp = _stamp(value)
            values[name] = stamp.isoformat() if stamp is not None else None
        else:
            if value is not None:
                _codes((value,))
            values[name] = value
    if row['detail_id'] is None and any(values[name] is not None for name in SOURCE_FIELDS[1:]):
        raise ValueError('missing detail cannot carry source evidence')
    return values


def reconcile_order_source_evidence(source_rows, checked_rows):
    if len(source_rows) != len(checked_rows):
        raise ValueError('source evidence row count changed')
    def controls(row):
        return tuple(_count(row[name]) for name in COUNT_CONTROLS) + tuple(_stamp(row[name]) for name in TIME_CONTROLS)
    expected = controls(source_rows[0]) if source_rows else (0, 0, 0, 0, None, None)
    headers, details, pairs = {}, [], Counter()
    for source, row in zip(source_rows, checked_rows, strict=True):
        if controls(source) != expected:
            raise ValueError('source evidence controls changed')
        key = row['cargo_owner'], row['sales_order_code']
        stamp = _stamp(row['header_updated_at'])
        if key in headers and headers[key] != stamp:
            raise ValueError('order header timestamps disagree across detail rows')
        headers[key] = stamp
        if row['detail_id'] is not None:
            details.append(row)
            pairs[(row['quantity_unit_reference'], row['sales_unit_reference'])] += 1
    header_times = [stamp for stamp in headers.values() if stamp is not None]
    detail_times = [_stamp(row['detail_updated_at']) for row in details if row['detail_updated_at'] is not None]
    actual = (sum(stamp is None for stamp in headers.values()),
              sum(row['detail_updated_at'] is None for row in details),
              sum(row['quantity_unit_reference'] is None for row in details),
              sum(row['sales_unit_reference'] is None for row in details),
              max(header_times, default=None), max(detail_times, default=None))
    if actual != expected:
        raise ValueError('source evidence does not reconcile')
    ordered = sorted(pairs, key=lambda pair: tuple((value is not None, value or '') for value in pair))
    return SalesOrderSourceEvidence(*actual[:4],
        *(stamp.isoformat() if stamp is not None else None for stamp in actual[4:]),
        tuple((*pair, pairs[pair]) for pair in ordered))

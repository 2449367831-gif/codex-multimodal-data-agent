"""Resume bounded actual-sales batches without narrowing the original scope."""

from dataclasses import dataclass
from fractions import Fraction
from hashlib import sha256
import math
from pathlib import Path
import time
from types import MappingProxyType

from sqlglot import exp

from ..knowledge_activation import active_snapshot
from .sales_detail_partitions import (
    SalesDetailPartitionPlan, SalesDetailSummary, _count, _decimal, _summary,
    _ctes, compile_sales_detail_control,
)
from .sales_detail_planning import actual_details_enabled
from .sales_pages import retrieve_sales_groups
from .semantic_types import SemanticRuntimeError, _parse_source_datetime


KEYS = ('cargo_owner', 'outbound_code', 'sku_code')
FIELDS = ('cargo_owner', 'warehouse_code', 'outbound_code', 'sku_code', 'ship_at', 'quantity')


def definition_signature(plan):
    root, version = active_snapshot(Path(__file__).resolve().parents[2])
    metric = (root / 'metrics/sales.outbound_actual.v2.yaml').read_bytes()
    return sha256(metric + (version or '').encode() + compile_sales_detail_control(plan).encode()).hexdigest()


def changed():
    return SemanticRuntimeError('原查询范围的数据或口径已变化，请重新查询；已取回批次不再作为完整结果',
                                reason='sales_detail_source_changed')


@dataclass(frozen=True, slots=True)
class SalesDetailCursor:
    plan: SalesDetailPartitionPlan
    baseline: SalesDetailSummary
    definition: str
    after: tuple[str, str, str] | None = None
    loaded: int = 0
    documents: int = 0
    quantity: Fraction = Fraction(0)
    batch_index: int = 1

    def __post_init__(self):
        if (type(self.plan) is not SalesDetailPartitionPlan or type(self.baseline) is not SalesDetailSummary
            or not isinstance(self.definition, str) or len(self.definition) != 64
            or any(type(n) is not int or n < 0 for n in (self.loaded, self.documents))
            or not 0 <= self.documents <= self.loaded <= self.baseline.detail_count
            or type(self.batch_index) is not int or self.batch_index < 1
            or type(self.quantity) is not Fraction or self.quantity < 0
            or self.quantity > Fraction(self.baseline.quantity)
            or ((self.after is None) != (self.loaded == 0))
            or (self.after is not None and (type(self.after) is not tuple or len(self.after) != 3
                or any(not isinstance(x, str) or not x or x != x.strip() for x in self.after)))
            or (not self.loaded and (self.quantity or self.documents or self.batch_index != 1))):
            raise ValueError('invalid checked sales detail cursor')

    @property
    def complete(self):
        return self.loaded == self.baseline.detail_count


@dataclass(frozen=True, slots=True)
class SalesDetailBatch:
    source: SalesDetailCursor
    cursor: SalesDetailCursor
    rows: tuple


def key_predicate(key, *, prefix=False):
    if key is None:
        return exp.false() if prefix else exp.true()
    if type(key) is not tuple or len(key) != 3 or any(not isinstance(value, str) or not value for value in key):
        raise ValueError('invalid detail continuation key')
    greater = exp.or_(*(exp.and_(
        *(exp.EQ(this=exp.column(KEYS[j]), expression=exp.Literal.string(key[j])) for j in range(i)),
        exp.GT(this=exp.column(KEYS[i]), expression=exp.Literal.string(key[i])),
    ) for i in range(3)))
    return exp.not_(greater) if prefix else greater


def compile_batch(cursor, size):
    if type(size) is not int or not 1 <= size <= 100000:
        raise ValueError('invalid detail batch size')
    return f"""WITH {_ctes(cursor.plan, cursor.plan.start, cursor.plan.end)}, batch_facts AS (
 SELECT * FROM facts WHERE {key_predicate(cursor.after).sql(dialect='doris')}
 ORDER BY cargo_owner,outbound_code,sku_code LIMIT {size}
)
SELECT cargo_owner,warehouse_code,outbound_code,sku_code,ship_at,quantity,
 header_variants,null_quantity_count,negative_quantity_count,COUNT(*) OVER() AS group_count
FROM batch_facts ORDER BY cargo_owner,outbound_code,sku_code"""


def compile_prefix_control(cursor):
    return f"""WITH {_ctes(cursor.plan, cursor.plan.start, cursor.plan.end)}, prefix_facts AS (
 SELECT * FROM facts WHERE {key_predicate(cursor.after, prefix=True).sql(dialect='doris')}
), prefix_documents AS (SELECT DISTINCT cargo_owner,outbound_code FROM prefix_facts)
SELECT COUNT(*) AS detail_count,COALESCE(SUM(quantity),0) AS quantity,
 (SELECT COUNT(*) FROM prefix_documents) AS document_count FROM prefix_facts LIMIT 1"""


def retrieve_sales_detail_batch(cursor, query, *, query_many=None, max_rows=5000, timeout_seconds=180,
                                batch_cap=5000):
    if (type(cursor) is not SalesDetailCursor or cursor.complete
        or type(batch_cap) is not int or not 1 <= batch_cap <= 100000
        or type(max_rows) is not int or not 1 <= max_rows <= batch_cap
        or isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 180):
        raise ValueError('invalid sales detail batch request')
    deadline = time.monotonic() + timeout_seconds

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise SemanticRuntimeError('本批取回超时，进度未推进，可重试本批', reason='query_timeout')
        return value

    def control(current, *, full):
        remaining()
        statement = compile_sales_detail_control(cursor.plan) if full else compile_prefix_control(current)
        rows = query(statement, purpose='sales detail batch range control')
        remaining()
        if full:
            valid = _summary(rows, cursor.plan) == cursor.baseline
        else:
            valid = (len(rows) == 1 and _count(rows[0]['detail_count']) == current.loaded
                     and _count(rows[0]['document_count']) == current.documents
                     and Fraction(_decimal(rows[0]['quantity'])) == current.quantity)
        if not valid:
            raise changed()

    def contract():
        if not actual_details_enabled() or definition_signature(cursor.plan) != cursor.definition:
            raise changed()

    contract()
    control(cursor, full=True)
    if cursor.after:
        control(cursor, full=False)
    raw_rows = retrieve_sales_groups(compile_batch(cursor, max_rows), query, query_many=query_many,
                                    page_size=min(max_rows, 10000), page_cap=10000,
                                    max_rows=max_rows, timeout_seconds=remaining())
    if len(raw_rows) != min(max_rows, cursor.baseline.detail_count - cursor.loaded):
        raise changed()
    last, quantity, documents = cursor.after, cursor.quantity, cursor.documents
    rows = []
    try:
        for raw in raw_rows:
            key = tuple(raw[field] for field in KEYS)
            warehouse = raw['warehouse_code']
            if (any(not isinstance(x, str) or not x or x != x.strip() for x in (*key, warehouse))
                or (last is not None and key <= last)
                or (cursor.plan.scope.owner != 'ALL' and key[0] != cursor.plan.scope.owner)
                or (cursor.plan.scope.warehouse_codes and warehouse not in cursor.plan.scope.warehouse_codes)
                or (cursor.plan.scope.sku_codes and key[2] not in cursor.plan.scope.sku_codes)):
                raise changed()
            stamp = _parse_source_datetime(raw['ship_at'], timezone_name='Asia/Shanghai')
            if (not cursor.plan.start <= stamp.date() < cursor.plan.end or _count(raw['header_variants']) != 1
                or _count(raw['null_quantity_count']) or _count(raw['negative_quantity_count'])):
                raise changed()
            documents += int(last is None or key[:2] != last[:2])
            quantity += Fraction(_decimal(raw['quantity']))
            last = key
            rows.append(MappingProxyType({field: raw[field] for field in FIELDS}))
        following = SalesDetailCursor(cursor.plan, cursor.baseline, cursor.definition, last,
            cursor.loaded + len(rows), documents, quantity, cursor.batch_index + 1)
        control(following, full=False)
        control(following, full=True)
        contract()
        if following.complete and (following.documents != cursor.baseline.document_count
                                   or following.quantity != Fraction(cursor.baseline.quantity)):
            raise changed()
    except (KeyError, TypeError, ValueError, ArithmeticError):
        raise changed() from None
    remaining()
    return SalesDetailBatch(cursor, following, tuple(rows))

"""Candidate return facts, preaggregated independently of sales and money."""

from dataclasses import dataclass
from decimal import Decimal, Inexact, localcontext

import sqlglot
from sqlglot import exp

from ..sql import SqlSafetyValidator
from .dms_points_orders import _codes, _count
from .semantic_types import SemanticRuntimeError, _parse_source_datetime


SOURCES = frozenset({'ywzt_ods.scm_return_order_header', 'ywzt_ods.scm_return_order_detail'})
KEYS = ('cargo_owner', 'sales_order_code', 'return_order_code', 'sku_code')
COUNTS = ('return_detail_count', 'negative_detail_count', 'missing_update_count')
FIELDS = KEYS + ('returned_qty', 'return_document_count') + COUNTS + ('latest_update',)
CONTROLS = ('total_returned_qty', 'total_detail_count', 'total_document_count',
            'total_negative_detail_count', 'total_missing_update_count', 'group_count', 'quality_errors')
QUALITY = ('invalid_header_count', 'invalid_detail_count', 'duplicate_detail_key_count',
           'missing_header_count', 'reference_conflict_count')
QUALITY_DETAILS = ('header_missing_identity', 'header_invalid_delete', 'header_multiple_active',
                   'detail_missing_identity', 'detail_missing_quantity', 'detail_invalid_delete',
                   'orphan_details', 'orphan_missing_reference', 'conflicting_detail_reference')


@dataclass(frozen=True)
class ReturnComponentPlan:
    owner_codes: tuple[str, ...] = ()
    order_codes: tuple[str, ...] = ()
    sku_codes: tuple[str, ...] = ()
    order_keys: tuple[tuple[str, str], ...] | None = None

    def __post_init__(self):
        for codes in (self.owner_codes, self.order_codes, self.sku_codes):
            _codes(codes)
        if self.order_keys is not None:
            if not isinstance(self.order_keys, tuple) or any(not isinstance(key, tuple) or len(key) != 2 for key in self.order_keys):
                raise ValueError('exact owner/order pairs required')
            for key in self.order_keys:
                for value in key: _codes((value,))
            if len(set(self.order_keys)) != len(self.order_keys):
                raise ValueError('duplicate order cohort key')


def compile_return_components(plan):
    if not isinstance(plan, ReturnComponentPlan):
        raise ValueError('return component plan required')

    def scope(field, codes):
        return (f'{field} IN (' + ','.join(exp.Literal.string(code).sql(dialect='doris') for code in codes) + ')') if codes else 'TRUE'

    def cohort(owner_field, order_field):
        if plan.order_keys is None: return 'TRUE'
        return '(' + ' OR '.join('(' + scope(owner_field, (owner,)) + ' AND ' + scope(order_field, (order,)) + ')'
                                for owner, order in plan.order_keys) + ')' if plan.order_keys else 'FALSE'

    owner = scope("NULLIF(TRIM(company_code),'')", plan.owner_codes)
    detail_owner = scope("NULLIF(TRIM(d.company_code),'')", plan.owner_codes)
    orphan_orders = scope("NULLIF(TRIM(d.ref_order_code),'')", plan.order_codes) + ' AND ' + cohort("NULLIF(TRIM(d.company_code),'')", "NULLIF(TRIM(d.ref_order_code),'')")
    orders = scope('sales_order_code', plan.order_codes) + ' AND ' + cohort('cargo_owner', 'sales_order_code')
    skus = scope("NULLIF(TRIM(d.sku_code),'')", plan.sku_codes)
    null_fields = ','.join('CAST(NULL AS ' + ('STRING' if name in KEYS else 'DATETIME' if name == 'latest_update' else 'DECIMAL(38,4)' if name == 'returned_qty' else 'BIGINT') + ') AS ' + name for name in FIELDS)
    sql = f"""WITH header_raw AS (
 SELECT NULLIF(TRIM(company_code),'') AS cargo_owner,
   NULLIF(TRIM(return_order_code),'') AS return_order_code,
   NULLIF(TRIM(ref_order_code),'') AS sales_order_code,
   _fdl_marked_deleted AS deleted
 FROM ywzt_ods.scm_return_order_header WHERE {owner}
), selected_keys AS (
 SELECT DISTINCT cargo_owner,return_order_code FROM header_raw h
 WHERE (deleted IS NULL OR deleted<>'true') AND (({orders}) OR EXISTS (
   SELECT 1 FROM ywzt_ods.scm_return_order_detail d
   WHERE h.cargo_owner<=>NULLIF(TRIM(d.company_code),'')
     AND h.return_order_code<=>NULLIF(TRIM(d.return_order_code),'')
     AND d.status=0 AND (d._fdl_marked_deleted IS NULL OR d._fdl_marked_deleted<>'true')
     AND {detail_owner} AND {skus} AND ({orphan_orders})
 ))
), headers AS (
 SELECT h.cargo_owner,h.return_order_code,MAX(h.sales_order_code) AS sales_order_code,
   SUM(CASE WHEN h.deleted='false' THEN 1 ELSE 0 END) AS header_count,
   SUM(CASE WHEN h.deleted IS NULL OR h.deleted NOT IN ('false','true')
         OR h.cargo_owner IS NULL OR h.return_order_code IS NULL OR h.sales_order_code IS NULL THEN 1 ELSE 0 END) AS errors
 FROM header_raw h WHERE (h.deleted IS NULL OR h.deleted<>'true') AND EXISTS (
   SELECT 1 FROM selected_keys k WHERE k.cargo_owner<=>h.cargo_owner AND k.return_order_code<=>h.return_order_code
 ) GROUP BY h.cargo_owner,h.return_order_code
), details AS (
 SELECT NULLIF(TRIM(d.company_code),'') AS cargo_owner,NULLIF(TRIM(d.return_order_code),'') AS return_order_code,
   NULLIF(TRIM(d.id),'') AS detail_id,NULLIF(TRIM(d.sku_code),'') AS sku_code,
   NULLIF(TRIM(d.ref_order_code),'') AS detail_reference,d.fulfill_qty AS returned_qty,
   d.updated_date AS latest_update,d._fdl_marked_deleted AS deleted
 FROM ywzt_ods.scm_return_order_detail d
 WHERE d.status=0 AND (d._fdl_marked_deleted IS NULL OR d._fdl_marked_deleted<>'true') AND {skus} AND {detail_owner}
   AND (EXISTS (SELECT 1 FROM selected_keys k
     WHERE k.cargo_owner<=>NULLIF(TRIM(d.company_code),'') AND k.return_order_code<=>NULLIF(TRIM(d.return_order_code),''))
     OR ({orphan_orders} AND NOT EXISTS (SELECT 1 FROM header_raw h
       WHERE h.cargo_owner<=>NULLIF(TRIM(d.company_code),'') AND h.return_order_code<=>NULLIF(TRIM(d.return_order_code),''))))
), linked AS (
 SELECT d.*,h.sales_order_code FROM details d JOIN headers h
 ON d.cargo_owner=h.cargo_owner AND d.return_order_code=h.return_order_code
 WHERE h.header_count>0
), document_skus AS (
 SELECT cargo_owner,sales_order_code,return_order_code,sku_code,
   SUM(CAST(returned_qty AS DECIMAL(38,4))) AS returned_qty,1 AS return_document_count,
   COUNT(*) AS return_detail_count,
   SUM(CASE WHEN returned_qty<0 THEN 1 ELSE 0 END) AS negative_detail_count,
   SUM(CASE WHEN latest_update IS NULL THEN 1 ELSE 0 END) AS missing_update_count,
   MAX(latest_update) AS latest_update
 FROM linked GROUP BY cargo_owner,sales_order_code,return_order_code,sku_code
), control AS (
 SELECT COALESCE(SUM(CAST(returned_qty AS DECIMAL(38,4))),0) AS total_returned_qty,
   COUNT(*) AS total_detail_count,
   COALESCE(SUM(CASE WHEN returned_qty<0 THEN 1 ELSE 0 END),0) AS total_negative_detail_count,
   COALESCE(SUM(CASE WHEN latest_update IS NULL THEN 1 ELSE 0 END),0) AS total_missing_update_count,
   MAX(latest_update) AS total_latest_update
 FROM details
), quality AS (
 SELECT (SELECT COUNT(*) FROM headers WHERE header_count<>1 OR errors>0) AS invalid_header_count,
   (SELECT COUNT(*) FROM details WHERE detail_id IS NULL OR sku_code IS NULL OR returned_qty IS NULL
        OR cargo_owner IS NULL OR return_order_code IS NULL OR deleted IS NULL OR deleted<>'false') AS invalid_detail_count,
   (SELECT COUNT(*) FROM (SELECT cargo_owner,detail_id FROM details GROUP BY cargo_owner,detail_id HAVING COUNT(*)>1) duplicated) AS duplicate_detail_key_count,
   (SELECT COUNT(*) FROM details d WHERE NOT EXISTS (SELECT 1 FROM headers h
        WHERE h.cargo_owner<=>d.cargo_owner AND h.return_order_code<=>d.return_order_code)) AS missing_header_count,
   (SELECT COUNT(*) FROM linked WHERE detail_reference IS NULL OR sales_order_code IS NULL
        OR detail_reference<>sales_order_code) AS reference_conflict_count
), report AS (
 SELECT 'group' AS row_kind,{','.join(FIELDS)} FROM document_skus
 UNION ALL SELECT 'control',{null_fields}
)
SELECT r.*,c.*,
 (SELECT COUNT(*) FROM (SELECT DISTINCT cargo_owner,return_order_code FROM linked) documents) AS total_document_count,
 1+(SELECT COUNT(*) FROM document_skus) AS group_count,{' + '.join('q.' + name for name in QUALITY)} AS quality_errors
FROM report r CROSS JOIN control c CROSS JOIN quality q
ORDER BY row_kind,{','.join(KEYS)} LIMIT 501"""
    SqlSafetyValidator().validate(sql, allowed_sources=SOURCES)
    return sql


def compile_return_quality(plan):
    tree = sqlglot.parse_one(compile_return_components(plan), read='doris')
    query = exp.select(*QUALITY).from_('quality').limit(1)
    query.set('with_', tree.args['with_'].copy())
    sql = query.sql(dialect='doris')
    SqlSafetyValidator().validate(sql, allowed_sources=SOURCES)
    return sql


def compile_return_quality_details(plan):
    tree = sqlglot.parse_one(compile_return_components(plan), read='doris')
    eligible_header = """(h.deleted IS NULL OR h.deleted<>'true') AND EXISTS (
        SELECT 1 FROM selected_keys k WHERE k.cargo_owner<=>h.cargo_owner AND k.return_order_code<=>h.return_order_code)"""
    orphan = """NOT EXISTS (SELECT 1 FROM headers h
        WHERE h.cargo_owner<=>d.cargo_owner AND h.return_order_code<=>d.return_order_code)"""
    queries = (
        f"SELECT COUNT(*) FROM header_raw h WHERE {eligible_header} AND (h.cargo_owner IS NULL OR h.return_order_code IS NULL OR h.sales_order_code IS NULL)",
        f"SELECT COUNT(*) FROM header_raw h WHERE {eligible_header} AND (h.deleted IS NULL OR h.deleted<>'false')",
        'SELECT COUNT(*) FROM headers WHERE header_count>1',
        'SELECT COUNT(*) FROM details WHERE detail_id IS NULL OR sku_code IS NULL OR cargo_owner IS NULL OR return_order_code IS NULL',
        'SELECT COUNT(*) FROM details WHERE returned_qty IS NULL',
        "SELECT COUNT(*) FROM details WHERE deleted IS NULL OR deleted<>'false'",
        f'SELECT COUNT(*) FROM details d WHERE {orphan}',
        f'SELECT COUNT(*) FROM details d WHERE {orphan} AND d.detail_reference IS NULL',
        'SELECT COUNT(*) FROM linked WHERE detail_reference IS NULL OR sales_order_code IS NULL OR detail_reference<>sales_order_code',
    )
    projections = [exp.Subquery(this=sqlglot.parse_one(sql, read='doris')).as_(name)
                   for name, sql in zip(QUALITY_DETAILS, queries, strict=True)]
    query = exp.select(*projections).limit(1)
    query.set('with_', tree.args['with_'].copy())
    sql = query.sql(dialect='doris')
    SqlSafetyValidator().validate(sql, allowed_sources=SOURCES)
    return sql


@dataclass(frozen=True)
class ReturnComponentResult:
    groups: list[dict]
    total_quantity: Decimal
    document_count: int
    detail_count: int
    latest_update: str | None


def _quantity(value):
    if value is None or isinstance(value, bool):
        raise ValueError('missing return quantity')
    number = Decimal(str(value))
    if not number.is_finite() or number != number.to_integral_value():
        raise ValueError('invalid integer return quantity')
    return number


def _time(value):
    return _parse_source_datetime(value, timezone_name='Asia/Shanghai') if value is not None else None


def reconcile_return_components(rows, plan, *, complete_limit=100000):
    try:
        with localcontext() as context:
            context.prec = 64
            context.traps[Inexact] = True
            return _reconcile(rows, plan, complete_limit)
    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
        raise SemanticRuntimeError('退货来源或汇总未通过完整性检查，暂未展示', reason='return_component_quality_failed') from exc


def _reconcile(rows, plan, limit):
    if not isinstance(plan, ReturnComponentPlan) or type(limit) is not int or not 1 <= limit <= 100000 or not rows or len(rows) > limit:
        raise ValueError('incomplete return report')

    def controls(row):
        return {**{name: _quantity(row[name]) if name == 'total_returned_qty' else _count(row[name]) for name in CONTROLS},
                'total_latest_update': _time(row['total_latest_update'])}

    expected = controls(rows[0])
    if expected['quality_errors'] or expected['group_count'] != len(rows):
        raise ValueError('return source quality or completeness failed')
    seen, documents, groups = set(), {}, {}
    total, detail_count, negative_count, missing_count, control_count = Decimal(0), 0, 0, 0, 0
    latest = None
    for row in rows:
        if controls(row) != expected:
            raise ValueError('return controls changed')
        if row['row_kind'] == 'control':
            control_count += 1
            if any(row[name] is not None for name in FIELDS):
                raise ValueError('invalid return control row')
            continue
        if row['row_kind'] != 'group':
            raise ValueError('unknown return row')
        key = tuple(row[name] for name in KEYS)
        if key in seen or any(not isinstance(v, str) or not v or v != v.strip(' ') for v in key):
            raise ValueError('invalid return identity')
        owner, order, document, sku = key
        if plan.order_keys is not None and (owner, order) not in plan.order_keys:
            raise ValueError('return outside exact order cohort')
        if any(codes and value not in codes for value, codes in ((owner, plan.owner_codes), (order, plan.order_codes), (sku, plan.sku_codes))):
            raise ValueError('return scope changed')
        if (owner, document) in documents and documents[owner, document] != order:
            raise ValueError('return belongs to conflicting sales orders')
        documents[owner, document] = order
        seen.add(key)
        quantity = _quantity(row['returned_qty'])
        count, negative, missing = (_count(row[name]) for name in COUNTS)
        if _count(row['return_document_count']) != 1 or count == 0 or negative > count or missing > count:
            raise ValueError('invalid return counts')
        if quantity < 0 and negative == 0:
            raise ValueError('negative return without source anomaly')
        stamp = _time(row['latest_update'])
        if (stamp is None) != (missing == count):
            raise ValueError('invalid return freshness')
        latest = max(latest, stamp) if latest and stamp else latest or stamp
        aggregate_key = owner, order, sku
        target = groups.setdefault(aggregate_key, dict(cargo_owner=owner, sales_order_code=order, sku_code=sku,
            returned_qty=Decimal(0), return_document_count=0, return_detail_count=0,
            negative_detail_count=0, missing_update_count=0, latest_update=None))
        target['returned_qty'] += quantity
        target['return_document_count'] += 1
        for field in COUNTS:
            target[field] += _count(row[field])
        previous = _time(target['latest_update'])
        merged_time = max(previous, stamp) if previous and stamp else previous or stamp
        target['latest_update'] = merged_time.isoformat() if merged_time else None
        total += quantity
        detail_count += count
        negative_count += negative
        missing_count += missing
    if control_count != 1 or (total, detail_count, len(documents), negative_count, missing_count, latest) != (
        expected['total_returned_qty'], expected['total_detail_count'], expected['total_document_count'],
        expected['total_negative_detail_count'], expected['total_missing_update_count'], expected['total_latest_update']):
        raise ValueError('return prejoin controls do not reconcile')
    return ReturnComponentResult([groups[key] for key in sorted(groups)], total, len(documents), detail_count,
                                 latest.isoformat() if latest else None)


def retrieve_return_components(plan, query, *, query_many=None, **budgets):
    from .sales_pages import AggregatePageError, retrieve_sales_groups
    try:
        def checked(rows):
            if any(_count(row['quality_errors']) for row in rows):
                raise SemanticRuntimeError('退货来源存在关联或字段异常，暂未展示', reason='return_component_quality_failed')
            return rows

        def read(sql, **kwargs):
            return checked(query(sql, **kwargs))

        def many(statements, **kwargs):
            return [checked(rows) for rows in query_many(statements, **kwargs)]

        rows = retrieve_sales_groups(compile_return_components(plan), read,
                                     query_many=many if query_many is not None else None, **budgets)
        return reconcile_return_components(rows, plan, complete_limit=budgets.get('max_rows', 100000))
    except AggregatePageError as exc:
        raise AggregatePageError(exc.page_failure, exc.pass_number, reason='return_component_page_invalid') from exc
    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
        raise SemanticRuntimeError('退货来源或汇总未通过完整性检查，暂未展示', reason='return_component_quality_failed') from exc

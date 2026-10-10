"""Independent sales-order cohort and SKU components for return composition."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, Inexact, localcontext
from zoneinfo import ZoneInfo

from sqlglot import exp

from ..sql import SqlSafetyValidator
from .dms_points_orders import _codes, _count
from .semantic_types import SemanticRuntimeError, _parse_source_datetime


SOURCES = frozenset({'ywzt_ods.scm_sales_order_header', 'ywzt_ods.scm_sales_order_detail'})
FIELDS = ('cargo_owner', 'sales_order_code', 'sku_code', 'currency_code', 'order_created',
          'header_payment', 'detail_payment', 'order_ship_qty', 'source_rows',
          'missing_payment_rows', 'negative_ship_rows', 'latest_update', 'missing_update_rows')


@dataclass(frozen=True)
class OrderComponentPlan:
    owner_codes: tuple[str, ...] = ()
    order_codes: tuple[str, ...] = ()
    sku_codes: tuple[str, ...] = ()
    start: datetime | None = None
    end: datetime | None = None

    def execution_scope(self):
        from ..query_scope import QueryScope
        # Composite SQL owns the full owner tuple; this legacy context is never
        # used to rewrite its predicates through apply_scope.
        return QueryScope(owner=self.owner_codes[0] if len(self.owner_codes) == 1 else 'ALL',
                          sku_codes=self.sku_codes, unit='source_quantity')

    def scope_metadata(self):
        scope = self.execution_scope().as_dict()
        if self.owner_codes:
            scope['owner_codes'] = list(self.owner_codes)
            if len(self.owner_codes) > 1 or self.owner_codes == ('ALL',):
                scope['owner'] = 'SELECTED'
        scope['order_codes'] = list(self.order_codes)
        return scope

    def __post_init__(self):
        for codes in (self.owner_codes, self.order_codes, self.sku_codes):
            _codes(codes)
        if (self.start is None) != (self.end is None):
            raise ValueError('both order date bounds required')
        if self.start is not None:
            if any(not isinstance(t, datetime) or t.tzinfo is None or t.utcoffset() is None or t.microsecond for t in (self.start, self.end)) or self.end <= self.start:
                raise ValueError('explicit aware half-open order dates required')


def compile_order_components(plan):
    if not isinstance(plan, OrderComponentPlan):
        raise ValueError('order component plan required')

    def scope(column, codes):
        return column + ' IN (' + ','.join(exp.Literal.string(v).sql(dialect='doris') for v in codes) + ')' if codes else 'TRUE'

    owner = scope("NULLIF(TRIM(company_code),'')", plan.owner_codes)
    orders = scope("NULLIF(TRIM(sales_order_code),'')", plan.order_codes)
    skus = scope("NULLIF(TRIM(d.sku_code),'')", plan.sku_codes)
    window = 'TRUE'
    if plan.start is not None:
        start, end = (t.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S') for t in (plan.start, plan.end))
        window = f"(order_created IS NULL OR (order_created>='{start}' AND order_created<'{end}'))"
    nulls = ','.join('CAST(NULL AS ' + ('STRING' if name in FIELDS[:4] else 'DATETIME' if name in ('order_created', 'latest_update') else 'DECIMAL(38,3)') + ') AS ' + name for name in FIELDS)
    sql = f"""WITH header_raw AS (
 SELECT NULLIF(TRIM(company_code),'') AS cargo_owner,NULLIF(TRIM(sales_order_code),'') AS sales_order_code,
   NULLIF(TRIM(currency_code),'') AS currency_code,created AS order_created,updated AS latest_update,
   total_pay_amount AS header_payment,_fdl_marked_deleted AS deleted
 FROM ywzt_ods.scm_sales_order_header WHERE {owner} AND {orders}
), selected_keys AS (
 SELECT DISTINCT cargo_owner,sales_order_code FROM header_raw
 WHERE (deleted IS NULL OR deleted<>'true') AND {window}
), headers AS (
 SELECT h.cargo_owner,h.sales_order_code,MAX(h.currency_code) AS currency_code,
   MAX(h.order_created) AS order_created,MAX(h.latest_update) AS latest_update,
   MAX(h.header_payment) AS header_payment,COUNT(*) AS variants,
   SUM(CASE WHEN h.deleted IS NULL OR h.deleted<>'false' OR h.cargo_owner IS NULL
       OR h.sales_order_code IS NULL OR h.order_created IS NULL THEN 1 ELSE 0 END) AS errors
 FROM header_raw h WHERE (h.deleted IS NULL OR h.deleted<>'true') AND EXISTS (
   SELECT 1 FROM selected_keys k WHERE k.cargo_owner<=>h.cargo_owner AND k.sales_order_code<=>h.sales_order_code
 ) GROUP BY h.cargo_owner,h.sales_order_code
), details AS (
 SELECT NULLIF(TRIM(d.company_code),'') AS cargo_owner,NULLIF(TRIM(d.sales_order_code),'') AS sales_order_code,
   NULLIF(TRIM(d.id),'') AS detail_id,NULLIF(TRIM(d.sku_code),'') AS sku_code,
   d.ship_qty AS order_ship_qty,d.total_pay_amount AS detail_payment,d.updated AS latest_update,
   d._fdl_marked_deleted AS deleted
 FROM ywzt_ods.scm_sales_order_detail d WHERE d.status=0
   AND (d._fdl_marked_deleted IS NULL OR d._fdl_marked_deleted<>'true') AND {skus}
   AND EXISTS (SELECT 1 FROM selected_keys k WHERE k.cargo_owner<=>NULLIF(TRIM(d.company_code),'')
     AND k.sales_order_code<=>NULLIF(TRIM(d.sales_order_code),''))
), sku_groups AS (
 SELECT cargo_owner,sales_order_code,sku_code,SUM(CAST(order_ship_qty AS DECIMAL(38,0))) AS order_ship_qty,
   SUM(COALESCE(detail_payment,0)) AS detail_payment,COUNT(*) AS source_rows,
   SUM(CASE WHEN detail_payment IS NULL THEN 1 ELSE 0 END) AS missing_payment_rows,
   SUM(CASE WHEN order_ship_qty<0 THEN 1 ELSE 0 END) AS negative_ship_rows,MAX(latest_update) AS latest_update,
   SUM(CASE WHEN latest_update IS NULL THEN 1 ELSE 0 END) AS missing_update_rows
 FROM details GROUP BY cargo_owner,sales_order_code,sku_code
), report AS (
 SELECT 'order' AS row_kind,cargo_owner,sales_order_code,CAST(NULL AS STRING) AS sku_code,currency_code,order_created,
   COALESCE(header_payment,0) AS header_payment,CAST(NULL AS DECIMAL(38,3)) AS detail_payment,
   CAST(NULL AS DECIMAL(38,0)) AS order_ship_qty,1 AS source_rows,
   CASE WHEN header_payment IS NULL THEN 1 ELSE 0 END AS missing_payment_rows,0 AS negative_ship_rows,latest_update,
   CASE WHEN latest_update IS NULL THEN 1 ELSE 0 END AS missing_update_rows FROM headers
 UNION ALL SELECT 'sku',s.cargo_owner,s.sales_order_code,s.sku_code,h.currency_code,h.order_created,
   NULL,s.detail_payment,s.order_ship_qty,s.source_rows,s.missing_payment_rows,s.negative_ship_rows,s.latest_update,s.missing_update_rows
   FROM sku_groups s JOIN headers h ON s.cargo_owner=h.cargo_owner AND s.sales_order_code=h.sales_order_code
 UNION ALL SELECT 'header_control',NULL,NULL,NULL,currency_code,NULL,SUM(COALESCE(header_payment,0)),NULL,NULL,
   COUNT(*),SUM(CASE WHEN header_payment IS NULL THEN 1 ELSE 0 END),0,MAX(latest_update),
   SUM(CASE WHEN latest_update IS NULL THEN 1 ELSE 0 END) FROM headers GROUP BY currency_code
 UNION ALL SELECT 'detail_control',NULL,NULL,NULL,h.currency_code,NULL,NULL,SUM(COALESCE(d.detail_payment,0)),
   SUM(CAST(d.order_ship_qty AS DECIMAL(38,0))),COUNT(*),SUM(CASE WHEN d.detail_payment IS NULL THEN 1 ELSE 0 END),
   SUM(CASE WHEN d.order_ship_qty<0 THEN 1 ELSE 0 END),MAX(d.latest_update),
   SUM(CASE WHEN d.latest_update IS NULL THEN 1 ELSE 0 END)
   FROM details d JOIN headers h ON d.cargo_owner=h.cargo_owner AND d.sales_order_code=h.sales_order_code GROUP BY h.currency_code
 UNION ALL SELECT 'sentinel',{nulls}
), quality AS (
 SELECT (SELECT COUNT(*) FROM headers WHERE variants<>1 OR errors>0)
   + (SELECT COUNT(*) FROM details WHERE detail_id IS NULL OR sku_code IS NULL OR order_ship_qty IS NULL
       OR deleted IS NULL OR deleted<>'false' OR cargo_owner IS NULL OR sales_order_code IS NULL)
   + (SELECT COUNT(*) FROM (SELECT cargo_owner,detail_id FROM details GROUP BY cargo_owner,detail_id HAVING COUNT(*)>1) duplicated) AS quality_errors
)
SELECT r.*,q.quality_errors,(SELECT COUNT(*) FROM report) AS group_count FROM report r CROSS JOIN quality q
ORDER BY row_kind,currency_code,cargo_owner,sales_order_code,sku_code LIMIT 501"""
    SqlSafetyValidator().validate(sql, allowed_sources=SOURCES)
    return sql


@dataclass(frozen=True)
class OrderComponentResult:
    orders: list[dict]
    groups: list[dict]
    currency_totals: dict

    @property
    def order_keys(self):
        return tuple((row['cargo_owner'], row['sales_order_code']) for row in self.orders)


def _number(value):
    if value is None or isinstance(value, bool):
        raise ValueError('missing order component')
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError('nonfinite order component')
    return number


def _time(value):
    return _parse_source_datetime(value, timezone_name='Asia/Shanghai') if value is not None else None


def _identity(value, *, nullable=False):
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not value or value != value.strip(' '):
        raise ValueError('invalid order identity')
    return value


def reconcile_order_components(rows, plan, *, complete_limit=100000):
    try:
        with localcontext() as context:
            context.prec = 64
            context.traps[Inexact] = True
            return _reconcile(rows, plan, complete_limit)
    except (TypeError, ValueError, KeyError, ArithmeticError) as exc:
        raise SemanticRuntimeError('销售订单分量未通过完整性与对账检查，暂未展示', reason='order_component_quality_failed') from exc


def _reconcile(rows, plan, limit):
    if not isinstance(plan, OrderComponentPlan) or type(limit) is not int or not 1 <= limit <= 100000 or not rows or len(rows) > limit:
        raise ValueError('incomplete order components')
    orders, groups, expected, actual = {}, {}, {}, {}
    sentinel = 0
    for row in rows:
        if _count(row['quality_errors']) or _count(row['group_count']) != len(rows):
            raise ValueError('order source quality or truncation')
        kind = row['row_kind']
        if kind == 'sentinel':
            sentinel += 1
            if any(row[name] is not None for name in FIELDS): raise ValueError('invalid sentinel')
            continue
        if kind not in ('order', 'sku', 'header_control', 'detail_control'):
            raise ValueError('unknown order component')
        currency = _identity(row['currency_code'], nullable=True)
        count, missing, negative = (_count(row[field]) for field in ('source_rows', 'missing_payment_rows', 'negative_ship_rows'))
        missing_update = _count(row['missing_update_rows'])
        if not count or missing > count or negative > count or missing_update > count:
            raise ValueError('invalid order source counts')
        is_header = kind in ('order', 'header_control')
        null_fields = ('detail_payment', 'order_ship_qty') if is_header else ('header_payment',)
        if any(row[name] is not None for name in null_fields) or is_header and negative:
            raise ValueError('measure leaked across order and detail grains')
        payment = _number(row['header_payment' if is_header else 'detail_payment'])
        quantity = Decimal(0) if is_header else _number(row['order_ship_qty'])
        if not is_header and quantity != quantity.to_integral_value():
            raise ValueError('fractional integer-source quantity')
        stamp = _time(row['latest_update'])
        if (stamp is None) != (missing_update == count):
            raise ValueError('source timestamp completeness mismatch')
        signature = (payment, quantity, count, missing, negative, missing_update, stamp)
        family = 'header' if is_header else 'detail'
        if kind.endswith('_control'):
            if (family, currency) in expected or any(row[name] is not None for name in FIELDS[:3] + ('order_created',)):
                raise ValueError('duplicate or malformed currency control')
            expected[family, currency] = signature
            continue
        owner, order = (_identity(row[name]) for name in ('cargo_owner', 'sales_order_code'))
        if plan.owner_codes and owner not in plan.owner_codes or plan.order_codes and order not in plan.order_codes:
            raise ValueError('order scope mismatch')
        created = _time(row['order_created'])
        if created is None or plan.start is not None and not plan.start <= created < plan.end:
            raise ValueError('order creation scope mismatch')
        value = dict(cargo_owner=owner, sales_order_code=order, currency_code=currency, order_created=created.isoformat(),
                     source_rows=count, missing_payment_rows=missing, missing_update_rows=missing_update,
                     latest_update=stamp.isoformat() if stamp else None)
        if kind == 'order':
            if count != 1 or negative or row['sku_code'] is not None or (owner, order) in orders:
                raise ValueError('invalid unique order')
            value['header_payment'] = None if missing else payment
            value['known_header_payment'] = payment
            orders[owner, order] = value
        else:
            sku = _identity(row['sku_code'])
            if plan.sku_codes and sku not in plan.sku_codes or (owner, order, sku) in groups:
                raise ValueError('invalid order SKU scope')
            if quantity < 0 and not negative:
                raise ValueError('missing negative source diagnostic')
            value.update(sku_code=sku, order_ship_qty=quantity, detail_payment=None if missing else payment,
                         known_detail_payment=payment, negative_ship_rows=negative)
            groups[owner, order, sku] = value
        accumulated = actual.setdefault((family, currency), [Decimal(0), Decimal(0), 0, 0, 0, 0, None])
        for index in range(6): accumulated[index] += signature[index]
        accumulated[6] = max(accumulated[6], stamp) if accumulated[6] and stamp else accumulated[6] or stamp
    if sentinel != 1 or {key: tuple(value) for key, value in actual.items()} != expected:
        raise ValueError('order currency controls do not reconcile')
    for key, group in groups.items():
        order = orders.get(key[:2])
        if order is None or any(order[field] != group[field] for field in ('currency_code', 'order_created')):
            raise ValueError('SKU not in the exact order cohort')
    with_details = {key[:2] for key in groups}
    for key, order in orders.items(): order['missing_details'] = key not in with_details
    totals = {}
    for (family, currency), values in actual.items():
        target = totals.setdefault(currency, dict(header_payment=Decimal(0), detail_payment=Decimal(0),
            known_header_payment=Decimal(0), known_detail_payment=Decimal(0),
            order_ship_qty=Decimal(0), order_count=0, detail_count=0,
            missing_header_payment_rows=0, missing_detail_payment_rows=0,
            missing_header_update_rows=0, missing_detail_update_rows=0))
        target[family + '_payment'] = None if values[3] else values[0]
        target['known_' + family + '_payment'] = values[0]
        target['order_count' if family == 'header' else 'detail_count'] = values[2]
        target['missing_' + family + '_payment_rows'] = values[3]
        target['missing_' + family + '_update_rows'] = values[5]
        if family == 'detail': target['order_ship_qty'] = values[1]
    return OrderComponentResult([orders[key] for key in sorted(orders)], [groups[key] for key in sorted(groups)], totals)


def retrieve_order_components(plan, query, *, query_many=None, **budgets):
    from .sales_pages import AggregatePageError, retrieve_sales_groups
    try:
        def checked(rows):
            if any(_count(row['quality_errors']) for row in rows):
                raise SemanticRuntimeError('销售订单来源存在异常，暂未展示', reason='order_component_quality_failed')
            return rows
        def read(sql, **kwargs): return checked(query(sql, **kwargs))
        def many(statements, **kwargs): return [checked(rows) for rows in query_many(statements, **kwargs)]
        rows = retrieve_sales_groups(compile_order_components(plan), read,
            query_many=many if query_many is not None else None, **budgets)
        return reconcile_order_components(rows, plan, complete_limit=budgets.get('max_rows', 100000))
    except AggregatePageError as exc:
        raise AggregatePageError(exc.page_failure, exc.pass_number, reason='order_component_page_invalid') from exc
    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
        raise SemanticRuntimeError('销售订单分量未通过完整性检查，暂未展示', reason='order_component_quality_failed') from exc

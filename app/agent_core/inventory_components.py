"""Explicit quality-stock components, independent source totals and diagnostics."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, Inexact, InvalidOperation, localcontext
from zoneinfo import ZoneInfo

import sqlglot
from sqlglot import exp

from .dms_points_orders import _codes, _count
from .semantic_types import SemanticRuntimeError, _parse_source_datetime


KEYS = ('cargo_owner_id', 'wms_code', 'sku_code')
STOCK_MEASURES = ('in_stock_ea', 'stock_lock_ea', 'freeze_ea', 'move_in_ea')
OCCUPATION_MEASURES = ('sales_occupation_ea', 'transfer_occupation_ea')
STOCK_COUNTS = ('stock_rows', 'negative_stock_rows', 'negative_stock_move_rows', 'negative_lock_rows',
                'negative_freeze_rows', 'negative_move_rows', 'missing_stock_time_rows')
OCCUPATION_COUNTS = ('occupation_rows', 'negative_occupation_rows', 'missing_reference_rows', 'future_occupation_rows')
MEASURES = STOCK_MEASURES + OCCUPATION_MEASURES
COUNTS = STOCK_COUNTS + OCCUPATION_COUNTS
TIMES = ('stock_latest', 'occupation_latest')
DERIVED = ('deduction_ea', 'allocation_ea', 'business_occupation_ea', 'raw_available_ea', 'available_ea')
FIELDS = KEYS + MEASURES + COUNTS + TIMES + DERIVED
QUALITY = ('invalid_key_count', 'invalid_stock_lock_count', 'invalid_occupation_status_count', 'missing_occupation_date_count')


class InventoryComponentQualityError(SemanticRuntimeError):
    def __init__(self, failures):
        super().__init__('库存来源字段或占用状态存在异常，暂未展示', reason='inventory_component_quality_failed')
        self.quality_failures = tuple(name for name in QUALITY if name in failures)


@dataclass(frozen=True)
class InventoryComponentPlan:
    observed_at: datetime
    occupation_since: datetime
    owner_codes: tuple[str, ...] = ()
    warehouse_codes: tuple[str, ...] = ()
    sku_codes: tuple[str, ...] = ()

    def __post_init__(self):
        for instant in (self.observed_at, self.occupation_since):
            if not isinstance(instant, datetime) or instant.tzinfo is None or instant.utcoffset() is None or instant.microsecond:
                raise ValueError('explicit timezone-aware second-precision instants required')
        if self.occupation_since >= self.observed_at:
            raise ValueError('occupation cutoff must precede observation')
        for codes in (self.owner_codes, self.warehouse_codes, self.sku_codes):
            _codes(codes)


def compile_inventory_components(plan):
    if not isinstance(plan, InventoryComponentPlan):
        raise ValueError('inventory component plan required')
    zone = ZoneInfo('Asia/Shanghai')
    since, observed = (instant.astimezone(zone).strftime('%Y-%m-%d %H:%M:%S') for instant in (plan.occupation_since, plan.observed_at))
    def scope(alias):
        terms = []
        for column, values in zip(KEYS, (plan.owner_codes, plan.warehouse_codes, plan.sku_codes)):
            if values:
                terms.append(f'TRIM(CAST({alias}.{column} AS STRING)) IN (' + ','.join(exp.Literal.string(v).sql(dialect='doris') for v in values) + ')')
        return ' AND '.join(terms) or 'TRUE'
    def keys(alias):
        return ','.join(f"NULLIF(TRIM(CAST({alias}.{key} AS STRING)),'') AS {key}" for key in KEYS)
    valid_keys = ' AND '.join(key + ' IS NOT NULL' for key in KEYS)
    invalid_keys = ' OR '.join(key + ' IS NULL' for key in KEYS)
    lock = "TRIM(COALESCE(w.stock_lock,''))"
    lock_valid = f"({lock}='' OR {lock} REGEXP '^-?[0-9]{{1,14}}([.][0-9]{{1,4}})?$')"
    status_valid = "TRIM(o.status) REGEXP '^[0-9]{1,9}$'"
    stock_values = [('in_stock_ea', 'w.in_stock_quantity'), ('freeze_ea', 'w.freeze_quantity'), ('move_in_ea', 'w.move_in_quantity')]
    stock_projection = ','.join(f'CAST(COALESCE({column},0) AS DECIMAL(28,4)) AS {name}' for name,column in stock_values)
    stock_aggregate = ','.join('SUM('+name+') AS '+name for name in STOCK_MEASURES)
    match = ' AND '.join('k.'+key+'=s.'+key for key in KEYS)
    occ_match = ' AND '.join('k.'+key+'=o.'+key for key in KEYS)
    joined = ','.join('COALESCE('+('s' if key in STOCK_MEASURES+STOCK_COUNTS else 'o')+'.'+key+',0) AS '+key for key in MEASURES+COUNTS)
    stock_totals = ','.join(f'COALESCE(SUM({name}),0) AS total_{name}' for name in STOCK_MEASURES)
    # Controls aggregate each fact independently, not the final joined result.
    total_projection = ','.join(('s' if key in STOCK_MEASURES+STOCK_COUNTS else 'o')+'.total_'+key for key in MEASURES+COUNTS)
    null_fields = ','.join('CAST(NULL AS '+('STRING' if key in KEYS else 'DATETIME' if key in TIMES else 'BIGINT' if key in COUNTS else 'DECIMAL(28,4)')+') AS '+key for key in FIELDS)
    return f"""WITH stock_raw AS (
 SELECT {keys('w')},{stock_projection},w.stock_change_time AS stock_latest,
   CASE WHEN {lock_valid} AND {lock}<>'' THEN CAST({lock} AS DECIMAL(28,4)) ELSE 0 END AS stock_lock_ea,
   CASE WHEN {lock_valid} THEN 0 ELSE 1 END AS invalid_lock
 FROM ywzt_ods.scm_warehous_manage w
 WHERE (w.inventory_status='ZP' OR w.inventory_status='' OR w.inventory_status IS NULL) AND ({scope('w')})
), occupation_raw AS (
 SELECT {keys('o')},CAST(COALESCE(o.freeze_quantity,0) AS DECIMAL(28,4)) AS occupation_ea,
   o.pre_order_code,o.created_date AS occupation_latest,
   CASE WHEN {status_valid} THEN CAST(TRIM(o.status) AS BIGINT) ELSE NULL END AS status_value,
   CASE WHEN o.status IS NOT NULL AND NOT ({status_valid}) THEN 1 ELSE 0 END AS invalid_status
 FROM ywzt_ods.scm_warehous_occupy o WHERE {scope('o')}
), occupation AS (
 SELECT *,CASE WHEN COALESCE(pre_order_code,'') LIKE 'HJXH-TH%' THEN occupation_ea ELSE 0 END AS transfer_occupation_ea,
   CASE WHEN COALESCE(pre_order_code,'') LIKE 'HJXH-TH%' THEN 0 ELSE occupation_ea END AS sales_occupation_ea
 FROM occupation_raw WHERE status_value<>1 AND occupation_latest>'{since}'
), stock_groups AS (
 SELECT {','.join(KEYS)},{stock_aggregate},COUNT(*) AS stock_rows,
   SUM(CASE WHEN in_stock_ea<0 THEN 1 ELSE 0 END) AS negative_stock_rows,
   SUM(CASE WHEN in_stock_ea+move_in_ea<0 THEN 1 ELSE 0 END) AS negative_stock_move_rows,
   SUM(CASE WHEN stock_lock_ea<0 THEN 1 ELSE 0 END) AS negative_lock_rows,
   SUM(CASE WHEN freeze_ea<0 THEN 1 ELSE 0 END) AS negative_freeze_rows,
   SUM(CASE WHEN move_in_ea<0 THEN 1 ELSE 0 END) AS negative_move_rows,
   SUM(CASE WHEN stock_latest IS NULL THEN 1 ELSE 0 END) AS missing_stock_time_rows,
   MAX(stock_latest) AS stock_latest
 FROM stock_raw WHERE {valid_keys} GROUP BY {','.join(KEYS)}
), occupation_groups AS (
 SELECT {','.join(KEYS)},SUM(sales_occupation_ea) AS sales_occupation_ea,SUM(transfer_occupation_ea) AS transfer_occupation_ea,
   COUNT(*) AS occupation_rows,SUM(CASE WHEN occupation_ea<0 THEN 1 ELSE 0 END) AS negative_occupation_rows,
   SUM(CASE WHEN pre_order_code IS NULL OR pre_order_code='' THEN 1 ELSE 0 END) AS missing_reference_rows,
   SUM(CASE WHEN occupation_latest>'{observed}' THEN 1 ELSE 0 END) AS future_occupation_rows,
   MAX(occupation_latest) AS occupation_latest
 FROM occupation WHERE {valid_keys} GROUP BY {','.join(KEYS)}
), all_keys AS (
 SELECT {','.join(KEYS)} FROM stock_groups UNION SELECT {','.join(KEYS)} FROM occupation_groups
), joined AS (
 SELECT {','.join('k.'+key for key in KEYS)},{joined},s.stock_latest,o.occupation_latest
 FROM all_keys k LEFT JOIN stock_groups s ON {match} LEFT JOIN occupation_groups o ON {occ_match}
), deductions AS (
 SELECT *,CASE WHEN stock_lock_ea<>0 THEN stock_lock_ea ELSE freeze_ea END AS deduction_ea,
   sales_occupation_ea AS allocation_ea,sales_occupation_ea+transfer_occupation_ea AS business_occupation_ea
 FROM joined
), calculated AS (
 SELECT *,CASE WHEN stock_rows>0 THEN in_stock_ea-business_occupation_ea-deduction_ea ELSE NULL END AS raw_available_ea
 FROM deductions
), groups AS (
 SELECT *,CASE WHEN stock_rows>0 THEN GREATEST(raw_available_ea,0) ELSE NULL END AS available_ea FROM calculated
), stock_controls AS (
 SELECT {stock_totals},COUNT(*) AS total_stock_rows,
   COALESCE(SUM(CASE WHEN in_stock_ea<0 THEN 1 ELSE 0 END),0) AS total_negative_stock_rows,
   COALESCE(SUM(CASE WHEN in_stock_ea+move_in_ea<0 THEN 1 ELSE 0 END),0) AS total_negative_stock_move_rows,
   COALESCE(SUM(CASE WHEN stock_lock_ea<0 THEN 1 ELSE 0 END),0) AS total_negative_lock_rows,
   COALESCE(SUM(CASE WHEN freeze_ea<0 THEN 1 ELSE 0 END),0) AS total_negative_freeze_rows,
   COALESCE(SUM(CASE WHEN move_in_ea<0 THEN 1 ELSE 0 END),0) AS total_negative_move_rows,
   COALESCE(SUM(CASE WHEN stock_latest IS NULL THEN 1 ELSE 0 END),0) AS total_missing_stock_time_rows,
   MAX(stock_latest) AS total_stock_latest FROM stock_raw
), occupation_controls AS (
 SELECT COALESCE(SUM(sales_occupation_ea),0) AS total_sales_occupation_ea,
   COALESCE(SUM(transfer_occupation_ea),0) AS total_transfer_occupation_ea,COUNT(*) AS total_occupation_rows,
   COALESCE(SUM(CASE WHEN occupation_ea<0 THEN 1 ELSE 0 END),0) AS total_negative_occupation_rows,
   COALESCE(SUM(CASE WHEN pre_order_code IS NULL OR pre_order_code='' THEN 1 ELSE 0 END),0) AS total_missing_reference_rows,
   COALESCE(SUM(CASE WHEN occupation_latest>'{observed}' THEN 1 ELSE 0 END),0) AS total_future_occupation_rows,
   MAX(occupation_latest) AS total_occupation_latest FROM occupation
), quality AS (
 SELECT (SELECT COUNT(*) FROM stock_raw WHERE {invalid_keys})+(SELECT COUNT(*) FROM occupation WHERE {invalid_keys}) AS invalid_key_count,
   (SELECT COUNT(*) FROM stock_raw WHERE invalid_lock>0) AS invalid_stock_lock_count,
   (SELECT COUNT(*) FROM occupation_raw WHERE invalid_status>0 AND (occupation_latest>'{since}' OR occupation_latest IS NULL)) AS invalid_occupation_status_count,
   (SELECT COUNT(*) FROM occupation_raw WHERE status_value<>1 AND occupation_latest IS NULL) AS missing_occupation_date_count
), report AS (
 SELECT 'group' AS row_kind,{','.join(FIELDS)} FROM groups
 UNION ALL SELECT 'control',{null_fields}
)
SELECT r.*,{total_projection},s.total_stock_latest,o.total_occupation_latest,q.*,
  1+(SELECT COUNT(*) FROM groups) AS group_count
FROM report r CROSS JOIN stock_controls s CROSS JOIN occupation_controls o CROSS JOIN quality q
ORDER BY row_kind,{','.join(KEYS)} LIMIT 501"""


def compile_inventory_quality(plan):
    """Inspect contract quality flags without returning candidate quantities."""
    report = sqlglot.parse_one(compile_inventory_components(plan), read='doris')
    diagnostic = exp.select(*QUALITY).from_('quality').limit(1)
    diagnostic.set('with_', report.args['with_'].copy())
    return diagnostic.sql(dialect='doris')


def _decimal(value):
    if value is None or isinstance(value,bool):
        raise ValueError('missing inventory component')
    try:
        number=Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('invalid inventory component') from exc
    if not number.is_finite():
        raise ValueError('nonfinite inventory component')
    return number


def _time(value):
    return _parse_source_datetime(value,timezone_name='Asia/Shanghai') if value is not None else None


@dataclass(frozen=True)
class InventoryComponentResult:
    groups: list[dict]
    totals: dict

    @property
    def diagnostics(self):
        rows = []
        source_counts = ('negative_stock_rows', 'negative_stock_move_rows', 'negative_lock_rows',
                         'negative_freeze_rows', 'negative_move_rows', 'negative_occupation_rows',
                         'missing_reference_rows')
        for group in self.groups:
            issues = [(name, 'source_data', group[name]) for name in source_counts if group[name]]
            issues.extend((name, 'source_metadata', group[name])
                          for name in ('missing_stock_time_rows', 'future_occupation_rows') if group[name])
            if not group['stock_rows']:
                issues.append(('missing_stock', 'source_data', None))
            issues.extend((name, 'calculation_effect', None)
                          for name in ('clamped', 'available_above_stock') if group[name])
            for issue, category, count in issues:
                rows.append({**{name: group[name] for name in FIELDS}, 'issue': issue,
                             'category': category, 'affected_source_rows': count})
        return rows


def reconcile_inventory_components(rows,plan,*,complete_limit=100000):
    # Doris SUM can widen to DECIMAL(38,4); caller Decimal precision is unrelated.
    with localcontext() as context:
        context.prec = 64
        context.traps[Inexact] = True
        return _reconcile_inventory_components(rows, plan, complete_limit=complete_limit)


def _reconcile_inventory_components(rows,plan,*,complete_limit):
    if not rows or not 1 <= complete_limit <= 100000 or len(rows)>complete_limit:
        raise ValueError('missing or truncated inventory report')
    def controls(row):
        return {**{'total_'+name:_decimal(row['total_'+name]) for name in MEASURES},
                **{'total_'+name:_count(row['total_'+name]) for name in COUNTS},
                **{name:_count(row[name]) for name in QUALITY+('group_count',)},
                **{'total_'+name:_time(row['total_'+name]) for name in TIMES}}
    expected=controls(rows[0])
    if any(expected[name] for name in QUALITY):
        raise ValueError('inventory source quality failed')
    if expected['group_count']!=len(rows):
        raise ValueError('incomplete inventory groups')
    totals={name:Decimal(0) for name in MEASURES+COUNTS}
    latest={name:None for name in TIMES}
    groups,seen,control_count=[],set(),0
    for row in rows:
        if controls(row)!=expected:
            raise ValueError('inventory controls changed')
        if row['row_kind']=='control':
            control_count+=1
            if any(row[name] is not None for name in FIELDS):
                raise ValueError('invalid inventory control row')
            continue
        if row['row_kind']!='group':
            raise ValueError('unknown inventory row')
        key=tuple(row[name] for name in KEYS)
        if key in seen or any(not isinstance(value,str) or not value or value!=value.strip(' ') for value in key):
            raise ValueError('invalid or duplicate inventory key')
        if any(values and value not in values for value,values in zip(key,(plan.owner_codes,plan.warehouse_codes,plan.sku_codes))):
            raise ValueError('inventory scope mismatch')
        seen.add(key)
        item={name:row[name] for name in KEYS}
        item.update({name:_decimal(row[name]) for name in MEASURES})
        item.update({name:_count(row[name]) for name in COUNTS})
        if item['stock_rows']+item['occupation_rows']==0:
            raise ValueError('inventory key without source records')
        for count in STOCK_COUNTS[1:]:
            if item[count]>item['stock_rows']:raise ValueError('invalid stock anomaly count')
        for count in OCCUPATION_COUNTS[1:]:
            if item[count]>item['occupation_rows']:raise ValueError('invalid occupation anomaly count')
        for measure_set,count in ((STOCK_MEASURES,'stock_rows'),(OCCUPATION_MEASURES,'occupation_rows')):
            if not item[count] and any(item[name] for name in measure_set):raise ValueError('components without source rows')
        for name in TIMES:
            stamp=_time(row[name])
            if name=='stock_latest' and (stamp is None)!=(item['missing_stock_time_rows']==item['stock_rows']):
                raise ValueError('invalid stock freshness')
            if name=='occupation_latest' and ((stamp is None)!=(item['occupation_rows']==0) or (stamp is not None and stamp<=plan.occupation_since)):
                raise ValueError('invalid occupation freshness')
            if stamp is not None:latest[name]=max(latest[name],stamp) if latest[name] else stamp
            item[name]=stamp.isoformat() if stamp else None
        deduction=item['stock_lock_ea'] if item['stock_lock_ea'] else item['freeze_ea']
        occupied=item['sales_occupation_ea']+item['transfer_occupation_ea']
        raw=item['in_stock_ea']-occupied-deduction if item['stock_rows'] else None
        calculated=dict(deduction_ea=deduction,allocation_ea=item['sales_occupation_ea'],business_occupation_ea=occupied,
                        raw_available_ea=raw,available_ea=max(raw,Decimal(0)) if raw is not None else None)
        for name,value in calculated.items():
            if (row[name] is not None if value is None else _decimal(row[name])!=value):
                raise ValueError('inventory formula reconciliation failed')
        item.update(calculated)
        item.update(clamped=raw is not None and raw<0,available_above_stock=raw is not None and max(raw,0)>item['in_stock_ea'])
        groups.append(item)
        for name in totals:totals[name]+=item[name]
    if control_count!=1 or any(totals[name]!=expected['total_'+name] for name in totals) or any(latest[name]!=expected['total_'+name] for name in TIMES):
        raise ValueError('inventory prejoin controls do not reconcile')
    groups.sort(key=lambda row:tuple(row[name] for name in KEYS))
    return InventoryComponentResult(groups,totals)


def retrieve_inventory_components(plan,query,*,query_many=None,consistency_retries=0,**budgets):
    from .sales_pages import AggregatePageError, retrieve_sales_groups
    try:
        def checked(rows):
            failures = {name for row in rows for name in QUALITY if _count(row[name])}
            if failures:
                raise InventoryComponentQualityError(failures)
            return rows
        def read(sql,*,purpose):
            return checked(query(sql,purpose='inventory components: bounded complete page'))
        def many(statements,*,purpose,timeout_seconds):
            return [checked(rows) for rows in query_many(statements,purpose='inventory components: bounded complete page',timeout_seconds=timeout_seconds)]
        rows=retrieve_sales_groups(compile_inventory_components(plan),read,
                                    query_many=many if query_many is not None else None,
                                    consistency_retries=consistency_retries,**budgets)
        return reconcile_inventory_components(rows,plan,complete_limit=budgets.get('max_rows',100000))
    except SemanticRuntimeError as exc:
        if exc.reason!='sales_page_invalid':raise
        if isinstance(exc, AggregatePageError):
            raise AggregatePageError(exc.page_failure, exc.pass_number, reason='inventory_component_page_invalid') from exc
        raise SemanticRuntimeError('库存分批结果存在缺页或变化，暂未展示',reason='inventory_component_page_invalid') from exc
    except (ValueError,TypeError,KeyError,ArithmeticError) as exc:
        raise SemanticRuntimeError('库存分量与独立总计未通过对账，暂未展示',reason='inventory_component_reconciliation_failed') from exc

"""Creation-month DMS order counts with independently reconciled header details."""

from collections import Counter
from dataclasses import dataclass
from datetime import date

from sqlglot import exp

from .semantic_types import SemanticRuntimeError, _parse_decimal, _parse_source_datetime


def _codes(values):
    if not isinstance(values, tuple) or len(values) > 200:
        raise ValueError('explicit code tuple required')
    if any(not isinstance(value, str) or not value or value != value.strip() or len(value) > 200
           or any(ord(char) < 32 for char in value) for value in values):
        raise ValueError('invalid code')
    if len(set(values)) != len(values):
        raise ValueError('duplicate scope code')


@dataclass(frozen=True, slots=True)
class DmsPointsOrderPlan:
    start: date | None = None
    end: date | None = None
    customer_codes: tuple[str, ...] = ()
    order_codes: tuple[str, ...] = ()

    def __post_init__(self):
        if (self.start is None) != (self.end is None):
            raise ValueError('both date bounds required')
        if self.start is not None and (type(self.start) is not date or type(self.end) is not date or self.end <= self.start):
            raise ValueError('invalid creation interval')
        _codes(self.customer_codes)
        _codes(self.order_codes)


def compile_dms_points_orders(plan: DmsPointsOrderPlan):
    def predicate(column, values):
        return 'TRUE' if not values else column + ' IN (' + ','.join(exp.Literal.string(value).sql(dialect='doris') for value in values) + ')'
    scope = predicate('customer_code', plan.customer_codes) + ' AND ' + predicate('sales_order_code', plan.order_codes)
    interval = 'TRUE' if plan.start is None else f"created_time >= '{plan.start.isoformat()} 00:00:00' AND created_time < '{plan.end.isoformat()} 00:00:00'"
    # Deletion, date and customer predicates must follow ranking; a historical
    # matching version must not resurrect an ineligible current order.
    return f"""WITH source_rows AS (
 SELECT NULLIF(TRIM(o.sales_order_code),'') AS sales_order_code,
   NULLIF(TRIM(o.customer_code),'') AS customer_code,NULLIF(TRIM(o.customer_name),'') AS customer_name,
   o.created_time,o.updated_time,o.version,o.id,o.is_points_order,o.deleted_flag,
   LOWER(TRIM(COALESCE(o._fdl_marked_deleted,''))) AS cdc_deleted
 FROM dms_ods.t_sales_order o
), ranked AS (
 SELECT *,DENSE_RANK() OVER (PARTITION BY sales_order_code
   ORDER BY updated_time DESC NULLS LAST,version DESC NULLS LAST,id DESC) AS version_rank
 FROM source_rows WHERE sales_order_code IS NOT NULL
), top_versions AS (
 SELECT DISTINCT sales_order_code,customer_code,customer_name,created_time,updated_time,
   is_points_order,deleted_flag,cdc_deleted
 FROM ranked WHERE version_rank=1
), version_controls AS (
 SELECT sales_order_code,COUNT(*) AS top_variants FROM top_versions GROUP BY sales_order_code
), candidates AS (
 SELECT * FROM top_versions WHERE is_points_order=1 AND COALESCE(deleted_flag,0)=0
   AND cdc_deleted NOT IN ('true','1') AND ({scope}) AND (created_time IS NULL OR ({interval}))
), eligible AS (
 SELECT * FROM candidates WHERE created_time IS NOT NULL AND cdc_deleted IN ('','false','0')
), orders AS (
 SELECT sales_order_code,MIN(customer_code) AS customer_code,MIN(customer_name) AS customer_name,
   MIN(created_time) AS created_time,MIN(updated_time) AS updated_time
 FROM eligible GROUP BY sales_order_code
), monthly AS (
 SELECT DATE_FORMAT(created_time,'%Y-%m') AS month,COUNT(*) AS order_count
 FROM orders GROUP BY DATE_FORMAT(created_time,'%Y-%m')
), quality AS (
 SELECT
   (SELECT COUNT(*) FROM source_rows WHERE sales_order_code IS NULL AND is_points_order=1
     AND COALESCE(deleted_flag,0)=0 AND cdc_deleted NOT IN ('true','1')
     AND ({scope}) AND (created_time IS NULL OR ({interval}))) AS blank_order_count,
   (SELECT COUNT(DISTINCT sales_order_code) FROM candidates WHERE created_time IS NULL) AS missing_created_count,
   (SELECT COUNT(DISTINCT c.sales_order_code) FROM candidates c JOIN version_controls v
     ON c.sales_order_code=v.sales_order_code WHERE v.top_variants>1) AS version_conflict_count,
   (SELECT COUNT(DISTINCT sales_order_code) FROM candidates WHERE cdc_deleted NOT IN ('','false','0')) AS invalid_delete_count
), controls AS (
 SELECT COUNT(*) AS total_order_count,(SELECT COUNT(*) FROM monthly) AS month_count,
   1+COUNT(*)+(SELECT COUNT(*) FROM monthly) AS group_count,
   COALESCE(SUM(CASE WHEN customer_code IS NULL THEN 1 ELSE 0 END),0) AS missing_customer_code_count,
   COALESCE(SUM(CASE WHEN customer_name IS NULL THEN 1 ELSE 0 END),0) AS missing_customer_name_count,
   COALESCE(SUM(CASE WHEN updated_time IS NULL THEN 1 ELSE 0 END),0) AS missing_updated_count,
   MAX(updated_time) AS source_latest_updated FROM orders
), report AS (
 SELECT 'control' AS row_kind,CAST(NULL AS STRING) AS month,CAST(NULL AS STRING) AS sales_order_code,
   CAST(NULL AS STRING) AS customer_code,CAST(NULL AS STRING) AS customer_name,
   CAST(NULL AS DATETIME) AS created_time,CAST(NULL AS DATETIME) AS updated_time,total_order_count AS order_count FROM controls
 UNION ALL
 SELECT 'month',month,NULL,NULL,NULL,NULL,NULL,order_count FROM monthly
 UNION ALL
 SELECT 'order',DATE_FORMAT(created_time,'%Y-%m'),sales_order_code,customer_code,customer_name,
   created_time,updated_time,1 FROM orders
)
SELECT r.*,c.total_order_count,c.month_count,c.group_count,c.missing_customer_code_count,
 c.missing_customer_name_count,c.missing_updated_count,c.source_latest_updated,
 q.blank_order_count,q.missing_created_count,q.version_conflict_count,q.invalid_delete_count
FROM report r CROSS JOIN controls c CROSS JOIN quality q
ORDER BY r.row_kind,r.month,r.sales_order_code LIMIT 501"""


def _count(value):
    number = _parse_decimal(value)
    if number < 0 or number != number.to_integral_value():
        raise ValueError('invalid order count')
    return int(number)


def _nullable_text(value):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError('invalid order text')
    return value


@dataclass(frozen=True)
class DmsPointsOrderResult:
    orders: list[dict]
    months: list[dict]
    order_count: int
    missing_customer_code_count: int
    missing_customer_name_count: int
    missing_updated_count: int
    source_latest_updated: str | None


def reconcile_dms_points_orders(rows, plan, *, complete_limit=100000):
    if type(complete_limit) is not int or not 1 <= complete_limit <= 100000 or not rows or len(rows) > complete_limit:
        raise ValueError('missing or truncated order controls')
    integer_controls = ('total_order_count', 'month_count', 'group_count', 'missing_customer_code_count',
                        'missing_customer_name_count', 'missing_updated_count', 'blank_order_count',
                        'missing_created_count', 'version_conflict_count', 'invalid_delete_count')
    control = {key: _count(rows[0][key]) for key in integer_controls}
    if any(control[key] for key in ('blank_order_count','missing_created_count','version_conflict_count','invalid_delete_count')):
        raise ValueError('DMS order source quality failed')
    if control['group_count'] != len(rows) or len(rows) != 1 + control['total_order_count'] + control['month_count']:
        raise ValueError('incomplete DMS report')
    latest = rows[0]['source_latest_updated']
    if latest is not None:
        latest = _parse_source_datetime(latest,timezone_name='Asia/Shanghai')
    orders, monthly, seen, summary_count = [], {}, set(), 0
    missing_code, missing_name, missing_updated = 0, 0, 0
    observed_latest = None
    for row in rows:
        if any(_count(row[key]) != control[key] for key in integer_controls):
            raise ValueError('inconsistent DMS controls')
        row_latest = row['source_latest_updated']
        row_latest = _parse_source_datetime(row_latest,timezone_name='Asia/Shanghai') if row_latest is not None else None
        if row_latest != latest:
            raise ValueError('inconsistent source modification time')
        kind, count = row['row_kind'], _count(row['order_count'])
        if kind == 'control':
            summary_count += 1
            if count != control['total_order_count'] or any(row[key] is not None for key in ('month','sales_order_code','customer_code','customer_name','created_time','updated_time')):
                raise ValueError('invalid DMS summary')
            continue
        month = row['month']
        if not isinstance(month,str) or len(month)!=7 or date.fromisoformat(month+'-01').strftime('%Y-%m')!=month:
            raise ValueError('invalid creation month')
        if kind == 'month':
            if month in monthly or count < 1 or any(row[key] is not None for key in ('sales_order_code','customer_code','customer_name','created_time','updated_time')):
                raise ValueError('invalid DMS monthly row')
            monthly[month] = count
            continue
        if kind != 'order' or count != 1:
            raise ValueError('invalid DMS report kind')
        code = _nullable_text(row['sales_order_code'])
        customer = _nullable_text(row['customer_code'])
        name = _nullable_text(row['customer_name'])
        if not code or code in seen or (plan.customer_codes and customer not in plan.customer_codes) or (plan.order_codes and code not in plan.order_codes):
            raise ValueError('duplicate or out-of-scope order')
        seen.add(code)
        created = _parse_source_datetime(row['created_time'],timezone_name='Asia/Shanghai')
        if created.strftime('%Y-%m') != month or (plan.start is not None and not plan.start <= created.date() < plan.end):
            raise ValueError('order outside creation interval')
        updated = row['updated_time']
        if updated is not None:
            updated = _parse_source_datetime(updated,timezone_name='Asia/Shanghai')
            observed_latest = max(observed_latest,updated) if observed_latest is not None else updated
        missing_code += customer is None
        missing_name += name is None
        missing_updated += updated is None
        orders.append({'sales_order_code':code,'customer_code':customer,'customer_name':name,
                       'created_time':created.strftime('%Y-%m-%d %H:%M:%S'),'month':month})
    actual_months = Counter(order['month'] for order in orders)
    if summary_count!=1 or len(orders)!=control['total_order_count'] or dict(actual_months)!=monthly or len(monthly)!=control['month_count']:
        raise ValueError('monthly and order totals do not reconcile')
    if (missing_code,missing_name,missing_updated,observed_latest)!=(control['missing_customer_code_count'],control['missing_customer_name_count'],control['missing_updated_count'],latest):
        raise ValueError('customer or freshness controls do not reconcile')
    return DmsPointsOrderResult(orders,[{'month':key,'order_count':value} for key,value in sorted(monthly.items())],
        len(orders),missing_code,missing_name,missing_updated,latest.strftime('%Y-%m-%d %H:%M:%S') if latest else None)


def retrieve_dms_points_orders(plan, query, *, query_many=None, page_size=500, max_rows=100000, max_bytes=64*1024*1024, timeout_seconds=180):
    from .sales_pages import retrieve_sales_groups
    purpose_label='DMS creation-month points orders: bounded report page'
    def checked(rows):
        if any(_count(row[key]) for row in rows for key in ('blank_order_count','missing_created_count','version_conflict_count','invalid_delete_count')):
            raise SemanticRuntimeError('DMS订单标识、创建时间或当前版本存在异常，暂未展示不完整结果',reason='dms_points_order_quality_failed')
        return rows
    def read(sql, *, purpose):
        return checked(query(sql,purpose=purpose_label))
    def read_many(statements, *, purpose, timeout_seconds):
        return [checked(rows) for rows in query_many(statements,purpose=purpose_label,timeout_seconds=timeout_seconds)]
    try:
        rows = retrieve_sales_groups(compile_dms_points_orders(plan),read,query_many=read_many if query_many is not None else None,page_size=page_size,max_rows=max_rows,max_bytes=max_bytes,timeout_seconds=timeout_seconds)
        return reconcile_dms_points_orders(rows,plan,complete_limit=max_rows)
    except SemanticRuntimeError as exc:
        if exc.reason != 'sales_page_invalid':
            raise
        raise SemanticRuntimeError('DMS订单分批结果存在缺页或变化，暂未展示结果',reason='dms_points_order_page_invalid') from exc
    except (ValueError,TypeError,KeyError,ArithmeticError) as exc:
        raise SemanticRuntimeError('DMS订单月度汇总与明细未通过对账，暂未展示结果',reason='dms_points_order_reconciliation_failed') from exc

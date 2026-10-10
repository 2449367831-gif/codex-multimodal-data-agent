"""Independent raw-source order/return reference; emit Boolean evidence only."""

import argparse
from collections import defaultdict
from decimal import Decimal
from fractions import Fraction
import json
from pathlib import Path
import runpy
import sys
from zoneinfo import ZoneInfo

import sqlglot
from sqlglot import exp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.agent_core.order_components import OrderComponentPlan, retrieve_order_components
from app.agent_core.order_return_composition import retrieve_order_returns
from app.agent_core.return_components import ReturnComponentPlan
from app.agent_core.sales_pages import retrieve_sales_groups
from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result

RETURN_REFERENCE = runpy.run_path(str(ROOT / 'tests/acceptance/check_return_components_live.py'))
identity = RETURN_REFERENCE['identity']
integer = RETURN_REFERENCE['integer']
instant = RETURN_REFERENCE['instant']
ORDER_QUALITY_FLAGS = ('header_missing_identity', 'header_invalid_delete', 'header_missing_created',
                       'header_multiple_active', 'detail_missing_identity', 'detail_invalid_delete',
                       'detail_missing_quantity', 'detail_duplicate_identity')
QUALITY_FLAGS = frozenset(ORDER_QUALITY_FLAGS) | frozenset(RETURN_REFERENCE['RETURN_QUALITY_FLAGS'])


def reference_sql(plan, source):
    if source == 'header':
        table = 'scm_sales_order_header'
        fields = ('company_code', 'sales_order_code', 'currency_code', 'total_pay_amount',
                  'created', 'updated', 'status', '_fdl_marked_deleted')
    elif source == 'detail':
        table = 'scm_sales_order_detail'
        fields = ('id', 'company_code', 'sales_order_code', 'sku_code', 'ship_qty',
                  'total_pay_amount', 'status', 'updated', '_fdl_marked_deleted')
    else:
        raise ValueError('unknown order reference source')
    conditions = ["(r._fdl_marked_deleted IS NULL OR r._fdl_marked_deleted<>'true')"]
    for field, codes in (('company_code', plan.owner_codes), ('sales_order_code', plan.order_codes),
                         ('sku_code', plan.sku_codes if source == 'detail' else ())):
        if codes:
            conditions.append(f'TRIM(r.{field}) IN (' + ','.join(exp.Literal.string(v).sql(dialect='doris') for v in codes) + ')')
    if source == 'detail':
        conditions.append('r.status=0')
    window = 'TRUE'
    if plan.start is not None:
        start, end = (exp.Literal.string(t.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')).sql(dialect='doris')
                      for t in (plan.start, plan.end))
        window = f'(h.created IS NULL OR (h.created>={start} AND h.created<{end}))'
    if source == 'detail' or plan.start is not None:
        # Keep every nondeleted version of selected keys, including versions
        # outside the date window. A date predicate on r would hide conflicts.
        conditions.append(f"""EXISTS (SELECT 1 FROM ywzt_ods.scm_sales_order_header h
          WHERE NULLIF(TRIM(h.company_code),'')<=>NULLIF(TRIM(r.company_code),'')
            AND NULLIF(TRIM(h.sales_order_code),'')<=>NULLIF(TRIM(r.sales_order_code),'')
            AND (h._fdl_marked_deleted IS NULL OR h._fdl_marked_deleted<>'true') AND {window})""")
    # Push down only exclusions independently applied by expected_orders.
    where = ' AND '.join(conditions) or 'TRUE'
    return f"SELECT {','.join('r.' + field for field in fields)},COUNT(*) OVER() AS group_count FROM ywzt_ods.{table} r WHERE {where} ORDER BY {','.join('r.' + field for field in fields)} LIMIT 501"


def money(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError('invalid reference payment')
    decimal = Decimal(str(value))
    if not decimal.is_finite():
        raise ValueError('invalid reference payment')
    return Fraction(decimal)


def reference_quality_sql(plan):
    def relation(source):
        tree = sqlglot.parse_one(reference_sql(plan, source), read='doris')
        tree.set('expressions', [field for field in tree.selects if field.alias_or_name != 'group_count'])
        tree.set('order', None)
        tree.set('limit', None)
        return tree.sql(dialect='doris')
    identity_columns = "NULLIF(TRIM(company_code),''),NULLIF(TRIM(sales_order_code),'')"
    tests = (
        "SELECT 1 FROM ref_headers WHERE NULLIF(TRIM(company_code),'') IS NULL OR NULLIF(TRIM(sales_order_code),'') IS NULL",
        "SELECT 1 FROM ref_headers WHERE _fdl_marked_deleted IS NULL OR _fdl_marked_deleted<>'false'",
        'SELECT 1 FROM ref_headers WHERE created IS NULL',
        f'SELECT 1 FROM ref_headers GROUP BY {identity_columns} HAVING COUNT(*)>1',
        "SELECT 1 FROM ref_details WHERE NULLIF(TRIM(id),'') IS NULL OR NULLIF(TRIM(sku_code),'') IS NULL OR NULLIF(TRIM(company_code),'') IS NULL OR NULLIF(TRIM(sales_order_code),'') IS NULL",
        "SELECT 1 FROM ref_details WHERE _fdl_marked_deleted IS NULL OR _fdl_marked_deleted<>'false'",
        'SELECT 1 FROM ref_details WHERE ship_qty IS NULL',
        "SELECT 1 FROM ref_details GROUP BY NULLIF(TRIM(company_code),''),NULLIF(TRIM(id),'') HAVING COUNT(*)>1",
    )
    columns = ','.join(f'CASE WHEN EXISTS ({test}) THEN 1 ELSE 0 END AS {key}' for key, test in zip(ORDER_QUALITY_FLAGS, tests))
    return (f'WITH ref_headers AS ({relation("header")}),ref_details AS ({relation("detail")}),'
            f'reference_order_quality AS (SELECT {columns}) SELECT * FROM reference_order_quality LIMIT 1')


def read_order_quality(plan, query):
    rows = query(reference_quality_sql(plan), purpose='independent order reference quality')
    if not isinstance(rows, (list, tuple)) or len(rows) != 1 or not isinstance(rows[0], dict) or set(rows[0]) != set(ORDER_QUALITY_FLAGS):
        raise ValueError('invalid reference quality projection')
    flags = {}
    for key in ORDER_QUALITY_FLAGS:
        try:
            number = integer(rows[0][key])
        except (TypeError, ArithmeticError) as exc:
            raise ValueError('invalid reference quality flag') from exc
        if number not in (0, 1):
            raise ValueError('invalid reference quality flag')
        flags[key] = bool(number)
    return flags


class ReferenceQualityError(ValueError):
    reason = 'order_reference_quality_failed'

    def __init__(self, flags):
        super().__init__('Independent order source quality failed')
        self.quality_flags = tuple(key for key in ORDER_QUALITY_FLAGS if flags[key])


def require_order_quality(plan, query):
    flags = read_order_quality(plan, query)
    if any(flags.values()):
        raise ReferenceQualityError(flags)


def stamp(value):
    parsed = instant(value)
    return parsed.isoformat() if parsed is not None else None


def expected_orders(headers, details, plan):
    versions = defaultdict(list)
    for row in headers:
        owner, order = identity(row['company_code']), identity(row['sales_order_code'])
        if (plan.owner_codes and owner not in plan.owner_codes) or (plan.order_codes and order not in plan.order_codes):
            continue
        if row['_fdl_marked_deleted'] != 'true':
            versions[owner, order].append(row)
    orders = {}
    for key, candidates in versions.items():
        if plan.start is not None and not any(row['created'] is None or plan.start <= instant(row['created']) < plan.end for row in candidates):
            continue
        if len(candidates) != 1 or None in key or candidates[0]['_fdl_marked_deleted'] != 'false':
            raise ValueError('reference header quality')
        row = candidates[0]
        created = stamp(row['created'])
        if created is None:
            raise ValueError('reference creation missing')
        payment = money(row['total_pay_amount'])
        orders[key] = dict(cargo_owner=key[0], sales_order_code=key[1], currency_code=identity(row['currency_code']),
            order_created=created, source_rows=1, missing_payment_rows=int(payment is None),
            missing_update_rows=int(row['updated'] is None), latest_update=stamp(row['updated']),
            header_payment=payment, known_header_payment=payment or Fraction(0), missing_details=True)

    lines, seen = defaultdict(list), set()
    for row in details:
        key = identity(row['company_code']), identity(row['sales_order_code'])
        sku = identity(row['sku_code'])
        if key not in orders or (plan.sku_codes and sku not in plan.sku_codes):
            continue
        if row['status'] != 0 or row['_fdl_marked_deleted'] == 'true':
            continue
        detail_id = identity(row['id'])
        if sku is None or detail_id is None or row['_fdl_marked_deleted'] != 'false' or (key[0], detail_id) in seen:
            raise ValueError('reference detail quality')
        seen.add((key[0], detail_id))
        lines[key + (sku,)].append((integer(row['ship_qty']), money(row['total_pay_amount']), instant(row['updated'])))

    groups = []
    for key in sorted(lines):
        values, order = lines[key], orders[key[:2]]
        order['missing_details'] = False
        missing = sum(amount is None for _, amount, _ in values)
        known = sum((amount for _, amount, _ in values if amount is not None), Fraction(0))
        times = [time for _, _, time in values if time is not None]
        groups.append(dict(cargo_owner=key[0], sales_order_code=key[1], sku_code=key[2],
            currency_code=order['currency_code'], order_created=order['order_created'],
            source_rows=len(values), missing_payment_rows=missing, missing_update_rows=len(values) - len(times),
            latest_update=max(times).isoformat() if times else None,
            order_ship_qty=sum(quantity for quantity, _, _ in values),
            detail_payment=None if missing else known, known_detail_payment=known,
            negative_ship_rows=sum(quantity < 0 for quantity, _, _ in values)))

    totals = {}
    for currency in {order['currency_code'] for order in orders.values()}:
        order_rows = [order for order in orders.values() if order['currency_code'] == currency]
        sku_rows = [group for group in groups if group['currency_code'] == currency]
        total = dict(order_count=len(order_rows), detail_count=sum(row['source_rows'] for row in sku_rows),
                     order_ship_qty=sum(row['order_ship_qty'] for row in sku_rows))
        for family, rows in (('header', order_rows), ('detail', sku_rows)):
            missing = sum(row['missing_payment_rows'] for row in rows)
            known = sum((row['known_' + family + '_payment'] for row in rows), Fraction(0))
            total.update({family + '_payment': None if missing else known, 'known_' + family + '_payment': known,
                'missing_' + family + '_payment_rows': missing,
                'missing_' + family + '_update_rows': sum(row['missing_update_rows'] for row in rows)})
        totals[currency] = total
    return dict(orders=[orders[key] for key in sorted(orders)], groups=groups, currency_totals=totals)


def expected_composition(sales, returns, amount_mode):
    if amount_mode not in ('pending', 'proportional'):
        raise ValueError('unconfirmed amount mode')
    groups, totals = [], {}
    sale_orders, return_orders = defaultdict(dict), defaultdict(dict)
    for row in sales['groups']:
        sale_orders[row['cargo_owner'], row['sales_order_code']][row['sku_code']] = row
    for row in returns['groups']:
        return_orders[row['cargo_owner'], row['sales_order_code']][row['sku_code']] = row
    for order in sales['orders']:
        key = order['cargo_owner'], order['sales_order_code']
        sold, returned = sale_orders[key], return_orders[key]
        for sku in sorted(sold.keys() | returned.keys()):
            sale, ret = sold.get(sku), returned.get(sku)
            shipped = sale['order_ship_qty'] if sale else None
            quantity = ret['returned_qty'] if ret else 0
            net = shipped - quantity if shipped is not None else None
            original = sale['detail_payment'] if sale else None
            currency = order['currency_code']
            conditions = [
                ('missing_sales_sku', sale is None),
                ('negative_sales_source', sale is not None and sale['negative_ship_rows'] > 0),
                ('negative_return_source', ret is not None and ret['negative_detail_count'] > 0),
                ('returns_exceed_shipment', shipped is not None and quantity > shipped),
                ('amount_choice_pending', amount_mode == 'pending'),
                ('missing_detail_payment', amount_mode == 'proportional' and sale is not None and original is None),
                ('missing_currency', amount_mode == 'proportional' and sale is not None and currency is None),
                ('negative_shipment_payment_unresolved', amount_mode == 'proportional' and shipped is not None and shipped < 0),
            ]
            paid = deduction = None
            if amount_mode == 'proportional' and original is not None and currency is not None and shipped >= 0:
                # Compute deduction first, independently of runtime's net-payment expression.
                deduction = original * Fraction(quantity, shipped) if shipped else Fraction(0)
                paid = original - deduction
                conditions.append(('zero_shipment_kept_payment', shipped == 0))
            groups.append(dict(cargo_owner=key[0], sales_order_code=key[1], sku_code=sku, currency_code=currency,
                order_ship_qty=shipped, returned_qty=quantity, net_qty=net, original_detail_payment=original,
                proportional_payment=paid, proportional_deduction=deduction,
                return_document_count=ret['return_document_count'] if ret else 0,
                return_detail_count=ret['return_detail_count'] if ret else 0,
                sales_detail_count=sale['source_rows'] if sale else 0,
                issues=tuple(name for name, applies in conditions if applies)))
            total = totals.setdefault(currency, dict(known_proportional_payment=Fraction(0),
                known_proportional_deduction=Fraction(0), unavailable_payment_groups=0))
            if paid is None:
                total['unavailable_payment_groups'] += 1
            else:
                total['known_proportional_payment'] += paid
                total['known_proportional_deduction'] += deduction
    return dict(orders=sales['orders'], groups=groups, currency_totals=totals)


def compare(actual, expected):
    return {field: getattr(actual, field) == expected[field] for field in ('orders', 'groups', 'currency_totals')}


class VerificationFailure(RuntimeError):
    def __init__(self, stage, cause):
        super().__init__('Independent verification did not complete')
        self.stage = stage
        self.reason = getattr(cause, 'reason', None)
        self.page_failure = getattr(cause, 'page_failure', None)
        self.quality_flags = tuple(key for key in getattr(cause, 'quality_flags', ()) if key in QUALITY_FLAGS)


def _step(stage, operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except VerificationFailure:
        raise
    except Exception as exc:
        raise VerificationFailure(stage, exc) from exc


def reference_snapshot(plan, query, *, amount_mode='pending', query_many=None, phase='reference', **budgets):
    _step(phase + '.sales_preflight', require_order_quality, plan, query)
    def read(source, sql):
        return _step(phase + '.' + source, retrieve_sales_groups, sql, query, query_many=query_many, **budgets)
    sales = _step(phase + '.sales_quality', expected_orders,
                  read('sales_header', reference_sql(plan, 'header')),
                  read('sales_detail', reference_sql(plan, 'detail')), plan)
    return_plan = ReturnComponentPlan(owner_codes=plan.owner_codes, order_codes=plan.order_codes, sku_codes=plan.sku_codes,
        order_keys=tuple((row['cargo_owner'], row['sales_order_code']) for row in sales['orders']))
    if sales['orders']:
        _step(phase + '.return_preflight', RETURN_REFERENCE['require_return_quality'], return_plan, query)
        headers = read('return_header', RETURN_REFERENCE['reference_sql'](return_plan, 'header'))
        details = read('return_detail', RETURN_REFERENCE['reference_sql'](return_plan, 'detail'))
    else:
        headers, details = [], []
    returns = _step(phase + '.return_quality', RETURN_REFERENCE['expected_result'], headers, details, return_plan)
    return dict(sales=sales, returns=returns,
                composition=_step(phase + '.composition', expected_composition, sales, returns, amount_mode))


def verify(plan, query, *, amount_mode='pending', query_many=None, **budgets):
    def reference(phase):
        return reference_snapshot(plan, query, amount_mode=amount_mode, query_many=query_many, phase=phase, **budgets)
    before = reference('reference_before')
    sales = _step('runtime_sales', retrieve_order_components, plan, query, query_many=query_many, **budgets)
    net = _step('runtime_composition', retrieve_order_returns, plan, query, amount_mode=amount_mode, query_many=query_many, **budgets)
    checks = {'sales_' + key: value for key, value in compare(sales, before['sales']).items()}
    checks.update({'net_' + key: value for key, value in compare(net, before['composition']).items()})
    checks['reference_stable'] = before == reference('reference_after')
    return checks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--page-size', type=int, default=2000)
    parser.add_argument('--amount-mode', choices=('pending', 'proportional'), default='pending')
    parser.add_argument('--quality-only', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.page_size <= 10000:
        parser.error('page size outside backend limit')
    _hide_windows_subprocesses()
    bridge = CodexMcpBridge(project_root=ROOT, backend_url='http://127.0.0.1:8010', timeout_seconds=180)
    with bridge.request_session() as client:
        def query(sql, *, purpose):
            return validate_doris_result(client.query(sql=sql, purpose='independent order return validation')).rows
        def many(statements, *, purpose, timeout_seconds):
            return [validate_doris_result(response).rows for response in client.query_many(
                statements=statements, purpose='independent order return validation', timeout_seconds=timeout_seconds)]
        if args.quality_only:
            flags = read_order_quality(OrderComponentPlan(), query)
            print(json.dumps({'case': 'order_returns', 'scope': 'ALL', 'verification': 'source_quality_only',
                              'quality_flags': flags, 'numeric_reconciled': False,
                              'published_business_acceptance': False}), flush=True)
            return 1 if any(flags.values()) else 0
        checks = verify(OrderComponentPlan(), query, query_many=many, amount_mode=args.amount_mode,
                        page_size=args.page_size, page_cap=max(500, args.page_size))
    print(json.dumps({'case': 'order_returns', 'scope': 'ALL', 'checks': checks,
                      'published_business_acceptance': False}), flush=True)
    return 0 if all(checks.values()) else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'case': 'order_returns', 'passed': False, 'error_type': type(exc).__name__,
                          'reason': getattr(exc, 'reason', None), 'page_failure': getattr(exc, 'page_failure', None),
                          'stage': getattr(exc, 'stage', None),
                          'quality_flags': [key for key in getattr(exc, 'quality_flags', ()) if key in QUALITY_FLAGS],
                          'published_business_acceptance': False}), flush=True)
        raise SystemExit(1)

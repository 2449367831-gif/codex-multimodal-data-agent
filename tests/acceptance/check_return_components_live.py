"""Independent return-fact reference; only Boolean evidence leaves memory."""

import argparse
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import sqlglot
from sqlglot import exp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.agent_core.return_components import ReturnComponentPlan, retrieve_return_components
from app.agent_core.sales_pages import retrieve_sales_groups
from app.agent_core.semantic_types import SemanticRuntimeError
from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result


RETURN_QUALITY_FLAGS = (
    'header_missing_identity', 'header_invalid_delete', 'header_multiple_active',
    'detail_missing_identity', 'detail_missing_quantity', 'detail_invalid_delete',
    'detail_duplicate_identity', 'orphan_details', 'conflicting_detail_reference',
)


def reference_sql(plan, source):
    if source == 'header':
        table = 'scm_return_order_header'
        fields = ('company_code', 'return_order_code', 'ref_order_code', 'status', 'updated_date', '_fdl_marked_deleted')
    elif source == 'detail':
        table = 'scm_return_order_detail'
        fields = ('id', 'company_code', 'return_order_code', 'ref_order_code', 'sku_code', 'fulfill_qty',
                  'status', 'updated_date', '_fdl_marked_deleted')
    else:
        raise ValueError('unknown return reference source')
    conditions = []
    for field, values in (('company_code', plan.owner_codes), ('sku_code', plan.sku_codes if source == 'detail' else ())):
        if values:
            conditions.append(f'TRIM(r.{field}) IN (' + ','.join(exp.Literal.string(v).sql(dialect='doris') for v in values) + ')')

    def matches(alias):
        owner = exp.func('NULLIF', exp.func('TRIM', exp.column('company_code', table=alias)), exp.Literal.string(''))
        order = exp.func('NULLIF', exp.func('TRIM', exp.column('ref_order_code', table=alias)), exp.Literal.string(''))
        tests = []
        if plan.order_codes:
            tests.append(order.copy().isin(*(exp.Literal.string(value) for value in plan.order_codes)))
        if plan.order_keys is not None:
            pairs = [exp.and_(owner.copy().eq(exp.Literal.string(o)), order.copy().eq(exp.Literal.string(s)))
                     for o, s in plan.order_keys]
            tests.append(exp.or_(*pairs) if pairs else exp.false())
        return exp.and_(*tests).sql(dialect='doris') if tests else 'TRUE'

    def same_document(alias):
        return (f"NULLIF(TRIM({alias}.company_code),'')<=>NULLIF(TRIM(r.company_code),'') AND "
                f"NULLIF(TRIM({alias}.return_order_code),'')<=>NULLIF(TRIM(r.return_order_code),'')")

    if source == 'detail':
        conditions.extend(['r.status=0', "(r._fdl_marked_deleted IS NULL OR r._fdl_marked_deleted<>'true')"])
    if plan.order_codes or plan.order_keys is not None:
        headers = f"""EXISTS (SELECT 1 FROM ywzt_ods.scm_return_order_header h WHERE {same_document('h')}
          AND (h._fdl_marked_deleted IS NULL OR h._fdl_marked_deleted<>'true') AND ({matches('h')}))"""
        if source == 'detail':
            # Keep mismatched detail references through a selected header, and
            # independently keep orphan references that belong to the cohort.
            conditions.append(f'(({matches("r")}) OR {headers})')
        else:
            skus = '' if not plan.sku_codes else ' AND TRIM(d.sku_code) IN (' + ','.join(exp.Literal.string(v).sql(dialect='doris') for v in plan.sku_codes) + ')'
            details = f"""EXISTS (SELECT 1 FROM ywzt_ods.scm_return_order_detail d WHERE {same_document('d')}
              AND d.status=0 AND (d._fdl_marked_deleted IS NULL OR d._fdl_marked_deleted<>'true')
              AND ({matches('d')}){skus})"""
            # Retain excluded headers referenced by selected details, so they
            # cannot be misclassified as absent headers. Never choose one version.
            conditions.append(f'({headers} OR {details})')
    where = ' AND '.join(conditions) or 'TRUE'
    return f"SELECT {','.join('r.' + field for field in fields)},COUNT(*) OVER() AS group_count FROM ywzt_ods.{table} r WHERE {where} ORDER BY {','.join('r.' + field for field in fields)} LIMIT 501"


def identity(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError('reference identity type')
    return value.strip(' ') or None


def integer(value):
    if value is None or isinstance(value, bool):
        raise ValueError('reference quantity missing')
    decimal = Decimal(str(value))
    if not decimal.is_finite() or decimal != decimal.to_integral_value():
        raise ValueError('reference quantity type')
    return int(decimal)


def instant(value):
    if value is None:
        return None
    parsed = datetime.fromisoformat(str(value))
    zone = ZoneInfo('Asia/Shanghai')
    return parsed.replace(tzinfo=zone) if parsed.tzinfo is None else parsed.astimezone(zone)


def reference_quality_sql(plan):
    def relation(source):
        tree = sqlglot.parse_one(reference_sql(plan, source), read='doris')
        tree.set('expressions', [field for field in tree.selects if field.alias_or_name != 'group_count'])
        tree.set('order', None)
        tree.set('limit', None)
        return tree.sql(dialect='doris')

    def same_document(left, right):
        return (f"NULLIF(TRIM({left}.company_code),'')<=>NULLIF(TRIM({right}.company_code),'') AND "
                f"NULLIF(TRIM({left}.return_order_code),'')<=>NULLIF(TRIM({right}.return_order_code),'')")

    tests = (
        "SELECT 1 FROM active_headers WHERE NULLIF(TRIM(company_code),'') IS NULL OR NULLIF(TRIM(return_order_code),'') IS NULL OR NULLIF(TRIM(ref_order_code),'') IS NULL",
        "SELECT 1 FROM active_headers WHERE _fdl_marked_deleted IS NULL OR _fdl_marked_deleted<>'false'",
        "SELECT 1 FROM active_headers GROUP BY NULLIF(TRIM(company_code),''),NULLIF(TRIM(return_order_code),'') HAVING COUNT(*)>1",
        "SELECT 1 FROM selected_details WHERE NULLIF(TRIM(company_code),'') IS NULL OR NULLIF(TRIM(return_order_code),'') IS NULL OR NULLIF(TRIM(id),'') IS NULL OR NULLIF(TRIM(sku_code),'') IS NULL",
        'SELECT 1 FROM selected_details WHERE fulfill_qty IS NULL',
        "SELECT 1 FROM selected_details WHERE _fdl_marked_deleted IS NULL OR _fdl_marked_deleted<>'false'",
        "SELECT 1 FROM selected_details GROUP BY NULLIF(TRIM(company_code),''),NULLIF(TRIM(id),'') HAVING COUNT(*)>1",
        f'SELECT 1 FROM selected_details d WHERE NOT EXISTS (SELECT 1 FROM ref_headers h WHERE {same_document("d", "h")})',
        f"""SELECT 1 FROM selected_details d JOIN active_headers h ON {same_document('d', 'h')}
          WHERE h._fdl_marked_deleted='false' AND (NULLIF(TRIM(d.ref_order_code),'') IS NULL
            OR NULLIF(TRIM(h.ref_order_code),'') IS NULL OR TRIM(d.ref_order_code)<>TRIM(h.ref_order_code))""",
    )
    columns = ','.join(f'CASE WHEN EXISTS ({test}) THEN 1 ELSE 0 END AS {key}'
                       for key, test in zip(RETURN_QUALITY_FLAGS, tests, strict=True))
    # Deleted-only headers exclude their details; genuinely absent headers do not.
    return f"""WITH ref_headers AS ({relation('header')}),ref_details AS ({relation('detail')}),
      active_headers AS (SELECT * FROM ref_headers WHERE _fdl_marked_deleted IS NULL OR _fdl_marked_deleted<>'true'),
      selected_details AS (SELECT d.* FROM ref_details d
        WHERE EXISTS (SELECT 1 FROM active_headers h WHERE {same_document('d', 'h')})
           OR NOT EXISTS (SELECT 1 FROM ref_headers h WHERE {same_document('d', 'h')})),
      reference_return_quality AS (SELECT {columns}) SELECT * FROM reference_return_quality LIMIT 1"""


def read_return_quality(plan, query):
    rows = query(reference_quality_sql(plan), purpose='independent return reference quality')
    if (not isinstance(rows, (list, tuple)) or len(rows) != 1 or not isinstance(rows[0], dict)
            or set(rows[0]) != set(RETURN_QUALITY_FLAGS)):
        raise ValueError('invalid reference quality projection')
    flags = {}
    for key in RETURN_QUALITY_FLAGS:
        try:
            number = integer(rows[0][key])
        except (TypeError, ArithmeticError) as exc:
            raise ValueError('invalid reference quality flag') from exc
        if number not in (0, 1):
            raise ValueError('invalid reference quality flag')
        flags[key] = bool(number)
    return flags


class ReferenceQualityError(ValueError):
    reason = 'return_reference_quality_failed'

    def __init__(self, flags):
        super().__init__('Independent return source quality failed')
        self.quality_flags = tuple(key for key in RETURN_QUALITY_FLAGS if flags[key])


def require_return_quality(plan, query):
    flags = read_return_quality(plan, query)
    if any(flags.values()):
        raise ReferenceQualityError(flags)


def verify_quality_rejection(plan, query, *, query_many=None, **budgets):
    before = read_return_quality(plan, query)
    if not any(before.values()):
        raise ValueError('quality rejection requires independent source defects')
    rejected = False
    try:
        retrieve_return_components(plan, query, query_many=query_many, **budgets)
    except SemanticRuntimeError as exc:
        if exc.reason != 'return_component_quality_failed':
            raise
        rejected = True
    after = read_return_quality(plan, query)
    return {'source_defect_observed': True, 'runtime_quality_rejected': rejected,
            'reference_stable': before == after}


def expected_result(headers, details, plan):
    # Either reference can place a document in scope for consistency checking.
    detail_documents = set()
    for detail in details:
        owner, sku = identity(detail['company_code']), identity(detail['sku_code'])
        reference = identity(detail['ref_order_code'])
        if (plan.owner_codes and owner not in plan.owner_codes
                or plan.sku_codes and sku not in plan.sku_codes
                or detail['status'] != 0 or detail['_fdl_marked_deleted'] == 'true'
                or plan.order_codes and reference not in plan.order_codes
                or plan.order_keys is not None and (owner, reference) not in plan.order_keys):
            continue
        detail_documents.add((owner, identity(detail['return_order_code'])))
    by_document = defaultdict(list)
    for header in headers:
        owner = identity(header['company_code'])
        if plan.owner_codes and owner not in plan.owner_codes:
            continue
        by_document[owner, identity(header['return_order_code'])].append(header)
    selected = {}
    for key, versions in by_document.items():
        eligible = [h for h in versions if h['_fdl_marked_deleted'] != 'true']
        if not eligible:
            continue
        if key not in detail_documents and not any((not plan.order_codes or identity(h['ref_order_code']) in plan.order_codes)
                   and (plan.order_keys is None or (key[0], identity(h['ref_order_code'])) in plan.order_keys) for h in eligible):
            continue
        if len(eligible) != 1 or eligible[0]['_fdl_marked_deleted'] != 'false' or None in key:
            raise ValueError('reference header quality')
        order = identity(eligible[0]['ref_order_code'])
        if order is None:
            raise ValueError('reference sales order missing')
        selected[key] = order
    seen, documents, group_documents, groups = set(), set(), defaultdict(set), {}
    quantity_total, detail_count, latest = 0, 0, None
    for detail in details:
        owner, sku = identity(detail['company_code']), identity(detail['sku_code'])
        if plan.owner_codes and owner not in plan.owner_codes or plan.sku_codes and sku not in plan.sku_codes:
            continue
        if detail['status'] != 0 or detail['_fdl_marked_deleted'] == 'true':
            continue
        document_key = owner, identity(detail['return_order_code'])
        reference = identity(detail['ref_order_code'])
        if document_key not in selected:
            if document_key not in by_document and (not plan.order_codes or reference in plan.order_codes) and (plan.order_keys is None or (owner, reference) in plan.order_keys):
                raise ValueError('reference orphan detail')
            continue
        order = selected[document_key]
        line = identity(detail['id'])
        if line is None or sku is None or detail['_fdl_marked_deleted'] != 'false':
            raise ValueError('reference detail quality')
        if reference != order:
            raise ValueError('reference order conflict')
        if (owner, line) in seen:
            raise ValueError('reference duplicate line')
        seen.add((owner, line))
        quantity = integer(detail['fulfill_qty'])
        stamp = instant(detail['updated_date'])
        key = owner, order, sku
        group = groups.setdefault(key, dict(cargo_owner=owner, sales_order_code=order, sku_code=sku,
            returned_qty=0, return_detail_count=0, return_document_count=0,
            negative_detail_count=0, missing_update_count=0, latest_update=None))
        group['returned_qty'] += quantity
        group['return_detail_count'] += 1
        group['negative_detail_count'] += int(quantity < 0)
        group['missing_update_count'] += int(stamp is None)
        old_time = instant(group['latest_update'])
        new_time = max(old_time, stamp) if old_time and stamp else old_time or stamp
        group['latest_update'] = new_time.isoformat() if new_time else None
        group_documents[key].add(document_key)
        documents.add(document_key)
        quantity_total += quantity
        detail_count += 1
        latest = max(latest, stamp) if latest and stamp else latest or stamp
    for key, group in groups.items():
        group['return_document_count'] = len(group_documents[key])
    return dict(groups=[groups[key] for key in sorted(groups)], total_quantity=quantity_total,
                document_count=len(documents), detail_count=detail_count,
                latest_update=latest.isoformat() if latest else None)


def compare(actual, expected):
    return {name: getattr(actual, name) == expected[name]
            for name in ('groups', 'total_quantity', 'document_count', 'detail_count', 'latest_update')}


def verify(plan, query, *, query_many=None, **budgets):
    def reference():
        require_return_quality(plan, query)
        headers = retrieve_sales_groups(reference_sql(plan, 'header'), query, query_many=query_many, **budgets)
        details = retrieve_sales_groups(reference_sql(plan, 'detail'), query, query_many=query_many, **budgets)
        return expected_result(headers, details, plan)
    before = reference()
    actual = retrieve_return_components(plan, query, query_many=query_many, **budgets)
    checks = compare(actual, before)
    checks['reference_stable'] = before == reference()
    return checks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--page-size', type=int, default=2000)
    parser.add_argument('--quality-only', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.page_size <= 10000:
        parser.error('page size outside backend limit')
    _hide_windows_subprocesses()
    bridge = CodexMcpBridge(project_root=ROOT, backend_url='http://127.0.0.1:8010', timeout_seconds=180)
    with bridge.request_session() as client:
        def query(sql, *, purpose):
            return validate_doris_result(client.query(sql=sql, purpose='independent return component validation')).rows
        def many(statements, *, purpose, timeout_seconds):
            return [validate_doris_result(response).rows for response in client.query_many(
                statements=statements, purpose='independent return component validation', timeout_seconds=timeout_seconds)]
        verifier = verify_quality_rejection if args.quality_only else verify
        checks = verifier(ReturnComponentPlan(), query, query_many=many,
                          page_size=args.page_size, page_cap=max(500, args.page_size))
    print(json.dumps({'case': 'return_components', 'scope': 'ALL', 'checks': checks,
                      'verification_kind': 'quality_rejection' if args.quality_only else 'business_result',
                      'published_business_acceptance': False}), flush=True)
    return 0 if all(checks.values()) else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'case': 'return_components', 'passed': False, 'error_type': type(exc).__name__,
                          'reason': getattr(exc, 'reason', None), 'page_failure': getattr(exc, 'page_failure', None),
                          'quality_flags': list(getattr(exc, 'quality_flags', ())),
                          'published_business_acceptance': False}), flush=True)
        raise SystemExit(1)

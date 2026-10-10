"""Fixed synthetic reference-scope SQL on Doris; no business source reads."""

from datetime import datetime
import json
from pathlib import Path
import runpy
import sys
from zoneinfo import ZoneInfo

import sqlglot
from sqlglot import exp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.agent_core.order_components import OrderComponentPlan
from app.agent_core.return_components import ReturnComponentPlan
from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result

ORDER = runpy.run_path(str(ROOT / 'tests/acceptance/check_order_returns_live.py'))
RETURN = ORDER['RETURN_REFERENCE']


def synthetic_sql(sql, *, fixture_rows=None):
    fields = {
        'scm_sales_order_header': ('company_code','sales_order_code','currency_code','total_pay_amount','created','updated','status','_fdl_marked_deleted'),
        'scm_sales_order_detail': ('id','company_code','sales_order_code','sku_code','ship_qty','total_pay_amount','status','updated','_fdl_marked_deleted'),
        'scm_return_order_header': ('company_code','return_order_code','ref_order_code','status','updated_date','_fdl_marked_deleted'),
        'scm_return_order_detail': ('id','company_code','return_order_code','ref_order_code','sku_code','fulfill_qty','status','updated_date','_fdl_marked_deleted'),
    }
    stamp = '2026-09-01 00:00:00'
    rows = {
        'scm_sales_order_header': [('A','S1','CNY',100,stamp,stamp,100,'false'),
            ('A','S1','CNY',100,'2020-01-01 00:00:00',stamp,100,'false'),
            ('A','OLD','CNY',100,'2020-01-01 00:00:00',stamp,100,'false')],
        'scm_sales_order_detail': [('D1','A','S1','001',10,60,0,stamp,'false'),
            ('D2','A','OLD','001',10,60,0,stamp,'false')],
        'scm_return_order_header': [('A','R1','S1',100,stamp,'false'),
            ('A','R1','OTHER',100,stamp,'false'),('A','DELETED','OTHER',100,stamp,'true'),
            ('B','UNRELATED','S1',100,stamp,'false')],
        'scm_return_order_detail': [('CONFLICT','A','R1','OTHER','001',2,0,stamp,'false'),
            ('EXCLUDED','A','DELETED','S1','001',2,0,stamp,'false'),
            ('ORPHAN','A','MISSING','S1','001',2,0,stamp,'false'),
            ('UNRELATED','B','UNRELATED','S1','001',2,0,stamp,'false')],
    }
    if fixture_rows is not None:
        if set(fixture_rows) - set(fields):
            raise ValueError('unexpected synthetic fixture table')
        rows.update(fixture_rows)
    tree = sqlglot.parse_one(sql, read='doris')
    used = set()
    for table in tree.find_all(exp.Table):
        if table.db:
            if table.db != 'ywzt_ods' or table.name not in fields:
                raise ValueError('unexpected synthetic source')
            name = table.name
            used.add(name)
            table.set('this', exp.to_identifier('fixture_' + name))
            table.set('db', None)
    fixtures = []
    for name in sorted(used):
        selects = []
        for values in rows[name]:
            columns = []
            for field, value in zip(fields[name], values):
                kind = 'DATETIME' if field in ('created','updated','updated_date') else 'INT' if isinstance(value,int) else 'STRING'
                literal = exp.Null() if value is None else exp.Literal.number(value) if isinstance(value,int) else exp.Literal.string(value)
                columns.append(exp.alias_(exp.cast(literal,kind),field))
            selects.append(exp.select(*columns))
        relation = selects[0]
        for select in selects[1:]: relation = relation.union(select, distinct=False)
        fixtures.append(exp.CTE(this=relation,alias=exp.TableAlias(this=exp.to_identifier('fixture_' + name))))
    existing = tree.args.get('with_')
    tree.set('with_', exp.With(expressions=fixtures + (existing.expressions if existing else [])))
    return tree.sql(dialect='doris')


def cases():
    window = OrderComponentPlan(start=datetime(2026,9,1,tzinfo=ZoneInfo('Asia/Shanghai')),
                                end=datetime(2026,10,1,tzinfo=ZoneInfo('Asia/Shanghai')))
    returns = ReturnComponentPlan(order_keys=(('A','S1'),))
    return [
        ('order_header_versions',ORDER['reference_sql'](window,'header'),'sales_order_code',['S1','S1']),
        ('order_detail_scope',ORDER['reference_sql'](window,'detail'),'id',['D1']),
        ('return_header_versions',RETURN['reference_sql'](returns,'header'),'return_order_code',['DELETED','R1','R1']),
        ('return_detail_conflicts_and_orphans',RETURN['reference_sql'](returns,'detail'),'id',['CONFLICT','EXCLUDED','ORPHAN']),
        ('return_empty_cohort',RETURN['reference_sql'](ReturnComponentPlan(order_keys=()),'detail'),'id',[]),
        ('quality_projection',ORDER['reference_quality_sql'](window),'header_multiple_active',[1]),
    ]


def main():
    _hide_windows_subprocesses()
    bridge = CodexMcpBridge(project_root=ROOT,backend_url='http://127.0.0.1:8010',timeout_seconds=180)
    checks = {}
    with bridge.request_session() as client:
        for name, sql, field, expected in cases():
            rows = validate_doris_result(client.query(sql=synthetic_sql(sql),purpose='synthetic reference scope SQL verification')).rows
            checks[name] = sorted(row[field] for row in rows) == expected
    print(json.dumps({'case':'reference_scope_synthetic_doris','checks':checks,'business_source_reads':False}),flush=True)
    return 0 if all(checks.values()) else 1


if __name__ == '__main__':
    try: raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'case':'reference_scope_synthetic_doris','passed':False,'error_type':type(exc).__name__}),flush=True)
        raise SystemExit(1)

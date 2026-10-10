"""Doris compatibility of synthetic pagination cases, not business acceptance."""

import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))

from app.agent_core.sales_pages import retrieve_sales_groups
from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result


FACTS="WITH facts AS (SELECT 'B' AS sku,2 AS quantity UNION ALL SELECT 'A',1 UNION ALL SELECT 'B',2 UNION ALL SELECT 'C',3 UNION ALL SELECT 'A',1) "
CASES=(
    ('distinct',FACTS+'SELECT DISTINCT sku,quantity,3 AS group_count FROM facts ORDER BY sku LIMIT 501', [('A','1'),('B','2'),('C','3')]),
    ('aggregate_alias',FACTS+'SELECT sku,SUM(quantity) AS quantity,3 AS group_count FROM facts GROUP BY sku ORDER BY quantity DESC,sku LIMIT 501',[('B','4'),('C','3'),('A','2')]),
    ('aggregate_expression',FACTS+'SELECT sku,SUM(quantity) AS quantity,3 AS group_count FROM facts GROUP BY sku ORDER BY SUM(quantity) DESC,sku LIMIT 501',[('B','4'),('C','3'),('A','2')]),
    ('select_positions',FACTS+'SELECT sku,SUM(quantity) AS quantity,3 AS group_count FROM facts GROUP BY sku ORDER BY 2 DESC,1 LIMIT 501',[('B','4'),('C','3'),('A','2')]),
    ('window_alias',FACTS+'SELECT sku,SUM(quantity) AS quantity,RANK() OVER(ORDER BY SUM(quantity) DESC) AS rank_value,3 AS group_count FROM facts GROUP BY sku ORDER BY rank_value,sku LIMIT 501',[('B','4'),('C','3'),('A','2')]),
    ('renamed_qualified',FACTS+'SELECT DISTINCT f.sku AS code,quantity,3 AS group_count FROM facts f ORDER BY f.sku DESC LIMIT 501',[('C','3'),('B','2'),('A','1')]),
)


def main():
    _hide_windows_subprocesses()
    bridge=CodexMcpBridge(project_root=ROOT,backend_url='http://127.0.0.1:8010',timeout_seconds=180)
    checks={}
    with bridge.request_session() as client:
        def read(sql,*,purpose):
            return validate_doris_result(client.query(sql=sql,purpose='synthetic paging compatibility; no business data')).rows
        for name,sql,expected in CASES:
            rows=retrieve_sales_groups(sql,read,page_size=2)
            actual=[(row.get('sku',row.get('code')),str(row['quantity'])) for row in rows]
            checks[name]=actual==expected and [int(row['result_ordinal']) for row in rows]==[1,2,3]
    print(json.dumps({'synthetic_only':True,'checks':checks}),flush=True)
    return 0 if all(checks.values()) else 1


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'synthetic_only':True,'passed':False,'error_type':type(exc).__name__,'reason':getattr(exc,'reason',None)}),flush=True)
        raise SystemExit(1)

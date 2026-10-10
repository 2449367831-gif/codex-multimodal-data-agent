"""Independent customer-name reconciliation; keep real rows in memory only."""

from collections import defaultdict
import json
from pathlib import Path
import sys

import yaml

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))

from app.agent_core.customer_names import CustomerNamePolicy, retrieve_customer_names
from app.agent_core.sales_pages import retrieve_sales_groups
from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result


def reference_sql():
    return """WITH observations AS (
 SELECT NULLIF(TRIM(company_code),'') AS owner_key,NULLIF(TRIM(code),'') AS customer_key,
   NULLIF(TRIM(name),'') AS name_value,NULLIF(TRIM(price_group),'') AS price_value,
   COUNT(*) AS observations,MAX(updated) AS latest_updated,
   SUM(CASE WHEN updated IS NULL THEN 1 ELSE 0 END) AS missing_updates
 FROM ywzt_ods.master_store
 GROUP BY NULLIF(TRIM(company_code),''),NULLIF(TRIM(code),''),NULLIF(TRIM(name),''),NULLIF(TRIM(price_group),'')
)
SELECT *,COUNT(*) OVER () AS group_count FROM observations
ORDER BY owner_key,customer_key,name_value,price_value LIMIT 501"""


def expected_labels(observations,policy):
    customers=defaultdict(list)
    for row in observations:
        if not row['owner_key'] or not row['customer_key']:
            raise ValueError('reference contains invalid customer identities')
        customers[(row['owner_key'],row['customer_key'])].append(row)
    latest=max((str(row['latest_updated']) for row in observations if row['latest_updated'] is not None),default=None)
    missing_updates=sum(int(row['missing_updates']) for row in observations)
    expected={}
    for key,groups in customers.items():
        names={row['name_value'] for row in groups if row['name_value'] is not None}
        prices={row['price_value'] for row in groups}
        name=next(iter(names)) if len(names)==1 else None
        price=next(iter(prices)) if len(prices)==1 else None
        exception=key[1] in policy.exception_codes
        merged=prices=={policy.price_group} and not exception
        expected[key]={
            'cargo_owner':key[0],'customer_code':key[1],'original_name':name,
            'display_name':policy.merged_name if merged else name,'price_group':price,
            'source_record_count':sum(int(row['observations']) for row in groups),
            'name_variants':len(names),'missing_name_rows':sum(int(row['observations']) for row in groups if row['name_value'] is None),
            'price_variants':len(prices),'missing_price_rows':sum(int(row['observations']) for row in groups if row['price_value'] is None),
            'is_exception':int(exception),'merge_applied':int(merged),'missing_master':0,
            'source_latest_updated':latest,'missing_update_rows':missing_updates,
        }
    return expected


def compare_labels(rows,expected):
    actual={(row['cargo_owner'],row['customer_code']):dict(row) for row in rows}
    for row in actual.values():
        if row['source_latest_updated'] is not None:
            row['source_latest_updated']=str(row['source_latest_updated'])
    return {'all_keys_names_flags_and_counts':actual==expected,'no_duplicate_identities':len(actual)==len(rows),
            'physical_source_records':sum(row['source_record_count'] for row in actual.values())==sum(row['source_record_count'] for row in expected.values())}


def main():
    _hide_windows_subprocesses()
    bridge=CodexMcpBridge(project_root=ROOT,backend_url='http://127.0.0.1:8010',timeout_seconds=180)
    def query(sql,*,purpose):
        return validate_doris_result(bridge.query(sql=sql,purpose=purpose)).rows
    policy=CustomerNamePolicy.from_contract(yaml.safe_load((ROOT/'metrics/customer.display_name.yaml').read_text(encoding='utf-8')))
    rows=retrieve_customer_names(policy,query)
    print(json.dumps({'stage':'customer_directory_read','complete':True}),flush=True)
    reference=retrieve_sales_groups(reference_sql(),query)
    checks=compare_labels(rows,expected_labels(reference,policy))
    print(json.dumps({'case':'T12_core','checks':checks}),flush=True)
    return 0 if checks and all(checks.values()) else 1


if __name__=='__main__':
    raise SystemExit(main())

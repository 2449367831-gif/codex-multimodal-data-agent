"""Actual chat API and every screened result page, independently reconciled."""

from datetime import datetime,timedelta
import http.cookiejar
import json
from pathlib import Path
import sys
import time
import urllib.request
from uuid import uuid4
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))

from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result
from app.agent_core.sales_pages import retrieve_sales_groups
from check_outbound_boxes_live import reference_sql,independently_compare


def main():
    _hide_windows_subprocesses()
    base="http://127.0.0.1:8021"
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def request(path,body=None,headers=None):
        req=urllib.request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,
                                  headers={"Content-Type":"application/json","Origin":base,**(headers or {})})
        with opener.open(req,timeout=30) as response:
            return json.load(response)
    session=request("/v1/session")
    headers={"X-CSRF-Token":session["csrf_token"],"Idempotency-Key":"boxes-acceptance-"+uuid4().hex}
    cases=json.loads((ROOT/"tests/acceptance/complex-query-cases.json").read_text(encoding="utf-8"))["cases"]
    body={"question":next(c["question"] for c in cases if c["id"]=="T08"),"conversation_id":session["conversation_id"]}
    task=request("/v1/tasks",body,headers)
    print(json.dumps({"task_id":task["task_id"],"state":task["state"]}),flush=True)
    deadline=time.monotonic()+300
    while task["state"] in {"queued","running"}:
        if time.monotonic()>=deadline:
            print(json.dumps({"task_id":task["task_id"],"state":"observation_timeout"}))
            return 2
        time.sleep(1)
        task=request("/v1/tasks/"+task["task_id"])
    result=task.get("result",{})
    if task.get("http_status")!=200 or result.get("result_state") not in {"live_success","no_data"}:
        print(json.dumps({"http_status":task.get("http_status"),"result_state":result.get("result_state"),"error_code":result.get("error",{}).get("code")}))
        return 1
    table=result["business_table"]
    pagination=table.get("pagination")
    rows=list(table["rows"])
    if pagination:
        for number in range(2,pagination["page_count"]+1):
            page=request(f"/v1/results/{pagination['result_id']}/pages/{number}")["business_table"]
            meta=page["pagination"]
            if meta["total_rows"]!=pagination["total_rows"] or meta["page"]!=number or page["columns"]!=table["columns"] or page["summary"]!=table["summary"]:
                raise ValueError("inconsistent public page")
            rows.extend(page["rows"])
    table={**table,"rows":rows}
    print(json.dumps({"stage":"all_public_pages_received"}),flush=True)
    bridge=CodexMcpBridge(project_root=ROOT,backend_url="http://127.0.0.1:8010",timeout_seconds=180)
    def query(sql,*,purpose):
        return validate_doris_result(bridge.query(sql=sql,purpose="T08 chat independent reference")).rows
    today=datetime.now(ZoneInfo("Asia/Shanghai")).date()
    reference=retrieve_sales_groups(reference_sql(today-timedelta(days=1),today),query)
    checks=independently_compare(table,reference)
    checks.update(no_clarification=result["clarification"]["required"] is False,
                  all_pages=len(rows)==pagination["total_rows"] if pagination else len(rows)<=100,
                  same_conversation=result["conversation_id"]==session["conversation_id"],
                  idempotent=request("/v1/tasks",body,headers)["task_id"]==task["task_id"],
                  public_columns=len(table["columns"])==11)
    print(json.dumps({"case":"T08","checks":checks}))
    return 0 if all(checks.values()) else 1


if __name__=="__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"case":"T08","error_type":type(exc).__name__,"reason":getattr(exc,"reason",None)}))
        raise SystemExit(2)

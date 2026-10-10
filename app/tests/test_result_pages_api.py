"""Authenticated paging never exposes unchecked rows or crosses installation owners."""

from copy import deepcopy
import json
import pytest

from fastapi.testclient import TestClient

from app.chat_api import IdentityResolver, create_chat_app
from app.chat_sessions import ChatSessionStore
from app.tests.test_chat_api import FakeChatRunner
from app.agent_core.semantic_types import SemanticRequest


def raw_table():
    return {"format": "table", "table_id": "purchase_received_detail", "complete_reconciled": True,
        "caption": "生产日期窗口内的采购入库明细", "summary": [], "notes": [],
        "rows": [{"cargo_owner": "TEST", "inbound_code": "TEST-R1", "detail_id": f"TEST-D{i}", "item_code": str(i),
                  "warehouse_code": "TEST-W1", "warehouse_name": "测试仓", "sku_code": "TEST-S1", "batch_number": None,
                  "production_date": "2026-09-04 00:00:00", "received_quantity": "1.25", "quantity_unit": "10",
                  "actual_arrival_date": "2026-09-09 00:00:00", "private_internal": "must never appear"} for i in range(501)]}


def build_app(tmp_path, table=None, *, reviewed=True, route="purchase_received_detail"):
    result_table = table if table is not None else raw_table()
    class Runner(FakeChatRunner):
        def run(self, question, **_):
            return {"status": "completed", "thread_id": "test-pages-thread", "result_state": "live_success",
                "interaction": {"outcome": "completed", "questions": [], "answer": "采购入库明细已完成对账"},
                "final_response": json.dumps({"outcome": "completed", "answer": "采购入库明细已完成对账"}),
                "query_execution": {"live_business_result_executed": True, "live_query_executed": True, "status": "live_result"},
                "query_plan": {"route": route}, "business_table": deepcopy(result_table)}
    class Reviewed:
        def classify(self, question):
            return SemanticRequest(route, None, None)
        def run_if_supported(self, question, **_):
            return Runner().run(question)
    return create_chat_app(Runner(), semantic_runtime=Reviewed() if reviewed else None, session_store=ChatSessionStore(tmp_path / "session.sqlite3"), identity_resolver=IdentityResolver(mode="anonymous"), enforce_loopback=False)


def submit(client):
    session = client.get("/v1/session").json()
    headers = {"X-CSRF-Token": session["csrf_token"], "Idempotency-Key": "test_receipt_pages_001"}
    conversation = client.post("/v1/conversations", headers=headers).json()["conversation_id"]
    return client.post("/v1/chat", headers=headers, json={"question": "查询采购入库明细", "conversation_id": conversation})


def test_all_pages_are_screened_owner_bound_and_idempotent(tmp_path):
    app = build_app(tmp_path)
    with TestClient(app) as a, TestClient(app) as b:
        response = submit(a)
        assert response.status_code == 200
        table = response.json()["business_table"]
        paging = table["pagination"]
        assert len(table["rows"]) == 100 and paging["total_rows"] == 501 and paging["page_count"] == 6
        uri = f"/v1/results/{paging['result_id']}/pages/6"
        final = a.get(uri)
        assert final.status_code == 200 and final.headers["cache-control"] == "no-store"
        assert final.json()["business_table"]["rows"][0]["detail_id"] == "TEST-D500"
        assert "private_internal" not in response.text + final.text
        assert submit(a).json()["business_table"]["pagination"]["result_id"] == paging["result_id"]
        assert b.get(uri).status_code != 200
        b.get("/v1/session")
        assert b.get(uri).status_code == 404
        app.state.result_pages.clock = lambda: paging["expires_at"] + 1
        assert a.get(uri).status_code == 410


def test_invalid_last_page_cannot_leak_a_successful_first_page(tmp_path):
    table = raw_table()
    table["rows"][-1]["sku_code"] = "SELECT secret"
    with TestClient(build_app(tmp_path, table)) as client:
        result = submit(client)
        assert result.status_code >= 400 and "business_table" not in result.json()
        assert not client.app.state.result_pages._results


def test_model_cannot_self_assert_complete_paginated_evidence(tmp_path):
    with TestClient(build_app(tmp_path, reviewed=False)) as client:
        result = submit(client)
        assert result.status_code >= 400 and "business_table" not in result.json()


@pytest.mark.parametrize("reviewed,invalid", [(True,False),(True,True),(False,False),(True,"notes"),(True,"note_count")])
def test_box_pages_preserve_nullable_conversions_and_reviewed_boundary(tmp_path,reviewed,invalid):
    table = {"format":"table","table_id":"sales_outbound_boxes","complete_reconciled":True,
             "caption":"按客户渠道汇总后的箱数","summary":[],"notes":["汇总后四舍五入；箱规缺失的空值不是零箱。"],
             "rows":[{"ship_date":"2026-09-09","cargo_owner":"TEST","warehouse_code":"W","warehouse_name":"测试仓",
                      "sku_code":f"TEST-{i}","customer_channel":"其他","quantity":"5.25","outbound_count":"1",
                      "box_spec":None,"box_quantity":None,"box_state":"缺少箱规"} for i in range(501)]}
    if invalid == "notes": table["notes"] = ["SELECT secret"]
    elif invalid == "note_count": table["notes"] = ["测试说明"] * 4
    elif invalid: table["rows"][-1]["box_state"] = "SELECT secret"
    with TestClient(build_app(tmp_path,table,reviewed=reviewed,route="sales_outbound_boxes")) as client:
        result = submit(client)
        if not reviewed or invalid:
            assert result.status_code >= 400 and "business_table" not in result.json()
        else:
            assert result.status_code == 200
            page = result.json()["business_table"]
            assert len(page["columns"]) == 11 and page["pagination"]["total_rows"] == 501
            tail = client.get(f"/v1/results/{page['pagination']['result_id']}/pages/6").json()["business_table"]
            assert tail["rows"][0]["sku_code"] == "TEST-500"
            assert tail["rows"][0]["box_quantity"] is None
            assert page["notes"] == tail["notes"] == table["notes"]

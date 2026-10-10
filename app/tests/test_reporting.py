from __future__ import annotations

from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from app.chat_api import IdentityResolver, create_chat_app
from app.chat_sessions import ChatSessionStore
from app.reporting import (
    ReportSourceError,
    build_report_document,
    normalize_report_tables,
    render_format,
)


def payload(*, rows=None, status="succeeded", result_state="live_success"):
    return {
        "status": "completed",
        "outcome": {"state": "completed"},
        "query_status": status,
        "result_state": result_state,
        "request_id": "12345678-1234-1234-1234-123456789012",
        "business_table": {
            "title": "按仓库销售出库",
            "caption": "本次查询结果",
            "columns": [
                {"key": "warehouse", "label": "仓库", "kind": "text"},
                {"key": "quantity", "label": "实际销售出库", "kind": "number", "unit": "EA"},
            ],
            "rows": rows if rows is not None else [{"warehouse": "测试仓", "quantity": "12.345"}],
            "notes": [],
            "summary": [],
        },
    }


def test_report_uses_live_rows_and_keeps_all_required_sections():
    source = payload()
    tables = normalize_report_tables(source)
    document = build_report_document(source, tables, title="供应链报告", audience="业务")
    assert document.tables[0].rows[0]["quantity"] == "12.345"
    assert any("核心" in item or "结果表" in item for item in document.findings)
    html, content_type, suffix = render_format(document, "html")
    assert suffix == ".html"
    assert content_type.startswith("text/html")
    assert "12.345" in html.decode("utf-8")
    assert "SELECT" not in html.decode("utf-8")


def test_report_rejects_empty_failed_but_accepts_verified_partial_views():
    for item in [
        payload(rows=[], status="empty_result", result_state="no_data"),
        payload(status="query_failed", result_state="failed"),
    ]:
        with pytest.raises(ReportSourceError):
            normalize_report_tables(item)
    for state in ("summary_only", "batch_complete"):
        source = payload(result_state=state)
        tables = normalize_report_tables(source)
        document = build_report_document(source, tables)
        assert document.warnings


def test_report_never_accepts_a_truncated_result():
    with pytest.raises(ReportSourceError):
        normalize_report_tables(payload(result_state="truncated_result"))


def test_report_expands_paged_result_before_building_files():
    source = payload(rows=[{"warehouse": "测试仓", "quantity": "1"}])
    source["business_table"]["pagination"] = {
        "result_id": "rp_test",
        "page": 1,
        "page_count": 2,
        "total_rows": 2,
    }

    def reader(result_id, page):
        assert result_id == "rp_test"
        return {"rows": [{"warehouse": "第二仓", "quantity": "2"}]} if page == 2 else source["business_table"]

    tables = normalize_report_tables(source, page_reader=reader)
    assert len(tables[0].rows) == 2
    assert tables[0].rows[-1]["warehouse"] == "第二仓"


def test_report_does_not_accept_browser_only_or_private_title_data():
    with pytest.raises(ReportSourceError):
        normalize_report_tables({"status": "completed", "outcome": {"state": "completed"}})
    source = payload()
    tables = normalize_report_tables(source)
    with pytest.raises(ReportSourceError):
        build_report_document(source, tables, title="SELECT internal data")


def test_report_api_reads_owned_completed_receipt_and_serves_files(tmp_path):
    store = ChatSessionStore(tmp_path / "sessions.sqlite3")

    class Runner:
        backend_url = "http://127.0.0.1:9"
        model = "test"
        reasoning_effort = "low"
        auth_configured = True

    app = create_chat_app(
        Runner(),
        session_store=store,
        identity_resolver=IdentityResolver(mode="anonymous"),
        enforce_loopback=False,
    )
    with TestClient(app) as client:
        bootstrap = client.get("/v1/session").json()
        csrf = bootstrap["csrf_token"]
        conversation_id = client.post("/v1/conversations", headers={"X-CSRF-Token": csrf}).json()["conversation_id"]
        session = store.ensure_session(
            client.cookies.get("doris_query_session"),
            client.cookies.get("doris_query_csrf"),
            app.state.identity_resolver.resolve({}),
        )
        request_id = "12345678-1234-1234-1234-123456789012"
        claim = store.claim_request(
            session,
            conversation_id,
            route="chat",
            idempotency_key="report_source_001",
            fingerprint="a" * 64,
            request_id=request_id,
        )
        source_payload = payload()
        store.complete_request(session, claim.receipt, source_payload)
        response = client.post(
            "/v1/reports",
            headers={"X-CSRF-Token": csrf},
            json={"conversation_id": conversation_id, "request_id": request_id, "formats": ["html", "pdf", "word", "excel", "csv", "png"]},
        )
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "completed"
        assert {item["format"] for item in result["files"]} == {"html", "pdf", "word", "excel", "csv", "png"}
        downloaded = client.get(result["files"][0]["url"])
        assert downloaded.status_code == 200
        assert downloaded.content

        empty = deepcopy(source_payload)
        empty["query_status"] = "empty_result"
        empty["result_state"] = "no_data"
        empty["business_table"]["rows"] = []
        empty_id = "22345678-1234-1234-1234-123456789012"
        empty_claim = store.claim_request(
            session,
            conversation_id,
            route="chat",
            idempotency_key="report_source_002",
            fingerprint="b" * 64,
            request_id=empty_id,
        )
        store.complete_request(session, empty_claim.receipt, payload=empty)
        no_report = client.post(
            "/v1/reports",
            headers={"X-CSRF-Token": csrf},
            json={"conversation_id": conversation_id, "request_id": empty_id, "formats": ["html"]},
        )
        assert no_report.status_code == 409
        assert no_report.json()["error"]["code"] == "empty_result"

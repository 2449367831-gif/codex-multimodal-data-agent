"""A zero denominator is a business state, not permission to hide bad facts."""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.agent_core.runtime import SemanticQueryRuntime
from app.agent_core.semantic_types import SemanticRequest, SemanticRuntimeError
from app.chat_api import _public_business_table, create_chat_app, IdentityResolver
from app.chat_sessions import ChatSessionStore
from app.tests.test_chat_api import FakeChatRunner
from app.tests.test_semantic_runtime import RecordingQueryClient
from app.tests.test_turnover_allocation_independence import START, END, handler, source_row, variant


def run(variant, row):
    return getattr(handler([row]), f"_run_{variant}_turnover")(
        SemanticRequest(f"{variant}_turnover", START, END))


@pytest.mark.parametrize("stock", ["100", "0"])
def test_zero_sales_returns_null_days_and_keeps_inventory(variant, stock):
    row = source_row(variant)
    prefix = "snapshot" if variant == "historical" else "inventory"
    row.update({f"{prefix}_ea": stock, "sales_ea": "0", "turnover_days": None,
                "null_sales_quantity_count": "0", "negative_sales_quantity_count": "0"})
    answer = run(variant, row)
    assert "无动销" in answer and "无法计算" in answer
    assert "周转：0天" not in answer
    table = _public_business_table(answer.business_table, live=True)
    assert table is not None
    assert table["rows"][0]["value"] == stock
    assert table["rows"][1]["value"] == "0"
    assert table["rows"][2]["value"] is None


@pytest.mark.parametrize("bad_ratio", ["0", "1", "NaN", "Infinity", "null", ""])
def test_zero_sales_requires_explicit_null_ratio(variant, bad_ratio):
    row = source_row(variant)
    row.update(sales_ea="0", turnover_days=bad_ratio,
               null_sales_quantity_count="0", negative_sales_quantity_count="0")
    with pytest.raises(SemanticRuntimeError):
        run(variant, row)


@pytest.mark.parametrize("field", ["null_sales_quantity_count", "negative_sales_quantity_count"])
@pytest.mark.parametrize("value", ["1", "-1", "0.5", None, True])
def test_invalid_sales_controls_never_become_no_movement(variant, field, value):
    row = source_row(variant)
    row.update(sales_ea="0", turnover_days=None,
               null_sales_quantity_count="0", negative_sales_quantity_count="0")
    row[field] = value
    with pytest.raises(SemanticRuntimeError):
        run(variant, row)


@pytest.mark.parametrize("field", ["turnover_days", "null_sales_quantity_count", "negative_sales_quantity_count"])
def test_missing_evidence_is_not_a_source_null(variant, field):
    row = source_row(variant)
    row.update(sales_ea="0", turnover_days=None,
               null_sales_quantity_count="0", negative_sales_quantity_count="0")
    del row[field]
    with pytest.raises(SemanticRuntimeError):
        run(variant, row)


def test_missing_inventory_does_not_become_no_movement(variant):
    row = source_row(variant)
    prefix = "snapshot" if variant == "historical" else "inventory"
    row.update({f"{prefix}_key_count": "0", "sales_ea": "0", "turnover_days": None,
                "null_sales_quantity_count": "0", "negative_sales_quantity_count": "0"})
    with pytest.raises(SemanticRuntimeError):
        run(variant, row)


@pytest.mark.parametrize("sales,ratio", [("31", None), ("-1", None)])
def test_only_zero_sales_permits_null_ratio(variant, sales, ratio):
    row = source_row(variant)
    row.update(sales_ea=sales, turnover_days=ratio,
               null_sales_quantity_count="0", negative_sales_quantity_count="0")
    with pytest.raises(SemanticRuntimeError):
        run(variant, row)


@pytest.mark.parametrize("quality_error", [False, True])
def test_chat_distinguishes_no_movement_from_bad_source_and_replays(
        variant, quality_error, tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.setattr("app.query_scope.active_snapshot", lambda _: (root, None))
    purpose = ("historical finished-goods inventory turnover" if variant == "historical"
               else "current real-time finished-goods inventory turnover")
    row = source_row(variant)
    row.update(sales_ea="0", turnover_days=None,
               null_sales_quantity_count="1" if quality_error else "0")
    query = RecordingQueryClient({purpose: [row]})
    runner = FakeChatRunner()
    runtime = SemanticQueryRuntime(query_client=query,
        clock=lambda: datetime(2026, 9, 11, 12, tzinfo=ZoneInfo("Asia/Shanghai")))
    question = "2026年8月历史库存周转" if variant == "historical" else "上月库存周转"
    assert runtime.classify(question).route == f"{variant}_turnover"
    app = create_chat_app(runner, semantic_runtime=runtime,
        session_store=ChatSessionStore(tmp_path / "sessions.sqlite3"),
        identity_resolver=IdentityResolver(mode="anonymous"), enforce_loopback=False)
    with TestClient(app) as client:
        token = client.get("/v1/session").json()["csrf_token"]
        headers = {"X-CSRF-Token": token, "Idempotency-Key": "turnover_zero_replay"}
        conversation = client.post("/v1/conversations", headers=headers).json()["conversation_id"]
        body = dict(conversation_id=conversation, question=question)
        first = client.post("/v1/chat", headers=headers, json=body)
        result = first.json()
        assert not runner.questions
        assert "scm_sales_shipment" not in first.text
        if quality_error:
            assert first.status_code >= 400
            assert not result.get("business_table")
            assert "无动销" not in first.text
        else:
            assert first.status_code == 200, result
            assert result["result_state"] == "live_success"
            assert not result["clarification"]["required"]
            assert result["business_table"]["rows"][2]["value"] is None
            assert "无动销" in first.text
            replay = client.post("/v1/chat", headers=headers, json=body)
            assert replay.json() == result
            assert len(query.calls) == 1

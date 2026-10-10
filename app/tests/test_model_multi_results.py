"""Synthetic coverage for complete, request-local model answer bundles."""

from copy import deepcopy
import json

import pytest

from app.codex_agent import _model_query_plan, _model_query_plans
from app.tests.test_model_execution_states import execute, model_host, turn
from app.tests.test_model_query_contract import business, payload, validation


def named(result_id="directory", title="仓库明细", *, empty=False):
    sql = "SELECT wms_code AS location, wms_name AS label FROM ywzt_ods.master_wms"
    if empty:
        sql += " WHERE wms_code = 'absent'"
    return {**business(sql), "result_id": result_id, "result_title": title}


def summary():
    return {"outcome": "query_plan", "kind": "business", "purpose": "summary",
            "sql": "SELECT COUNT(*) AS total FROM ywzt_ods.master_wms",
            "result_id": "summary", "result_title": "仓库汇总",
            "result_columns": [{"key": "total", "label": "仓库数量"}]}


def completed(ids=None):
    return {"outcome": "completed", "answer": "已查询仓库汇总及明细。",
            "result_ids": ["summary", "directory"] if ids is None else ids}


def test_named_business_plan_retains_display_identity():
    plan = _model_query_plan(json.dumps(named()))
    assert plan["result_id"] == "directory"
    assert plan["result_title"] == "仓库明细"


@pytest.mark.parametrize("mutation", [
    {"result_id": ""}, {"result_id": []}, {"result_id": "previous/thread"},
    {"result_id": "A"}, {"result_id": "x" * 49}, {"result_title": None},
    {"result_title": "SELECT secret FROM table"}, {"result_title": "数据库密码"},
    {"kind": "validation", "checks": validation()["checks"]},
])
def test_invalid_named_projection_is_not_a_plan(mutation):
    assert _model_query_plan(json.dumps({**named(), **mutation})) is None


def test_metadata_cannot_reserve_a_business_result():
    plan = {"outcome": "query_plan", "kind": "metadata", "sql": "SELECT 1",
            "result_id": "summary", "result_title": "仓库汇总"}
    assert _model_query_plan(json.dumps(plan)) is None


@pytest.mark.parametrize("rows", [[], [{"location": "001", "label": "测试仓"}]])
def test_summary_detail_and_validation_are_delivered_together(model_host, rows):
    thread, bridge = model_host
    thread.run.side_effect = [turn(summary()), turn(named()), turn(validation()), turn(completed())]
    bridge.query.side_effect = [payload([{"total": len(rows)}]), payload(rows, ["location", "label"]), payload([{"conflicts": 0}])]
    result = execute()
    assert result["query_error"] is None
    assert result["result_state"] == "live_success"
    tables = result["verified_business_results"]
    assert [t["result_id"] for t in tables] == ["summary", "directory"]
    assert tables[0]["rows"] == [{"total": len(rows)}]
    assert tables[1]["rows"] == rows
    assert result["verified_business_result"] is None
    assert result["query_execution"]["successful_business_query_call_count"] == 2
    assert result["query_execution"]["validation_query_call_count"] == 1
    assert all("HOST_RESULT_MANIFEST" in c.args[0] for c in thread.run.call_args_list[1:])
    assert "directory" in thread.run.call_args_list[-1].args[0]


def test_independent_business_queries_can_be_planned_as_one_batch(model_host):
    thread, bridge = model_host
    batch = {
        "outcome": "query_plan_batch",
        "plans": [summary(), named()],
    }
    thread.run.side_effect = [turn(batch), turn(completed())]
    bridge.query.side_effect = [
        payload([{"total": 1}]),
        payload([{"location": "001", "label": "测试仓"}]),
    ]
    result = execute()
    assert result["query_error"] is None
    assert result["result_state"] == "live_success"
    assert [item["result_id"] for item in result["verified_business_results"]] == [
        "summary", "directory"
    ]
    assert bridge.query.call_count == 2
    assert thread.run.call_count == 2


def test_complete_batch_skips_redundant_model_confirmation(model_host):
    thread, bridge = model_host
    batch = {
        "outcome": "query_plan_batch",
        "plans": [summary(), named(), validation()],
        "completion": {"result_ids": ["summary", "directory"]},
    }
    thread.run.side_effect = [turn(batch)]
    bridge.query.side_effect = [
        payload([{"total": 1}]),
        payload([{"location": "001", "label": "测试仓"}]),
        payload([{"conflicts": 0}]),
    ]
    result = execute()
    assert result["query_error"] is None
    assert result["interaction"]["outcome"] == "completed"
    assert [item["result_id"] for item in result["verified_business_results"]] == ["summary", "directory"]
    assert thread.run.call_count == 1
    assert bridge.query.call_count == 3


@pytest.mark.parametrize("completion", [
    {"result_ids": ["summary"]},
    {"result_ids": ["summary", "missing"]},
    {"result_ids": []},
    {"result_ids": ["summary", "directory"], "answer": "不允许"},
])
def test_completion_manifest_cannot_skip_business_tables_or_checks(completion):
    from app.codex_agent import _model_query_plans
    batch = {"outcome": "query_plan_batch", "plans": [summary(), named(), validation()],
             "completion": completion}
    assert _model_query_plans(json.dumps(batch, ensure_ascii=False)) is None


def test_completion_requires_independent_validation_plan():
    from app.codex_agent import _model_query_plans
    batch = {"outcome": "query_plan_batch", "plans": [summary(), named()],
             "completion": {"result_ids": ["summary", "directory"]}}
    assert _model_query_plans(json.dumps(batch, ensure_ascii=False)) is None


@pytest.mark.parametrize("batch", [
    {"outcome": "query_plan_batch"},
    {"outcome": "query_plan_batch", "plans": [summary(), named(), validation()],
     "completion": None},
    {"outcome": "query_plan_batch", "plans": [
        {"outcome": "query_plan", "kind": "metadata", "sql": "SELECT 1", "purpose": "probe"},
        summary(), validation()], "completion": {"result_ids": ["summary"]}},
    {"outcome": "query_plan_batch", "plans": [
        {**summary(), "result_id": None}, named(), validation()],
     "completion": {"result_ids": ["directory"]}},
    {"outcome": "query_plan_batch", "plans": [
        {**summary(), "completion": {"result_ids": ["summary"]}}, named(), validation()],
     "completion": {"result_ids": ["summary", "directory"]}},
])
def test_completion_is_strict_batch_envelope_only(batch):
    from app.codex_agent import _model_query_plans

    assert _model_query_plans(json.dumps(batch, ensure_ascii=False)) is None


@pytest.mark.parametrize("marker", ["completion", "_completion"])
def test_single_plan_cannot_carry_batch_completion_marker(marker):
    plan = {**summary(), marker: {"result_ids": ["summary"]}}
    assert _model_query_plans(json.dumps(plan, ensure_ascii=False)) is None


def test_late_clarification_does_not_publish_intermediate_business_table(model_host):
    thread, bridge = model_host
    thread.run.side_effect = [
        turn(named()),
        turn({"outcome": "clarification_required", "questions": [{
            "id": "scope", "prompt": "请选择范围", "selection": "single",
            "options": [{"id": "all", "label": "全部"}],
        }]}),
    ]
    bridge.query.return_value = payload([{"location": "001", "label": "测试仓"}])
    result = execute()
    assert result["interaction"]["outcome"] == "clarification_required"
    assert "verified_business_results" not in result
    assert bridge.query.call_count == 1


def test_batch_with_duplicate_plan_is_rejected_before_execution(model_host):
    thread, bridge = model_host
    duplicate = {"outcome": "query_plan_batch", "plans": [summary(), summary()]}
    thread.run.side_effect = [turn(duplicate), turn(duplicate)]
    result = execute()
    assert result["interaction"]["outcome"] != "completed"
    assert bridge.query.call_count == 0


def test_all_selected_empty_results_have_no_data_state(model_host):
    thread, bridge = model_host
    thread.run.side_effect = [turn(named()), turn(named("other", "其他仓库", empty=True)),
                              turn(completed(["directory", "other"]))]
    bridge.query.return_value = payload([], ["location", "label"])
    result = execute()
    assert result["result_state"] == "no_data"
    assert len(result["verified_business_results"]) == 2


@pytest.mark.parametrize("answer", ["\n".join(["测试业务说明"] * 169), "说明" * 5000,
    "SELECT private FROM ywzt_ods.private", "", None],
    ids=["many-lines", "long-answer", "private-prose", "empty-prose", "missing-prose"])
def test_verified_named_tables_survive_bad_optional_prose(model_host, answer):
    thread, bridge = model_host
    final = {"outcome": "completed", "result_ids": ["directory"]}
    if answer is not None:
        final["answer"] = answer
    thread.run.side_effect = [turn(named()), turn(final)]
    bridge.query.return_value = payload([{"location": "001", "label": "测试仓"}])
    result = execute()
    assert result["interaction"] == {"outcome": "completed", "questions": [], "answer": "查询已完成，结果见下方表格。"}
    assert json.loads(result["final_response"]) == result["interaction"]
    assert result["verified_business_results"][0]["rows"] == [{"location": "001", "label": "测试仓"}]
    assert thread.run.call_count == 2 and bridge.query.call_count == 1


@pytest.mark.parametrize("mutation", [
    {"answer": "尚待业务审批\n" * 100},
    {"answer": "candidate contract\n" * 100},
    {"answer": "测试说明\n" * 100, "questions": None},
    {"answer": "测试说明\n" * 100, "questions": [{}]},
    {"outcome": "blocked"},
    {"outcome": "failed"},
])
def test_prose_replacement_cannot_bypass_pending_or_unapproved_results(model_host, mutation):
    thread, bridge = model_host
    final = {"outcome": "completed", "result_ids": ["directory"], **mutation}
    thread.run.side_effect = [turn(named()), turn(final)]
    bridge.query.return_value = payload([{"location": "001", "label": "测试仓"}])
    result = execute()
    assert result["interaction"]["outcome"] != "completed"


def test_long_prose_without_a_verified_table_is_not_recovered(model_host):
    thread, bridge = model_host
    thread.run.return_value = turn({"outcome": "completed", "answer": "测试说明\n" * 100})
    result = execute()
    assert result["interaction"]["outcome"] != "completed"
    assert not result.get("verified_business_results")
    assert not bridge.query.called


@pytest.mark.parametrize("ids", [["summary"], ["summary", "missing"], ["summary", "summary"], [], "summary", [None]])
def test_incomplete_or_forged_manifest_never_returns_partial_answer(model_host, ids):
    thread, bridge = model_host
    thread.run.side_effect = [turn(summary()), turn(named()), turn(completed(ids)), turn(completed(ids))]
    bridge.query.side_effect = [payload([{"total": 1}]), payload([{"location": "001", "label": "测试仓"}])]
    result = execute()
    assert result["interaction"]["outcome"] == "failed"
    assert result.get("verified_business_results") is None
    assert result["verified_business_result"] is None
    assert bridge.query.call_count == 2


def test_missing_manifest_can_be_repaired_without_requerying(model_host):
    thread, bridge = model_host
    thread.run.side_effect = [turn(summary()), turn(named()),
                              turn({"outcome": "completed", "answer": "已完成"}), turn(completed())]
    bridge.query.side_effect = [payload([{"total": 1}]), payload([{"location": "001", "label": "测试仓"}])]
    result = execute()
    assert result["query_error"] is None
    assert len(result["verified_business_results"]) == 2
    assert bridge.query.call_count == 2
    assert "HOST_ANSWER_REPAIR" in thread.run.call_args_list[-1].args[0]


def test_duplicate_result_identity_stops_before_running_replacement(model_host):
    thread, bridge = model_host
    thread.run.side_effect = [turn(named()), turn(named(empty=True))]
    bridge.query.return_value = payload([{"location": "001", "label": "测试仓"}])
    result = execute()
    assert result["interaction"]["outcome"] == "failed"
    assert bridge.query.call_count == 1
    assert result.get("verified_business_results") is None


def test_failed_validation_suppresses_the_complete_bundle(model_host):
    thread, bridge = model_host
    thread.run.side_effect = [turn(summary()), turn(named()), turn(validation())]
    bridge.query.side_effect = [payload([{"total": 1}]), payload([{"location": "001", "label": "测试仓"}]), payload([{"conflicts": 1}])]
    result = execute()
    assert result["query_error"]["code"] == "query_invalid_result"
    assert result.get("verified_business_results") is None
    assert result["verified_business_result"] is None


def test_unnamed_exploration_does_not_join_the_answer_bundle(model_host):
    thread, bridge = model_host
    thread.run.side_effect = [turn(business()), turn(summary()), turn(completed(["summary"]))]
    bridge.query.side_effect = [payload([{"location": "001", "label": "测试仓"}]), payload([{"total": 1}])]
    result = execute()
    assert [t["result_id"] for t in result["verified_business_results"]] == ["summary"]


def test_result_references_cannot_resolve_another_request(model_host):
    thread, bridge = model_host
    thread.run.side_effect = [turn(summary()), turn(completed(["summary"]))]
    bridge.query.return_value = payload([{"total": 1}])
    assert execute()["query_error"] is None
    thread.run.side_effect = [turn(named()), turn(completed()), turn(completed())]
    bridge.query.return_value = payload([{"location": "001", "label": "测试仓"}])
    assert execute()["interaction"]["outcome"] == "failed"


def test_bundle_screening_is_atomic_and_values_remain_host_owned():
    from app.chat_api import ChatExecutionError, _public_model_result_tables
    from app.result_pages import ResultPageStore

    raw = [{"result_id": "directory", "result_title": "仓库明细", "columns": ["location", "label"],
            "result_columns": named()["result_columns"],
            "rows": [{"location": f"{i:05d}", "label": "测试仓"} for i in range(501)]},
           {"result_id": "summary", "result_title": "仓库汇总", "columns": ["total"],
            "result_columns": summary()["result_columns"], "rows": [{"total": 501}]}]
    store = ResultPageStore()
    broken = deepcopy(raw)
    broken[1]["rows"] = [{"total": "not numeric; SELECT password FROM secret"}]
    with pytest.raises(ChatExecutionError):
        _public_model_result_tables(broken, pages_store=store, owner="owner")
    assert not store._results
    tables = _public_model_result_tables(raw, pages_store=store, owner="owner")
    assert [t["title"] for t in tables] == ["仓库明细", "仓库汇总"]
    assert tables[1]["rows"] == [{"col_1": "501"}]
    rid = tables[0]["pagination"]["result_id"]
    assert store.page("owner", rid, 2)["rows"][0] == {"col_1": "00100", "col_2": "测试仓"}
    from app.result_pages import ResultPageError
    with pytest.raises(ResultPageError):
        store.page("other-owner", rid, 2)
    public = json.dumps(tables, ensure_ascii=False)
    assert '"location"' not in public and '"result_columns"' not in public


def test_gateway_table_identity_alias_is_normalized_before_screening():
    from app.chat_api import _public_model_result_tables
    from app.result_pages import ResultPageStore

    table = named()
    table["table_id"] = table.pop("result_id")
    table["title"] = table.pop("result_title")
    table["columns"] = ["location", "label"]
    table["rows"] = [{"location": "001", "label": "测试仓"}]
    tables = _public_model_result_tables(
        [table], pages_store=ResultPageStore(), owner="owner"
    )
    assert tables[0]["title"] == "仓库明细"


def test_bundle_has_combined_row_byte_and_table_limits(monkeypatch):
    from app.model_query_contract import BusinessResultSet
    from app.query_outcomes import DorisResultError

    collector = BusinessResultSet()
    for i in range(4):
        collector.add(named(f"table_{i}", f"仓库表{i}"), {"rows": [], "columns": []})
    with pytest.raises(DorisResultError):
        collector.check_plan(named("fifth", "更多仓库"))
    monkeypatch.setattr("app.model_query_contract.MAX_ANSWER_ROWS", 1)
    collector = BusinessResultSet()
    collector.add(summary(), {"rows": [{"total": 1}], "columns": ["total"]})
    with pytest.raises(DorisResultError):
        collector.add(named(), {"rows": [{"location": "001", "label": "测试仓"}], "columns": ["location", "label"]})
    assert len(collector.manifest()) == 1
    monkeypatch.setattr("app.model_query_contract.MAX_ANSWER_BYTES", 1)
    with pytest.raises(DorisResultError):
        BusinessResultSet().add(summary(), {"rows": [], "columns": ["total"]})


def test_multiple_page_reservations_fail_atomically_at_capacity():
    from app.result_pages import ResultPageError, ResultPageStore

    page = {"rows": [{"value": "synthetic"}]}
    size = len(json.dumps([page], ensure_ascii=False).encode("utf-8"))
    store = ResultPageStore(max_bytes=size * 3, max_owner_bytes=size)
    other = store.create("other", [page])["pagination"]["result_id"]
    with pytest.raises(ResultPageError):
        store.create_many("owner", [[page], [page]])
    assert list(store._results) == [other]
    assert store.page("other", other, 1)["rows"] == page["rows"]


@pytest.mark.parametrize("prose", ["short", "long", "wrong_number", "invalid_reference", "later_page"])
def test_model_bundle_crosses_actual_chat_boundary_and_idempotent_replay(model_host, tmp_path, monkeypatch, prose):
    from fastapi.testclient import TestClient
    from app.chat_api import create_chat_app, IdentityResolver
    from app.chat_sessions import ChatSessionStore
    from app.tests.test_chat_api import FakeChatRunner

    thread, bridge = model_host
    final = completed()
    if prose == "long":
        final["answer"] = "测试业务说明\n" * 169
    elif prose == "wrong_number":
        final["answer"] = "共有999个仓库。"
    elif prose == "invalid_reference":
        final["answer_references"] = [{"result_id": "previous_request", "row": 0}]
    elif prose == "later_page":
        final["answer_references"] = [{"result_id": "summary", "row": 0}, {"result_id": "directory", "row": 500}]
    thread.run.side_effect = [turn(summary()), turn(named()), turn(validation()), turn(final)]
    bridge.query.side_effect = [payload([{"total": 501}]),
        payload([{"location": f"{i:05d}", "label": "测试仓"} for i in range(501)]), payload([{"conflicts": 0}])]
    result = execute()
    assert "999" not in result["interaction"]["answer"]
    runner = FakeChatRunner()
    calls = []

    def run(*args, **kwargs):
        calls.append(1)
        return result

    monkeypatch.setattr(runner, "run", run)
    app = create_chat_app(runner, session_store=ChatSessionStore(tmp_path / "chat.sqlite3"),
        identity_resolver=IdentityResolver(mode="anonymous"), enforce_loopback=False)
    with TestClient(app) as client:
        session = client.get("/v1/session").json()
        body = {"question": "查询仓库汇总及明细", "conversation_id": session["conversation_id"]}
        headers = {"X-CSRF-Token": session["csrf_token"], "Idempotency-Key": "multi-result-e2e"}
        response = client.post("/v1/chat", json=body, headers=headers)
        assert response.status_code == 200
        answer = response.json()
        assert answer["status"] == "completed"
        assert [t["title"] for t in answer["business_tables"]] == ["仓库汇总", "仓库明细"]
        assert answer["business_table"] == answer["business_tables"][0]
        assert "999" not in answer["business_result"]["text"]
        assert "501" in answer["business_result"]["text"]
        if prose == "later_page":
            assert "00500" in answer["business_result"]["text"]
        page_id = answer["business_tables"][1]["pagination"]["result_id"]
        assert client.get(f"/v1/results/{page_id}/pages/2").json()["business_table"]["rows"][0]["col_1"] == "00100"
        assert client.post("/v1/chat", json=body, headers=headers).json() == answer
        assert len(calls) == 1
        assert '"result_columns"' not in response.text and '"location"' not in response.text
        assert '"conflicts"' not in response.text and "SELECT " not in response.text
        with TestClient(app) as other:
            other.get("/v1/session")
            assert other.get(f"/v1/results/{page_id}/pages/2").status_code == 404

"""Counterexamples for independent T30 public acceptance; synthetic rows only."""

from copy import deepcopy
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys

import pytest

from app.tests.test_outbound_links import links, execute

DIRECTORY = Path(__file__).resolve().parents[2] / "tests/acceptance"
sys.path.insert(0, str(DIRECTORY))
spec = spec_from_file_location("outbound_link_public_acceptance", DIRECTORY / "check_outbound_links_chat_live.py")
verifier = module_from_spec(spec)
spec.loader.exec_module(verifier)


def reference():
    return [{"cargo_owner":"A", "outbound_code":"0001", "referenced_order_code":"SO-1",
             "sales_order_code":"SO-1", "source_order_code":"HEADER-1", "detail_source_order_code":"DETAIL-1",
             "source_platform_code":"PLATFORM-1", "ref_order_type":"JYCK", "order_type":"SO07",
             "ship_at":"2026-09-11 10:00:00", "relationship_state":"matched"}]


def table(rows=None):
    rows = deepcopy(reference() if rows is None else rows)
    projected = []
    for row in rows:
        item = {public:row[private] for public, private in verifier.FIELDS.items()}
        item["relationship_state"] = {"matched":"已匹配销售订单", "unmatched":"未匹配销售订单"}[row["relationship_state"]]
        projected.append(item)
    return {"columns":[{"key":key,"kind":"text"} for key in verifier.FIELDS], "rows":projected,
            "summary":[{"label":"出库单数","value":str(len({(r['cargo_owner'],r['outbound_code']) for r in rows})),"unit":"单"}],
            "caption":"全部时间 · 全部货主 · 最新出库的一张单", "notes":["来源解释", "粒度解释", "鲜度边界"],
            "truncated":False}


def test_exact_public_fields_and_values_match():
    assert all(verifier.compare_table(table(), reference()).values())


@pytest.mark.parametrize("field", list(verifier.FIELDS))
def test_every_field_is_compared_not_just_counts(field):
    value = table()
    value["rows"][0][field] = "WRONG"
    assert not all(verifier.compare_table(value, reference()).values())


@pytest.mark.parametrize("change", [
    lambda t:t["rows"].append(deepcopy(t["rows"][0])),
    lambda t:t["rows"].clear(),
    lambda t:t["columns"].append(deepcopy(t["columns"][0])),
    lambda t:t["columns"][0].update(kind="number"),
    lambda t:t["rows"][0].update(private="SECRET"),
    lambda t:t["rows"][0].update(outbound_code=1),
    lambda t:t["summary"][0].update(value="1.5"),
    lambda t:t["summary"][0].update(value=True),
    lambda t:t["summary"][0].update(unit="EA"),
    lambda t:t.update(truncated=True),
    lambda t:t.update(pagination={"page_count":2}),
    lambda t:t.update(caption="仅货主 A"),
    lambda t:t.update(notes=[]),
])
def test_malformed_or_incomplete_public_result_cannot_pass(change):
    value = table(); change(value)
    assert not all(verifier.compare_table(value, reference()).values())


def test_multiple_detail_sources_do_not_multiply_document_count():
    rows = reference()
    rows.append({**rows[0], "detail_source_order_code":None})
    assert all(verifier.compare_table(table(rows), rows).values())
    value = table(rows); value["summary"][0]["value"] = "2"
    assert not verifier.compare_table(value, rows)["document_count"]


def test_owner_is_part_of_identity_and_missing_orders_are_retained():
    rows = reference()
    rows.append({**rows[0], "cargo_owner":"B", "sales_order_code":None, "source_order_code":None,
                 "detail_source_order_code":None, "source_platform_code":None, "order_type":None,
                 "relationship_state":"unmatched"})
    assert all(verifier.compare_table(table(rows), rows).values())
    value = table(rows); value["rows"].pop()
    assert not all(verifier.compare_table(value, rows).values())


def test_matching_bad_reference_is_not_evidence():
    rows = reference() * 2
    assert not verifier.compare_table(table(rows), rows)["unique_complete_links"]


def test_candidate_preflight_does_not_call_chat_or_database():
    def forbidden(*args, **kwargs):
        raise AssertionError("unpublished acceptance must have no external calls")
    result = verifier.verify(published=False, request=forbidden, query=forbidden, question="original")
    assert result == {"case":"T30_chat", "state":"not_verified", "reason":"candidate_not_published"}


def test_original_question_is_exact_requirement():
    question = verifier.acceptance_question()
    assert question.startswith("查询一张出库单的关联链路")
    assert "订单来源编码" in question and "哪张表" in question


def test_latest_selection_has_no_hidden_date_or_owner_limit():
    sql = verifier.latest_reference_sql()
    assert "LIMIT 1" in sql
    assert "2026" not in sql and "HJXH" not in sql
    assert "ship_at DESC" in sql and "cargo_owner ASC" in sql and "outbound_code ASC" in sql


def test_reference_snapshot_rejects_latest_identity_movement():
    calls = 0
    def query(sql):
        nonlocal calls
        calls += 1
        if calls == 1: return [{"cargo_owner":"A","outbound_code":"0001","ship_at":"2026-09-11 10:00:00"}]
        if calls == 5: return [{"cargo_owner":"B","outbound_code":"0002","ship_at":"2026-09-11 10:00:01"}]
        return []
    with pytest.raises(ValueError, match="reference_latest_changed"):
        verifier.read_reference(query)


def test_independent_reference_keeps_latest_ties_and_all_source_codes(links):
    from app.agent_core.outbound_links import OutboundLinkPlan
    identity, rows = verifier.read_reference(lambda sql:[dict(row) for row in links.execute(sql)])
    assert identity[:2] == ("A", "END")
    assert {r["detail_source_order_code"] for r in rows} == {"LINE-A1", "LINE-A2"}
    compiled = execute(links, OutboundLinkPlan(latest=True))
    assert {verifier.signature(r) for r in compiled} == {verifier.signature(r) for r in rows}


@pytest.mark.parametrize("mutation", [
    "UPDATE ywzt_ods.scm_sales_shipment_header SET delete_flag=7 WHERE outbound_code='END'",
    "INSERT INTO ywzt_ods.scm_sales_shipment_header VALUES ('A','END','CONFLICT','SO07','2026-09-10 01:00:00',0,'false')",
    "UPDATE ywzt_ods.scm_sales_order_detail SET _fdl_marked_deleted='invalid' WHERE source_order_code='LINE-A1'",
    "UPDATE ywzt_ods.scm_sales_shipment_header SET _fdl_marked_deleted='' WHERE outbound_code='END'",
])
def test_independent_reference_rejects_ambiguous_source_evidence(links, mutation):
    links.execute(mutation)
    with pytest.raises(ValueError):
        verifier.read_reference(lambda sql:[dict(row) for row in links.execute(sql)])


def test_unrelated_owner_conflict_cannot_contaminate_latest_reference(links):
    links.execute("INSERT INTO ywzt_ods.scm_sales_shipment_header VALUES ('B','END','OTHER','SO07','2026-09-10 01:00:00',7,'false')")
    _, rows = verifier.read_reference(lambda sql:[dict(row) for row in links.execute(sql)])
    assert {r["cargo_owner"] for r in rows} == {"A"}


@pytest.mark.parametrize("mutation,expected", [
    (None, "verified"),
    ("source", "failed"),
    ("receipt", "idempotent_result_changed"),
    ("pending", "existing_user_clarification"),
])
def test_real_asgi_task_workflow_and_independent_verifier(links, tmp_path, monkeypatch, mutation, expected):
    from fastapi.testclient import TestClient
    from app.agent_core.outbound_links import OutboundLinkPlan
    from app.agent_core.runtime import SemanticQueryRuntime
    from app.chat_api import create_chat_app, IdentityResolver
    from app.chat_sessions import ChatSessionStore
    from app.tests.test_chat_api import FakeChatRunner

    monkeypatch.setattr("app.agent_core.outbound_link_planning.active_link_contract", lambda _:True)
    records = execute(links, OutboundLinkPlan(latest=True))
    class Query:
        calls = 0
        def query(self, **kwargs):
            self.calls += 1
            return {"status":"succeeded", "execution":{"executor_name":"doris", "is_mock":False,
                    "rows":deepcopy(records), "row_count":len(records), "truncated":False}}
    class Runner(FakeChatRunner):
        def run(self, *args, **kwargs):
            raise AssertionError("T30 verified route cannot call model")
    query = Query()
    app = create_chat_app(Runner(), semantic_runtime=SemanticQueryRuntime(query_client=query),
                          session_store=ChatSessionStore(tmp_path / "sessions.sqlite3"),
                          identity_resolver=IdentityResolver(mode="anonymous"), enforce_loopback=False)
    posts = {}
    with TestClient(app) as client:
        def request(path, body=None, headers=None):
            response = client.get(path) if body is None else client.post(path, json=body, headers=headers)
            assert response.status_code < 400
            payload = response.json()
            if path == "/v1/session" and mutation == "pending":
                payload["clarification_pending"] = True
            if body is not None:
                key = headers["Idempotency-Key"]
                posts[key] = posts.get(key, 0) + 1
                if mutation == "receipt" and posts[key] > 1:
                    payload["task_id"] = "wrong-receipt"
            return payload
        def independent(sql):
            rows = [dict(row) for row in links.execute(sql)]
            if mutation == "source" and "o.source_order_code" in sql:
                for row in rows: row["source_order_code"] = "WRONG"
            return rows
        if expected == "idempotent_result_changed":
            with pytest.raises(ValueError, match=expected):
                verifier.verify(published=True, request=request, query=independent, question=verifier.acceptance_question(), pause=lambda _:None)
        else:
            result = verifier.verify(published=True, request=request, query=independent, question=verifier.acceptance_question(), pause=lambda _:None)
            assert result.get("reason", result["state"]) == expected, result
            assert query.calls == (0 if mutation == "pending" else 2)
            if mutation is None:
                assert len(posts) == 2 and set(posts.values()) == {2}


@pytest.mark.parametrize("status", [404, 410, 403])
def test_terminal_observation_failure_is_not_retried_forever(status):
    import urllib.error
    observations = 0
    def request(path, body=None, headers=None):
        nonlocal observations
        if path == "/v1/session": return {"conversation_id":"c-test", "csrf_token":"token"}
        if body is not None: return {"task_id":"task-test", "state":"running"}
        observations += 1
        if observations > 2: raise AssertionError("terminal task observation was retried")
        raise urllib.error.HTTPError("http://127.0.0.1:8021"+path, status, "unavailable", {}, None)
    with pytest.raises(urllib.error.HTTPError):
        verifier.verify(published=True, request=request, query=None, question="original", pause=lambda _:None)
    assert observations == 1


@pytest.mark.parametrize("failure", ["timeout", "unavailable"])
def test_transient_observation_failure_reuses_same_task_without_reposting(failure):
    import urllib.error
    posts, gets = [], []
    class FinishedObservation(Exception):
        pass
    def request(path, body=None, headers=None):
        if path == "/v1/session": return {"conversation_id":"c-test", "csrf_token":"token"}
        if body is not None:
            posts.append(body)
            return {"task_id":"task-original", "state":"running"}
        gets.append(path)
        if len(gets) == 1:
            if failure == "timeout": raise TimeoutError()
            raise urllib.error.HTTPError("http://127.0.0.1:8021"+path, 503, "unavailable", {}, None)
        raise FinishedObservation()
    with pytest.raises(FinishedObservation):
        verifier.verify(published=True, request=request, query=None, question="original", pause=lambda _:None)
    assert len(posts) == 1 and gets == ["/v1/tasks/task-original"] * 2

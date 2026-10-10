"""Presentation pages are owner-bound, immutable, expiring and memory-only."""

import pytest

from app.result_pages import ResultPageStore, ResultPageError


def table(n):
    return {"format": "table", "title": "Synthetic", "rows": [{"id": str(n)}]}


def test_pages_cannot_cross_owners_and_are_not_caller_mutable():
    store = ResultPageStore()
    first = store.create("A", [table(1), table(2)])
    rid = first["pagination"]["result_id"]
    assert first["pagination"]["total_rows"] == 2
    first["rows"][0]["id"] = "changed"
    assert store.page("A", rid, 1)["rows"][0]["id"] == "1"
    assert store.page("A", rid, 2)["rows"][0]["id"] == "2"
    with pytest.raises(ResultPageError) as e: store.page("B", rid, 1)
    assert e.value.http_status == 404


def test_expiration_restart_and_unknown_id_never_return_some_other_result():
    now = [1]
    store = ResultPageStore(ttl_seconds=60, clock=lambda: now[0])
    first = store.create("A", [table(1)])
    rid = first["pagination"]["result_id"]
    now[0] = 62
    with pytest.raises(ResultPageError) as e: store.page("A", rid, 1)
    assert e.value.http_status == 410
    with pytest.raises(ResultPageError): ResultPageStore().page("A", rid, 1)


@pytest.mark.parametrize("page", [0, -1, True, 3, "1"])
def test_invalid_page_bounds(page):
    store = ResultPageStore()
    rid = store.create("A", [table(1), table(2)])["pagination"]["result_id"]
    with pytest.raises(ResultPageError): store.page("A", rid, page)


def test_capacity_exhaustion_is_explicit_and_does_not_evict_other_users():
    store = ResultPageStore(max_bytes=200, max_owner_bytes=200)
    rid = store.create("A", [table(1)])["pagination"]["result_id"]
    with pytest.raises(ResultPageError): store.create("B", [table(2)] * 10)
    assert store.page("A", rid, 1)["rows"][0]["id"] == "1"

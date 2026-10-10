from datetime import date, datetime
from decimal import Decimal

import pytest

from app.agent_core.sales_drilldown import SalesDrilldownPlan, SalesRankingSnapshot, RankedSku
from app.ranking_contexts import RankingContextStore, RankingContextError


def snapshot():
    return SalesRankingSnapshot(SalesDrilldownPlan(date(2026, 9, 9), date(2026, 9, 10)),
        (RankedSku(1, 'A', 'Product A', Decimal('1.25'), 1),), Decimal('1.25'), 1, Decimal('1.25'), 1, 1,
        datetime(2026, 9, 9, 12))


def test_context_is_owner_bound_and_only_returns_opaque_metadata():
    store = RankingContextStore(clock=lambda: 1000)
    original = snapshot()
    token = store.create('owner-a', original)
    assert set(token) == {'result_id', 'expires_at'}
    assert store.get('owner-a', token['result_id'], 'A') is original
    with pytest.raises(RankingContextError) as wrong_owner:
        store.get('owner-b', token['result_id'], 'A')
    assert wrong_owner.value.http_status == 404
    with pytest.raises(RankingContextError) as wrong_sku:
        store.get('owner-a', token['result_id'], 'B')
    assert wrong_sku.value.http_status == 400


def test_expiry_restart_and_owner_capacity_do_not_evict_other_owners():
    now = [1000]
    store = RankingContextStore(clock=lambda: now[0], ttl_seconds=10, max_owner_entries=1)
    first = store.create('a', snapshot())
    other = store.create('b', snapshot())
    with pytest.raises(RankingContextError) as capacity:
        store.create('a', snapshot())
    assert capacity.value.http_status == 503
    assert store.get('b', other['result_id'], 'A')
    with pytest.raises(RankingContextError):
        RankingContextStore().get('a', first['result_id'], 'A')
    now[0] = 1010
    with pytest.raises(RankingContextError) as expired:
        store.get('a', first['result_id'], 'A')
    assert expired.value.http_status == 410
    assert store.create('a', snapshot())['result_id'] != first['result_id']


@pytest.mark.parametrize('bad', [None, {}, {'plan': 'fake'}])
def test_untyped_context_is_not_cached(bad):
    with pytest.raises(ValueError):
        RankingContextStore().create('a', bad)


def test_cache_has_byte_budget_and_rejects_without_partial_insert():
    store = RankingContextStore(max_bytes=1)
    with pytest.raises(RankingContextError):
        store.create('a', snapshot())
    assert not store._results

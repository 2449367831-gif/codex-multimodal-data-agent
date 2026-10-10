from datetime import date, datetime
from decimal import Decimal
from fractions import Fraction

import pytest

from app.agent_core.sales_detail_batches import SalesDetailCursor, SalesDetailBatch
from app.agent_core.sales_detail_partitions import SalesDetailPartitionPlan, SalesDetailSummary
from app.sales_detail_contexts import SalesDetailContextStore, DetailContextError


def cursor():
    return SalesDetailCursor(SalesDetailPartitionPlan(date(2026, 9, 1), date(2026, 9, 2)),
        SalesDetailSummary(Decimal('4'), 2, 4, datetime(2026, 9, 1, 12)), 'a'*64)


def test_owner_and_backend_conversation_both_bind_the_continuation():
    store = SalesDetailContextStore()
    initial = cursor()
    meta = store.create('owner', 'conversation', initial)
    for owner, conversation in [('other', 'conversation'), ('owner', 'other')]:
        with pytest.raises(DetailContextError) as error:
            store.lookup(owner, conversation, meta['result_id'], 1)
        assert error.value.http_status == 404
    assert store.lookup('owner', 'conversation', meta['result_id'], 1) == (initial, None)
    assert set(meta) == {'result_id', 'next_batch', 'loaded_rows', 'total_rows', 'complete', 'expires_at'}


def test_preview_does_not_commit_and_task_receipt_advances_only_once():
    store = SalesDetailContextStore()
    initial = cursor()
    meta = store.create('owner', 'conversation', initial)
    following = SalesDetailCursor(initial.plan, initial.baseline, initial.definition,
        ('A', 'O1', 'B'), 2, 1, Fraction(2), 2)
    batch = SalesDetailBatch(initial, following, ())
    assert store.preview('owner', 'conversation', meta['result_id'], batch)['loaded_rows'] == 2
    assert store.lookup('owner', 'conversation', meta['result_id'], 1) == (initial, None)
    store.commit('owner', 'conversation', meta['result_id'], batch, 'task_'+'a'*32)
    assert store.lookup('owner', 'conversation', meta['result_id'], 1) == (None, 'task_'+'a'*32)
    assert store.lookup('owner', 'conversation', meta['result_id'], 2) == (following, None)
    with pytest.raises(ValueError):
        store.commit('owner', 'conversation', meta['result_id'], batch, 'task_'+'b'*32)


def test_capacity_and_expiration_do_not_create_unbounded_contexts():
    now = [100]
    store = SalesDetailContextStore(max_owner_entries=1, ttl_seconds=10, clock=lambda: now[0])
    meta = store.create('owner', 'conversation', cursor())
    with pytest.raises(DetailContextError) as error:
        store.create('owner', 'conversation', cursor())
    assert error.value.http_status == 503
    now[0] = 111
    with pytest.raises(DetailContextError) as error:
        store.lookup('owner', 'conversation', meta['result_id'], 1)
    assert error.value.http_status == 410
    assert store.create('owner', 'conversation', cursor())['result_id'] != meta['result_id']


def test_service_restart_cannot_execute_a_browser_cached_cursor():
    meta = SalesDetailContextStore().create('owner', 'conversation', cursor())
    with pytest.raises(DetailContextError) as error:
        SalesDetailContextStore().lookup('owner', 'conversation', meta['result_id'], 1)
    assert error.value.http_status == 404

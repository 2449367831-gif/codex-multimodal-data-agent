"""Owner-bound continuation plans and task receipts; no business rows on disk."""

from dataclasses import asdict, dataclass, field
import json
import math
import re
import secrets
import threading
import time

from .agent_core.sales_detail_batches import SalesDetailBatch, SalesDetailCursor
from .errors import RuntimeErrorBase


DETAIL_CONTEXT_ID = re.compile(r'db_[A-Za-z0-9_-]{32}\Z')


class DetailContextError(RuntimeErrorBase):
    code = 'detail_continuation_unavailable'
    http_status = 410


@dataclass
class _Context:
    owner: str
    conversation: str
    cursor: SalesDetailCursor
    expires: float
    size: int
    receipts: dict[int, str] = field(default_factory=dict)


class SalesDetailContextStore:
    def __init__(self, *, ttl_seconds=3600, max_bytes=16*1024*1024, max_owner_entries=32, clock=time.time):
        self.ttl, self.max_bytes, self.max_owner_entries, self.clock = ttl_seconds, max_bytes, max_owner_entries, clock
        self._contexts = {}
        self._lock = threading.Lock()

    @staticmethod
    def _metadata(identifier, item, cursor=None):
        cursor = cursor or item.cursor
        return {'result_id': identifier, 'next_batch': cursor.batch_index,
                'loaded_rows': cursor.loaded, 'total_rows': cursor.baseline.detail_count,
                'complete': cursor.complete, 'expires_at': math.floor(item.expires)}

    def create(self, owner, conversation, cursor):
        if (not owner or not conversation or type(cursor) is not SalesDetailCursor
                or cursor.loaded or cursor.complete or cursor.baseline.detail_count > 2**53-1):
            raise ValueError('initial checked continuation required')
        size = len(json.dumps(asdict(cursor), default=str, ensure_ascii=False).encode())
        with self._lock:
            now = self.clock()
            self._contexts = {key: item for key, item in self._contexts.items() if item.expires > now}
            if (sum(item.size for item in self._contexts.values()) + size > self.max_bytes
                or sum(item.owner == owner for item in self._contexts.values()) >= self.max_owner_entries):
                error = DetailContextError('分批任务容量不足，请稍后重新查询')
                error.http_status = 503
                raise error
            identifier = 'db_' + secrets.token_urlsafe(24)
            item = _Context(owner, conversation, cursor, now + self.ttl, size)
            self._contexts[identifier] = item
            return self._metadata(identifier, item)

    def _get(self, owner, conversation, identifier):
        item = self._contexts.get(identifier) if isinstance(identifier, str) and DETAIL_CONTEXT_ID.fullmatch(identifier) else None
        if item is None or item.owner != owner or item.conversation != conversation:
            error = DetailContextError('分批任务不存在或已失效，请重新查询')
            error.http_status = 404
            raise error
        if item.expires <= self.clock():
            del self._contexts[identifier]
            raise DetailContextError('分批任务已过期，请重新查询')
        return item

    def lookup(self, owner, conversation, identifier, index):
        with self._lock:
            item = self._get(owner, conversation, identifier)
            if type(index) is not int or index < 1:
                raise ValueError('invalid batch index')
            if index in item.receipts:
                return None, item.receipts[index]
            if index != item.cursor.batch_index or item.cursor.complete:
                error = DetailContextError('批次顺序已变化，请刷新后继续')
                error.http_status = 409
                raise error
            return item.cursor, None

    def preview(self, owner, conversation, identifier, batch):
        with self._lock:
            item = self._get(owner, conversation, identifier)
            if type(batch) is not SalesDetailBatch or item.cursor != batch.source:
                raise ValueError('batch does not match continuation')
            return self._metadata(identifier, item, batch.cursor)

    def commit(self, owner, conversation, identifier, batch, task_id):
        with self._lock:
            item = self._get(owner, conversation, identifier)
            if (type(batch) is not SalesDetailBatch or item.cursor != batch.source
                    or not isinstance(task_id, str) or not re.fullmatch(r'task_[a-f0-9]{32}', task_id)):
                raise ValueError('batch commit does not match continuation')
            # Receipts keep only task IDs. The existing task store owns public responses.
            if sum(value.size for value in self._contexts.values()) + 96 > self.max_bytes:
                error = DetailContextError('分批任务容量不足，进度未推进')
                error.http_status = 503
                raise error
            item.receipts[batch.source.batch_index] = task_id
            item.cursor = batch.cursor
            item.size += 96

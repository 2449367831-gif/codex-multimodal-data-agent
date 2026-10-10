"""Ephemeral owner-bound ranking context; never accepts a browser query plan."""

from dataclasses import asdict, dataclass
import json
import math
import re
import secrets
import threading
import time

from .agent_core.sales_drilldown import SalesRankingSnapshot
from .errors import RuntimeErrorBase


RANKING_ID = re.compile(r'rr_[A-Za-z0-9_-]{32}\Z')


class RankingContextError(RuntimeErrorBase):
    code = 'ranking_unavailable'
    http_status = 410


@dataclass(frozen=True, slots=True)
class _Ranking:
    owner: str
    snapshot: SalesRankingSnapshot
    expires: float
    size: int


class RankingContextStore:
    def __init__(self, *, ttl_seconds=3600, max_bytes=16 * 1024 * 1024,
                 max_owner_entries=32, clock=time.time):
        self.ttl = ttl_seconds
        self.max_bytes = max_bytes
        self.max_owner_entries = max_owner_entries
        self.clock = clock
        self._results: dict[str, _Ranking] = {}
        self._lock = threading.Lock()

    def create(self, owner, snapshot):
        if not isinstance(owner, str) or not owner or type(snapshot) is not SalesRankingSnapshot or not snapshot.ranking:
            raise ValueError('Nonempty checked owner-bound ranking required')
        size = len(json.dumps(asdict(snapshot), default=str, ensure_ascii=False).encode('utf-8'))
        with self._lock:
            now = self.clock()
            self._results = {key: item for key, item in self._results.items() if item.expires > now}
            if (sum(item.size for item in self._results.values()) + size > self.max_bytes
                    or sum(item.owner == owner for item in self._results.values()) >= self.max_owner_entries):
                error = RankingContextError('排名缓存容量不足，请稍后重新查询')
                error.http_status = 503
                raise error
            result_id = 'rr_' + secrets.token_urlsafe(24)
            expires = now + self.ttl
            self._results[result_id] = _Ranking(owner, snapshot, expires, size)
            return {'result_id': result_id, 'expires_at': math.floor(expires)}

    def get(self, owner, result_id, sku_code):
        with self._lock:
            item = self._results.get(result_id) if isinstance(result_id, str) and RANKING_ID.fullmatch(result_id) else None
            if item is None or item.owner != owner:
                error = RankingContextError('排名不存在或已失效，请重新查询排名')
                error.http_status = 404
                raise error
            if item.expires <= self.clock():
                del self._results[result_id]
                raise RankingContextError('排名已过期，请重新查询排名')
            if not isinstance(sku_code, str) or sku_code not in {row.sku_code for row in item.snapshot.ranking}:
                error = RankingContextError('所选SKU不在本次排名中')
                error.http_status = 400
                raise error
            return item.snapshot

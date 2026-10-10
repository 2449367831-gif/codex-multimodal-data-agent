"""Owner-scoped memory cache for screened, completely reconciled result pages."""

from copy import deepcopy
from dataclasses import dataclass
import json
import math
import re
import secrets
import threading
import time

from .errors import RuntimeErrorBase


RESULT_ID = re.compile(r"rp_[A-Za-z0-9_-]{32}\Z")


class ResultPageError(RuntimeErrorBase):
    code = "result_page_unavailable"
    http_status = 410


@dataclass
class _Result:
    owner: str
    expires: float
    pages: list[dict]
    size: int
    total: int


class ResultPageStore:
    def __init__(self, *, ttl_seconds=3600, max_bytes=128 * 1024 * 1024, max_owner_bytes=64 * 1024 * 1024, clock=time.time):
        self.ttl = ttl_seconds
        self.max_bytes = max_bytes
        self.max_owner_bytes = max_owner_bytes
        self.clock = clock
        self._results: dict[str, _Result] = {}
        self._lock = threading.Lock()

    def create(self, owner: str, pages: list[dict]) -> dict:
        return self.create_many(owner, [pages])[0]

    def create_many(self, owner: str, groups: list[list[dict]]) -> list[dict]:
        """Reserve a whole answer atomically; a capacity error retains no part."""
        if (not owner or not groups or any(not pages or any(not isinstance(p.get("rows"), list)
                for p in pages) for pages in groups)):
            raise ValueError("Invalid screened result pages")
        copied = deepcopy(groups)
        sizes = [len(json.dumps(pages, ensure_ascii=False).encode("utf-8")) for pages in copied]
        size = sum(sizes)
        with self._lock:
            self._results = {key: result for key, result in self._results.items() if result.expires > self.clock()}
            total_size = sum(r.size for r in self._results.values())
            owner_size = sum(r.size for r in self._results.values() if r.owner == owner)
            if total_size + size > self.max_bytes or owner_size + size > self.max_owner_bytes:
                error = ResultPageError("结果缓存容量不足，请稍后重新查询")
                error.http_status = 503
                raise error
            created = []
            for pages, page_size in zip(copied, sizes, strict=True):
                rid = "rp_" + secrets.token_urlsafe(24)
                result = _Result(owner, self.clock() + self.ttl, pages, page_size, sum(len(p["rows"]) for p in pages))
                self._results[rid] = result
                created.append(self._public(rid, result, 1))
            return created

    def page(self, owner: str, result_id: str, page: int) -> dict:
        with self._lock:
            result = self._results.get(result_id) if isinstance(result_id, str) and RESULT_ID.fullmatch(result_id) else None
            if result is None or result.owner != owner:
                error = ResultPageError("结果不存在或已失效，请重新查询")
                error.http_status = 404
                raise error
            if result.expires <= self.clock():
                del self._results[result_id]
                raise ResultPageError("结果已过期，请重新查询")
            if type(page) is not int or not 1 <= page <= len(result.pages):
                error = ResultPageError("结果页码无效")
                error.http_status = 400
                raise error
            return self._public(result_id, result, page)

    @staticmethod
    def _public(rid, result, page):
        value = deepcopy(result.pages[page - 1])
        start = sum(len(p["rows"]) for p in result.pages[:page - 1])
        value["pagination"] = {
            "result_id": rid, "page": page, "page_count": len(result.pages),
            "total_rows": result.total, "start_row": start + 1 if result.total else 0,
            "end_row": start + len(value["rows"]), "expires_at": math.floor(result.expires),
            "reconciled": True,
        }
        return value

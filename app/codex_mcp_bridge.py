"""Host-side MCP bridge for model gateways that omit MCP tool calls."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack, asynccontextmanager, contextmanager
import math
import os
from pathlib import Path
from threading import get_ident
from typing import Any, Mapping

from anyio.from_thread import start_blocking_portal
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from .query_outcomes import (
    DorisResultError,
    normalize_doris_tool_result,
    validate_doris_result,
)


MAX_PAGE_CONCURRENCY = 4


class CodexMcpBridgeError(DorisResultError):
    """The host could not complete the single allowed Doris tool call."""


def _payload_from_result(result: Any) -> dict[str, Any]:
    normalized = normalize_doris_tool_result(result)
    if normalized.payload is None:
        if normalized.state == "query_unavailable":
            raise CodexMcpBridgeError("backend_unavailable")
        if normalized.state == "query_rejected":
            raise CodexMcpBridgeError("backend_rejected")
        raise CodexMcpBridgeError(
            normalized.reason or "execution_missing",
            missing_fields=normalized.missing_fields,
            repairable=normalized.state == "query_failed",
        )
    return dict(normalized.payload)


def _verify_live_payload(payload: Mapping[str, Any]) -> None:
    try:
        validate_doris_result(payload)
    except DorisResultError as exc:
        raise CodexMcpBridgeError(
            exc.reason,
            diagnostic=exc.diagnostic,
            missing_fields=exc.missing_fields,
            repairable=exc.reason == "query_failed",
        ) from exc


def _bridge_failure(exc: BaseException) -> CodexMcpBridgeError:
    if isinstance(exc, CodexMcpBridgeError):
        return exc
    if isinstance(exc, TimeoutError):
        return CodexMcpBridgeError('query_timeout')
    if isinstance(exc, BaseExceptionGroup):
        reasons = {_bridge_failure(child).reason for child in exc.exceptions}
        if len(reasons) == 1:
            return CodexMcpBridgeError(reasons.pop())
    return CodexMcpBridgeError('backend_unavailable')


class _RequestQuery:
    """Thread-affine handle; never shared or cached across requests."""

    def __init__(self, bridge, portal, session):
        self._bridge, self._portal, self._session = bridge, portal, session
        self._owner_thread = get_ident()
        self._active = True

    def query(self, *, sql: str, purpose: str) -> dict[str, Any]:
        if not self._active or get_ident() != self._owner_thread:
            raise CodexMcpBridgeError('backend_unavailable')
        try:
            return self._portal.call(self._bridge._call_session, self._session, sql, purpose)
        except CodexMcpBridgeError:
            self._active = False
            raise
        except Exception as exc:
            self._active = False
            raise _bridge_failure(exc) from exc

    def close(self):
        self._active = False
        self._session = self._portal = self._bridge = None

    def query_many(self, *, statements, purpose: str, timeout_seconds: float):
        if not self._active or get_ident() != self._owner_thread:
            raise CodexMcpBridgeError('backend_unavailable')
        if (
            not isinstance(statements, (tuple, list)) or not 1 <= len(statements) <= MAX_PAGE_CONCURRENCY
            or any(not isinstance(sql, str) or not sql.strip() for sql in statements)
            or isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds) or timeout_seconds <= 0
        ):
            raise ValueError('invalid bounded query batch')
        try:
            return self._portal.call(
                self._bridge._call_many, self._session, tuple(statements), purpose, timeout_seconds,
            )
        except Exception as exc:
            self._active = False
            raise _bridge_failure(exc) from exc


class _QuerySequence:
    """Lazy request-local reuse; a failed call is never replayed implicitly."""

    def __init__(self, bridge):
        self._bridge = bridge
        self._owner_thread = get_ident()
        self._active = True
        self._stack = ExitStack()
        self._client = None

    def _ensure_client(self):
        if self._client is None:
            self._client = self._stack.enter_context(self._bridge.request_session())
        return self._client

    def _discard(self, error=None):
        stack, self._stack = self._stack, ExitStack()
        self._client = None
        try:
            stack.close()
        except Exception:
            if error is None:
                raise

    def query(self, *, sql: str, purpose: str):
        if not self._active or get_ident() != self._owner_thread:
            raise CodexMcpBridgeError('backend_unavailable')
        try:
            return self._ensure_client().query(sql=sql, purpose=purpose)
        except BaseException as exc:
            # Release a poisoned transport immediately, before model repair.
            # The host still decides whether any subsequent plan may execute.
            self._discard(exc)
            raise

    def close(self, error=None):
        self._active = False
        self._bridge = None
        self._discard(error)


class CodexMcpBridge:
    """Call the repository's only MCP tool without exposing the SQL publicly."""

    def __init__(
        self,
        *,
        project_root: Path,
        backend_url: str,
        python_executable: Path | None = None,
        timeout_seconds: float = 95.0,
    ) -> None:
        self.project_root = project_root.resolve()
        self.backend_url = backend_url
        self.python_executable = (python_executable or Path(os.sys.executable)).resolve()
        self.timeout_seconds = timeout_seconds

    @asynccontextmanager
    async def _open_session(self):
        server = StdioServerParameters(
            command=str(self.python_executable),
            args=["-I", "-s", "-m", "app.mcp_server"],
            cwd=str(self.project_root),
            env={
                "AGENT_QUERY_BACKEND_URL": self.backend_url,
                "PYTHONIOENCODING": "utf-8",
            },
        )
        async with stdio_client(server) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                async with asyncio.timeout(self.timeout_seconds):
                    await session.initialize()
                yield session

    async def _call_session(self, session, sql: str, purpose: str) -> dict[str, Any]:
        async with asyncio.timeout(self.timeout_seconds):
            result = await session.call_tool(
                "query_doris_sql",
                {"sql": sql, "purpose": purpose},
                read_timeout_seconds=self.timeout_seconds,
            )
        payload = _payload_from_result(result)
        _verify_live_payload(payload)
        return payload

    async def _query_async(self, sql: str, purpose: str) -> dict[str, Any]:
        async with self._open_session() as session:
            return await self._call_session(session, sql, purpose)

    async def _call_many(self, session, statements, purpose, timeout_seconds):
        # TaskGroup cancels and joins sibling calls before a failed batch exits.
        # Results remain in page order even if requests complete out of order.
        async with asyncio.timeout(min(self.timeout_seconds, timeout_seconds)):
            async with asyncio.TaskGroup() as group:
                tasks = [group.create_task(self._call_session(session, sql, purpose)) for sql in statements]
        return [task.result() for task in tasks]

    @contextmanager
    def request_session(self):
        # AnyIO enters and exits the async MCP contexts in the same task. Each
        # request owns its portal and child process, even when the bridge is shared.
        stack = ExitStack()
        scoped = None
        body_error = None
        try:
            try:
                portal = stack.enter_context(start_blocking_portal(name='doris-request-mcp'))
                session = stack.enter_context(portal.wrap_async_context_manager(self._open_session()))
                scoped = _RequestQuery(self, portal, session)
            except Exception as exc:
                raise _bridge_failure(exc) from exc
            yield scoped
        except BaseException as exc:
            body_error = exc
            raise
        finally:
            if scoped is not None:
                scoped.close()
            try:
                stack.close()
            except Exception as exc:
                if body_error is None:
                    raise _bridge_failure(exc) from exc

    @contextmanager
    def query_sequence(self):
        sequence = _QuerySequence(self)
        body_error = None
        try:
            yield sequence
        except BaseException as exc:
            body_error = exc
            raise
        finally:
            sequence.close(body_error)

    def query(self, *, sql: str, purpose: str) -> dict[str, Any]:
        try:
            return asyncio.run(self._query_async(sql, purpose))
        except CodexMcpBridgeError:
            raise
        except Exception as exc:
            raise _bridge_failure(exc) from exc

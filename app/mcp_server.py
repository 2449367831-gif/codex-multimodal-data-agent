from __future__ import annotations

import os
import time
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .query_outcomes import DorisResultError, backend_error


SERVER_INSTRUCTIONS = (
    "This server has one capability: run model-generated read-only Doris SQL through "
    "query_doris_sql. Submit exactly one SELECT or WITH ... SELECT statement and fully "
    "qualify physical tables. When columns or tables are uncertain, query "
    "information_schema first. Never submit DDL, DML, DCL, OUTFILE, locks, external table "
    "functions, or dangerous functions. Treat backend errors as failures, preserve each "
    "request's business scope, and disclose when results are truncated."
)

QUERY_TOOL_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)
HTTP_QUERY_MAX_ATTEMPTS = 2
HTTP_QUERY_RETRY_BACKOFF_SECONDS = 0.25
TRANSIENT_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


class QueryBackend(Protocol):
    def query(self, *, sql: str, purpose: str | None = None) -> dict[str, Any]: ...


class HttpQueryBackend:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 95.0,
        max_response_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("AGENT_QUERY_BACKEND_URL must use HTTP on a loopback host")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes

    def _query_once(self, payload: dict[str, Any]) -> tuple[int, bytes]:
        with httpx.Client(
            timeout=self.timeout_seconds,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            with client.stream(
                "POST",
                f"{self.base_url}/v1/sql",
                json=payload,
            ) as response:
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > self.max_response_bytes:
                        raise DorisResultError("result_truncated")
                    chunks.append(chunk)
                return response.status_code, b"".join(chunks)

    def query(self, *, sql: str, purpose: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"sql": sql}
        if purpose is not None:
            payload["purpose"] = purpose
        status_code: int | None = None
        body = b""
        for attempt in range(HTTP_QUERY_MAX_ATTEMPTS):
            try:
                status_code, body = self._query_once(payload)
            except httpx.RequestError as exc:
                if attempt + 1 >= HTTP_QUERY_MAX_ATTEMPTS:
                    reason = "query_timeout" if isinstance(exc, httpx.TimeoutException) else "backend_unavailable"
                    raise DorisResultError(reason) from exc
                time.sleep(HTTP_QUERY_RETRY_BACKOFF_SECONDS)
                continue
            if (
                status_code in TRANSIENT_HTTP_STATUSES
                and attempt + 1 < HTTP_QUERY_MAX_ATTEMPTS
            ):
                time.sleep(HTTP_QUERY_RETRY_BACKOFF_SECONDS)
                continue
            break
        if status_code is None:  # pragma: no cover - loop always assigns or raises
            raise DorisResultError("backend_unavailable")
        response = httpx.Response(status_code, content=body)
        if response.is_success:
            try:
                value = response.json()
            except ValueError as exc:
                raise DorisResultError("execution_invalid") from exc
            if not isinstance(value, dict):
                raise DorisResultError("execution_invalid")
            return value
        try:
            decoded = response.json()
            error_payload = decoded.get("error", {}) if isinstance(decoded, dict) else {}
        except ValueError:
            error_payload = {}
        raise backend_error(response.status_code, error_payload)


def build_mcp_server(backend: QueryBackend | None = None) -> MCPServer:
    query_backend = backend or HttpQueryBackend(
        os.getenv("AGENT_QUERY_BACKEND_URL", "http://127.0.0.1:8010")
    )
    server = MCPServer(
        name="doris-query-agent",
        title="Doris Query Agent",
        description="One-purpose MCP server for arbitrary read-only Doris SELECT queries.",
        instructions=SERVER_INSTRUCTIONS,
        version="0.2.0",
        log_level="WARNING",
    )

    @server.tool(
        title="Query Doris with read-only SQL",
        description=(
            "Validate and execute one model-generated Doris SELECT or WITH query. Physical "
            "tables must be schema-qualified. The backend enforces schema, timeout, row, "
            "function, read-only account, and audit controls. Use information_schema queries "
            "to discover tables and columns when needed."
        ),
        annotations=QUERY_TOOL_ANNOTATIONS,
    )
    def query_doris_sql(sql: str, purpose: str | None = None) -> dict[str, Any]:
        try:
            return query_backend.query(sql=sql, purpose=purpose)
        except DorisResultError as exc:
            # Preserve the failure category through MCP without returning raw
            # HTTP diagnostics or mislabeling an error as an empty query.
            return {"status": "failed", "error": exc.as_tool_dict()}

    return server


mcp = build_mcp_server()


def main() -> int:
    mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

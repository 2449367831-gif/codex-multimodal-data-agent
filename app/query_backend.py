from __future__ import annotations

from contextlib import asynccontextmanager
from ipaddress import ip_address
import os
from typing import Any

from .adhoc_sql import AdHocSqlService
from .errors import ParameterValidationError, RuntimeErrorBase


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().casefold()
    if normalized == "localhost":
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


def require_loopback_bind_host(host: str) -> str:
    if not _is_loopback_host(host):
        raise RuntimeError(
            "The unauthenticated query backend may only bind to a loopback address"
        )
    return host


def create_query_backend(
    service: AdHocSqlService | None = None,
    *,
    expose_docs: bool = False,
) -> Any:
    try:
        from fastapi import Body, FastAPI
        from fastapi.responses import JSONResponse
    except ImportError as exc:  # pragma: no cover - required runtime dependency
        raise RuntimeError("FastAPI is required for the Doris query backend") from exc

    sql_service = service or AdHocSqlService()

    @asynccontextmanager
    async def lifespan(_app: Any):
        try:
            yield
        finally:
            sql_service.close()

    app = FastAPI(
        title="Codex Doris Read-Only Query Backend",
        version="0.2.0",
        description="Local-only validation, execution, and audit API for model-generated Doris SELECT queries.",
        lifespan=lifespan,
        docs_url="/docs" if expose_docs else None,
        redoc_url="/redoc" if expose_docs else None,
        openapi_url="/openapi.json" if expose_docs else None,
    )
    app.state.sql_service = sql_service

    @app.middleware("http")
    async def loopback_only(request: Any, call_next: Any) -> Any:
        client_host = request.client.host if request.client is not None else ""
        if client_host != "testclient" and not _is_loopback_host(client_host):
            return JSONResponse(
                status_code=403,
                content={
                    "error": {
                        "code": "loopback_required",
                        "message": "This query backend accepts loopback clients only",
                        "details": {},
                    }
                },
            )
        return await call_next(request)

    @app.exception_handler(RuntimeErrorBase)
    async def runtime_error_handler(_request: Any, exc: RuntimeErrorBase) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": exc.as_public_dict()},
        )

    @app.get("/health")
    def health() -> dict[str, Any]:
        return sql_service.health()

    @app.get("/live")
    async def live() -> dict[str, Any]:
        """Return a cheap process liveness response for the hidden supervisor.

        ``/health`` intentionally probes Doris and may be slower than a
        process watchdog interval. It must not be used as a restart signal.
        """

        return {"status": "ok", "ready": True, "service": "query_backend"}

    @app.post("/v1/sql")
    def query_sql(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        allowed = {"sql", "purpose"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ParameterValidationError(
                "Unknown SQL query fields",
                details={"unknown": unknown},
            )
        sql = payload.get("sql")
        purpose = payload.get("purpose")
        if not isinstance(sql, str) or not sql.strip():
            raise ParameterValidationError("sql must be a non-empty string")
        if purpose is not None and not isinstance(purpose, str):
            raise ParameterValidationError("purpose must be a string")
        return sql_service.query(sql=sql, purpose=purpose)

    return app


app = create_query_backend()


def main() -> int:
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - required runtime dependency
        raise RuntimeError("uvicorn is required for the Doris query backend") from exc
    host = require_loopback_bind_host(os.getenv("AGENT_HOST", "127.0.0.1"))
    port = int(os.getenv("AGENT_PORT", "8010"))
    uvicorn.run("app.query_backend:app", host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

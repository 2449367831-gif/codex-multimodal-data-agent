"""Deliberately inert compatibility entrypoint.

The historical contract API remains available only through an explicit
``app.api.create_app`` call in local regression code. Accidentally serving
``app.main:app`` must never publish its SQL/result-bearing routes.
"""

from ipaddress import ip_address

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def _loopback(host: str) -> bool:
    value = (host or "").strip().casefold()
    if value in {"localhost", "testclient"}:
        return True
    try:
        return ip_address(value).is_loopback
    except ValueError:
        return False


app = FastAPI(
    title="Doris Query Agent Compatibility Shell",
    version="0.1.0",
    description="Disabled legacy entrypoint; use app.chat_api for the process-only UI.",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.middleware("http")
async def loopback_only(request: Request, call_next):
    host = request.client.host if request.client is not None else ""
    if not _loopback(host):
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "code": "loopback_required",
                    "message": "该兼容入口仅允许本机访问",
                    "details": {},
                }
            },
        )
    return await call_next(request)


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "status": "disabled",
        "ready": False,
        "message": "历史合同入口已停用，请使用 app.chat_api",
    }

from __future__ import annotations

from ipaddress import ip_address
from typing import Any

from .errors import MetricNotFoundError, ParameterValidationError, RuntimeErrorBase
from .agent import AgentWorkflow
from .service import QueryService


def create_app(
    service: QueryService | None = None,
    *,
    legacy_enabled: bool = False,
    enforce_loopback: bool = True,
    expose_docs: bool = False,
) -> Any:
    try:
        from fastapi import Body, FastAPI, Query
        from fastapi.responses import JSONResponse
    except ImportError as exc:  # pragma: no cover - depends on optional package
        raise RuntimeError(
            "FastAPI is not installed. Run: python -m pip install -r app/requirements-api.txt"
        ) from exc

    query_service = service or QueryService()
    app = FastAPI(
        title="Governed Doris Query Agent Runtime",
        version="0.1.0",
        description="Metric-contract routing, safe query execution, and audit APIs.",
        docs_url="/docs" if expose_docs else None,
        redoc_url="/redoc" if expose_docs else None,
        openapi_url="/openapi.json" if expose_docs else None,
    )
    app.state.query_service = query_service
    agent_workflow = AgentWorkflow(query_service)
    app.state.agent_workflow = agent_workflow

    @app.middleware("http")
    async def legacy_boundary(request: Any, call_next: Any) -> Any:
        # The historical contract API returns SQL, bindings, source metadata,
        # and result rows.  Keep it available for local regression only; the
        # production app.main entrypoint disables it before serving requests.
        path = str(getattr(request.url, "path", "") or "")
        if not legacy_enabled and path != "/health":
            from fastapi.responses import JSONResponse

            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "endpoint_not_found",
                        "message": "该运行入口不提供用户查询接口",
                        "details": {},
                    }
                },
            )
        if enforce_loopback:
            client = getattr(request, "client", None)
            host = str(getattr(client, "host", "") or "")
            is_testclient = host.casefold() == "testclient"
            is_loopback = host.casefold() == "localhost"
            if not is_loopback and not is_testclient:
                try:
                    is_loopback = ip_address(host).is_loopback
                except ValueError:
                    is_loopback = False
            if not is_loopback and not is_testclient:
                from fastapi.responses import JSONResponse

                return JSONResponse(
                    status_code=403,
                    content={
                        "error": {
                            "code": "loopback_required",
                            "message": "该内部运行入口仅允许本机访问",
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
        return query_service.health()

    @app.get("/v1/metrics")
    def list_metrics() -> dict[str, Any]:
        metrics = query_service.list_metrics()
        return {"items": metrics, "count": len(metrics)}

    @app.get("/v1/metrics/{metric_id}")
    def get_metric(metric_id: str) -> dict[str, Any]:
        return query_service.get_metric(metric_id)

    @app.get("/v1/releases")
    def list_releases() -> dict[str, Any]:
        releases = query_service.list_releases()
        return {"items": releases, "count": len(releases)}

    @app.get("/v1/releases/current")
    def current_release() -> dict[str, Any]:
        return {
            "manifest": query_service.current_release(),
            "planning_manifest": query_service.planning_release(),
        }

    @app.post("/v1/route")
    def route(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        allowed = {"question", "limit"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ParameterValidationError("Unknown route fields", details={"unknown": unknown})
        limit = payload.get("limit", 5)
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ParameterValidationError("limit must be an integer")
        return query_service.router.route(payload.get("question", ""), limit=limit).as_dict()

    @app.post("/v1/query")
    def query_metric(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        allowed = {"metric_id", "question", "parameters", "include_sql", "dry_run"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ParameterValidationError("Unknown query fields", details={"unknown": unknown})
        metric_id = payload.get("metric_id")
        question = payload.get("question")
        if metric_id is not None and not isinstance(metric_id, str):
            raise ParameterValidationError("metric_id must be a string")
        if question is not None and not isinstance(question, str):
            raise ParameterValidationError("question must be a string")
        if not metric_id and not question:
            raise ParameterValidationError("Provide metric_id or question")
        parameters = payload.get("parameters", {})
        include_sql = payload.get("include_sql", False)
        dry_run = payload.get("dry_run", False)
        if not isinstance(include_sql, bool) or not isinstance(dry_run, bool):
            raise ParameterValidationError("include_sql and dry_run must be booleans")
        if dry_run:
            plan = query_service.plan_and_audit(
                metric_id=metric_id,
                parameters=parameters,
                question=question,
            )
            return {"status": "planned", "plan": plan.as_dict(include_sql=include_sql)}
        return query_service.query(
            metric_id=metric_id,
            parameters=parameters,
            question=question,
            include_sql=include_sql,
        )

    @app.post("/v1/agent")
    def run_agent(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        allowed = {"question", "parameters", "execute", "include_sql"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ParameterValidationError("Unknown agent fields", details={"unknown": unknown})
        question = payload.get("question")
        if not isinstance(question, str) or not question.strip():
            raise ParameterValidationError("question must be a non-empty string")
        parameters = payload.get("parameters", {})
        if not isinstance(parameters, dict):
            raise ParameterValidationError("parameters must be a JSON object")
        execute = payload.get("execute", False)
        include_sql = payload.get("include_sql", False)
        if not isinstance(execute, bool) or not isinstance(include_sql, bool):
            raise ParameterValidationError("execute and include_sql must be booleans")
        return agent_workflow.invoke(
            question=question,
            supplied_parameters=parameters,
            execute=execute,
            include_sql=include_sql,
        )

    @app.get("/v1/audits")
    def list_audits(
        limit: int = Query(default=50, ge=1, le=500),
        metric_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        records = query_service.audit.list(limit=limit, metric_id=metric_id)
        return {"items": records, "count": len(records)}

    @app.get("/v1/audits/{request_id}")
    def get_audit(request_id: str) -> dict[str, Any]:
        record = query_service.audit.get(request_id)
        if record is None:
            raise MetricNotFoundError(
                f"Unknown audit request_id: {request_id}",
                details={"request_id": request_id},
            )
        return record

    return app

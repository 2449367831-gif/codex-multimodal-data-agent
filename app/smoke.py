from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from .settings import RuntimeSettings
from .service import QueryService


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    with TemporaryDirectory(prefix="query-agent-smoke-") as temp_dir:
        settings = RuntimeSettings(
            project_root=project_root,
            executor_mode="mock",
            audit_db_path=Path(temp_dir) / "audit.sqlite3",
            max_rows=100,
        )
        service = QueryService(settings)
        assert service.health()["status"] == "ok"
        assert len(service.list_metrics()) >= 4

        route = service.router.route("查询当前实时成品库存周转")
        assert route.selected_metric_id == "inventory.turnover_finished_goods_realtime"

        result = service.query(
            metric_id="sales.outbound_actual",
            question="smoke test",
            parameters={
                "start_inclusive": "2026-08-01 00:00:00",
                "end_exclusive": "2026-08-02 00:00:00",
                "output_grain": "total",
            },
            include_sql=True,
        )
        assert result["status"] == "mock_incomplete"
        assert result["execution"]["is_mock"] is True
        assert service.audit.get(result["request_id"])["status"] == "mock_incomplete"

    print("Runtime smoke passed: knowledge load, route, plan, mock execute, audit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

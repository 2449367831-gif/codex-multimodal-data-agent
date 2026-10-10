"""Dependencies shared by metric handlers, never shared across requests."""

from datetime import date
from zoneinfo import ZoneInfo

from ..execution import QueryExecutionSession


class MetricHandler:
    changsha_huangxing_wms = "10114790"

    def __init__(self, execution: QueryExecutionSession, *, timezone_name: str):
        self.execution = execution
        self.owner = execution.legacy_owner
        self.timezone_name = timezone_name
        self._zone = ZoneInfo(timezone_name)

    def _execute(self, sql: str, *, purpose: str):
        return self.execution.query(sql, purpose=purpose)

    @staticmethod
    def _sql_date(value: date) -> str:
        return value.isoformat()

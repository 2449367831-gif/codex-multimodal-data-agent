"""T34 fixed synthetic scalar execution through the approved Doris MCP only."""

from datetime import datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.adhoc_sql import AdHocSqlValidator
from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result
from app.sql_translation import translate_mysql_sql


def scalar_fixture():
    end = datetime(2026, 9, 10)
    values = {
        "end": end, "start": end - timedelta(days=1),
        "before": end - timedelta(days=1, seconds=1),
        "quantity": Decimal("0.1234567890123456789"),
        "text": "literal'; SELECT 2; -- :untouched",
    }
    sql = """WITH facts AS (
      SELECT :start AS shipped_at, :quantity AS qty
      UNION ALL SELECT :before AS shipped_at, 99 AS qty
      UNION ALL SELECT :end AS shipped_at, 88 AS qty
    ) SELECT DATE_FORMAT(DATE_SUB(:end, INTERVAL 1 DAY), '%Y-%m-%d %H:%i:%s') AS window_start,
      SUM(IF(shipped_at >= :start AND shipped_at < :end, IFNULL(qty, 0), 0)) AS exact_quantity,
      COUNT(*) AS fact_count, IF(NULL, 1, 2) AS null_choice, :text AS bound_text,
      IFNULL(NULL, 7) AS `空值处理`
    FROM facts"""
    expected = {"window_start": "2026-09-09 00:00:00", "exact_quantity": values["quantity"],
                "fact_count": 3, "null_choice": 2, "bound_text": values["text"], "空值处理": 7}
    translated = translate_mysql_sql(sql, bindings=values, table_mapping={}, source_columns={},
                                    validator=AdHocSqlValidator(allowed_schemas=("ywzt_ods",), max_rows=10))
    return translated, expected


def compare(rows, expected):
    if len(rows) != 1 or set(rows[0]) != set(expected):
        return {"exact_result_shape": False}
    row = rows[0]
    return {
        "exact_result_shape": True,
        "date_format_and_date_sub": row["window_start"] == expected["window_start"],
        "exclusive_window_and_decimal": Decimal(str(row["exact_quantity"])) == expected["exact_quantity"],
        "union_all_cardinality": Decimal(str(row["fact_count"])) == expected["fact_count"],
        "null_conditional": Decimal(str(row["null_choice"])) == expected["null_choice"],
        "bound_string_not_sql": row["bound_text"] == expected["bound_text"],
        "quoted_alias_and_null_fallback": Decimal(str(row["空值处理"])) == expected["空值处理"],
    }


def main():
    _hide_windows_subprocesses()
    translated, expected = scalar_fixture()
    bridge = CodexMcpBridge(project_root=ROOT, backend_url="http://127.0.0.1:8010", timeout_seconds=60)
    result = validate_doris_result(bridge.query(sql=translated.sql, purpose="T34 synthetic scalar compatibility verification"))
    checks = compare(result.rows, expected)
    print(json.dumps({"case": "T34_synthetic_doris", "checks": checks}))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"case": "T34_synthetic_doris", "state": "failed", "error_type": type(exc).__name__}))
        raise SystemExit(1)

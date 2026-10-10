"""Request-local Doris execution and verifiable result evidence."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import time
from typing import Any, Mapping

import sqlglot
from sqlglot import exp

from ..chat_tasks import report_progress
from ..errors import RuntimeErrorBase
from ..query_scope import QueryScope, apply_scope
from ..query_outcomes import DorisResultError, backend_error, validate_doris_result
from .semantic_types import QueryClient, SemanticRuntimeError


@dataclass
class QueryExecutionSession:
    client: QueryClient
    scope: QueryScope
    legacy_owner: str
    calls: int = 0
    succeeded: int = 0
    row_counts: list[int] = field(default_factory=list)
    business_succeeded: int = 0
    business_row_counts: list[int] = field(default_factory=list)
    _inventory_warehouse_pairs: tuple[tuple[str, str], ...] | None = None

    def query(self, sql: str, *, purpose: str) -> list[Mapping[str, Any]]:
        return self._query_checked(self._scoped_sql(sql), purpose=purpose, business=True)

    def _scoped_sql(self, sql: str) -> str:
        tree = sqlglot.parse_one(sql, read="doris")
        uses_inventory = any(
            t.db == "ywzt_ods" and t.name == "sku_inventory"
            for t in tree.find_all(exp.Table)
        )
        if uses_inventory and self.scope.warehouse_codes:
            self._resolve_inventory_warehouses()
        return apply_scope(
            sql, self.scope, legacy_owner=self.legacy_owner,
            inventory_warehouse_pairs=self._inventory_warehouse_pairs,
        )

    def query_many(self, statements, *, purpose: str, timeout_seconds: float):
        return self._query_batch(statements, purpose=purpose, timeout_seconds=timeout_seconds, scoped=True)

    def _query_many_checked(self, statements, *, purpose: str, timeout_seconds: float):
        """For compilers that own scope placement; backend gates remain mandatory."""
        return self._query_batch(statements, purpose=purpose, timeout_seconds=timeout_seconds, scoped=False)

    def _query_batch(self, statements, *, purpose: str, timeout_seconds: float, scoped: bool):
        if (not isinstance(statements, (list,tuple)) or not 1 <= len(statements) <= 4
            or any(not isinstance(sql,str) or not sql.strip() for sql in statements)
            or isinstance(timeout_seconds,bool) or not isinstance(timeout_seconds,(int,float))
            or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 180):
            raise ValueError('invalid execution batch')
        deadline = time.monotonic() + timeout_seconds
        def remaining():
            value = deadline - time.monotonic()
            if value <= 0:
                raise SemanticRuntimeError('分批查询超时，未返回完整结果', reason='query_timeout')
            return value
        batch_query = getattr(self.client, 'query_many', None)
        if not callable(batch_query):
            results = []
            for sql in statements:
                remaining()
                results.append(self.query(sql, purpose=purpose) if scoped else
                               self._query_checked(sql, purpose=purpose, business=True))
                remaining()
            return results
        prepared = [self._scoped_sql(sql) for sql in statements] if scoped else list(statements)
        budget = remaining()
        report_progress('query')
        self.calls += len(prepared)
        responses = self._invoke(batch_query, statements=prepared, purpose=purpose, timeout_seconds=budget)
        remaining()
        if not isinstance(responses, (list,tuple)) or len(responses) != len(prepared):
            raise SemanticRuntimeError('分批查询未返回完整结果', reason='execution_invalid')
        rows = [self._record_response(response, business=True) for response in responses]
        remaining()
        return rows

    def _resolve_inventory_warehouses(self) -> None:
        if self._inventory_warehouse_pairs is not None:
            return
        codes = ", ".join(exp.Literal.string(c).sql(dialect="doris") for c in self.scope.warehouse_codes)
        owner_filter = ""
        if self.scope.owner != "ALL":
            owner = exp.Literal.string(self.scope.owner).sql(dialect="doris")
            owner_filter = f" AND m.cargo_owner = {owner}"
        # Validate both directions before filtering inventory by display name.
        # Selecting codes before counting names would hide ambiguous mappings.
        rows = self._query_checked(f"""WITH mappings AS (
  SELECT DISTINCT TRIM(CAST(cargo_owner_id AS STRING)) AS cargo_owner,
         TRIM(wms_code) AS wms_code, TRIM(wms_desc) AS warehouse_name
  FROM ywzt_ods.master_wms
  WHERE COALESCE(TRIM(CAST(status AS STRING)), '0') = '0'
), name_controls AS (
  SELECT cargo_owner, warehouse_name, COUNT(DISTINCT wms_code) AS name_code_count
  FROM mappings GROUP BY cargo_owner, warehouse_name
)
SELECT m.cargo_owner, m.wms_code, m.warehouse_name, n.name_code_count
FROM mappings AS m
JOIN name_controls AS n
  ON m.cargo_owner = n.cargo_owner AND m.warehouse_name = n.warehouse_name
WHERE m.wms_code IN ({codes}){owner_filter}""",
            purpose="inventory warehouse mapping verification", business=False)
        mapped: dict[tuple[str, str], str] = {}
        for row in rows:
            owner = str(row.get("cargo_owner") or "").strip()
            code = str(row.get("wms_code") or "").strip()
            name = str(row.get("warehouse_name") or "").strip()
            if (
                not owner or not name or code not in self.scope.warehouse_codes
                or str(row.get("name_code_count")) != "1"
                or (self.scope.owner != "ALL" and owner != self.scope.owner)
                or ((owner, code) in mapped and mapped[(owner, code)] != name)
            ):
                raise SemanticRuntimeError("仓库名称映射存在冲突，暂未展示库存", reason="inventory_warehouse_mapping_conflict")
            mapped[(owner, code)] = name
        if {code for _, code in mapped} != set(self.scope.warehouse_codes):
            raise SemanticRuntimeError("未找到完整的仓库映射，暂未展示库存", reason="inventory_warehouse_mapping_missing")
        self._inventory_warehouse_pairs = tuple(sorted({(owner, name) for (owner, _), name in mapped.items()}))

    def _query_checked(self, sql: str, *, purpose: str, business: bool) -> list[Mapping[str, Any]]:
        report_progress("query")
        self.calls += 1
        response = self._invoke(self.client.query, sql=sql, purpose=purpose)
        return self._record_response(response, business=business)

    @staticmethod
    def _invoke(method, **arguments):
        try:
            return method(**arguments)
        except DorisResultError as exc:
            raise SemanticRuntimeError(exc.message, reason=exc.reason) from exc
        except RuntimeErrorBase as exc:
            failure = backend_error(exc.http_status, {"code": exc.code})
            raise SemanticRuntimeError(failure.message, reason=failure.reason) from exc
        except (TimeoutError, ConnectionError) as exc:
            raise SemanticRuntimeError("取数连接未完成，请稍后重试", reason="backend_unavailable") from exc
        except Exception as exc:
            raise SemanticRuntimeError("本次取数未完成，请稍后重试", reason="query_failed") from exc

    def _record_response(self, response, *, business):
        report_progress("validation")
        try:
            rows = validate_doris_result(response).rows
        except DorisResultError as exc:
            raise SemanticRuntimeError(exc.message, reason=exc.reason) from exc
        for row in rows:
            if "__sales_join_conflicts" in row:
                try:
                    conflicts = int(row["__sales_join_conflicts"])
                except (TypeError, ValueError):
                    conflicts = -1
                if conflicts != 0:
                    raise SemanticRuntimeError("销售关联存在重复单号或主数据，数量可能被放大，未展示结果", reason="sales_join_conflict")
            if "__inventory_base_source_rows" in row:
                try:
                    source_rows = int(row["__inventory_base_source_rows"])
                except (TypeError, ValueError):
                    source_rows = 0
                if source_rows <= 0:
                    raise SemanticRuntimeError(
                        "库存来源没有可核验记录，未将缺失库存计为零", reason="inventory_source_empty"
                    )
        self.succeeded += 1
        if business:
            self.business_succeeded += 1
            self.business_row_counts.append(len(rows))
        self.row_counts.append(len(rows))
        return rows

    def evidence(self) -> dict[str, Any]:
        live = self.calls > 0 and self.succeeded == self.calls
        return {
            "live_query_executed": live,
            "live_business_result_executed": live and self.business_succeeded > 0,
            "query_call_count": self.calls,
            "successful_query_call_count": self.succeeded,
            "successful_business_query_call_count": self.business_succeeded,
            "metadata_query_call_count": self.succeeded - self.business_succeeded,
            "failed_query_call_count": self.calls - self.succeeded,
            "unexpected_tool_call_count": 0,
            "result_state": (
                "live_success" if live and any(self.business_row_counts)
                else "no_data" if live and self.business_succeeded
                else "failed"
            ),
            "status": (
                "live_result"
                if live and self.business_succeeded > 0
                else "metadata_only"
                if live and self.succeeded > 0
                else "no_live_result"
            ),
        }

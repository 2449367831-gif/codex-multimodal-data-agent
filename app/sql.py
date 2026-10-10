from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import re
from pathlib import Path
from threading import RLock
from typing import Any, Iterable

import yaml

from .errors import (
    SqlSafetyError,
    TemplateNotExecutableError,
    UnsupportedTemplateParameterError,
)


NAMED_PARAMETER_RE = re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)")
FORBIDDEN_TOKENS = {
    "INSERT",
    "UPDATE",
    "DELETE",
    "CREATE",
    "ALTER",
    "DROP",
    "TRUNCATE",
    "MERGE",
    "LOAD",
    "CALL",
    "GRANT",
    "REVOKE",
    "OUTFILE",
    "INFILE",
    "INTO",
    "SET",
    "USE",
    "SLEEP",
    "BENCHMARK",
    "LOAD_FILE",
    "GET_LOCK",
}
CONTROL_PARAMETERS = {"output_grain"}
DANGEROUS_FUNCTIONS = {"sleep", "benchmark", "load_file", "get_lock"}


def strip_sql_comments(sql: str) -> str:
    """Remove comments without treating comment markers inside literals as syntax."""
    result: list[str] = []
    index = 0
    quote: str | None = None
    line_comment = False
    block_comment = False
    while index < len(sql):
        char = sql[index]
        next_char = sql[index + 1] if index + 1 < len(sql) else ""

        if line_comment:
            if char in "\r\n":
                line_comment = False
                result.append(char)
            else:
                result.append(" ")
            index += 1
            continue

        if block_comment:
            if char == "*" and next_char == "/":
                result.extend((" ", " "))
                index += 2
                block_comment = False
            else:
                result.append(char if char in "\r\n" else " ")
                index += 1
            continue

        if quote is not None:
            result.append(char)
            if char == "\\" and next_char:
                result.append(next_char)
                index += 2
                continue
            if char == quote:
                if next_char == quote:
                    result.append(next_char)
                    index += 2
                    continue
                quote = None
            index += 1
            continue

        if char in {"'", '"', "`"}:
            quote = char
            result.append(char)
            index += 1
            continue
        if char == "-" and next_char == "-":
            result.extend((" ", " "))
            index += 2
            line_comment = True
            continue
        if char == "/" and next_char == "*":
            result.extend((" ", " "))
            index += 2
            block_comment = True
            continue
        result.append(char)
        index += 1

    if block_comment:
        raise SqlSafetyError("SQL contains an unterminated block comment")
    return "".join(result)


def mask_sql_literals(sql: str) -> str:
    result: list[str] = []
    index = 0
    quote: str | None = None
    while index < len(sql):
        char = sql[index]
        if quote is None:
            if char in {"'", '"'}:
                quote = char
                result.append(" ")
            else:
                result.append(char)
            index += 1
            continue
        if char == quote:
            if index + 1 < len(sql) and sql[index + 1] == quote:
                result.extend((" ", " "))
                index += 2
                continue
            quote = None
        result.append(" ")
        index += 1
    if quote is not None:
        raise SqlSafetyError("SQL contains an unterminated string literal")
    return "".join(result)


def _normalized_relation(value: str) -> str:
    return value.replace("`", "").strip().casefold()


@dataclass(frozen=True)
class CompiledTemplate:
    sql: str
    bindings: dict[str, Any]
    source_tables: tuple[str, ...]
    validation_results: tuple[dict[str, Any], ...]


class TemplateCompiler:
    def __init__(
        self,
        project_root: Path,
        *,
        reload_on_change: bool = True,
        preload_template_refs: Iterable[str] = (),
    ) -> None:
        self.project_root = project_root.resolve()
        self.reload_on_change = reload_on_change
        repository_capability_path = self.project_root / "app" / "template_capabilities.yaml"
        self.capability_path = (
            repository_capability_path
            if repository_capability_path.is_file()
            else Path(__file__).with_name("template_capabilities.yaml")
        )
        self._lock = RLock()
        self._capability_signature: tuple[int, int] | None = None
        self._template_cache: dict[Path, str] = {}
        self._cached_file_digests: dict[str, str] = {}
        self.capabilities: dict[str, Any] = {}
        self._reload_capabilities()
        if not self.reload_on_change:
            for template_ref in sorted(set(preload_template_refs)):
                template_path = self._resolve_template_path(template_ref)
                self._template_cache[template_path] = self._read_utf8_snapshot(template_path)

    def _read_utf8_snapshot(self, path: Path) -> str:
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise TemplateNotExecutableError(f"Unable to load runtime SQL input: {path}") from exc
        try:
            relative_path = path.resolve().relative_to(self.project_root).as_posix()
        except ValueError as exc:
            if not self.reload_on_change:
                raise TemplateNotExecutableError(
                    "Frozen runtime inputs must be stored under the protected project root"
                ) from exc
        else:
            with self._lock:
                self._cached_file_digests[relative_path] = sha256(raw).hexdigest()
        return text

    def cached_file_digests(self) -> dict[str, str]:
        with self._lock:
            return dict(self._cached_file_digests)

    def _resolve_template_path(self, template_ref: str) -> Path:
        template_path = (self.project_root / template_ref).resolve()
        try:
            template_path.relative_to(self.project_root)
        except ValueError as exc:
            raise TemplateNotExecutableError("Template path escapes the project root") from exc
        if not template_path.is_file():
            raise TemplateNotExecutableError(f"SQL template not found: {template_ref}")
        return template_path

    def _reload_capabilities(self) -> None:
        stat = self.capability_path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        with self._lock:
            if signature == self._capability_signature or (
                not self.reload_on_change and self._capability_signature is not None
            ):
                return
            doc = yaml.safe_load(self._read_utf8_snapshot(self.capability_path)) or {}
            metrics = doc.get("metrics", {})
            if not isinstance(metrics, dict):
                raise TemplateNotExecutableError("template_capabilities.yaml metrics must be a mapping")
            self.capabilities = metrics
            self._capability_signature = signature

    def compile(self, metric: dict[str, Any], parameters: dict[str, Any]) -> CompiledTemplate:
        self._reload_capabilities()
        metric_id = metric["id"]
        metric_ref = f"{metric_id}@{metric.get('version')}"
        with self._lock:
            capability = dict(self.capabilities.get(metric_ref, {}))
        if not capability:
            raise TemplateNotExecutableError(
                f"Metric {metric_ref} has no explicitly registered template capability",
                details={"metric_ref": metric_ref},
            )
        if not capability.get("executable", False):
            raise TemplateNotExecutableError(
                f"Metric {metric_ref} does not have an executable approved template",
                details={"reason": capability.get("reason")},
            )

        template_ref = metric.get("template")
        if not isinstance(template_ref, str) or not template_ref:
            raise TemplateNotExecutableError(f"Metric {metric_id} has no SQL template")
        template_path = self._resolve_template_path(template_ref)
        with self._lock:
            sql = self._template_cache.get(template_path)
        if self.reload_on_change:
            sql = self._read_utf8_snapshot(template_path)
            with self._lock:
                self._template_cache[template_path] = sql
        elif sql is None:
            raise TemplateNotExecutableError(
                "Frozen runtime refused a template that was not preloaded after integrity validation",
                details={"template": template_ref},
            )
        executable_sql = strip_sql_comments(sql).strip()
        if not executable_sql or not re.match(r"^(SELECT|WITH)\b", executable_sql, flags=re.IGNORECASE):
            raise TemplateNotExecutableError(
                f"Template for {metric_id} is documentation-only or not a SELECT query"
            )

        allowed_grains = capability.get("output_grains")
        requested_grain = parameters.get("output_grain")
        if allowed_grains and requested_grain not in allowed_grains:
            raise UnsupportedTemplateParameterError(
                f"Approved template for {metric_id} does not implement output_grain={requested_grain}",
                details={"supported_output_grains": allowed_grains},
            )

        placeholders = set(NAMED_PARAMETER_RE.findall(executable_sql))
        unknown_placeholders = sorted(placeholders - set(parameters))
        if unknown_placeholders:
            raise TemplateNotExecutableError(
                "Template references parameters missing from the contract",
                details={"parameters": unknown_placeholders},
            )

        ignored_explicit: list[str] = []
        for name, value in parameters.items():
            if name in placeholders or name in CONTROL_PARAMETERS:
                continue
            if value not in (None, "ALL"):
                ignored_explicit.append(name)
        if ignored_explicit:
            raise UnsupportedTemplateParameterError(
                "Approved template cannot apply one or more requested filters",
                details={"unsupported_parameters": sorted(ignored_explicit)},
            )

        validation_sql = NAMED_PARAMETER_RE.sub(
            lambda match: f"%({match.group(1)})s", executable_sql
        )
        dbapi_source_sql = executable_sql.replace("%", "%%")
        compiled_sql = NAMED_PARAMETER_RE.sub(
            lambda match: f"%({match.group(1)})s", dbapi_source_sql
        )
        bindings = {name: parameters[name] for name in sorted(placeholders)}
        allowed_sources = set(metric.get("sources", {}).get("required", []))
        allowed_sources.update(metric.get("sources", {}).get("conditional", []))
        safety = SqlSafetyValidator().validate(validation_sql, allowed_sources=allowed_sources)
        contract_checks = self._validate_contract_static_checks(metric, validation_sql)
        return CompiledTemplate(
            sql=compiled_sql,
            bindings=bindings,
            source_tables=tuple(safety["source_tables"]),
            validation_results=tuple([*safety["checks"], *contract_checks]),
        )

    @staticmethod
    def _validate_contract_static_checks(
        metric: dict[str, Any],
        compiled_sql: str,
    ) -> list[dict[str, Any]]:
        required = set(metric.get("required_validations", []))
        static_checks = {
            "shipment_header_non_multiplication",
            "sales_order_non_multiplication",
        }
        if not required.intersection(static_checks):
            return []
        try:
            import sqlglot
            from sqlglot import expressions as exp
        except ImportError as exc:  # pragma: no cover - required runtime dependency
            raise TemplateNotExecutableError(
                "SQLGlot is required to prove contract-specific non-multiplication rules"
            ) from exc

        parser_sql = re.sub(r"%\([A-Za-z_][A-Za-z0-9_]*\)s", "0", compiled_sql)
        try:
            root = sqlglot.parse_one(parser_sql, read="mysql")
        except Exception as exc:
            raise TemplateNotExecutableError(
                "Unable to prove contract-specific non-multiplication rules from the SQL AST",
                details={"error": str(exc)},
            ) from exc

        def unwrap_parens(expression: Any) -> Any:
            while isinstance(expression, exp.Paren):
                expression = expression.this
            return expression

        def and_terms(expression: Any) -> list[Any]:
            expression = unwrap_parens(expression)
            if isinstance(expression, exp.And):
                return [*and_terms(expression.this), *and_terms(expression.expression)]
            return [expression]

        if isinstance(root, exp.SetOperation) or any(root.find_all(exp.SetOperation)):
            raise TemplateNotExecutableError(
                "Contract-protected shipment SQL cannot use UNION, INTERSECT, or EXCEPT"
            )

        header_nodes = [
            table
            for table in root.find_all(exp.Table)
            if str(table.db or "").casefold() == "ywzt_ods"
            and str(table.name or "").casefold() == "scm_sales_shipment_header"
        ]
        results: list[dict[str, Any]] = []
        shipment_cte = None
        shipment_select = None
        if "shipment_header_non_multiplication" in required:
            if len(header_nodes) != 1:
                raise TemplateNotExecutableError(
                    "Shipment header deduplication must use one unambiguous physical-header CTE"
                )
            header = header_nodes[0]
            shipment_cte = header.find_ancestor(exp.CTE)
            if shipment_cte is None or not isinstance(shipment_cte.this, exp.Select):
                raise TemplateNotExecutableError(
                    "Shipment header must be deduplicated in one simple SELECT CTE before joining detail"
                )
            if shipment_cte.alias_column_names:
                raise TemplateNotExecutableError(
                    "Shipment header CTE cannot remap output columns with an explicit column list"
                )
            shipment_select = shipment_cte.this
            if header.find_ancestor(exp.Select) is not shipment_select:
                raise TemplateNotExecutableError(
                    "Shipment header cannot be hidden in a nested or set-operation CTE"
                )
            header_alias = str(header.alias_or_name or "").casefold()
            group = shipment_select.args.get("group")
            group_expressions = list(group.expressions) if group is not None else []
            business_key = set(metric.get("filters", {}).get("shipment_header_key", ["outbound_code"]))
            if (
                group is None
                or set(group.args) != {"expressions"}
                or len(group_expressions) != len(business_key)
                or any(not isinstance(column, exp.Column) or str(column.table or "").casefold() != header_alias for column in group_expressions)
                or {str(column.name).casefold() for column in group_expressions} != business_key
            ):
                raise TemplateNotExecutableError(
                    "Shipment header CTE must group exactly by the declared qualified business key"
                )
            for key in business_key:
                projected = [p.this if isinstance(p, exp.Alias) else p for p in shipment_select.expressions if str(p.alias_or_name).casefold() == key]
                if len(projected) != 1 or not isinstance(projected[0], exp.Column) or projected[0].name.casefold() != key or projected[0].table.casefold() != header_alias:
                    raise TemplateNotExecutableError("Shipment header CTE must preserve each declared business-key column")

            outbound_projections: list[Any] = []
            for projection in shipment_select.expressions:
                if str(projection.alias_or_name or "").casefold() != "outbound_code":
                    continue
                outbound_projections.append(
                    projection.this if isinstance(projection, exp.Alias) else projection
                )
            if (
                len(outbound_projections) != 1
                or not isinstance(outbound_projections[0], exp.Column)
                or str(outbound_projections[0].name or "").casefold() != "outbound_code"
                or str(outbound_projections[0].table or "").casefold() != header_alias
            ):
                raise TemplateNotExecutableError(
                    "Shipment header CTE must expose exactly the grouped outbound_code column"
                )

            cte_name = str(shipment_cte.alias_or_name).casefold()
            cte_consumers = [
                table
                for table in root.find_all(exp.Table)
                if (
                not table.db
                and str(table.name or "").casefold() == cte_name
                and table.find_ancestor(exp.CTE) is None
                )
            ]
            if len(cte_consumers) != 1:
                raise TemplateNotExecutableError(
                    "The deduplicated shipment-header CTE must be consumed exactly once"
                )

            detail_nodes = [
                table
                for table in root.find_all(exp.Table)
                if str(table.db or "").casefold() == "ywzt_ods"
                and str(table.name or "").casefold() == "scm_sales_shipment_detail"
            ]
            consumer = cte_consumers[0]
            outer_select = consumer.find_ancestor(exp.Select)
            if len(detail_nodes) != 1 or outer_select is not root:
                raise TemplateNotExecutableError(
                    "Shipment detail and the deduplicated header must meet in one final SELECT"
                )
            detail = detail_nodes[0]
            if detail.find_ancestor(exp.Select) is not outer_select:
                raise TemplateNotExecutableError(
                    "Shipment detail cannot be hidden in a nested query before aggregation"
                )
            detail_join = detail.find_ancestor(exp.Join)
            join_condition = detail_join.args.get("on") if detail_join is not None else None
            join_side = (
                str(detail_join.args.get("side") or "").casefold()
                if detail_join is not None
                else ""
            )
            join_kind = (
                str(detail_join.args.get("kind") or "").casefold()
                if detail_join is not None
                else ""
            )
            join_method = (
                str(detail_join.args.get("method") or "").casefold()
                if detail_join is not None
                else ""
            )
            if (
                detail_join is None
                or join_condition is None
                or join_side
                or join_kind not in {"", "inner"}
                or join_method
            ):
                raise TemplateNotExecutableError(
                    "Shipment detail must use an explicit keyed INNER JOIN to the deduplicated header"
                )
            consumer_alias = str(consumer.alias_or_name or "").casefold()
            detail_alias = str(detail.alias_or_name or "").casefold()
            joined_keys: set[str] = set()
            for term in and_terms(join_condition):
                term = unwrap_parens(term)
                if not isinstance(term, exp.EQ):
                    continue
                left = term.this
                right = term.expression
                if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
                    continue
                for header_column, detail_column in ((left, right), (right, left)):
                    if (
                        str(header_column.name or "").casefold() in business_key
                        and str(header_column.table or "").casefold() == consumer_alias
                        and str(detail_column.name or "").casefold() == str(header_column.name or "").casefold()
                        and str(detail_column.table or "").casefold() == detail_alias
                    ):
                        joined_keys.add(str(header_column.name).casefold())
            if joined_keys != business_key:
                raise TemplateNotExecutableError(
                    "Shipment detail JOIN must directly equate every declared business-key column"
                )
            results.append({
                "id": "shipment_header_non_multiplication",
                "passed": True,
                "engine": "sqlglot_contract_ast",
            })

        if "sales_order_non_multiplication" in required:
            order_nodes = [
                table
                for table in root.find_all(exp.Table)
                if str(table.db or "").casefold() == "ywzt_ods"
                and str(table.name or "").casefold() == "scm_sales_order_header"
            ]
            if len(order_nodes) != 1 or len(header_nodes) != 1:
                raise TemplateNotExecutableError(
                    "Sales-order validity must use one source-order table inside the shipment-header CTE"
                )
            order_table = order_nodes[0]
            exists = order_table.find_ancestor(exp.Exists)
            header = header_nodes[0]
            header_cte = shipment_cte or header.find_ancestor(exp.CTE)
            header_select = shipment_select or (
                header_cte.this if header_cte is not None and isinstance(header_cte.this, exp.Select) else None
            )
            if (
                exists is None
                or not isinstance(exists.this, exp.Select)
                or order_table.find_ancestor(exp.Select) is not exists.this
                or exists.find_ancestor(exp.Not) is not None
                or header_cte is None
                or header_select is None
                or exists.find_ancestor(exp.CTE) is not header_cte
            ):
                raise TemplateNotExecutableError(
                    "Sales-order validity must be a positive EXISTS filter in the shipment-header CTE"
                )

            where = header_select.args.get("where")
            if where is None or not any(
                unwrap_parens(term) is exists for term in and_terms(where.this)
            ):
                raise TemplateNotExecutableError(
                    "Sales-order EXISTS must be a mandatory top-level AND filter"
                )

            order_alias = str(order_table.alias_or_name or "").casefold()
            header_alias = str(header.alias_or_name or "").casefold()
            inner_where = exists.this.args.get("where")
            correlated = False
            inner_terms = and_terms(inner_where.this) if inner_where is not None else []
            for equality in inner_terms:
                equality = unwrap_parens(equality)
                if not isinstance(equality, exp.EQ):
                    continue
                left = equality.this
                right = equality.expression
                if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
                    continue
                pairs = ((left, right), (right, left))
                if any(
                    str(order_column.name or "").casefold() == "sales_order_code"
                    and str(order_column.table or "").casefold() == order_alias
                    and str(header_column.name or "").casefold() == "ref_order_code"
                    and str(header_column.table or "").casefold() == header_alias
                    for order_column, header_column in pairs
                ):
                    correlated = True
                    break
            if not correlated:
                raise TemplateNotExecutableError(
                    "Sales-order EXISTS must correlate sales_order_code to shipment ref_order_code"
                )
            if "company_code" in metric.get("filters", {}).get("shipment_header_key", []):
                owner_correlated = any(
                    isinstance(unwrap_parens(term), exp.EQ)
                    and isinstance(unwrap_parens(term).this, exp.Column)
                    and isinstance(unwrap_parens(term).expression, exp.Column)
                    and {str(c.table).casefold() for c in unwrap_parens(term).find_all(exp.Column)} == {order_alias, header_alias}
                    and len(list(unwrap_parens(term).find_all(exp.Column))) == 2
                    and all(c.name.casefold() == "company_code" for c in unwrap_parens(term).find_all(exp.Column))
                    for term in inner_terms
                )
                if not owner_correlated:
                    raise TemplateNotExecutableError("Sales-order EXISTS must correlate the same cargo owner")
            results.append({
                "id": "sales_order_non_multiplication",
                "passed": True,
                "engine": "sqlglot_contract_ast",
            })

        return results


class SqlSafetyValidator:
    """SQLGlot-first validation with a conservative standard-library fallback."""

    def validate(self, sql: str, *, allowed_sources: set[str]) -> dict[str, Any]:
        clean = strip_sql_comments(sql)
        masked = mask_sql_literals(clean)
        normalized_for_parser = re.sub(r"%\([A-Za-z_][A-Za-z0-9_]*\)s", "0", clean)

        checks: list[dict[str, Any]] = []
        parser_name = "stdlib_fallback"
        try:
            import sqlglot
            from sqlglot import expressions as exp
        except ImportError:
            self._fallback_statement_check(masked)
        else:
            try:
                statements = sqlglot.parse(normalized_for_parser, read="mysql")
            except Exception as exc:
                raise SqlSafetyError("SQLGlot could not parse the approved template", details={"error": str(exc)}) from exc
            if len(statements) != 1:
                raise SqlSafetyError("Exactly one SQL statement is required")
            root = statements[0]
            forbidden_types = (
                exp.Insert,
                exp.Update,
                exp.Delete,
                exp.Create,
                exp.Drop,
                exp.Alter,
                exp.Command,
            )
            if isinstance(root, forbidden_types) or any(root.find(node_type) for node_type in forbidden_types):
                raise SqlSafetyError("Only a read-only SELECT statement is permitted")
            if root.find(exp.Select) is None:
                raise SqlSafetyError("Only SELECT or WITH ... SELECT is permitted")
            if root.find(exp.Into) is not None or root.find(exp.Lock) is not None:
                raise SqlSafetyError("SELECT INTO and locking clauses are not permitted")
            dangerous_functions = []
            for function in root.find_all(exp.Anonymous):
                name = str(function.name or "").casefold()
                if name in DANGEROUS_FUNCTIONS:
                    dangerous_functions.append(name)
            if dangerous_functions:
                raise SqlSafetyError(
                    "Dangerous SQL functions are not permitted",
                    details={"functions": sorted(set(dangerous_functions))},
                )
            parser_name = "sqlglot"

        self._fallback_statement_check(masked)
        checks.append({"id": "single_statement_and_read_only", "passed": True, "engine": parser_name})

        if parser_name == "sqlglot":
            source_tables = self._extract_source_tables_ast(root, exp)
        else:
            source_tables = self._extract_source_tables(masked)
        unknown = sorted(set(source_tables) - {item.casefold() for item in allowed_sources})
        if unknown:
            raise SqlSafetyError(
                "SQL template references tables outside the metric contract",
                details={"unknown_sources": unknown},
            )
        checks.append(
            {
                "id": "source_table_allowlist",
                "passed": True,
                "source_tables": source_tables,
            }
        )
        checks.append({"id": "bound_parameters", "passed": True})
        return {"source_tables": source_tables, "checks": checks}

    @staticmethod
    def _fallback_statement_check(masked_sql: str) -> None:
        stripped = masked_sql.strip()
        semicolons = [index for index, char in enumerate(stripped) if char == ";"]
        if len(semicolons) > 1 or (semicolons and semicolons[0] != len(stripped) - 1):
            raise SqlSafetyError("Exactly one SQL statement is required")
        statement = stripped[:-1].rstrip() if stripped.endswith(";") else stripped
        if not re.match(r"^(SELECT|WITH)\b", statement, flags=re.IGNORECASE):
            raise SqlSafetyError("Only SELECT or WITH ... SELECT is permitted")
        tokens = set(re.findall(r"\b[A-Za-z_]+\b", statement.upper()))
        forbidden = sorted(tokens & FORBIDDEN_TOKENS)
        if forbidden:
            raise SqlSafetyError(
                "Forbidden SQL token detected",
                details={"tokens": forbidden},
            )

    @staticmethod
    def _extract_source_tables(masked_sql: str) -> list[str]:
        """Conservative fallback used only when SQLGlot is unavailable."""
        if SqlSafetyValidator._has_ambiguous_from_comma(masked_sql):
            raise SqlSafetyError(
                "SQL parser is required to validate comma-separated source tables"
            )
        cte_names = {
            match.group(1).casefold()
            for match in re.finditer(r"(?:\bWITH\b|,)\s*`?([A-Za-z_][A-Za-z0-9_]*)`?\s+AS\s*\(", masked_sql, flags=re.IGNORECASE)
        }
        relations = []
        for match in re.finditer(
            r"\b(?:FROM|JOIN)\s+((?:`?[A-Za-z_][A-Za-z0-9_]*`?\.)?`?[A-Za-z_][A-Za-z0-9_]*`?)",
            masked_sql,
            flags=re.IGNORECASE,
        ):
            relation = _normalized_relation(match.group(1))
            if relation in cte_names:
                continue
            if "." not in relation:
                raise SqlSafetyError(
                    "Physical tables must be fully qualified",
                    details={"table": relation},
                )
            relations.append(relation)
        return sorted(set(relations))

    @staticmethod
    def _has_ambiguous_from_comma(masked_sql: str) -> bool:
        """Detect a top-level comma in a FROM/JOIN source list for the fallback."""
        depth = 0
        in_source = False
        token = []
        for char in masked_sql:
            if char == "(":
                depth += 1
            elif char == ")":
                depth = max(0, depth - 1)
            if char.isalpha() or char == "_":
                token.append(char)
                continue
            word = "".join(token).upper()
            token.clear()
            if word in {"FROM", "JOIN"}:
                in_source = True
            elif word in {"WHERE", "GROUP", "ORDER", "HAVING", "LIMIT", "UNION", "QUALIFY"}:
                in_source = False
            if in_source and depth == 0 and char == ",":
                return True
        return False

    @staticmethod
    def _extract_source_tables_ast(root: Any, exp: Any) -> list[str]:
        """Extract every physical table from SQLGlot, including comma joins."""
        cte_names = {
            str(cte.alias_or_name).casefold()
            for cte in root.find_all(exp.CTE)
            if cte.alias_or_name
        }
        relations: list[str] = []
        for table in root.find_all(exp.Table):
            name = str(table.name or "").strip()
            db = str(table.db or "").strip()
            catalog = str(table.catalog or "").strip()
            if not db and not catalog and name.casefold() in cte_names:
                continue
            if not db and not catalog:
                raise SqlSafetyError(
                    "Physical tables must be fully qualified",
                    details={"table": name},
                )
            parts = [part for part in (catalog, db, name) if part]
            relation = ".".join(_normalized_relation(part) for part in parts)
            relations.append(relation)
        return sorted(set(relations))

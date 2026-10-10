from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
import re
import sys
from typing import Any

import yaml

try:
    from jsonschema import Draft202012Validator, FormatChecker
except ImportError:  # pragma: no cover - handled explicitly for local setup
    Draft202012Validator = None  # type: ignore[assignment]
    FormatChecker = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.release_integrity import manifest_integrity_errors  # noqa: E402

ROUTABLE_METRIC_STATUSES = {"active"}
TEMPLATE_VERIFIED_STATUS = "verified_template"
SQL_PARAMETER_RE = re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)")


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        value = yaml.safe_load(stream)
    if not isinstance(value, dict):
        raise ValueError("top-level YAML value must be a mapping")
    return value


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("top-level JSON value must be an object")
    return value


def relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def schema_errors(value: dict[str, Any], schema: dict[str, Any], path: Path) -> list[str]:
    if Draft202012Validator is None or FormatChecker is None:
        return [
            "jsonschema is not installed; run "
            "pip install -r requirements-contracts.txt before validation"
        ]
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = []
    for error in sorted(validator.iter_errors(value), key=lambda item: list(item.absolute_path)):
        location = ".".join(str(part) for part in error.absolute_path)
        prefix = f"{relative(path)}:{location}" if location else relative(path)
        errors.append(f"{prefix}: {error.message}")
    return errors


def strip_sql_comments(sql: str) -> str:
    without_blocks = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return re.sub(r"--[^\r\n]*", " ", without_blocks)


def split_ref(value: str) -> tuple[str, str]:
    if "@" not in value:
        raise ValueError(f"version-pinned reference required, got {value!r}")
    identifier, version = value.rsplit("@", 1)
    return identifier, version


def normalize_alias(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def extract_sql_tables(sql: str) -> set[str]:
    """Use the runtime AST validator so CI and execution share one allow-list gate."""
    try:
        from app.sql import SqlSafetyValidator
    except ImportError as exc:
        raise ValueError("runtime SQL validator is unavailable") from exc
    try:
        result = SqlSafetyValidator().validate(sql, allowed_sources=set())
    except Exception as exc:
        details = getattr(exc, "details", {})
        unknown = details.get("unknown_sources") if isinstance(details, dict) else None
        if unknown:
            return set(unknown)
        raise ValueError(f"SQL AST validation failed: {exc}") from exc
    return set(result["source_tables"])


def nested_value(document: Any, path: str) -> Any:
    value = document
    parts = path.lstrip("/").split("/") if path.startswith("/") else path.split(".")
    for part in parts:
        if not isinstance(value, dict) or part not in value:
            raise KeyError(path)
        value = value[part]
    return value


def validate_repository() -> tuple[list[str], list[str], dict[str, int]]:
    errors: list[str] = []
    warnings: list[str] = []
    stats = {
        "metric_versions": 0,
        "metric_ids": 0,
        "table_versions": 0,
        "table_ids": 0,
        "golden_cases": 0,
        "release_manifests": 0,
    }

    if Draft202012Validator is None:
        errors.append(
            "jsonschema is not installed; run "
            "pip install -r requirements-contracts.txt before validation"
        )
        return errors, warnings, stats

    try:
        metric_schema = load_json(ROOT / "schemas" / "metric-contract.schema.json")
        table_schema = load_json(ROOT / "schemas" / "source-table.schema.json")
        source_schema = load_json(ROOT / "schemas" / "data-source.schema.json")
        golden_schema = load_json(ROOT / "schemas" / "golden-question-suite.schema.json")
        release_schema = load_json(ROOT / "schemas" / "release-manifest.schema.json")
        for schema in (metric_schema, table_schema, source_schema, golden_schema, release_schema):
            Draft202012Validator.check_schema(schema)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"schema setup: {exc}")
        return errors, warnings, stats

    try:
        validation_rules = load_yaml(ROOT / "rules" / "validations.yaml").get("validations", {})
        validation_ids = set(validation_rules)
        release_policy = load_yaml(ROOT / "rules" / "release-policy.yaml")
        required_release_gates = set(release_policy.get("required_gates", []))
        semantic_assertions = load_yaml(
            ROOT / "rules" / "thread-derived-semantics.yaml"
        ).get("semantic_assertions", [])
        template_capabilities = load_yaml(ROOT / "app" / "template_capabilities.yaml").get(
            "metrics", {}
        )
    except (OSError, ValueError, yaml.YAMLError) as exc:
        errors.append(f"rule setup: {exc}")
        return errors, warnings, stats

    data_sources: dict[str, dict[str, Any]] = {}
    for path in sorted((ROOT / "sources").glob("*.yaml")):
        try:
            source = load_yaml(path)
            errors.extend(schema_errors(source, source_schema, path))
            source_id = source.get("id")
            if source_id in data_sources:
                errors.append(f"{relative(path)}: duplicate data source id {source_id}")
            elif isinstance(source_id, str):
                data_sources[source_id] = source
        except (OSError, ValueError, yaml.YAMLError) as exc:
            errors.append(f"{relative(path)}: {exc}")

    table_versions: dict[tuple[str, str], dict[str, Any]] = {}
    table_paths: dict[tuple[str, str], Path] = {}
    tables_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path in sorted((ROOT / "sources" / "tables").rglob("*.yaml")):
        try:
            table = load_yaml(path)
            errors.extend(schema_errors(table, table_schema, path))
            table_id = table.get("id")
            version = table.get("version")
            if not isinstance(table_id, str) or not isinstance(version, str):
                continue
            key = (table_id, version)
            if key in table_versions:
                errors.append(f"{relative(path)}: duplicate table version {table_id}@{version}")
                continue
            table_versions[key] = table
            table_paths[key] = path
            tables_by_id[table_id].append(table)

            if table.get("source_id") not in data_sources:
                errors.append(f"{relative(path)}: unknown source_id {table.get('source_id')}")
            fields = table.get("fields", {})
            field_names = set(fields) if isinstance(fields, dict) else set()
            for field in table.get("aggregate_key", []):
                if field not in field_names:
                    errors.append(f"{relative(path)}: aggregate_key field {field!r} is not declared")
            business_key = table.get("business_key")
            if isinstance(business_key, str):
                business_key_fields = [business_key]
            elif isinstance(business_key, list):
                business_key_fields = business_key
            else:
                business_key_fields = []
            for business_key_field in business_key_fields:
                if business_key_field not in field_names:
                    errors.append(
                        f"{relative(path)}: business_key field {business_key_field!r} is not declared"
                    )
            freshness = table.get("freshness")
            if isinstance(freshness, dict) and freshness.get("field") not in field_names:
                errors.append(
                    f"{relative(path)}: freshness field {freshness.get('field')!r} is not declared"
                )
            observation_path = table.get("metadata_sync", {}).get("observations_path", "")
            if observation_path and not str(observation_path).startswith("observations/"):
                errors.append(f"{relative(path)}: observations_path must stay under observations/")
        except (OSError, ValueError, yaml.YAMLError) as exc:
            errors.append(f"{relative(path)}: {exc}")

    for key, table in table_versions.items():
        join_to = table.get("join_to")
        if isinstance(join_to, dict) and join_to.get("table") not in tables_by_id:
            errors.append(
                f"{relative(table_paths[key])}: join target {join_to.get('table')!r} is not declared"
            )

    metric_versions: dict[tuple[str, str], dict[str, Any]] = {}
    metric_paths: dict[tuple[str, str], Path] = {}
    metrics_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    active_by_id: dict[str, list[str]] = defaultdict(list)
    aliases: dict[str, list[tuple[str, str, str, str]]] = defaultdict(list)
    for path in sorted((ROOT / "metrics").rglob("*.yaml")):
        try:
            metric = load_yaml(path)
            errors.extend(schema_errors(metric, metric_schema, path))
            metric_id = metric.get("id")
            version = metric.get("version")
            if not isinstance(metric_id, str) or not isinstance(version, str):
                continue
            key = (metric_id, version)
            if key in metric_versions:
                errors.append(f"{relative(path)}: duplicate metric version {metric_id}@{version}")
                continue
            metric_versions[key] = metric
            metric_paths[key] = path
            metrics_by_id[metric_id].append(metric)

            status = metric.get("status")
            approval = metric.get("approval", {})
            approval_state = approval.get("state") if isinstance(approval, dict) else None
            if status in ROUTABLE_METRIC_STATUSES:
                active_by_id[metric_id].append(version)
                if approval_state != "approved":
                    errors.append(f"{relative(path)}: active metric must have approval.state=approved")
            elif status == TEMPLATE_VERIFIED_STATUS:
                warnings.append(
                    f"{metric_id}@{version} is template-verified only; production routing requires active + approved"
                )
            if approval_state == "approved" and (
                not approval.get("approved_by") or not approval.get("approved_at")
            ):
                errors.append(f"{relative(path)}: approved contract requires approved_by and approved_at")
            if status in {"deprecated", "retired"} and not metric.get("validity", {}).get("effective_to"):
                errors.append(f"{relative(path)}: {status} contract requires validity.effective_to")

            for alias in metric.get("aliases", []):
                aliases[normalize_alias(str(alias))].append(
                    (
                        metric_id,
                        version,
                        str(metric.get("business_concept", "")),
                        str(metric.get("clarification", {}).get("ambiguity_group", "")),
                    )
                )

            declared_sources = set(metric.get("sources", {}).get("required", []))
            declared_sources.update(metric.get("sources", {}).get("conditional", []))
            for table_id in sorted(declared_sources):
                if table_id not in tables_by_id:
                    errors.append(f"{relative(path)}: unknown source table {table_id}")
            declared_field_names = {
                field
                for table_id in declared_sources
                for table in tables_by_id.get(table_id, [])
                for field in table.get("fields", {})
            }
            for parameter_name, parameter in metric.get("parameters", {}).items():
                if not isinstance(parameter, dict):
                    continue
                field_bindings = {
                    key: value
                    for key, value in parameter.items()
                    if key.endswith("_field") and isinstance(value, str)
                }
                component_fields = parameter.get("component_fields", {})
                if isinstance(component_fields, dict):
                    field_bindings.update(
                        {f"component_fields.{key}": value for key, value in component_fields.items()}
                    )
                for binding_name, field_name in field_bindings.items():
                    raw_field = str(field_name).rsplit(".", 1)[-1]
                    if raw_field not in declared_field_names:
                        errors.append(
                            f"{relative(path)}: parameter {parameter_name}.{binding_name} "
                            f"references undeclared field {field_name}"
                        )

            unknown_validations = set(metric.get("required_validations", [])) - validation_ids
            for validation_id in sorted(unknown_validations):
                errors.append(f"{relative(path)}: unknown validation {validation_id}")

            time_window = metric.get("time_window")
            if not isinstance(time_window, dict) or not time_window:
                errors.append(f"{relative(path)}: time_window must be a non-empty mapping")
            else:
                time_fields = [time_window.get("field"), time_window.get("freshness_field")]
                for time_field in (item for item in time_fields if item):
                    raw_field = str(time_field).rsplit(".", 1)[-1]
                    if raw_field not in declared_field_names:
                        errors.append(
                            f"{relative(path)}: time field {time_field!r} is absent from declared sources"
                        )

            if "source_freshness" in metric.get("required_validations", []):
                has_fresh_source = any(
                    any("freshness" in table for table in tables_by_id.get(table_id, []))
                    for table_id in declared_sources
                )
                if not has_fresh_source:
                    errors.append(
                        f"{relative(path)}: source_freshness validation has no source freshness contract"
                    )

            execution = metric.get("execution", {})
            execution_mode = execution.get("mode") if isinstance(execution, dict) else None
            execution_template = execution.get("template") if isinstance(execution, dict) else None
            legacy_template = metric.get("template")
            if legacy_template and execution_template and legacy_template != execution_template:
                errors.append(f"{relative(path)}: template and execution.template disagree")
            template_name = execution_template or legacy_template
            if execution_mode == "sql_template" and not template_name:
                errors.append(f"{relative(path)}: sql_template execution requires a template")
            if template_name:
                template_path = ROOT / str(template_name)
                if not template_path.is_file():
                    errors.append(f"{relative(path)}: missing template {template_name}")
                elif execution_mode == "sql_template":
                    sql = template_path.read_text(encoding="utf-8")
                    sql_without_comments = strip_sql_comments(sql).strip()
                    statements = [part.strip() for part in sql_without_comments.split(";") if part.strip()]
                    if len(statements) != 1:
                        errors.append(
                            f"{relative(template_path)}: expected one SQL statement, got {len(statements)}"
                        )
                    elif not re.match(r"^(SELECT|WITH)\b", statements[0], re.IGNORECASE):
                        errors.append(f"{relative(template_path)}: statement must start with SELECT or WITH")

                    forbidden_keywords = {
                        str(word).upper()
                        for source in data_sources.values()
                        for word in source.get("forbidden_keywords", [])
                    }
                    for keyword in sorted(forbidden_keywords):
                        if re.search(rf"\b{re.escape(keyword)}\b", sql_without_comments, re.IGNORECASE):
                            errors.append(f"{relative(template_path)}: forbidden keyword {keyword}")

                    try:
                        referenced_tables = extract_sql_tables(sql_without_comments)
                    except ValueError as exc:
                        errors.append(f"{relative(template_path)}: {exc}")
                        referenced_tables = set()
                    for table_id in sorted(referenced_tables - set(tables_by_id)):
                        errors.append(f"{relative(template_path)}: undeclared table reference {table_id}")
                    for table_id in sorted(referenced_tables - declared_sources):
                        errors.append(
                            f"{relative(template_path)}: table {table_id} is not declared by {metric_id}"
                        )
                    template_parameters = set(SQL_PARAMETER_RE.findall(sql_without_comments))
                    unknown_parameters = template_parameters - set(metric.get("parameters", {}))
                    for parameter in sorted(unknown_parameters):
                        errors.append(
                            f"{relative(template_path)}: parameter :{parameter} is not declared by {metric_id}"
                        )
                    declared_statuses = metric.get("filters", {}).get("completion_statuses")
                    if isinstance(declared_statuses, list) and all(
                        isinstance(value, int) for value in declared_statuses
                    ):
                        status_match = re.search(
                            r"(?:\b[A-Za-z_][A-Za-z0-9_]*\.)?status\s+IN\s*\(([^)]*)\)",
                            sql_without_comments,
                            re.IGNORECASE,
                        )
                        if status_match is None:
                            errors.append(
                                f"{relative(template_path)}: no status IN predicate matches the metric contract"
                            )
                        else:
                            template_statuses = [
                                int(value)
                                for value in re.findall(r"(?<![A-Za-z0-9_])\d+", status_match.group(1))
                            ]
                            if template_statuses != declared_statuses:
                                errors.append(
                                    f"{relative(template_path)}: status list {template_statuses!r} "
                                    f"does not match {metric_id}@{version} {declared_statuses!r}"
                                )
            # Source-only composites explicitly declare []; omission is still an error.
            if execution_mode == "composite" and "depends_on" not in metric:
                errors.append(f"{relative(path)}: composite metric requires depends_on")
        except (OSError, ValueError, yaml.YAMLError) as exc:
            errors.append(f"{relative(path)}: {exc}")

    for metric_id, versions in active_by_id.items():
        if len(versions) > 1:
            errors.append(f"metrics: multiple active versions for {metric_id}: {sorted(versions)}")

    for alias, refs in aliases.items():
        unique_metric_ids = {item[0] for item in refs}
        if len(unique_metric_ids) <= 1:
            continue
        concepts = {item[2] for item in refs}
        ambiguity_groups = {item[3] for item in refs}
        if len(concepts) != 1 or len(ambiguity_groups) != 1 or "" in ambiguity_groups:
            errors.append(
                f"metrics: alias {alias!r} collides across incompatible contracts: "
                + ", ".join(f"{item[0]}@{item[1]}" for item in refs)
            )

    dependency_graph: dict[str, set[str]] = defaultdict(set)
    for key, metric in metric_versions.items():
        path = metric_paths[key]
        dependencies = metric.get("depends_on", [])
        dependency_versions = metric.get("dependency_versions", {})
        if set(dependencies) != set(dependency_versions):
            errors.append(f"{relative(path)}: dependency_versions keys must exactly match depends_on")
        dependency_source_union: set[str] = set()
        for dependency_id in dependencies:
            dependency_graph[metric["id"]].add(dependency_id)
            dependency_version = dependency_versions.get(dependency_id)
            dependency_key = (dependency_id, dependency_version)
            if dependency_key not in metric_versions:
                errors.append(f"{relative(path)}: unknown dependency {dependency_id}@{dependency_version}")
                continue
            dependency = metric_versions[dependency_key]
            dependency_source_union.update(dependency.get("sources", {}).get("required", []))
            dependency_source_union.update(dependency.get("sources", {}).get("conditional", []))
        declared_sources = set(metric.get("sources", {}).get("required", []))
        declared_sources.update(metric.get("sources", {}).get("conditional", []))
        for missing_source in sorted(dependency_source_union - declared_sources):
            errors.append(
                f"{relative(path)}: dependency source {missing_source} is not declared by composite metric"
            )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(metric_id: str) -> None:
        if metric_id in visiting:
            errors.append(f"metrics: dependency cycle detected at {metric_id}")
            return
        if metric_id in visited:
            return
        visiting.add(metric_id)
        for dependency_id in dependency_graph.get(metric_id, set()):
            visit(dependency_id)
        visiting.remove(metric_id)
        visited.add(metric_id)

    for metric_id in dependency_graph:
        visit(metric_id)

    if not isinstance(semantic_assertions, list):
        errors.append("rules/thread-derived-semantics.yaml: semantic_assertions must be a list")
    else:
        for assertion in semantic_assertions:
            if not isinstance(assertion, dict):
                errors.append("rules/thread-derived-semantics.yaml: assertion must be a mapping")
                continue
            assertion_id = str(assertion.get("id") or "unnamed")
            try:
                metric_key = split_ref(str(assertion.get("metric_ref") or ""))
            except ValueError as exc:
                errors.append(f"semantic assertion {assertion_id}: {exc}")
                continue
            metric = metric_versions.get(metric_key)
            if metric is None:
                errors.append(
                    f"semantic assertion {assertion_id}: unknown metric {assertion.get('metric_ref')}"
                )
                continue
            path = str(assertion.get("path") or "")
            try:
                actual = nested_value(metric, path)
            except KeyError:
                errors.append(f"semantic assertion {assertion_id}: missing path {path}")
                continue
            expected = assertion.get("expected")
            operator = assertion.get("operator")
            if operator == "equals" and actual != expected:
                errors.append(
                    f"semantic assertion {assertion_id}: expected {expected!r}, got {actual!r}"
                )
            elif operator == "contains_all":
                if not isinstance(actual, list) or not isinstance(expected, list):
                    errors.append(
                        f"semantic assertion {assertion_id}: contains_all requires list values"
                    )
                else:
                    missing = [item for item in expected if item not in actual]
                    if missing:
                        errors.append(
                            f"semantic assertion {assertion_id}: missing values {missing!r}"
                        )
            elif operator not in {"equals", "contains_all"}:
                errors.append(
                    f"semantic assertion {assertion_id}: unsupported operator {operator!r}"
                )

    if not isinstance(template_capabilities, dict):
        errors.append("app/template_capabilities.yaml: metrics must be a mapping")
    else:
        for ref in template_capabilities:
            try:
                metric_key = split_ref(str(ref))
            except ValueError as exc:
                errors.append(f"app/template_capabilities.yaml: {exc}")
                continue
            if metric_key not in metric_versions:
                errors.append(
                    f"app/template_capabilities.yaml: unknown metric capability ref {ref}"
                )

    profiles: dict[str, dict[str, Any]] = {}
    for path in sorted((ROOT / "profiles").rglob("*.yaml")):
        try:
            profile = load_yaml(path)
            profile_id = profile.get("id")
            if not isinstance(profile_id, str):
                errors.append(f"{relative(path)}: profile id must be a string")
                continue
            if profile_id in profiles:
                errors.append(f"{relative(path)}: duplicate profile id {profile_id}")
            profiles[profile_id] = profile
            sku_table = profile.get("filters", {}).get("sku", {}).get("table")
            if sku_table and sku_table not in tables_by_id:
                errors.append(f"{relative(path)}: unknown profile SKU table {sku_table}")
            warehouse_status = profile.get("filters", {}).get("warehouse", {}).get("status")
            if isinstance(warehouse_status, str) and warehouse_status.startswith("pending"):
                warnings.append(
                    f"profile {profile_id} has unresolved warehouse scope ({warehouse_status}); runtime must clarify or block"
                )
        except (OSError, ValueError, yaml.YAMLError) as exc:
            errors.append(f"{relative(path)}: {exc}")

    golden_suites: dict[tuple[str, str], dict[str, Any]] = {}
    golden_case_ids: set[str] = set()
    for path in sorted((ROOT / "tests" / "golden_questions").rglob("*.yaml")):
        try:
            suite = load_yaml(path)
            errors.extend(schema_errors(suite, golden_schema, path))
            suite_id = suite.get("suite_id")
            version = suite.get("version")
            if not isinstance(suite_id, str) or not isinstance(version, str):
                continue
            suite_key = (suite_id, version)
            if suite_key in golden_suites:
                errors.append(f"{relative(path)}: duplicate golden suite {suite_id}@{version}")
            golden_suites[suite_key] = suite
            for case in suite.get("cases", []):
                case_id = case.get("id")
                if case_id in golden_case_ids:
                    errors.append(f"{relative(path)}: duplicate golden case id {case_id}")
                elif isinstance(case_id, str):
                    golden_case_ids.add(case_id)
                expected = case.get("expected", {})
                action = expected.get("action")
                metric_id = expected.get("metric_id")
                metric_version = expected.get("metric_version")
                if action == "route" and (metric_id, metric_version) not in metric_versions:
                    errors.append(
                        f"{relative(path)}:{case_id}: unknown route target {metric_id}@{metric_version}"
                    )
                referenced_metric_ids = set(expected.get("candidate_metric_ids", []))
                referenced_metric_ids.update(expected.get("forbidden_metric_ids", []))
                for referenced_id in sorted(referenced_metric_ids - set(metrics_by_id)):
                    errors.append(f"{relative(path)}:{case_id}: unknown metric reference {referenced_id}")
                if metric_id and metric_id in expected.get("forbidden_metric_ids", []):
                    errors.append(f"{relative(path)}:{case_id}: route target is also forbidden")
                profile_id = expected.get("profile_id")
                if profile_id and profile_id not in profiles:
                    errors.append(f"{relative(path)}:{case_id}: unknown profile {profile_id}")
                if action == "route" and (metric_id, metric_version) in metric_versions:
                    metric_parameters = set(metric_versions[(metric_id, metric_version)].get("parameters", {}))
                    unknown_parameters = set(expected.get("parameters", {})) - metric_parameters
                    for parameter in sorted(unknown_parameters):
                        errors.append(
                            f"{relative(path)}:{case_id}: expected parameter {parameter} is not in {metric_id}"
                        )
                if case.get("test_level") == "router_smoke":
                    if action != "route":
                        errors.append(f"{relative(path)}:{case_id}: router_smoke must expect route")
                    if expected.get("parameters"):
                        errors.append(
                            f"{relative(path)}:{case_id}: parameter assertions require contract_only or end_to_end"
                        )
        except (OSError, ValueError, yaml.YAMLError) as exc:
            errors.append(f"{relative(path)}: {exc}")

    release_ids: set[str] = set()
    knowledge_versions: set[str] = set()
    for path in sorted((ROOT / "releases").rglob("*.yaml")):
        try:
            manifest = load_yaml(path)
            errors.extend(schema_errors(manifest, release_schema, path))
            release_id = manifest.get("release_id")
            knowledge_version = manifest.get("knowledge_version")
            if release_id in release_ids:
                errors.append(f"{relative(path)}: duplicate release_id {release_id}")
            elif isinstance(release_id, str):
                release_ids.add(release_id)
            if knowledge_version in knowledge_versions:
                errors.append(f"{relative(path)}: duplicate knowledge_version {knowledge_version}")
            elif isinstance(knowledge_version, str):
                knowledge_versions.add(knowledge_version)

            selected_metric_versions: dict[str, str] = {}
            for ref in manifest.get("contracts", {}).get("metrics", []):
                try:
                    ref_key = split_ref(ref)
                    if ref_key not in metric_versions:
                        errors.append(f"{relative(path)}: unknown metric release ref {ref}")
                    else:
                        selected_metric_versions[ref_key[0]] = ref_key[1]
                except ValueError as exc:
                    errors.append(f"{relative(path)}: {exc}")
            for metric_id, metric_version in selected_metric_versions.items():
                metric = metric_versions.get((metric_id, metric_version), {})
                for dependency_id, dependency_version in metric.get(
                    "dependency_versions", {}
                ).items():
                    selected_dependency_version = selected_metric_versions.get(dependency_id)
                    if selected_dependency_version is None:
                        errors.append(
                            f"{relative(path)}: release metric {metric_id}@{metric_version} "
                            f"omits dependency {dependency_id}@{dependency_version}"
                        )
                    elif selected_dependency_version != dependency_version:
                        errors.append(
                            f"{relative(path)}: release selects {dependency_id}@"
                            f"{selected_dependency_version}, but {metric_id}@{metric_version} "
                            f"pins {dependency_id}@{dependency_version}"
                        )
            for ref in manifest.get("contracts", {}).get("sources", []):
                try:
                    ref_key = split_ref(ref)
                    if ref_key not in table_versions:
                        errors.append(f"{relative(path)}: unknown source release ref {ref}")
                except ValueError as exc:
                    errors.append(f"{relative(path)}: {exc}")
            for ref in manifest.get("golden_suites", []):
                try:
                    ref_key = split_ref(ref)
                    if ref_key not in golden_suites:
                        errors.append(f"{relative(path)}: unknown golden suite release ref {ref}")
                except ValueError as exc:
                    errors.append(f"{relative(path)}: {exc}")

            missing_gates = required_release_gates - set(manifest.get("required_gates", []))
            for gate in sorted(missing_gates):
                errors.append(f"{relative(path)}: missing required release gate {gate}")

            if manifest.get("status") == "released" or "integrity" in manifest:
                for integrity_error in manifest_integrity_errors(ROOT, manifest):
                    errors.append(f"{relative(path)}: {integrity_error}")
            else:
                warnings.append(
                    f"{relative(path)}: candidate has no integrity block; run "
                    f"python tools/update_release_integrity.py {relative(path)}"
                )

            if manifest.get("status") == "released":
                if manifest.get("approval", {}).get("state") != "approved" or not manifest.get("released_at"):
                    errors.append(f"{relative(path)}: released manifest requires approval and released_at")
                for ref in manifest.get("contracts", {}).get("metrics", []):
                    try:
                        metric = metric_versions.get(split_ref(ref), {})
                    except ValueError:
                        continue
                    if metric.get("status") != "active" or metric.get("approval", {}).get("state") != "approved":
                        errors.append(
                            f"{relative(path)}: released manifest contains production-ineligible metric {ref}"
                        )
        except (OSError, ValueError, yaml.YAMLError) as exc:
            errors.append(f"{relative(path)}: {exc}")

    stats.update(
        metric_versions=len(metric_versions),
        metric_ids=len(metrics_by_id),
        table_versions=len(table_versions),
        table_ids=len(tables_by_id),
        golden_cases=len(golden_case_ids),
        release_manifests=len(release_ids),
    )
    return errors, warnings, stats


def main() -> int:
    errors, warnings, stats = validate_repository()
    if errors:
        print("Knowledge-base validation failed:")
        print("\n".join(f"- {error}" for error in errors))
        if warnings:
            print("Warnings:")
            print("\n".join(f"- {warning}" for warning in warnings))
        return 1
    print(
        "Knowledge-base validation passed: "
        f"{stats['metric_versions']} metric versions ({stats['metric_ids']} IDs), "
        f"{stats['table_versions']} table versions ({stats['table_ids']} IDs), "
        f"{stats['golden_cases']} golden cases, "
        f"{stats['release_manifests']} release manifests"
    )
    if warnings:
        print("Warnings:")
        print("\n".join(f"- {warning}" for warning in warnings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Read-only contract differences and conservative declared-dependency impact."""

from collections import defaultdict, deque
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
import json
import math
import re


_MISSING = object()
_ID = re.compile(r"[A-Za-z0-9_.-]{1,200}\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")


def _validate(value, depth=0):
    if depth > 64:
        raise ValueError("invalid_contract_value")
    if value is None or type(value) in (str, bool, int, date, datetime):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is Decimal and value.is_finite():
        return
    if isinstance(value, list):
        for item in value:
            _validate(item, depth + 1)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _validate(item, depth + 1)
        return
    raise ValueError("invalid_contract_value")


def _typed(value):
    if isinstance(value, dict):
        return ["mapping", [[key, _typed(value[key])] for key in sorted(value)]]
    if isinstance(value, list):
        return ["list", [_typed(item) for item in value]]
    if type(value) in (date, datetime):
        return [type(value).__name__, value.isoformat()]
    if type(value) is Decimal:
        return ["decimal", str(value)]
    return [type(value).__name__, value]


def _encoded(value):
    return json.dumps(_typed(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _output_value(value):
    if isinstance(value, dict):
        return {key: _output_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_output_value(item) for item in value]
    if type(value) in (date, datetime):
        return {"yaml_type": type(value).__name__, "iso8601": value.isoformat()}
    if type(value) is Decimal:
        return {"numeric_type": "decimal", "value": str(value)}
    return value


class KnowledgeSnapshot:
    def __init__(self, *, metrics, sources, templates=None):
        self.contracts = {}
        for kind, values in (("metric", metrics), ("source", sources)):
            if not isinstance(values, (list, tuple)):
                raise ValueError("invalid_contract_collection")
            for value in values:
                _validate(value)
                if not isinstance(value, dict) or not isinstance(value.get("id"), str) or not _ID.fullmatch(value["id"]):
                    raise ValueError("invalid_contract_identity")
                if not isinstance(value.get("version"), str) or not value["version"]:
                    raise ValueError("invalid_contract_version")
                key = f"{kind}:{value['id']}"
                if key in self.contracts:
                    raise ValueError("duplicate_selected_contract")
                self.contracts[key] = deepcopy(value)
        for name, digest in (templates or {}).items():
            if not isinstance(name, str) or not name or not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
                raise ValueError("invalid_template_digest")
            self.contracts[f"template:{name}"] = {"id": name, "sha256": digest}


def _category(path, kind):
    if kind == "template":
        return "implementation"
    root = path.split("/")[1]
    if root == "updated_at":
        return "metadata"
    if root in {"approval", "status"}:
        return "governance"
    if root in {"version", "contract_schema_version"}:
        return "version"
    return "semantic"


def _differences(before, after, path=""):
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(before.keys() | after.keys()):
            escaped = key.replace("~", "~0").replace("/", "~1")
            yield from _differences(before.get(key, _MISSING), after.get(key, _MISSING), path + "/" + escaped)
    elif before is _MISSING or after is _MISSING or _encoded(before) != _encoded(after):
        yield path or "/", before, after


def _graph(snapshot, side):
    edges = defaultdict(set)
    issues = []

    def issue(code, key, dependency=None):
        value = {"side": side, "code": code, "contract": key}
        if dependency is not None:
            value["dependency"] = dependency
        issues.append(value)

    def link(dependency, consumer, missing_code):
        edges[dependency].add(consumer)
        if dependency not in snapshot.contracts:
            issue(missing_code, consumer, dependency)

    for key, value in sorted(snapshot.contracts.items()):
        if not key.startswith("metric:"):
            continue
        execution = value.get("execution", {})
        if not isinstance(execution, dict):
            issue("invalid_execution", key)
            execution = {}
        validations = value.get("required_validations", [])
        if not isinstance(validations, list) or any(not isinstance(check, str) or not check for check in validations):
            issue("invalid_required_validations", key)
        if execution.get("mode") == "composite" and "depends_on" not in value:
            issue("missing_dependency_declaration", key)
        dependencies = value.get("depends_on", [])
        pins = value.get("dependency_versions", {})
        if not isinstance(dependencies, list) or any(not isinstance(d, str) for d in dependencies):
            issue("invalid_dependencies", key)
            dependencies = []
        if not isinstance(pins, dict) or set(pins) != set(dependencies) or len(set(dependencies)) != len(dependencies):
            issue("invalid_dependency_pins", key)
            pins = pins if isinstance(pins, dict) else {}
        for dependency in dependencies:
            target = "metric:" + dependency
            link(target, key, "missing_metric")
            actual = snapshot.contracts.get(target)
            if actual and pins.get(dependency) != actual["version"]:
                issue("dependency_version_mismatch", key, target)
        sources = value.get("sources", {})
        if not isinstance(sources, dict):
            issue("invalid_sources", key)
            sources = {}
        for references in sources.values():
            if not isinstance(references, list) or any(not isinstance(ref, str) for ref in references):
                issue("invalid_sources", key)
                continue
            for reference in references:
                link("source:" + reference, key, "missing_source")
        template = execution.get("template", value.get("template", _MISSING))
        if template is not _MISSING:
            if isinstance(template, str) and template:
                link("template:" + template, key, "missing_template")
            else:
                issue("invalid_template", key)

    # Detect cycles in each version separately, not in the union of two valid graphs.
    nodes = set(snapshot.contracts) | set(edges)
    indegrees = {node: 0 for node in nodes}
    for consumers in edges.values():
        for consumer in consumers:
            indegrees[consumer] += 1
    queue = deque(sorted(node for node, degree in indegrees.items() if not degree))
    while queue:
        for consumer in sorted(edges[queue.popleft()]):
            indegrees[consumer] -= 1
            if not indegrees[consumer]:
                queue.append(consumer)
    remaining = sorted(node for node, degree in indegrees.items() if degree)
    if remaining:
        issues.append({"side": side, "code": "dependency_cycle", "affected_contracts": remaining})
    return edges, issues


def assess_changes(before, after, *, include_values=False):
    """Diff selected contracts; neither equivalence proof nor publication approval."""
    old_edges, old_issues = _graph(before, "before")
    new_edges, new_issues = _graph(after, "after")
    edges = {key: old_edges.get(key, set()) | new_edges.get(key, set()) for key in old_edges.keys() | new_edges.keys()}
    changes = []
    affected = set()
    semantic = False
    for key in sorted(before.contracts.keys() | after.contracts.keys()):
        old, new = before.contracts.get(key), after.contracts.get(key)
        kind, identifier = key.split(":", 1)
        fields = []
        for path, previous, current in _differences(old or {}, new or {}):
            field = {"path": path, "category": _category(path, kind),
                     "before_exists": previous is not _MISSING, "after_exists": current is not _MISSING}
            for label, value in (("before", previous), ("after", current)):
                field[label + "_sha256"] = None if value is _MISSING else sha256(_encoded(value).encode()).hexdigest()
                if include_values and value is not _MISSING:
                    field[label] = _output_value(value)
            fields.append(field)
        if not fields:
            continue
        meaningful = any(field["category"] != "metadata" for field in fields)
        semantic_change = any(field["category"] in {"semantic", "implementation"} for field in fields)
        semantic |= semantic_change
        impacted = set()
        if meaningful:
            seen = set()
            queue = deque([key])
            while queue:
                node = queue.popleft()
                if node in seen:
                    continue
                seen.add(node)
                if node.startswith("metric:"):
                    impacted.add(node.removeprefix("metric:"))
                queue.extend(sorted(edges.get(node, ())))
            affected.update(impacted)
        changes.append({"kind": kind, "id": identifier,
                        "operation": "added" if old is None else "removed" if new is None else "modified",
                        "before_version": old.get("version") if old else None,
                        "after_version": new.get("version") if new else None,
                        "fields": fields, "affected_metrics": sorted(impacted),
                        "requires_version_change": bool(kind != "template" and semantic_change and old and new and old.get("version") == new.get("version"))})
    checks = set()
    if affected:
        checks.update(("business_definition_review", "dependency_regression", "scope_and_time_boundaries",
                       "quantity_reconciliation", "document_count_deduplication", "grain_and_join_fanout",
                       "source_freshness", "null_and_zero_handling", "implementation_contract_alignment"))
        for snapshot in (before, after):
            for identifier in affected:
                metric = snapshot.contracts.get("metric:" + identifier, {})
                validations = metric.get("required_validations", [])
                if isinstance(validations, list):
                    checks.update(value for value in validations if isinstance(value, str))
                if identifier.startswith("inventory.") or str(metric.get("business_concept", "")).startswith("inventory."):
                    checks.add("inventory_component_recalculation")
    if any(change["kind"] == "source" and any(f["category"] == "semantic" for f in change["fields"]) for change in changes):
        checks.add("source_schema_compatibility")
    if any(change["kind"] == "template" and change["affected_metrics"] for change in changes):
        checks.add("consumer_version_review")
    issues = old_issues + new_issues
    return {"state": "assessed_not_executed", "coverage": "declared_contract_graph_only",
            "assessment_complete": not issues, "publication_authorized": False,
            "historical_comparability": "requires_review" if semantic else "not_established",
            "changes": changes, "affected_metrics": sorted(affected), "required_checks": sorted(checks),
            "issues": issues,
            "limitations": ["Undeclared runtime consumers and arbitrary SQL expressions are not inferred.",
                            "Profiles, aliases outside contracts, global rules, routing and Python implementations are not compared.",
                            "No database query, numeric reconciliation, semantic equivalence proof or approval was performed."]}

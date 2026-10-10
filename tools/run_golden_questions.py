from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any

from validate_knowledge_base import ROOT, load_yaml, normalize_alias, validate_repository

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.agent import AgentWorkflow  # noqa: E402
from app.service import QueryService  # noqa: E402
from app.settings import RuntimeSettings  # noqa: E402


def _response_parameters(response: dict[str, Any]) -> dict[str, Any]:
    if isinstance(response.get("plan"), dict):
        return response["plan"].get("parameters", {})
    for key in ("parameters", "extracted_parameters"):
        if isinstance(response.get(key), dict):
            return response[key]
    return {}


def _equivalent_value(actual: Any, expected: Any) -> bool:
    if actual == expected:
        return True
    if isinstance(actual, datetime) and isinstance(expected, str):
        actual = actual.isoformat()
    elif isinstance(actual, date) and isinstance(expected, str):
        return actual.isoformat() == expected
    if isinstance(actual, str) and isinstance(expected, str):
        try:
            actual_dt = datetime.fromisoformat(actual)
            expected_dt = datetime.fromisoformat(expected)
        except ValueError:
            return False
        if expected_dt.tzinfo is not None:
            expected_dt = expected_dt.replace(tzinfo=None)
        if actual_dt.tzinfo is not None:
            actual_dt = actual_dt.replace(tzinfo=None)
        return actual_dt == expected_dt
    return False


def _check_end_to_end(
    case: dict[str, Any],
    response: dict[str, Any],
    service: QueryService,
) -> list[str]:
    expected = case["expected"]
    failures: list[str] = []
    raw_action = response.get("action")
    actual_action = "route" if raw_action in {"planned", "blocked"} and response.get(
        "metric_id"
    ) else raw_action
    if raw_action == "planned":
        actual_action = "route"
    if actual_action != expected["action"]:
        failures.append(f"action expected {expected['action']}, got {raw_action}")

    plan = response.get("plan") if isinstance(response.get("plan"), dict) else {}
    actual_metric_id = plan.get("metric_id") or response.get("metric_id")
    expected_metric_id = expected.get("metric_id")
    if expected_metric_id and actual_metric_id != expected_metric_id:
        failures.append(f"metric expected {expected_metric_id}, got {actual_metric_id}")
    if expected.get("metric_version") and actual_metric_id:
        actual_version = plan.get("metric_version") or service.get_metric(actual_metric_id).get(
            "version"
        )
        if str(actual_version) != str(expected["metric_version"]):
            failures.append(
                f"metric version expected {expected['metric_version']}, got {actual_version}"
            )

    for key in ("reason_code", "clarification_key", "profile_id"):
        if key in expected and response.get(key) != expected[key]:
            failures.append(f"{key} expected {expected[key]!r}, got {response.get(key)!r}")
    for key in ("candidate_metric_ids", "unavailable_variants"):
        if key in expected and set(response.get(key, [])) != set(expected[key]):
            failures.append(f"{key} expected {expected[key]}, got {response.get(key, [])}")

    parameters = _response_parameters(response)
    for name, expected_value in expected.get("parameters", {}).items():
        actual_value = parameters.get(name)
        if not _equivalent_value(actual_value, expected_value):
            failures.append(
                f"parameter {name} expected {expected_value!r}, got {actual_value!r}"
            )

    forbidden = set(expected.get("forbidden_metric_ids", []))
    if actual_metric_id in forbidden:
        failures.append(f"selected forbidden metric {actual_metric_id}")
    previous = case.get("context", {}).get("previous_request") or {}
    previous_parameters = previous.get("parameters", {}) if isinstance(previous, dict) else {}
    for name in expected.get("must_not_inherit", []):
        if name in parameters and name in previous_parameters and parameters[name] == previous_parameters[name]:
            failures.append(f"parameter {name} was inherited from previous request")
    return failures


def main() -> int:
    validation_errors, _, _ = validate_repository()
    if validation_errors:
        print("Golden-question runner blocked by knowledge-base validation errors:")
        print("\n".join(f"- {error}" for error in validation_errors))
        return 1

    failures: list[str] = []
    router_executed = 0
    end_to_end_executed = 0
    contract_only = 0
    with TemporaryDirectory(prefix="query-agent-golden-") as directory:
        service = QueryService(
            RuntimeSettings(
                project_root=ROOT,
                executor_mode="mock",
                audit_db_path=Path(directory) / "audit.sqlite3",
            )
        )
        agent = AgentWorkflow(service)
        alias_index: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for metric in service.knowledge.metrics():
            for alias in metric.get("aliases", []):
                alias_index[normalize_alias(str(alias))].append(
                    (metric["id"], metric["version"])
                )
        for path in sorted((ROOT / "tests" / "golden_questions").rglob("*.yaml")):
            suite = load_yaml(path)
            for case in suite.get("cases", []):
                level = case.get("test_level")
                if level == "contract_only":
                    contract_only += 1
                    continue
                if level == "end_to_end":
                    end_to_end_executed += 1
                    now_value = case.get("context", {}).get("now")
                    now = datetime.fromisoformat(now_value) if now_value else None
                    response = agent.invoke(question=str(case["question"]), now=now)
                    for failure in _check_end_to_end(case, response, service):
                        failures.append(f"{case['id']}: {failure}")
                    continue

                router_executed += 1
                question = normalize_alias(str(case["question"]))
                matches: list[tuple[int, str, str]] = []
                for alias, refs in alias_index.items():
                    if alias and alias in question:
                        matches.extend((len(alias), metric_id, version) for metric_id, version in refs)
                if not matches:
                    failures.append(f"{case['id']}: no metric alias matched")
                    continue
                longest = max(item[0] for item in matches)
                winners = {(item[1], item[2]) for item in matches if item[0] == longest}
                expected = (case["expected"]["metric_id"], case["expected"]["metric_version"])
                if winners != {expected}:
                    failures.append(
                        f"{case['id']}: expected {expected[0]}@{expected[1]}, got "
                        + ", ".join(f"{metric_id}@{version}" for metric_id, version in sorted(winners))
                    )

    if router_executed == 0:
        failures.append("no router_smoke cases were executed")
    if end_to_end_executed == 0:
        failures.append("no end_to_end cases were executed")
    if failures:
        print("Golden-question smoke tests failed:")
        print("\n".join(f"- {failure}" for failure in failures))
        return 1
    print(
        f"Golden-question tests passed: {router_executed} deterministic router cases, "
        f"{end_to_end_executed} end-to-end Agent cases, {contract_only} contract-only cases"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

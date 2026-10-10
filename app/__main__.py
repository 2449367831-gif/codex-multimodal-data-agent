from __future__ import annotations

import argparse
import json

from .serialization import to_json_value
from .service import QueryService


def _process_only_agent_output(value: object) -> dict[str, object]:
    """Keep the legacy CLI from becoming a SQL/result disclosure channel."""

    payload = value if isinstance(value, dict) else {}
    action = str(payload.get("action") or "blocked").casefold()
    if action in {"executed", "planned", "success", "completed"}:
        state = "completed"
        label = "取数已完成"
    elif action in {"clarify", "clarification_required"}:
        state = "needs_selection"
        label = "需要选择业务口径"
    elif action in {"failed", "error"}:
        state = "failed"
        label = "本次取数未完成"
    else:
        state = "blocked"
        label = "当前请求未完成"
    return {
        "status": state,
        "process": [
            {"id": "context", "state": "completed"},
            {"id": "query", "state": state if state != "needs_selection" else "blocked"},
        ],
        "outcome": {"state": state, "label": label},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Governed Doris query runtime")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("health")
    subparsers.add_parser("metrics")
    route_parser = subparsers.add_parser("route")
    route_parser.add_argument("question")
    agent_parser = subparsers.add_parser("agent")
    agent_parser.add_argument("question")
    agent_parser.add_argument("--execute", action="store_true")
    agent_parser.add_argument("--include-sql", action="store_true")
    args = parser.parse_args()

    service = QueryService()
    if args.command == "health":
        value = service.health()
    elif args.command == "metrics":
        value = service.list_metrics()
    elif args.command == "route":
        value = service.router.route(args.question).as_dict()
    else:
        from .agent import AgentWorkflow

        value = AgentWorkflow(service).invoke(
            question=args.question,
            execute=args.execute,
            include_sql=args.include_sql,
        )
    # The historical workflow remains available for local regression, but its
    # command-line surface is process-only.  Detailed SQL/result inspection is
    # deliberately not a supported user feature.
    if args.command == "agent":
        value = _process_only_agent_output(value)
    print(json.dumps(to_json_value(value), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

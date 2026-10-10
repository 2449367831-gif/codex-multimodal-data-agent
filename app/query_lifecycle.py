"""Single request lifecycle for model-planned Doris queries.

The public chat API deliberately does not own query execution decisions.  This
module is the private boundary between one Codex thread, the read-only Doris
bridge, and the result contract.  A request gets one bounded lifecycle; a
repair may change SQL, but it may not change the declared result contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Callable, Mapping

from .query_outcomes import DorisResultError, validate_doris_result
from .worker_progress import phase as worker_phase
from .model_query_contract import (
    BusinessResultSet,
    validate_business_projection,
    validate_reconciliation,
)


@dataclass(frozen=True, slots=True)
class LifecycleResult:
    result: Any
    result_items: list[Any]
    answer_results: BusinessResultSet
    last_verified_business: dict[str, Any] | None
    bridge_calls: list[dict[str, Any]]
    metadata_query_count: int
    validation_query_count: int
    business_states: list[str]
    query_error: dict[str, Any] | None
    host_failure: dict[str, Any] | None
    recovery_attempted: bool
    final_contract: dict[str, Any] | None
    selected_reviewed_route: str | None = None
    validation_warnings: list[dict[str, str]] = field(default_factory=list)


class QueryLifecycle:
    """Run one bounded plan/execute/verify/render lifecycle."""

    def __init__(
        self,
        *,
        thread: Any,
        bridge: Any,
        result: Any,
        result_items: list[Any],
        plans: list[dict[str, Any]],
        question: str,
        max_steps: int,
        turn_runner: Callable[..., Any],
        plan_parser: Callable[[Any], list[dict[str, Any]] | None],
        plan_fingerprint: Callable[[str], str],
        same_contract: Callable[[Mapping[str, Any], Mapping[str, Any]], bool],
        repair_plans_preserve_contract: Callable[[list[dict[str, Any]] | None, Mapping[str, Any]], bool],
        retry_prompt: Callable[..., str],
        contract_repair_prompt: Callable[..., str],
        render_prompt: Callable[..., str],
        repeated_prompt: Callable[..., str],
        pending_prompt: Callable[[Any], str],
        effort: Any,
        model: str,
        sandbox: Any,
        is_metadata: Callable[[str], bool],
        status_value: Callable[[Any], Any] = lambda value: value,
    ) -> None:
        self.thread = thread
        self.bridge = bridge
        self.result = result
        self.result_items = list(result_items)
        # Keep parser output immutable from the caller's perspective.  The
        # completion marker is host metadata, not part of an executable plan.
        self.plans = [dict(plan) for plan in plans]
        self.question = question
        self.max_steps = max_steps
        self.turn_runner = turn_runner
        self.plan_parser = plan_parser
        self.plan_fingerprint = plan_fingerprint
        self.same_contract = same_contract
        self.repair_plans_preserve_contract = repair_plans_preserve_contract
        self.retry_prompt = retry_prompt
        self.contract_repair_prompt = contract_repair_prompt
        self.render_prompt = render_prompt
        self.repeated_prompt = repeated_prompt
        self.pending_prompt = pending_prompt
        self.effort = effort
        self.model = model
        self.sandbox = sandbox
        self.is_metadata = is_metadata
        self.status_value = status_value
        self.final_contract: dict[str, Any] | None = None
        self.selected_reviewed_route: str | None = next(
            (
                plan.get("reviewed_route")
                for plan in self.plans
                if isinstance(plan.get("reviewed_route"), str)
            ),
            None,
        )
        self.batch_completion = None
        if self.plans and isinstance(self.plans[-1].get("_completion"), Mapping):
            self.batch_completion = dict(self.plans[-1]["_completion"])
            self.plans[-1] = {key: value for key, value in self.plans[-1].items()
                              if key != "_completion"}

    @staticmethod
    def _without_completion(plans: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        """Copy plans and discard envelope metadata before execution."""
        if not plans:
            return []
        return [
            {key: value for key, value in plan.items() if key != "_completion"}
            for plan in plans
        ]

    @staticmethod
    def _is_clarification_response(value: Any) -> bool:
        """Recognize a model-owned selectable decision without trusting copy."""
        if not isinstance(value, str):
            return False
        try:
            parsed = json.loads(value.strip())
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        if not isinstance(parsed, Mapping):
            return False
        outcome = str(parsed.get("outcome", parsed.get("status", ""))).casefold()
        questions = parsed.get("questions")
        return outcome in {"clarification_required", "needs_selection"} and isinstance(questions, list) and bool(questions)

    def _turn(self, prompt: str) -> Any:
        value = self.turn_runner(
            self.thread,
            prompt,
            effort=self.effort,
            model=self.model,
            sandbox=self.sandbox,
        )
        self.result_items.extend(list(getattr(value, "items", None) or []))
        try:
            plans = self.plan_parser(getattr(value, "final_response", None))
        except Exception:
            plans = None
        if plans:
            for plan in plans:
                route = plan.get("reviewed_route")
                if isinstance(route, str):
                    self.selected_reviewed_route = route
        return value

    def _execute_and_validate(self, plan: Mapping[str, Any], answers: BusinessResultSet, *, step: int):
        """Execute exactly one plan and apply the same validation on every retry."""

        # Reserve named deliverables before spending a Doris call. A duplicate
        # identity is a contract error, not a reason to run a replacement query.
        answers.check_plan(plan)
        with worker_phase("query", step=step):
            payload = self.bridge.query(sql=plan["sql"], purpose=plan["purpose"])
        with worker_phase("validation", step=step):
            verified = validate_doris_result(payload)
            if plan.get("kind") == "validation":
                validate_reconciliation(verified.rows, plan["checks"])
            elif "result_columns" in plan:
                validate_business_projection(payload["execution"], plan["result_columns"])
        return payload, verified

    def run(self) -> LifecycleResult:
        answers = BusinessResultSet()
        calls: list[dict[str, Any]] = []
        metadata_count = 0
        validation_count = 0
        validation_warnings: list[dict[str, str]] = []
        business_states: list[str] = []
        last_business: dict[str, Any] | None = None
        query_error: dict[str, Any] | None = None
        host_failure: dict[str, Any] | None = None
        recovery_attempted = False
        executed: set[str] = set()
        successful: dict[str, dict[str, Any]] = {}
        repair_attempts = 0
        repeated_recovery_attempted = False
        unavailable_retry_attempted = False
        auxiliary_completion_recovery_attempted = False
        empty_business_pending = False
        empty_diagnostic_recovery_attempted = False
        pending = list(self.plans[1:])
        plan = self.plans[0]
        suspended: list[dict[str, Any]] = []
        repair_contract: dict[str, Any] | None = None
        batch_results: list[Mapping[str, Any]] = []

        for step in range(self.max_steps):
            fingerprint = self.plan_fingerprint(plan["sql"])
            if fingerprint in executed:
                cached = successful.get(fingerprint)
                same_completed_contract = bool(cached and self.same_contract(plan, cached["plan"]))
                if not repeated_recovery_attempted and query_error is None and same_completed_contract:
                    repeated_recovery_attempted = True
                    try:
                        resumed = self._turn(self.repeated_prompt(
                            self.question,
                            {key: value for key, value in successful.get(fingerprint, {"plan": plan}).items() if key != "plan"},
                            answers.manifest(),
                        ))
                        resumed_plans = self.plan_parser(getattr(resumed, "final_response", None))
                        final_object = getattr(resumed, "final_response", None)
                        if (
                            self.status_value(getattr(resumed, "status", None)) == "completed"
                            and resumed_plans
                        ):
                            candidate = resumed_plans[0]
                            if self.plan_fingerprint(candidate["sql"]) not in executed:
                                self.result = resumed
                                plan = dict(candidate)
                                plan.pop("_completion", None)
                                pending = self._without_completion(resumed_plans[1:])
                                self.batch_completion = None
                                continue
                        if (
                            self.status_value(getattr(resumed, "status", None)) == "completed"
                            and not resumed_plans
                            and isinstance(final_object, str)
                        ):
                            self.result = resumed
                            self.batch_completion = None
                            break
                    except Exception:
                        pass
                host_failure = {"stage": "repeated_plan", "step": step + 2}
                query_error = query_error or DorisResultError("query_failed").as_public_dict()
                break
            executed.add(fingerprint)
            call = {"server": "doris_query_agent", "tool": "query_doris_sql", "status": "failed", "read_only": True}
            calls.append(call)
            try:
                payload, verified = self._execute_and_validate(plan, answers, step=step + 1)
            except DorisResultError as exc:
                host_failure = {"stage": "query", "diagnostic": exc.diagnostic, "step": step + 1}
                query_error = exc.as_public_dict()
                if exc.reason == "result_truncated" and plan.get("reviewed_route"):
                    # Preserve the capacity failure for the API handoff. Once
                    # the model selected a capability, another oversized SQL
                    # attempt cannot improve it and may mask the original state.
                    self.selected_reviewed_route = plan["reviewed_route"]
                    recovery_attempted = True
                    break
                retry_succeeded = False
                nonfatal_validation = (
                    plan.get("kind") == "validation"
                    and exc.reason == "reconciliation_failed"
                    and (bool(answers.manifest()) or last_business is not None)
                )
                if nonfatal_validation:
                    validation_warnings.append({
                        "code": "reconciliation_failed",
                        "message": "自动对账校验未通过，已展示已成功执行的业务结果；结论需复核。",
                    })
                    call["status"] = "failed"
                    # This call did not succeed.  Keep it in the failed-call
                    # count and expose the warning, but never count it as a
                    # successful validation: the evidence contract derives
                    # business-call counts from successful calls.
                    successful[fingerprint] = {"plan": plan, "row_count": 0}
                    query_error = None
                    host_failure = None
                    manifest = answers.manifest()
                    self.final_contract = ({
                        "outcome": "completed",
                        "questions": [],
                        "result_ids": [item["result_id"] for item in manifest],
                    } if manifest else None)
                    break
                # A transient service outage may be retried once with the exact
                # same SQL and business scope. It must never be re-planned by
                # the model in this branch.
                if exc.reason == "backend_unavailable" and not unavailable_retry_attempted:
                    unavailable_retry_attempted = True
                    recovery_attempted = True
                    retry_call = {
                        "server": "doris_query_agent",
                        "tool": "query_doris_sql",
                        "status": "failed",
                        "read_only": True,
                        "retry_of": len(calls) - 1,
                    }
                    calls.append(retry_call)
                    try:
                        payload, verified = self._execute_and_validate(plan, answers, step=step + 1)
                        retry_call["status"] = "completed"
                        call = retry_call
                        query_error = None
                        host_failure = None
                        retry_succeeded = True
                    except DorisResultError as retry_exc:
                        exc = retry_exc
                        query_error = exc.as_public_dict()
                repairable_diagnostic = exc.diagnostic in {
                    "unknown_column", "unknown_table", "ambiguous_column",
                    "syntax_error", "unsupported_function", "type_mismatch",
                }
                if not retry_succeeded and query_error is not None and repair_attempts < 2 and (
                    (exc.reason == "query_failed" and (repairable_diagnostic or exc.repairable))
                    or exc.reason == "result_truncated"
                ):
                    repair_attempts += 1
                    recovery_attempted = True
                    try:
                        repaired = self._turn(self.retry_prompt(
                            self.question, exc.code, diagnostic=exc.diagnostic,
                            required_plan=repair_contract or plan,
                        ))
                        repaired_plans = self.plan_parser(getattr(repaired, "final_response", None))
                        required = repair_contract or plan
                        if (
                            self.status_value(getattr(repaired, "status", None)) == "completed"
                            and repaired_plans
                            and self.repair_plans_preserve_contract(repaired_plans, required)
                        ):
                            reviewed = next((p.get("reviewed_route") for p in repaired_plans
                                             if self.same_contract(p, required) and p.get("reviewed_route")), None)
                            if exc.reason == "result_truncated" and reviewed:
                                self.selected_reviewed_route = reviewed
                                self.result = repaired
                                break
                            if repair_contract is None:
                                repair_contract = plan
                                suspended = pending
                            # The original completion declaration is no
                            # longer sufficient after a failed execution.
                            # Require the repaired lifecycle to prove its own
                            # manifest instead of completing from stale state.
                            self.batch_completion = None
                            self.result = repaired
                            plan = dict(repaired_plans[0])
                            plan.pop("_completion", None)
                            pending = self._without_completion(repaired_plans[1:])
                            continue
                        if (
                            self.status_value(getattr(repaired, "status", None)) == "completed"
                            and self._is_clarification_response(getattr(repaired, "final_response", None))
                        ):
                            # A failed query can reveal a missing business
                            # decision. Return the selectable decision instead
                            # of converting it into a generic execution error;
                            # no partial result is selected or published.
                            self.result = repaired
                            self.batch_completion = None
                            query_error = None
                            host_failure = None
                            break
                        # Some gateways answer the first repair request with
                        # prose or a partial envelope. Spend the second and
                        # final repair budget on an explicit contract-only
                        # JSON correction in the same model thread.
                        if repair_attempts < 2:
                            repair_attempts += 1
                            format_prompt = self.contract_repair_prompt(
                                self.question, getattr(repaired, "final_response", None)
                            )
                            if exc.diagnostic:
                                format_prompt += (
                                    "\n[HOST_QUERY_ERROR] 上一条查询失败类别保持为 "
                                    + exc.code
                                    + " ("
                                    + exc.diagnostic
                                    + ")，请修复查询计划后再执行。"
                                )
                            formatted = self._turn(format_prompt)
                            formatted_plans = self.plan_parser(getattr(formatted, "final_response", None))
                            if (
                                self.status_value(getattr(formatted, "status", None)) == "completed"
                                and formatted_plans
                                and self.repair_plans_preserve_contract(formatted_plans, required)
                            ):
                                reviewed = next((p.get("reviewed_route") for p in formatted_plans
                                                 if self.same_contract(p, required) and p.get("reviewed_route")), None)
                                if exc.reason == "result_truncated" and reviewed:
                                    self.selected_reviewed_route = reviewed
                                    self.result = formatted
                                    break
                                if repair_contract is None:
                                    repair_contract = plan
                                    suspended = pending
                                self.batch_completion = None
                                self.result = formatted
                                plan = dict(formatted_plans[0])
                                plan.pop("_completion", None)
                                pending = self._without_completion(formatted_plans[1:])
                                continue
                    except Exception:
                        pass
                if not retry_succeeded:
                    break

            call["status"] = "completed"
            # A successful repair replaces the prior failure.  The earlier
            # diagnostic remains server-side only and must not poison the final
            # no-data or live-success state.
            if repair_contract is None:
                query_error = None
                host_failure = None
            if self.is_metadata(plan["sql"]):
                metadata_count += 1
            elif plan.get("kind") == "validation":
                validation_count += 1
            else:
                business_states.append(verified.state)
                empty_business_pending = not verified.rows
                last_business = {
                    "columns": list(payload["execution"].get("columns", [])),
                    "rows": [dict(row) for row in verified.rows],
                    **({"result_columns": plan["result_columns"]} if "result_columns" in plan else {}),
                }
                try:
                    answers.add(plan, last_business)
                except DorisResultError as exc:
                    query_error = exc.as_public_dict()
                    break
            successful[fingerprint] = {"plan": plan, "row_count": len(verified.rows)}
            if repair_contract is not None and self.same_contract(plan, repair_contract):
                repair_contract = None
                pending.extend(suspended)
                suspended = []
                query_error = None
                host_failure = None
            if pending and empty_business_pending:
                # A validation plan drafted before an empty business result
                # cannot explain why the result is empty. Drop only those
                # stale control plans and let the same model thread diagnose
                # the request's entity and core filters first. Independent
                # business plans remain in their original order.
                pending = [item for item in pending if item.get("kind") != "validation"]
            if pending:
                batch_results.append(payload)
                plan = pending.pop(0)
                if repair_contract is None:
                    query_error = None
                    host_failure = None
                continue

            # A complete independent batch already contains every business
            # projection and its validation evidence. Do not spend another
            # model turn asking it to restate a manifest that the host can
            # construct from the screened result set.
            if (self.batch_completion is not None and repair_contract is None
                    and query_error is None and host_failure is None
                    and not suspended and not pending):
                expected_ids = list(self.batch_completion.get("result_ids", []))
                observed_ids = [item["result_id"] for item in answers.manifest()]
                if (validation_count > 0 and expected_ids
                        and set(expected_ids) == set(observed_ids)
                        and len(expected_ids) == len(observed_ids)):
                    self.final_contract = {
                        "outcome": "completed",
                        "questions": [],
                        "result_ids": expected_ids,
                    }
                    break

            try:
                empty_result_handoff = ""
                if empty_business_pending:
                    empty_result_handoff = (
                        "\n[HOST_EMPTY_BUSINESS_RESULT]\n"
                        "刚才的业务查询在真实 Doris 中成功执行，但返回 0 行。这个结果不能自动证明业务数据不存在，"
                        "也不能直接进入 validation 或以空表结束。请最多进行两次有意义的诊断：优先核验本请求中的实体"
                        "解析（例如用户给的是商品简称而不是完整主数据名称/SKU编码）和核心筛选条件；必要时先查询主数据"
                        "得到候选编码、规范名称、货主和分类，再用候选编码重规划原业务查询。不得把诊断查询当作交付表，"
                        "不得放宽用户明确的规格、日期、货主、仓库、渠道或单位条件。若多个候选无法区分，返回一次性"
                        "clarification_required 选择；若诊断后仍无数据，返回 completed 并明确这是当前条件下的空结果。"
                        "不要用 validation 对账查询替代空结果诊断。\n"
                    )
                rendered = self._turn(
                    self.render_prompt(payload, question=self.question,
                                       manifest=answers.manifest(), earlier_results=batch_results)
                    + empty_result_handoff
                    + self.pending_prompt(repair_contract)
                )
            except Exception:
                try:
                    rendered = self._turn(
                        self.render_prompt(payload, question=self.question,
                                           manifest=answers.manifest(), earlier_results=batch_results)
                        + self.pending_prompt(repair_contract)
                        + "\n[HOST_RENDER_RETRY] 本步已经由宿主校验，不要重复查询；完成尚缺的交付结果。"
                    )
                    recovery_attempted = True
                except Exception:
                    host_failure = {"stage": "model_response", "step": step + 1}
                    query_error = DorisResultError("query_failed").as_public_dict()
                    break
            if self.status_value(getattr(rendered, "status", None)) != "completed" or not isinstance(getattr(rendered, "final_response", None), str):
                host_failure = {"stage": "model_response", "step": step + 1}
                query_error = DorisResultError("query_failed").as_public_dict()
                break
            self.result = rendered
            next_plans = self.plan_parser(rendered.final_response)
            self.batch_completion = None
            if next_plans and isinstance(next_plans[-1].get("_completion"), Mapping):
                self.batch_completion = dict(next_plans[-1].pop("_completion"))
            if empty_business_pending and next_plans and next_plans[0].get("kind") == "validation":
                # The model must not use an integrity check as a substitute
                # for diagnosing an empty business result. Give it one
                # explicit, bounded same-thread handoff. This is generic for
                # every entity and metric; it does not select a route or
                # alter the user's filters.
                if not empty_diagnostic_recovery_attempted:
                    empty_diagnostic_recovery_attempted = True
                    recovery_attempted = True
                    try:
                        diagnostic = self._turn(
                            "[HOST_EMPTY_RESULT_DIAGNOSTIC_REQUIRED] 刚才的实时业务查询成功但返回0行，"
                            "当前不能执行或展示validation对账。请先诊断导致空结果的实体解析或核心筛选条件，"
                            "必要时查询主数据得到候选编码、规范名称、货主和分类，再用候选编码重规划原业务查询。"
                            "保持用户明确的日期、货主、仓库、渠道、规格和单位，不得把放宽条件后的结果冒充原结果。"
                            "最多进行两次有意义诊断；诊断查询不是交付表。只返回新的私有query_plan、"
                            "query_plan_batch或clarification_required选择数组，不要返回validation计划，不要声称已完成。\n"
                            "CURRENT_REQUEST_CAPSULE:\n" + self.question
                        )
                        diagnostic_plans = self.plan_parser(getattr(diagnostic, "final_response", None))
                        diagnostic_response = getattr(diagnostic, "final_response", None)
                        if diagnostic_plans:
                            self.result = diagnostic
                            rendered = diagnostic
                            next_plans = diagnostic_plans
                        elif (self._is_clarification_response(diagnostic_response)
                              or (isinstance(diagnostic_response, str)
                                  and json.loads(diagnostic_response).get("outcome") == "completed")):
                            self.result = diagnostic
                            query_error = None
                            host_failure = None
                            break
                        else:
                            next_plans = []
                    except Exception:
                        next_plans = []
                # Never execute a validation-only continuation after an empty
                # result. If the bounded diagnostic produced no business plan,
                # the lifecycle can finish as a confirmed empty result.
                next_plans = [item for item in next_plans if item.get("kind") != "validation"]
                if not next_plans:
                    host_failure = {"stage": "empty_result_diagnosis", "step": step + 1}
                    query_error = DorisResultError("plan_invalid").as_public_dict()
                    break
            if next_plans and self.plan_fingerprint(next_plans[0]["sql"]) in executed and len(next_plans) > 1:
                host_failure = {"stage": "repeated_plan", "step": step + 2}
                query_error = DorisResultError("query_failed").as_public_dict()
                break
            if repair_contract is not None and not self.repair_plans_preserve_contract(next_plans, repair_contract):
                host_failure = {"stage": "pending_repair", "step": step + 1}
                query_error = query_error or DorisResultError("query_failed").as_public_dict()
                break
            if next_plans:
                plan = dict(next_plans[0])
                plan.pop("_completion", None)
                pending = self._without_completion(next_plans[1:])
                continue
            if self._is_clarification_response(getattr(rendered, "final_response", None)):
                # The model may discover an unresolved decision only after a
                # verified intermediate result. Keep the result private and
                # ask the user for the missing decision in one selectable turn.
                self.result = rendered
                query_error = None
                host_failure = None
                break
            if (
                not next_plans
                and last_business is None
                and plan.get("kind") in {"metadata", "validation"}
                and not auxiliary_completion_recovery_attempted
            ):
                # Metadata and validation are auxiliary controls, never a
                # business deliverable. Some gateways prematurely emit
                # `completed` after an auxiliary query. Give the same model
                # thread one explicit continuation handoff so it must either
                # plan the business query or ask a real clarification question.
                auxiliary_completion_recovery_attempted = True
                recovery_attempted = True
                try:
                    continued = self._turn(
                        "[HOST_AUXILIARY_ONLY_CONTINUE] 刚才的查询只完成了辅助探查或对账控制，"
                        "它不是用户要的业务结果，本轮不能以completed结束。请继续根据当前原始问题和"
                        "已加载知识选择适用口径；只返回一个私有 query_plan JSON，或完整的"
                        "clarification_required选择数组。不要重复元数据探查，不要声称已完成，"
                        "不要输出普通说明或SQL之外的Markdown。\n"
                        "CURRENT_REQUEST_CAPSULE:\n" + self.question
                    )
                    continued_plans = self.plan_parser(getattr(continued, "final_response", None))
                    continued_response = getattr(continued, "final_response", None)
                    if continued_plans:
                        self.result = continued
                        self.batch_completion = None
                        plan = dict(continued_plans[0])
                        plan.pop("_completion", None)
                        pending = self._without_completion(continued_plans[1:])
                        continue
                    if self._is_clarification_response(continued_response):
                        self.result = continued
                        query_error = None
                        host_failure = None
                        break
                except Exception:
                    pass
                host_failure = {"stage": "model_response", "step": step + 1}
                query_error = DorisResultError("query_failed").as_public_dict()
                break
            # A malformed plan after a live result is a contract failure, not a
            # completed answer. Give the same thread one format-only repair;
            # this never re-runs the already verified business query.
            try:
                parsed = json.loads(rendered.final_response.strip()) if isinstance(rendered.final_response, str) else None
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed = None
            if isinstance(parsed, Mapping) and parsed.get("outcome") in {"query_plan", "query_plan_batch"}:
                try:
                    repaired = self._turn(self.contract_repair_prompt(self.question, rendered.final_response))
                    repaired_plans = self.plan_parser(getattr(repaired, "final_response", None))
                    if repaired_plans:
                        self.result = repaired
                        self.batch_completion = None
                        plan = dict(repaired_plans[0])
                        plan.pop("_completion", None)
                        pending = self._without_completion(repaired_plans[1:])
                        continue
                except Exception:
                    # The business query already passed Doris validation. A
                    # format-repair failure must not escape the lifecycle or
                    # cause the verified result to be treated as fabricated.
                    host_failure = {"stage": "plan_contract", "step": step + 1}
                    query_error = DorisResultError("query_failed").as_public_dict()
                if query_error is None:
                    query_error = DorisResultError("execution_invalid").as_public_dict()
                if host_failure is None:
                    host_failure = {"stage": "plan_contract", "step": step + 1}
            break
        else:
            host_failure = {"stage": "step_budget", "step": self.max_steps}
            query_error = query_error or DorisResultError("query_failed").as_public_dict()

        return LifecycleResult(
            result=self.result,
            result_items=self.result_items,
            answer_results=answers,
            last_verified_business=last_business,
            bridge_calls=calls,
            metadata_query_count=metadata_count,
            validation_query_count=validation_count,
            business_states=business_states,
            query_error=query_error,
            host_failure=host_failure if query_error else None,
            recovery_attempted=recovery_attempted,
            final_contract=self.final_contract,
            selected_reviewed_route=self.selected_reviewed_route,
            validation_warnings=list(validation_warnings),
        )

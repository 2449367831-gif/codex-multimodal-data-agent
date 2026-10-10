from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import uuid4

from .audit import AuditStore
from .errors import ContractNotApprovedError, RouteAmbiguousError, RuntimeErrorBase
from .executors import QueryExecutor, build_executor
from .knowledge import KnowledgeBase
from .models import QueryPlan, utc_now_iso
from .parameters import ContractParameterValidator
from .router import MetricRouter
from .settings import RuntimeSettings
from .sql import TemplateCompiler


class QueryService:
    def __init__(
        self,
        settings: RuntimeSettings | None = None,
        *,
        executor: QueryExecutor | None = None,
        audit_store: AuditStore | None = None,
    ) -> None:
        self.settings = settings or RuntimeSettings.from_env()
        self.executor = executor or build_executor(self.settings)
        doris_runtime = self.executor.name == "doris"
        self.knowledge = KnowledgeBase(
            self.settings.project_root,
            release_id=self.settings.release_id,
            reload_on_change=not doris_runtime,
            require_trusted_release=doris_runtime,
            trusted_release_sha256=self.settings.trusted_release_sha256,
        )
        self.router = MetricRouter(self.knowledge)
        self.parameter_validator = ContractParameterValidator()
        self.compiler = TemplateCompiler(
            self.settings.project_root,
            reload_on_change=not doris_runtime,
            preload_template_refs=(
                self.knowledge.selected_template_refs() if doris_runtime else ()
            ),
        )
        if doris_runtime:
            self.knowledge.verify_runtime_snapshot_integrity(
                self.compiler.cached_file_digests()
            )
        self.audit = audit_store or AuditStore(self.settings.audit_db_path)

    def health(self) -> dict[str, Any]:
        executor_health = self.executor.health()
        public_executor_health = {
            key: executor_health.get(key)
            for key in ("name", "ready", "missing", "database", "connection_tested")
            if key in executor_health
        }
        return {
            "status": "ok" if executor_health.get("ready") else "degraded",
            "knowledge_base": self.knowledge.health(),
            "executor": public_executor_health,
            "audit": {
                "enabled": self.settings.audit_db_path is not None,
                "stores_result_rows": False,
            },
            "contract_execution_policy": {
                "doris_requires_active_and_approved": True,
                "doris_requires_released_manifest_exact_metric_version": True,
                "allow_unapproved_override": self.settings.allow_unapproved_contracts,
                "override_name": "AGENT_ALLOW_UNAPPROVED_CONTRACTS",
                "override_warning": (
                    "Temporary pre-release smoke-test bypass; keep false in production."
                ),
                "trusted_release_anchor_configured": bool(
                    self.settings.trusted_release_sha256
                ),
            },
        }

    def list_metrics(self) -> list[dict[str, Any]]:
        return [self._metric_summary(metric) for metric in self.knowledge.metrics()]

    def get_metric(self, metric_id: str) -> dict[str, Any]:
        return self.knowledge.metric(metric_id)

    def list_releases(self) -> list[dict[str, Any]]:
        return self.knowledge.releases()

    def current_release(self) -> dict[str, Any] | None:
        return self.knowledge.released_manifest()

    def planning_release(self) -> dict[str, Any] | None:
        return self.knowledge.planning_manifest()

    @staticmethod
    def _metric_summary(metric: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": metric["id"],
            "version": str(metric["version"]),
            "status": metric.get("status"),
            "business_definition": metric["business_definition"],
            "grain": metric["grain"],
            "unit": metric["unit"],
            "parameters": deepcopy(metric["parameters"]),
            "required_validations": list(metric["required_validations"]),
            "has_template": bool(metric.get("template")),
        }

    def build_plan(
        self,
        *,
        metric_id: str | None,
        parameters: dict[str, Any] | None,
        question: str | None = None,
        request_id: str | None = None,
    ) -> QueryPlan:
        resolved_metric_id = metric_id
        if not resolved_metric_id:
            decision = self.router.route(question or "")
            if decision.needs_clarification or not decision.selected_metric_id:
                raise RouteAmbiguousError(
                    decision.clarification_question or "Metric route is ambiguous",
                    details=decision.as_dict(),
                )
            resolved_metric_id = decision.selected_metric_id

        metric = self.knowledge.metric(resolved_metric_id)
        normalized = self.parameter_validator.validate(metric, parameters)
        compiled = self.compiler.compile(metric, normalized)
        release_id, source_versions = self.knowledge.plan_release_context(
            metric_id=metric["id"],
            metric_version=str(metric["version"]),
            source_ids=compiled.source_tables,
        )

        declared_validation_results = list(compiled.validation_results)
        completed_ids = {item.get("id") for item in declared_validation_results}
        for validation_id in metric.get("required_validations", []):
            if validation_id in completed_ids:
                continue
            rule = self.knowledge.validation(validation_id)
            declared_validation_results.append(
                {
                    "id": validation_id,
                    "passed": None,
                    "status": "pending_runtime_or_business_check",
                    "severity": (rule or {}).get("severity"),
                }
            )

        return QueryPlan(
            request_id=request_id or str(uuid4()),
            metric_id=metric["id"],
            metric_version=str(metric["version"]),
            release_id=release_id,
            source_versions=source_versions,
            question=question,
            parameters=normalized,
            sql=compiled.sql,
            bindings=compiled.bindings,
            validation_results=tuple(declared_validation_results),
            source_tables=compiled.source_tables,
            executor_name=self.executor.name,
        )

    def plan_and_audit(
        self,
        *,
        metric_id: str | None,
        parameters: dict[str, Any] | None,
        question: str | None = None,
    ) -> QueryPlan:
        request_id = str(uuid4())
        try:
            plan = self.build_plan(
                metric_id=metric_id,
                parameters=parameters,
                question=question,
                request_id=request_id,
            )
        except RuntimeErrorBase as exc:
            self._write_error_audit(request_id, metric_id, question, parameters, "blocked", exc)
            raise
        now = utc_now_iso()
        self.audit.upsert(
            {
                **plan.as_dict(),
                "created_at": plan.created_at,
                "updated_at": now,
                "status": "planned",
            }
        )
        return plan

    def query(
        self,
        *,
        metric_id: str | None,
        parameters: dict[str, Any] | None,
        question: str | None = None,
        include_sql: bool = False,
    ) -> dict[str, Any]:
        request_id = str(uuid4())
        audit_metric_id = metric_id
        try:
            if self.executor.name == "doris" and not self.settings.allow_unapproved_contracts:
                resolved_metric_id = metric_id
                if not resolved_metric_id:
                    decision = self.router.route(question or "")
                    if decision.needs_clarification or not decision.selected_metric_id:
                        raise RouteAmbiguousError(
                            decision.clarification_question or "Metric route is ambiguous",
                            details=decision.as_dict(),
                        )
                    resolved_metric_id = decision.selected_metric_id
                audit_metric_id = resolved_metric_id
                self._enforce_doris_contract_preflight(resolved_metric_id)
            plan = self.build_plan(
                metric_id=metric_id,
                parameters=parameters,
                question=question,
                request_id=request_id,
            )
        except RuntimeErrorBase as exc:
            self._write_error_audit(
                request_id, audit_metric_id, question, parameters, "blocked", exc
            )
            raise

        if self.executor.name == "doris":
            self._enforce_doris_release_gate(plan, request_id=request_id)

        self.audit.upsert(
            {
                **plan.as_dict(),
                "updated_at": utc_now_iso(),
                "status": "running",
            }
        )
        try:
            result = self.executor.execute(plan.sql, plan.bindings, metric_id=plan.metric_id)
        except RuntimeErrorBase as exc:
            self._write_error_audit(
                request_id,
                plan.metric_id,
                question,
                plan.parameters,
                "failed",
                exc,
                plan=plan,
            )
            raise

        has_pending_blocking = any(
            item.get("passed") is None and item.get("severity") == "blocking"
            for item in plan.validation_results
        )
        final_status = (
            "mock_incomplete"
            if result.is_mock and has_pending_blocking
            else "mock_succeeded"
            if result.is_mock
            else "incomplete"
            if has_pending_blocking
            else "succeeded"
        )

        self.audit.upsert(
            {
                **plan.as_dict(),
                "updated_at": utc_now_iso(),
                "status": final_status,
                "duration_ms": result.duration_ms,
                "row_count": result.row_count,
                "truncated": result.truncated,
                "is_mock": result.is_mock,
            }
        )
        return {
            "request_id": request_id,
            "status": final_status,
            "metric": {
                "id": plan.metric_id,
                "version": plan.metric_version,
                "release_id": plan.release_id,
                "definition": self.knowledge.metric(plan.metric_id)["business_definition"],
                "unit": self.knowledge.metric(plan.metric_id)["unit"],
                "grain": self.knowledge.metric(plan.metric_id)["grain"],
            },
            "parameters": plan.as_dict()["parameters"],
            "validation_results": list(plan.validation_results),
            "source_tables": list(plan.source_tables),
            "source_versions": dict(plan.source_versions),
            "release_id": plan.release_id,
            "execution": result.as_dict(),
            **({"sql": plan.sql, "bindings": plan.as_dict()["bindings"]} if include_sql else {}),
        }

    def _write_error_audit(
        self,
        request_id: str,
        metric_id: str | None,
        question: str | None,
        parameters: dict[str, Any] | None,
        status: str,
        error: RuntimeErrorBase,
        *,
        plan: QueryPlan | None = None,
    ) -> None:
        now = utc_now_iso()
        self.audit.upsert(
            {
                "request_id": request_id,
                "created_at": plan.created_at if plan else now,
                "updated_at": now,
                "status": status,
                "metric_id": plan.metric_id if plan else metric_id,
                "metric_version": plan.metric_version if plan else None,
                "release_id": plan.release_id if plan else None,
                "source_versions": plan.source_versions if plan else {},
                "question": question,
                "parameters": plan.parameters if plan else (parameters or {}),
                "sql": plan.sql if plan else None,
                "bindings": plan.bindings if plan else {},
                "validation_results": list(plan.validation_results) if plan else [],
                "executor_name": self.executor.name,
                "error": error.as_dict(),
            }
        )

    def _enforce_doris_release_gate(self, plan: QueryPlan, *, request_id: str) -> None:
        """Require every source version used by the plan to be in the released manifest."""
        released = self.knowledge.released_manifest()
        contracts = released.get("contracts", {}) if released else {}
        if not isinstance(contracts, dict):
            contracts = {}
        released_source_refs = contracts.get("sources", [])
        if not isinstance(released_source_refs, list):
            released_source_refs = []
        required_source_refs = sorted(
            f"{source_id}@{version}" for source_id, version in plan.source_versions.items()
        )
        missing_source_refs = sorted(set(required_source_refs) - set(released_source_refs))
        exact_source_match = not missing_source_refs and len(plan.source_versions) == len(
            plan.source_tables
        )
        pending_blocking_validations = sorted(
            str(item.get("id"))
            for item in plan.validation_results
            if item.get("passed") is None and item.get("severity") == "blocking"
        )
        if self.settings.allow_unapproved_contracts:
            return
        if not exact_source_match or pending_blocking_validations:
            details = {
                "metric_id": plan.metric_id,
                "metric_version": plan.metric_version,
                "released_release_id": released.get("release_id") if released else None,
                "exact_source_match": exact_source_match,
                "required_source_refs": required_source_refs,
                "missing_source_refs": missing_source_refs,
                "pending_blocking_validations": pending_blocking_validations,
                "dry_run_available": True,
                "override_env": "AGENT_ALLOW_UNAPPROVED_CONTRACTS=true",
            }
            error = ContractNotApprovedError(
                "Doris execution requires released sources and no pending blocking validations",
                details=details,
            )
            self._write_error_audit(
                request_id,
                plan.metric_id,
                plan.question,
                plan.parameters,
                "blocked",
                error,
                plan=plan,
            )
            raise error

    def _enforce_doris_contract_preflight(self, metric_id: str) -> None:
        """Block ineligible metric contracts before template compilation or Doris access."""
        contract = self.knowledge.metric(metric_id)
        metric_version = str(contract.get("version"))
        metric_ref = f"{metric_id}@{metric_version}"
        approval_state = contract.get("approval", {}).get("state")
        released = self.knowledge.released_manifest()
        contracts = released.get("contracts", {}) if released else {}
        if not isinstance(contracts, dict):
            contracts = {}
        metric_refs = contracts.get("metrics", [])
        if not isinstance(metric_refs, list):
            metric_refs = []
        exact_release_match = bool(released and metric_ref in metric_refs)
        if (
            contract.get("status") == "active"
            and approval_state == "approved"
            and exact_release_match
        ):
            return
        raise ContractNotApprovedError(
            "Doris execution requires an active, approved metric in the released manifest",
            details={
                "metric_id": metric_id,
                "metric_version": metric_version,
                "metric_ref": metric_ref,
                "status": contract.get("status"),
                "approval_state": approval_state,
                "released_release_id": released.get("release_id") if released else None,
                "exact_release_match": exact_release_match,
                "dry_run_available": True,
                "override_env": "AGENT_ALLOW_UNAPPROVED_CONTRACTS=true",
            },
        )

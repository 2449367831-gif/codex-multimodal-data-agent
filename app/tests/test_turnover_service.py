from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.artifacts import ArtifactStore
from app.chat_api import ChatTimeoutError, IdentityResolver, create_chat_app
from app.chat_sessions import ChatSessionStore
from app.turnover_service import (
    ALLOW_UNAPPROVED_ENV,
    REPORT_METRIC_ID,
    TurnoverReportRuntime,
    TurnoverRuntimeError,
    complete_window,
    is_standard_center_report,
    report_window_days,
)
from app.turnover_report import build_turnover_report
from app.query_outcomes import DorisResultError


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class _CountingQueryClient:
    def __init__(self, response: dict[str, object] | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.response = response or {
            "status": "succeeded",
            "execution": {
                "executor_name": "doris",
                "is_mock": False,
                "truncated": False,
                "columns": [],
                "rows": [],
                "row_count": 0,
            }
        }

    def query(self, *, sql: str, purpose: str | None = None) -> dict[str, object]:
        self.calls.append({"sql": sql, "purpose": purpose})
        return self.response


class _ReleasedKnowledge:
    def __init__(
        self,
        *,
        source_version: str = "1.0.0",
        manifest_source_version: str | None = None,
    ) -> None:
        self.source_version = source_version
        self.manifest_source_version = manifest_source_version or source_version
        self.verify_calls = 0

    def metric(self, _metric_id: str) -> dict[str, object]:
        return {
            "id": "inventory.turnover_7d_realtime",
            "version": "1.0.0",
            "status": "active",
            "approval": {"state": "approved"},
            "sources": {"required": ["source.table"], "conditional": []},
        }

    def released_manifest(self) -> dict[str, object]:
        return {
            "release_id": "release-test",
            "status": "released",
            "released_at": "2026-08-29T00:00:00+08:00",
            "approval": {"state": "approved"},
            "contracts": {
                "metrics": ["inventory.turnover_7d_realtime@1.0.0"],
                "sources": [f"source.table@{self.manifest_source_version}"],
            },
        }

    def plan_release_context(self, **_kwargs: object) -> tuple[str, dict[str, str]]:
        return "release-test", {"source.table": self.source_version}

    def verify_runtime_snapshot_integrity(self, _digests: dict[str, str]) -> None:
        self.verify_calls += 1


class _Runner:
    backend_url = "http://127.0.0.1:9"
    model = "gpt-5.6-sol"
    reasoning_effort = "xhigh"
    auth_configured = True

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.clarifies = False
        self.error: Exception | None = None

    def run(self, question: str, **_kwargs: object) -> dict[str, object]:
        self.calls.append(question)
        if self.error is not None:
            raise self.error
        interaction = (
            {
                "outcome": "clarification_required",
                "questions": [
                    {
                        "id": "output_grain",
                        "prompt": "请选择结果粒度",
                        "selection": "single",
                        "options": [
                            {"id": "total", "label": "总计"},
                            {"id": "warehouse", "label": "按仓库"},
                        ],
                    }
                ],
            }
            if self.clarifies
            else {"outcome": "completed", "questions": []}
        )
        return {
            "status": "completed",
            "final_response": "已完成",
            "thread_id": "thread-turnover-test",
            "interaction": interaction,
            "query_execution": {
                "live_business_result_executed": True,
                "status": "live_result",
            },
            "mcp_calls": [],
        }

    def public_health(self, _health: object) -> dict[str, object]:
        return {"status": "ok", "ready": True, "query_backend": {"ready": True}}


class _ReportRuntime:
    def __init__(self, artifact_store: ArtifactStore) -> None:
        self.artifact_store = artifact_store
        self.calls: list[dict[str, object]] = []
        self.error: Exception | None = None

    def run(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        artifact = self.artifact_store.register_bytes(
            b"\x89PNG\r\n\x1a\nreport",
            principal_hash=str(kwargs["principal_hash"]),
            conversation_id=str(kwargs["conversation_id"]),
            request_id=str(kwargs["request_id"]),
            kind="center_warehouse_turnover_7d",
            title="中心仓与分仓周转",
            period="2026-08-22 至 2026-08-29",
        )
        return {"artifact": artifact}


class TurnoverRuntimeReleaseGateTestCase(unittest.TestCase):
    def test_report_rejects_incomplete_live_evidence_before_rendering(self):
        cases = (
            ({"is_mock": None}, "execution_invalid"),
            ({"is_mock": True}, "not_live_result"),
            ({"truncated": None}, "execution_invalid"),
            ({"truncated": True}, "result_truncated"),
            ({"row_count": 1}, "row_count_mismatch"),
            ({"row_count": False}, "execution_invalid"),
            ({"rows": ["invalid"]}, "rows_invalid"),
        )
        for changes, reason in cases:
            with self.subTest(reason=reason, changes=changes), TemporaryDirectory() as folder:
                query = _CountingQueryClient()
                query.response["execution"].update(changes)
                runtime = self._runtime(query, artifact_root=folder, knowledge=_ReleasedKnowledge())
                with patch("app.turnover_service.render_turnover_image") as render:
                    with self.assertRaises(DorisResultError) as caught:
                        runtime.run(principal_hash="test", conversation_id="test", request_id="test", window_days=14)
                    self.assertEqual(reason, caught.exception.reason)
                    render.assert_not_called()

    def test_report_preserves_backend_error_envelope(self):
        for code, reason in (("query_timeout", "query_failed"), ("query_rejected", "backend_rejected"),
                             ("query_unavailable", "backend_unavailable"), ("query_truncated", "result_truncated")):
            with self.subTest(code=code), TemporaryDirectory() as folder:
                query = _CountingQueryClient({"status": "failed", "error": {"code": code}})
                runtime = self._runtime(query, artifact_root=folder, knowledge=_ReleasedKnowledge())
                with self.assertRaises(DorisResultError) as caught:
                    runtime.run(principal_hash="test", conversation_id="test", request_id="test", window_days=14)
                self.assertEqual(reason, caught.exception.reason)

    def test_query_failure_class_is_preserved_by_report_runtime(self):
        class FailingQuery:
            def __init__(self, reason):
                self.reason = reason

            def query(self, **kwargs):
                raise DorisResultError(self.reason)

        for reason in ("query_timeout", "backend_unavailable", "backend_rejected", "result_truncated"):
            with self.subTest(reason=reason), TemporaryDirectory() as folder:
                runtime = self._runtime(FailingQuery(reason), artifact_root=folder, knowledge=_ReleasedKnowledge())
                with self.assertRaises(DorisResultError) as caught:
                    runtime.run(principal_hash="test", conversation_id="test", request_id="test", window_days=14)
                self.assertEqual(reason, caught.exception.reason)

    def test_report_window_is_not_hardcoded_to_seven_days(self) -> None:
        self.assertEqual(14, report_window_days("生成中心仓和分仓14天周转数据图片"))
        self.assertTrue(is_standard_center_report("生成中心仓和分仓14天周转数据图片"))
        start, end = complete_window("2026-08-29", now=datetime(2026, 8, 29, 12), window_days=14)
        self.assertEqual("2026-08-15", start.date().isoformat())
        self.assertEqual("2026-08-29", end.date().isoformat())

    def test_report_window_accepts_long_complete_requests(self) -> None:
        self.assertEqual(100, report_window_days("生成中心仓和分仓100天周转数据图片"))
        self.assertEqual(14, report_window_days("生成中心仓和分仓十四天周转数据图片"))
        self.assertIsNone(report_window_days("生成中心仓和分仓3651天周转数据图片"))
        with self.assertRaises(TurnoverRuntimeError):
            complete_window("2026-08-29", now=datetime(2026, 8, 29, 12), window_days=0)

    def _runtime(self, query_client: _CountingQueryClient, **kwargs: object) -> TurnoverReportRuntime:
        kwargs.setdefault("allow_unapproved_contracts", False)
        return TurnoverReportRuntime(
            artifact_store=ArtifactStore(Path(kwargs.pop("artifact_root", ".")) / "artifacts"),
            project_root=PROJECT_ROOT,
            query_client=query_client,
            clock=lambda _tz=None: datetime(2026, 8, 29, 12, 0),
            **kwargs,
        )

    def test_candidate_contract_is_blocked_before_doris_call(self) -> None:
        with TemporaryDirectory(prefix="turnover-release-gate-") as directory:
            query_client = _CountingQueryClient()
            runtime = self._runtime(query_client, artifact_root=directory)
            with patch("app.turnover_service.active_snapshot", return_value=(PROJECT_ROOT, None)), self.assertRaises(TurnoverRuntimeError) as raised:
                runtime.run(
                    principal_hash="principal-hash",
                    conversation_id="conversation-id",
                    request_id="request-id",
                    end_date_exclusive="2026-08-29",
                )

            self.assertEqual("report_contract_not_released", raised.exception.code)
            self.assertEqual([], query_client.calls)
            self.assertEqual(
                "周转报表合同尚未发布，未生成图片",
                raised.exception.public_message,
            )
            self.assertNotIn(REPORT_METRIC_ID, raised.exception.public_message)
            self.assertNotIn("candidate-0.1.0", raised.exception.public_message)

    def test_explicit_smoke_override_reaches_quality_gate_but_not_image(self) -> None:
        with TemporaryDirectory(prefix="turnover-release-override-") as directory:
            query_client = _CountingQueryClient()
            runtime = self._runtime(
                query_client,
                artifact_root=directory,
                allow_unapproved_contracts=True,
            )
            with self.assertRaises(TurnoverRuntimeError) as raised:
                runtime.run(
                    principal_hash="principal-hash",
                    conversation_id="conversation-id",
                    request_id="request-id",
                    end_date_exclusive="2026-08-29",
                )

            self.assertEqual("report_quality_blocked", raised.exception.code)
            self.assertEqual("quality_validation", raised.exception.reason)
            self.assertEqual(1, len(query_client.calls))

    def test_environment_smoke_override_is_opt_in(self) -> None:
        with TemporaryDirectory(prefix="turnover-release-env-override-") as directory:
            query_client = _CountingQueryClient()
            with patch.dict(os.environ, {ALLOW_UNAPPROVED_ENV: "true"}):
                runtime = self._runtime(
                    query_client,
                    artifact_root=directory,
                    allow_unapproved_contracts=None,
                )
                with self.assertRaises(TurnoverRuntimeError) as raised:
                    runtime.run(
                        principal_hash="principal-hash",
                        conversation_id="conversation-id",
                        request_id="request-id",
                        end_date_exclusive="2026-08-29",
                    )

            self.assertEqual("report_quality_blocked", raised.exception.code)
            self.assertEqual(1, len(query_client.calls))

    def test_override_argument_must_be_boolean(self) -> None:
        with TemporaryDirectory(prefix="turnover-release-override-type-") as directory:
            with self.assertRaises(ValueError):
                self._runtime(
                    _CountingQueryClient(),
                    artifact_root=directory,
                    allow_unapproved_contracts="false",
                )

    def test_released_contract_requires_exact_source_versions(self) -> None:
        with TemporaryDirectory(prefix="turnover-release-exact-") as directory:
            query_client = _CountingQueryClient()
            knowledge = _ReleasedKnowledge(source_version="1.0.0")
            runtime = self._runtime(
                query_client,
                artifact_root=directory,
                knowledge=knowledge,
            )
            with self.assertRaises(TurnoverRuntimeError) as raised:
                runtime.run(
                    principal_hash="principal-hash",
                    conversation_id="conversation-id",
                    request_id="request-id",
                    end_date_exclusive="2026-08-29",
                )

            self.assertEqual("report_quality_blocked", raised.exception.code)
            self.assertEqual(1, len(query_client.calls))
            self.assertEqual(1, knowledge.verify_calls)

            mismatched_knowledge = _ReleasedKnowledge(
                source_version="2.0.0", manifest_source_version="1.0.0"
            )
            mismatched_runtime = self._runtime(
                _CountingQueryClient(),
                artifact_root=directory,
                knowledge=mismatched_knowledge,
            )
            with self.assertRaises(TurnoverRuntimeError) as mismatch:
                mismatched_runtime.run(
                    principal_hash="principal-hash",
                    conversation_id="conversation-id",
                    request_id="request-id-2",
                    end_date_exclusive="2026-08-29",
                )
            self.assertEqual("report_contract_not_released", mismatch.exception.code)
            self.assertEqual("released_source_mismatch", mismatch.exception.reason)


class TurnoverQualityControlTestCase(unittest.TestCase):
    """The repeated SQL controls must be consumed exactly once and fail closed."""

    def _runtime(self) -> TurnoverReportRuntime:
        return TurnoverReportRuntime(
            artifact_store=ArtifactStore(Path(".")),
            project_root=PROJECT_ROOT,
            query_client=_CountingQueryClient(),
            allow_unapproved_contracts=True,
        )

    def _rows(self) -> list[dict[str, object]]:
        controls = {
            "source_table_inventory_row_count": 2,
            "source_table_outbound_header_count": 2,
            "warehouse_scope_row_count": 2,
            "warehouse_center_row_count": 1,
            "warehouse_detail_row_count": 1,
            "warehouse_mapping_conflict_count": 0,
            "invalid_parent_mapping_count": 0,
            "unresolved_mapping_count": 0,
            "center_parent_anomaly_count": 0,
            "ambiguous_outbound_count": 0,
            "control_inventory_total_ea": "300",
            "control_outbound_total_ea": "70",
            "control_inventory_row_count": 2,
            "control_outbound_row_count": 2,
            "control_negative_stock_count": 0,
            "control_duplicate_inventory_key_count": 0,
            "control_negative_outbound_count": 0,
            "source_table_max_wms_updated_date": "2026-08-29 10:00:00",
        }
        rows: list[dict[str, object]] = []
        for warehouse in (
            {
                "wms_code": "CENTER-01",
                "warehouse_name": "中心仓",
                "wms_node": 10,
                "wms_type": 10,
                "belonging_center": "中心仓",
                "belonging_center_code": "CENTER-01",
                "available_ea": "200",
                "outbound_7d_ea": "40",
            },
            {
                "wms_code": "DETAIL-01",
                "warehouse_name": "分仓一",
                "wms_node": 12,
                "wms_type": 11,
                "belonging_center": "中心仓",
                "belonging_center_code": "CENTER-01",
                "available_ea": "100",
                "outbound_7d_ea": "30",
            },
        ):
            row = dict(warehouse)
            row.update(
                {
                    "inventory_row_count": 1,
                    "outbound_row_count": 1,
                    "negative_stock_count": 0,
                    "negative_outbound_count": 0,
                    "source_max_stock_change_time": "2026-08-29 10:00:00",
                    "source_max_ship_at": "2026-08-28 10:00:00",
                    **controls,
                }
            )
            rows.append(row)
        return rows

    def _report(self, rows: list[dict[str, object]]):
        return build_turnover_report(
            rows,
            "2026-08-22",
            "2026-08-29",
            profile_name="测试口径",
        )

    def test_repeated_controls_are_not_summed_by_warehouse_count(self) -> None:
        rows = self._rows()
        controls = self._runtime()._quality_controls(rows, self._report(rows))
        self.assertEqual(Decimal("300"), controls["control_inventory_total_ea"])
        self.assertEqual(Decimal("70"), controls["control_outbound_total_ea"])

    def test_empty_inventory_source_blocks_even_with_zero_warn_profile(self) -> None:
        from app.turnover_service import TURNOVER_PROFILES
        rows = self._rows()
        for row in rows:
            row["inventory_base_source_row_count"] = 0
        with self.assertRaises(TurnoverRuntimeError) as raised:
            self._runtime()._quality_controls(rows, self._report(rows), profile=TURNOVER_PROFILES["actual_finished_goods"])
        self.assertEqual("inventory_source_empty", raised.exception.reason)

    def test_control_total_mismatch_blocks_image(self) -> None:
        rows = self._rows()
        for row in rows:
            row["control_inventory_total_ea"] = "301"
        with self.assertRaises(TurnoverRuntimeError) as raised:
            self._runtime()._quality_controls(rows, self._report(rows))
        self.assertEqual("inventory_control_total_mismatch", raised.exception.reason)

    def test_invalid_parent_counter_blocks_image(self) -> None:
        rows = self._rows()
        for row in rows:
            row["invalid_parent_mapping_count"] = 1
        with self.assertRaises(TurnoverRuntimeError) as raised:
            self._runtime()._quality_controls(rows, self._report(rows))
        self.assertEqual("warehouse_mapping_invalid_parent", raised.exception.reason)

    def test_center_parent_anomaly_is_a_warning_not_a_mapping_failure(self) -> None:
        rows = self._rows()
        for row in rows:
            row["center_parent_anomaly_count"] = 2
        controls = self._runtime()._quality_controls(rows, self._report(rows))
        self.assertEqual(2, controls["center_parent_anomaly_count"])

    def test_duplicate_inventory_composite_key_blocks_image(self) -> None:
        rows = self._rows()
        for row in rows:
            row["control_duplicate_inventory_key_count"] = 1
        with self.assertRaises(TurnoverRuntimeError) as raised:
            self._runtime()._quality_controls(rows, self._report(rows))
        self.assertEqual("inventory_composite_key_duplicate", raised.exception.reason)


class TurnoverChatArtifactTestCase(unittest.TestCase):
    def test_report_query_failure_is_specific_and_replayable(self):
        for reason in ("query_timeout", "backend_unavailable", "backend_rejected", "result_truncated"):
            with self.subTest(reason=reason):
                failure = DorisResultError(reason)
                self.runtime.error = failure
                headers = {"X-CSRF-Token": self.csrf, "Idempotency-Key": f"report_failure_{reason}"}
                body = {"conversation_id": self.conversation_id, "question": "生成中心仓和分仓14日周转数据图片"}
                response = self.client.post("/v1/chat", headers=headers, json=body)
                self.assertEqual(failure.http_status, response.status_code)
                self.assertEqual(failure.code, response.json()["error"]["code"])
                self.assertEqual(failure.result_state, response.json()["error"]["result_state"])
                self.assertNotIn("artifacts", response.json())
                calls = len(self.runtime.calls)
                replay = self.client.post("/v1/chat", headers=headers, json=body)
                self.assertEqual(response.json(), replay.json())
                self.assertEqual(calls, len(self.runtime.calls))

    def test_natural_report_variants_do_not_invoke_model(self):
        self.runner.error = ChatTimeoutError("model must not run for a known report")
        for wording, days in (("14日", 14), ("十四天", 14), ("二十一天", 21), ("30个完整自然日", 30)):
            with self.subTest(wording=wording):
                response = self.client.post("/v1/chat", headers={"X-CSRF-Token": self.csrf}, json={
                    "conversation_id": self.conversation_id,
                    "question": f"生成中心仓和分仓{wording}周转数据图片",
                })
                self.assertEqual(200, response.status_code)
                self.assertEqual("completed", response.json()["status"])
                self.assertEqual(days, self.runtime.calls[-1]["window_days"])
        self.assertEqual([], self.runner.calls)

    def test_standard_fourteen_day_template_executes_without_reconfirming(self):
        response = self.client.post(
            "/v1/chat",
            headers={"X-CSRF-Token": self.csrf},
            json={
                "conversation_id": self.conversation_id,
                "question": "生成中心仓和分仓近14天周转数据图片",
            },
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual("completed", response.json()["status"])
        self.assertEqual(14, self.runtime.calls[-1]["window_days"])
        self.assertEqual([], self.runner.calls)

    def test_business_w37_selection_is_candidate_only_and_never_queries(self):
        pending = self.client.post(
            "/v1/chat",
            headers={"X-CSRF-Token": self.csrf},
            json={
                "conversation_id": self.conversation_id,
                "question": "复现W37近14天业务表",
                "report_kind": "center_warehouse_turnover_7d",
            },
        ).json()
        response = self.client.post(
            "/v1/chat",
            headers={"X-CSRF-Token": self.csrf},
            json={
                "conversation_id": self.conversation_id,
                "clarification_token": pending["clarification"]["token"],
                "clarification_answers": [
                    {
                        "question_id": "turnover_inventory_profile",
                        "option_id": "available_plus_transit_finished_goods",
                    },
                    {
                        "question_id": "turnover_window_semantics",
                        "option_id": "business_w37_source_window",
                    },
                ],
            },
        )
        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual("blocked", body["status"])
        self.assertEqual("blocked", body["outcome"]["state"])
        self.assertIn("实际为7天", body["message"])
        self.assertNotIn("artifacts", body)
        self.assertEqual([], self.runtime.calls)
        self.assertEqual([], self.runner.calls)

    def test_report_selection_keeps_original_window(self):
        pending = self.client.post("/v1/chat", headers={"X-CSRF-Token": self.csrf}, json={
            "conversation_id": self.conversation_id,
            "question": "生成中心仓和分仓周转数据图片",
            "report_kind": "center_warehouse_turnover_7d",
            "window_days": 14,
            "report_end_date_exclusive": "2026-08-29",
        }).json()
        response = self.client.post("/v1/chat", headers={"X-CSRF-Token": self.csrf}, json={
            "conversation_id": self.conversation_id,
            "clarification_token": pending["clarification"]["token"],
            "clarification_answers": [{"question_id": "turnover_inventory_profile", "option_id": "actual_finished_goods"}],
        })
        self.assertEqual(200, response.status_code)
        self.assertEqual(14, self.runtime.calls[-1]["window_days"])
        self.assertEqual("2026-08-29", self.runtime.calls[-1]["end_date_exclusive"])

    def setUp(self) -> None:
        self.directory = TemporaryDirectory(prefix="turnover-chat-test-")
        root = Path(self.directory.name)
        self.store = ChatSessionStore(root / "sessions.sqlite3")
        self.artifacts = ArtifactStore(root / "artifacts")
        self.runner = _Runner()
        self.runtime = _ReportRuntime(self.artifacts)
        self.app = create_chat_app(
            self.runner,
            session_store=self.store,
            identity_resolver=IdentityResolver(mode="anonymous"),
            artifact_store=self.artifacts,
            turnover_runtime=self.runtime,
            enforce_loopback=False,
        )
        self.client = TestClient(self.app)
        bootstrap = self.client.get("/v1/session").json()
        self.csrf = bootstrap["csrf_token"]
        self.conversation_id = self.client.post(
            "/v1/conversations", headers={"X-CSRF-Token": self.csrf}
        ).json()["conversation_id"]

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_report_choice_is_meaningful_and_image_is_owner_bound(self) -> None:
        pending = self.client.post(
            "/v1/chat",
            headers={"X-CSRF-Token": self.csrf},
            json={
                "conversation_id": self.conversation_id,
                "question": "生成中心仓和分仓周转数据图片",
                "report_kind": "center_warehouse_turnover_7d",
            },
        )
        self.assertEqual(200, pending.status_code)
        pending_payload = pending.json()
        labels = [
            option["label"]
            for question in pending_payload["clarification"]["questions"]
            for option in question["options"]
        ]
        self.assertNotIn("该选项", labels)
        self.assertEqual(0, len(self.runner.calls))
        token = pending_payload["clarification"]["token"]
        completed = self.client.post(
            "/v1/chat",
            headers={"X-CSRF-Token": self.csrf},
            json={
                "conversation_id": self.conversation_id,
                "clarification_token": token,
                "clarification_answers": [
                    {
                        "question_id": "turnover_inventory_profile",
                        "option_id": "actual_finished_goods",
                    }
                ],
            },
        )
        self.assertEqual(200, completed.status_code)
        payload = completed.json()
        self.assertEqual("completed", payload["outcome"]["state"])
        self.assertEqual(1, len(payload["artifacts"]))
        self.assertNotIn("sql", payload)
        artifact_url = payload["artifacts"][0]["url"]
        self.assertEqual(200, self.client.get(artifact_url).status_code)

        foreign = TestClient(self.app)
        foreign_bootstrap = foreign.get("/v1/session").json()
        self.assertEqual(404, foreign.get(artifact_url).status_code)
        self.assertNotEqual(foreign_bootstrap.get("conversation_id"), self.conversation_id)

    def test_report_runtime_suppresses_model_follow_up_clarification(self) -> None:
        self.runner.clarifies = True
        pending = self.client.post(
            "/v1/chat",
            headers={"X-CSRF-Token": self.csrf},
            json={
                "conversation_id": self.conversation_id,
                "question": "生成中心仓和分仓周转数据图片",
                "report_kind": "center_warehouse_turnover_7d",
            },
        )
        token = pending.json()["clarification"]["token"]
        completed = self.client.post(
            "/v1/chat",
            headers={"X-CSRF-Token": self.csrf},
            json={
                "conversation_id": self.conversation_id,
                "report_kind": "center_warehouse_turnover_7d",
                "clarification_token": token,
                "clarification_answers": [
                    {
                        "question_id": "turnover_inventory_profile",
                        "option_id": "actual_finished_goods",
                    }
                ],
            },
        )
        self.assertEqual(200, completed.status_code)
        payload = completed.json()
        self.assertEqual("completed", payload["outcome"]["state"])
        self.assertFalse(payload["clarification"]["required"])
        self.assertEqual(1, len(payload["artifacts"]))

    def test_report_does_not_wait_for_a_second_codex_turn(self) -> None:
        self.runner.error = ChatTimeoutError("model timeout")
        pending = self.client.post(
            "/v1/chat",
            headers={"X-CSRF-Token": self.csrf},
            json={
                "conversation_id": self.conversation_id,
                "question": "生成中心仓和分仓周转数据图片",
                "report_kind": "center_warehouse_turnover_7d",
            },
        )
        token = pending.json()["clarification"]["token"]
        completed = self.client.post(
            "/v1/chat",
            headers={"X-CSRF-Token": self.csrf},
            json={
                "conversation_id": self.conversation_id,
                "report_kind": "center_warehouse_turnover_7d",
                "clarification_token": token,
                "clarification_answers": [
                    {
                        "question_id": "turnover_inventory_profile",
                        "option_id": "actual_finished_goods",
                    }
                ],
            },
        )
        self.assertEqual(200, completed.status_code)
        payload = completed.json()
        self.assertEqual("completed", payload["status"])
        self.assertEqual("completed", payload["outcome"]["state"])
        self.assertEqual(1, len(payload["artifacts"]))
        self.assertEqual([], self.runner.calls)
        self.assertEqual(1, len(self.runtime.calls))

    def test_blocked_report_never_reports_model_live_result_as_completed(self) -> None:
        from app.turnover_service import TurnoverRuntimeError

        self.runtime.error = TurnoverRuntimeError(
            "report_contract_not_released",
            "周转报表合同尚未发布，未生成图片",
            reason="released_manifest_missing",
        )
        pending = self.client.post(
            "/v1/chat",
            headers={"X-CSRF-Token": self.csrf},
            json={
                "conversation_id": self.conversation_id,
                "question": "生成中心仓和分仓周转数据图片",
                "report_kind": "center_warehouse_turnover_7d",
            },
        )
        token = pending.json()["clarification"]["token"]
        completed = self.client.post(
            "/v1/chat",
            headers={"X-CSRF-Token": self.csrf},
            json={
                "conversation_id": self.conversation_id,
                "report_kind": "center_warehouse_turnover_7d",
                "clarification_token": token,
                "clarification_answers": [
                    {
                        "question_id": "turnover_inventory_profile",
                        "option_id": "actual_finished_goods",
                    }
                ],
            },
        )
        self.assertEqual(200, completed.status_code)
        payload = completed.json()
        self.assertEqual("blocked", payload["status"])
        self.assertEqual("blocked", payload["outcome"]["state"])
        self.assertEqual("blocked", payload["process"]["state"])
        self.assertEqual("报表口径尚未审批发布，暂未生成图片", payload["message"])
        self.assertNotIn("artifacts", payload)


if __name__ == "__main__":
    unittest.main()

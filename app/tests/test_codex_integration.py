from __future__ import annotations

import asyncio
from contextlib import nullcontext
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import tomllib
import unittest
from unittest.mock import MagicMock, patch

from mcp import Client
from openai_codex import ApprovalMode, Sandbox
from openai_codex.generated.v2_all import ReasoningEffort

from app.codex_agent import (
    assert_codex_environment_isolated,
    business_timezone,
    codex_model_config_overrides,
    codex_model_settings,
    codex_mcp_config_overrides,
    codex_knowledge_context,
    isolated_codex_home,
    mcp_call_summaries,
    query_execution_evidence,
    query_execution_evidence_from_items,
    project_codex_home,
    run_codex_question,
    sensitive_environment_keys,
    _model_query_plan,
    _max_host_query_steps,
    _last_verified_business_from_items,
)
from app.codex_mcp_bridge import _verify_live_payload, CodexMcpBridgeError
from app.mcp_server import HttpQueryBackend, build_mcp_server
from app.knowledge_activation import active_snapshot


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class RecordingBackend:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def query(self, *, sql: str, purpose: str | None = None) -> dict[str, object]:
        self.calls.append({"sql": sql, "purpose": purpose})
        return {
            "request_id": "request-1",
            "status": "succeeded",
            "source_tables": ["ywzt_ods.example"],
            "execution": {
                "columns": ["answer"],
                "rows": [{"answer": 1}],
                "row_count": 1,
                "truncated": False,
                "executor_name": "doris",
                "is_mock": False,
            },
        }


def live_mcp_result() -> SimpleNamespace:
    return SimpleNamespace(
        structured_content=RecordingBackend().query(sql="test fixture"),
        content=[],
    )


class CodexMcpIntegrationTestCase(unittest.TestCase):
    def test_server_exposes_exactly_one_read_only_sql_tool(self) -> None:
        async def verify() -> None:
            async with Client(build_mcp_server(RecordingBackend())) as client:
                result = await client.list_tools()
                self.assertEqual(["query_doris_sql"], [tool.name for tool in result.tools])
                tool = result.tools[0]
                self.assertTrue(tool.annotations.read_only_hint)
                self.assertFalse(tool.annotations.destructive_hint)
                self.assertIn("sql", tool.input_schema["properties"])
                self.assertIn("purpose", tool.input_schema["properties"])

        asyncio.run(verify())

    def test_query_tool_forwards_only_sql_and_purpose(self) -> None:
        async def verify() -> None:
            backend = RecordingBackend()
            async with Client(build_mcp_server(backend)) as client:
                result = await client.call_tool(
                    "query_doris_sql",
                    {
                        "sql": "SELECT COUNT(*) AS answer FROM ywzt_ods.example",
                        "purpose": "count rows",
                    },
                )
            self.assertFalse(result.is_error)
            self.assertEqual("succeeded", result.structured_content["status"])
            self.assertEqual(
                [
                    {
                        "sql": "SELECT COUNT(*) AS answer FROM ywzt_ods.example",
                        "purpose": "count rows",
                    }
                ],
                backend.calls,
            )

        asyncio.run(verify())

    def test_http_backend_refuses_non_loopback_urls(self) -> None:
        for url in ("https://example.com", "http://192.0.2.1:8010", "file:///tmp/x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                HttpQueryBackend(url)


class CodexEnvironmentIsolationTestCase(unittest.TestCase):
    def test_host_query_step_budget_is_tunable_but_finite(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_MAX_HOST_QUERY_STEPS", None)
            self.assertEqual(32, _max_host_query_steps())
            os.environ["AGENT_MAX_HOST_QUERY_STEPS"] = "96"
            self.assertEqual(96, _max_host_query_steps())
            os.environ["AGENT_MAX_HOST_QUERY_STEPS"] = "999999"
            self.assertEqual(128, _max_host_query_steps())
            os.environ["AGENT_MAX_HOST_QUERY_STEPS"] = "invalid"
            self.assertEqual(32, _max_host_query_steps())

    def test_model_query_plan_is_private_and_requires_one_select_payload(self) -> None:
        plan = _model_query_plan(
            '{"outcome":"query_plan","sql":"SELECT 1","purpose":"probe"}'
        )
        self.assertEqual({"sql": "SELECT 1", "purpose": "probe"}, plan)
        self.assertIsNone(_model_query_plan('{"outcome":"completed","answer":"1"}'))
        self.assertIsNone(_model_query_plan("not json"))

    def test_model_query_plan_can_select_a_released_reviewed_recovery_route(self) -> None:
        plan = _model_query_plan(
            '{"outcome":"query_plan","sql":"SELECT 1","purpose":"detail",'
            '"reviewed_route":"sales_outbound_details"}'
        )
        self.assertEqual("sales_outbound_details", plan["reviewed_route"])
        self.assertIsNone(
            _model_query_plan(
                '{"outcome":"query_plan","sql":"SELECT 1","purpose":"detail",'
                '"reviewed_route":"not_a_released_route"}'
            )
        )

    def test_model_query_plan_accepts_json_fences_without_executing_other_languages(self) -> None:
        payload = '{"outcome":"query_plan","sql":"SELECT 1","purpose":"probe"}'
        for language in ("json", "JSON", ""):
            with self.subTest(language=language):
                self.assertEqual(
                    {"sql": "SELECT 1", "purpose": "probe"},
                    _model_query_plan("```" + language + "\n" + payload + "\n```"),
                )
        self.assertIsNone(_model_query_plan("```python\n" + payload + "\n```"))
        self.assertIsNone(_model_query_plan('```json\n{"outcome":"query_plan"}\n```'))

    @patch("app.codex_agent.CodexMcpBridge")
    @patch("app.codex_agent.isolated_codex_home")
    @patch("app.codex_agent.assert_codex_environment_isolated")
    @patch("openai_codex.CodexConfig")
    @patch("openai_codex.Codex")
    def test_host_bridge_turn_converts_model_plan_to_private_live_answer(
        self,
        codex_class: MagicMock,
        config_class: MagicMock,
        _isolation_check: MagicMock,
        isolated_home: MagicMock,
        bridge_class: MagicMock,
    ) -> None:
        isolated_home.return_value = nullcontext(Path("C:/isolated-codex-home"))
        codex = codex_class.return_value.__enter__.return_value
        thread = codex.thread_start.return_value
        thread.id = "thread-bridge"
        plan_result = SimpleNamespace(
            id="turn-plan", status="completed",
            final_response=json.dumps({
                "outcome": "query_plan", "sql": "SELECT wms_code FROM ywzt_ods.master_wms", "purpose": "bridge test"
            }), items=[], usage=None,
        )
        answer_result = SimpleNamespace(
            id="turn-answer", status="completed",
            final_response=json.dumps({
                "outcome": "completed", "answer": "已完成查询"
            }), items=[], usage=None,
        )
        thread.run.side_effect = [plan_result, answer_result]
        bridge_class.return_value.query_sequence.return_value = nullcontext(bridge_class.return_value)
        bridge_class.return_value.query.return_value = {
            "status": "succeeded",
            "execution": {
                "columns": ["answer"], "rows": [{"answer": 1}], "row_count": 1,
                "truncated": False, "executor_name": "doris", "is_mock": False,
            },
        }

        response = run_codex_question(
            "查询桥接测试",
            project_root=PROJECT_ROOT,
            backend_url="http://127.0.0.1:8010",
        )

        self.assertEqual("completed", response["interaction"]["outcome"])
        self.assertEqual("已完成查询", response["interaction"]["answer"])
        self.assertTrue(response["query_execution"]["live_business_result_executed"])
        self.assertEqual(1, len(response["mcp_calls"]))
        bridge_class.return_value.query.assert_called_once_with(
            sql="SELECT wms_code FROM ywzt_ods.master_wms", purpose="bridge test"
        )
        self.assertNotIn("SELECT 1", json.dumps(response["interaction"], ensure_ascii=False))

    @patch("app.codex_agent.CodexMcpBridge")
    @patch("app.codex_agent.isolated_codex_home")
    @patch("app.codex_agent.assert_codex_environment_isolated")
    @patch("openai_codex.CodexConfig")
    @patch("openai_codex.Codex")
    def test_missing_native_tool_call_requests_plan_in_same_thread(
        self,
        codex_class: MagicMock,
        config_class: MagicMock,
        _isolation_check: MagicMock,
        isolated_home: MagicMock,
        bridge_class: MagicMock,
    ) -> None:
        isolated_home.return_value = nullcontext(Path("C:/isolated-codex-home"))
        codex = codex_class.return_value.__enter__.return_value
        thread = codex.thread_start.return_value
        thread.id = "thread-plan-recovery"
        first = SimpleNamespace(
            id="turn-first", status="completed", final_response="我正在处理", items=[], usage=None
        )
        planned = SimpleNamespace(
            id="turn-plan-recovery", status="completed",
            final_response=json.dumps({
                "outcome": "query_plan", "sql": "SELECT wms_code FROM ywzt_ods.master_wms", "purpose": "plan recovery"
            }), items=[], usage=None,
        )
        rendered = SimpleNamespace(
            id="turn-rendered", status="completed",
            final_response=json.dumps({"outcome": "completed", "answer": "已完成查询"}),
            items=[], usage=None,
        )
        thread.run.side_effect = [first, planned, rendered]
        bridge_class.return_value.query_sequence.return_value = nullcontext(bridge_class.return_value)
        bridge_class.return_value.query.return_value = {
            "status": "succeeded",
            "execution": {
                "columns": ["answer"], "rows": [{"answer": 1}], "row_count": 1,
                "truncated": False, "executor_name": "doris", "is_mock": False,
            },
        }

        response = run_codex_question(
            "查询桥接计划恢复测试",
            project_root=PROJECT_ROOT,
            backend_url="http://127.0.0.1:8010",
        )

        self.assertEqual("completed", response["interaction"]["outcome"])
        self.assertTrue(response["query_execution"]["live_business_result_executed"])
        self.assertEqual(3, thread.run.call_count)
        self.assertIn(
            "HOST_QUERY_PLAN_REQUIRED",
            thread.run.call_args_list[1].args[0],
        )
        bridge_class.return_value.query.assert_called_once_with(
            sql="SELECT wms_code FROM ywzt_ods.master_wms", purpose="plan recovery"
        )
        self.assertNotIn("SELECT 1", json.dumps(response["interaction"], ensure_ascii=False))

    def test_mcp_bridge_accepts_only_verified_live_payloads(self) -> None:
        payload = {
            "status": "succeeded",
            "execution": {
                "executor_name": "doris",
                "is_mock": False,
                "truncated": False,
                "columns": ["value"],
                "rows": [{"value": 1}],
                "row_count": 1,
            },
        }
        _verify_live_payload(payload)
        for key, value in (("is_mock", True), ("truncated", True), ("row_count", 2)):
            invalid = json.loads(json.dumps(payload))
            invalid["execution"][key] = value
            with self.subTest(key=key), self.assertRaises(CodexMcpBridgeError):
                _verify_live_payload(invalid)

    def test_structured_knowledge_context_contains_metrics_and_sources(self) -> None:
        context = json.loads(codex_knowledge_context(PROJECT_ROOT))
        manifest = context["governed_runtime_snapshot"]["planning_manifest"]
        self.assertEqual(
            set(manifest["contracts"]["metrics"]),
            {f"{metric['id']}@{metric['version']}" for metric in context["metrics"]},
        )
        self.assertEqual(active_snapshot(PROJECT_ROOT)[1], context["runtime"]["release_digest"])
        self.assertGreaterEqual(len(context["sources"]), 7)
        sales_metric = next(
            metric
            for metric in context["metrics"]
            if metric["id"] == "sales.outbound_actual"
        )
        self.assertEqual("2.0.0", sales_metric["version"])
        self.assertEqual([700, 900, 701, 901], sales_metric["filters"]["completion_statuses"])
        selected_ids = {metric["id"] for metric in context["metrics"]}
        self.assertIn("inventory.turnover_finished_goods_realtime", selected_ids)
        self.assertIn("inventory.turnover_finished_goods_snapshot", selected_ids)
        self.assertNotIn("inventory.turnover_mtd_realtime", selected_ids)
        self.assertFalse(
            next(
                item["selected"]
                for item in context["contract_version_index"]
                if item["id"] == "sales.outbound_actual" and item["version"] == "1.0.0"
            )
        )
        profile_ids = {profile["id"] for profile in context["profiles"]}
        self.assertTrue(
            {"hjxh_business_reference_centers", "hjxh_operating_centers"}
            <= profile_ids
        )
        self.assertIn("rules", context)
        self.assertIn("release_manifests", context)
        self.assertIn("templates", context)
        self.assertEqual(16, context["coverage"]["imported_logic_family_count"])
        catalog = context["imported_logic_catalog"]
        self.assertEqual("imported_candidate", catalog["status"])
        self.assertEqual([], catalog["source_file_audit"]["missing_files"])
        self.assertGreater(catalog["completeness_summary"]["incomplete_candidate_count"], 0)
        self.assertEqual(52, context["coverage"]["known_unlisted_source_file_count"])
        self.assertEqual(28, context["coverage"]["source_sql_files_reviewed"])

    def test_request_context_omits_test_and_history_bulk_not_contract_rules(self) -> None:
        full = json.loads(codex_knowledge_context(PROJECT_ROOT))
        compact = json.loads(codex_knowledge_context(PROJECT_ROOT, question="昨天实际销售出库量", compact=True))
        self.assertNotIn("golden_tests", compact)
        self.assertNotIn("imported_thread_references", compact)
        full_source_ids = {source["id"] for source in full["sources"]}
        compact_source_ids = {source["id"] for source in compact["sources"]}
        sales_metric = next(
            metric for metric in compact["metrics"] if metric["id"] == "sales.outbound_actual"
        )
        required_sources = set(sales_metric["sources"]["required"])
        self.assertTrue(required_sources <= full_source_ids)
        self.assertTrue(required_sources <= compact_source_ids)
        self.assertTrue(set(compact["rules"]) <= set(full["rules"]))
        self.assertIn("rules/agent-policy.yaml", compact["rules"])
        self.assertEqual(
            ["sales.finished_goods_exceptions", "sales.outbound_actual"],
            compact["retrieval"]["loaded_metric_ids"],
        )
        exception_metric = next(
            metric for metric in compact["metrics"]
            if metric["id"] == "sales.finished_goods_exceptions"
        )
        self.assertEqual("explicit_finished_goods_exceptions", exception_metric["variant"])
        self.assertTrue(exception_metric["applicability"]["not_applicable_when"])
        self.assertEqual("2.0.0", exception_metric["dependency_versions"]["sales.outbound_actual"])
        self.assertFalse(compact["retrieval"]["full_contracts_truncated"])
        self.assertLess(len(json.dumps(compact)), len(json.dumps(full)))

    def test_question_gets_a_candidate_logic_family_routing_hint(self) -> None:
        context = json.loads(
            codex_knowledge_context(PROJECT_ROOT, question="查询线下可提货箱数")
        )
        hint = context["request_routing_hint"]
        self.assertEqual("matched", hint["status"])
        self.assertEqual("pickup.offline_availability", hint["recommended_family_id"])

    def test_query_execution_evidence_requires_a_successful_doris_call(self) -> None:
        self.assertEqual(
            {
                "live_query_executed": True,
                "live_business_result_executed": True,
                "query_call_count": 1,
                "successful_query_call_count": 1,
                "metadata_query_call_count": 0,
                "successful_business_query_call_count": 1,
                "failed_query_call_count": 0,
                "unexpected_tool_call_count": 0,
                "status": "live_result",
            },
            query_execution_evidence(
                [
                    {
                        "server": "doris_query_agent",
                        "tool": "query_doris_sql",
                        "status": "completed",
                    }
                ]
            ),
        )
        self.assertFalse(query_execution_evidence([])["live_query_executed"])

    def test_metadata_only_call_is_not_a_business_result(self) -> None:
        item = SimpleNamespace(
            type="mcpToolCall",
            server="doris_query_agent",
            tool="query_doris_sql",
            status=SimpleNamespace(value="completed"),
            read_only_hint=True,
            duration_ms=1,
            arguments={
                "sql": "SELECT column_name FROM information_schema.columns"
            },
            result=live_mcp_result(),
        )
        evidence = query_execution_evidence_from_items([item])
        self.assertTrue(evidence["live_query_executed"])
        self.assertFalse(evidence["live_business_result_executed"])
        self.assertEqual("metadata_only", evidence["status"])

    def test_business_query_with_freshness_cte_is_still_a_business_result(self) -> None:
        item = SimpleNamespace(
            type="mcpToolCall",
            server="doris_query_agent",
            tool="query_doris_sql",
            status=SimpleNamespace(value="completed"),
            read_only_hint=True,
            duration_ms=1,
            arguments={
                "sql": """WITH freshness AS (
                    SELECT MAX(update_time) AS latest
                    FROM information_schema.tables
                )
                SELECT COUNT(*) AS total
                FROM ywzt_ods.sku_inventory i
                CROSS JOIN freshness"""
            },
            result=live_mcp_result(),
        )
        evidence = query_execution_evidence_from_items([item])
        self.assertTrue(evidence["live_business_result_executed"])
        self.assertEqual("live_result", evidence["status"])

    def test_metadata_cte_and_connectivity_probe_are_not_business_results(self) -> None:
        for sql in (
            "WITH t AS (SELECT column_name FROM information_schema.columns) SELECT * FROM t",
            "SELECT 1 AS connected",
        ):
            with self.subTest(sql=sql):
                item = SimpleNamespace(
                    type="mcpToolCall", server="doris_query_agent", tool="query_doris_sql",
                    status="completed", arguments={"sql": sql}, result=live_mcp_result(),
                )
                evidence = query_execution_evidence_from_items([item])
                self.assertEqual("metadata_only", evidence["status"])

    def test_completed_transport_requires_valid_live_backend_result(self) -> None:
        for mutation in (
            {"executor_name": "mock"}, {"is_mock": True}, {"truncated": True},
            {"row_count": 5}, {"row_count": True}, {"rows": ["invalid"]},
        ):
            with self.subTest(mutation=mutation):
                result = live_mcp_result()
                result.structured_content["execution"].update(mutation)
                item = SimpleNamespace(
                    type="mcpToolCall", server="doris_query_agent", tool="query_doris_sql",
                    status="completed", arguments={"sql": "SELECT * FROM ywzt_ods.example"}, result=result,
                )
                evidence = query_execution_evidence_from_items([item])
                self.assertEqual("no_live_result", evidence["status"])
                self.assertEqual(1, evidence["failed_query_call_count"])

    def test_missing_or_rejected_backend_payload_does_not_count_as_success(self) -> None:
        for result in (None, {}, {"isError": True}, {"structuredContent": {"status": "rejected"}}):
            with self.subTest(result=result):
                item = SimpleNamespace(
                    type="mcpToolCall", server="doris_query_agent", tool="query_doris_sql",
                    status="completed", result=result,
                )
                self.assertFalse(query_execution_evidence_from_items([item])["live_query_executed"])

    def test_text_mcp_result_and_desc_sort_are_supported(self) -> None:
        payload = live_mcp_result().structured_content
        item = SimpleNamespace(
            type="mcpToolCall", server="doris_query_agent", tool="query_doris_sql",
            status="completed", arguments={"sql": "SELECT answer FROM ywzt_ods.example ORDER BY answer DESC"},
            result={"content": [{"type": "text", "text": json.dumps(payload)}]},
        )
        self.assertFalse(query_execution_evidence_from_items([item])["live_business_result_executed"])
        self.assertEqual("invalid_result", query_execution_evidence_from_items([item])["normalized_result_state"])

    def test_verified_rows_supply_missing_gateway_columns(self) -> None:
        for columns in (None, []):
            with self.subTest(columns=columns):
                payload = live_mcp_result().structured_content
                payload["execution"]["columns"] = columns
                item = SimpleNamespace(
                    type="mcpToolCall", server="doris_query_agent", tool="query_doris_sql",
                    status="completed", arguments={"sql": "SELECT answer FROM ywzt_ods.example"},
                    result={"structuredContent": payload},
                )
                expected = None if columns is None else {"columns": [], "rows": [{"answer": 1}]}
                self.assertEqual(expected, _last_verified_business_from_items([item]))

    def test_invalid_or_metadata_result_cannot_supply_business_columns(self) -> None:
        payload = live_mcp_result().structured_content
        payload["execution"].pop("columns")
        item = SimpleNamespace(
            type="mcpToolCall", server="doris_query_agent", tool="query_doris_sql",
            status="completed", arguments={"sql": "SELECT 1"},
            result={"structuredContent": payload},
        )
        self.assertIsNone(_last_verified_business_from_items([item]))
        item.arguments = {"sql": "SELECT answer FROM ywzt_ods.example"}
        payload["execution"]["truncated"] = True
        self.assertIsNone(_last_verified_business_from_items([item]))

    def test_sensitive_environment_is_rejected_without_values_in_error(self) -> None:
        environment = {
            "PATH": "safe",
            "DORIS_PASSWORD": "do-not-report",
            "AGENT_TRUSTED_RELEASE_SHA256": "digest-do-not-report",
            "OPENAI_API_KEY": "model-key-do-not-report",
            "AGENT_CHAT_IDENTITY_SECRET": "identity-do-not-report",
            "AGENT_CHAT_PRINCIPAL_PEPPER": "pepper-do-not-report",
            "AGENT_CHAT_SESSION_DB_PATH": "session-path-do-not-report",
            "AGENT_INSTALLATION_ID_PATH": "installation-path-do-not-report",
        }
        self.assertEqual(
            (
                "AGENT_CHAT_IDENTITY_SECRET",
                "AGENT_CHAT_PRINCIPAL_PEPPER",
                "AGENT_CHAT_SESSION_DB_PATH",
                "AGENT_INSTALLATION_ID_PATH",
                "AGENT_TRUSTED_RELEASE_SHA256",
                "DORIS_PASSWORD",
                "OPENAI_API_KEY",
            ),
            sensitive_environment_keys(environment),
        )
        with self.assertRaises(RuntimeError) as raised:
            assert_codex_environment_isolated(environment)
        message = str(raised.exception)
        self.assertIn("DORIS_PASSWORD", message)
        self.assertNotIn("do-not-report", message)
        self.assertNotIn("digest-do-not-report", message)
        self.assertNotIn("model-key-do-not-report", message)
        self.assertNotIn("identity-do-not-report", message)
        self.assertNotIn("pepper-do-not-report", message)
        self.assertNotIn("session-path-do-not-report", message)
        self.assertNotIn("installation-path-do-not-report", message)

    def test_clean_environment_is_allowed(self) -> None:
        assert_codex_environment_isolated({"PATH": "safe"})

    def test_business_timezone_is_validated(self) -> None:
        self.assertEqual("Asia/Shanghai", business_timezone({}))
        self.assertEqual(
            "UTC",
            business_timezone({"AGENT_BUSINESS_TIMEZONE": "UTC"}),
        )
        with self.assertRaises(RuntimeError):
            business_timezone({"AGENT_BUSINESS_TIMEZONE": "not/a-zone"})

    def test_model_settings_default_to_project_gateway(self) -> None:
        settings = codex_model_settings(environment={})
        self.assertEqual("gpt-6", settings["model"])
        self.assertEqual("low", settings["reasoning_effort"])
        self.assertEqual("agent_gateway", settings["model_provider"])
        self.assertEqual("http://192.168.110.9:8088", settings["base_url"])

        overrides = "\n".join(codex_model_config_overrides(settings))
        self.assertIn('model_provider="agent_gateway"', overrides)
        self.assertIn('model="gpt-6"', overrides)
        self.assertIn('model_reasoning_effort="low"', overrides)
        self.assertIn(
            'model_providers.agent_gateway.base_url="http://192.168.110.9:8088"',
            overrides,
        )
        self.assertIn("model_providers.agent_gateway.requires_openai_auth=true", overrides)
        self.assertIn('model_providers.agent_gateway.wire_api="responses"', overrides)
        self.assertNotIn("responses_websocket", overrides)
        self.assertNotIn("OPENAI_API_KEY", overrides)

    def test_model_settings_reject_invalid_values(self) -> None:
        with self.assertRaises(ValueError):
            codex_model_settings(reasoning_effort="impossible", environment={})
        with self.assertRaises(ValueError):
            codex_model_settings(base_url="file:///tmp/model", environment={})
        with self.assertRaises(ValueError):
            codex_model_settings(base_url="http://user:pass@example.com", environment={})
        with self.assertRaises(ValueError):
            codex_model_settings(base_url="http://203.0.113.10:8088", environment={})

    def test_project_codex_home_is_under_runtime_directory(self) -> None:
        root = PROJECT_ROOT.resolve()
        self.assertEqual(
            (root / "var" / "codex-home").resolve(),
            project_codex_home(root, environment={}),
        )

    def test_isolated_codex_home_copies_auth_but_not_global_config(self) -> None:
        with TemporaryDirectory(prefix="codex-source-home-") as directory:
            source = Path(directory)
            (source / "auth.json").write_text('{"token":"test-only"}', encoding="utf-8")
            (source / "config.toml").write_text(
                '[mcp_servers.must_not_load]\ncommand="never-run"\n',
                encoding="utf-8",
            )
            with patch.dict("os.environ", {}, clear=True):
                with isolated_codex_home(source) as isolated:
                    self.assertEqual(
                        '{"token":"test-only"}',
                        (isolated / "auth.json").read_text(encoding="utf-8"),
                    )
                    self.assertFalse((isolated / "config.toml").exists())
                self.assertFalse(isolated.exists())

    def test_mcp_call_summary_excludes_arguments_and_results(self) -> None:
        item = SimpleNamespace(
            type="mcpToolCall",
            server="doris_query_agent",
            tool="query_doris_sql",
            status=SimpleNamespace(value="completed"),
            read_only_hint=True,
            duration_ms=12,
            arguments={"sql": "sensitive SQL"},
            result=live_mcp_result(),
        )
        self.assertEqual(
            [
                {
                    "server": "doris_query_agent",
                    "tool": "query_doris_sql",
                    "status": "completed",
                    "read_only": True,
                    "duration_ms": 12,
                }
            ],
            mcp_call_summaries([item]),
        )

    def test_sdk_overrides_expose_only_query_tool(self) -> None:
        root = PROJECT_ROOT.resolve()
        executable = Path("C:/Python313/python.exe")
        overrides = codex_mcp_config_overrides(
            root,
            python_executable=executable,
            backend_url="http://127.0.0.1:8123",
        )
        combined = "\n".join(overrides)

        self.assertIn(json.dumps(str(executable), separators=(",", ":")), combined)
        self.assertIn('enabled_tools=["query_doris_sql"]', combined)
        self.assertIn('AGENT_QUERY_BACKEND_URL="http://127.0.0.1:8123"', combined)
        self.assertIn("features.shell_tool=false", combined)
        self.assertIn("features.plugins=false", combined)
        self.assertIn("features.memories=false", combined)
        self.assertIn("features.browser_use=false", combined)
        self.assertIn("features.workspace_dependencies=false", combined)
        self.assertIn("features.skill_search=true", combined)
        self.assertIn('web_search="disabled"', combined)
        self.assertIn("agents.enabled=false", combined)
        self.assertNotIn("mcp_servers.cloudflare", combined)

    def test_sdk_mcp_uses_windowless_interpreter_when_available(self) -> None:
        with TemporaryDirectory(prefix="windowless-mcp-") as directory:
            root = Path(directory)
            console_python = root / "python.exe"
            windowless_python = root / "pythonw.exe"
            console_python.write_bytes(b"")
            windowless_python.write_bytes(b"")

            overrides = codex_mcp_config_overrides(
                PROJECT_ROOT.resolve(),
                python_executable=console_python,
                backend_url="http://127.0.0.1:8123",
            )
            combined = "\n".join(overrides)
            expected = windowless_python if os.name == "nt" else console_python

            self.assertIn(json.dumps(str(expected), separators=(",", ":")), combined)
            if os.name == "nt":
                self.assertNotIn(json.dumps(str(console_python), separators=(",", ":")), combined)
        self.assertNotIn("DORIS_PASSWORD", combined)
        self.assertNotIn("AGENT_TRUSTED_RELEASE_SHA256", combined)

    def test_project_codex_config_is_single_tool_and_contains_no_credentials(self) -> None:
        document = tomllib.loads(
            (PROJECT_ROOT / ".codex" / "config.toml").read_text(encoding="utf-8")
        )
        config = document["mcp_servers"]["doris_query_agent"]
        self.assertNotIn("model_provider", document)
        self.assertNotIn("model_providers", document)
        self.assertEqual("disabled", document["web_search"])
        self.assertFalse(document["features"]["shell_tool"])
        self.assertFalse(document["features"]["plugins"])
        self.assertFalse(document["features"]["memories"])
        self.assertFalse(document["features"]["browser_use"])
        self.assertFalse(document["features"]["workspace_dependencies"])
        self.assertTrue(document["features"]["skill_search"])
        self.assertFalse(document["agents"]["enabled"])
        self.assertFalse(document["apps"]["_default"]["default_tools_enabled"])
        self.assertTrue(config["required"])
        self.assertEqual(["-I", "-s", "-m", "app.mcp_server"], config["args"])
        self.assertEqual(["query_doris_sql"], config["enabled_tools"])
        self.assertEqual(
            "http://127.0.0.1:8010",
            config["env"]["AGENT_QUERY_BACKEND_URL"],
        )
        for key in (
            "DORIS_HOST",
            "DORIS_PORT",
            "DORIS_USER",
            "DORIS_PASSWORD",
            "DORIS_DATABASE",
            "AGENT_TRUSTED_RELEASE_SHA256",
        ):
            self.assertNotIn(key, config["env"])

    @patch("app.codex_agent.isolated_codex_home")
    @patch("app.codex_agent.assert_codex_environment_isolated")
    @patch("openai_codex.CodexConfig")
    @patch("openai_codex.Codex")
    def test_sdk_thread_is_ephemeral_read_only_and_deny_all(
        self,
        codex_class: MagicMock,
        config_class: MagicMock,
        _isolation_check: MagicMock,
        isolated_home: MagicMock,
    ) -> None:
        isolated_home.return_value = nullcontext(Path("C:/isolated-codex-home"))
        codex = codex_class.return_value.__enter__.return_value
        thread = codex.thread_start.return_value
        thread.id = "thread-id"
        result = thread.run.return_value
        result.id = "turn-id"
        result.status = "completed"
        result.final_response = "queried"
        result.items = [
            SimpleNamespace(
                type="mcpToolCall",
                server="doris_query_agent",
                tool="query_doris_sql",
                status=SimpleNamespace(value="completed"),
                read_only_hint=True,
                duration_ms=1,
                arguments={"sql": "SELECT * FROM ywzt_ods.example"},
                result=live_mcp_result(),
            )
        ]
        result.usage = None

        response = run_codex_question(
            "查询数据",
            project_root=PROJECT_ROOT,
            backend_url="http://127.0.0.1:8010",
        )

        self.assertEqual("thread-id", response["thread_id"])
        self.assertEqual(1, len(response["mcp_calls"]))
        self.assertTrue(response["query_execution"]["live_query_executed"])
        config_kwargs = config_class.call_args.kwargs
        self.assertFalse(config_kwargs["experimental_api"])
        self.assertTrue(config_kwargs["config_overrides"])
        combined_overrides = "\n".join(config_kwargs["config_overrides"])
        self.assertIn('model_provider="agent_gateway"', combined_overrides)
        self.assertIn('model="gpt-6"', combined_overrides)
        self.assertIn('model_reasoning_effort="low"', combined_overrides)
        self.assertNotIn("OPENAI_API_KEY", combined_overrides)
        self.assertEqual(
            "C:\\isolated-codex-home",
            config_kwargs["env"]["CODEX_HOME"],
        )
        thread_kwargs = codex.thread_start.call_args.kwargs
        self.assertEqual(ApprovalMode.deny_all, thread_kwargs["approval_mode"])
        self.assertEqual(Sandbox.read_only, thread_kwargs["sandbox"])
        self.assertTrue(thread_kwargs["ephemeral"])
        self.assertEqual("gpt-6", thread_kwargs["model"])
        self.assertEqual("agent_gateway", thread_kwargs["model_provider"])
        self.assertIn(
            "BUSINESS TIMEZONE: Asia/Shanghai",
            thread_kwargs["developer_instructions"],
        )
        self.assertEqual(1, thread.run.call_count)
        first_prompt = thread.run.call_args.args[0]
        self.assertIn("查询数据", first_prompt)
        self.assertIn("HOST_QUERY_PLAN_FIRST", first_prompt)
        self.assertEqual(ReasoningEffort.low, thread.run.call_args.kwargs["effort"])
        self.assertEqual("gpt-6", thread.run.call_args.kwargs["model"])
        self.assertEqual(Sandbox.read_only, thread.run.call_args.kwargs["sandbox"])


if __name__ == "__main__":
    unittest.main()

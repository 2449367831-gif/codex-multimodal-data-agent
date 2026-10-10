from __future__ import annotations

from datetime import date, datetime
import unittest
from zoneinfo import ZoneInfo

from app.semantic_runtime import SemanticQueryRuntime, SemanticRuntimeError


BUSINESS_NOW = datetime(2026, 9, 2, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


class RecordingQueryClient:
    def __init__(self, rows_by_purpose: dict[str, list[dict[str, object]]]) -> None:
        self.rows_by_purpose = rows_by_purpose
        self.calls: list[dict[str, str]] = []

    def query(self, *, sql: str, purpose: str | None = None) -> dict[str, object]:
        purpose_value = purpose or ""
        self.calls.append({"sql": sql, "purpose": purpose_value})
        return {
            "status": "succeeded",
            "execution": {
                "columns": list(dict.fromkeys(key for row in self.rows_by_purpose[purpose_value] for key in row)),
                "rows": self.rows_by_purpose[purpose_value],
                "row_count": len(self.rows_by_purpose[purpose_value]),
                "truncated": False,
                "executor_name": "doris",
                "is_mock": False,
            },
        }


def runtime(client: RecordingQueryClient) -> SemanticQueryRuntime:
    return SemanticQueryRuntime(
        query_client=client,
        timezone_name="Asia/Shanghai",
        clock=lambda _tz=None: BUSINESS_NOW,
    )


class SemanticRuntimeClassificationTestCase(unittest.TestCase):
    def test_classifies_purchase_inbound_top_sku_without_sales_or_turnover_choices(self) -> None:
        service = runtime(RecordingQueryClient({}))

        request = service.classify("查询本月采购入库数量最高的前10个SKU")

        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual("inbound_summary", request.route)
        self.assertEqual(10, request.top_n)
        self.assertFalse(request.show_warehouses)
        self.assertTrue(request.show_top_sku)
        self.assertEqual("2026-09-01", request.start.isoformat())
        self.assertEqual("2026-09-02", request.end.isoformat())

    def test_classifies_the_three_requested_questions_with_business_boundaries(self) -> None:
        client = RecordingQueryClient({})
        service = runtime(client)

        outbound = service.classify("昨天各仓库实际销售出库")
        self.assertIsNotNone(outbound)
        assert outbound is not None
        self.assertEqual("sales_outbound_by_warehouse", outbound.route)
        self.assertEqual("2026-09-01", outbound.start.isoformat())
        self.assertEqual("2026-09-02", outbound.end.isoformat())

        turnover = service.classify("上月库存周转")
        self.assertIsNotNone(turnover)
        assert turnover is not None
        self.assertEqual("realtime_turnover", turnover.route)
        self.assertEqual("2026-08-01", turnover.start.isoformat())
        self.assertEqual("2026-09-01", turnover.end.isoformat())

        batch = service.classify("8月3号生产日期的烤肠在长沙黄兴的库存量")
        self.assertIsNotNone(batch)
        assert batch is not None
        self.assertEqual("batch_inventory", batch.route)
        self.assertEqual("2026-08-03", batch.production_date.isoformat())

    def test_iso_dates_work_when_they_touch_chinese_text(self) -> None:
        service = runtime(RecordingQueryClient({}))

        outbound = service.classify("2026-09-01各仓实际销售出库")
        self.assertIsNotNone(outbound)
        assert outbound is not None
        self.assertEqual("sales_outbound_by_warehouse", outbound.route)
        self.assertEqual("2026-09-01", outbound.start.isoformat())

        batch = service.classify("查2026-08-03生产日期长沙黄兴脆皮肠库存")
        self.assertIsNotNone(batch)
        assert batch is not None
        self.assertEqual("batch_inventory", batch.route)
        self.assertEqual("2026-08-03", batch.production_date.isoformat())

    def test_named_completed_month_uses_current_inventory_by_default(self) -> None:
        service = runtime(RecordingQueryClient({}))

        for question in ("8月份库存周转", "2026-08库存周转"):
            request = service.classify(question)
            self.assertIsNotNone(request, question)
            assert request is not None
            self.assertEqual("realtime_turnover", request.route)
            self.assertEqual("2026-08-01", request.start.isoformat())
            self.assertEqual("2026-09-01", request.end.isoformat())

        self.assertIsNone(service.classify("9月份库存周转"))
        self.assertIsNone(service.classify("2027年1月库存周转"))

    def test_semantic_boundaries_do_not_execute_the_actual_sales_contract(self) -> None:
        service = runtime(RecordingQueryClient({}))

        blocked_questions = (
            "昨天各仓未完成销售出库量",
            "昨天各仓订单计划销售出库量",
            "用当前库存除以本月销售订单请求量算周转",
            "昨天发货量是多少",
            "昨天各仓发货量",
        )
        for question in blocked_questions:
            self.assertIsNone(service.classify(question), question)

        ranked = service.classify("2026年9月1日发货数量top20的SKU")
        self.assertIsNotNone(ranked)
        assert ranked is not None
        self.assertEqual("sales_outbound_top_sku", ranked.route)
        self.assertEqual(20, ranked.top_n)

    def test_dated_sausage_inventory_requires_a_production_date_basis(self) -> None:
        service = runtime(RecordingQueryClient({}))

        self.assertIsNone(service.classify("查2026-08-03长沙黄兴脆皮肠库存"))
        request = service.classify("查2026-08-03生产的长沙黄兴脆皮肠库存")
        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual("batch_inventory", request.route)

    def test_standalone_top_inbound_question_has_an_explicit_default_day(self) -> None:
        request = runtime(RecordingQueryClient({})).classify("最多入库的是什么sku")
        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual("inbound_summary", request.route)
        self.assertTrue(request.show_top_sku)
        self.assertFalse(request.show_warehouses)
        self.assertEqual("2026-09-01", request.start.isoformat())

    def test_classifies_sales_outbound_top_sku_with_a_bounded_rank(self) -> None:
        request = runtime(RecordingQueryClient({})).classify(
            "昨天销售出库top20的sku是哪些"
        )
        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual("sales_outbound_top_sku", request.route)
        self.assertEqual(20, request.top_n)
        self.assertEqual("2026-09-01", request.start.isoformat())

        yesterday_alias = runtime(RecordingQueryClient({})).classify(
            "昨日实际销售出库排名前5的商品"
        )
        self.assertIsNotNone(yesterday_alias)
        assert yesterday_alias is not None
        self.assertEqual("sales_outbound_top_sku", yesterday_alias.route)
        self.assertEqual(5, yesterday_alias.top_n)

        table_request = runtime(RecordingQueryClient({})).classify(
            "用表格的形式告诉我昨天出库数量top20的sku"
        )
        self.assertIsNotNone(table_request)
        assert table_request is not None
        self.assertEqual("sales_outbound_top_sku", table_request.route)
        self.assertEqual(20, table_request.top_n)

        sales_alias = runtime(RecordingQueryClient({})).classify(
            "昨日销量排名前10的SKU"
        )
        self.assertIsNotNone(sales_alias)
        assert sales_alias is not None
        self.assertEqual("sales_outbound_top_sku", sales_alias.route)
        self.assertEqual(10, sales_alias.top_n)

        shipment_alias = runtime(RecordingQueryClient({})).classify(
            "请用表格列出2026年9月2日销售发货量排名前5的商品"
        )
        self.assertIsNotNone(shipment_alias)
        assert shipment_alias is not None
        self.assertEqual("sales_outbound_top_sku", shipment_alias.route)
        self.assertEqual("2026-09-02", shipment_alias.start.isoformat())
        self.assertEqual(5, shipment_alias.top_n)

        natural_rank = runtime(RecordingQueryClient({})).classify(
            "昨天实际销售出库数量最高的20个SKU，按实发数量降序列表格"
        )
        self.assertIsNotNone(natural_rank)
        assert natural_rank is not None
        self.assertEqual("sales_outbound_top_sku", natural_rank.route)
        self.assertEqual(20, natural_rank.top_n)

        sold_rank = runtime(RecordingQueryClient({})).classify(
            "昨天卖出数量最高的20个SKU"
        )
        self.assertIsNotNone(sold_rank)
        assert sold_rank is not None
        self.assertEqual("sales_outbound_top_sku", sold_rank.route)
        self.assertEqual(20, sold_rank.top_n)

        planned = runtime(RecordingQueryClient({})).classify(
            "昨天订单计划销售出库top20的sku"
        )
        self.assertIsNone(planned)

    def test_warehouse_actual_outbound_uses_all_sales_types_and_ship_quantity(self) -> None:
        service = runtime(RecordingQueryClient({}))
        sql = service._sales_sql(date(2026, 9, 1), date(2026, 9, 2))

        self.assertIn("TRIM(h.ref_order_type) IN ('JYCK', 'HHCK', 'BFCK', 'QTCK')", sql)
        self.assertIn("TRIM(h.ref_order_type) LIKE 'SO%'", sql)
        self.assertIn("SUM(COALESCE(d.ship_qty, 0)) AS outbound_ea", sql)
        self.assertNotIn("allocate_qty", sql)
        self.assertNotIn("reconciliation_difference", sql)
        self.assertNotIn("SUM(COALESCE(a.allocate_qty, 0)) AS outbound_ea", sql)

    def test_classifies_explicit_current_sausage_inventory(self) -> None:
        request = runtime(RecordingQueryClient({})).classify(
            "查询长沙黄兴仓库当前烤肠库存"
        )
        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual("current_inventory", request.route)
        self.assertEqual("sausage", request.product_keyword)
        self.assertEqual("10114790", request.warehouse_code)
        self.assertEqual("长沙黄兴", request.warehouse_label)

        finished_goods = runtime(RecordingQueryClient({})).classify("当前实时成品库存")
        self.assertIsNotNone(finished_goods)
        assert finished_goods is not None
        self.assertEqual("current_inventory", finished_goods.route)

    def test_classifies_dms_points_air_fryer_month_range_without_using_sales_route(self) -> None:
        request = runtime(RecordingQueryClient({})).classify(
            "看下dms的1-8月积分兑换的空气炸锅有多少"
        )
        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual("dms_points_redemption", request.route)
        self.assertEqual("2026-01-01", request.start.isoformat())
        self.assertEqual("2026-09-01", request.end.isoformat())
        self.assertEqual("8030101", request.product_code)
        self.assertEqual("皇家小虎空气炸锅5L", request.product_name)

    def test_classifies_dms_points_air_fryer_for_a_single_day(self) -> None:
        request = runtime(RecordingQueryClient({})).classify(
            "昨天DMS积分兑换空气炸锅数量"
        )
        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual("dms_points_redemption", request.route)
        self.assertEqual("2026-09-01", request.start.isoformat())
        self.assertEqual("2026-09-02", request.end.isoformat())

    def test_classifies_transfer_outbound_separately_from_sales(self) -> None:
        request = runtime(RecordingQueryClient({})).classify("昨天调拨出库量")
        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual("transfer_outbound", request.route)
        self.assertEqual("2026-09-01", request.start.isoformat())
        self.assertEqual("2026-09-02", request.end.isoformat())

    def test_transfer_outbound_uses_dbck_and_ship_quantity(self) -> None:
        sql = runtime(RecordingQueryClient({}))._sales_sql(
            date(2026, 9, 1), date(2026, 9, 2), transfer=True
        )
        self.assertIn("TRIM(h.ref_order_type) = 'DBCK'", sql)
        self.assertIn("SUM(COALESCE(d.ship_qty, 0)) AS outbound_ea", sql)
        self.assertNotIn("allocate_qty", sql)
        self.assertNotIn("scm_sales_order_header", sql)

    def test_natural_channel_grouping_maps_to_customer_channel_dimension(self) -> None:
        request = runtime(RecordingQueryClient({})).classify(
            "昨天线上线下各渠道实际出库数量和出库单数，用表格展示。"
        )
        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual("sales_aggregate", request.route)
        self.assertEqual(("customer_channel",), request.sales_aggregation.dimensions)

    def test_all_outbound_type_query_keeps_transfer_documents_and_sales_documents(self) -> None:
        request = runtime(RecordingQueryClient({})).classify(
            "昨天所有出库类型实际出库数量和单数，按出库类型汇总。"
        )
        self.assertIsNotNone(request)
        assert request is not None
        self.assertTrue(request.all_outbound_types)
        self.assertEqual(("ref_order_type",), request.sales_aggregation.dimensions)

    def test_complex_channel_fulfillment_query_is_planned_deterministically(self) -> None:
        request = runtime(RecordingQueryClient({})).classify(
            "查询 2026-09-01 全天 HJXH 生产产品销售出库，按渠道细分 + 2C/2B汇总，输出出库单笔数、实发数量、SKU数。"
        )
        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual("sales_price_group", request.route)
        self.assertEqual(("customer_channel", "fulfillment_type"), request.sales_aggregation.dimensions)
        self.assertEqual("2c9c0da1948322ec01948bac2924000a", request.price_group_parent)
        self.assertEqual("HJXH", request.scope.owner)
        self.assertTrue(request.finished_goods_only)

    def test_classifies_dms_exchange_wording_without_requiring_points_word(self) -> None:
        request = runtime(RecordingQueryClient({})).classify(
            "看下dms的1-8月份兑换的空气炸锅有多少"
        )
        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual("dms_points_redemption", request.route)
        self.assertEqual("2026-01-01", request.start.isoformat())
        self.assertEqual("2026-09-01", request.end.isoformat())

    def test_dms_route_requires_an_explicit_dms_marker(self) -> None:
        request = runtime(RecordingQueryClient({})).classify(
            "1-8月积分兑换的空气炸锅有多少"
        )
        self.assertIsNone(request)

    def test_common_paraphrases_and_negative_controls_stay_on_their_contracts(self) -> None:
        service = runtime(RecordingQueryClient({}))
        positive_cases = {
            "统计昨日各仓已完成销售发货数量": "sales_outbound_by_warehouse",
            "昨天各仓库实际销售出库量，不含退货，按仓库汇总": "sales_outbound_by_warehouse",
            "本月截至昨天各仓库实际销售出库量": "sales_outbound_by_warehouse",
            "昨天线下渠道各仓实际销售出库数量和出库单数": "sales_aggregate",
            "昨天各仓销售出库数量，排除逻辑删除单据": "sales_outbound_by_warehouse",
            "昨天各仓销售出库实际发了多少，按仓库汇总，不要分配数量": "sales_outbound_by_warehouse",
            "昨天各仓实际发出量，按仓库统计": "sales_outbound_by_warehouse",
            "前天分仓实际出库量": "sales_outbound_by_warehouse",
            "2026/09/01各仓销售出库": "sales_outbound_by_warehouse",
            "昨天销量top20 SKU": "sales_outbound_top_sku",
            "用表格告诉我昨日出库数量前5个商品": "sales_outbound_top_sku",
            "8月成品库存周转天数": "realtime_turnover",
            "2026年8月份库存周转": "realtime_turnover",
            "本月实时成品库存周转": "realtime_turnover",
            "最近7个完整自然日当前库存周转": "realtime_turnover",
            "长沙黄兴8月3日生产的烤肠库存": "batch_inventory",
            "长沙黄兴8.3生产日期库存": "batch_inventory",
            "查询8月3日生产的成品在长沙黄兴仓当前库存，没数据就明确说明，不要猜零": "batch_inventory",
            "查询黄兴仓2026年8月3日生产批次脆皮肠库存": "batch_inventory",
            "现在各仓成品库存": "current_inventory",
            "长沙黄兴实时香肠库存": "current_inventory",
            "昨天各仓入库数量": "inbound_summary",
            "昨日收货top10 SKU": "inbound_summary",
            "DMS 1月至8月兑换空气锅数量": "dms_points_redemption",
            "看下dms的1~8月份积分订单空气炸锅数量": "dms_points_redemption",
        }
        for question, expected_route in positive_cases.items():
            with self.subTest(question=question):
                request = service.classify(question)
                self.assertIsNotNone(request)
                assert request is not None
                self.assertEqual(expected_route, request.route)

        negative_cases = (
            "昨天发货量是多少",
            "昨天各仓出库量",
            "昨天各仓销售订单请求发货量",
            "昨天各仓订单计划销售出库量",
            "昨天各仓未完成销售出库量",
            "昨天各仓退货出库量",
            "用当前库存除以本月销售订单请求量算周转",
            "当前库存周转",
            "9月库存周转",
            "各仓当前库存周转",
            "空气炸锅库存周转",
            "查2026-08-03长沙黄兴烤肠库存",
            "现在库存有多少",
            "查询2026年8月20日可用库存",
            "DMS空气炸锅积分兑换多少",
            "1-8月积分兑换的空气炸锅有多少",
        )
        for question in negative_cases:
            with self.subTest(question=question):
                self.assertIsNone(service.classify(question))


class SemanticRuntimeExecutionTestCase(unittest.TestCase):
    def test_selected_window_and_grain_execute_without_model_reinterpretation(self) -> None:
        client = RecordingQueryClient(
            {
                "actual sales outbound by warehouse": [
                    {
                        "warehouse_name": "仓甲",
                        "outbound_ea": "12",
                        "allocation_ea": "12",
                        "reconciliation_difference": "0",
                        "total_ea": "20",
                        "warehouse_count": 2,
                    },
                    {
                        "warehouse_name": "仓乙",
                        "outbound_ea": "8",
                        "allocation_ea": "8",
                        "reconciliation_difference": "0",
                        "total_ea": "20",
                        "warehouse_count": 2,
                    },
                ]
            }
        )
        result = runtime(client).run_selected_if_supported(
            "帮我看看最近业务情况",
            [
                {"question_id": "metric", "option_id": "sales_outbound_actual"},
                {"question_id": "time_window", "option_id": "last_7_complete_days"},
                {"question_id": "output_grain", "option_id": "total"},
            ],
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result["query_execution"]["live_business_result_executed"])
        self.assertEqual(
            [{"warehouse": "全部仓库", "quantity": "20"}],
            result["business_table"]["rows"],
        )
        self.assertIn("2026年8月26日至2026年9月1日", result["interaction"]["answer"])
        self.assertIn("2026-08-26 00:00:00", client.calls[0]["sql"])
        self.assertIn("2026-09-02 00:00:00", client.calls[0]["sql"])

    def test_selected_current_turnover_uses_current_inventory_numerator(self) -> None:
        client = RecordingQueryClient(
            {
                "current real-time finished-goods inventory turnover": [
                    {
                        "inventory_ea": "100",
                        "inventory_key_count": 1,
                        "inventory_warehouse_count": 1,
                        "negative_inventory_key_count": 0,
                        "inventory_latest_freshness": "2026-09-02 10:00:00",
                        "null_sales_quantity_count": "0",
                        "negative_sales_quantity_count": "0",
                        "sales_ea": "14",
                        "allocation_ea": "14",
                        "reconciliation_difference": "0",
                        "complete_natural_days": 7,
                        "turnover_days": "50",
                    }
                ]
            }
        )
        result = runtime(client).run_selected_if_supported(
            "查询库存周转",
            [
                {
                    "question_id": "turnover_inventory_point",
                    "option_id": "current_realtime_finished_goods",
                },
                {"question_id": "sales_window", "option_id": "last_7_complete_days"},
                {"question_id": "output_grain", "option_id": "total"},
            ],
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual("realtime_inventory_turnover", result["business_table"]["table_id"])
        self.assertIn("当前实时可用成品库存（含在途）周转：50天", result["interaction"]["answer"])
        self.assertIn("available_quantity", client.calls[0]["sql"])
        self.assertIn("in_transit_quantity", client.calls[0]["sql"])

    def test_selected_dms_metric_executes_without_model_reinterpretation(self) -> None:
        client = RecordingQueryClient(
            {
                "DMS points redemption air fryer by payment month": [
                    {
                        "grain": "summary",
                        "month_label": "",
                        "sku_code": "",
                        "sku_name": "",
                        "redemption_quantity": "10",
                        "order_count": 4,
                        "product_count": 1,
                        "month_count": 1,
                        "negative_line_count": 0,
                        "logical_line_count": 4,
                        "name_variant_count": 1,
                        "freshness_ok": 1,
                        "header_max_updated_time": "2026-09-02 10:00:00",
                        "detail_max_updated_time": "2026-09-02 10:00:00",
                    },
                    {
                        "grain": "month",
                        "month_label": "2026-08",
                        "sku_code": "8030101",
                        "sku_name": "皇家小虎空气炸锅5L",
                        "redemption_quantity": "10",
                        "order_count": 4,
                        "product_count": 1,
                        "month_count": 1,
                        "negative_line_count": 0,
                        "logical_line_count": 4,
                        "name_variant_count": 1,
                        "freshness_ok": 1,
                        "header_max_updated_time": "2026-09-02 10:00:00",
                        "detail_max_updated_time": "2026-09-02 10:00:00",
                    },
                ]
            }
        )
        result = runtime(client).run_selected_if_supported(
            "看下dms的1-8月份兑换空气炸锅数量",
            [{"question_id": "metric", "option_id": "dms_points_redemption_air_fryer"}],
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual("dms_points_redemption", result["business_table"]["table_id"])
        self.assertEqual("10", result["business_table"]["summary"][0]["value"])
        self.assertEqual(1, len(client.calls))

    def test_sales_route_uses_current_sales_contract_and_reconciles_all_warehouses(self) -> None:
        client = RecordingQueryClient(
            {
                "actual sales outbound by warehouse": [
                    {
                        "warehouse_name": "仓甲",
                        "outbound_ea": "12",
                        "allocation_ea": "12",
                        "reconciliation_difference": "0",
                        "total_ea": "20",
                        "warehouse_count": 2,
                    },
                    {
                        "warehouse_name": "仓乙",
                        "outbound_ea": "8",
                        "allocation_ea": "8",
                        "reconciliation_difference": "0",
                        "total_ea": "20",
                        "warehouse_count": 2,
                    },
                ]
            }
        )
        result = runtime(client).run_if_supported("昨天各仓库实际销售出库")
        self.assertIsNotNone(result)
        assert result is not None
        answer = result["interaction"]["answer"]
        self.assertIn("20 EA", answer)
        self.assertIn("仓甲 12 EA", answer)
        table = result["business_table"]
        self.assertEqual("sales_outbound_by_warehouse", table["table_id"])
        self.assertEqual(["仓甲", "仓乙"], [row["warehouse"] for row in table["rows"]])
        self.assertEqual("20", table["summary"][0]["value"])
        sql = client.calls[0]["sql"]
        self.assertNotIn("DBCK", sql)
        self.assertNotIn("JOIN sku_scope", sql)
        self.assertIn("2026-09-01 00:00:00", sql)
        self.assertIn("2026-09-02 00:00:00", sql)

    def test_sales_top_sku_route_returns_stable_ranked_rows_and_total_control(self) -> None:
        client = RecordingQueryClient(
            {
                "actual sales outbound top sku": [
                    {
                        "sku_code": "SKU-A",
                        "sku_name": "商品甲",
                        "outbound_ea": "120",
                        "outbound_order_count": 5,
                        "total_ea": "200",
                        "all_sku_count": 2,
                        "blank_sku_count": 0,
                        "sku_name_conflict_count": 0,
                        "negative_detail_count": 0,
                    },
                    {
                        "sku_code": "SKU-B",
                        "sku_name": "商品乙",
                        "outbound_ea": "80",
                        "outbound_order_count": 3,
                        "total_ea": "200",
                        "all_sku_count": 2,
                        "blank_sku_count": 0,
                        "sku_name_conflict_count": 0,
                        "negative_detail_count": 0,
                    },
                ]
            }
        )
        result = runtime(client).run_if_supported("昨天销售出库top20的sku是哪些")
        self.assertIsNotNone(result)
        assert result is not None
        answer = result["interaction"]["answer"]
        self.assertIn("Top 20 SKU", answer)
        self.assertIn("商品甲（SKU-A） 120 EA / 5 单", answer)
        self.assertIn("商品乙（SKU-B） 80 EA / 3 单", answer)
        self.assertIn("有效销售出库合计：200 EA", answer)
        table = result["business_table"]
        self.assertEqual("sales_outbound_top_sku", table["table_id"])
        self.assertEqual("default", result["presentation"])
        self.assertEqual("1", table["rows"][0]["rank"])
        self.assertEqual("SKU-A", table["rows"][0]["sku_code"])
        self.assertEqual("120", table["rows"][0]["quantity"])
        sql = client.calls[0]["sql"]
        self.assertIn("LIMIT 20", sql)
        self.assertIn("h.status IN (700, 900, 701, 901)", sql.replace("`", ""))
        self.assertIn("ORDER BY t.outbound_ea DESC, t.sku_code ASC", sql)
        self.assertNotIn("updated_date", sql)
        self.assertIn("so.deleted_flag", sql)

    def test_table_wording_is_preserved_and_returns_the_requested_top_sku_table(self) -> None:
        client = RecordingQueryClient(
            {
                "actual sales outbound top sku": [
                    {
                        "sku_code": "SKU-A",
                        "sku_name": "商品甲",
                        "outbound_ea": "120",
                        "outbound_order_count": 5,
                        "total_ea": "120",
                        "all_sku_count": 1,
                        "blank_sku_count": 0,
                        "sku_name_conflict_count": 0,
                        "negative_detail_count": 0,
                    }
                ]
            }
        )
        result = runtime(client).run_if_supported(
            "用表格的形式告诉我昨天出库数量top20的sku"
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual("table", result["presentation"])
        self.assertEqual("sales_outbound_top_sku", result["business_table"]["table_id"])
        self.assertEqual("SKU-A", result["business_table"]["rows"][0]["sku_code"])

    def test_turnover_route_checks_formula_and_fixed_snapshot_period(self) -> None:
        client = RecordingQueryClient(
            {
                "current real-time finished-goods inventory turnover": [
                    {
                        "inventory_ea": "100",
                        "inventory_key_count": 1,
                        "inventory_warehouse_count": 1,
                        "negative_inventory_key_count": 0,
                        "inventory_latest_freshness": "2026-09-02 10:00:00",
                        "null_sales_quantity_count": "0",
                        "negative_sales_quantity_count": "0",
                        "sales_ea": "31",
                        "allocation_ea": "31",
                        "reconciliation_difference": "0",
                        "complete_natural_days": 31,
                        "turnover_days": "100",
                    }
                ]
            }
        )
        result = runtime(client).run_if_supported("上月库存周转")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("2026年8月当前实时可用成品库存（含在途）周转：100天", result["interaction"]["answer"])
        table = result["business_table"]
        self.assertEqual("realtime_inventory_turnover", table["table_id"])
        self.assertEqual("100", table["rows"][-1]["value"])
        self.assertEqual("天", table["rows"][-1]["unit"])
        sql = client.calls[0]["sql"]
        self.assertIn("sku_inventory", sql)
        self.assertIn("2026-08-01 00:00:00", sql)
        self.assertNotIn("DBCK", sql)

    def test_batch_route_reports_missing_current_and_separate_historical_reference(self) -> None:
        client = RecordingQueryClient(
            {
                "production-date sausage inventory at Changsha Huangxing": [
                    {
                        "current_row_count": 0,
                        "current_sku_count": 0,
                        "current_stock_value_count": 0,
                        "current_actual_value_count": 0,
                        "current_stock_ea": 0,
                        "current_actual_ea": 0,
                        "current_freshness": None,
                        "latest_backup_date": "2026-08-25",
                        "backup_row_count": 1,
                        "backup_sku_count": 1,
                        "backup_stock_value_count": 1,
                        "backup_actual_value_count": 1,
                        "backup_stock_ea": 0,
                        "backup_actual_ea": 0,
                    }
                ]
            }
        )
        result = runtime(client).run_if_supported("8月3号生产日期的烤肠在长沙黄兴的库存量")
        self.assertIsNotNone(result)
        assert result is not None
        answer = result["interaction"]["answer"]
        self.assertIn("当前没有匹配库存记录", answer)
        self.assertNotIn("当前库存：0 EA", answer)
        self.assertEqual("no_data", result["result_state"])
        self.assertIn("2026-08-25", answer)
        table = result["business_table"]
        self.assertEqual("batch_inventory", table["table_id"])
        self.assertIsNone(table["rows"][0]["current_stock"])
        self.assertEqual("2026-08-25", table["rows"][0]["snapshot_date"])

    def test_current_inventory_route_aggregates_explicit_sausage_scope(self) -> None:
        client = RecordingQueryClient(
            {
                "inventory warehouse mapping verification": [
                    {"cargo_owner": "HJXH", "wms_code": "10114790",
                     "warehouse_name": "皇家小虎长沙黄兴万纬仓xs", "name_code_count": 1},
                ],
                "current finished-goods inventory": [
                    {
                        "warehouse_name": "长沙黄兴",
                        "actual_ea": "42",
                        "sku_count": 2,
                        "negative_key_count": 0,
                        "latest_freshness": "2026-09-03 10:00:00",
                        "total_ea": "42",
                        "warehouse_count": 1,
                        "total_sku_count": 2,
                        "total_negative_key_count": 0,
                        "negative_warehouse_count": 0,
                        "warehouse_name_conflict_count": 0,
                        "total_latest_freshness": "2026-09-03 10:00:00",
                    }
                ]
            }
        )
        result = runtime(client).run_if_supported("查询长沙黄兴仓库当前烤肠库存")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("当前实时库存：42 EA", result["interaction"]["answer"])
        self.assertEqual("current_inventory", result["business_table"]["table_id"])
        self.assertEqual("42", result["business_table"]["rows"][0]["quantity"])
        sql = client.calls[-1]["sql"]
        self.assertIn("皇家小虎长沙黄兴万纬仓xs", sql)
        self.assertNotIn("wms_desc = '10114790'", sql)
        self.assertIn("sku_inventory", sql)
        self.assertIn("available_quantity", sql)
        self.assertIn("in_transit_quantity", sql)
        self.assertIn("sku_name LIKE '%烤肠%'", sql)

    def test_inbound_route_returns_quantity_tons_and_top_sku(self) -> None:
        client = RecordingQueryClient(
            {
                "actual inbound quantity and tons by warehouse": [
                    {
                        "grain": "summary",
                        "warehouse_name": "",
                        "sku_name": "",
                        "inbound_ea": "30",
                        "inbound_tons": "3.5",
                        "unweighted_ea": "0",
                        "missing_master_ea": "0",
                        "warehouse_count": 1,
                    },
                    {
                        "grain": "warehouse",
                        "warehouse_name": "仓甲",
                        "sku_name": "",
                        "inbound_ea": "30",
                        "inbound_tons": "3.5",
                        "unweighted_ea": "0",
                        "missing_master_ea": "0",
                        "warehouse_count": 1,
                    },
                    {
                        "grain": "sku",
                        "warehouse_name": "",
                        "sku_name": "商品甲",
                        "inbound_ea": "30",
                        "inbound_tons": "3.5",
                        "unweighted_ea": "0",
                        "missing_master_ea": "0",
                        "warehouse_count": 1,
                    },
                ]
            }
        )
        result = runtime(client).run_if_supported("昨天各仓入库数量与吨数，最多入库的是什么sku")
        self.assertIsNotNone(result)
        assert result is not None
        answer = result["interaction"]["answer"]
        self.assertIn("30 EA", answer)
        self.assertIn("3.5 吨", answer)
        self.assertIn("商品甲", answer)
        table = result["business_table"]
        self.assertEqual("inbound_by_warehouse", table["table_id"])
        self.assertEqual("仓甲", table["rows"][0]["warehouse"])
        self.assertEqual("30", table["rows"][0]["inbound_quantity"])

    def test_inbound_route_keeps_reconciled_ea_when_dimension_names_are_missing(self) -> None:
        client = RecordingQueryClient(
            {
                "actual inbound quantity and tons by warehouse": [
                    {
                        "grain": "summary",
                        "wms_code": "",
                        "sku_code": "",
                        "warehouse_name": "",
                        "sku_name": "",
                        "inbound_ea": "30",
                        "inbound_tons": "2.5",
                        "unweighted_ea": "5",
                        "missing_master_ea": "5",
                        "warehouse_count": 2,
                    },
                    {
                        "grain": "warehouse",
                        "wms_code": "W1",
                        "sku_code": "",
                        "warehouse_name": "仓甲",
                        "sku_name": "",
                        "inbound_ea": "20",
                        "inbound_tons": "2",
                        "unweighted_ea": "0",
                        "missing_master_ea": "0",
                        "warehouse_count": 2,
                    },
                    {
                        "grain": "warehouse",
                        "wms_code": "W2",
                        "sku_code": "",
                        "warehouse_name": "",
                        "sku_name": "",
                        "inbound_ea": "10",
                        "inbound_tons": "0.5",
                        "unweighted_ea": "5",
                        "missing_master_ea": "5",
                        "warehouse_count": 2,
                    },
                    {
                        "grain": "sku",
                        "wms_code": "",
                        "sku_code": "S1",
                        "warehouse_name": "",
                        "sku_name": "商品甲",
                        "inbound_ea": "20",
                        "inbound_tons": "2",
                        "unweighted_ea": "0",
                        "missing_master_ea": "0",
                        "warehouse_count": 2,
                    },
                    {
                        "grain": "sku",
                        "wms_code": "",
                        "sku_code": "S2",
                        "warehouse_name": "",
                        "sku_name": "",
                        "inbound_ea": "10",
                        "inbound_tons": "0.5",
                        "unweighted_ea": "5",
                        "missing_master_ea": "5",
                        "warehouse_count": 2,
                    },
                ]
            }
        )
        result = runtime(client).run_if_supported("昨天各仓入库数量与吨数，最多入库的是什么sku")
        self.assertIsNotNone(result)
        assert result is not None
        answer = result["interaction"]["answer"]
        self.assertIn("30 EA", answer)
        self.assertIn("2.5 吨", answer)
        self.assertIn("未匹配仓库 (W2)", answer)
        self.assertIn("部分 SKU 未匹配主数据", answer)
        table = result["business_table"]
        self.assertEqual("未匹配仓库 (W2)", table["rows"][1]["warehouse"])

    def test_dms_route_reconciles_months_and_returns_a_public_table(self) -> None:
        client = RecordingQueryClient(
            {
                "DMS points redemption air fryer by payment month": [
                    {
                        "grain": "summary",
                        "month_label": "",
                        "sku_code": "",
                        "sku_name": "",
                        "redemption_quantity": "1119",
                        "order_count": 617,
                        "product_count": 1,
                        "month_count": 3,
                        "negative_line_count": 0,
                        "logical_line_count": 617,
                        "name_variant_count": 1,
                        "freshness_ok": 1,
                        "header_max_updated_time": "2026-09-03T15:00:00",
                        "detail_max_updated_time": "2026-09-03T15:00:00",
                    },
                    {
                        "grain": "month",
                        "month_label": "2026-06",
                        "sku_code": "8030101",
                        "sku_name": "皇家小虎空气炸锅5L",
                        "redemption_quantity": "49",
                        "order_count": 38,
                        "product_count": 1,
                        "month_count": 3,
                        "negative_line_count": 0,
                        "logical_line_count": 617,
                        "name_variant_count": 1,
                        "freshness_ok": 1,
                        "header_max_updated_time": "2026-09-03T15:00:00",
                        "detail_max_updated_time": "2026-09-03T15:00:00",
                    },
                    {
                        "grain": "month",
                        "month_label": "2026-07",
                        "sku_code": "8030101",
                        "sku_name": "皇家小虎空气炸锅5L",
                        "redemption_quantity": "309",
                        "order_count": 197,
                        "product_count": 1,
                        "month_count": 3,
                        "negative_line_count": 0,
                        "logical_line_count": 617,
                        "name_variant_count": 1,
                        "freshness_ok": 1,
                        "header_max_updated_time": "2026-09-03T15:00:00",
                        "detail_max_updated_time": "2026-09-03T15:00:00",
                    },
                    {
                        "grain": "month",
                        "month_label": "2026-08",
                        "sku_code": "8030101",
                        "sku_name": "皇家小虎空气炸锅5L",
                        "redemption_quantity": "761",
                        "order_count": 382,
                        "product_count": 1,
                        "month_count": 3,
                        "negative_line_count": 0,
                        "logical_line_count": 617,
                        "name_variant_count": 1,
                        "freshness_ok": 1,
                        "header_max_updated_time": "2026-09-03T15:00:00",
                        "detail_max_updated_time": "2026-09-03T15:00:00",
                    },
                ]
            }
        )
        result = runtime(client).run_if_supported(
            "看下dms的1-8月份兑换的空气炸锅有多少"
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("2026年1月至8月", result["interaction"]["answer"])
        self.assertIn("1119 台", result["interaction"]["answer"])
        self.assertEqual("dms_points_redemption", result["business_table"]["table_id"])
        self.assertEqual(["49", "309", "761"], [
            row["redemption_quantity"] for row in result["business_table"]["rows"]
        ])
        self.assertEqual("1119", result["business_table"]["summary"][0]["value"])
        self.assertEqual(1, len(client.calls))
        self.assertIn("dms_ods.t_sales_order", client.calls[0]["sql"])
        self.assertIn("item_type", client.calls[0]["sql"])

    def test_dms_route_blocks_stale_source_controls(self) -> None:
        client = RecordingQueryClient(
            {
                "DMS points redemption air fryer by payment month": [
                    {
                        "grain": "summary",
                        "month_label": "",
                        "sku_code": "",
                        "sku_name": "",
                        "redemption_quantity": "0",
                        "order_count": 0,
                        "product_count": 0,
                        "month_count": 0,
                        "negative_line_count": 0,
                        "logical_line_count": 0,
                        "name_variant_count": 0,
                        "freshness_ok": 0,
                        "header_max_updated_time": "2026-08-31T23:59:59",
                        "detail_max_updated_time": "2026-08-31T23:59:59",
                    }
                ]
            }
        )
        with self.assertRaises(SemanticRuntimeError):
            runtime(client).run_if_supported("看下dms的1-8月积分兑换的空气炸锅有多少")


class SemanticRuntimeFailureTestCase(unittest.TestCase):
    def test_corrupt_actual_total_never_returns_a_business_answer(self) -> None:
        client = RecordingQueryClient(
            {
                "actual sales outbound by warehouse": [
                    {
                        "warehouse_name": "仓甲",
                        "outbound_ea": "12",
                        "allocation_ea": "11",
                        "reconciliation_difference": "2",
                        "total_ea": "13",
                        "warehouse_count": 1,
                    }
                ]
            }
        )
        with self.assertRaises(SemanticRuntimeError):
            runtime(client).run_if_supported("昨天各仓库实际销售出库")

    def test_sales_top_sku_rejects_unsorted_or_negative_rows(self) -> None:
        client = RecordingQueryClient(
            {
                "actual sales outbound top sku": [
                    {
                        "sku_code": "SKU-A",
                        "sku_name": "商品甲",
                        "outbound_ea": "80",
                        "outbound_order_count": 2,
                        "total_ea": "200",
                        "all_sku_count": 2,
                        "blank_sku_count": 0,
                        "sku_name_conflict_count": 0,
                        "negative_detail_count": 0,
                    },
                    {
                        "sku_code": "SKU-B",
                        "sku_name": "商品乙",
                        "outbound_ea": "120",
                        "outbound_order_count": 3,
                        "total_ea": "200",
                        "all_sku_count": 2,
                        "blank_sku_count": 0,
                        "sku_name_conflict_count": 0,
                        "negative_detail_count": 0,
                    },
                ]
            }
        )
        with self.assertRaises(SemanticRuntimeError):
            runtime(client).run_if_supported("昨天销售出库top20的sku是哪些")

    def test_sales_top_sku_rejects_conflicting_master_names(self) -> None:
        client = RecordingQueryClient(
            {
                "actual sales outbound top sku": [
                    {
                        "sku_code": "SKU-A",
                        "sku_name": "商品甲",
                        "outbound_ea": "120",
                        "outbound_order_count": 5,
                        "total_ea": "120",
                        "all_sku_count": 1,
                        "blank_sku_count": 0,
                        "sku_name_conflict_count": 1,
                        "negative_detail_count": 0,
                    }
                ]
            }
        )

        with self.assertRaises(SemanticRuntimeError):
            runtime(client).run_if_supported("昨天销售出库top20的sku是哪些")


if __name__ == "__main__":
    unittest.main()

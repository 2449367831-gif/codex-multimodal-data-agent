from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.agent_core.runtime import SemanticQueryRuntime
from app.agent_core.semantic_types import SemanticRuntimeError
from app.agent_core.receipt_summary import receipt_production_summary_clarification
from app.chat_api import _public_business_table


OCCUPATION_QUESTION = (
    "查询仓库 GZHYC-777，货主 HJXH，SKU 5001060111000362 的库存占用拆分。"
    "条件：统计最近 60 天；status <> 1；pre_order_code LIKE 'HJXH-TH%' "
    "认定为调拨占用；其他占用认定为销售占用。"
    "输出：调拨占用、销售占用、占用合计、关联单号数"
)
RECEIPT_QUESTION = (
    "查询 2026-09-14 全天德州和江西成品仓的采购入库产成品，按生产日期汇总。"
    "仓库：德州：8987502；江西：WMS000098。"
    "条件：采购单号不为空；SKU 为产成品；排除取消入库单；排除逻辑删除；"
    "输出入库单数、明细行数、实收入库数量"
)
OCCUPATION_VARIANT_QUESTION = (
    "查仓库 GZHYC-777、货主 HJXH、SKU 5001060111000362 最近60天的库存占用分拆，"
    "按调拨占用和销售占用分类，status不等于1，输出占用合计和关联单号数"
)
RECEIPT_VARIANT_QUESTION = (
    "统计2026年9月14日德州:8987502、江西:WMS000098成品仓采购入库产成品，"
    "按生产日期统计；采购单号非空，排除取消入库单和逻辑删除"
)
RECEIPT_WAREHOUSE_PRODUCTION_QUESTION = (
    "查询最近3天德州和江西成品仓的采购入库，按仓库和生产日期汇总"
    "入库单数、SKU数、计划入库数量及实收入库数量，并提示生产日期为空的明细数量。"
)
STATUS_ZERO_QUESTION = (
    "查询2026-09-17全天所有货主的实际销售出库，按出库状态统计实发数量和出库单数；"
    "同时列出各状态实发总量为0的出库单数。有效状态700、900、701、901，"
    "零数量单据不能丢弃。用表格展示。"
)


class Client:
    def __init__(self, rows):
        self.rows = rows
        self.sql = []

    def query(self, *, sql, purpose):
        self.sql.append(sql)
        return {
            "status": "succeeded",
            "execution": {
                "executor_name": "doris",
                "is_mock": False,
                "rows": self.rows,
                "row_count": len(self.rows),
                "truncated": False,
            },
        }


def runtime(rows):
    return SemanticQueryRuntime(
        query_client=Client(rows),
        clock=lambda zone: datetime(2026, 9, 18, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )


def test_occupation_question_is_request_local_and_reconciled():
    service = runtime([{
        "transfer_occupation": "3",
        "sales_occupation": "7",
        "occupation_total": "10",
        "associated_order_count": "2",
        "source_row_count": "3",
        "missing_order_count": "1",
        "negative_quantity_count": "0",
    }])
    plan = service.classify(OCCUPATION_QUESTION)
    assert plan is not None and plan.route == "inventory_occupation_split"
    assert plan.scope.owner == "HJXH"
    assert plan.scope.warehouse_codes == ("GZHYC-777",)
    assert plan.scope.sku_codes == ("5001060111000362",)
    result = service.execute_plan(plan, question=OCCUPATION_QUESTION)
    assert result["result_state"] == "live_success"
    assert result["query_plan"]["grain"] == "occupation_split"
    assert result["business_table"]["rows"][0]["occupation_total"] == "10"
    assert _public_business_table(result["business_table"], live=True) is not None
    assert "GZHYC-777" in service.query_client.sql[0]
    assert "HJXH" in service.query_client.sql[0]
    assert "5001060111000362" in service.query_client.sql[0]


def test_occupation_quality_failure_never_publishes_number():
    service = runtime([{
        "transfer_occupation": "3",
        "sales_occupation": "8",
        "occupation_total": "10",
        "associated_order_count": "2",
        "source_row_count": "3",
        "missing_order_count": "0",
        "negative_quantity_count": "0",
    }])
    with pytest.raises(SemanticRuntimeError, match="完整性校验"):
        service.execute_plan(service.classify(OCCUPATION_QUESTION), question=OCCUPATION_QUESTION)


def test_receipt_question_uses_arrival_day_and_production_grouping():
    service = runtime([{
        "production_day": "2026-09-10",
        "source_unit": "EA",
        "receipt_count": "2",
        "detail_count": "3",
        "received_quantity": "12",
        "detail_total": "3",
        "detail_distinct": "3",
        "max_header_variants": "1",
    }])
    plan = service.classify(RECEIPT_QUESTION)
    assert plan is not None and plan.route == "purchase_received_by_production_date"
    assert plan.start.isoformat() == "2026-09-14"
    assert plan.end.isoformat() == "2026-09-15"
    assert plan.receipt_summary_plan.purchase_basis == "purchase_order_reference"
    assert plan.scope.warehouse_codes == ("8987502", "WMS000098")
    result = service.execute_plan(plan, question=RECEIPT_QUESTION)
    assert result["result_state"] == "live_success"
    assert result["query_plan"]["grain"] == "production_date+source_unit"
    assert result["business_table"]["rows"][0]["production_day"] == "2026-09-10"
    assert _public_business_table(result["business_table"], live=True) is not None
    sql = service.query_client.sql[0]
    assert "2026-09-14 00:00:00" in sql and "2026-09-15 00:00:00" in sql
    assert "po_order_code" in sql and "warehous_status" in sql


def test_receipt_units_are_not_cross_unit_summed():
    service = runtime([
        {
            "production_day": "2026-09-10", "source_unit": "EA",
            "receipt_count": "1", "detail_count": "1", "received_quantity": "12",
            "detail_total": "2", "detail_distinct": "2", "max_header_variants": "1",
        },
        {
            "production_day": "2026-09-10", "source_unit": "BOX",
            "receipt_count": "1", "detail_count": "1", "received_quantity": "3",
            "detail_total": "2", "detail_distinct": "2", "max_header_variants": "1",
        },
    ])
    result = service.execute_plan(service.classify(RECEIPT_QUESTION), question=RECEIPT_QUESTION)
    assert {row["source_unit"] for row in result["business_table"]["rows"]} == {"EA", "BOX"}
    assert result["business_table"]["summary"][2]["unit"] == "按来源单位"


def test_natural_language_variants_keep_the_same_explicit_routes():
    occupation_service = runtime([{
        "transfer_occupation": "1", "sales_occupation": "2", "occupation_total": "3",
        "associated_order_count": "1", "source_row_count": "2",
        "missing_order_count": "0", "negative_quantity_count": "0",
    }])
    occupation_plan = occupation_service.classify(OCCUPATION_VARIANT_QUESTION)
    assert occupation_plan is not None
    assert occupation_plan.route == "inventory_occupation_split"

    receipt_service = runtime([{
        "production_day": "2026-09-12", "source_unit": "EA",
        "receipt_count": "1", "detail_count": "1", "received_quantity": "5",
        "detail_total": "1", "detail_distinct": "1", "max_header_variants": "1",
    }])
    receipt_plan = receipt_service.classify(RECEIPT_VARIANT_QUESTION)
    assert receipt_plan is not None
    assert receipt_plan.route == "purchase_received_by_production_date"


def test_status_zero_reporting_stays_on_sales_aggregate_route():
    service = runtime([{
        "shipment_status": "700", "quantity": "0", "sku_count": "1",
        "outbound_count": "1", "total_quantity": "0", "total_outbound_count": "1",
        "total_sku_count": "1", "latest_shipment_at": "2026-09-17 12:00:00",
        "group_count": "1", "header_conflict_count": "0", "null_quantity_count": "0",
        "negative_quantity_count": "0", "blank_key_count": "0",
    }])
    plan = service.classify(STATUS_ZERO_QUESTION)
    assert plan is not None
    assert plan.route == "sales_aggregate"
    assert plan.sales_aggregation.dimensions == ("shipment_status",)


def test_receipt_basis_is_one_choice_and_selection_executes_the_same_route():
    question = (
        "查询2026年9月14日德州:8987502成品仓采购入库产成品，按生产日期统计"
    )
    card = receipt_production_summary_clarification(question)
    assert card is not None
    assert [option["id"] for option in card["questions"][0]["options"]] == [
        "purchase_order_reference", "purchase_type", "both",
    ]
    service = runtime([{
        "production_day": "2026-09-12", "source_unit": "EA",
        "receipt_count": "1", "detail_count": "1", "received_quantity": "5",
        "detail_total": "1", "detail_distinct": "1", "max_header_variants": "1",
    }])
    plan = service.plan_selected(
        question,
        [{"question_id": "purchase_basis", "option_ids": ["purchase_type"]}],
    )
    assert plan is not None
    assert plan.route == "purchase_received_by_production_date"
    assert plan.receipt_summary_plan.purchase_basis == "purchase_type"


def test_multi_day_warehouse_receipt_report_is_fully_planned():
    service = runtime([
        {
            "cargo_owner": "HJXH", "wms_code": "DT", "warehouse_name": "德州成品仓",
            "production_day": "2026-09-16", "source_unit": "EA",
            "receipt_count": "1", "sku_count": "2", "planned_quantity": "100",
            "received_quantity": "90", "empty_production_detail_count": "0", "detail_count": "2",
            "detail_total": "3", "detail_distinct": "3", "control_empty_production_detail_count": "1",
            "negative_quantity_count": "0", "max_receipt_variants": "1", "max_warehouse_name_variants": "1",
        },
        {
            "cargo_owner": "HJXH", "wms_code": "JX", "warehouse_name": "江西成品仓",
            "production_day": "生产日期为空", "source_unit": "EA",
            "receipt_count": "1", "sku_count": "1", "planned_quantity": "20",
            "received_quantity": "20", "empty_production_detail_count": "1", "detail_count": "1",
            "detail_total": "3", "detail_distinct": "3", "control_empty_production_detail_count": "1",
            "negative_quantity_count": "0", "max_receipt_variants": "1", "max_warehouse_name_variants": "1",
        },
    ])
    plan = service.classify(RECEIPT_WAREHOUSE_PRODUCTION_QUESTION)
    assert plan is not None
    assert plan.route == "purchase_received_by_warehouse_production_date"
    assert plan.start.isoformat() == "2026-09-15"
    assert plan.end.isoformat() == "2026-09-18"
    assert plan.scope.warehouse_labels == ("德州", "江西")
    assert plan.receipt_warehouse_summary_plan.purchase_basis == "purchase_type"
    result = service.execute_plan(plan, question=RECEIPT_WAREHOUSE_PRODUCTION_QUESTION)
    assert result["result_state"] == "live_success"
    table = result["business_table"]
    assert table["complete_reconciled"] is True
    assert {row["warehouse_name"] for row in table["rows"]} == {"德州成品仓", "江西成品仓"}
    assert table["summary"][-1]["value"] == "1"
    sql = service.query_client.sql[0]
    assert "master_wms" in sql
    assert "inbound_type = '10'" in sql
    assert "total_quantity" in sql and "received_quantity" in sql

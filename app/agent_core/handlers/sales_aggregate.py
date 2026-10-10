"""Reconcile each sales measure at its own grain before public presentation."""

from decimal import Decimal

from sqlglot import exp

from ..sales_aggregation import compile_sales_aggregation
from ..semantic_types import SemanticAnswer, SemanticRuntimeError, _display_table, _parse_decimal, _parse_source_datetime
from .base import MetricHandler


def _integer(value):
    number = _parse_decimal(value)
    if number < 0 or number != number.to_integral_value():
        raise ValueError("invalid count")
    return int(number)


class SalesAggregateHandler(MetricHandler):
    # The BFF splits screened rows into 100-row result pages. Keep the
    # complete-query ceiling so a valid warehouse+SKU report does not become
    # a false failure merely because it has more than 500 groups.
    row_limit = 100000

    def _read_rows(self, request):
        return self._execute(
            compile_sales_aggregation(
                request.sales_aggregation,
                start=request.start,
                end=request.end,
                # Scope application adds reviewed exception SKUs. Keeping the
                # compiler's plain group-50 CTE out in that case prevents the
                # CTE from filtering those exceptions before scope expansion.
                finished_goods_only=(
                    request.finished_goods_only
                    and not request.scope.finished_goods_exceptions
                ),
                all_outbound_types=request.all_outbound_types,
            ),
            purpose="actual sales quantity and distinct outbound documents",
        )

    def run(self, request):
        plan = request.sales_aggregation
        rows = self._read_rows(request)
        if len(rows) > self.row_limit:
            raise SemanticRuntimeError("结果超过展示上限", reason="result_truncated")
        displayed, seen = [], set()
        latest = None
        total, document_total, quantity_sum, document_sum = Decimal(0), 0, Decimal(0), 0
        sku_total = None
        missing_customer_quantity = None
        try:
            for row in rows:
                current_total = _parse_decimal(row.get("total_quantity"))
                current_documents = _integer(row.get("total_outbound_count"))
                current_skus = _integer(row.get("total_sku_count")) if plan.include_sku_count else None
                if not displayed:
                    total, document_total = current_total, current_documents
                    latest = _parse_source_datetime(row.get("latest_shipment_at"), timezone_name=self.timezone_name)
                    sku_total = current_skus
                row_latest = _parse_source_datetime(row.get("latest_shipment_at"), timezone_name=self.timezone_name)
                if row_latest != latest or not request.start <= row_latest.date() < request.end:
                    raise ValueError("invalid source event time")
                if total != current_total or document_total != current_documents or _integer(row.get("group_count")) != len(rows):
                    raise ValueError("inconsistent controls")
                if plan.include_sku_count and sku_total != current_skus:
                    raise ValueError("inconsistent sku controls")
                if any(_integer(row.get(key)) for key in ("header_conflict_count", "null_quantity_count", "negative_quantity_count", "blank_key_count")):
                    raise ValueError("source quality control failed")
                if "customer_channel" in plan.dimensions:
                    if _integer(row.get("customer_conflict_count")):
                        raise ValueError("conflicting customer types")
                    missing = _parse_decimal(row.get("missing_customer_quantity"))
                    if missing < 0 or missing > current_total or (missing_customer_quantity is not None and missing != missing_customer_quantity):
                        raise ValueError("invalid customer coverage")
                    missing_customer_quantity = missing
                    if row.get("customer_channel") not in {"线上", "线下", "其他"}:
                        raise ValueError("invalid customer channel")
                quantity, documents = _parse_decimal(row.get("quantity")), _integer(row.get("outbound_count"))
                if quantity < 0 or documents < 1 or documents > document_total:
                    raise ValueError("invalid measure")
                zero_documents = None
                if plan.include_zero_document_count:
                    zero_documents = _integer(row.get("zero_outbound_count"))
                    if zero_documents < 0 or zero_documents > documents:
                        raise ValueError("invalid zero-document count")
                key = tuple(str(row.get(k) or "").strip() for k in plan.keys)
                if key in seen or not all(key):
                    raise ValueError("missing or duplicate group key")
                seen.add(key)
                item = {k: v for k, v in zip(plan.keys, key)}
                if "warehouse" in plan.dimensions:
                    if _integer(row.get("warehouse_name_variants")) > 1:
                        raise ValueError("conflicting warehouse names")
                    item["warehouse_name"] = str(row.get("warehouse_name") or item["warehouse_code"]).strip()
                item.update(quantity=format(quantity, "f"), outbound_count=str(documents))
                if zero_documents is not None:
                    item["zero_outbound_count"] = str(zero_documents)
                if plan.include_sku_count:
                    sku_count = _integer(row.get("sku_count"))
                    if sku_count < 1 or current_skus is None or sku_count > current_skus:
                        raise ValueError("invalid sku count")
                    item["sku_count"] = str(sku_count)
                displayed.append(item)
                quantity_sum += quantity
                document_sum += documents
            if quantity_sum != total or ("sku" not in plan.dimensions and document_sum != document_total):
                raise ValueError("aggregate reconciliation failed")
        except (ValueError, TypeError) as exc:
            raise SemanticRuntimeError("出库数量或单数未通过对账，暂未展示结果", reason="sales_aggregate_reconciliation_failed") from exc
        if request.scope.finished_goods_exceptions:
            self._verify_finished_membership(displayed, request.scope.finished_goods_exceptions)
        window = f"{request.start.isoformat()} 00:00 至 {request.end.isoformat()} 00:00（不含结束时点）"
        text = f"查询成功：实际销售出库 {format(total, 'f')} EA，出库单数 {document_total} 单。" if rows else "查询成功，当前范围没有有效销售出库记录。"
        text += f"北京时间 {window}。完成状态为700、900、701、901；零数量有效单仍计入出库单数。"
        if rows:
            text += f"最近出库时间：{latest.strftime('%Y-%m-%d %H:%M:%S')}；该时间不代表数据同步时间。"
        columns = [{"key": key, "kind": "text"} for key in plan.keys]
        if "warehouse" in plan.dimensions:
            columns.append({"key": "warehouse_name", "kind": "text"})
        columns += [{"key": "quantity", "kind": "number", "unit": "EA"}, {"key": "outbound_count", "kind": "number", "unit": "单"}]
        if plan.include_zero_document_count:
            columns.append({"key": "zero_outbound_count", "kind": "number", "unit": "单"})
        if plan.include_sku_count:
            columns.append({"key": "sku_count", "kind": "number", "unit": "个"})
        table = _display_table(
            table_id="sales_aggregate", title="销售出库数量与单数", caption=f"{window} · 状态700/900/701/901",
            columns=columns, rows=displayed,
            summary=[{"label": "实际销售出库", "value": format(total, "f"), "unit": "EA"},
                     {"label": "出库单数", "value": str(document_total), "unit": "单"}],
            empty_label="查询成功，当前范围没有有效销售出库记录",
        )
        table["notes"] = ["零数量的有效出库单仍计入单数；出库单数不等于销售订单数。"]
        if plan.include_sku_count:
            table["summary"].append({"label": "SKU数", "value": str(sku_total or 0), "unit": "个"})
            table["notes"].append("SKU数按各行粒度去重；汇总SKU数独立去重，不累加分组SKU数。")
        if plan.order_types:
            table["notes"].append("本次出库类型范围：" + "、".join(plan.order_types) + "。")
        if latest is not None:
            table["notes"].append(f"最近出库时间：{latest.strftime('%Y-%m-%d %H:%M:%S')}。这是业务事件时间，不代表数据同步时间。")
        if request.scope.finished_goods_exceptions:
            table["notes"].append("成品范围为成品组50，另保留例外SKU " + "、".join(request.scope.finished_goods_exceptions) + "；普通辅料不计入。")
        if missing_customer_quantity is not None:
            table["missing_customer_quantity"] = format(missing_customer_quantity, "f")
        if len(table.get("notes", [])) > 3:
            notes = table["notes"]
            table["notes"] = [*notes[:2], "".join(notes[2:])]
        return SemanticAnswer(text, table)

    def _verify_finished_membership(self, rows, exceptions):
        codes = sorted({row["sku_code"] for row in rows} - set(exceptions))
        for offset in range(0, len(codes), 200):
            batch = codes[offset:offset + 200]
            literals = ", ".join(exp.Literal.string(code).sql(dialect="doris") for code in batch)
            # Check unfiltered master groups; applying the product filter here
            # would hide conflicts between finished and non-finished records.
            controls = self.execution._query_checked(f"""SELECT TRIM(sku_code) AS sku_code,
  COUNT(DISTINCT COALESCE(NULLIF(TRIM(sku_group), ''), '<missing>')) AS group_variants,
  MIN(TRIM(sku_group)) AS sku_group
FROM ywzt_ods.master_sku
WHERE TRIM(sku_code) IN ({literals})
GROUP BY TRIM(sku_code) LIMIT 201""", purpose="finished-goods membership verification", business=False)
            if len(controls) != len(batch) or {str(row.get("sku_code")) for row in controls} != set(batch) or any(
                str(row.get("group_variants")) != "1" or str(row.get("sku_group")) != "50" for row in controls
            ):
                raise SemanticRuntimeError("成品主数据分类存在冲突或变化，暂未展示结果", reason="finished_goods_classification_conflict")

from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from inspect_doris_scope import (
    ENV_FILE,
    connect,
    load_candidate_products,
    load_candidate_warehouses,
    load_env_file,
    text,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
JSON_OUTPUT = DATA_DIR / "dashboard-data.json"
JS_OUTPUT = DATA_DIR / "dashboard-data.js"

CATEGORY_NAMES = {
    "5001": "烤肠类",
    "5002": "调理肉禽类",
    "5003": "油炸小食类",
    "5004": "午餐肉培根类",
    "5005": "水产预制菜",
    "5006": "米面主食类",
    "5007": "烘焙披萨类",
    "5008": "酱料调味类",
}

PROVINCE_NAMES = {
    "110000": "北京", "120000": "天津", "130000": "河北", "140000": "山西", "150000": "内蒙古",
    "210000": "辽宁", "220000": "吉林", "230000": "黑龙江",
    "310000": "上海", "320000": "江苏", "330000": "浙江", "340000": "安徽", "350000": "福建",
    "360000": "江西", "370000": "山东", "410000": "河南", "420000": "湖北", "430000": "湖南",
    "440000": "广东", "450000": "广西", "460000": "海南", "500000": "重庆", "510000": "四川",
    "520000": "贵州", "530000": "云南", "540000": "西藏", "610000": "陕西", "620000": "甘肃",
    "630000": "青海", "640000": "宁夏", "650000": "新疆",
}

REGION_BY_PROVINCE = {
    "110000": "华北", "120000": "华北", "130000": "华北", "140000": "华北", "150000": "华北",
    "210000": "东北", "220000": "东北", "230000": "东北",
    "310000": "华东", "320000": "华东", "330000": "华东", "340000": "华东", "350000": "华东",
    "360000": "华东", "370000": "华东",
    "410000": "华中", "420000": "华中", "430000": "华中",
    "440000": "华南", "450000": "华南", "460000": "华南",
    "500000": "西南", "510000": "西南", "520000": "西南", "530000": "西南", "540000": "西南",
    "610000": "西北", "620000": "西北", "630000": "西北", "640000": "西北", "650000": "西北",
}

REGION_ORDER = ["华东", "华南", "华中", "华北", "西南", "西北", "东北"]
MONTH_WEIGHTS = [0.05, 0.10, 0.15, 0.20, 0.20, 0.30]
LOW_COVER_DAYS = 14.0
WATCH_COVER_DAYS = 30.0
HIGH_COVER_DAYS = 60.0


def number(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def integer(value: Any) -> int:
    return int(round(number(value)))


def month_shift(month_start: date, delta: int) -> date:
    month_index = month_start.year * 12 + month_start.month - 1 + delta
    return date(month_index // 12, month_index % 12 + 1, 1)


def sql_placeholders(values: Iterable[Any]) -> str:
    values = list(values)
    if not values:
        raise ValueError("SQL scope cannot be empty")
    return ",".join(["%s"] * len(values))


def fetch_all(cursor: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    cursor.execute(sql, params)
    return list(cursor.fetchall())


def category_code_for_sku(sku_code: str, master_row: dict[str, Any] | None) -> str:
    master_code = text((master_row or {}).get("finished_product_category"))
    if master_code:
        return master_code
    # The operational SKU code carries the finished-product class in positions 3-6.
    inferred = sku_code[2:6] if len(sku_code) >= 6 else ""
    return inferred if inferred in CATEGORY_NAMES else "other"


def risk_status(stock_qty: float, sales_30d_qty: float) -> tuple[str, float | None]:
    if sales_30d_qty > 0:
        cover_days = stock_qty / (sales_30d_qty / 30.0)
        if cover_days < LOW_COVER_DAYS:
            return "replenish", cover_days
        if cover_days < WATCH_COVER_DAYS:
            return "watch", cover_days
        if cover_days > HIGH_COVER_DAYS:
            return "pause", cover_days
        return "healthy", cover_days
    if stock_qty > 0:
        return "pause", None
    return "healthy", None


def cube_cell() -> dict[str, Any]:
    return {
        "stock_qty": 0.0,
        "all_stock_qty": 0.0,
        "stock_value": 0.0,
        "priced_stock_qty": 0.0,
        "sales_30d_qty": 0.0,
        "sales_30d_amount": 0.0,
        "request_30d_qty": 0.0,
        "sales_detail_rows": 0,
        "sales_amount_nonzero_rows": 0,
        "in_transit_qty": 0.0,
        "monthly_qty": defaultdict(float),
        "monthly_amount": defaultdict(float),
        "warehouses": set(),
    }


def forecast_from_history(values: list[float]) -> tuple[float, float]:
    if len(values) != 6:
        raise ValueError("Forecast requires exactly six monthly values")
    weighted = sum(value * weight for value, weight in zip(values, MONTH_WEIGHTS))
    recent = values[-3:]
    if recent[0] > 0 and recent[-1] > 0:
        trend = math.sqrt(recent[-1] / recent[0]) - 1.0
    elif recent[-1] > recent[0]:
        trend = 0.20
    elif recent[-1] < recent[0]:
        trend = -0.20
    else:
        trend = 0.0
    capped_trend = max(-0.20, min(0.20, trend))
    return max(weighted * (1.0 + capped_trend), 0.0), capped_trend


def iso_or_empty(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    if not value:
        return None
    raw = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[:19], fmt)
        except ValueError:
            continue
    return None


def main() -> None:
    load_env_file(ENV_FILE)
    products, workbook_audit = load_candidate_products()
    candidate_warehouses = load_candidate_warehouses()
    product_by_code = {row["sku_code"]: row for row in products}
    sku_codes = list(product_by_code)
    warehouse_names = [row["wms_desc"] for row in candidate_warehouses]
    sku_ph = sql_placeholders(sku_codes)
    warehouse_name_ph = sql_placeholders(warehouse_names)

    today = date.today()
    tomorrow = today + timedelta(days=1)
    current_month = today.replace(day=1)
    history_months = [month_shift(current_month, delta) for delta in range(-6, 0)]
    history_start = history_months[0]
    last_30_start = today - timedelta(days=29)
    purchase_history_start = today - timedelta(days=179)
    price_history_start = today - timedelta(days=364)

    audit: dict[str, Any] = {
        "workbook": workbook_audit,
        "query_window": {
            "history_months": [month.strftime("%Y-%m") for month in history_months],
            "last_30_days": [last_30_start.isoformat(), today.isoformat()],
            "purchase_history_start": purchase_history_start.isoformat(),
        },
    }

    with connect() as conn, conn.cursor() as cursor:
        master_rows = fetch_all(
            cursor,
            f"""
            SELECT sku_code, sku_name, finished_product_category, net_weight,
                   sku_group, use_status, status, controlsale
            FROM master_sku
            WHERE sku_code IN ({sku_ph})
            """,
            sku_codes,
        )
        master_by_sku = {text(row.get("sku_code")): row for row in master_rows}

        warehouse_rows = fetch_all(
            cursor,
            f"""
            SELECT wms_code, wms_desc, wms_type, province, city, wms_status, status
            FROM master_wms
            WHERE wms_desc IN ({warehouse_name_ph})
            """,
            warehouse_names,
        )
        warehouse_by_name = {text(row.get("wms_desc")): row for row in warehouse_rows}
        warehouse_codes = [text(row.get("wms_code")) for row in warehouse_rows if text(row.get("wms_code"))]
        warehouse_code_ph = sql_placeholders(warehouse_codes)

        inventory_rows = fetch_all(
            cursor,
            f"""
            WITH inventory AS (
                SELECT wms_code, sku_code,
                       SUM(IFNULL(in_stock_quantity, 0)) AS all_stock_qty,
                       SUM(IFNULL(CAST(NULLIF(stock_lock, '') AS DECIMAL(20, 3)), 0)) AS stock_lock_qty,
                       SUM(IFNULL(freeze_quantity, 0)) AS freeze_qty,
                       MAX(created_date) AS inventory_updated_at
                FROM scm_warehous_manage
                WHERE cargo_owner_id = 'HJXH'
                  AND (inventory_status = 'ZP' OR inventory_status IS NULL OR inventory_status = '')
                  AND wms_code IN ({warehouse_code_ph})
                  AND sku_code IN ({sku_ph})
                GROUP BY wms_code, sku_code
            ),
            allocate AS (
                SELECT wms_code, sku_code,
                       SUM(IFNULL(freeze_quantity, 0)) AS allocate_qty
                FROM scm_warehous_occupy
                WHERE cargo_owner_id = 'HJXH'
                  AND created_date > DATE_SUB(NOW(), INTERVAL 60 DAY)
                  AND status != 1
                  AND wms_code IN ({warehouse_code_ph})
                  AND sku_code IN ({sku_ph})
                GROUP BY wms_code, sku_code
            )
            SELECT inventory.wms_code, inventory.sku_code,
                   inventory.all_stock_qty, inventory.stock_lock_qty,
                   inventory.freeze_qty, IFNULL(allocate.allocate_qty, 0) AS allocate_qty,
                   GREATEST(
                       inventory.all_stock_qty - IFNULL(allocate.allocate_qty, 0)
                       - CASE WHEN inventory.stock_lock_qty = 0 THEN inventory.freeze_qty ELSE inventory.stock_lock_qty END,
                       0
                   ) AS available_qty,
                   inventory.inventory_updated_at
            FROM inventory
            LEFT JOIN allocate
              ON allocate.wms_code = inventory.wms_code
             AND allocate.sku_code = inventory.sku_code
            """,
            warehouse_codes + sku_codes + warehouse_codes + sku_codes,
        )

        sales_monthly_rows = fetch_all(
            cursor,
            f"""
            SELECT DATE_FORMAT(h.ship_at, '%%Y-%%m') AS sale_month,
                   h.facility_code AS wms_code,
                   d.sku_code,
                   SUM(IFNULL(d.ship_qty, 0)) AS ship_qty,
                   SUM(IFNULL(d.request_qty, 0)) AS request_qty,
                   SUM(IFNULL(d.total_pay_amount, 0)) AS sales_amount,
                   COUNT(*) AS detail_rows,
                   SUM(CASE WHEN IFNULL(d.total_pay_amount, 0) <> 0 THEN 1 ELSE 0 END) AS amount_nonzero_rows
            FROM scm_sales_shipment_header h
            INNER JOIN scm_sales_shipment_detail d ON d.outbound_code = h.outbound_code
            WHERE h.outbound_code LIKE 'HJXH%%'
              AND h.ship_at >= %s
              AND h.ship_at < %s
              AND h.status IN ('700','900','701','901')
              AND (h.ref_order_type IN ('JYCK','HHCK','BFCK','QTCK') OR h.ref_order_type LIKE 'SO%%')
              AND h.facility_code IN ({warehouse_code_ph})
              AND d.sku_code IN ({sku_ph})
            GROUP BY DATE_FORMAT(h.ship_at, '%%Y-%%m'), h.facility_code, d.sku_code
            """,
            [history_start, tomorrow] + warehouse_codes + sku_codes,
        )

        sales_30d_rows = fetch_all(
            cursor,
            f"""
            SELECT h.facility_code AS wms_code, d.sku_code,
                   SUM(IFNULL(d.ship_qty, 0)) AS ship_qty,
                   SUM(IFNULL(d.request_qty, 0)) AS request_qty,
                   SUM(IFNULL(d.total_pay_amount, 0)) AS sales_amount,
                   COUNT(*) AS detail_rows,
                   SUM(CASE WHEN IFNULL(d.total_pay_amount, 0) <> 0 THEN 1 ELSE 0 END) AS amount_nonzero_rows
            FROM scm_sales_shipment_header h
            INNER JOIN scm_sales_shipment_detail d ON d.outbound_code = h.outbound_code
            WHERE h.outbound_code LIKE 'HJXH%%'
              AND h.ship_at >= %s
              AND h.ship_at < %s
              AND h.status IN ('700','900','701','901')
              AND (h.ref_order_type IN ('JYCK','HHCK','BFCK','QTCK') OR h.ref_order_type LIKE 'SO%%')
              AND h.facility_code IN ({warehouse_code_ph})
              AND d.sku_code IN ({sku_ph})
            GROUP BY h.facility_code, d.sku_code
            """,
            [last_30_start, tomorrow] + warehouse_codes + sku_codes,
        )

        price_rows = fetch_all(
            cursor,
            f"""
            SELECT d.sku_code, d.standard_purchase_price,
                   d.created_date, d.input_inventory_date, d.id
            FROM scm_po_header h
            INNER JOIN scm_po_detail d ON d.po_code = h.po_code
            WHERE h.cargo_owner_id = 'HJXH'
              AND h.po_type = 'CCP'
              AND h.status = 0
              AND d.status = 0
              AND d.standard_purchase_price > 0
              AND d.created_date >= %s
              AND d.sku_code IN ({sku_ph})
            ORDER BY d.sku_code, d.created_date DESC, d.id DESC
            """,
            [price_history_start] + sku_codes,
        )

        purchase_rows = fetch_all(
            cursor,
            f"""
            SELECT h.po_code, h.take_delivery_wms AS wms_code,
                   h.vendor_code, h.vendor_name, h.order_status, h.complete_mark,
                   d.sku_code, d.request_quantity, d.received_quantity, d.due_in_quantity,
                   COALESCE(d.delivery_time, h.delivery_time) AS planned_delivery_time,
                   d.input_inventory_date AS actual_inventory_time,
                   d.standard_purchase_price, d.created_date
            FROM scm_po_header h
            INNER JOIN scm_po_detail d ON d.po_code = h.po_code
            WHERE h.cargo_owner_id = 'HJXH'
              AND h.po_type = 'CCP'
              AND h.status = 0
              AND d.status = 0
              AND d.created_date >= %s
              AND h.take_delivery_wms IN ({warehouse_code_ph})
              AND d.sku_code IN ({sku_ph})
            """,
            [purchase_history_start] + warehouse_codes + sku_codes,
        )

    candidate_warehouse_by_name = {row["wms_desc"]: row for row in candidate_warehouses}
    warehouse_dimensions: list[dict[str, Any]] = []
    warehouse_by_code: dict[str, dict[str, Any]] = {}
    for candidate_name in warehouse_names:
        db_row = warehouse_by_name.get(candidate_name)
        if not db_row:
            continue
        province_code = text(db_row.get("province"))
        region = REGION_BY_PROVINCE.get(province_code, "其他")
        source_row = candidate_warehouse_by_name[candidate_name]
        dimension = {
            "code": text(db_row.get("wms_code")),
            "name": candidate_name,
            "short_name": source_row.get("short_name") or candidate_name,
            "province_code": province_code,
            "province": PROVINCE_NAMES.get(province_code, province_code or "未配置"),
            "region": region,
            "service_provinces": source_row.get("service_provinces", ""),
        }
        warehouse_dimensions.append(dimension)
        warehouse_by_code[dimension["code"]] = dimension

    product_dimensions: list[dict[str, Any]] = []
    product_dimension_by_code: dict[str, dict[str, Any]] = {}
    for sku_code, source_row in product_by_code.items():
        master_row = master_by_sku.get(sku_code)
        category_code = category_code_for_sku(sku_code, master_row)
        dimension = {
            "sku_code": sku_code,
            "sku_name": text((master_row or {}).get("sku_name")) or source_row["sku_name"],
            "category_code": category_code,
            "category_name": CATEGORY_NAMES.get(category_code, "其他在售成品"),
            "strategy": source_row["strategy"],
            "product_type": source_row["product_type"],
            "master_sku_matched": bool(master_row),
        }
        product_dimensions.append(dimension)
        product_dimension_by_code[sku_code] = dimension

    latest_price: dict[str, float] = {}
    price_date: dict[str, str] = {}
    for row in price_rows:
        sku_code = text(row.get("sku_code"))
        if sku_code and sku_code not in latest_price:
            latest_price[sku_code] = number(row.get("standard_purchase_price"))
            price_date[sku_code] = iso_or_empty(row.get("created_date"))

    cube: dict[tuple[str, str], dict[str, Any]] = defaultdict(cube_cell)
    warehouse_cube: dict[tuple[str, str], dict[str, Any]] = defaultdict(cube_cell)

    inventory_latest_at = ""
    for row in inventory_rows:
        warehouse = warehouse_by_code.get(text(row.get("wms_code")))
        product = product_dimension_by_code.get(text(row.get("sku_code")))
        if not warehouse or not product:
            continue
        key = (warehouse["region"], product["category_code"])
        stock_qty = number(row.get("available_qty"))
        price = latest_price.get(product["sku_code"])
        for cell in (cube[key], warehouse_cube[(warehouse["code"], product["category_code"])]):
            cell["stock_qty"] += stock_qty
            cell["all_stock_qty"] += number(row.get("all_stock_qty"))
            cell["warehouses"].add(warehouse["code"])
            if price:
                cell["stock_value"] += stock_qty * price
                cell["priced_stock_qty"] += stock_qty
        inventory_latest_at = max(inventory_latest_at, iso_or_empty(row.get("inventory_updated_at")))

    for row in sales_monthly_rows:
        warehouse = warehouse_by_code.get(text(row.get("wms_code")))
        product = product_dimension_by_code.get(text(row.get("sku_code")))
        if not warehouse or not product:
            continue
        key = (warehouse["region"], product["category_code"])
        month = text(row.get("sale_month"))
        for cell in (cube[key], warehouse_cube[(warehouse["code"], product["category_code"])]):
            cell["monthly_qty"][month] += number(row.get("ship_qty"))
            cell["monthly_amount"][month] += number(row.get("sales_amount"))

    for row in sales_30d_rows:
        warehouse = warehouse_by_code.get(text(row.get("wms_code")))
        product = product_dimension_by_code.get(text(row.get("sku_code")))
        if not warehouse or not product:
            continue
        key = (warehouse["region"], product["category_code"])
        for cell in (cube[key], warehouse_cube[(warehouse["code"], product["category_code"])]):
            cell["sales_30d_qty"] += number(row.get("ship_qty"))
            cell["request_30d_qty"] += number(row.get("request_qty"))
            cell["sales_30d_amount"] += number(row.get("sales_amount"))
            cell["sales_detail_rows"] += integer(row.get("detail_rows"))
            cell["sales_amount_nonzero_rows"] += integer(row.get("amount_nonzero_rows"))

    vendor_metrics: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "vendor_code": "", "vendor_name": "", "line_count": 0,
        "request_qty": 0.0, "received_qty": 0.0, "completed_lines": 0,
        "timing_covered_lines": 0, "on_time_lines": 0, "late_lines": 0,
    })
    in_transit_total = 0.0
    for row in purchase_rows:
        warehouse = warehouse_by_code.get(text(row.get("wms_code")))
        product = product_dimension_by_code.get(text(row.get("sku_code")))
        if not warehouse or not product:
            continue
        request_qty = number(row.get("request_quantity"))
        received_qty = number(row.get("received_quantity"))
        due_qty = number(row.get("due_in_quantity"))
        order_status = text(row.get("order_status"))
        remaining_qty = max(due_qty, request_qty - received_qty, 0.0)
        if order_status in {"12", "13", "14", "15", "16"} and remaining_qty > 0:
            key = (warehouse["region"], product["category_code"])
            cube[key]["in_transit_qty"] += remaining_qty
            warehouse_cube[(warehouse["code"], product["category_code"])]["in_transit_qty"] += remaining_qty
            in_transit_total += remaining_qty

        vendor_key = text(row.get("vendor_code")) or text(row.get("vendor_name")) or "未配置供应商"
        vendor = vendor_metrics[vendor_key]
        vendor["vendor_code"] = text(row.get("vendor_code"))
        vendor["vendor_name"] = text(row.get("vendor_name")) or vendor_key
        vendor["line_count"] += 1
        vendor["request_qty"] += request_qty
        vendor["received_qty"] += received_qty
        is_completed = request_qty > 0 and received_qty >= request_qty
        if is_completed:
            vendor["completed_lines"] += 1
            planned = parse_datetime(row.get("planned_delivery_time"))
            actual = parse_datetime(row.get("actual_inventory_time"))
            if planned and actual:
                vendor["timing_covered_lines"] += 1
                if actual <= planned:
                    vendor["on_time_lines"] += 1
                else:
                    vendor["late_lines"] += 1

    category_codes = [code for code in CATEGORY_NAMES if any(p["category_code"] == code for p in product_dimensions)]
    regions = [region for region in REGION_ORDER if any(w["region"] == region for w in warehouse_dimensions)]
    month_keys = [month.strftime("%Y-%m") for month in history_months]

    matrix: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    totals = defaultdict(float)
    risk_counts = defaultdict(int)
    for region in regions:
        region_row = {"region": region, "categories": []}
        for category_code in category_codes:
            cell = cube[(region, category_code)]
            history_qty = [cell["monthly_qty"].get(month, 0.0) for month in month_keys]
            forecast_qty, trend_rate = forecast_from_history(history_qty)
            status, cover_days = risk_status(cell["stock_qty"], cell["sales_30d_qty"])
            risk_counts[status] += 1
            value_coverage = cell["priced_stock_qty"] / cell["stock_qty"] if cell["stock_qty"] > 0 else 1.0
            item = {
                "category_code": category_code,
                "category_name": CATEGORY_NAMES[category_code],
                "status": status,
                "cover_days": round(cover_days, 1) if cover_days is not None else None,
                "available_qty": round(cell["stock_qty"], 2),
                "all_stock_qty": round(cell["all_stock_qty"], 2),
                "sales_30d_qty": round(cell["sales_30d_qty"], 2),
                "request_30d_qty": round(cell["request_30d_qty"], 2),
                "sales_30d_amount": round(cell["sales_30d_amount"], 2),
                "inventory_value": round(cell["stock_value"], 2),
                "inventory_value_coverage": round(value_coverage, 4),
                "in_transit_qty": round(cell["in_transit_qty"], 2),
                "forecast_next_month_qty": round(forecast_qty, 2),
                "trend_rate": round(trend_rate, 4),
                "monthly_history": [
                    {"month": month, "qty": round(cell["monthly_qty"].get(month, 0.0), 2), "amount": round(cell["monthly_amount"].get(month, 0.0), 2)}
                    for month in month_keys
                ],
                "warehouse_count": len(cell["warehouses"]),
            }
            region_row["categories"].append(item)
            totals["stock_qty"] += cell["stock_qty"]
            totals["all_stock_qty"] += cell["all_stock_qty"]
            totals["stock_value"] += cell["stock_value"]
            totals["priced_stock_qty"] += cell["priced_stock_qty"]
            totals["sales_30d_qty"] += cell["sales_30d_qty"]
            totals["request_30d_qty"] += cell["request_30d_qty"]
            totals["sales_30d_amount"] += cell["sales_30d_amount"]
            totals["sales_detail_rows"] += cell["sales_detail_rows"]
            totals["sales_amount_nonzero_rows"] += cell["sales_amount_nonzero_rows"]
            totals["forecast_qty"] += forecast_qty

            if status in {"replenish", "watch", "pause"}:
                if status == "replenish":
                    title = f"{region} · {CATEGORY_NAMES[category_code]}需要优先补货"
                    detail = f"可用库存仅覆盖 {cover_days:.1f} 天，采购在途 {integer(cell['in_transit_qty']):,} 件。"
                    priority = 3
                elif status == "watch":
                    title = f"{region} · {CATEGORY_NAMES[category_code]}进入库存观察区"
                    detail = f"可用库存覆盖 {cover_days:.1f} 天，接近 14 天补货阈值。"
                    priority = 2
                else:
                    title = f"{region} · {CATEGORY_NAMES[category_code]}建议暂停加单"
                    if cover_days is None:
                        detail = f"近 30 天无销量但仍有 {integer(cell['stock_qty']):,} 件可用库存。"
                    else:
                        detail = f"可用库存覆盖 {cover_days:.1f} 天，高于 60 天上限。"
                    priority = 1
                actions.append({
                    "priority": priority,
                    "status": status,
                    "region": region,
                    "category_code": category_code,
                    "category_name": CATEGORY_NAMES[category_code],
                    "title": title,
                    "detail": detail,
                    "cover_days": round(cover_days, 1) if cover_days is not None else None,
                    "available_qty": round(cell["stock_qty"], 2),
                    "in_transit_qty": round(cell["in_transit_qty"], 2),
                })
        matrix.append(region_row)

    actions.sort(key=lambda row: (-row["priority"], row["cover_days"] if row["cover_days"] is not None else 10**9))
    actions = actions[:12]

    warehouse_matrix: list[dict[str, Any]] = []
    warehouse_actions: list[dict[str, Any]] = []
    for warehouse in warehouse_dimensions:
        warehouse_row = {
            "warehouse_code": warehouse["code"],
            "warehouse_name": warehouse["name"],
            "short_name": warehouse["short_name"],
            "region": warehouse["region"],
            "categories": [],
        }
        for category_code in category_codes:
            cell = warehouse_cube[(warehouse["code"], category_code)]
            history_qty = [cell["monthly_qty"].get(month, 0.0) for month in month_keys]
            forecast_qty, trend_rate = forecast_from_history(history_qty)
            status, cover_days = risk_status(cell["stock_qty"], cell["sales_30d_qty"])
            value_coverage = cell["priced_stock_qty"] / cell["stock_qty"] if cell["stock_qty"] > 0 else 1.0
            item = {
                "category_code": category_code,
                "category_name": CATEGORY_NAMES[category_code],
                "status": status,
                "cover_days": round(cover_days, 1) if cover_days is not None else None,
                "available_qty": round(cell["stock_qty"], 2),
                "all_stock_qty": round(cell["all_stock_qty"], 2),
                "sales_30d_qty": round(cell["sales_30d_qty"], 2),
                "request_30d_qty": round(cell["request_30d_qty"], 2),
                "sales_30d_amount": round(cell["sales_30d_amount"], 2),
                "inventory_value": round(cell["stock_value"], 2),
                "inventory_value_coverage": round(value_coverage, 4),
                "in_transit_qty": round(cell["in_transit_qty"], 2),
                "forecast_next_month_qty": round(forecast_qty, 2),
                "trend_rate": round(trend_rate, 4),
                "monthly_history": [
                    {"month": month, "qty": round(cell["monthly_qty"].get(month, 0.0), 2), "amount": round(cell["monthly_amount"].get(month, 0.0), 2)}
                    for month in month_keys
                ],
            }
            warehouse_row["categories"].append(item)
            if status in {"replenish", "watch", "pause"}:
                if status == "replenish":
                    title = f"{warehouse['short_name']} · {CATEGORY_NAMES[category_code]}需要优先补货"
                    detail = f"可用库存覆盖 {cover_days:.1f} 天，在途 {integer(cell['in_transit_qty']):,} 件。"
                    priority = 3
                elif status == "watch":
                    title = f"{warehouse['short_name']} · {CATEGORY_NAMES[category_code]}进入观察区"
                    detail = f"可用库存覆盖 {cover_days:.1f} 天，接近 14 天补货阈值。"
                    priority = 2
                else:
                    title = f"{warehouse['short_name']} · {CATEGORY_NAMES[category_code]}建议暂停加单"
                    detail = (
                        f"近 30 天无销量但仍有 {integer(cell['stock_qty']):,} 件可用库存。"
                        if cover_days is None else f"可用库存覆盖 {cover_days:.1f} 天，高于 60 天上限。"
                    )
                    priority = 1
                warehouse_actions.append({
                    "priority": priority,
                    "status": status,
                    "region": warehouse["region"],
                    "warehouse_code": warehouse["code"],
                    "warehouse_name": warehouse["name"],
                    "warehouse_short_name": warehouse["short_name"],
                    "category_code": category_code,
                    "category_name": CATEGORY_NAMES[category_code],
                    "title": title,
                    "detail": detail,
                    "cover_days": round(cover_days, 1) if cover_days is not None else None,
                    "available_qty": round(cell["stock_qty"], 2),
                    "in_transit_qty": round(cell["in_transit_qty"], 2),
                })
        warehouse_matrix.append(warehouse_row)
    warehouse_actions.sort(key=lambda row: (-row["priority"], row["cover_days"] if row["cover_days"] is not None else 10**9))

    sales_trend: list[dict[str, Any]] = []
    for month in month_keys:
        qty = sum(cube[key]["monthly_qty"].get(month, 0.0) for key in cube)
        amount = sum(cube[key]["monthly_amount"].get(month, 0.0) for key in cube)
        sales_trend.append({"month": month, "type": "actual", "qty": round(qty, 2), "amount": round(amount, 2)})
    sales_trend.append({
        "month": current_month.strftime("%Y-%m"),
        "type": "forecast",
        "qty": round(totals["forecast_qty"], 2),
        "amount": None,
    })

    supplier_rows: list[dict[str, Any]] = []
    supplier_total_covered = 0
    supplier_total_ontime = 0
    for vendor in vendor_metrics.values():
        if vendor["request_qty"] <= 0:
            continue
        fulfillment = vendor["received_qty"] / vendor["request_qty"]
        on_time_rate = (
            vendor["on_time_lines"] / vendor["timing_covered_lines"]
            if vendor["timing_covered_lines"] > 0 else None
        )
        supplier_total_covered += vendor["timing_covered_lines"]
        supplier_total_ontime += vendor["on_time_lines"]
        supplier_rows.append({
            **vendor,
            "request_qty": round(vendor["request_qty"], 2),
            "received_qty": round(vendor["received_qty"], 2),
            "fulfillment_rate": round(fulfillment, 4),
            "on_time_rate": round(on_time_rate, 4) if on_time_rate is not None else None,
        })
    supplier_rows.sort(
        key=lambda row: (
            row["on_time_rate"] is None,
            row["on_time_rate"] if row["on_time_rate"] is not None else 1.0,
            -row["request_qty"],
        )
    )
    supplier_rows = supplier_rows[:8]
    supplier_on_time_rate = supplier_total_ontime / supplier_total_covered if supplier_total_covered else None

    total_cover_days = (
        totals["stock_qty"] / (totals["sales_30d_qty"] / 30.0)
        if totals["sales_30d_qty"] > 0 else None
    )
    outbound_fulfillment = (
        totals["sales_30d_qty"] / totals["request_30d_qty"]
        if totals["request_30d_qty"] > 0 else None
    )
    inventory_value_coverage = (
        totals["priced_stock_qty"] / totals["stock_qty"] if totals["stock_qty"] > 0 else 1.0
    )
    sales_amount_coverage = (
        totals["sales_amount_nonzero_rows"] / totals["sales_detail_rows"]
        if totals["sales_detail_rows"] > 0 else 0.0
    )

    replenish_actions = [row for row in actions if row["status"] == "replenish"]
    pause_actions = [row for row in actions if row["status"] == "pause"]
    conclusions: list[dict[str, str]] = []
    if replenish_actions:
        top = replenish_actions[0]
        conclusions.append({"tone": "risk", "text": f"首要缺货风险：{top['region']} {top['category_name']}，库存覆盖 {top['cover_days']:.1f} 天。"})
    if pause_actions:
        top = pause_actions[0]
        cover_text = "近 30 天无销量" if top["cover_days"] is None else f"库存覆盖 {top['cover_days']:.1f} 天"
        conclusions.append({"tone": "warning", "text": f"首要积压风险：{top['region']} {top['category_name']}，{cover_text}。"})
    if supplier_on_time_rate is not None:
        conclusions.append({"tone": "neutral", "text": f"供应商按时入库率 {supplier_on_time_rate:.1%}，基于 {supplier_total_covered:,} 条计划与实际时间完整的采购明细。"})
    conclusions.append({"tone": "neutral", "text": f"当前在售范围 {len(product_dimensions)} 个 SKU、{len(warehouse_dimensions)} 个仓库；库存金额价格覆盖 {inventory_value_coverage:.1%}。"})

    risk_cell_count = risk_counts["replenish"] + risk_counts["pause"]
    total_matrix_cells = sum(risk_counts.values()) or 1
    health_score = round(100 * (
        risk_counts["healthy"]
        + risk_counts["watch"] * 0.75
        + risk_counts["replenish"] * 0.25
        + risk_counts["pause"] * 0.35
    ) / total_matrix_cells)
    payload = {
        "meta": {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "data_date": today.isoformat(),
            "inventory_updated_at": inventory_latest_at,
            "source": "Doris / ywzt_ods",
            "scope_source": str(workbook_audit.get("source_file", "")),
            "scope": {
                "product_count": len(product_dimensions),
                "master_sku_matched_count": len(master_by_sku),
                "warehouse_count": len(warehouse_dimensions),
                "region_count": len(regions),
                "category_count": len(category_codes),
            },
            "definitions": {
                "product_scope": "基础数据线上.xlsx：产品大类=产成品，布仓策略为全铺或常温类",
                "available_inventory": "在库 - 业务中台占用 - 锁定（锁定为0时扣冻结），最低为0",
                "inventory_value": "可用库存 × 最近365天最新有效标准采购价",
                "sales": "已完成出库明细，近30天按ship_at统计，金额取total_pay_amount",
                "forecast": "近6个完整月销量按5%/10%/15%/20%/20%/30%加权，并叠加最近3个月趋势（±20%封顶）",
                "risk": "库存覆盖<14天补货、14-30天观察、>60天暂停；近30天无销量且有库存也归为暂停",
                "in_transit": "采购单状态12-16，取到货数量与申请量减已收量两者较大值",
                "supplier_on_time": "完整收货明细中，全部入库时间不晚于计划交付时间",
            },
            "quality": {
                "inventory_value_coverage": round(inventory_value_coverage, 4),
                "sales_amount_detail_coverage": round(sales_amount_coverage, 4),
                "supplier_timing_covered_lines": supplier_total_covered,
                "unmatched_product_codes": [p["sku_code"] for p in product_dimensions if not p["master_sku_matched"]],
                "unmatched_warehouse_names": [name for name in warehouse_names if name not in warehouse_by_name],
            },
        },
        "filters": {
            "regions": regions,
            "warehouses": warehouse_dimensions,
            "categories": [{"code": code, "name": CATEGORY_NAMES[code]} for code in category_codes],
        },
        "products": product_dimensions,
        "kpis": {
            "sales_amount_30d": round(totals["sales_30d_amount"], 2),
            "available_inventory_qty": round(totals["stock_qty"], 2),
            "inventory_value": round(totals["stock_value"], 2),
            "inventory_value_coverage": round(inventory_value_coverage, 4),
            "turnover_days": round(total_cover_days, 1) if total_cover_days is not None else None,
            "outbound_fulfillment_rate": round(outbound_fulfillment, 4) if outbound_fulfillment is not None else None,
            "purchase_in_transit_qty": round(in_transit_total, 2),
            "supplier_on_time_rate": round(supplier_on_time_rate, 4) if supplier_on_time_rate is not None else None,
            "risk_cell_count": risk_cell_count,
            "health_score": health_score,
        },
        "conclusions": conclusions,
        "matrix": matrix,
        "warehouse_matrix": warehouse_matrix,
        "actions": actions,
        "warehouse_actions": warehouse_actions,
        "sales_trend": sales_trend,
        "inventory_structure": [
            {"status": "replenish", "label": "补货风险", "count": risk_counts["replenish"]},
            {"status": "watch", "label": "观察", "count": risk_counts["watch"]},
            {"status": "healthy", "label": "健康", "count": risk_counts["healthy"]},
            {"status": "pause", "label": "高库存/无销量", "count": risk_counts["pause"]},
        ],
        "supplier_ranking": supplier_rows,
        "audit": audit,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    JSON_OUTPUT.write_text(encoded + "\n", encoding="utf-8")
    JS_OUTPUT.write_text("window.SUPPLY_CHAIN_DASHBOARD_DATA = " + encoded + ";\n", encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "json": str(JSON_OUTPUT),
        "js": str(JS_OUTPUT),
        "product_count": len(product_dimensions),
        "warehouse_count": len(warehouse_dimensions),
        "inventory_rows": len(inventory_rows),
        "sales_monthly_rows": len(sales_monthly_rows),
        "sales_30d_rows": len(sales_30d_rows),
        "purchase_rows": len(purchase_rows),
        "risk_cell_count": risk_cell_count,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

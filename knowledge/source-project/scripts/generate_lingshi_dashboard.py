from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import shutil
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from copy import copy
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import openpyxl
import pymysql
from openpyxl.cell.cell import MergedCell
from openpyxl.utils import get_column_letter
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATE = ROOT / "templates" / "lingshi_dashboard_template.xlsx"
DEFAULT_ENV_FILE = ROOT / "config" / "pickup.env"
DEFAULT_PUSH_ENV_FILE = ROOT / "config" / "push.env"
DEFAULT_OUTPUT_DIR = ROOT / "outputs"

TODAY = date.today()
RECENT_PRODUCTION_DAYS = 75
CARGO_OWNER_ID = "HJXH"
FDS_CARGO_OWNER_ID = "FDS"
FDS_SKU_CODES = {"5006070600000100"}
DASHBOARD_METRICS = ["可用库存", "在途数量", "7日均销", "可用天数"]
DASHBOARD_GROUP_WIDTH = len(DASHBOARD_METRICS)
ALERT_DAYS_THRESHOLD = 5
DATE_EXEMPT_SKU_NAME_KEYWORDS = ("盒",)
REPORT_EXCLUDED_SKU_CODES = {"5001070072210220"}
SCREENSHOT_EXCLUDED_SKU_CODES = {"5001010070000125"}
SCREENSHOT_EXCLUDED_SKU_NAME_KEYWORDS = ("芝士",)
FDS_WAREHOUSE_BY_HJXH_WAREHOUSE = {
    "皇家小虎长沙黄兴万纬仓xs": {"wms_code": "WMS000190", "wms_desc": "肥大圣长沙黄兴万纬仓"},
    "皇家小虎合肥万纬仓xs": {"wms_code": "WMS000187", "wms_desc": "肥大圣合肥万纬仓xs"},
    "皇家小虎南昌万纬仓xs": {"wms_code": "WMS000182", "wms_desc": "肥大圣南昌万纬仓xs"},
    "皇家小虎泉州万纬仓xs": {"wms_code": "WMS000183", "wms_desc": "肥大圣泉州万纬仓xs"},
}

SALES_HEADERS = [
    "发货日期",
    "仓库编码",
    "仓库描述",
    "商品编码",
    "商品描述",
    "总发货量(箱)",
    "线上汇总(箱)",
    "抖音(箱)",
    "抖音小时达(箱)",
    "拼多多系(箱)",
    "快手系(箱)",
    "淘宝系(箱)",
    "京东系(箱)",
    "唯品会(箱)",
    "线上其它(箱)",
    "线下汇总(箱)",
    "线下社区团购_平台(箱)",
    "线下社区团购_分销(箱)",
    "线下其它(箱)",
    "其它(箱)",
]

INVENTORY_HEADERS = [
    "货主",
    "wms_desc",
    "SKU编码",
    "SKU名称",
    "箱规",
    "可用数量",
    "汇总数量",
    "在库数量",
    "在途数量",
    "分配数量",
    "今日总出库数(箱)",
    "昨日总出库数(箱)",
    "7日内总出库数(箱)",
    "近七天平均出库数(箱)",
    "库存可用天数",
    "锁定数量",
    "冻结数量",
    "业务中台占用数量",
    "OFS占有用数量",
    "采购在途",
    "调拨在途",
    "在途生产日期最早",
    "在途生产日期最晚",
]


def load_env_file(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"env file not found: {path}")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_optional_env_file(path: Path) -> None:
    if path.exists():
        load_env_file(path)


def connect():
    host = os.environ.get("PICKUP_DB_HOST", "").strip()
    user = os.environ.get("PICKUP_DB_USER", "").strip()
    password = os.environ.get("PICKUP_DB_PASSWORD", "")
    database = os.environ.get("PICKUP_DB_NAME", "ywzt_ods").strip() or "ywzt_ods"
    port = int(os.environ.get("PICKUP_DB_PORT", "9030"))
    missing = [
        name
        for name, value in {
            "PICKUP_DB_HOST": host,
            "PICKUP_DB_USER": user,
            "PICKUP_DB_PASSWORD": password,
        }.items()
        if not value
    ]
    if missing:
        raise SystemExit(f"missing database env vars: {', '.join(missing)}")
    return pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=20,
        read_timeout=240,
        write_timeout=60,
    )


def to_number(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def excel_number(value: Any) -> int | float:
    number = to_number(value)
    if abs(number - round(number)) < 0.000001:
        return int(round(number))
    return round(number, 6)


def is_date_exempt_sku(sku: dict[str, Any]) -> bool:
    sku_name = str(sku.get("sku_name") or "")
    return any(keyword in sku_name for keyword in DATE_EXEMPT_SKU_NAME_KEYWORDS)


def is_report_excluded_sku(sku: dict[str, Any]) -> bool:
    return str(sku.get("sku_code") or "").strip() in REPORT_EXCLUDED_SKU_CODES


def is_screenshot_excluded_sku(sku: dict[str, Any]) -> bool:
    sku_code = str(sku.get("sku_code") or "").strip()
    sku_name = str(sku.get("sku_name") or "").strip()
    return sku_code in SCREENSHOT_EXCLUDED_SKU_CODES or any(
        keyword in sku_name for keyword in SCREENSHOT_EXCLUDED_SKU_NAME_KEYWORDS
    )


def ceil_non_negative(value: Any) -> int:
    number = to_number(value)
    if number <= 0:
        return 0
    return int(math.ceil(number))


def ceil_box_qty(ea_qty: Any, box_spec: Any) -> int:
    unit = to_number(box_spec)
    if unit <= 0:
        return 0
    return ceil_non_negative(to_number(ea_qty) / unit)


def cell_snapshot(cell) -> dict[str, Any]:
    return {
        "style": copy(cell._style),
        "font": copy(cell.font),
        "fill": copy(cell.fill),
        "border": copy(cell.border),
        "alignment": copy(cell.alignment),
        "protection": copy(cell.protection),
        "number_format": cell.number_format,
    }


def apply_cell_snapshot(cell, snapshot: dict[str, Any]) -> None:
    cell._style = copy(snapshot["style"])
    cell.font = copy(snapshot["font"])
    cell.fill = copy(snapshot["fill"])
    cell.border = copy(snapshot["border"])
    cell.alignment = copy(snapshot["alignment"])
    cell.protection = copy(snapshot["protection"])
    cell.number_format = snapshot["number_format"]


def row_style_snapshot(ws, template_row: int, max_col: int) -> list[dict[str, Any]]:
    return [cell_snapshot(ws.cell(template_row, col)) for col in range(1, max_col + 1)]


def apply_row_style(ws, row_idx: int, styles: list[dict[str, Any]]) -> None:
    for col_idx, style in enumerate(styles, start=1):
        apply_cell_snapshot(ws.cell(row_idx, col_idx), style)


def clear_data_rows(ws, header_count: int = 1) -> None:
    if ws.max_row > header_count:
        ws.delete_rows(header_count + 1, ws.max_row - header_count)


def read_template_scope(template: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    wb = openpyxl.load_workbook(template, data_only=False)
    ws = wb["看板"]

    warehouses: list[dict[str, Any]] = []
    for col in range(4, ws.max_column + 1, 3):
        warehouse_name = ws.cell(1, col).value
        if warehouse_name:
            warehouses.append(
                {
                    "column": col,
                    "warehouse_name": str(warehouse_name).strip(),
                    "short_name": str(ws.cell(2, col).value or "").strip(),
                }
            )

    skus: list[dict[str, Any]] = []
    for row in range(4, ws.max_row + 1):
        sku_code = ws.cell(row, 1).value
        if sku_code:
            skus.append(
                {
                    "row": row,
                    "sku_code": str(sku_code).strip(),
                    "sku_name": str(ws.cell(row, 2).value or "").strip(),
                    "box_spec": to_number(ws.cell(row, 3).value),
                }
            )
    return warehouses, skus


def match_warehouses(connection, names: list[str]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    placeholders = ",".join(["%s"] * len(names))
    sql = f"""
        SELECT wms_code, wms_desc, wms_type
        FROM master_wms
        WHERE wms_desc IN ({placeholders})
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, names)
        exact_rows = list(cursor.fetchall())

    by_name = {str(row["wms_desc"]): row for row in exact_rows}
    unresolved = [name for name in names if name not in by_name]
    candidates: list[dict[str, Any]] = []

    if unresolved:
        like_clauses = " OR ".join(["wms_desc LIKE %s"] * len(unresolved))
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT wms_code, wms_desc, wms_type
                FROM master_wms
                WHERE {like_clauses}
                ORDER BY wms_desc
                """,
                [f"%{name.replace('xs', '').replace('-', '%')}%" for name in unresolved],
            )
            candidates = list(cursor.fetchall())
        for name in unresolved:
            simplified = name.replace("xs", "")
            hits = [row for row in candidates if simplified in str(row.get("wms_desc") or "")]
            if len(hits) == 1:
                by_name[name] = hits[0]

    audit = []
    for name in names:
        row = by_name.get(name)
        audit.append(
            {
                "requested_name": name,
                "wms_code": row.get("wms_code") if row else None,
                "wms_desc": row.get("wms_desc") if row else None,
                "matched": bool(row),
            }
        )
    return by_name, audit


def build_sku_routes(
    warehouses: list[dict[str, Any]],
    skus: list[dict[str, Any]],
    warehouse_map: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    routes: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    for sku in skus:
        sku_code = str(sku["sku_code"])
        date_exempt = is_date_exempt_sku(sku)
        for warehouse in warehouses:
            report_warehouse = str(warehouse["warehouse_name"])
            if sku_code in FDS_SKU_CODES:
                fds_warehouse = FDS_WAREHOUSE_BY_HJXH_WAREHOUSE.get(report_warehouse)
                if not fds_warehouse:
                    audit.append(
                        {
                            "sku_code": sku_code,
                            "sku_name": sku.get("sku_name"),
                            "report_warehouse": report_warehouse,
                            "cargo_owner_id": FDS_CARGO_OWNER_ID,
                            "source_wms_code": None,
                            "source_wms_desc": None,
                            "route_type": "fds_no_matching_city",
                            "date_exempt": date_exempt,
                        }
                    )
                    continue
                routes.append(
                    {
                        "cargo_owner_id": FDS_CARGO_OWNER_ID,
                        "source_wms_code": fds_warehouse["wms_code"],
                        "source_wms_desc": fds_warehouse["wms_desc"],
                        "report_wms_desc": report_warehouse,
                        "source_sku_code": sku_code,
                        "report_sku_code": sku_code,
                        "route_type": "fds_city_warehouse",
                        "date_exempt": date_exempt,
                    }
                )
                audit.append(
                    {
                        "sku_code": sku_code,
                        "sku_name": sku.get("sku_name"),
                        "report_warehouse": report_warehouse,
                        "cargo_owner_id": FDS_CARGO_OWNER_ID,
                        "source_wms_code": fds_warehouse["wms_code"],
                        "source_wms_desc": fds_warehouse["wms_desc"],
                        "route_type": "fds_city_warehouse",
                        "date_exempt": date_exempt,
                    }
                )
                continue

            matched = warehouse_map.get(report_warehouse)
            if not matched:
                audit.append(
                    {
                        "sku_code": sku_code,
                        "sku_name": sku.get("sku_name"),
                        "report_warehouse": report_warehouse,
                        "cargo_owner_id": CARGO_OWNER_ID,
                        "source_wms_code": None,
                        "source_wms_desc": None,
                        "route_type": "hjxh_unmatched_warehouse",
                        "date_exempt": date_exempt,
                    }
                )
                continue
            routes.append(
                {
                    "cargo_owner_id": CARGO_OWNER_ID,
                    "source_wms_code": matched["wms_code"],
                    "source_wms_desc": matched["wms_desc"],
                    "report_wms_desc": report_warehouse,
                    "source_sku_code": sku_code,
                    "report_sku_code": sku_code,
                    "route_type": "hjxh_template_warehouse",
                    "date_exempt": date_exempt,
                }
            )
    return routes, audit


def route_cte_sql(route_count: int) -> str:
    selects: list[str] = []
    for idx in range(route_count):
        if idx == 0:
            selects.append(
                """
                SELECT
                    %s AS cargo_owner_id,
                    %s AS source_wms_code,
                    %s AS report_wms_desc,
                    %s AS sku_code,
                    %s AS report_sku_code,
                    %s AS date_exempt
                """
            )
        else:
            selects.append("SELECT %s, %s, %s, %s, %s, %s")
    return "\nUNION ALL\n".join(selects)


def fetch_box_specs(connection, sku_codes: list[str]) -> tuple[dict[str, float], list[dict[str, Any]]]:
    unique_codes = sorted({str(code).strip() for code in sku_codes if str(code).strip()})
    if not unique_codes:
        return {}, []

    placeholders = ",".join(["%s"] * len(unique_codes))
    sql = f"""
        SELECT
            u.sku_code,
            MAX(u.sku_name) AS sku_name,
            MAX(CASE WHEN u.unit = '40' THEN u.ea_convert_quantity ELSE NULL END) AS box_spec,
            MAX(m.sku_group) AS sku_group
        FROM master_sku_unit u
        LEFT JOIN master_sku m
            ON m.sku_code = u.sku_code
        WHERE u.sku_code IN ({placeholders})
        GROUP BY u.sku_code
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, unique_codes)
        rows = list(cursor.fetchall())

    by_sku: dict[str, float] = {}
    audit: list[dict[str, Any]] = []
    row_by_sku = {str(row.get("sku_code") or ""): row for row in rows}
    for sku_code in unique_codes:
        row = row_by_sku.get(sku_code, {})
        box_spec = to_number(row.get("box_spec"))
        if box_spec > 0:
            by_sku[sku_code] = box_spec
        audit.append(
            {
                "sku_code": sku_code,
                "sku_name": row.get("sku_name"),
                "box_spec": excel_number(box_spec),
                "sku_group": row.get("sku_group"),
                "matched": box_spec > 0,
            }
        )
    return by_sku, audit


def fetch_recent_inventory(
    connection,
    sku_routes: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not sku_routes:
        return [], {}

    route_sql = route_cte_sql(len(sku_routes))
    sql = f"""
        WITH route AS (
            {route_sql}
        )
        SELECT
            w.cargo_owner_id AS cargo_owner_id,
            w.wms_code AS warehouse_code,
            MAX(mw.wms_desc) AS source_warehouse_name,
            r.report_wms_desc AS warehouse_name,
            w.sku_code AS sku_code,
            r.report_sku_code AS report_sku_code,
            COALESCE(m.sku_name, MAX(w.sku_name)) AS sku_name,
            SUM(IFNULL(w.in_stock_quantity, 0)) AS recent_stock_qty,
            SUM(IFNULL(w.in_stock_quantity, 0)) AS summary_qty,
            SUM(IFNULL(w.in_stock_quantity, 0)) AS in_stock_qty,
            CURDATE() AS stock_snapshot_date,
            MIN(DATE(w.production_date)) AS min_production_date,
            MAX(DATE(w.production_date)) AS max_production_date,
            COUNT(*) AS source_rows
        FROM scm_warehous_manage w
        LEFT JOIN master_wms mw
            ON mw.wms_code = w.wms_code
        LEFT JOIN master_sku m
            ON m.sku_code = w.sku_code
        INNER JOIN route r
            ON r.cargo_owner_id = w.cargo_owner_id
           AND r.source_wms_code = w.wms_code
           AND r.sku_code = w.sku_code
        WHERE (
              r.date_exempt = 1
              OR (
                  DATE(w.production_date) > CURDATE() - INTERVAL {RECENT_PRODUCTION_DAYS} DAY
                  AND DATE(w.production_date) <= CURDATE()
              )
          )
          AND (w.inventory_status = 'ZP' OR w.inventory_status IS NULL OR w.inventory_status = '')
          AND IFNULL(w.in_stock_quantity, 0) <> 0
          AND (m.sku_group = '50' OR r.date_exempt = 1)
        GROUP BY
            w.cargo_owner_id,
            w.wms_code,
            r.report_wms_desc,
            w.sku_code,
            r.report_sku_code,
            m.sku_name
    """
    params: list[Any] = []
    for route in sku_routes:
        params.extend(
            [
                route["cargo_owner_id"],
                route["source_wms_code"],
                route["report_wms_desc"],
                route["source_sku_code"],
                route["report_sku_code"],
                1 if route.get("date_exempt") else 0,
            ]
        )
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = list(cursor.fetchall())

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT CURDATE() AS snapshot_date
            """,
        )
        snapshot = cursor.fetchone() or {}

    return rows, snapshot


def fetch_sales(
    connection,
    sku_routes: list[dict[str, Any]],
    sales_start: date,
    sales_end_exclusive: date,
) -> list[dict[str, Any]]:
    if not sku_routes:
        return []

    route_sql = route_cte_sql(len(sku_routes))
    sql = f"""
        WITH route AS (
            {route_sql}
        )
        SELECT
            DATE(h.ship_at) AS ship_date,
            h.facility_code AS warehouse_code,
            MAX(w.wms_desc) AS source_warehouse_name,
            r.report_wms_desc AS warehouse_name,
            i.sku_code AS sku_code,
            r.report_sku_code AS report_sku_code,
            m.sku_name AS sku_name,
            SUM(i.ship_qty) AS total_ship_qty,
            SUM(CASE WHEN s.type = 'Z004' THEN i.ship_qty ELSE 0 END) AS online_ship_qty,
            SUM(CASE WHEN s.type = 'Z004' AND s.platform_code = 'DY' THEN i.ship_qty ELSE 0 END) AS online_douyin_qty,
            SUM(CASE WHEN s.type = 'Z004' AND s.platform_code = 'DYXD' THEN i.ship_qty ELSE 0 END) AS online_douyin_hour_qty,
            SUM(CASE WHEN s.type = 'Z004' AND s.platform_code IN ('PDD', 'PDDDF') THEN i.ship_qty ELSE 0 END) AS online_pdd_qty,
            SUM(CASE WHEN s.type = 'Z004' AND s.platform_code IN ('KS', 'KSDF') THEN i.ship_qty ELSE 0 END) AS online_kuaishou_qty,
            SUM(CASE WHEN s.type = 'Z004' AND s.platform_code IN ('TGC', 'MJYX', 'TB', 'CT', 'MAOCHAO') THEN i.ship_qty ELSE 0 END) AS online_taobao_qty,
            SUM(CASE WHEN s.type = 'Z004' AND s.platform_code IN ('JDVMI', 'JD', 'JD_MARKET') THEN i.ship_qty ELSE 0 END) AS online_jd_qty,
            SUM(CASE WHEN s.type = 'Z004' AND s.platform_code = 'WPH' THEN i.ship_qty ELSE 0 END) AS online_vip_qty,
            SUM(
                CASE
                    WHEN s.type = 'Z004'
                     AND s.platform_code NOT IN (
                        'DY', 'DYXD', 'PDD', 'PDDDF', 'KS', 'KSDF',
                        'TGC', 'MJYX', 'TB', 'CT', 'MAOCHAO',
                        'JDVMI', 'JD', 'JD_MARKET', 'WPH'
                     )
                    THEN i.ship_qty
                    ELSE 0
                END
            ) AS online_other_qty,
            SUM(CASE WHEN s.type = 'Z001' THEN i.ship_qty ELSE 0 END) AS offline_ship_qty,
            SUM(CASE WHEN s.type = 'Z001' AND s.group1 = '03' THEN i.ship_qty ELSE 0 END) AS offline_community_platform_qty,
            SUM(CASE WHEN s.type = 'Z001' AND s.price_group = '122' THEN i.ship_qty ELSE 0 END) AS offline_community_distribution_qty,
            SUM(CASE WHEN s.type = 'Z001' AND NOT (s.group1 = '03' OR s.price_group = '122') THEN i.ship_qty ELSE 0 END) AS offline_other_qty,
            SUM(CASE WHEN s.type NOT IN ('Z001', 'Z004') OR s.type IS NULL THEN i.ship_qty ELSE 0 END) AS other_ship_qty
        FROM scm_sales_shipment_header h
        INNER JOIN scm_sales_order_header h1
            ON h1.sales_order_code = h.ref_order_code
        INNER JOIN scm_sales_shipment_detail i
            ON i.outbound_code = h.outbound_code
        LEFT JOIN master_store s
            ON s.code = h.store_code
        LEFT JOIN master_sku m
            ON m.sku_code = i.sku_code
        LEFT JOIN master_wms w
            ON w.wms_code = h.facility_code
        INNER JOIN route r
            ON r.source_wms_code = h.facility_code
           AND r.sku_code = i.sku_code
           AND (h.wms_owner_code = r.cargo_owner_id OR (r.cargo_owner_id = 'FDS' AND h.wms_owner_code IS NULL))
        WHERE h.ship_at >= %s
          AND h.ship_at < %s
          AND h.status IN ('700', '900', '701', '901')
          AND (h.outbound_code LIKE 'HJXH%%' OR h.outbound_code LIKE 'FDS%%')
          AND (
              h.ref_order_type IN ('JYCK', 'HHCK', 'BFCK', 'QTCK')
              OR h.ref_order_type LIKE 'SO%%'
          )
        GROUP BY
            DATE(h.ship_at),
            h.facility_code,
            r.report_wms_desc,
            i.sku_code,
            r.report_sku_code,
            m.sku_name
    """
    params = []
    for route in sku_routes:
        params.extend(
            [
                route["cargo_owner_id"],
                route["source_wms_code"],
                route["report_wms_desc"],
                route["source_sku_code"],
                route["report_sku_code"],
                1 if route.get("date_exempt") else 0,
            ]
        )
    params.extend([sales_start, sales_end_exclusive])
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return list(cursor.fetchall())


def fetch_recent_transit(
    connection,
    sku_routes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not sku_routes:
        return []

    route_sql = route_cte_sql(len(sku_routes))
    sql = f"""
        WITH route AS (
            {route_sql}
        ),
        po AS (
            SELECT
                p.cargo_owner_id AS cargo_owner_id,
                p.take_delivery_wms AS warehouse_code,
                MAX(mw.wms_desc) AS source_warehouse_name,
                r.report_wms_desc AS warehouse_name,
                d.sku_code AS sku_code,
                r.report_sku_code AS report_sku_code,
                COALESCE(m.sku_name, MAX(d.sku_name)) AS sku_name,
                SUM(IFNULL(d.request_quantity, 0)) AS po_transit_qty,
                0 AS transfer_transit_qty,
                MIN(DATE(d.produc_date)) AS min_transit_production_date,
                MAX(DATE(d.produc_date)) AS max_transit_production_date,
                COUNT(*) AS po_source_rows,
                0 AS transfer_source_rows
            FROM scm_po_header p
            INNER JOIN scm_po_detail d
                ON p.po_code = d.po_code
            LEFT JOIN master_wms mw
                ON mw.wms_code = p.take_delivery_wms
            LEFT JOIN master_sku m
                ON m.sku_code = d.sku_code
            INNER JOIN route r
                ON r.cargo_owner_id = p.cargo_owner_id
               AND r.source_wms_code = p.take_delivery_wms
               AND r.sku_code = d.sku_code
            WHERE p.order_status IN ('12', '13', '14', '15', '16')
              AND d.status = 0
              AND (
                  r.date_exempt = 1
                  OR (
                      DATE(d.produc_date) > CURDATE() - INTERVAL {RECENT_PRODUCTION_DAYS} DAY
                      AND DATE(d.produc_date) <= CURDATE()
                  )
              )
              AND (d.inventory_status = 'ZP' OR d.inventory_status IS NULL OR d.inventory_status = '')
              AND IFNULL(d.request_quantity, 0) <> 0
              AND (m.sku_group = '50' OR r.date_exempt = 1)
            GROUP BY
                p.cargo_owner_id,
                p.take_delivery_wms,
                r.report_wms_desc,
                d.sku_code,
                r.report_sku_code,
                m.sku_name
        ),
        tr AS (
            SELECT
                d.cargo_owner_id AS cargo_owner_id,
                p.receive_wms AS warehouse_code,
                MAX(mw.wms_desc) AS source_warehouse_name,
                r.report_wms_desc AS warehouse_name,
                d.sku_code AS sku_code,
                r.report_sku_code AS report_sku_code,
                COALESCE(m.sku_name, MAX(d.sku_name)) AS sku_name,
                0 AS po_transit_qty,
                SUM(IFNULL(d.allocate_qty, 0)) AS transfer_transit_qty,
                MIN(DATE(d.production_date)) AS min_transit_production_date,
                MAX(DATE(d.production_date)) AS max_transit_production_date,
                0 AS po_source_rows,
                COUNT(*) AS transfer_source_rows
            FROM scm_transfer_order_header p
            INNER JOIN scm_sales_shipment_allocate_wms d
                ON p.outbound_code = d.outbound_code
            LEFT JOIN master_wms mw
                ON mw.wms_code = p.receive_wms
            LEFT JOIN master_sku m
                ON m.sku_code = d.sku_code
            INNER JOIN route r
                ON r.cargo_owner_id = d.cargo_owner_id
               AND r.source_wms_code = p.receive_wms
               AND r.sku_code = d.sku_code
            WHERE p.order_status IN ('13', '14', '15')
              AND (
                  r.date_exempt = 1
                  OR (
                      DATE(d.production_date) > CURDATE() - INTERVAL {RECENT_PRODUCTION_DAYS} DAY
                      AND DATE(d.production_date) <= CURDATE()
                  )
              )
              AND (d.inventory_status = 'ZP' OR d.inventory_status IS NULL OR d.inventory_status = '')
              AND IFNULL(d.allocate_qty, 0) <> 0
              AND (m.sku_group = '50' OR r.date_exempt = 1)
            GROUP BY
                d.cargo_owner_id,
                p.receive_wms,
                r.report_wms_desc,
                d.sku_code,
                r.report_sku_code,
                m.sku_name
        ),
        unioned AS (
            SELECT * FROM po
            UNION ALL
            SELECT * FROM tr
        )
        SELECT
            cargo_owner_id,
            warehouse_code,
            MAX(source_warehouse_name) AS source_warehouse_name,
            warehouse_name,
            sku_code,
            report_sku_code,
            MAX(sku_name) AS sku_name,
            SUM(po_transit_qty) + SUM(transfer_transit_qty) AS transit_qty,
            SUM(po_transit_qty) AS po_transit_qty,
            SUM(transfer_transit_qty) AS transfer_transit_qty,
            MIN(min_transit_production_date) AS min_transit_production_date,
            MAX(max_transit_production_date) AS max_transit_production_date,
            SUM(po_source_rows) AS po_source_rows,
            SUM(transfer_source_rows) AS transfer_source_rows
        FROM unioned
        GROUP BY
            cargo_owner_id,
            warehouse_code,
            warehouse_name,
            sku_code,
            report_sku_code
    """
    params: list[Any] = []
    for route in sku_routes:
        params.extend(
            [
                route["cargo_owner_id"],
                route["source_wms_code"],
                route["report_wms_desc"],
                route["source_sku_code"],
                route["report_sku_code"],
                1 if route.get("date_exempt") else 0,
            ]
        )
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return list(cursor.fetchall())


def build_transit_lookup(transit_rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    lookup: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {
            "transit_qty": 0.0,
            "po_transit_qty": 0.0,
            "transfer_transit_qty": 0.0,
            "po_source_rows": 0,
            "transfer_source_rows": 0,
            "min_transit_production_date": None,
            "max_transit_production_date": None,
        }
    )
    for row in transit_rows:
        key = (str(row.get("warehouse_name") or ""), str(row.get("report_sku_code") or row.get("sku_code") or ""))
        target = lookup[key]
        target["transit_qty"] += to_number(row.get("transit_qty"))
        target["po_transit_qty"] += to_number(row.get("po_transit_qty"))
        target["transfer_transit_qty"] += to_number(row.get("transfer_transit_qty"))
        target["po_source_rows"] += int(to_number(row.get("po_source_rows")))
        target["transfer_source_rows"] += int(to_number(row.get("transfer_source_rows")))
        min_date = row.get("min_transit_production_date")
        max_date = row.get("max_transit_production_date")
        if min_date and (target["min_transit_production_date"] is None or min_date < target["min_transit_production_date"]):
            target["min_transit_production_date"] = min_date
        if max_date and (target["max_transit_production_date"] is None or max_date > target["max_transit_production_date"]):
            target["max_transit_production_date"] = max_date
    return lookup


def merge_inventory_and_transit_rows(
    inventory_rows: list[dict[str, Any]],
    transit_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = list(inventory_rows)
    existing = {
        (str(row.get("warehouse_name") or ""), str(row.get("report_sku_code") or row.get("sku_code") or ""))
        for row in rows
    }
    for row in transit_rows:
        key = (str(row.get("warehouse_name") or ""), str(row.get("report_sku_code") or row.get("sku_code") or ""))
        if key in existing:
            continue
        rows.append(
            {
                "cargo_owner_id": row.get("cargo_owner_id"),
                "warehouse_code": row.get("warehouse_code"),
                "source_warehouse_name": row.get("source_warehouse_name"),
                "warehouse_name": row.get("warehouse_name"),
                "sku_code": row.get("sku_code"),
                "report_sku_code": row.get("report_sku_code"),
                "sku_name": row.get("sku_name"),
                "recent_stock_qty": 0,
                "summary_qty": 0,
                "in_stock_qty": 0,
                "stock_snapshot_date": TODAY,
                "min_production_date": None,
                "max_production_date": None,
                "source_rows": 0,
            }
        )
        existing.add(key)
    return rows


def inventory_excel_row(
    row: dict[str, Any],
    sales_lookup: dict[tuple[str, str], dict[str, float]],
    transit_lookup: dict[tuple[str, str], dict[str, Any]],
    box_specs: dict[str, float],
) -> list[Any]:
    key = (str(row.get("warehouse_name") or ""), str(row.get("report_sku_code") or row.get("sku_code") or ""))
    sales = sales_lookup.get(key, {})
    transit = transit_lookup.get(key, {})
    sku_code = str(row.get("report_sku_code") or row.get("sku_code") or "")
    box_spec = box_specs.get(sku_code, 0)
    recent_sales = sales.get("total_ship_qty", 0.0)
    stock = to_number(row.get("recent_stock_qty"))
    transit_qty = to_number(transit.get("transit_qty"))
    po_transit_qty = to_number(transit.get("po_transit_qty"))
    transfer_transit_qty = to_number(transit.get("transfer_transit_qty"))
    recent_sales_box = ceil_box_qty(recent_sales, box_spec)
    avg_sales_box = math.ceil(recent_sales_box / 7) if recent_sales_box else 0
    stock_qty = ceil_non_negative(stock)
    transit_integer_qty = ceil_non_negative(transit_qty)
    summary_qty = stock_qty + transit_integer_qty
    days = math.ceil(summary_qty / avg_sales_box) if avg_sales_box else 0
    return [
        row.get("cargo_owner_id") or CARGO_OWNER_ID,
        row.get("warehouse_name"),
        row.get("report_sku_code") or row.get("sku_code"),
        row.get("sku_name"),
        excel_number(box_spec),
        stock_qty,
        summary_qty,
        ceil_non_negative(row.get("in_stock_qty")),
        transit_integer_qty,
        0,
        0,
        0,
        recent_sales_box,
        avg_sales_box,
        days,
        0,
        0,
        0,
        0,
        ceil_non_negative(po_transit_qty),
        ceil_non_negative(transfer_transit_qty),
        transit.get("min_transit_production_date"),
        transit.get("max_transit_production_date"),
    ]


def sales_excel_row(row: dict[str, Any], box_specs: dict[str, float]) -> list[Any]:
    sku_code = str(row.get("report_sku_code") or row.get("sku_code") or "")
    box_spec = box_specs.get(sku_code, 0)
    return [
        row.get("ship_date"),
        row.get("warehouse_code"),
        row.get("warehouse_name"),
        row.get("report_sku_code") or row.get("sku_code"),
        row.get("sku_name"),
        ceil_box_qty(row.get("total_ship_qty"), box_spec),
        ceil_box_qty(row.get("online_ship_qty"), box_spec),
        ceil_box_qty(row.get("online_douyin_qty"), box_spec),
        ceil_box_qty(row.get("online_douyin_hour_qty"), box_spec),
        ceil_box_qty(row.get("online_pdd_qty"), box_spec),
        ceil_box_qty(row.get("online_kuaishou_qty"), box_spec),
        ceil_box_qty(row.get("online_taobao_qty"), box_spec),
        ceil_box_qty(row.get("online_jd_qty"), box_spec),
        ceil_box_qty(row.get("online_vip_qty"), box_spec),
        ceil_box_qty(row.get("online_other_qty"), box_spec),
        ceil_box_qty(row.get("offline_ship_qty"), box_spec),
        ceil_box_qty(row.get("offline_community_platform_qty"), box_spec),
        ceil_box_qty(row.get("offline_community_distribution_qty"), box_spec),
        ceil_box_qty(row.get("offline_other_qty"), box_spec),
        ceil_box_qty(row.get("other_ship_qty"), box_spec),
    ]


def build_sales_lookup(sales_rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, float]]:
    lookup: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in sales_rows:
        key = (str(row.get("warehouse_name") or ""), str(row.get("report_sku_code") or row.get("sku_code") or ""))
        for field in (
            "total_ship_qty",
            "online_ship_qty",
            "offline_ship_qty",
            "offline_community_platform_qty",
            "offline_community_distribution_qty",
            "offline_other_qty",
        ):
            lookup[key][field] += to_number(row.get(field))
    return lookup


def write_rows(ws, headers: list[str], rows: list[list[Any]], date_columns: set[int] | None = None) -> None:
    date_columns = date_columns or set()
    max_col = len(headers)
    header_styles = row_style_snapshot(ws, 1, max_col)
    data_styles = row_style_snapshot(ws, 2 if ws.max_row >= 2 else 1, max_col)
    clear_data_rows(ws, 1)

    for col_idx, header in enumerate(headers, start=1):
        cell = ws.cell(1, col_idx, header)
        apply_cell_snapshot(cell, header_styles[col_idx - 1])

    for row_idx, row_values in enumerate(rows, start=2):
        apply_row_style(ws, row_idx, data_styles)
        for col_idx, value in enumerate(row_values, start=1):
            cell = ws.cell(row_idx, col_idx, value)
            if col_idx in date_columns:
                cell.number_format = "yyyy-mm-dd"

    if rows:
        ws.auto_filter.ref = f"A1:{get_column_letter(max_col)}{len(rows) + 1}"


def rebuild_dashboard_layout(ws, warehouses: list[dict[str, Any]], skus: list[dict[str, Any]]) -> list[dict[str, Any]]:
    max_data_row = max([int(sku["row"]) for sku in skus], default=ws.max_row)
    target_max_col = 3 + len(warehouses) * DASHBOARD_GROUP_WIDTH
    for merged_range in list(ws.merged_cells.ranges):
        ws.unmerge_cells(str(merged_range))

    for row in range(1, max(max_data_row, ws.max_row) + 1):
        for col in range(4, max(target_max_col, ws.max_column) + 1):
            cell = ws.cell(row, col)
            if isinstance(cell, MergedCell):
                continue
            cell.value = None

    for row in (1, 2):
        for col in range(1, 4):
            cell = ws.cell(row, col)
            if not isinstance(cell, MergedCell):
                cell.value = None
    ws.cell(3, 1, "产品编码")
    ws.cell(3, 2, "产品名称")
    ws.cell(3, 3, "箱规")

    for row in range(4, ws.max_row + 1):
        for col in range(1, 4):
            cell = ws.cell(row, col)
            if not isinstance(cell, MergedCell):
                cell.value = None
    for sku in skus:
        row = int(sku["row"])
        ws.cell(row, 1, sku.get("sku_code"))
        ws.cell(row, 2, sku.get("sku_name"))

    rebuilt: list[dict[str, Any]] = []
    for idx, warehouse in enumerate(warehouses):
        start_col = 4 + idx * DASHBOARD_GROUP_WIDTH
        end_col = start_col + DASHBOARD_GROUP_WIDTH - 1
        ws.merge_cells(start_row=1, start_column=start_col, end_row=1, end_column=end_col)
        ws.merge_cells(start_row=2, start_column=start_col, end_row=2, end_column=end_col)
        ws.cell(1, start_col, warehouse["warehouse_name"])
        ws.cell(2, start_col, warehouse.get("short_name") or warehouse["warehouse_name"])
        for offset, metric in enumerate(DASHBOARD_METRICS):
            ws.cell(3, start_col + offset, metric)
        updated = dict(warehouse)
        updated["column"] = start_col
        rebuilt.append(updated)

    if ws.max_column > target_max_col:
        ws.delete_cols(target_max_col + 1, ws.max_column - target_max_col)

    return rebuilt


def set_cached_dashboard_values(
    ws,
    warehouses: list[dict[str, Any]],
    skus: list[dict[str, Any]],
    inventory_rows: list[dict[str, Any]],
    sales_rows: list[dict[str, Any]],
    transit_rows: list[dict[str, Any]],
    box_specs: dict[str, float],
) -> dict[str, Any]:
    inventory_qty: dict[tuple[str, str], float] = defaultdict(float)
    for row in inventory_rows:
        inventory_qty[
            (
                str(row.get("warehouse_name") or ""),
                str(row.get("report_sku_code") or row.get("sku_code") or ""),
            )
        ] += to_number(row.get("recent_stock_qty"))

    sales_qty: dict[tuple[str, str], float] = defaultdict(float)
    for row in sales_rows:
        sales_qty[
            (
                str(row.get("warehouse_name") or ""),
                str(row.get("report_sku_code") or row.get("sku_code") or ""),
            )
        ] += to_number(row.get("total_ship_qty"))

    transit_qty: dict[tuple[str, str], float] = defaultdict(float)
    for row in transit_rows:
        transit_qty[
            (
                str(row.get("warehouse_name") or ""),
                str(row.get("report_sku_code") or row.get("sku_code") or ""),
            )
        ] += to_number(row.get("transit_qty"))

    nonzero_inventory = 0
    nonzero_transit = 0
    nonzero_sales = 0
    for sku in skus:
        row_idx = int(sku["row"])
        sku_code = str(sku["sku_code"])
        box_spec = box_specs.get(sku_code, 0)
        ws.cell(row_idx, 3, excel_number(box_spec) if box_spec else None)
        for warehouse in warehouses:
            col = int(warehouse["column"])
            warehouse_name = str(warehouse["warehouse_name"])
            stock_box = ceil_box_qty(inventory_qty.get((warehouse_name, sku_code), 0.0), box_spec)
            transit_box = ceil_box_qty(transit_qty.get((warehouse_name, sku_code), 0.0), box_spec)
            sales_7d_box = ceil_box_qty(sales_qty.get((warehouse_name, sku_code), 0.0), box_spec)
            avg_sales_box = math.ceil(sales_7d_box / 7) if sales_7d_box else 0
            if stock_box:
                nonzero_inventory += 1
            if transit_box:
                nonzero_transit += 1
            if avg_sales_box:
                nonzero_sales += 1
            ws.cell(row_idx, col, stock_box)
            ws.cell(row_idx, col + 1, transit_box)
            ws.cell(row_idx, col + 2, avg_sales_box)
            ws.cell(row_idx, col + 3, math.ceil(stock_box / avg_sales_box) if avg_sales_box else None)
    return {
        "nonzero_inventory_cells": nonzero_inventory,
        "nonzero_transit_cells": nonzero_transit,
        "nonzero_sales_cells": nonzero_sales,
    }


def format_dashboard_for_push(ws) -> None:
    font_name = "Microsoft YaHei"
    group_fill = PatternFill(fill_type="solid", fgColor="D9EAD3")
    city_fill = PatternFill(fill_type="solid", fgColor="EAF4E8")
    stock_fill = PatternFill(fill_type="solid", fgColor="E8F3E6")
    transit_fill = PatternFill(fill_type="solid", fgColor="F0EAF8")
    sales_fill = PatternFill(fill_type="solid", fgColor="E8F0FA")
    days_fill = PatternFill(fill_type="solid", fgColor="FFF1DC")
    odd_row_fill = PatternFill(fill_type="solid", fgColor="FFFFFF")
    even_row_fill = PatternFill(fill_type="solid", fgColor="F8FBF6")
    soft_red_fill = PatternFill(fill_type="solid", fgColor="F9DADA")
    low_days_font = Font(name=font_name, size=9, color="8F2F2F", bold=True)
    thin_side = Side(style="thin", color="D7E2D3")
    header_side = Side(style="thin", color="AFC9AA")
    body_border = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)
    header_border = Border(left=header_side, right=header_side, top=header_side, bottom=header_side)

    ws.auto_filter.ref = None
    ws.freeze_panes = "D4"
    ws.sheet_view.showGridLines = False
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0

    ws.column_dimensions["A"].width = 21
    ws.column_dimensions["A"].hidden = True
    ws.column_dimensions["B"].width = 38
    ws.column_dimensions["B"].hidden = False
    ws.column_dimensions["C"].width = 7
    ws.column_dimensions["C"].hidden = True
    for row in range(1, ws.max_row + 1):
        ws.row_dimensions[row].height = 17.25
    for col in range(4, ws.max_column + 1, DASHBOARD_GROUP_WIDTH):
        ws.column_dimensions[get_column_letter(col)].width = 8.8
        ws.column_dimensions[get_column_letter(col + 1)].width = 8.8
        ws.column_dimensions[get_column_letter(col + 2)].width = 9.2
        ws.column_dimensions[get_column_letter(col + 3)].width = 9.2

    for row in range(1, ws.max_row + 1):
        for col in range(1, ws.max_column + 1):
            cell = ws.cell(row, col)
            current = cell.alignment
            cell.alignment = Alignment(
                horizontal=current.horizontal,
                vertical=current.vertical,
                text_rotation=current.text_rotation,
                wrap_text=False,
                shrink_to_fit=current.shrink_to_fit,
                indent=current.indent,
                relativeIndent=current.relativeIndent,
                justifyLastLine=current.justifyLastLine,
                readingOrder=current.readingOrder,
            )
            cell.font = Font(name=font_name, size=9, color="1F2933")
            cell.border = body_border

    for col in range(1, ws.max_column + 1):
        for row in (1, 2, 3):
            cell = ws.cell(row, col)
            cell.font = Font(name=font_name, size=9, bold=True, color="111827")
            cell.border = header_border
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=False, shrink_to_fit=True)
        ws.cell(1, col).fill = group_fill
        ws.cell(2, col).fill = city_fill

    for col in range(1, 4):
        for row in (1, 2, 3):
            ws.cell(row, col).fill = group_fill if row < 3 else city_fill

    for col in range(4, ws.max_column + 1, DASHBOARD_GROUP_WIDTH):
        ws.cell(3, col).fill = stock_fill
        ws.cell(3, col + 1).fill = transit_fill
        ws.cell(3, col + 2).fill = sales_fill
        ws.cell(3, col + 3).fill = days_fill

    for row in range(4, ws.max_row + 1):
        row_fill = even_row_fill if row % 2 == 0 else odd_row_fill
        for col in range(1, ws.max_column + 1):
            cell = ws.cell(row, col)
            cell.fill = row_fill
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=False, shrink_to_fit=True)
        ws.cell(row, 1).alignment = Alignment(horizontal="left", vertical="center", wrap_text=False, shrink_to_fit=True)
        ws.cell(row, 2).alignment = Alignment(horizontal="left", vertical="center", wrap_text=False, shrink_to_fit=True)
        ws.cell(row, 3).number_format = "0"
        for col in range(4, ws.max_column + 1, DASHBOARD_GROUP_WIDTH):
            ws.cell(row, col).number_format = "#,##0"
            ws.cell(row, col + 1).number_format = "#,##0"
            ws.cell(row, col + 2).number_format = "#,##0"
            ws.cell(row, col + 3).number_format = "#,##0"

    for col in range(7, ws.max_column + 1, DASHBOARD_GROUP_WIDTH):
        for row in range(4, ws.max_row + 1):
            value = ws.cell(row, col).value
            if isinstance(value, (int, float)) and value < 10:
                cell = ws.cell(row, col)
                cell.fill = soft_red_fill
                cell.font = low_days_font


def render_dashboard_image(ws, image_path: Path, visible_product_rows: list[int]) -> Path:
    from PIL import Image, ImageDraw, ImageFont

    font_cache: dict[tuple[int, bool], Any] = {}
    font_paths = {
        False: [
            Path(r"C:\Windows\Fonts\msyh.ttc"),
            Path(r"C:\Windows\Fonts\simhei.ttf"),
            Path(r"C:\Windows\Fonts\arial.ttf"),
        ],
        True: [
            Path(r"C:\Windows\Fonts\msyhbd.ttc"),
            Path(r"C:\Windows\Fonts\msyh.ttc"),
            Path(r"C:\Windows\Fonts\simhei.ttf"),
            Path(r"C:\Windows\Fonts\arialbd.ttf"),
        ],
    }

    def load_font(size: int, bold: bool = False):
        key = (size, bold)
        if key in font_cache:
            return font_cache[key]
        for font_path in font_paths[bold]:
            if font_path.exists():
                try:
                    font_cache[key] = ImageFont.truetype(str(font_path), size)
                    return font_cache[key]
                except OSError:
                    continue
        font_cache[key] = ImageFont.load_default()
        return font_cache[key]

    def fill_rgb(cell, fallback: str = "FFFFFF") -> tuple[int, int, int]:
        color = None
        if cell.fill and cell.fill.fill_type == "solid":
            color = cell.fill.fgColor.rgb or cell.fill.start_color.rgb
        if not color or color == "00000000":
            color = fallback
        color = str(color).replace("#", "")[-6:]
        return tuple(int(color[i : i + 2], 16) for i in (0, 2, 4))

    def text_value(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if abs(value - round(value)) < 0.000001:
                return f"{int(round(value)):,}"
            return f"{value:,.1f}"
        return str(value)

    probe = Image.new("RGB", (10, 10), "white")
    probe_draw = ImageDraw.Draw(probe)
    product_font = load_font(14)
    product_width = 330
    for row_idx in visible_product_rows:
        text = text_value(ws.cell(row_idx, 2).value)
        if not text:
            continue
        bounds = probe_draw.textbbox((0, 0), text, font=product_font)
        product_width = max(product_width, min(520, bounds[2] - bounds[0] + 24))

    visible_cols = [2] + list(range(4, ws.max_column + 1))
    col_widths: dict[int, int] = {2: product_width}
    for col in range(4, ws.max_column + 1, DASHBOARD_GROUP_WIDTH):
        col_widths[col] = 66
        col_widths[col + 1] = 66
        col_widths[col + 2] = 68
        col_widths[col + 3] = 68

    row_heights = {1: 28, 2: 24, 3: 25}
    for row_idx in visible_product_rows:
        row_heights[row_idx] = 23

    visible_rows = [1, 2, 3, *visible_product_rows]

    margin = 14
    table_width = sum(col_widths[col] for col in visible_cols)
    table_height = sum(row_heights[row] for row in visible_rows)
    image = Image.new("RGB", (table_width + margin * 2, table_height + margin * 2), (247, 250, 246))
    draw = ImageDraw.Draw(image)
    grid = (192, 209, 187)
    text_color = (17, 24, 39)
    muted_text = (31, 41, 55)
    low_text = (143, 47, 47)

    x_by_col: dict[int, int] = {}
    x = margin
    for col in visible_cols:
        x_by_col[col] = x
        x += col_widths[col]
    y_by_row: dict[int, int] = {}
    y = margin
    for row in visible_rows:
        y_by_row[row] = y
        y += row_heights[row]

    def fit_font(text: str, width: int, height: int, start_size: int, bold: bool = False, min_size: int = 8):
        for size in range(start_size, min_size - 1, -1):
            font = load_font(size, bold)
            left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
            if right - left <= width - 8 and bottom - top <= height - 4:
                return font
        return load_font(min_size, bold)

    def draw_cell(
        row: int,
        col: int,
        width: int | None = None,
        height: int | None = None,
        value: Any | None = None,
        fill: tuple[int, int, int] | None = None,
        bold: bool = False,
        align: str = "center",
    ) -> None:
        cell = ws.cell(row, col)
        x0 = x_by_col[col]
        y0 = y_by_row[row]
        width = width or col_widths[col]
        height = height or row_heights[row]
        fill = fill or fill_rgb(cell)
        draw.rectangle((x0, y0, x0 + width, y0 + height), fill=fill, outline=grid)
        text = text_value(cell.value if value is None else value)
        if not text:
            return
        font = fit_font(text, width, height, 13 if row <= 3 else 14, bold=bold)
        color = low_text if fill == (249, 218, 218) else (text_color if row <= 3 else muted_text)
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        text_width = right - left
        text_height = bottom - top
        if align == "left":
            tx = x0 + 7
        else:
            tx = x0 + max(0, (width - text_width) / 2)
        ty = y0 + max(0, (height - text_height) / 2) - 1
        draw.text((tx, ty), text, font=font, fill=color)

    product_header_height = row_heights[1] + row_heights[2] + row_heights[3]
    draw_cell(1, 2, height=product_header_height, value=ws.cell(3, 2).value, fill=fill_rgb(ws.cell(3, 2), "EAF4E8"), bold=True)

    for col in range(4, ws.max_column + 1, DASHBOARD_GROUP_WIDTH):
        group_width = sum(col_widths[col + offset] for offset in range(DASHBOARD_GROUP_WIDTH))
        draw_cell(1, col, width=group_width, value=ws.cell(1, col).value, fill=fill_rgb(ws.cell(1, col), "D9EAD3"), bold=True)
        draw_cell(2, col, width=group_width, value=ws.cell(2, col).value, fill=fill_rgb(ws.cell(2, col), "EAF4E8"), bold=True)
        for offset in range(DASHBOARD_GROUP_WIDTH):
            draw_cell(3, col + offset, bold=True)

    for row in visible_product_rows:
        draw_cell(row, 2, align="left")
        for col in range(4, ws.max_column + 1):
            draw_cell(row, col)

    image_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(image_path, optimize=True)
    return image_path


def collect_inventory_alerts(
    ws,
    warehouses: list[dict[str, Any]],
    skus: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    for sku in skus:
        if is_screenshot_excluded_sku(sku):
            continue
        row_idx = int(sku["row"])
        sku_code = str(sku["sku_code"])
        product_name = str(ws.cell(row_idx, 2).value or sku.get("sku_name") or sku.get("sku_code") or "").strip()
        for warehouse in warehouses:
            col = int(warehouse["column"])
            warehouse_name = str(warehouse.get("warehouse_name") or "")
            stock_box = max(0, math.ceil(to_number(ws.cell(row_idx, col).value)))
            transit_box = max(0, math.ceil(to_number(ws.cell(row_idx, col + 1).value)))
            avg_sales_box = max(0, math.ceil(to_number(ws.cell(row_idx, col + 2).value)))
            if avg_sales_box <= 0:
                continue
            available_days = math.ceil(to_number(ws.cell(row_idx, col + 3).value))
            if available_days >= ALERT_DAYS_THRESHOLD:
                continue
            days_with_transit = math.ceil((stock_box + transit_box) / avg_sales_box)
            priority = "P0 立即处理" if available_days <= 1 else "P1 今日处理"
            if stock_box == 0:
                action = "核实缺货原因及补货/调拨"
            elif transit_box > 0 and days_with_transit >= ALERT_DAYS_THRESHOLD:
                action = "跟进在途到货时间"
            elif transit_box > 0:
                action = "确认在途并补充补货/调拨"
            else:
                action = "确认补货或调拨安排"
            alerts.append(
                {
                    "product_name": product_name,
                    "warehouse": str(warehouse.get("short_name") or warehouse.get("warehouse_name") or "").strip(),
                    "stock_box": stock_box,
                    "available_cases": stock_box,
                    "available_days": available_days,
                    "transit_box": transit_box,
                    "transit_cases": transit_box,
                    "days_with_transit": days_with_transit,
                    "priority": priority,
                    "action": action,
                }
            )
    return sorted(
        alerts,
        key=lambda item: (
            int(item["available_days"]),
            str(item["warehouse"]),
            str(item["product_name"]),
        ),
    )


def group_inventory_alerts(alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in alerts:
        grouped[str(item["warehouse"])].append(item)

    warehouse_groups: list[dict[str, Any]] = []
    for warehouse, items in grouped.items():
        sorted_items = sorted(
            items,
            key=lambda item: (
                int(item["available_days"]),
                str(item["product_name"]),
            ),
        )
        warehouse_groups.append(
            {
                "warehouse": warehouse,
                "items": sorted_items,
                "alert_count": len(sorted_items),
                "p0_count": sum(1 for item in sorted_items if str(item["priority"]).startswith("P0")),
                "minimum_days": min(int(item["available_days"]) for item in sorted_items),
            }
        )

    return sorted(
        warehouse_groups,
        key=lambda group: (
            -int(group["alert_count"]),
            -int(group["p0_count"]),
            int(group["minimum_days"]),
            str(group["warehouse"]),
        ),
    )


def render_inventory_alert_image(alerts: list[dict[str, Any]], image_path: Path) -> Path:
    from PIL import Image, ImageDraw, ImageFont

    font_cache: dict[tuple[int, bool], Any] = {}
    font_paths = {
        False: [
            Path(r"C:\Windows\Fonts\msyh.ttc"),
            Path(r"C:\Windows\Fonts\simhei.ttf"),
            Path(r"C:\Windows\Fonts\arial.ttf"),
        ],
        True: [
            Path(r"C:\Windows\Fonts\msyhbd.ttc"),
            Path(r"C:\Windows\Fonts\msyh.ttc"),
            Path(r"C:\Windows\Fonts\simhei.ttf"),
            Path(r"C:\Windows\Fonts\arialbd.ttf"),
        ],
    }

    def load_font(size: int, bold: bool = False):
        key = (size, bold)
        if key in font_cache:
            return font_cache[key]
        for font_path in font_paths[bold]:
            if font_path.exists():
                try:
                    font_cache[key] = ImageFont.truetype(str(font_path), size)
                    return font_cache[key]
                except OSError:
                    continue
        font_cache[key] = ImageFont.load_default()
        return font_cache[key]

    def text_size(draw, value: str, text_font) -> tuple[int, int]:
        left, top, right, bottom = draw.textbbox((0, 0), value, font=text_font)
        return right - left, bottom - top

    def fit_font(draw, value: str, width: int, start_size: int, bold: bool = False, min_size: int = 12):
        for size in range(start_size, min_size - 1, -1):
            text_font = load_font(size, bold)
            text_width, _ = text_size(draw, value, text_font)
            if text_width <= width - 18:
                return text_font
        return load_font(min_size, bold)

    def center_text(draw, box: tuple[int, int, int, int], value: str, text_font, fill: str) -> None:
        text_width, text_height = text_size(draw, value, text_font)
        left, top, right, bottom = box
        draw.text(
            (left + (right - left - text_width) / 2, top + (bottom - top - text_height) / 2 - 2),
            value,
            font=text_font,
            fill=fill,
        )

    groups = group_inventory_alerts(alerts)
    width = 2000
    margin = 50
    title_height = 142
    header_height = 60
    line_height = 36
    group_padding = 18
    row_heights = [max(72, group_padding * 2 + line_height * len(group["items"])) for group in groups]
    footer_height = 86
    height = title_height + header_height + sum(row_heights) + footer_height
    image = Image.new("RGB", (width, height), "#FFFFFF")
    draw = ImageDraw.Draw(image)

    draw.text((margin, 32), "零食很忙库存预警明细", font=load_font(36, True), fill="#111111")
    draw.text(
        (margin, 89),
        f"{TODAY.isoformat()}  |  仅提醒可用天数低于{ALERT_DAYS_THRESHOLD}天",
        font=load_font(20),
        fill="#666666",
    )
    count_text = "仓库按预警产品数由多到少排序  |  件数按箱规折算并向上取整"
    count_font = load_font(18)
    count_width, _ = text_size(draw, count_text, count_font)
    draw.text((width - margin - count_width, 91), count_text, font=count_font, fill="#666666")
    draw.line((margin, 126, width - margin, 126), fill="#222222", width=2)

    columns = [
        ("仓库", 125),
        ("预警产品", 500),
        ("可用件数(箱)", 145),
        ("在途件数(箱)", 135),
        ("可用天数", 125),
        ("含在途天数", 145),
        ("处理优先级", 170),
        ("建议动作", 555),
    ]
    table_left = margin
    table_top = title_height
    table_right = table_left + sum(col_width for _, col_width in columns)
    table_bottom = table_top + header_height + sum(row_heights)

    x = table_left
    for label, col_width in columns:
        box = (x, table_top, x + col_width, table_top + header_height)
        draw.rectangle(box, fill="#F2F2F2")
        center_text(draw, box, label, load_font(18, True), "#222222")
        x += col_width
    draw.line((table_left, table_top, table_right, table_top), fill="#222222", width=2)
    draw.line((table_left, table_top + header_height, table_right, table_top + header_height), fill="#777777", width=1)

    fields = [
        "warehouse",
        "product_name",
        "available_cases",
        "transit_cases",
        "available_days",
        "days_with_transit",
        "priority",
        "action",
    ]
    y = table_top + header_height
    for group, row_height in zip(groups, row_heights):
        x = table_left
        items = list(group["items"])
        column_values = {
            "warehouse": [str(group["warehouse"])],
            "product_name": [str(item["product_name"]) for item in items],
            "available_cases": [f"{int(item['available_cases']):,}" for item in items],
            "transit_cases": [f"{int(item['transit_cases']):,}" for item in items],
            "available_days": [f"{int(item['available_days'])}天" for item in items],
            "days_with_transit": [f"{int(item['days_with_transit'])}天" for item in items],
            "priority": [str(item["priority"]) for item in items],
            "action": [str(item["action"]) for item in items],
        }
        for col_index, ((_, col_width), field) in enumerate(zip(columns, fields)):
            cell = (x, y, x + col_width, y + row_height)
            values = column_values[field]
            if field == "warehouse":
                center_text(draw, cell, values[0], load_font(19, True), "#222222")
            else:
                for line_index, value in enumerate(values):
                    line_top = y + group_padding + line_index * line_height
                    line_box = (x, line_top, x + col_width, line_top + line_height)
                    if field in ("product_name", "action"):
                        text_font = fit_font(draw, value, col_width, 16, min_size=11)
                        _, text_height = text_size(draw, value, text_font)
                        draw.text(
                            (x + 12, line_top + (line_height - text_height) / 2 - 2),
                            value,
                            font=text_font,
                            fill="#222222",
                        )
                    elif field == "available_days":
                        center_text(draw, line_box, value, load_font(18, True), "#B42318")
                    elif field == "priority":
                        color = "#B42318" if value.startswith("P0") else "#8A5B00"
                        center_text(draw, line_box, value, load_font(15, True), color)
                    elif field in ("available_cases", "transit_cases"):
                        center_text(draw, line_box, value, load_font(17), "#222222")
                    else:
                        center_text(draw, line_box, value, load_font(16), "#444444")
            x += col_width
        draw.line((table_left, y + row_height, table_right, y + row_height), fill="#D8D8D8", width=1)
        y += row_height

    x = table_left
    draw.line((table_left, table_top, table_left, table_bottom), fill="#D0D0D0", width=1)
    for _, col_width in columns:
        x += col_width
        draw.line((x, table_top, x, table_bottom), fill="#D0D0D0", width=1)

    footer_top = table_bottom + 30
    draw.line((margin, footer_top, width - margin, footer_top), fill="#222222", width=1)
    draw.text(
        (margin, footer_top + 24),
        "说明：可用件数/在途件数=原始EA数量÷master_sku_unit箱规，并向上取整；P0为0-1天，P1为2-4天。",
        font=load_font(17),
        fill="#555555",
    )

    image_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(image_path, optimize=True)
    return image_path


def add_audit_sheet(wb, audit: dict[str, Any]) -> None:
    name = "刷新审计"
    if name in wb.sheetnames:
        del wb[name]
    ws = wb.create_sheet(name)
    rows = [
        ["项目", "值"],
        ["生成时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
        ["库存口径", f"非盒类生产日期距今天 {RECENT_PRODUCTION_DAYS} 天以内；SKU名称含盒不限制生产日期"],
        ["库存来源", "ywzt_ods.scm_warehous_manage 实时库存"],
        ["在途口径", f"非盒类采购/调拨在途生产日期距今天 {RECENT_PRODUCTION_DAYS} 天以内；SKU名称含盒不限制生产日期"],
        ["在途来源", "采购 scm_po_detail.produc_date；调拨 scm_sales_shipment_allocate_wms.production_date"],
        ["箱规来源", audit.get("box_spec_source", "")],
        ["看板箱规换算", "看板库存/在途/7日均销均按箱规换算并向上取整"],
        ["底表数量口径", "库存底表保留源数量并向上取整；动销底表按箱规换算"],
        ["缺失箱规SKU数", audit.get("box_spec_missing_count", 0)],
        ["动销口径", f"{audit['sales_start']} 至 {audit['sales_end_inclusive']} 近 7 个完整发货日"],
        ["货主", f"{CARGO_OWNER_ID}; {FDS_CARGO_OWNER_ID}(肥大圣SKU)"],
        ["模板文件", audit["template"]],
        ["输出文件", audit["output"]],
        ["看板截图", audit.get("dashboard_image", "")],
        ["预警口径", f"可用库存天数低于 {ALERT_DAYS_THRESHOLD} 天"],
        ["预警截图", audit.get("alert_image", "")],
        ["预警项目数", audit.get("alert_count", 0)],
        ["预警件数口径", "可用/在途原始EA数量除以master_sku_unit中unit=40的ea_convert_quantity，并向上取整"],
        ["仓库数", audit["warehouse_count"]],
        ["匹配仓库数", audit["matched_warehouse_count"]],
        ["看板SKU数", audit["sku_count"]],
        ["SKU仓库路由数", audit.get("sku_route_count", 0)],
        ["库存底表行数", audit["inventory_row_count"]],
        ["在途底表行数", audit.get("transit_row_count", 0)],
        ["在途数量合计", audit.get("transit_qty_total", 0)],
        ["采购在途合计", audit.get("po_transit_qty_total", 0)],
        ["调拨在途合计", audit.get("transfer_transit_qty_total", 0)],
        ["动销底表行数", audit["sales_row_count"]],
        ["库存快照日", audit.get("stock_snapshot_date") or ""],
        ["生产日期下限", audit["production_date_threshold"]],
        ["看板非零库存格", audit["dashboard"].get("nonzero_inventory_cells", 0)],
        ["看板非零在途格", audit["dashboard"].get("nonzero_transit_cells", 0)],
        ["看板非零动销格", audit["dashboard"].get("nonzero_sales_cells", 0)],
    ]
    for row_idx, row_values in enumerate(rows, start=1):
        for col_idx, value in enumerate(row_values, start=1):
            ws.cell(row_idx, col_idx, value)

    start = len(rows) + 3
    ws.cell(start, 1, "请求仓库")
    ws.cell(start, 2, "匹配编码")
    ws.cell(start, 3, "匹配仓库")
    ws.cell(start, 4, "是否匹配")
    for offset, row in enumerate(audit["warehouse_matches"], start=1):
        ws.cell(start + offset, 1, row.get("requested_name"))
        ws.cell(start + offset, 2, row.get("wms_code"))
        ws.cell(start + offset, 3, row.get("wms_desc"))
        ws.cell(start + offset, 4, row.get("matched"))

    route_start = start + len(audit["warehouse_matches"]) + 3
    ws.cell(route_start, 1, "特殊SKU")
    ws.cell(route_start, 2, "看板仓")
    ws.cell(route_start, 3, "真实货主")
    ws.cell(route_start, 4, "真实仓库")
    special_routes = [
        row for row in audit.get("sku_route_audit", []) if str(row.get("sku_code")) in FDS_SKU_CODES
    ]
    for offset, row in enumerate(special_routes, start=1):
        ws.cell(route_start + offset, 1, row.get("sku_code"))
        ws.cell(route_start + offset, 2, row.get("report_warehouse"))
        ws.cell(route_start + offset, 3, row.get("cargo_owner_id"))
        ws.cell(route_start + offset, 4, row.get("source_wms_desc") or row.get("route_type"))

    box_start = route_start + max(len(special_routes), 1) + 3
    ws.cell(box_start, 1, "箱规SKU")
    ws.cell(box_start, 2, "SKU名称")
    ws.cell(box_start, 3, "箱规")
    ws.cell(box_start, 4, "是否匹配")
    for offset, row in enumerate(audit.get("box_spec_audit", []), start=1):
        ws.cell(box_start + offset, 1, row.get("sku_code"))
        ws.cell(box_start + offset, 2, row.get("sku_name"))
        ws.cell(box_start + offset, 3, row.get("box_spec"))
        ws.cell(box_start + offset, 4, row.get("matched"))

    for col in range(1, 5):
        ws.column_dimensions[get_column_letter(col)].width = 28


def build_dashboard(args: argparse.Namespace) -> tuple[Path, dict[str, Any]]:
    load_env_file(args.env_file)
    warehouses, template_skus = read_template_scope(args.template)
    excluded_report_skus = [sku for sku in template_skus if is_report_excluded_sku(sku)]
    skus = [
        {**sku, "row": row}
        for row, sku in enumerate(
            (sku for sku in template_skus if not is_report_excluded_sku(sku)),
            start=4,
        )
    ]
    warehouse_names = [row["warehouse_name"] for row in warehouses]
    sku_codes = [row["sku_code"] for row in skus]

    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"零食很忙看板_生产日期{RECENT_PRODUCTION_DAYS}天内库存_{TODAY.isoformat()}.xlsx"
    shutil.copy2(args.template, output)

    with connect() as connection:
        warehouse_map, warehouse_audit = match_warehouses(connection, warehouse_names)
        sku_routes, sku_route_audit = build_sku_routes(warehouses, skus, warehouse_map)
        box_specs, box_spec_audit = fetch_box_specs(connection, sku_codes)

        inventory_rows, snapshot = fetch_recent_inventory(connection, sku_routes)
        transit_rows = fetch_recent_transit(connection, sku_routes)
        sales_start = TODAY - timedelta(days=7)
        sales_end = TODAY
        sales_rows = fetch_sales(connection, sku_routes, sales_start, sales_end)

    sales_lookup = build_sales_lookup(sales_rows)
    transit_lookup = build_transit_lookup(transit_rows)
    inventory_detail_rows = merge_inventory_and_transit_rows(inventory_rows, transit_rows)
    inventory_excel_rows = [
        inventory_excel_row(row, sales_lookup, transit_lookup, box_specs)
        for row in sorted(
            inventory_detail_rows,
            key=lambda item: (
                str(item.get("warehouse_name") or ""),
                str(item.get("sku_code") or ""),
            ),
        )
    ]
    sales_excel_rows = [
        sales_excel_row(row, box_specs)
        for row in sorted(
            sales_rows,
            key=lambda item: (
                str(item.get("ship_date") or ""),
                str(item.get("warehouse_name") or ""),
                str(item.get("sku_code") or ""),
            ),
        )
    ]

    wb = openpyxl.load_workbook(output)
    ws_sales = wb["动销"]
    ws_inventory = wb["库存"]
    write_rows(ws_sales, SALES_HEADERS, sales_excel_rows, date_columns={1})
    write_rows(ws_inventory, INVENTORY_HEADERS, inventory_excel_rows, date_columns={22, 23})

    ws_dashboard = wb["看板"]
    warehouses = rebuild_dashboard_layout(ws_dashboard, warehouses, skus)
    dashboard_summary = set_cached_dashboard_values(
        ws_dashboard,
        warehouses,
        skus,
        inventory_rows,
        sales_rows,
        transit_rows,
        box_specs,
    )
    format_dashboard_for_push(ws_dashboard)
    screenshot_skus = [sku for sku in skus if not is_screenshot_excluded_sku(sku)]
    screenshot_product_rows = [int(sku["row"]) for sku in screenshot_skus]
    dashboard_image_path = output.with_suffix(".png")
    render_dashboard_image(ws_dashboard, dashboard_image_path, screenshot_product_rows)
    alerts = collect_inventory_alerts(ws_dashboard, warehouses, screenshot_skus)
    alert_image_path = output.with_name(f"{output.stem}_预警.png")
    if alerts:
        render_inventory_alert_image(alerts, alert_image_path)
    elif alert_image_path.exists():
        alert_image_path.unlink()

    # Keep formulas available for manual refresh by writing an adjacent audit sheet only; cached dashboard values are
    # intentional because openpyxl cannot evaluate formulas and many WPS setups open workbooks without recalculation.
    wb.calculation.fullCalcOnLoad = True
    wb.calculation.forceFullCalc = True

    stock_snapshot = snapshot.get("snapshot_date")
    audit = {
        "template": str(args.template),
        "output": str(output),
        "dashboard_image": str(dashboard_image_path),
        "alert_image": str(alert_image_path) if alerts else "",
        "alert_count": len(alerts),
        "alerts": alerts,
        "warehouse_count": len(warehouses),
        "matched_warehouse_count": sum(1 for row in warehouse_audit if row.get("matched")),
        "sku_count": len(skus),
        "report_excluded_skus": [
            {"sku_code": sku.get("sku_code"), "sku_name": sku.get("sku_name")}
            for sku in excluded_report_skus
        ],
        "screenshot_sku_count": len(screenshot_skus),
        "screenshot_excluded_skus": [
            {"sku_code": sku.get("sku_code"), "sku_name": sku.get("sku_name")}
            for sku in skus
            if is_screenshot_excluded_sku(sku)
        ],
        "box_spec_source": "ywzt_ods.master_sku_unit, unit=40, ea_convert_quantity",
        "box_spec_missing_count": sum(1 for row in box_spec_audit if not row.get("matched")),
        "box_spec_audit": box_spec_audit,
        "inventory_row_count": len(inventory_excel_rows),
        "transit_row_count": len(transit_rows),
        "transit_qty_total": excel_number(sum(to_number(row.get("transit_qty")) for row in transit_rows)),
        "po_transit_qty_total": excel_number(sum(to_number(row.get("po_transit_qty")) for row in transit_rows)),
        "transfer_transit_qty_total": excel_number(
            sum(to_number(row.get("transfer_transit_qty")) for row in transit_rows)
        ),
        "sales_row_count": len(sales_excel_rows),
        "stock_snapshot_date": stock_snapshot.isoformat() if hasattr(stock_snapshot, "isoformat") else stock_snapshot,
        "production_date_threshold": (TODAY - timedelta(days=RECENT_PRODUCTION_DAYS)).isoformat(),
        "sales_start": sales_start.isoformat(),
        "sales_end_inclusive": (sales_end - timedelta(days=1)).isoformat(),
        "warehouse_matches": warehouse_audit,
        "sku_route_count": len(sku_routes),
        "sku_route_audit": sku_route_audit,
        "dashboard": dashboard_summary,
    }
    add_audit_sheet(wb, audit)
    wb.save(output)

    audit_path = output.with_suffix(".audit.json")
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return output, audit


def wecom_post(webhook: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        webhook,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            text = response.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"WeCom webhook request failed: {exc}") from exc
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"WeCom webhook returned non-JSON response: {text[:200]}") from exc
    if result.get("errcode") not in (0, None):
        raise RuntimeError(f"WeCom webhook error: {result}")
    return result


def wecom_upload_file(webhook: str, file_path: Path) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(webhook)
    query = urllib.parse.parse_qs(parsed.query)
    key_values = query.get("key", [])
    if not key_values or not key_values[0]:
        raise ValueError("WeCom webhook URL missing key parameter")
    upload_url = f"{parsed.scheme}://{parsed.netloc}/cgi-bin/webhook/upload_media?key={urllib.parse.quote(key_values[0])}&type=file"

    boundary = "----CodexLingShiDashboardBoundary"
    filename = file_path.name
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    file_bytes = file_path.read_bytes()
    body = b"".join(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            f'Content-Disposition: form-data; name="media"; filename="{filename}"\r\n'.encode("utf-8"),
            f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"),
            file_bytes,
            b"\r\n",
            f"--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    request = urllib.request.Request(
        upload_url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            text = response.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"WeCom file upload failed: {exc}") from exc
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"WeCom upload returned non-JSON response: {text[:200]}") from exc
    if result.get("errcode") not in (0, None):
        raise RuntimeError(f"WeCom upload error: {result}")
    if not result.get("media_id"):
        raise RuntimeError(f"WeCom upload missing media_id: {result}")
    return result


def wecom_send_image(webhook: str, image_path: Path) -> dict[str, Any]:
    image_bytes = image_path.read_bytes()
    payload = {
        "msgtype": "image",
        "image": {
            "base64": base64.b64encode(image_bytes).decode("ascii"),
            "md5": hashlib.md5(image_bytes).hexdigest(),
        },
    }
    return wecom_post(webhook, payload)


def send_wecom_dashboard(webhook: str, output: Path, audit: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    alert_image_value = str(audit.get("alert_image") or "").strip()
    if alert_image_value:
        alert_image = Path(alert_image_value)
        if alert_image.exists():
            results.append(wecom_send_image(webhook, alert_image))
    dashboard_image = Path(str(audit.get("dashboard_image") or output.with_suffix(".png")))
    if dashboard_image.exists():
        results.append(wecom_send_image(webhook, dashboard_image))
    upload_result = wecom_upload_file(webhook, output)
    media_id = upload_result["media_id"]
    results.append(upload_result)
    results.append(wecom_post(webhook, {"msgtype": "file", "file": {"media_id": media_id}}))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="生成零食很忙看板：生产日期 75 天内库存 + 7 日均销")
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--push-env-file", type=Path, default=DEFAULT_PUSH_ENV_FILE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--run-date", type=str, default=None, help="运行日期，格式 YYYY-MM-DD；默认使用服务器当天日期")
    parser.add_argument("--send", action="store_true", help="生成后推送到企业微信机器人")
    parser.add_argument("--webhook", default=None, help="企业微信机器人 Webhook；默认读取 WECOM_WEBHOOK_URL")
    args = parser.parse_args()

    global TODAY
    if args.run_date:
        TODAY = datetime.strptime(args.run_date, "%Y-%m-%d").date()

    output, audit = build_dashboard(args)
    sent_to_wecom = False
    wecom_results: list[dict[str, Any]] = []
    if args.send:
        load_optional_env_file(args.push_env_file)
        webhook = args.webhook or os.environ.get("WECOM_WEBHOOK_URL", "").strip()
        if not webhook:
            raise SystemExit("missing WECOM_WEBHOOK_URL in config/push.env or --webhook")
        wecom_results = send_wecom_dashboard(webhook, output, audit)
        sent_to_wecom = True
    print(
        json.dumps(
            {
                "ok": True,
                "output": str(output),
                "inventory_row_count": audit["inventory_row_count"],
                "transit_row_count": audit["transit_row_count"],
                "transit_qty_total": audit["transit_qty_total"],
                "sales_row_count": audit["sales_row_count"],
                "matched_warehouse_count": audit["matched_warehouse_count"],
                "sku_count": audit["sku_count"],
                "stock_snapshot_date": audit["stock_snapshot_date"],
                "sales_start": audit["sales_start"],
                "sales_end_inclusive": audit["sales_end_inclusive"],
                "dashboard_image": audit.get("dashboard_image"),
                "alert_image": audit.get("alert_image"),
                "alert_count": audit.get("alert_count", 0),
                "sent_to_wecom": sent_to_wecom,
                "wecom_results": wecom_results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

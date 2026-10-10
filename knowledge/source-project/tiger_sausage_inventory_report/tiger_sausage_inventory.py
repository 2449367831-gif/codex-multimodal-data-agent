from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import pymysql


APP_VERSION = "2026.08.12"
DEFAULT_SKU_CODES = [
    "5001010101200700",  # 原味 700g，20 袋/箱
    "5001010101213500",  # 原味 3.5kg，4 袋/箱
    "5001010102200700",  # 黑椒味 700g，20 袋/箱
    "5001010102213500",  # 黑椒味 3.5kg，4 袋/箱
]


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ROOT = app_root()


class Config:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.values = self._read(self.path)
        pickup_path = self.path.with_name("pickup.env")
        self.pickup_values = self._read(pickup_path) if pickup_path.exists() else {}

    @staticmethod
    def _read(path: Path) -> dict[str, str]:
        if not path.exists():
            raise FileNotFoundError(f"配置文件不存在：{path}")
        values: dict[str, str] = {}
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
        return values

    def get(self, key: str, default: str = "") -> str:
        environment_value = os.environ.get(key)
        if environment_value is not None:
            return environment_value.strip()
        value = self.values.get(key, "").strip()
        if value and value not in {"请填写", "changeme"}:
            return value
        pickup_aliases = {
            "DB_HOST": "PICKUP_DB_HOST",
            "DB_PORT": "PICKUP_DB_PORT",
            "DB_NAME": "PICKUP_DB_NAME",
            "DB_USER": "PICKUP_DB_USER",
            "DB_PASSWORD": "PICKUP_DB_PASSWORD",
        }
        alias = pickup_aliases.get(key)
        if alias:
            fallback = self.pickup_values.get(alias, "").strip()
            if fallback:
                return fallback
        return value or default.strip()

    def get_bool(self, key: str, default: bool = False) -> bool:
        value = self.get(key, "true" if default else "false").lower()
        if value in {"1", "true", "yes", "y", "on"}:
            return True
        if value in {"0", "false", "no", "n", "off"}:
            return False
        raise ValueError(f"{key} 必须是 true 或 false，当前值：{value}")

    def get_int(self, key: str, default: int) -> int:
        return int(self.get(key, str(default)))

    def split(self, key: str, separator: str, default: str = "") -> list[str]:
        return [item.strip() for item in self.get(key, default).split(separator) if item.strip()]

    def resolve_path(self, key: str, default: str) -> Path:
        path = Path(self.get(key, default))
        if path.is_absolute():
            return path
        return (ROOT / path).resolve()


def setup_logging(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("tiger_sausage_inventory")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    file_handler = logging.FileHandler(
        log_dir / "tiger_sausage_inventory.log", encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger


def required_config(config: Config) -> None:
    missing = []
    for key in ["DB_HOST", "DB_USER", "DB_PASSWORD"]:
        value = config.get(key)
        if not value or value in {"请填写", "changeme"}:
            missing.append(key)
    if missing:
        raise ValueError("以下数据库配置未填写：" + ", ".join(missing))


def connect(config: Config) -> pymysql.connections.Connection:
    return pymysql.connect(
        host=config.get("DB_HOST"),
        port=config.get_int("DB_PORT", 9030),
        user=config.get("DB_USER"),
        password=config.get("DB_PASSWORD"),
        database=config.get("DB_NAME", "ywzt_ods"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=config.get_int("DB_CONNECT_TIMEOUT_SECONDS", 20),
        read_timeout=config.get_int("DB_READ_TIMEOUT_SECONDS", 180),
        write_timeout=60,
        autocommit=True,
    )


def sku_codes(config: Config) -> list[str]:
    configured = config.split("TARGET_SKU_CODES", ",")
    codes = configured or DEFAULT_SKU_CODES
    if len(codes) != len(set(codes)):
        raise ValueError("TARGET_SKU_CODES 存在重复 SKU")
    return codes


def sku_label_overrides(config: Config) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in config.split("SKU_LABELS", "|"):
        if ":" not in item:
            raise ValueError(f"SKU_LABELS 格式错误：{item}")
        code, label = item.split(":", 1)
        result[code.strip()] = label.strip()
    return result


def infer_sku_label(sku_name: str) -> str:
    if "黑椒" in sku_name:
        flavor = "黑椒"
    elif "原味" in sku_name:
        flavor = "原味"
    else:
        flavor = sku_name

    upper = sku_name.upper().replace(" ", "")
    if "3500G" in upper or "3.5KG" in upper:
        spec = "3.5kg"
    elif "0700G" in upper or "700G" in upper:
        spec = "700g"
    else:
        spec = ""
    return f"{flavor}{spec}"


def fetch_products(
    connection: pymysql.connections.Connection,
    codes: list[str],
    label_overrides: dict[str, str],
) -> list[dict[str, Any]]:
    placeholders = ",".join(["%s"] * len(codes))
    sql = f"""
SELECT
    m.sku_code,
    MAX(m.sku_name) AS sku_name,
    MAX(m.sku_group) AS sku_group,
    MAX(CASE WHEN u.unit = 40 THEN u.ea_convert_quantity END) AS box_spec
FROM ywzt_ods.master_sku m
LEFT JOIN ywzt_ods.master_sku_unit u
    ON u.sku_code = m.sku_code
WHERE m.sku_code IN ({placeholders})
GROUP BY m.sku_code
ORDER BY FIELD(m.sku_code, {placeholders})
"""
    with connection.cursor() as cursor:
        cursor.execute(sql, tuple(codes + codes))
        rows = list(cursor.fetchall())

    by_code = {str(row["sku_code"]): row for row in rows}
    missing = [code for code in codes if code not in by_code]
    if missing:
        raise RuntimeError("SKU 未在 master_sku 找到：" + ", ".join(missing))

    products: list[dict[str, Any]] = []
    for code in codes:
        row = by_code[code]
        box_spec = to_decimal(row.get("box_spec"))
        if box_spec <= 0:
            raise RuntimeError(f"SKU {code} 未维护有效箱规（master_sku_unit.unit=40）")
        sku_name = str(row.get("sku_name") or code)
        products.append(
            {
                "sku_code": code,
                "sku_name": sku_name,
                "sku_group": str(row.get("sku_group") or ""),
                "box_spec": box_spec,
                "label": label_overrides.get(code) or infer_sku_label(sku_name),
            }
        )
    return products


def fetch_stock_rows(
    connection: pymysql.connections.Connection, codes: list[str]
) -> list[dict[str, Any]]:
    placeholders = ",".join(["%s"] * len(codes))
    sql = f"""
WITH inventory AS (
    SELECT
        wms_code,
        cargo_owner_id,
        sku_code,
        MAX(sku_name) AS source_sku_name,
        SUM(IFNULL(in_stock_quantity, 0)) AS in_stock_quantity,
        SUM(IFNULL(CAST(NULLIF(stock_lock, '') AS DECIMAL(20, 3)), 0)) AS stock_lock_qty,
        SUM(IFNULL(freeze_quantity, 0)) AS freeze_qty,
        COUNT(*) AS inventory_row_count
    FROM ywzt_ods.scm_warehous_manage
    WHERE (inventory_status = 'ZP' OR inventory_status IS NULL OR inventory_status = '')
      AND cargo_owner_id = 'HJXH'
      AND sku_code IN ({placeholders})
    GROUP BY wms_code, cargo_owner_id, sku_code
),
occupy AS (
    SELECT
        wms_code,
        cargo_owner_id,
        sku_code,
        SUM(IFNULL(freeze_quantity, 0)) AS allocate_qty
    FROM ywzt_ods.scm_warehous_occupy
    WHERE created_date > DATE_SUB(NOW(), INTERVAL 60 DAY)
      AND status != 1
      AND cargo_owner_id = 'HJXH'
      AND sku_code IN ({placeholders})
    GROUP BY wms_code, cargo_owner_id, sku_code
),
stock_keys AS (
    SELECT wms_code, cargo_owner_id, sku_code FROM inventory
    UNION
    SELECT wms_code, cargo_owner_id, sku_code FROM occupy
)
SELECT
    k.wms_code AS warehouse_code,
    mw.wms_desc AS warehouse_name,
    mw.wms_type,
    k.sku_code,
    COALESCE(ms.sku_name, i.source_sku_name, k.sku_code) AS sku_name,
    IFNULL(i.in_stock_quantity, 0) AS in_stock_quantity,
    IFNULL(o.allocate_qty, 0) AS allocate_qty,
    IFNULL(i.stock_lock_qty, 0) AS stock_lock_qty,
    IFNULL(i.freeze_qty, 0) AS freeze_qty,
    GREATEST(
        IFNULL(i.in_stock_quantity, 0)
        - IFNULL(o.allocate_qty, 0)
        - CASE
            WHEN IFNULL(i.stock_lock_qty, 0) = 0 THEN IFNULL(i.freeze_qty, 0)
            ELSE IFNULL(i.stock_lock_qty, 0)
          END,
        0
    ) AS available_qty,
    IFNULL(i.inventory_row_count, 0) AS inventory_row_count
FROM stock_keys k
LEFT JOIN inventory i
    ON i.wms_code = k.wms_code
   AND i.cargo_owner_id = k.cargo_owner_id
   AND i.sku_code = k.sku_code
LEFT JOIN occupy o
    ON o.wms_code = k.wms_code
   AND o.cargo_owner_id = k.cargo_owner_id
   AND o.sku_code = k.sku_code
LEFT JOIN ywzt_ods.master_wms mw
    ON mw.wms_code = k.wms_code
LEFT JOIN ywzt_ods.master_sku ms
    ON ms.sku_code = k.sku_code
ORDER BY mw.wms_desc, k.wms_code, k.sku_code
"""
    with connection.cursor() as cursor:
        cursor.execute(sql, tuple(codes + codes))
        return list(cursor.fetchall())


def query_with_retry(
    config: Config, logger: logging.Logger
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    codes = sku_codes(config)
    labels = sku_label_overrides(config)
    retries = max(config.get_int("DB_RETRY_COUNT", 3), 1)
    delay = max(config.get_int("DB_RETRY_DELAY_SECONDS", 5), 0)
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            connection = connect(config)
            try:
                products = fetch_products(connection, codes, labels)
                rows = fetch_stock_rows(connection, codes)
                return products, rows
            finally:
                connection.close()
        except Exception as exc:
            last_error = exc
            logger.warning("数据库查询失败，第 %s/%s 次：%s", attempt, retries, exc)
            if attempt < retries and delay:
                time.sleep(delay)
    raise RuntimeError(f"数据库查询连续失败 {retries} 次：{last_error}") from last_error


def load_previous_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"历史成功状态文件损坏：{path}：{exc}") from exc
    return data if isinstance(data, dict) else {}


def validate_source_against_state(
    source_rows: list[dict[str, Any]], previous_state: dict[str, Any], config: Config
) -> None:
    previous_count = int(previous_state.get("source_stock_key_count") or 0)
    if previous_count <= 0:
        return
    current_count = len(source_rows)
    ratio = float(config.get("MIN_SOURCE_KEY_RATIO", "0.7"))
    if not 0 < ratio <= 1:
        raise ValueError("MIN_SOURCE_KEY_RATIO 必须大于 0 且不大于 1")
    if current_count < previous_count * ratio:
        raise RuntimeError(
            "库存源键数明显缩水："
            f"上次 {previous_count}，本次 {current_count}，低于保护比例 {ratio:.0%}，已停止推送"
        )


def save_success_state(
    path: Path,
    report_time: datetime,
    audit: dict[str, Any],
    total_box_qty: Decimal,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "version": APP_VERSION,
        "report_time": report_time.isoformat(timespec="seconds"),
        "source_stock_key_count": audit["source_stock_key_count"],
        "source_inventory_row_count": audit["source_inventory_row_count"],
        "included_warehouse_count": audit["included_warehouse_count"],
        "total_box_qty": float(total_box_qty),
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def to_decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def box_quantity(available_qty: Any, box_spec: Any, mode: str) -> Decimal:
    spec = to_decimal(box_spec)
    if spec <= 0:
        raise ValueError("箱规必须大于 0")
    exact = max(to_decimal(available_qty), Decimal("0")) / spec
    if mode == "decimal":
        return exact.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if mode == "floor":
        return exact.quantize(Decimal("1"), rounding=ROUND_FLOOR)
    if mode == "ceil":
        return exact.quantize(Decimal("1"), rounding=ROUND_CEILING)
    if mode == "round":
        return exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    raise ValueError("BOX_ROUNDING 仅支持 decimal、floor、ceil、round")


def warehouse_allowed(row: dict[str, Any], config: Config) -> tuple[bool, str]:
    warehouse_name = str(row.get("warehouse_name") or "")
    warehouse_type = str(row.get("wms_type") or "")
    allowed_types = set(config.split("WAREHOUSE_TYPES", ",", "10,11,16"))
    include_unmapped = config.get_bool("INCLUDE_UNMAPPED_WAREHOUSES", True)
    if warehouse_type:
        if allowed_types and warehouse_type not in allowed_types:
            return False, f"仓库类型 {warehouse_type} 不在范围"
    elif not include_unmapped:
        return False, "仓库主数据未维护"

    for keyword in config.split(
        "EXCLUDE_WAREHOUSE_KEYWORDS", "|", "残次|次品|虚拟仓|测试仓"
    ):
        if keyword and keyword.lower() in warehouse_name.lower():
            return False, f"命中排除词 {keyword}"
    return True, ""


def short_warehouse_name(name: str, code: str, shorten: bool) -> str:
    result = name.strip() or f"未维护仓库（{code}）"
    if not shorten:
        return result
    result = result.removeprefix("皇家小虎")
    result = re.sub(r"xs$", "", result, flags=re.IGNORECASE)
    return result.strip()


def aggregate_report(
    products: list[dict[str, Any]], rows: list[dict[str, Any]], config: Config
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    product_by_code = {row["sku_code"]: row for row in products}
    rounding = config.get("BOX_ROUNDING", "decimal").lower()
    shorten = config.get_bool("SHORTEN_WAREHOUSE_NAMES", True)
    show_zero = config.get_bool("SHOW_ZERO_WAREHOUSES", True)

    warehouses: dict[str, dict[str, Any]] = {}
    excluded: dict[str, dict[str, Any]] = {}
    unmapped_codes: set[str] = set()
    raw_inventory_rows = 0

    for row in rows:
        code = str(row.get("sku_code") or "")
        if code not in product_by_code:
            continue
        raw_inventory_rows += int(to_decimal(row.get("inventory_row_count")))
        allowed, reason = warehouse_allowed(row, config)
        warehouse_code = str(row.get("warehouse_code") or "")
        warehouse_name = str(row.get("warehouse_name") or "")
        if not warehouse_name:
            unmapped_codes.add(warehouse_code)
        if not allowed:
            excluded.setdefault(
                warehouse_code,
                {
                    "warehouse_code": warehouse_code,
                    "warehouse_name": warehouse_name,
                    "reason": reason,
                },
            )
            continue

        product = product_by_code[code]
        item_box_qty = box_quantity(
            row.get("available_qty"), product["box_spec"], rounding
        )
        warehouse = warehouses.setdefault(
            warehouse_code,
            {
                "warehouse_code": warehouse_code,
                "warehouse_name": short_warehouse_name(
                    warehouse_name, warehouse_code, shorten
                ),
                "source_warehouse_name": warehouse_name,
                "wms_type": str(row.get("wms_type") or ""),
                "total_box_qty": Decimal("0"),
                "items": {},
            },
        )
        warehouse["items"][code] = {
            "sku_code": code,
            "sku_name": product["sku_name"],
            "label": product["label"],
            "box_spec": product["box_spec"],
            "in_stock_quantity": to_decimal(row.get("in_stock_quantity")),
            "allocate_qty": to_decimal(row.get("allocate_qty")),
            "stock_lock_qty": to_decimal(row.get("stock_lock_qty")),
            "freeze_qty": to_decimal(row.get("freeze_qty")),
            "available_qty": to_decimal(row.get("available_qty")),
            "box_qty": item_box_qty,
        }
        warehouse["total_box_qty"] += item_box_qty

    for warehouse in warehouses.values():
        for product in products:
            code = product["sku_code"]
            if code not in warehouse["items"]:
                warehouse["items"][code] = {
                    "sku_code": code,
                    "sku_name": product["sku_name"],
                    "label": product["label"],
                    "box_spec": product["box_spec"],
                    "in_stock_quantity": Decimal("0"),
                    "allocate_qty": Decimal("0"),
                    "stock_lock_qty": Decimal("0"),
                    "freeze_qty": Decimal("0"),
                    "available_qty": Decimal("0"),
                    "box_qty": Decimal("0"),
                }

    result = list(warehouses.values())
    if not show_zero:
        result = [row for row in result if row["total_box_qty"] > 0]
    result.sort(key=lambda row: (-row["total_box_qty"], row["warehouse_name"]))

    if not rows:
        raise RuntimeError("库存查询未返回任何记录，已停止推送")
    if raw_inventory_rows < config.get_int("MIN_INVENTORY_SOURCE_ROWS", 1):
        raise RuntimeError(
            f"库存源记录数仅 {raw_inventory_rows}，低于保护阈值，已停止推送"
        )
    if not result:
        raise RuntimeError("仓库过滤后没有可推送记录，请检查仓库类型和排除词配置")

    audit = {
        "source_stock_key_count": len(rows),
        "source_inventory_row_count": raw_inventory_rows,
        "included_warehouse_count": len(result),
        "excluded_warehouses": list(excluded.values()),
        "unmapped_warehouse_codes": sorted(unmapped_codes),
        "box_rounding": rounding,
        "warehouse_types": sorted(
            set(config.split("WAREHOUSE_TYPES", ",", "10,11,16"))
        ),
    }
    return result, audit


def format_quantity(value: Decimal) -> str:
    text = f"{value:,.2f}"
    return text.rstrip("0").rstrip(".")


def total_boxes(warehouses: list[dict[str, Any]]) -> Decimal:
    return sum((row["total_box_qty"] for row in warehouses), Decimal("0"))


def message_parts(
    warehouses: list[dict[str, Any]],
    products: list[dict[str, Any]],
    report_time: datetime,
    config: Config,
) -> list[str]:
    max_bytes = config.get_int("WECOM_MAX_MESSAGE_BYTES", 3500)
    if max_bytes < 800:
        raise ValueError("WECOM_MAX_MESSAGE_BYTES 不能小于 800")
    rounding = config.get("BOX_ROUNDING", "decimal").lower()
    rounding_text = {
        "decimal": "精确箱数，保留2位小数",
        "floor": "只计完整箱，向下取整",
        "ceil": "有余数计1箱，向上取整",
        "round": "四舍五入为整数箱",
    }[rounding]
    header = [
        "# 虎皮肉肠库存日报",
        f"> 数据时间：{report_time:%Y-%m-%d %H:%M:%S}",
        f"> 仓库数：{len(warehouses)}　合计：<font color=\"info\">{format_quantity(total_boxes(warehouses))} 箱</font>",
        f"> 口径：可用库存÷箱规；{rounding_text}",
        "",
    ]
    row_blocks = []
    for index, warehouse in enumerate(warehouses, start=1):
        details = "｜".join(
            f"{product['label']} {format_quantity(warehouse['items'][product['sku_code']]['box_qty'])}"
            for product in products
        )
        row_blocks.append(
            f"{index}. **{warehouse['warehouse_name']}**：<font color=\"warning\">{format_quantity(warehouse['total_box_qty'])} 箱</font>\n   {details}"
        )
    footer = ["", "> 可用库存＝在库－近60天有效占用－锁定/冻结；已排除残次、次品、虚拟及非配置仓库。"]

    parts: list[list[str]] = []
    current = list(header)
    for block in row_blocks:
        candidate = "\n".join(current + [block] + footer)
        if len(candidate.encode("utf-8")) > max_bytes and len(current) > len(header):
            parts.append(current)
            current = ["# 虎皮肉肠库存日报（续）", "", block]
        else:
            current.append(block)
    current.extend(footer)
    parts.append(current)

    count = len(parts)
    output: list[str] = []
    for index, lines in enumerate(parts, start=1):
        if count > 1:
            lines[0] = re.sub(r"（续）$", "", lines[0]) + f"（{index}/{count}）"
        content = "\n".join(lines)
        if len(content.encode("utf-8")) > max_bytes:
            raise RuntimeError("单条企微消息仍超长，请调大拆分粒度或缩短仓库名称")
        output.append(content)
    return output


def valid_webhook(url: str) -> bool:
    if not url or "请填写" in url:
        return False
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    return (
        parsed.scheme == "https"
        and parsed.netloc == "qyapi.weixin.qq.com"
        and parsed.path.endswith("/cgi-bin/webhook/send")
        and bool(query.get("key", [""])[0])
    )


def send_wecom_markdown(webhook: str, content: str, timeout: int) -> dict[str, Any]:
    payload = json.dumps(
        {"msgtype": "markdown", "markdown": {"content": content}},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        webhook,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"企微请求失败：{exc}") from exc
    try:
        result = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"企微返回非 JSON：{body[:200]}") from exc
    if result.get("errcode") != 0:
        raise RuntimeError(f"企微推送失败：{result}")
    return result


def json_ready(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, dict):
        return {key: json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_ready(item) for item in value]
    return value


def write_outputs(
    output_dir: Path,
    report_time: datetime,
    products: list[dict[str, Any]],
    warehouses: list[dict[str, Any]],
    audit: dict[str, Any],
    parts: list[str],
    runtime: dict[str, Any],
) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = report_time.strftime("%Y%m%d_%H%M%S")
    base = output_dir / f"虎皮肉肠库存日报_{stamp}"
    csv_path = base.with_suffix(".csv")
    json_path = base.with_suffix(".json")
    preview_path = base.with_suffix(".txt")

    product_columns = [row["label"] for row in products]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["仓库编码", "仓库名称", "仓库类型", "总箱数"]
            + product_columns,
        )
        writer.writeheader()
        for warehouse in warehouses:
            row = {
                "仓库编码": warehouse["warehouse_code"],
                "仓库名称": warehouse["warehouse_name"],
                "仓库类型": warehouse["wms_type"],
                "总箱数": format_quantity(warehouse["total_box_qty"]),
            }
            for product in products:
                row[product["label"]] = format_quantity(
                    warehouse["items"][product["sku_code"]]["box_qty"]
                )
            writer.writerow(row)

    document = {
        "ok": True,
        "version": APP_VERSION,
        "report_time": report_time,
        "products": products,
        "warehouses": warehouses,
        "audit": audit,
        "runtime": runtime,
    }
    json_path.write_text(
        json.dumps(json_ready(document), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    preview_path.write_text("\n\n--- 下一条消息 ---\n\n".join(parts), encoding="utf-8")
    return {
        "csv": str(csv_path),
        "json": str(json_path),
        "preview": str(preview_path),
    }


def config_summary(config: Config, dry_run: bool) -> dict[str, Any]:
    return {
        "ok": True,
        "version": APP_VERSION,
        "config_file": str(config.path),
        "db_host": config.get("DB_HOST"),
        "db_port": config.get_int("DB_PORT", 9030),
        "db_name": config.get("DB_NAME", "ywzt_ods"),
        "db_user_configured": bool(config.get("DB_USER"))
        and config.get("DB_USER") != "请填写",
        "db_password_configured": bool(config.get("DB_PASSWORD"))
        and config.get("DB_PASSWORD") != "请填写",
        "wecom_enabled": config.get_bool("WECOM_ENABLED", True),
        "webhook_configured": valid_webhook(config.get("WECOM_WEBHOOK_URL")),
        "dry_run": dry_run,
        "target_sku_codes": sku_codes(config),
        "warehouse_types": config.split("WAREHOUSE_TYPES", ",", "10,11,16"),
        "box_rounding": config.get("BOX_ROUNDING", "decimal"),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="每日虎皮肉肠分仓库存企微推送")
    parser.add_argument(
        "--config", default=str(ROOT / "config" / "config.env"), help="配置文件路径"
    )
    parser.add_argument("--dry-run", action="store_true", help="只取数和生成预览，不推送企微")
    parser.add_argument("--check-config", action="store_true", help="只检查配置格式，不连接数据库")
    parser.add_argument("--webhook", default="", help="临时覆盖配置中的企微机器人链接")
    parser.add_argument("--output-dir", default="", help="临时覆盖输出目录")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = Config(Path(args.config))
    if args.webhook:
        os.environ["WECOM_WEBHOOK_URL"] = args.webhook.strip()
    dry_run = args.dry_run or config.get_bool("DRY_RUN", False)
    if args.check_config:
        print(json.dumps(config_summary(config, dry_run), ensure_ascii=False, indent=2))
        return 0

    required_config(config)
    output_dir = (
        Path(args.output_dir).resolve()
        if args.output_dir
        else config.resolve_path("OUTPUT_DIR", "outputs")
    )
    state_file = config.resolve_path("STATE_FILE", "state/last_success.json")
    logger = setup_logging(config.resolve_path("LOG_DIR", "logs"))
    report_time = datetime.now()
    logger.info("开始查询虎皮肉肠分仓可用库存，version=%s", APP_VERSION)

    try:
        products, source_rows = query_with_retry(config, logger)
        previous_state = load_previous_state(state_file)
        validate_source_against_state(source_rows, previous_state, config)
        warehouses, audit = aggregate_report(products, source_rows, config)
        parts = message_parts(warehouses, products, report_time, config)

        wecom_enabled = config.get_bool("WECOM_ENABLED", True)
        webhook = config.get("WECOM_WEBHOOK_URL")
        responses: list[dict[str, Any]] = []
        sent = False
        if not dry_run and wecom_enabled:
            if not valid_webhook(webhook):
                raise ValueError("WECOM_WEBHOOK_URL 未填写或格式不正确")
            timeout = config.get_int("WECOM_TIMEOUT_SECONDS", 20)
            for index, part in enumerate(parts, start=1):
                logger.info("推送企微消息 %s/%s", index, len(parts))
                responses.append(send_wecom_markdown(webhook, part, timeout))
            sent = True
        else:
            logger.info("当前为预览模式或已关闭企微推送，不会发送消息")

        runtime = {
            "dry_run": dry_run,
            "wecom_enabled": wecom_enabled,
            "sent_to_wecom": sent,
            "wecom_message_count": len(parts),
            "wecom_responses": responses,
        }
        files = write_outputs(
            output_dir, report_time, products, warehouses, audit, parts, runtime
        )
        if not dry_run or config.get_bool("UPDATE_STATE_ON_DRY_RUN", True):
            save_success_state(
                state_file, report_time, audit, total_boxes(warehouses)
            )
        result = {
            "ok": True,
            "version": APP_VERSION,
            "report_time": report_time.isoformat(timespec="seconds"),
            "warehouse_count": len(warehouses),
            "total_box_qty": float(total_boxes(warehouses)),
            "source_stock_key_count": audit["source_stock_key_count"],
            "source_inventory_row_count": audit["source_inventory_row_count"],
            "excluded_warehouse_count": len(audit["excluded_warehouses"]),
            "unmapped_warehouse_codes": audit["unmapped_warehouse_codes"],
            "message_count": len(parts),
            "sent_to_wecom": sent,
            "dry_run": dry_run,
            "files": files,
            "state_file": str(state_file),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        logger.info(
            "完成：仓库=%s，总箱数=%s，企微发送=%s",
            len(warehouses),
            format_quantity(total_boxes(warehouses)),
            sent,
        )
        return 0
    except Exception as exc:
        logger.exception("运行失败：%s", exc)
        print(
            json.dumps(
                {"ok": False, "version": APP_VERSION, "error": str(exc)},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

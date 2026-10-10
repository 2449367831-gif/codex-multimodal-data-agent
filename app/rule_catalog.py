"""Explicit, versioned rule-query catalog for the browser quick runner."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
import re
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from .agent_core.catalog import ReleasedMetricCatalog
from .agent_core.semantic_types import SemanticRequest
from .query_scope import QueryScope
from .knowledge_activation import active_snapshot


RULE_CATALOG_VERSION = "rules-v2"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RULE_METRIC_IDS = {
    "actual_sales_by_warehouse": "sales.business_quantity",
    "actual_sales_top_sku": "sales.business_quantity",
    "current_finished_goods_inventory": "inventory.finished_goods_realtime",
    "inventory_turnover_by_warehouse": "inventory.turnover_finished_goods_realtime",
    "inventory_turnover_total": "inventory.turnover_finished_goods_realtime",
    "center_warehouse_turnover_report": "inventory.turnover_7d_realtime",
}
_RULE_FIELDS = {
    "actual_sales_by_warehouse": {"window", "owner", "warehouses", "sku_codes", "start_date", "end_date"},
    "actual_sales_top_sku": {"window", "owner", "warehouses", "sku_codes", "top_n", "start_date", "end_date"},
    "current_finished_goods_inventory": {"owner", "warehouses", "sku_codes"},
    "inventory_turnover_by_warehouse": {"window", "owner", "warehouses", "sku_codes", "start_date", "end_date"},
    "inventory_turnover_total": {"window", "owner", "warehouses", "sku_codes", "start_date", "end_date"},
    "center_warehouse_turnover_report": {"window", "end_date"},
}


def _released_metric_ids(root: Path) -> set[str]:
    """Expose only rules backed by the active approved knowledge snapshot."""

    try:
        return set(ReleasedMetricCatalog(root).metric_ids())
    except (OSError, KeyError, TypeError, ValueError):
        # A broken or missing release must fail closed. The UI can display an
        # empty rule catalog while natural-language requests still report the
        # normal knowledge/service health error through their own path.
        return set()


def _options(values: list[tuple[str, str]]) -> list[dict[str, str]]:
    return [{"id": value, "label": label} for value, label in values]


def _field(field_id: str, label: str, kind: str, *, options=None, default=None, help_text=None):
    value = {"id": field_id, "label": label, "type": kind}
    if options is not None:
        value["options"] = options
    if default is not None:
        value["default"] = default
    if help_text:
        value["help"] = help_text
    return value


def _warehouse_options(root: Path) -> list[dict[str, str]]:
    # The current approved turnover template is the authoritative center list
    # after the Guangzhou Shunde rollout. Keep the older operating-center file
    # as a compatibility fallback for installations without that release.
    template_path = root / "profiles" / "hjxh_turnover_template_v3.yaml"
    if template_path.is_file():
        profile = yaml.safe_load(template_path.read_text(encoding="utf-8"))
        centers = profile.get("centers", [])
    else:
        profile = yaml.safe_load(
            (root / "profiles" / "hjxh_operating_centers.yaml").read_text(encoding="utf-8")
        )
        centers = profile.get("scope", {}).get("centers", [])
    options: list[dict[str, str]] = []
    seen: set[str] = set()
    for center in centers:
        rows = [center, *(center.get("details") or [])] if isinstance(center, dict) else []
        for row in rows:
            if not isinstance(row, dict) or not row.get("code") or not row.get("name"):
                continue
            code = str(row["code"])
            if code in seen:
                continue
            seen.add(code)
            options.append({"id": code, "label": str(row["name"])})
    return options


def rule_catalog(root: Path) -> dict[str, Any]:
    try:
        semantic_root, _release_digest = active_snapshot(root)
    except (OSError, TypeError, ValueError, KeyError):
        return {"version": RULE_CATALOG_VERSION, "timezone": "Asia/Shanghai", "release_digest": None, "rules": []}
    warehouses = _warehouse_options(semantic_root)
    time_options = _options([
        ("yesterday", "昨天"),
        ("last_7_complete_days", "最近7个完整自然日"),
        ("last_14_complete_days", "最近14个完整自然日"),
        ("previous_month", "上个月"),
        ("custom", "自定义日期区间"),
    ])
    owner_options = _options([("ALL", "全部货主"), ("HJXH", "HJXH")])
    warehouse_help = "不选择表示全部仓库；选择后只统计选中的已核验仓库。"
    rules = [
            {
                "id": "actual_sales_by_warehouse",
                "title": "销售数量 · 按仓库",
                "description": "按新销售口径汇总：社区平台用订单请求量，其余渠道用实发量。",
                "fields": [
                    _field("window", "统计时间", "select", options=time_options, default="yesterday"),
                    _field("owner", "货主", "select", options=owner_options, default="ALL"),
                    _field("warehouses", "仓库", "multi_select", options=warehouses, help_text=warehouse_help),
                    _field("start_date", "开始日期", "date"),
                    _field("end_date", "结束日期（不含）", "date"),
                ],
            },
            {
                "id": "actual_sales_top_sku",
                "title": "销售数量 · SKU TOP",
                "description": "按新销售口径排名；出库单与社区平台订单分别统计。",
                "fields": [
                    _field("window", "统计时间", "select", options=time_options, default="yesterday"),
                    _field("owner", "货主", "select", options=owner_options, default="ALL"),
                    _field("warehouses", "仓库", "multi_select", options=warehouses, help_text=warehouse_help),
                    _field("top_n", "TOP 数量", "select", options=_options([(str(n), f"前 {n} 个") for n in (10, 20, 50, 100)]), default="20"),
                    _field("start_date", "开始日期", "date"),
                    _field("end_date", "结束日期（不含）", "date"),
                ],
            },
            {
                "id": "current_finished_goods_inventory",
                "title": "当前实时成品库存",
                "description": "可用成品库存加在途，按仓库汇总，单位 EA。",
                "fields": [
                    _field("owner", "货主", "select", options=owner_options, default="ALL"),
                    _field("warehouses", "仓库", "multi_select", options=warehouses, help_text=warehouse_help),
                    _field("sku_codes", "SKU（可选）", "text", help_text="多个 SKU 用逗号分隔；留空表示全部成品。"),
                ],
            },
            {
                "id": "inventory_turnover_by_warehouse",
                "title": "库存周转 · 按仓库",
                "description": "当前可用成品库存加在途，除以所选完整自然日的新销售口径日均量。",
                "fields": [
                    _field("window", "销售时间", "select", options=time_options, default="last_7_complete_days"),
                    _field("owner", "货主", "select", options=owner_options, default="ALL"),
                    _field("warehouses", "仓库", "multi_select", options=warehouses, help_text=warehouse_help),
                    _field("start_date", "开始日期", "date"),
                    _field("end_date", "结束日期（不含）", "date"),
                ],
            },
            {
                "id": "inventory_turnover_total",
                "title": "库存周转 · 汇总",
                "description": "按所选完整自然日计算总体库存周转天数。",
                "fields": [
                    _field("window", "销售时间", "select", options=time_options, default="last_7_complete_days"),
                    _field("owner", "货主", "select", options=owner_options, default="ALL"),
                    _field("start_date", "开始日期", "date"),
                    _field("end_date", "结束日期（不含）", "date"),
                ],
            },
            {
                "id": "center_warehouse_turnover_report",
                "title": "中心仓与分仓14天周转",
                "description": "按业务模板生成中心仓近7天、分仓近14天的周转图片；固定使用9个中心和已核验分仓。",
                "fields": [
                    _field("window", "销售时间", "select", options=_options([("last_14_complete_days", "最近14个完整自然日")]), default="last_14_complete_days"),
                    _field("end_date", "截止日期（不含）", "date", help_text="留空使用当前业务日；中心仓取前7个完整自然日，分仓取前14个完整自然日。"),
                ],
            },
    ]
    for rule in rules:
        if rule["id"] == "center_warehouse_turnover_report":
            continue
        field_ids = {field["id"] for field in rule["fields"]}
        if "warehouses" not in field_ids:
            rule["fields"].insert(2, _field("warehouses", "仓库", "multi_select", options=warehouses, help_text=warehouse_help))
        if "sku_codes" not in field_ids:
            rule["fields"].append(_field("sku_codes", "SKU（可选）", "text", help_text="多个 SKU 用逗号分隔；留空表示全部。"))
    released = _released_metric_ids(root)
    return {
        "version": RULE_CATALOG_VERSION,
        "timezone": "Asia/Shanghai",
        "release_digest": _release_digest,
        "rules": [
            rule for rule in rules
            if _RULE_METRIC_IDS.get(rule["id"]) in released
        ],
    }


def _clean_list(value: Any, *, max_items: int = 100) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = re.split(r"[,，、\s]+", value.strip()) if value.strip() else []
    elif isinstance(value, list):
        values = value
    else:
        raise ValueError("条件列表格式无效")
    if len(values) > max_items or any(not isinstance(item, str) for item in values):
        raise ValueError("条件列表格式无效")
    return list(dict.fromkeys(item.strip() for item in values if item.strip()))


def _date(value: Any, name: str) -> date:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise ValueError(f"{name}格式无效")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name}格式无效") from exc


def _window(params: dict[str, Any], *, now: datetime) -> tuple[str, date, date]:
    window = params.get("window", "yesterday")
    today = now.date()
    if window == "yesterday":
        return "昨天", today - timedelta(days=1), today
    if window == "last_7_complete_days":
        return "最近7个完整自然日", today - timedelta(days=7), today
    if window == "last_14_complete_days":
        return "最近14个完整自然日", today - timedelta(days=14), today
    if window == "previous_month":
        first = today.replace(day=1)
        previous_end = first
        previous_start = (first - timedelta(days=1)).replace(day=1)
        return "上个月", previous_start, previous_end
    if window == "custom":
        start = _date(params.get("start_date"), "开始日期")
        end = _date(params.get("end_date"), "结束日期")
        if end <= start:
            raise ValueError("结束日期必须晚于开始日期")
        if end > today:
            raise ValueError("结束日期不能晚于当前业务日")
        if (end - start).days > 3660:
            raise ValueError("统计区间不能超过10年")
        return f"{start.isoformat()}至{end.isoformat()}", start, end
    raise ValueError("统计时间选项无效")


def _owner(params: dict[str, Any]) -> str:
    # An omitted scope is ALL. The UI may provide HJXH explicitly, but the
    # server must not inherit the previous conversation's owner.
    owner = params.get("owner", "ALL")
    if owner not in {"ALL", "HJXH"}:
        raise ValueError("货主选项无效")
    return "" if owner == "ALL" else "HJXH货主"


def _owner_code(params: dict[str, Any]) -> str:
    owner = params.get("owner", "ALL")
    if owner not in {"ALL", "HJXH"}:
        raise ValueError("货主选项无效")
    return owner


def _warehouse_labels(params: dict[str, Any], root: Path) -> list[str]:
    selected = _clean_list(params.get("warehouses"))
    allowed = {item["id"]: item["label"] for item in _warehouse_options(root)}
    if any(code not in allowed for code in selected):
        raise ValueError("仓库选项无效")
    return [allowed[code] for code in selected]


def _warehouse_scope(params: dict[str, Any], root: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    selected = tuple(_clean_list(params.get("warehouses")))
    allowed = {item["id"]: item["label"] for item in _warehouse_options(root)}
    if any(code not in allowed for code in selected):
        raise ValueError("仓库选项无效")
    return selected, tuple(allowed[code] for code in selected)


def _scope_phrase(params: dict[str, Any], root: Path) -> str:
    labels = _warehouse_labels(params, root)
    return "，" + "、".join(labels) if labels else ""


def _sku_phrase(params: dict[str, Any]) -> str:
    values = _clean_list(params.get("sku_codes"), max_items=100)
    if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", value) for value in values):
        raise ValueError("SKU格式无效")
    return "，SKU " + "、".join(f"`{value}`" for value in values) if values else ""


def build_rule_request(rule_id: Any, params: Any, *, root: Path, now: datetime) -> dict[str, Any]:
    if not isinstance(rule_id, str) or not rule_id:
        raise ValueError("规则选项无效")
    if not isinstance(params, dict):
        raise ValueError("规则条件格式无效")
    catalog_data = rule_catalog(root)
    catalog_ids = {rule["id"] for rule in catalog_data["rules"]}
    if rule_id not in catalog_ids:
        raise ValueError("规则选项无效")
    unknown_fields = set(params) - _RULE_FIELDS.get(rule_id, set())
    if unknown_fields:
        raise ValueError("存在不支持的规则条件")
    definition = next(rule for rule in catalog_data["rules"] if rule["id"] == rule_id)
    params = {
        **{field["id"]: field["default"] for field in definition["fields"] if "default" in field},
        **params,
    }
    business_tz = ZoneInfo("Asia/Shanghai")
    now = now.astimezone(business_tz) if now.tzinfo else now.replace(tzinfo=business_tz)
    if rule_id == "current_finished_goods_inventory":
        window_label, start, end = "当前实时", now.date(), now.date() + timedelta(days=1)
    else:
        window_label, start, end = _window(params, now=now)
    owner = _owner(params)
    semantic_root, _release_digest = active_snapshot(root)
    warehouse_codes, warehouse_labels = _warehouse_scope(params, semantic_root)
    warehouse = "，" + "、".join(warehouse_labels) if warehouse_labels else ""
    sku_phrase = _sku_phrase(params)
    warehouse += sku_phrase
    sku_codes = tuple(_clean_list(params.get("sku_codes"), max_items=100))
    if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", value) for value in sku_codes):
        raise ValueError("SKU格式无效")
    scope = QueryScope(
        owner=_owner_code(params),
        warehouse_codes=warehouse_codes,
        warehouse_labels=warehouse_labels,
        sku_codes=sku_codes,
    )
    common = {
        "rule_version": catalog_data["version"],
        "release_digest": _release_digest,
    }
    if rule_id == "actual_sales_by_warehouse":
        plan = SemanticRequest(
            route="business_sales_by_warehouse", start=start, end=end,
            show_warehouses=True, scope=scope,
        )
        return {**common, "question": f"查询{window_label}{owner}的新口径销售数量，按仓库汇总{warehouse}", "plan": plan}
    if rule_id == "actual_sales_top_sku":
        top_n = str(params.get("top_n", "20"))
        if top_n not in {"10", "20", "50", "100"}:
            raise ValueError("TOP 数量选项无效")
        plan = SemanticRequest(
            route="business_sales_top_sku", start=start, end=end,
            show_top_sku=True, top_n=int(top_n), scope=scope,
        )
        return {**common, "question": f"查询{window_label}{owner}的新口径销售数量，按SKU汇总销售数量、出库单数和社区平台订单数，列出发货数量最高的前{top_n}个SKU{warehouse}", "plan": plan}
    if rule_id == "current_finished_goods_inventory":
        scope = QueryScope(
            owner=_owner_code(params), warehouse_codes=warehouse_codes,
            warehouse_labels=warehouse_labels, sku_codes=sku_codes,
            finished_goods=True,
        )
        plan = SemanticRequest(
            route="current_inventory", start=now.date(), end=now.date() + timedelta(days=1),
            show_warehouses=True, finished_goods_only=True,
            scope=scope,
        )
        return {**common, "question": f"查询当前{owner}实时成品库存，按仓库汇总{warehouse}", "plan": plan}
    if rule_id == "center_warehouse_turnover_report":
        if params.get("window") != "last_14_complete_days":
            raise ValueError("该周转模板固定使用最近14个完整自然日")
        end_date_value = params.get("end_date")
        if end_date_value in (None, ""):
            report_end_date_exclusive = None
        else:
            report_end_date = _date(end_date_value, "截止日期")
            if report_end_date > now.date():
                raise ValueError("截止日期不能晚于当前业务日")
            report_end_date_exclusive = report_end_date.isoformat()
        return {
            **common,
            "question": "生成中心仓和分仓14天周转数据图片",
            "report_kind": "center_warehouse_turnover_7d",
            "report_profile_id": "actual_finished_goods",
            "window_days": 14,
            "report_end_date_exclusive": report_end_date_exclusive,
        }
    if rule_id == "inventory_turnover_by_warehouse":
        scope = QueryScope(
            owner=_owner_code(params), warehouse_codes=warehouse_codes,
            warehouse_labels=warehouse_labels, sku_codes=sku_codes, finished_goods=True,
        )
        plan = SemanticRequest(
            route="realtime_turnover_by_warehouse", start=start, end=end,
            show_warehouses=True, finished_goods_only=True,
            turnover_grain="warehouse", scope=scope,
        )
        return {**common, "question": f"查询{window_label}{owner}库存周转，按仓库{warehouse}", "plan": plan}
    if rule_id == "inventory_turnover_total":
        scope = QueryScope(owner=_owner_code(params), warehouse_codes=warehouse_codes,
                           warehouse_labels=warehouse_labels, sku_codes=sku_codes, finished_goods=True)
        plan = SemanticRequest(
            route="realtime_turnover", start=start, end=end,
            finished_goods_only=True, turnover_grain="total",
            scope=scope,
        )
        return {**common, "question": f"查询{window_label}{owner}库存周转{warehouse}", "plan": plan}
    raise ValueError("规则选项无效")

"""Diagnose name provenance over the whole Top20 scope; never print source rows."""

import argparse
from datetime import datetime, timedelta
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import sqlglot
from sqlglot import exp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.agent_core.sales_drilldown import SalesDrilldownPlan, compile_sales_drilldown, retrieve_sales_ranking, retrieve_ranked_sku
from app.agent_core.sales_pages import retrieve_sales_groups
from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result

KEYS = ("sku_code", "cargo_owner", "warehouse_code")
NAMES = ("sku_name", "warehouse_name")


def reference_module():
    spec = spec_from_file_location("independent_names", Path(__file__).with_name("check_sales_drilldown_live.py"))
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def profile_sql(plan, *, reference=False):
    source = reference_module().reference_sql(plan) if reference else compile_sales_drilldown(plan)
    tree = sqlglot.parse_one(source, read="doris")
    relation = "selected" if reference else "report"
    owner = "owner AS cargo_owner" if reference else "cargo_owner"
    group = sqlglot.parse_one(
        f"SELECT DISTINCT sku_code,{owner},warehouse_code,sku_name,warehouse_name FROM {relation}"
        + ("" if reference else " WHERE row_kind='detail'"), read="doris")
    ctes = tree.args["with_"].copy()
    ctes.append("expressions", exp.CTE(this=group, alias=exp.TableAlias(this=exp.to_identifier("name_groups"))))
    result = sqlglot.parse_one(
        "SELECT *,COUNT(*) OVER() AS group_count FROM name_groups "
        "ORDER BY sku_code,cargo_owner,warehouse_code,sku_name,warehouse_name LIMIT 501", read="doris")
    result.set("with_", ctes)
    return result.sql(dialect="doris")


def compare_profiles(actual, expected):
    def keyed(rows):
        result = {}
        for row in rows:
            key = tuple(row[field] for field in KEYS)
            if key in result:
                raise ValueError("conflicting name-profile key")
            result[key] = tuple(row[field] for field in NAMES)
        return result
    left, right = keyed(actual), keyed(expected)
    shared = left.keys() & right.keys()
    changed = [key for key in shared if left[key] != right[key]]
    return {
        "same_keys": left.keys() == right.keys(),
        "same_names": left == right,
        "product_name_differences": sum(left[key][0] != right[key][0] for key in shared),
        "warehouse_name_differences": sum(left[key][1] != right[key][1] for key in shared),
        "runtime_code_fallbacks_on_differences": sum(left[key][1] == key[2] for key in changed),
        "reference_code_fallbacks_on_differences": sum(right[key][1] == key[2] for key in changed),
    }


def detail_name_diagnostics(snapshot, reference):
    products = {row.sku_code: row.sku_name for row in snapshot.ranking}
    actual = { (row.sku_code, row.cargo_owner, row.outbound_code):
        (products[row.sku_code], row.warehouse_name, row.warehouse_code) for row in snapshot.details }
    expected = { (row["sku_code"], row["owner"], row["outbound_code"]):
        (row["sku_name"], row["warehouse_name"], row["warehouse_code"]) for row in reference }
    if len(actual) != len(snapshot.details) or len(expected) != len(reference) or actual.keys() != expected.keys():
        raise ValueError("name diagnostic requires unique matching detail keys")
    return {
        "product_name_differences": sum(actual[key][0] != expected[key][0] for key in actual),
        "warehouse_name_differences": sum(actual[key][1] != expected[key][1] for key in actual),
        "runtime_code_fallbacks": sum(actual[key][1] == actual[key][2] and actual[key][1] != expected[key][1] for key in actual),
        "reference_code_fallbacks": sum(expected[key][1] == expected[key][2] and actual[key][1] != expected[key][1] for key in actual),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--diagnostic-rank", type=int, choices=range(1, 21))
    args = parser.parse_args()
    _hide_windows_subprocesses()
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    plan = SalesDrilldownPlan(today - timedelta(days=1), today)
    bridge = CodexMcpBridge(project_root=ROOT, backend_url="http://127.0.0.1:8010", timeout_seconds=180)
    with bridge.request_session() as client:
        def read(sql, *, purpose):
            return validate_doris_result(client.query(sql=sql, purpose=purpose)).rows
        if args.diagnostic_rank:
            budgets = {"page_size": 2000, "page_cap": 2000}
            ranking = retrieve_sales_ranking(plan, read, **budgets)
            selected = next(row for row in ranking.ranking if row.rank == args.diagnostic_rank)
            detail = retrieve_ranked_sku(ranking, selected.sku_code, read, **budgets)
            module = reference_module()
            reference = retrieve_sales_groups(module.reference_sql(plan, selected.sku_code), read, **budgets)
            checks = module.compare(detail, reference)
            print(json.dumps({"case": "T38_selected_name_diagnosis", "full_acceptance": False,
                              "checks": checks, "name_diagnostics": detail_name_diagnostics(detail, reference)}), flush=True)
            return 0 if all(checks.values()) else 1
        profiles = []
        for reference in (False, True, False, True):
            profiles.append(retrieve_sales_groups(profile_sql(plan, reference=reference), read, page_size=2000, page_cap=2000))
            print(json.dumps({"stage": "profile_read", "completed_profiles": len(profiles)}), flush=True)
        checks = compare_profiles(profiles[0], profiles[1])
        checks["runtime_stable"] = compare_profiles(profiles[0], profiles[2])["same_names"]
        checks["reference_stable"] = compare_profiles(profiles[1], profiles[3])["same_names"]
        checks["second_comparison"] = compare_profiles(profiles[2], profiles[3])
        print(json.dumps({"case": "T38_name_diagnosis", "checks": checks}), flush=True)
        return 0 if checks["same_names"] and checks["runtime_stable"] and checks["reference_stable"] and checks["second_comparison"]["same_names"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"case": "T38_name_diagnosis", "error_type": type(exc).__name__, "reason": getattr(exc, "reason", None)}), flush=True)
        raise SystemExit(1)

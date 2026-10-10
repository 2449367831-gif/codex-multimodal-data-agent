"""Reconcile directory shares against an independent in-memory source ledger."""

import argparse
from collections import Counter, defaultdict
from decimal import Decimal, localcontext
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.codex_agent import _hide_windows_subprocesses
from app.codex_mcp_bridge import CodexMcpBridge
from app.query_outcomes import validate_doris_result
from scripts.evaluate_generalization import ChatClient
from tests.acceptance.check_partition_ranking_live import complete_rows


REFERENCE_SQL = """SELECT DISTINCT
 TRIM(CAST(cargo_owner_id AS STRING)) AS owner,
 TRIM(wms_code) AS code, TRIM(wms_desc) AS name,
 NULLIF(TRIM(CAST(wms_type AS STRING)), '') AS kind,
 NULLIF(TRIM(CAST(wms_node AS STRING)), '') AS node
FROM ywzt_ods.master_wms
WHERE COALESCE(TRIM(CAST(status AS STRING)), '0') = '0'
ORDER BY owner,code,name,kind,node LIMIT 10000"""


def reference_counts(rows, keyword):
    assert len(rows) < 10000, "reference_capacity_reached"
    variants = defaultdict(set)
    matched = set()
    for row in rows:
        identity = row["owner"], row["code"]
        variants[identity].add((row["name"], row["kind"], row["node"]))
        if row["name"] is not None and keyword in row["name"]:
            matched.add(identity)
    counts = Counter()
    for identity in matched:
        assert all(isinstance(k, str) and k.strip() for k in identity), "source_identity_invalid"
        assert len(variants[identity]) == 1, "source_mapping_conflict"
        name, kind, _ = next(iter(variants[identity]))
        assert name, "source_name_invalid"
        label = "未配置" if kind is None else "云仓（12）" if kind == "12" else f"未映射（{kind}）"
        counts[label] += 1
    return counts


def compare(table, rows, expected):
    columns = {c["label"]: c for c in table["columns"]}
    assert set(columns) == {"仓库类型", "仓库数量", "数量占比（%）"}, "wrong_projection"
    assert columns["仓库数量"].get("unit") == "个", "wrong_count_unit"
    assert columns["数量占比（%）"].get("kind") == "number", "wrong_ratio_type"
    actual = {}
    total = sum(expected.values())
    with localcontext() as context:
        context.prec = 64
        for row in rows:
            label = row[columns["仓库类型"]["key"]]
            assert label not in actual and label in expected, "unexpected_or_duplicate_group"
            count = Decimal(row[columns["仓库数量"]["key"]])
            ratio = Decimal(row[columns["数量占比（%）"]["key"]])
            assert count == expected[label], "wrong_group_count"
            assert ratio == Decimal(format(count * 100 / Decimal(total), ".2f")), "wrong_ratio"
            actual[label] = count
    assert actual.keys() == expected.keys(), "missing_groups"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, default=240)
    args = parser.parse_args()
    assert 30 <= args.timeout <= 900, "invalid_observation_timeout"
    _hide_windows_subprocesses()
    bridge = CodexMcpBridge(project_root=ROOT, backend_url="http://127.0.0.1:8010", timeout_seconds=180)
    client = ChatClient("http://127.0.0.1:8021")
    session = client.request("/v1/session")
    with bridge.request_session() as source:
        for index, keyword in enumerate(("沈阳", "长沙", "acceptance_'%_no_such_warehouse"), 1):
            before = validate_doris_result(source.query(sql=REFERENCE_SQL, purpose="independent directory ledger")).rows
            expected = reference_counts(before, keyword)
            question = (f"查询名称包含“{keyword}”的所有仓库，按货主和仓库编码联合去重，"
                        "按仓库类型统计仓库数量和数量占比，同名不同编码分别计数，用中文表格展示。")
            payload = {"conversation_id": session["conversation_id"], "question": question}
            key = "directory-share-acceptance-" + uuid4().hex
            task = client.request("/v1/tasks", payload, session["csrf_token"], key)
            identifier = task["task_id"]
            print(json.dumps({"case": index, "task_id": identifier, "state": task["state"]}), flush=True)
            started = time.monotonic()
            while task["state"] in {"queued", "running"} and time.monotonic() - started < args.timeout:
                time.sleep(2)
                task = client.request("/v1/tasks/" + identifier)
            assert task["state"] == "completed", "task_not_completed"
            result = task["result"]
            assert result["result_state"] == ("live_success" if expected else "no_data"), "wrong_result_state"
            assert "business_tables" not in result, "unrequested_detail_table"
            table = result["business_table"]
            compare(table, complete_rows(client, table), expected)
            after = validate_doris_result(source.query(sql=REFERENCE_SQL, purpose="independent directory stability")).rows
            assert before == after, "source_changed"
            assert client.request("/v1/tasks", payload, session["csrf_token"], key) == task, "idempotent_replay_changed"
            print(json.dumps({"case": index, "task_id": identifier, "verification": "passed",
                              "checks": ["independent_ledger", "compound_identity", "all_groups",
                                         "ratios", "literal_name", "projection", "source_stability", "idempotency"]}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"verification": "failed", "category": type(exc).__name__,
                          "check": str(exc) if type(exc) is AssertionError else None}), flush=True)
        raise SystemExit(1) from None

"""Opt-in live acceptance through the same chat API; never stores result rows.

Uses the installation's canonical conversation. It stops on clarification or
failure rather than submitting more questions into an unresolved conversation.
Only case IDs, states, durations and assertion failures are printed.
"""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
from http.cookiejar import CookieJar
import json
from pathlib import Path
import time
import unicodedata
from urllib.parse import urlparse
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]


def _label(value):
    """Normalize typography, not semantic units or unrelated column meanings."""
    text = "".join(unicodedata.normalize("NFKC", str(value)).split())
    if text.endswith("(%)"):
        text = text[:-3]
    return text


def _number(value):
    if value is None or isinstance(value, bool):
        raise ValueError("missing or ambiguous number")
    number = Decimal(str(value).replace(",", "").strip().removesuffix("%"))
    if not number.is_finite():
        raise ValueError("nonfinite number")
    return number


def check_calculations(case, table):
    """Check public values in memory; never include values in failure output."""
    checks = case.get("checks", [])
    if not checks:
        return []
    rows = table["rows"]
    columns = table["columns"]
    failures = []

    def values(column_index):
        alternatives = {_label(label) for label in case["required_columns"][column_index]}
        key = next(column["key"] for column in columns if _label(column["label"]) in alternatives)
        return [row[key] for row in rows]

    def detail_row_indices():
        # Public result tables may contain a verified total row.  It is a
        # separate aggregate and must not be treated as another partition.
        if not case.get("required_columns"):
            return range(len(rows))
        alternatives = {_label(label) for label in case["required_columns"][0]}
        key = next((column["key"] for column in columns if _label(column["label"]) in alternatives), None)
        if key is None:
            return range(len(rows))
        return [index for index, row in enumerate(rows)
                if str(row.get(key, "")).strip() not in {"合计", "总计", "合计数"}]

    for check in checks:
        try:
            if check == "descending_quantity":
                quantities = list(map(_number, values(1)))
                if quantities != sorted(quantities, reverse=True):
                    raise ValueError("wrong ranking")
            elif check == "positive_integer_count":
                count_values = values(1)
                indices = detail_row_indices()
                if any(n < 1 or n != n.to_integral_value()
                       for n in map(_number, (count_values[index] for index in indices))):
                    raise ValueError("invalid count")
            elif check == "unique_groups":
                dimensions = [values(index) for index in case.get("group_column_indices", [0])]
                groups = [tuple(None if dimension[index] is None else str(dimension[index]).strip()
                                for dimension in dimensions) for index in detail_row_indices()]
                if not dimensions or len(groups) != len(set(groups)):
                    raise ValueError("duplicate business group")
            elif check == "partition_share":
                count_values = values(1)
                share_values = values(2)
                indices = detail_row_indices()
                counts = list(map(_number, (count_values[index] for index in indices)))
                shares = list(map(_number, (share_values[index] for index in indices)))
                total = sum(counts)
                if total <= 0 or abs(sum(shares) - 100) > Decimal("0.01") or any(
                    abs(share - count / total * 100) > Decimal("0.01")
                    for count, share in zip(counts, shares)
                ):
                    raise ValueError("partition share mismatch")
            elif check == "quantity_share":
                quantities = list(map(_number, values(1)))
                shares = list(map(_number, values(3)))
                total = sum(quantities)
                if total == 0 or any(abs(share - quantity / total * 100) > Decimal("0.01")
                                     for quantity, share in zip(quantities, shares)):
                    raise ValueError("share mismatch")
                counts = list(map(_number, values(2)))
                if any(n < 0 or n != n.to_integral_value() for n in counts):
                    raise ValueError("invalid document count")
            elif check == "period_daily_average":
                day_counts = case["period_days"]
                keys = {column["label"]: column["key"] for column in columns}
                if len(day_counts) != 2 or any(type(day) is not int or day < 1 for day in day_counts):
                    raise ValueError("invalid period lengths")
                state_columns = [f"期间{name}记录状态" in keys for name in ("一", "二")]
                if any(state_columns) and not all(state_columns):
                    raise ValueError("incomplete period states")
                has_record_states = all(state_columns)
                for row in rows:
                    averages = []
                    for number, name in enumerate(("一", "二")):
                        quantity = row[keys[f"期间{name}出库数量"]]
                        average = row[keys[f"期间{name}日均量"]]
                        if has_record_states:
                            state = row[keys[f"期间{name}记录状态"]]
                            if state not in {"有记录", "无记录"}:
                                raise ValueError("invalid period state")
                            missing = state == "无记录"
                        else:
                            missing = quantity == "无记录"
                        if missing:
                            if has_record_states and (quantity is not None or average is not None):
                                raise ValueError("missing period replaced")
                            if not has_record_states and average != "无记录":
                                raise ValueError("missing period replaced")
                            averages.append(None)
                        else:
                            if quantity is None or average is None:
                                raise ValueError("record period missing numeric value")
                            calculated = _number(quantity) / day_counts[number]
                            if abs(_number(average) - calculated) > Decimal("0.005"):
                                raise ValueError("wrong day count")
                            averages.append(calculated)
                    change = row[keys["日均变化率"]]
                    left, right = averages
                    if left is None or right is None:
                        if change != "无记录":
                            raise ValueError("missing comparison")
                    elif left == 0:
                        if change != "分母为零":
                            raise ValueError("zero denominator")
                    elif abs(_number(change) - (right - left) / left * 100) > Decimal("0.005"):
                        raise ValueError("wrong change rate")
            else:
                raise ValueError("unknown acceptance check")
        except (ValueError, InvalidOperation, KeyError, TypeError, StopIteration, ZeroDivisionError):
            failures.append(f"calculation_failed_{check}")
    return failures


def check_answer(case, task):
    failures = []
    result = task.get("result") or {}
    if task.get("state") != "completed" or result.get("result_state") != "live_success":
        return ["no_complete_live_answer"]
    if "required_tables" in case:
        return check_table_bundle(case, task)
    table = result.get("business_table")
    if not isinstance(table, dict) or not table.get("rows"):
        return ["missing_business_table"]
    columns = table.get("columns") or []
    labels = {_label(column.get("label", "")) for column in columns}
    for index, alternatives in enumerate(case.get("required_columns", [])):
        if not labels.intersection(map(_label, alternatives)):
            failures.append(f"missing_requested_column_{index + 1}")
    if labels.intersection(map(_label, case.get("forbidden_columns", []))):
        failures.append("wrong_output_grain")
    if case.get("max_rows") and len(table["rows"]) > case["max_rows"]:
        failures.append("wrong_ranking_limit")
    if not result.get("business_result", {}).get("text"):
        failures.append("missing_business_explanation")
    wire = json.dumps(result, ensure_ascii=False).casefold()
    if any(marker in wire for marker in ("ywzt_ods.", "dms_ods.", "select ", "traceback (", "password=")):
        failures.append("internal_details_exposed")
    failures.extend(check_calculations(case, table))
    return failures


def check_table_bundle(case, task):
    result = task["result"]
    tables = result.get("business_tables")
    expected = case["required_tables"]
    if not isinstance(tables, list) or len(tables) != len(expected):
        return ["incomplete_table_bundle"]
    failures = []
    for index, (spec, table) in enumerate(zip(expected, tables)):
        if not isinstance(table, dict) or _label(table.get("title")) not in set(map(_label, spec["titles"])):
            failures.append(f"wrong_table_identity_{index + 1}")
            continue
        if table.get("pagination", {}).get("page_count", 1) > 1:
            failures.append(f"bundle_check_requires_complete_rows_{index + 1}")
        local = {**task, "result": {**result, "business_table": table}}
        failures.extend(f"table_{index + 1}_{error}" for error in check_answer(spec, local))
    if failures:
        return failures
    for check in case.get("bundle_checks", []):
        try:
            if check != "warehouse_detail_partition":
                raise ValueError("unknown bundle check")

            def key(table_index, column_index):
                alternatives = set(map(_label, expected[table_index]["required_columns"][column_index]))
                return next(c["key"] for c in tables[table_index]["columns"] if _label(c["label"]) in alternatives)

            detail_counts, identities = {}, set()
            for row in tables[1]["rows"]:
                identity = (row[key(1, 0)], row[key(1, 1)])
                if any(v is None or not str(v).strip() for v in identity) or identity in identities:
                    raise ValueError("duplicate or missing warehouse identity")
                identities.add(identity)
                if case.get("warehouse_keyword", "") not in str(row[key(1, 2)]):
                    raise ValueError("warehouse scope mismatch")
                category = row[key(1, 3)]
                detail_counts[category] = detail_counts.get(category, 0) + 1
            summary_counts = {row[key(0, 0)]: _number(row[key(0, 1)]) for row in tables[0]["rows"]}
            if summary_counts != detail_counts:
                raise ValueError("summary differs from details")
        except (ValueError, InvalidOperation, KeyError, TypeError, StopIteration):
            failures.append(f"bundle_calculation_failed_{check}")
    return failures


class ChatClient:
    def __init__(self, base_url):
        parsed = urlparse(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("acceptance client requires the local chat service")
        self.base = base_url.rstrip("/")
        self.opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))

    def request(self, path, payload=None, csrf=None, key=None):
        headers = {"Accept": "application/json", "Origin": self.base}
        if csrf:
            headers["X-CSRF-Token"] = csrf
        if key:
            headers["Idempotency-Key"] = key
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        with self.opener.open(Request(self.base + path, body, headers), timeout=20) as response:
            return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8021")
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--timeout", type=int, default=960)
    parser.add_argument("--live", action="store_true", help="execute selected cases in the installation conversation")
    args = parser.parse_args()
    cases = json.loads((ROOT / "tests/evaluations/generalization.json").read_text(encoding="utf-8"))["cases"]
    if args.case:
        if set(args.case) - {case["id"] for case in cases}:
            parser.error("unknown case ID")
        cases = [case for case in cases if case["id"] in args.case]
    if not args.live:
        for case in cases:
            print(json.dumps({"case_id": case["id"], "family": case["family"], "state": "not_run"}))
        return 0
    if not 30 <= args.timeout <= 1800:
        parser.error("timeout must be between 30 and 1800 seconds")
    client = ChatClient(args.base_url)
    for case in cases:
        started = time.monotonic()
        session = client.request("/v1/session")
        task = client.request("/v1/tasks", {"question": case["question"], "conversation_id": session["conversation_id"]},
                              session["csrf_token"], "acceptance-" + uuid4().hex)
        identifier = task["task_id"]
        while task["state"] in {"queued", "running"} and time.monotonic() - started < args.timeout:
            time.sleep(2)
            task = client.request("/v1/tasks/" + identifier)
        failures = check_answer(case, task)
        report = {"case_id": case["id"], "task_id": identifier, "state": task["state"],
                  "elapsed_seconds": round(time.monotonic() - started, 2), "failures": failures}
        print(json.dumps(report), flush=True)
        if failures:
            # An observation timeout does not cancel or restart the actual job.
            return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"state": "acceptance_error", "category": type(exc).__name__}), flush=True)
        raise SystemExit(1) from None

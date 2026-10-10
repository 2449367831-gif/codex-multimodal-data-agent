"""Run representative hard user wording through the live service.

Only task IDs, public states and failure categories are printed. Business rows,
SQL, source names and answer text are intentionally not written to output.
"""

from __future__ import annotations

import argparse
from io import BytesIO
import json
import re
import time
from uuid import uuid4

from PIL import Image

from scripts.evaluate_generalization import ChatClient


CASES = (
    ("yesterday_warehouse_actual", "昨天各仓库实际销售出库量，用表格展示。", "live_success"),
    ("yesterday_top20", "昨天实际销售出库量TOP20的SKU，用表格展示，按实发数量从高到低。", "live_success"),
    ("last_month_turnover", "上个月的库存周转是多少天？", "live_success"),
    ("production_date_inventory", "8月3号生产日期的烤肠在长沙黄兴的库存量，用表格展示。", "live_success"),
    ("dms_redemption", "查询DMS的1-8月积分兑换的空气炸锅有多少。", "live_success"),
    ("fourteen_day_report", "生成中心仓和分仓14天周转数据图片。", "report_success"),
    ("arbitrary_grouped_sales", "查询昨天HJXH生产产品按仓库和SKU汇总实际销售出库数量，并列出出库单数。", "live_success"),
    ("explicit_inventory_formula", "当前可用成品库存加在途除以近14天平均实际销售成品数量，按仓库计算库存周转。", "live_success"),
)


def verify_task(task, expected):
    if expected not in {'live_success', 'report_success'}:
        raise ValueError('unknown acceptance expectation')
    result = task.get('result') if isinstance(task.get('result'), dict) else {}
    if task.get('state') != 'completed' or task.get('http_status') != 200:
        raise ValueError('task did not complete successfully')
    table = result.get('business_table')
    if (result.get('result_state') not in {'live_success', 'no_data', 'summary_only'}
            or result.get('status') != 'completed' or not isinstance(table, dict)
            or not isinstance(table.get('rows'), list) or not table.get('columns')
            or result.get('error') or result.get('clarification', {}).get('required')):
        raise ValueError('missing completed public business result')
    if expected == 'report_success':
        artifacts = result.get('artifacts')
        if (result.get('result_state') != 'live_success' or not table['rows']
                or not isinstance(artifacts, list) or len(artifacts) != 1):
            raise ValueError('report did not produce an image and table')
        artifact = artifacts[0]
        if (not isinstance(artifact, dict) or artifact.get('content_type') != 'image/png'
                or not isinstance(artifact.get('artifact_id'), str)
                or not re.fullmatch(r'art_[A-Za-z0-9_-]{24,80}', artifact['artifact_id'])
                or artifact.get('url') != '/v1/artifacts/' + artifact['artifact_id']):
            raise ValueError('report artifact descriptor invalid')


def verify_report_image(client, artifact):
    limit = 16 * 1024 * 1024
    with client.opener.open(client.base + artifact['url'], timeout=20) as response:
        if response.status != 200 or response.headers.get_content_type() != 'image/png':
            raise ValueError('report image not retrievable')
        content = response.read(limit + 1)
    if len(content) > limit:
        raise ValueError('report image exceeds acceptance limit')
    with Image.open(BytesIO(content)) as image:
        if image.format != 'PNG' or image.width < 1000 or image.height < 100:
            raise ValueError('report image invalid')
        image.verify()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()
    if not 60 <= args.timeout <= 1800:
        parser.error("invalid timeout")
    client = ChatClient("http://127.0.0.1:8021")
    session = client.request("/v1/session")
    for case_id, question, expected in CASES:
        task = client.request("/v1/tasks", {
            "conversation_id": session["conversation_id"],
            "question": question,
        }, session["csrf_token"], "hard-live-" + case_id + "-" + uuid4().hex)
        started = time.monotonic()
        while task["state"] in {"queued", "running"} and time.monotonic() - started < args.timeout:
            time.sleep(2)
            task = client.request("/v1/tasks/" + task["task_id"])
        result = task.get("result") if isinstance(task.get("result"), dict) else {}
        error = result.get("error") if isinstance(result.get("error"), dict) else {}
        table = result.get("business_table") if isinstance(result.get("business_table"), dict) else None
        outcome = result.get("outcome") if isinstance(result.get("outcome"), dict) else {}
        status = {
            "case": case_id,
            "task_id": task.get("task_id"),
            "state": task.get("state"),
            "http_status": task.get("http_status"),
            "result_state": result.get("result_state"),
            "outcome": outcome.get("state"),
            "has_table": table is not None,
            "table_rows": len(table.get("rows", [])) if table else 0,
            "error_code": error.get("code"),
            "elapsed_seconds": round(time.monotonic() - started, 2),
        }
        print(json.dumps(status, ensure_ascii=False), flush=True)
        if task["state"] in {"queued", "running"}:
            raise RuntimeError("case timed out")
        verify_task(task, expected)
        if expected == 'report_success':
            verify_report_image(client, result['artifacts'][0])
        print(json.dumps({'case': case_id, 'verification': 'flow_passed',
                          'image_verified': expected == 'report_success', 'independent_numeric_reconciled': False}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"verification": "failed", "category": type(exc).__name__}), flush=True)
        raise SystemExit(2) from None

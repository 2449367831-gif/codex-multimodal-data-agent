"""T31 running-service selection contract; no business query or row persistence."""

import http.cookiejar
import json
from pathlib import Path
import time
import urllib.request
from uuid import uuid4


def main():
    base = 'http://127.0.0.1:8021'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(path, body=None, headers=None):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type': 'application/json', 'Origin': base, **(headers or {})})
        with opener.open(req, timeout=20) as response:
            return json.load(response)

    def wait(task):
        deadline = time.monotonic() + 60
        while task['state'] in {'queued', 'running'}:
            if time.monotonic() > deadline:
                raise TimeoutError('selection task did not finish within observation window')
            time.sleep(0.5)
            task = request('/v1/tasks/' + task['task_id'])
        return task

    session = request('/v1/session')
    if session.get('clarification_pending'):
        raise ValueError('existing clarification must remain untouched')
    cases = json.loads((Path(__file__).with_name('complex-query-cases.json')).read_text(encoding='utf-8'))['cases']
    question = next(case['question'] for case in cases if case['id'] == 'T31')
    headers = {'X-CSRF-Token': session['csrf_token'], 'Idempotency-Key': 'conflict-first-' + uuid4().hex}
    first = wait(request('/v1/tasks', {'conversation_id': session['conversation_id'], 'question': question}, headers))
    assert first['http_status'] == 200
    card = first['result']['clarification']
    assert card['required'] and [len(q['options']) for q in card['questions']] == [2, 2, 2, 2, 2, 3]
    assert not first['result'].get('business_table')
    resumed = request('/v1/clarification/resume', {'conversation_id': session['conversation_id']}, headers)['clarification']
    assert resumed['questions'] == card['questions'] and resumed['draft_id'] == card['draft_id']
    selected = {'conversation_id': session['conversation_id'], 'clarification_token': resumed['token'],
        'clarification_answers': [{'question_id': key, 'option_ids': [value]} for key, value in zip(
            ('tc_variant', 'tc_stock', 'tc_sales', 'tc_returns', 'tc_scope', 'tc_window'),
            ('tc_a', 'tc_realtime', 'tc_actual', 'tc_keep_returns', 'tc_all', 'tc_custom'))]}
    selected['clarification_answers'][-1]['date_range'] = {'start': '2026-08-01', 'end': '2026-09-01'}
    selected_headers = {**headers, 'Idempotency-Key': 'conflict-selected-' + uuid4().hex}
    task = wait(request('/v1/tasks', selected, selected_headers))
    assert task['http_status'] == 200
    result = task['result']
    checks = {
        'six_decisions': True, 'same_draft_after_rotation': True,
        'date_range_accepted': True,
        'unpublished_not_live': result['result_state'] == 'business_definition_unpublished' and not result.get('business_table'),
        'conflict_explained': '销售区间与方案不一致' in result.get('message', ''),
        'no_repeat_questions': not result['clarification']['required'] and '无需重复选择' in result.get('message', ''),
        'same_conversation': result['conversation_id'] == session['conversation_id'],
        'idempotent': request('/v1/tasks', selected, selected_headers)['task_id'] == task['task_id'],
    }
    print(json.dumps({'case': 'T31', 'checks': checks}), flush=True)
    return 0 if all(checks.values()) else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'error_type': type(exc).__name__}), flush=True)
        raise SystemExit(2)

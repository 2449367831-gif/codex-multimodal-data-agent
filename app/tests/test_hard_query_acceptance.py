from copy import deepcopy
from email.message import Message
from io import BytesIO
from types import SimpleNamespace

from PIL import Image
import pytest

from tests.acceptance.check_hard_queries_live import verify_task, verify_report_image


def successful_task():
    identifier = 'art_' + 'a' * 24
    return {'state': 'completed', 'http_status': 200, 'result': {
        'status': 'completed', 'result_state': 'live_success',
        'business_table': {'columns': [{'key': 'a', 'label': '数量'}], 'rows': [{'a': 1}]},
        'clarification': {'required': False},
        'artifacts': [{'artifact_id': identifier, 'url': '/v1/artifacts/' + identifier,
                       'content_type': 'image/png'}],
    }}


@pytest.mark.parametrize('expected', ['live_success', 'report_success'])
def test_completed_public_results_are_accepted(expected):
    verify_task(successful_task(), expected)


@pytest.mark.parametrize('fault', ['state', 'http', 'result_state', 'status', 'table', 'error', 'clarification'])
@pytest.mark.parametrize('expected', ['live_success', 'report_success'])
def test_failure_or_clarification_never_passes_as_a_business_answer(fault, expected):
    task = successful_task()
    if fault == 'state': task['state'] = 'failed'
    if fault == 'http': task['http_status'] = 502
    if fault == 'result_state': task['result']['result_state'] = 'failed'
    if fault == 'status': task['result']['status'] = 'blocked'
    if fault == 'table': task['result'].pop('business_table')
    if fault == 'error': task['result']['error'] = {'code': 'query_timeout'}
    if fault == 'clarification': task['result']['clarification']['required'] = True
    with pytest.raises(ValueError): verify_task(task, expected)


@pytest.mark.parametrize('fault', ['missing_image', 'empty_table', 'no_data', 'url', 'type', 'id'])
def test_report_requires_both_table_and_png(fault):
    task = successful_task()
    if fault == 'missing_image': task['result'].pop('artifacts')
    if fault == 'empty_table': task['result']['business_table']['rows'] = []
    if fault == 'no_data': task['result']['result_state'] = 'no_data'
    if fault == 'url': task['result']['artifacts'][0]['url'] = 'https://example.com/report.png'
    if fault == 'type': task['result']['artifacts'][0]['content_type'] = 'text/html'
    if fault == 'id': task['result']['artifacts'][0]['artifact_id'] = '../private'
    with pytest.raises(ValueError): verify_task(task, 'report_success')


@pytest.mark.parametrize('bad_image', [False, True])
def test_report_fetch_checks_image_in_memory(bad_image):
    buffer = BytesIO()
    Image.new('RGB', (1000, 100), 'white').save(buffer, format='PNG')
    response = BytesIO(b'not a png' if bad_image else buffer.getvalue())
    response.status = 200
    response.headers = Message()
    response.headers['Content-Type'] = 'image/png'
    artifact = deepcopy(successful_task()['result']['artifacts'][0])
    def open_image(url, **kwargs):
        assert url == 'http://127.0.0.1:8021' + artifact['url']
        return response
    client = SimpleNamespace(base='http://127.0.0.1:8021', opener=SimpleNamespace(open=open_image))
    if bad_image:
        with pytest.raises(OSError): verify_report_image(client, artifact)
    else:
        verify_report_image(client, artifact)

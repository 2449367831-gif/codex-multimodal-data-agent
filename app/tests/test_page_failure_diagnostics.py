from copy import deepcopy

import pytest

from app.agent_core.sales_pages import retrieve_sales_groups
from app.agent_core.semantic_types import SemanticRuntimeError
from app.tests.test_bounded_sales_pages import Pages, SQL


@pytest.mark.parametrize('failure', ['batch_shape', 'page_shape', 'page_length', 'ordinal_changed', 'count_changed', 'content_changed', 'invalid_response'])
def test_page_diagnostic_is_specific_and_not_public(failure):
    pages = Pages(3)

    def many(statements, **kwargs):
        rows = pages.many(statements, **kwargs)
        if pages.passes == 1 and len(pages.calls) == 1:
            return rows
        if failure == 'batch_shape':
            return rows[:-1]
        if failure == 'page_shape':
            return [None for _ in rows]
        if failure == 'page_length':
            return [[] for _ in rows]
        if failure == 'ordinal_changed':
            rows[0][0]['result_ordinal'] += 1
        if failure == 'count_changed':
            rows[0][0]['group_count'] += 1
        if failure == 'content_changed' and pages.passes == 2:
            rows[0][0]['quantity'] = 'different'
        if failure == 'invalid_response':
            rows[0][0]['result_ordinal'] = 'private-invalid-payload'
        return rows

    with pytest.raises(SemanticRuntimeError) as caught:
        retrieve_sales_groups(SQL, pages.query, query_many=many, page_size=1)
    error = caught.value
    assert error.reason == 'sales_page_invalid'
    assert error.page_failure == failure
    assert error.pass_number == (2 if failure == 'content_changed' else 1)
    public = str(error.as_public_dict())
    assert failure not in public and 'private-invalid-payload' not in public
    assert 'page_failure' not in public and 'pass_number' not in public


def test_successful_pages_unchanged_by_diagnostics():
    pages = Pages(7)
    expected = deepcopy(pages.rows)
    assert retrieve_sales_groups(SQL, pages.query, query_many=pages.many, page_size=2) == expected

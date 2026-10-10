"""Every new report binds the current release, not the first loaded snapshot."""

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from app.tests.test_turnover_service import _CountingQueryClient, _ReleasedKnowledge
from app.artifacts import ArtifactStore
from app.turnover_service import TurnoverReportRuntime, TurnoverRuntimeError


def runtime(tmp_path, **kwargs):
    query = _CountingQueryClient()
    return TurnoverReportRuntime(
        project_root=tmp_path, artifact_store=ArtifactStore(tmp_path / 'artifacts'),
        query_client=query, allow_unapproved_contracts=False, **kwargs,
    ), query


def test_new_report_rechecks_current_release_before_doris(tmp_path):
    report, query = runtime(tmp_path)
    previous = _ReleasedKnowledge()
    revoked = Mock(spec=_ReleasedKnowledge)
    revoked.metric.return_value = {
        **previous.metric('test'), 'status': 'candidate',
        'approval': {'state': 'pending_business_approval'},
    }
    first, second = tmp_path / 'release-one', tmp_path / 'release-two'
    with patch('app.turnover_service.active_snapshot', side_effect=[(first, 'one'), (second, 'two')]) as active, \
            patch('app.turnover_service.KnowledgeBase', side_effect=[previous, revoked]) as knowledge:
        report._assert_contract_released()
        with pytest.raises(TurnoverRuntimeError) as caught:
            report.run(principal_hash='test', conversation_id='test', request_id='test')
    assert caught.value.code == 'report_contract_not_released'
    assert caught.value.reason == 'metric_not_active_or_approved'
    assert query.calls == []
    assert active.call_count == 2
    assert [call.args[0] for call in knowledge.call_args_list] == [first, second]
    assert [call.kwargs['trusted_release_sha256'] for call in knowledge.call_args_list] == ['one', 'two']


def test_broken_current_pointer_does_not_fall_back_to_old_release(tmp_path):
    report, query = runtime(tmp_path)
    with patch('app.turnover_service.active_snapshot', side_effect=[(tmp_path, None), ValueError('private path')]), \
            patch('app.turnover_service.KnowledgeBase', return_value=_ReleasedKnowledge()):
        report._assert_contract_released()
        with pytest.raises(TurnoverRuntimeError) as caught:
            report.run(principal_hash='test', conversation_id='test', request_id='test')
    assert caught.value.code == 'report_contract_not_released'
    assert caught.value.reason == 'knowledge_base_unavailable'
    assert 'private path' not in caught.value.public_message
    assert query.calls == []


def test_explicitly_injected_knowledge_remains_request_authority(tmp_path):
    knowledge = _ReleasedKnowledge()
    report, query = runtime(tmp_path, knowledge=knowledge)
    with patch('app.turnover_service.active_snapshot', side_effect=AssertionError('unexpected pointer read')):
        report._assert_contract_released()
        report._assert_contract_released()
    assert report.knowledge is knowledge
    assert knowledge.verify_calls == 2
    assert query.calls == []

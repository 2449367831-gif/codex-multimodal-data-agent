import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from app.codex_mcp_bridge import CodexMcpBridge, CodexMcpBridgeError
from app.tests.test_query_outcomes import payload


@pytest.fixture
def transport(monkeypatch):
    state=SimpleNamespace(opened=[],closed=[],initialized=[],calls=[],initialize_error=None,
                          query_error=None,reply=None,cleanup_error=None,hang_initialize=False,hang_query=False)
    @asynccontextmanager
    async def stdio(server):
        identity=object()
        task=asyncio.current_task()
        state.opened.append((identity,server))
        try:yield identity,identity
        finally:
            assert asyncio.current_task() is task
            state.closed.append(identity)
            if state.cleanup_error:raise state.cleanup_error
    class Session:
        def __init__(self,reader,writer):
            self.identity=reader
        async def __aenter__(self):
            self.task=asyncio.current_task()
            return self
        async def __aexit__(self,*_):
            assert asyncio.current_task() is self.task
        async def initialize(self):
            state.initialized.append(self.identity)
            if state.hang_initialize:await asyncio.Event().wait()
            if state.initialize_error:raise state.initialize_error
        async def call_tool(self,name,arguments,read_timeout_seconds):
            state.calls.append((self.identity,name,arguments,read_timeout_seconds))
            if state.hang_query:await asyncio.Event().wait()
            if state.query_error:raise state.query_error
            return SimpleNamespace(is_error=False,structured_content=deepcopy(state.reply) if state.reply is not None else payload([{'fixture':arguments['purpose']}]))
    monkeypatch.setattr('app.codex_mcp_bridge.stdio_client',stdio)
    monkeypatch.setattr('app.codex_mcp_bridge.ClientSession',Session)
    return state


def bridge(**kwargs):
    return CodexMcpBridge(project_root=Path.cwd(),backend_url='http://127.0.0.1:8010',**kwargs)


def test_pages_reuse_one_initialized_session_without_result_cache(transport):
    parent=bridge()
    with parent.request_session() as query:
        first=query.query(sql='SELECT 1',purpose='first fixture')
        second=query.query(sql='SELECT 2',purpose='second fixture')
        assert first!=second
        assert len(transport.opened)==len(transport.initialized)==1
        assert len(transport.calls)==2 and not transport.closed
    assert len(transport.closed)==1
    assert [call[1] for call in transport.calls]==['query_doris_sql']*2
    assert [call[2]['sql'] for call in transport.calls]==['SELECT 1','SELECT 2']
    assert all(call[3]==95 for call in transport.calls)
    with pytest.raises(CodexMcpBridgeError):query.query(sql='SELECT 3',purpose='expired fixture')
    assert len(transport.calls)==2


def test_distinct_requests_never_reuse_each_others_session(transport):
    parent=bridge()
    def request(label):
        with parent.request_session() as scoped:
            return scoped.query(sql='SELECT 1',purpose=label)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(request,'request A')
        second=pool.submit(request,'request B')
        results=[first.result(timeout=5),second.result(timeout=5)]
    assert len(transport.opened)==len(transport.closed)==2
    assert len({call[0] for call in transport.calls})==2
    assert {response['execution']['rows'][0]['fixture'] for response in results}=={'request A','request B'}


def test_scoped_handle_cannot_cross_request_threads(transport):
    with bridge().request_session() as scoped:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(scoped.query,sql='SELECT 1',purpose='wrong thread')
            with pytest.raises(CodexMcpBridgeError):future.result(timeout=5)
        assert transport.calls==[]
        scoped.query(sql='SELECT 1',purpose='owner thread')
    assert len(transport.closed)==1


@pytest.mark.parametrize('mutation,reason',[
    ({'truncated':True},'result_truncated'),({'is_mock':True},'not_live_result'),
    ({'row_count':4},'row_count_mismatch')])
def test_every_page_is_verified_and_failed_handle_cannot_continue(transport,mutation,reason):
    with bridge().request_session() as scoped:
        scoped.query(sql='SELECT 1',purpose='valid')
        transport.reply=payload()
        transport.reply['execution'].update(mutation)
        with pytest.raises(CodexMcpBridgeError) as failure:scoped.query(sql='SELECT 2',purpose='invalid')
        assert failure.value.reason==reason
        with pytest.raises(CodexMcpBridgeError):scoped.query(sql='SELECT 3',purpose='must not execute')
        assert len(transport.calls)==2


def test_backend_rejection_is_not_transport_success(transport):
    transport.reply={'status':'failed','error':{'code':'sql_safety_error','message':'PRIVATE SQL'}}
    with pytest.raises(CodexMcpBridgeError) as failure:
        with bridge().request_session() as scoped:scoped.query(sql='SELECT 1',purpose='rejected fixture')
    assert failure.value.reason=='backend_rejected'
    assert 'PRIVATE' not in str(failure.value)
    assert len(transport.closed)==1


@pytest.mark.parametrize('phase',['initialize','query'])
def test_timeout_closes_session_and_portal_thread(transport,phase):
    setattr(transport,'hang_'+phase,True)
    before={thread.ident for thread in threading.enumerate()}
    with pytest.raises(CodexMcpBridgeError) as failure:
        with bridge(timeout_seconds=0.03).request_session() as scoped:
            scoped.query(sql='SELECT 1',purpose='timeout fixture')
    assert failure.value.reason=='query_timeout'
    assert len(transport.opened)==len(transport.closed)==1
    assert {thread.ident for thread in threading.enumerate()}==before


@pytest.mark.parametrize('phase',['initialize','query','cleanup'])
def test_transport_and_cleanup_failures_have_no_private_output(transport,phase):
    setattr(transport,phase+'_error',RuntimeError('PRIVATE TRANSPORT DIAGNOSTIC'))
    with pytest.raises(CodexMcpBridgeError) as failure:
        with bridge().request_session() as scoped:scoped.query(sql='SELECT 1',purpose='failure fixture')
    assert failure.value.reason=='backend_unavailable'
    assert 'PRIVATE' not in str(failure.value)
    assert len(transport.opened)==len(transport.closed)==1


def test_business_exception_survives_cleanup_failure(transport):
    transport.cleanup_error=RuntimeError('PRIVATE CLEANUP')
    original=ValueError('business reconciliation fixture')
    with pytest.raises(ValueError) as failure:
        with bridge().request_session():raise original
    assert failure.value is original and len(transport.closed)==1


def test_standalone_query_remains_supported(transport):
    parent=bridge()
    parent.query(sql='SELECT 1',purpose='standalone A')
    parent.query(sql='SELECT 2',purpose='standalone B')
    assert len(transport.opened)==len(transport.closed)==len(transport.initialized)==2


def test_semantic_runtime_owns_session_per_execution(transport,monkeypatch):
    from app.agent_core.handlers.base import MetricHandler
    from app.agent_core.runtime import SemanticQueryRuntime
    from app.agent_core.semantic_types import SemanticRequest
    class Handler(MetricHandler):
        def run(self,request):
            self._execute('SELECT 1',purpose='page A')
            self._execute('SELECT 2',purpose='page B')
            return 'Synthetic completion'
    monkeypatch.setitem(__import__('app.agent_core.runtime',fromlist=['HANDLERS']).HANDLERS,'session_fixture',(Handler,'run'))
    runtime=SemanticQueryRuntime(query_client=bridge())
    plan=SemanticRequest(route='session_fixture',start=None,end=None)
    for _ in range(2):
        result=runtime.execute_plan(plan,question='synthetic request')
        assert result['query_execution']['successful_query_call_count']==2
    assert len(transport.opened)==len(transport.closed)==2 and len(transport.calls)==4


def test_semantic_runtime_startup_failure_keeps_outcome(transport,monkeypatch):
    from app.agent_core.handlers.base import MetricHandler
    from app.agent_core.runtime import SemanticQueryRuntime
    from app.agent_core.semantic_types import SemanticRequest, SemanticRuntimeError
    class Handler(MetricHandler):
        def run(self,request):raise AssertionError('handler must not run after startup failed')
    monkeypatch.setitem(__import__('app.agent_core.runtime',fromlist=['HANDLERS']).HANDLERS,'session_fixture',(Handler,'run'))
    transport.initialize_error=RuntimeError('PRIVATE STARTUP')
    with pytest.raises(SemanticRuntimeError) as failure:
        SemanticQueryRuntime(query_client=bridge()).execute_plan(SemanticRequest(route='session_fixture',start=None,end=None),question='synthetic request')
    assert failure.value.reason=='backend_unavailable'


def test_batch_calls_overlap_but_return_in_order(transport,monkeypatch):
    parent=bridge()
    active=set()
    finished=[]
    original=parent._call_session
    async def call(session,sql,purpose):
        active.add(sql)
        while len(active)<4:await asyncio.sleep(0)
        await asyncio.sleep((4-int(sql[-1]))*.005)
        result=await original(session,sql,purpose)
        result['execution']['rows'][0]['fixture']=sql
        finished.append(sql)
        return result
    monkeypatch.setattr(parent,'_call_session',call)
    statements=['SELECT '+str(i) for i in range(1,5)]
    with parent.request_session() as scoped:
        rows=scoped.query_many(statements=statements,purpose='batch fixture',timeout_seconds=2)
    assert finished==list(reversed(statements))
    assert [row['execution']['rows'][0]['fixture'] for row in rows]==statements
    assert len(transport.opened)==len(transport.closed)==1
    assert {call[1] for call in transport.calls}=={'query_doris_sql'}


@pytest.mark.parametrize('reason',['backend_rejected','query_timeout','result_truncated','not_live_result'])
def test_one_failed_batch_call_cancels_and_joins_siblings(transport,monkeypatch,reason):
    parent=bridge()
    active=set()
    ended=set()
    async def call(session,sql,purpose):
        active.add(sql)
        try:
            while len(active)<4:await asyncio.sleep(0)
            if sql=='SELECT 2':raise CodexMcpBridgeError(reason)
            await asyncio.Event().wait()
        finally:ended.add(sql)
    monkeypatch.setattr(parent,'_call_session',call)
    with parent.request_session() as scoped:
        with pytest.raises(CodexMcpBridgeError) as failure:
            scoped.query_many(statements=['SELECT '+str(i) for i in range(1,5)],purpose='failed batch',timeout_seconds=2)
        assert failure.value.reason==reason and len(ended)==4
        with pytest.raises(CodexMcpBridgeError):scoped.query(sql='SELECT 1',purpose='poisoned handle')
    assert len(transport.closed)==1


def test_batch_deadline_cancels_all_calls_without_portal_leak(transport):
    transport.hang_query=True
    before={thread.ident for thread in threading.enumerate()}
    with pytest.raises(CodexMcpBridgeError) as failure:
        with bridge().request_session() as scoped:
            scoped.query_many(statements=['SELECT 1','SELECT 2'],purpose='batch timeout',timeout_seconds=.03)
    assert failure.value.reason=='query_timeout'
    assert len(transport.calls)==2 and len(transport.closed)==1
    assert {thread.ident for thread in threading.enumerate()}==before


@pytest.mark.parametrize('statements,seconds',[([],1),(['SELECT 1']*5,1),([''],1),(['SELECT 1'],0),
                                              (['SELECT 1'],float('nan')),(['SELECT 1'],float('inf'))])
def test_invalid_batch_rejected_before_tool_calls(transport,statements,seconds):
    with bridge().request_session() as scoped:
        with pytest.raises(ValueError):scoped.query_many(statements=statements,purpose='invalid',timeout_seconds=seconds)
        assert not transport.calls
        scoped.query(sql='SELECT 1',purpose='valid after invalid arguments')


def test_batch_handles_remain_thread_affine_and_expire(transport):
    with bridge().request_session() as scoped:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(scoped.query_many,statements=['SELECT 1'],purpose='wrong thread',timeout_seconds=1)
            with pytest.raises(CodexMcpBridgeError):future.result(timeout=5)
        assert not transport.calls
        scoped.query_many(statements=['SELECT 1'],purpose='owner',timeout_seconds=1)
    with pytest.raises(CodexMcpBridgeError):scoped.query_many(statements=['SELECT 1'],purpose='expired',timeout_seconds=1)
    assert len(transport.calls)==1


def test_real_mcp_dispatch_supports_bounded_concurrent_tool_calls():
    from mcp import Client
    from app.mcp_server import build_mcp_server
    barrier=threading.Barrier(4,timeout=3)
    class Backend:
        def query(self,*,sql,purpose):
            barrier.wait()
            return payload([{'fixture':sql}])
    async def check():
        async with Client(build_mcp_server(Backend())) as client:
            calls=[client.call_tool('query_doris_sql',{'sql':f'SELECT {i}','purpose':'synthetic concurrency'}) for i in range(4)]
            results=await asyncio.wait_for(asyncio.gather(*calls),5)
            assert [result.structured_content['execution']['rows'][0]['fixture'] for result in results]==[f'SELECT {i}' for i in range(4)]
    asyncio.run(check())


@pytest.mark.parametrize('mutation,reason', [({'truncated':True},'result_truncated'),
                                           ({'is_mock':True},'not_live_result'),
                                           ({'row_count':3},'row_count_mismatch')])
def test_batch_verifies_live_evidence_on_every_payload(transport,mutation,reason):
    transport.reply=payload()
    transport.reply['execution'].update(mutation)
    with bridge().request_session() as scoped:
        with pytest.raises(CodexMcpBridgeError) as failure:
            scoped.query_many(statements=['SELECT 1','SELECT 2'],purpose='invalid batch payload',timeout_seconds=1)
        assert failure.value.reason==reason
        before=len(transport.calls)
        with pytest.raises(CodexMcpBridgeError):scoped.query_many(statements=['SELECT 1'],purpose='poisoned',timeout_seconds=1)
        assert len(transport.calls)==before


def test_concurrent_request_batches_keep_separate_sessions(transport):
    parent=bridge()
    barrier=threading.Barrier(2,timeout=3)
    def request(label):
        with parent.request_session() as scoped:
            barrier.wait()
            return scoped.query_many(statements=['SELECT 1']*4,purpose=label,timeout_seconds=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(request,'A')
        second=pool.submit(request,'B')
        results=[first.result(timeout=5),second.result(timeout=5)]
    assert [{row['execution']['rows'][0]['fixture'] for row in batch} for batch in results]==[{'A'},{'B'}]
    owners={}
    for identity,_,arguments,_ in transport.calls:
        owners.setdefault(identity,set()).add(arguments['purpose'])
    assert len(owners)==2 and list(sorted(owners.values(),key=str))==[{'A'},{'B'}]
    assert len(transport.opened)==len(transport.closed)==2


def test_sequence_is_lazy_reuses_one_session_and_discards_all_handles(transport):
    with bridge().query_sequence() as sequence:
        assert not transport.opened
        first = sequence.query(sql='SELECT 1', purpose='first')
        client = sequence._client
        second = sequence.query(sql='SELECT 2', purpose='second')
        assert first != second
        assert len(transport.opened) == len(transport.initialized) == 1
        assert len(transport.calls) == 2 and not transport.closed
    assert len(transport.closed) == 1
    assert sequence._client is sequence._bridge is None
    assert client._session is client._portal is client._bridge is None
    with pytest.raises(CodexMcpBridgeError):
        sequence.query(sql='SELECT 3', purpose='closed')
    assert len(transport.calls) == 2


def test_unused_sequence_does_not_start_mcp(transport):
    with bridge().query_sequence():
        pass
    assert not transport.opened and not transport.calls


@pytest.mark.parametrize('phase', ['initialize', 'query'])
def test_sequence_failure_is_not_replayed_and_next_explicit_call_opens_new_session(transport, phase):
    with bridge().query_sequence() as sequence:
        setattr(transport, phase + '_error', RuntimeError('private failure'))
        with pytest.raises(CodexMcpBridgeError) as failure:
            sequence.query(sql='SELECT 1', purpose='failed')
        assert failure.value.reason == 'backend_unavailable'
        assert len(transport.opened) == len(transport.closed) == 1
        assert len(transport.calls) == (1 if phase == 'query' else 0)
        setattr(transport, phase + '_error', None)
        sequence.query(sql='SELECT 2', purpose='new explicit plan')
        sequence.query(sql='SELECT 3', purpose='next step')
        assert len(transport.opened) == 2
        assert transport.calls[-1][0] is transport.calls[-2][0]
    assert len(transport.closed) == 2


@pytest.mark.parametrize('mutation,reason', [({'truncated':True},'result_truncated'),
    ({'is_mock':True},'not_live_result'), ({'row_count':3},'row_count_mismatch')])
def test_sequence_preserves_payload_failure_without_automatic_recovery(transport, mutation, reason):
    transport.reply = payload()
    transport.reply['execution'].update(mutation)
    with bridge().query_sequence() as sequence:
        with pytest.raises(CodexMcpBridgeError) as failure:
            sequence.query(sql='SELECT 1', purpose='invalid payload')
        assert failure.value.reason == reason
        assert len(transport.calls) == len(transport.closed) == 1
    assert len(transport.calls) == 1


@pytest.mark.parametrize('phase', ['initialize', 'query'])
def test_sequence_timeout_releases_portal_before_model_can_replan(transport, phase):
    before = {thread.ident for thread in threading.enumerate()}
    setattr(transport, 'hang_' + phase, True)
    with bridge(timeout_seconds=.03).query_sequence() as sequence:
        with pytest.raises(CodexMcpBridgeError) as failure:
            sequence.query(sql='SELECT 1', purpose='timeout')
        assert failure.value.reason == 'query_timeout'
        assert {thread.ident for thread in threading.enumerate()} == before
        assert len(transport.opened) == len(transport.closed) == 1


def test_sequence_cleanup_does_not_replace_original_failure(transport):
    original = ValueError('business fixture')
    with pytest.raises(ValueError) as failure:
        with bridge().query_sequence() as sequence:
            sequence.query(sql='SELECT 1', purpose='checked')
            transport.cleanup_error = RuntimeError('private cleanup')
            raise original
    assert failure.value is original and len(transport.closed) == 1


def test_sequence_query_error_survives_cleanup_error(transport):
    transport.reply = {'status':'failed','error':{'code':'sql_safety_error'}}
    transport.cleanup_error = RuntimeError('private cleanup')
    with bridge().query_sequence() as sequence:
        with pytest.raises(CodexMcpBridgeError) as failure:
            sequence.query(sql='SELECT 1', purpose='rejected')
        assert failure.value.reason == 'backend_rejected'
        assert len(transport.calls) == len(transport.closed) == 1


def test_sequence_clean_body_cannot_hide_transport_cleanup_failure(transport):
    with pytest.raises(CodexMcpBridgeError) as failure:
        with bridge().query_sequence() as sequence:
            sequence.query(sql='SELECT 1', purpose='checked')
            transport.cleanup_error = RuntimeError('private cleanup')
    assert failure.value.reason == 'backend_unavailable'


def test_sequence_handles_cannot_cross_threads(transport):
    with bridge().query_sequence() as sequence:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(sequence.query, sql='SELECT 1', purpose='foreign')
            with pytest.raises(CodexMcpBridgeError):
                future.result(timeout=5)
        assert not transport.opened
        sequence.query(sql='SELECT 2', purpose='owner')
    assert len(transport.opened) == len(transport.closed) == 1


def test_simultaneous_sequences_never_share_connections_or_results(transport):
    parent = bridge()
    barrier = threading.Barrier(2, timeout=3)

    def request(owner):
        with parent.query_sequence() as sequence:
            barrier.wait()
            return [sequence.query(sql=f'SELECT {i}', purpose=owner) for i in range(3)]

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(request, 'A'), pool.submit(request, 'B')
        results = [a.result(timeout=5), b.result(timeout=5)]
    assert len(transport.opened) == len(transport.closed) == 2
    assert [{r['execution']['rows'][0]['fixture'] for r in group} for group in results] == [{'A'}, {'B'}]
    assert all(len({call[0] for call in transport.calls if call[2]['purpose'] == owner}) == 1
               for owner in ('A', 'B'))

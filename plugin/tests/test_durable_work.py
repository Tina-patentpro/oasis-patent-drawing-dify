import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from patent_drawing.operation_store import OperationStore
from patent_drawing.workspace import Workspace
from tests.test_duplicate_image_dispatch import staged, successful_reply


class WorkerStopped(BaseException):
    pass


def queue_setup(tmp_path):
    from patent_drawing.work_queue import WorkActor, WorkConfig, WorkQueue

    actor = WorkActor('a' * 32, 'alice', 'conversation-a')
    config = WorkConfig('isolated-app', {name: 'b' * 64 for name in
                        ('plugin', 'dsl', 'agent', 'worker')})
    store = OperationStore(tmp_path)
    return store, WorkQueue(store, config, lease_seconds=0), actor


def attach_intake(w, wid, revision=1):
    w.store.save(w.owner, 'intake:active', {
        'workspace_id': wid, 'revision': revision, 'confirmed_revision': revision,
    })


def imported_vector(store, actor):
    w = Workspace(store, actor.owner)
    text = '控制器与传感器连接。'
    wid = w.ingest([{'name': 'disclosure.txt', 'blob': text.encode()}])['workspace_id']
    state = w.read(wid)
    plan = {
        'schema_version': 'drawing-set-2', 'set_id': 'control', 'revision': 1,
        'source_sha256': state['source_sha256'], 'title': '连接关系', 'patent_type': 'invention',
        'technical_types': state['technical_types'],
        'parts': [{'id': 'control', 'name': '控制器', 'label': '1'}],
        'facts': [{'id': 'connected', 'status': 'confirmed', 'text': text,
                   'evidence': [{'page_id': state['bundle']['pages'][0]['id'], 'quote': text}]}],
        'views': [{'id': ident, 'engine': 'vector', 'requested_type': 'relationship',
                   'delivered_type': 'relationship', 'title': '关系图', 'purpose': '连接关系',
                   'instruction': '示意连接关系', 'parts': ['control'], 'diagram': {
                       'width': 640, 'height': 600, 'primitives': [
                           {'id': 'body', 'type': 'rect', 'part': 'control', 'box': [110, 90, 340, 290]},
                           {'type': 'label', 'part': 'control', 'at': [415, 200],
                            'target': [335, 192], 'target_shape': 'body'}
                       ],
                   }} for ident in ('main', 'second')],
    }
    w.stage(wid, plan)
    attach_intake(w, wid)
    return w, wid


def finish(worker, receipt, queue, actor, wid):
    for _ in range(8):
        result = queue.inspect(actor, wid, task_id=receipt['task_id'])
        if result['job_status'] not in {'queued', 'running'}:
            return result
        assert worker.run_one()
    pytest.fail('task did not become terminal')


def test_submission_response_lost_and_parallel_owner_readback(tmp_path):
    from patent_drawing.work_queue import WorkActor, WorkQueue
    from patent_drawing.work_worker import WorkWorker

    store, queue, alice = queue_setup(tmp_path)
    w, wid = imported_vector(store, alice)
    bob = WorkActor(alice.tenant, 'bob', 'conversation-b')
    other, second_wid = imported_vector(store, bob)
    first = queue.enqueue(alice, wid, 1, 'draw', expected_work=w.work_identity(wid))
    restarted = WorkQueue(OperationStore(tmp_path), queue.config)
    with store.owner_lock(alice.owner):
        # Replaying an already committed admission needs no per-owner lock.
        with ThreadPoolExecutor(1) as executor:
            assert executor.submit(restarted.enqueue, alice, wid, 1, 'draw').result(2)['task_id'] == first['task_id']
            assert executor.submit(restarted.inspect, alice, wid).result(2)['job_status'] == 'queued'
        with pytest.raises(ValueError, match='任务'):
            restarted.inspect(bob, wid, task_id=first['task_id'])
        second = restarted.enqueue(bob, second_wid, 1, 'draw', expected_work=other.work_identity(second_wid))
        with ThreadPoolExecutor(1) as executor:
            worker = WorkWorker(restarted, lambda owner: Workspace(restarted.store, owner))
            assert executor.submit(worker.run_one).result(4)
        assert restarted.inspect(bob, second_wid, task_id=second['task_id'])['completed_view_ids'] == ['main']
        assert restarted.inspect(alice, wid)['completed_view_ids'] == []


def test_restart_after_render_or_package_reads_same_revision(tmp_path, monkeypatch):
    from patent_drawing.work_worker import WorkWorker

    store, queue, actor = queue_setup(tmp_path)
    w, wid = imported_vector(store, actor)
    receipt = queue.enqueue(actor, wid, 1, 'draw')
    worker = WorkWorker(queue, lambda owner: Workspace(store, owner))
    save = store.save
    render_commits = []

    def stopped_after_render(owner, name, value):
        save(owner, name, value)
        if isinstance(value, dict) and value.get('png') and value.get('spec_sha256'):
            render_commits.append(value['id'])
            raise WorkerStopped()

    with monkeypatch.context() as patch:
        patch.setattr(store, 'save', stopped_after_render)
        with pytest.raises(WorkerStopped):
            worker.run_one()
    assert queue.inspect(actor, wid)['job_status'] == 'running'
    assert worker.run_one()
    assert queue.inspect(actor, wid)['completed_view_ids'] == ['main', 'second']
    terminal_job = queue.inspect(actor, wid)
    assert terminal_job['execution_stage'] == 'package'

    def stopped_after_package(owner, name, value):
        save(owner, name, value)
        if name == 'workspace:' + wid and (value.get('current') or {}).get('delivery_snapshot'):
            raise WorkerStopped()

    with monkeypatch.context() as patch:
        patch.setattr(store, 'save', stopped_after_package)
        with pytest.raises(WorkerStopped):
            worker.run_one()
    previous = w.package(wid, export_current=True, revision=1)
    result = finish(worker, receipt, queue, actor, wid)
    assert render_commits == ['main']
    assert result['job_status'] == 'completed'
    assert result['export_revision'] == 1
    assert result['file_hashes']['drawings.pdf'] == hashlib.sha256(previous['pdf']).hexdigest()
    assert queue.files(actor, wid, receipt['task_id'])['drawings.zip'] == previous['zip']


@pytest.mark.parametrize('saved_response', [False, True])
def test_interrupted_image_is_not_charged_twice(tmp_path, monkeypatch, saved_response):
    from patent_drawing.work_queue import WorkActor
    from patent_drawing.work_worker import WorkWorker

    store, queue, _ = queue_setup(tmp_path)
    actor = WorkActor('c' * 32, 'test', 'image-conversation')
    entered = threading.Event()
    continue_model = threading.Event()
    sent = []

    def supplier(request):
        sent.append(request.url.path)
        entered.set()
        assert continue_model.wait(5)
        if not saved_response:
            raise WorkerStopped()
        return successful_reply(request)

    w, wid = staged(store, 'placeholder', httpx.MockTransport(supplier))
    w.owner, actor_owner = actor.owner, w.owner
    # This synthetic fixture deliberately has text-only disclosure and no
    # source page artifact, so copying accepted state has no ownership bypass.
    store.save(actor.owner, 'workspace:' + wid, store.load(actor_owner, 'workspace:' + wid))
    attach_intake(w, wid)
    receipt = queue.enqueue(actor, wid, 1, 'draw', allow_models=True)
    worker = WorkWorker(queue, lambda owner: w)
    with monkeypatch.context() as patch:
        if saved_response:
            def stop_before_sqlite_response_commit(*args):
                raise WorkerStopped()
            patch.setattr(store, '_commit_response', stop_before_sqlite_response_commit)
        with ThreadPoolExecutor(2) as executor:
            future = executor.submit(worker.run_one)
            try:
                assert entered.wait(5)
                snapshot = queue.inspect(actor, wid)
                assert snapshot['job_status'] == 'running'
                assert snapshot['execution_stage'] == 'render'
                assert snapshot['operation_id'].startswith('render_')
                assert queue.enqueue(actor, wid, 1, 'draw')['task_id'] == receipt['task_id']
            finally:
                continue_model.set()
            with pytest.raises(WorkerStopped):
                future.result(8)
    restarted = WorkWorker(queue, lambda owner: Workspace(store, owner))
    result = finish(restarted, receipt, queue, actor, wid)
    assert sent == ['/v1/images/generations'] and w.image_dispatch_count(wid) == 1
    assert result['job_status'] == ('partial' if saved_response else 'unknown')
    if saved_response:
        assert 'main.png' in queue.files(actor, wid, receipt['task_id'])
    else:
        assert result['resume_action'] == 'inspect_original_result'


def test_native_dsl_durable_status_bypasses_admission_and_model_nodes():
    from pathlib import Path
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'chatflows'))
    from build_quality_chatflow import build

    graph = build(figure_routing=True, complex_agent=True, durable_work=True)['workflow']['graph']
    nodes = {row['id']: row['data'] for row in graph['nodes']}
    for name, node in nodes.items():
        if node['type']=='code':
            compile(node['code'], name, 'exec')
    assert not any(row['data']['type'] == 'loop' for row in graph['nodes'])
    assert nodes['q_complex_agent']['agent_parameters']['durable_work']['value'] == 'yes'
    assert nodes['q_agent_enqueue']['tool_parameters']['preparation_json']['value'] == ['q_complex_agent', 'text']
    assert any(edge['source'] == 'q_complex_agent' and edge['sourceHandle'] == 'source'
               and edge['target'] == 'q_agent_enqueue' for edge in graph['edges'])
    assert any(edge['source'] == 'q_complex_agent' and edge['sourceHandle'] == 'fail-branch'
               and nodes[edge['target']]['tool_parameters']['action']['value'] == 'inspect_work'
               for edge in graph['edges'])
    assert nodes['q_inspect_work']['tool_parameters']['action']['value'] == 'inspect_work'
    assert any(edge['source'] == 'q_durable_early_switch' and edge['target'] == 'q_inspect_work'
               for edge in graph['edges'])


def test_claims_chatflow_has_only_claim_intake_and_plan_tools():
    from pathlib import Path
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'chatflows'))
    from build_quality_chatflow import build

    app = build(claims_only=True)
    nodes = {node['id']: node['data'] for node in app['workflow']['graph']['nodes']}
    assert [field['variable'] for field in nodes['q_start']['variables']] == ['materials']
    tools = {node['tool_parameters']['action']['value']: node['tool_parameters']
             for node in nodes.values() if node['type'] == 'tool'}
    assert set(tools) == {'intake', 'admit_work', 'inspect_work'}
    assert tools['intake']['source_mode']['value'] == 'claims_only'
    assert {node['type'] for node in nodes.values()}.isdisjoint({'agent', 'llm'})
    env = {}
    exec(nodes['q_admission_result']['code'], env)
    assert env['main']([{'workspace_id': 'test', 'job_status': 'queued'}])['job_status'] == 'queued'


def test_durable_intake_never_instantiates_inline_classifier(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from dify_plugin.entities.tool import ToolRuntime
    from patent_drawing.work_queue import WorkConfig
    from tests.test_intake import KV
    from tools.workspace import PatentDrawingWorkspace

    config = WorkConfig('isolated-app', {name: 'b' * 64 for name in ('plugin', 'dsl', 'agent', 'worker')})
    monkeypatch.setattr(WorkConfig, 'load', classmethod(lambda cls: config))
    monkeypatch.setenv('PATENT_DRAWING_STATE_ROOT', str(tmp_path))
    monkeypatch.delenv('PATENT_DRAWING_ALLOW_MODELS', raising=False)

    def forbidden_classifier(*args):
        pytest.fail('durable intake constructed the synchronous model client')

    monkeypatch.setattr('patent_drawing.intake_model.IntakeClassifier', forbidden_classifier)
    tool = PatentDrawingWorkspace(
        ToolRuntime(credentials={'openai_api_key': 'configured-but-unused'}, user_id='alice', session_id='turn'),
        SimpleNamespace(storage=KV(), app_id='isolated-app', conversation_id='conversation-a'))
    messages = list(tool.invoke({'action': 'intake', 'context_id': 'conversation-a', 'durable_work': 'yes',
                                 'query': '一种含弹簧的安装结构，控制器读取传感器。'}))
    assert messages[-1].message.json_object['model_calls'] == 0


def test_held_source_keeps_zero_provider_calls_and_local_draw(tmp_path):
    from patent_drawing.work_queue import WorkConfig, WorkQueue
    from patent_drawing.work_worker import WorkWorker

    store, queue, actor = queue_setup(tmp_path)
    workspace, wid = imported_vector(store, actor)
    state = workspace.read(wid)
    config = WorkConfig(queue.config.app_id, queue.config.hashes,
                        source_holds=(state['bundle']['sources'][0]['sha256'],))
    queue = WorkQueue(store, config)
    staged_plan = state.pop('work')
    workspace.save(wid, state)
    result = queue.enqueue(actor, wid, 1, 'plan', query='规划', allow_models=True)
    assert result['job_status'] == 'waiting_input' and result['blocking_state'] == 'source_external_hold'
    assert not queue.candidates()
    state['work'] = staged_plan
    workspace.save(wid, state)
    task = queue.enqueue(actor, wid, 1, 'draw', allow_models=True)
    assert task['job_status'] == 'queued'
    worker = WorkWorker(queue, lambda owner: Workspace(store, owner, allow_models=True))
    result = finish(worker, task, queue, actor, wid)
    assert result['job_status'] == 'completed'
    assert result['model_calls'] == 0



def test_new_linked_confirmation_reaches_receive_after_previous_draw(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from dify_plugin.entities.tool import ToolRuntime
    from patent_drawing.work_queue import WorkActor, WorkConfig
    from patent_drawing.work_worker import WorkWorker
    from tests.test_intake import KV
    from tools.workspace import PatentDrawingWorkspace, namespace

    store, queue, alice = queue_setup(tmp_path)
    kv = KV()
    actor = WorkActor(namespace(store, kv), alice.user, alice.conversation)
    workspace, wid = imported_vector(store, actor)
    initial = queue.enqueue(actor, wid, 1, 'draw')
    worker = WorkWorker(queue, lambda owner: Workspace(store, owner))
    assert finish(worker, initial, queue, actor, wid)['export_revision'] == 1
    monkeypatch.setattr(WorkConfig, 'load', classmethod(lambda cls: queue.config))
    monkeypatch.setenv('PATENT_DRAWING_STATE_ROOT', str(tmp_path))
    monkeypatch.delenv('PATENT_DRAWING_ALLOW_MODELS', raising=False)
    tool = PatentDrawingWorkspace(
        ToolRuntime(credentials={}, user_id=actor.user, session_id='turn'),
        SimpleNamespace(storage=kv, app_id=queue.config.app_id, conversation_id=actor.conversation))

    def call(action, **extras):
        return list(tool.invoke({'durable_work': 'yes', 'action': action, 'context_id': actor.conversation,
                                 'workspace_id': wid, 'expected_revision': '1', **extras}))[-1].message.json_object

    preview = call('receive', query='修改：标号1改为101')['linked_edit']
    assert preview['ready'] and preview['visible_edit_view_ids'] == ['main', 'second']
    assert call('admit_work', query='继续')['task_id'] == initial['task_id']
    for confirmation_code in ('f' * 16, preview['confirmation_code']):
        command = '确认图单并生成 ' + confirmation_code
        assert call('admit_work', query=command)['route'] == 'receive'
        confirmed = call('receive', query=command)
        assert confirmed['confirmed'] == (confirmation_code == preview['confirmation_code'])
    second = call('enqueue_work', work_kind='draw', expected_work_identity=confirmed['work_identity'])
    assert second['task_id'] != initial['task_id']
    assert call('admit_work', query=command)['task_id'] == second['task_id']
    assert finish(worker, second, queue, actor, wid)['export_revision'] == 2
    assert call('admit_work', query=command)['task_id'] == second['task_id']
    assert call('admit_work', query='确认图单并生成 ' + 'f' * 16)['route'] == 'receive'
    assert queue.files(actor, task_id=initial['task_id'])['drawings.zip'] != queue.files(actor, task_id=second['task_id'])['drawings.zip']


def test_native_agent_accepts_saved_preparation_and_reuses_luna_for_control(tmp_path, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    from dify_plugin.entities.tool import ToolRuntime
    from patent_drawing.client import OpenAIImagesClient
    from patent_drawing.figure_routing import DecisionClient, MODEL
    from patent_drawing.text_client import OpenAITextClient
    from patent_drawing.work_queue import WorkActor, WorkConfig, WorkQueue
    from patent_drawing.work_worker import WorkWorker
    from tests.test_duplicate_image_dispatch import policy_file
    from tests.test_intake import KV
    from tools.workspace import PatentDrawingWorkspace

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'agent-plugin'))
    from agent.prepare import ComplexDrawingAgent

    actor = WorkActor('a' * 32, 'alice', 'conversation-a')
    config = WorkConfig('isolated-app', {key: 'b' * 64 for key in ('plugin', 'dsl', 'agent', 'worker')},
                        direct_comparison={actor.user: [actor.conversation]})
    monkeypatch.setattr(WorkConfig, 'load', classmethod(lambda cls: config))
    policy = policy_file(tmp_path, 'mock-model')
    monkeypatch.setenv('PATENT_DRAWING_STATE_ROOT', str(tmp_path / 'state'))
    monkeypatch.setenv('PATENT_DRAWING_ALLOW_MODELS', '1')
    monkeypatch.setenv('PATENT_DRAWING_ISOLATED_WORKER', '1')
    monkeypatch.setenv('PATENT_DRAWING_OPERATOR_POLICY', str(policy))
    store = OperationStore(tmp_path / 'state')
    calls = []
    image_payloads = []

    def supplier(request):
        payload = json.loads(request.content)
        calls.append(request.url.path)
        if request.url.path == '/v1/decisions':
            return httpx.Response(200, headers={'x-request-id': 'req_fixture_luna'}, json={
                'model': MODEL, 'answers': [{'name': 'main', 'type': 'choice', 'choice': 'complex'}]})
        if request.url.path == '/v1/responses':
            scope = json.loads(payload['input'][1]['content'])['scope']
            output = json.dumps({'scope': scope, 'views': {'main': 'identity'}})
            return httpx.Response(200, headers={'x-request-id': 'req_fixture_sol'}, json={
                'status': 'completed', 'output': [{'type': 'message', 'content': [
                    {'type': 'output_text', 'text': output}]}]})
        image_payloads.append(payload)
        return successful_reply(request)

    transport = httpx.MockTransport(supplier)
    original, wid = staged(store, 'agent-fixture', transport)
    state = original.read(wid)
    state['proposal'] = state['work']['plan']
    state['proposal'].pop('visibility_contract', None)
    state['work'] = None
    workspace = Workspace(store, actor.owner)
    workspace.save(wid, state)
    attach_intake(workspace, wid)
    queue = WorkQueue(store, config)
    worker = WorkWorker(queue, lambda owner: Workspace(store, owner, allow_models=True,
        image_client=OpenAIImagesClient('fixture', transport=transport),
        text_client=OpenAITextClient('fixture', transport=transport),
        decision_client=DecisionClient('fixture', transport=transport)))

    kv = KV()
    kv.set('patent-drawing-workspace-namespace-v3', actor.tenant.encode())
    tool = PatentDrawingWorkspace(
        ToolRuntime(credentials={}, user_id=actor.user, session_id='turn'),
        SimpleNamespace(storage=kv, app_id=config.app_id, conversation_id=actor.conversation))
    unbound_tool = PatentDrawingWorkspace(
        ToolRuntime(credentials={}, user_id=actor.user, session_id='agent-call'),
        SimpleNamespace(storage=kv))
    base = {'workspace_id': wid, 'context_id': actor.conversation,
            'expected_revision': '1', 'durable_work': 'yes'}

    def invoke(action, query='', **params):
        messages = list(tool.invoke({**base, 'action': action, 'query': query, 'image_budget': 1, **params}))
        return messages[-1].message.json_object

    route = invoke('admit_work', '继续准备复杂图')
    assert route['job_status'] == 'queued'
    assert worker.run_one()
    route = invoke('admit_work', '继续准备复杂图')
    assert route['route'] == 'agent'
    agent = SimpleNamespace(
        session=SimpleNamespace(app_id=config.app_id, conversation_id=actor.conversation),
        create_text_message=lambda text: text)
    handoff = next(ComplexDrawingAgent._invoke(agent, {**base, 'scope': route['scope']}))
    assert json.loads(handoff) == {'context_id': actor.conversation, 'workspace_id': wid,
                                    'scope': route['scope'], 'expected_revision': '1'}
    agent_request = {**base, 'action': 'enqueue_work', 'work_kind': 'agent_prepare',
                     'scope': route['scope'], 'preparation_json': handoff}
    with pytest.raises(Exception, match='绑定'):
        list(unbound_tool.invoke(agent_request))
    wrong_handoff = {**json.loads(handoff), 'expected_revision': '2'}
    with pytest.raises(Exception, match='交接|修订'):
        invoke('enqueue_work', work_kind='agent_prepare', scope=route['scope'],
               preparation_json=json.dumps(wrong_handoff))
    with pytest.raises(Exception, match='交接'):
        invoke('enqueue_work', work_kind='agent_prepare', scope=route['scope'])
    assert calls == ['/v1/decisions']
    for user, app, conversation in (
            ('different-user', None, actor.conversation),
            (actor.user, 'different-app', actor.conversation),
            (actor.user, config.app_id, 'different-conversation')):
        denied = PatentDrawingWorkspace(
            ToolRuntime(credentials={}, user_id=user, session_id='other'),
            SimpleNamespace(storage=kv, app_id=app, conversation_id=conversation))
        with pytest.raises(Exception, match='授权|绑定'):
            list(denied.invoke(agent_request))
    receipt = list(tool.invoke(agent_request))[-1].message.json_object
    assert receipt['job_status'] == 'queued' and calls == ['/v1/decisions']
    assert list(tool.invoke(agent_request))[-1].message.json_object['task_id'] == receipt['task_id']
    assert worker.run_one()
    scope = {'task_id': receipt['task_id'], 'scope': receipt['scope']}
    accepted = invoke('accept_image_preparation', **scope)
    assert accepted['preparation_status'] == 'accepted'
    assert invoke('accept_image_preparation', **scope)['preparation_status'] == 'accepted'
    fork = queue.fork_comparison(actor, wid)
    invoke('receive', '确认图单', require_image_route='agent')
    direct = invoke('admit_work', '确认图单（直连对照）')
    assert direct['workspace_id'] == fork['branch_workspace_id']
    a = invoke('admit_work', '生成')
    assert finish(worker, a, queue, actor, wid)['job_status'] == 'partial'
    b = invoke('admit_work', '生成（直连对照）')
    assert finish(worker, b, queue, actor, direct['workspace_id'])['job_status'] == 'partial'
    assert calls == ['/v1/decisions', '/v1/responses', '/v1/images/generations', '/v1/images/generations']
    a_payload, b_payload = image_payloads
    assert a_payload['model'] == b_payload['model'] == 'gpt-image-2.6-sunburst'
    assert a_payload.pop('prompt') != b_payload.pop('prompt')
    assert a_payload == b_payload


@pytest.mark.parametrize(('app', 'conversation'), [(None, None), (None, 'conversation-a'),
        ('different-app', 'conversation-a'), ('isolated-app', None), ('isolated-app', 'different-conversation')])
def test_native_tool_requires_trusted_app_and_conversation(tmp_path, monkeypatch, app, conversation):
    from types import SimpleNamespace
    from dify_plugin.entities.tool import ToolRuntime
    from patent_drawing.work_queue import WorkConfig
    from tests.test_intake import KV
    from tools.workspace import PatentDrawingWorkspace

    _, queue, actor = queue_setup(tmp_path)
    monkeypatch.setattr(WorkConfig, 'load', classmethod(lambda cls: queue.config))
    monkeypatch.setenv('PATENT_DRAWING_STATE_ROOT', str(tmp_path))
    tool = PatentDrawingWorkspace(
        ToolRuntime(credentials={'work_app_id': queue.config.app_id}, user_id=actor.user, session_id='turn'),
        SimpleNamespace(storage=KV(), app_id=app, conversation_id=conversation))
    with pytest.raises(Exception, match='绑定'):
        list(tool.invoke({'action': 'inspect_work', 'context_id': actor.conversation, 'durable_work': 'yes'}))


def test_confirmed_intake_fast_path_preserves_model_evidence(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from dify_plugin.entities.tool import ToolRuntime
    from patent_drawing.intake import Intake
    from patent_drawing.work_queue import WorkActor, WorkConfig
    from tests.test_intake import KV
    from tools.workspace import PatentDrawingWorkspace, namespace

    store, queue, actor = queue_setup(tmp_path)
    kv = KV()
    current_actor = WorkActor(namespace(store, kv), actor.user, actor.conversation)
    workspace, wid = imported_vector(store, current_actor)
    monkeypatch.setattr(WorkConfig, 'load', classmethod(lambda cls: queue.config))
    monkeypatch.setenv('PATENT_DRAWING_STATE_ROOT', str(tmp_path))
    monkeypatch.delenv('PATENT_DRAWING_ALLOW_MODELS', raising=False)
    tool = PatentDrawingWorkspace(ToolRuntime(credentials={}, user_id=actor.user, session_id='turn'),
                                   SimpleNamespace(storage=kv, app_id=queue.config.app_id,
                                                   conversation_id=actor.conversation))
    common = {'action': 'intake', 'durable_work': 'yes', 'context_id': actor.conversation}

    def run(**params):
        return list(tool.invoke({**common, **params}))[-1].message.json_object

    active = store.load(current_actor.owner, 'intake:active')
    store.save(current_actor.owner, 'intake:active', {**Intake(workspace).new(), **active,
        'bundle': workspace.read(wid)['bundle'],
        'fields': {name: {'value': value} for name, value in (
            ('patent_type', 'invention'), ('technical_type', 'electrical'), ('application_stage', 'draft'))}})
    receipt = run(query='导入计划', workspace_id=wid, expected_revision='1')
    assert receipt['route'] == 'forward'
    assert receipt['model_calls'] == 0 and receipt['model_execution_enabled'] is False
    messages = list(tool.invoke({'action': 'admit_work', 'durable_work': 'yes', 'context_id': actor.conversation,
                                 'workspace_id': wid, 'expected_revision': '1',
                                 'query': '导入计划', 'plan_json': '{}'}))
    admission = messages[-1].message.json_object
    assert admission['route'] == 'receive'
    assert admission['model_calls'] == 0 and admission['model_execution_enabled'] is False


def test_task_readback_survives_invalid_model_policy(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from dify_plugin.entities.tool import ToolRuntime
    from patent_drawing.work_queue import WorkActor, WorkConfig
    from tests.test_intake import KV
    from tools.workspace import PatentDrawingWorkspace, namespace

    store, queue, actor = queue_setup(tmp_path)
    kv = KV()
    scoped = WorkActor(namespace(store, kv), actor.user, actor.conversation)
    workspace, wid = imported_vector(store, scoped)
    receipt = queue.enqueue(scoped, wid, 1, 'draw', expected_work=workspace.work_identity(wid))
    monkeypatch.setattr(WorkConfig, 'load', classmethod(lambda cls: queue.config))
    monkeypatch.setenv('PATENT_DRAWING_STATE_ROOT', str(tmp_path))
    monkeypatch.setenv('PATENT_DRAWING_ALLOW_MODELS', '1')
    monkeypatch.setenv('PATENT_DRAWING_ISOLATED_WORKER', '1')
    monkeypatch.setenv('PATENT_DRAWING_OPERATOR_POLICY', str(tmp_path / 'missing.json'))
    tool = PatentDrawingWorkspace(
        ToolRuntime(credentials={}, user_id=actor.user, session_id='turn'),
        SimpleNamespace(storage=kv, app_id=queue.config.app_id, conversation_id=actor.conversation))
    result = list(tool.invoke({'action': 'inspect_work', 'context_id': actor.conversation,
                               'query': '查看任务 ' + receipt['task_id']}))[-1].message.json_object
    assert result['task_id'] == receipt['task_id'] and result['model_calls'] == 0

"""Regression for intake through the public SDK boundary, with no network."""
from types import SimpleNamespace

import pytest
from dify_plugin.entities.tool import ToolRuntime

from patent_drawing.operation_store import OperationStore
from tools.workspace import PatentDrawingWorkspace


class KV:
    def __init__(self):
        self.items = {}

    def exist(self, key):
        return key in self.items

    def get(self, key):
        return self.items[key]

    def set(self, key, value):
        self.items[key] = value


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('PATENT_DRAWING_STATE_ROOT', str(tmp_path / 'state'))
    monkeypatch.delenv('PATENT_DRAWING_ALLOW_MODELS', raising=False)
    return PatentDrawingWorkspace(
        ToolRuntime(credentials={}, user_id='alice', session_id='invocation'),
        SimpleNamespace(storage=KV()))


def call(client, query, revision='', **params):
    messages = list(client.invoke({'action': 'intake', 'context_id': 'case-a',
        'query': query, 'expected_revision': revision, **params}))
    return messages[-1].message.json_object


def test_first_description_and_short_answers_never_become_new_material(client):
    first = call(client, '弹簧支承安装板，希望补充结构关系图。')
    assert first['workspace_id'] == ''
    assert first['technical_type'] == 'mechanical'
    assert first['pending_question'] == 'patent_type'
    second = call(client, '发明', first['intake_revision'])
    assert second['pending_question'] == 'application_stage'
    third = call(client, '还没提交', second['intake_revision'])
    assert third['application_stage'] == 'draft'
    confirmed = call(client, '确认', third['intake_revision'])
    assert confirmed['workspace_id'] and confirmed['route'] == 'reply'
    preview = call(client, '预览', confirmed['intake_revision'])
    assert preview['route'] == 'forward'
    assert preview['workspace_id'] == confirmed['workspace_id']


def test_attachment_only_is_retained_without_reupload(client, monkeypatch):
    monkeypatch.setattr('tools.workspace.read_file', lambda *args: '电路控制电压输出。'.encode())
    first = call(client, '', files=[{'filename': 'source.txt'}])
    assert first['workspace_id'] == '' and first['technical_type'] == 'electrical'
    second = call(client, '实用新型', first['intake_revision'])
    third = call(client, '已提交', second['intake_revision'])
    final = call(client, '确认', third['intake_revision'])
    assert final['workspace_id'] and final['application_stage'] == 'filed'
    assert final['model_calls'] == 0


def test_natural_plan_request_reaches_workspace_after_source_confirmation(client):
    request = '规划一套专利附图。请用图片表达结构关系，辅助图用线图。'
    first = call(client, '弹簧安装板。', patent_type='invention', application_stage='draft')
    pending = call(client, request, first['intake_revision'])
    assert pending['route'] == 'reply' and not pending['workspace_id']
    accepted = call(client, '确认', pending['intake_revision'])
    assert accepted['workspace_id']
    result = call(client, request, accepted['intake_revision'])
    assert result['route'] == 'forward'
    assert result['workspace_id'] == accepted['workspace_id'] and result['model_calls'] == 0
    polite = call(client, '请帮我规划一组附图，先看候选图单。', result['intake_revision'])
    assert polite['route'] == 'forward' and polite['model_calls'] == 0
    for query in ('先不要规划一套专利附图。', '规划一套附图，但先不调用模型。', '要规划一套专利附图吗？'):
        refused = call(client, query, polite['intake_revision'])
        assert refused['route'] == 'reply' and refused['model_calls'] == 0


def test_mechanical_interlock_and_circuit_are_inferred_together(client):
    material = '配电盘的机械互锁机构通过凸轮限制控制杆，电路检测电流。'
    first = call(client, '', materials=material)
    assert first['technical_type'] == 'mechanical,electrical'
    correction = call(client, '技术领域：电气电路', first['intake_revision'])
    assert correction['technical_type'] == 'electrical'


def test_continue_during_collection_and_stale_confirm_cannot_execute(client):
    first = call(client, '弹簧支承安装板。')
    answer = call(client, '继续', first['intake_revision'])
    assert answer['route'] == 'reply' and not answer['workspace_id']
    latest = call(client, '发明', answer['intake_revision'])
    stale = call(client, '确认', first['intake_revision'])
    assert stale['intake_revision'] == latest['intake_revision']
    assert stale['workspace_id'] == '' and stale['route'] == 'reply'


def test_confirm_replay_is_idempotent_and_correction_invalidates(client, tmp_path):
    first = call(client, '弹簧安装板。', patent_type='invention', application_stage='draft')
    final = call(client, '确认', first['intake_revision'], operation_id='confirm-once')
    replay = call(client, '确认', first['intake_revision'], operation_id='confirm-once')
    assert replay == final
    changed = call(client, '更正：已经提交申请', final['intake_revision'])
    assert changed['application_stage'] == 'filed'
    assert changed['workspace_id'] == ''
    assert call(client, '生成', changed['intake_revision'])['route'] == 'reply'


def test_intake_is_owner_bound(client):
    first = call(client, '弹簧安装板。')
    other = PatentDrawingWorkspace(
        ToolRuntime(credentials={}, user_id='bob', session_id='invocation'), client.session)
    result = call(other, '继续', first['intake_revision'])
    assert not result['workspace_id'] and not result['technical_type']


def test_document_mentions_do_not_confirm_application_facts(client):
    result = call(client, '', materials='背景技术：已有发明专利已提交，公开一种弹簧安装板。')
    assert result['patent_type'] == '' and result['application_stage'] == ''
    assert result['pending_question'] == 'patent_type'


def test_new_material_requires_explicit_replacement(client):
    first = call(client, '弹簧安装板。')
    result = call(client, '预览', first['intake_revision'], materials='电路电压控制。')
    assert result['technical_type'] == 'mechanical'
    assert '更新材料' in result['message']
    updated = call(client, '更新材料', result['intake_revision'], materials='电路电压控制。')
    assert updated['technical_type'] == 'electrical' and not updated['workspace_id']


def test_existing_r21_workspace_can_export_directly(client):
    old = list(client.invoke({'action': 'receive', 'context_id': 'case-a',
        'query': '预览', 'materials': '弹簧安装板。'}))[-1].message.json_object
    result = call(client, '导出工程示意', workspace_id=old['workspace_id'])
    assert result['workspace_id'] == old['workspace_id'] and result['route'] == 'forward'


def test_missing_revision_after_reconnect_reads_state_without_mutation(client):
    first = call(client, '弹簧安装板。')
    result = call(client, '发明')
    assert result['patent_type'] == ''
    assert result['intake_revision'] == first['intake_revision']


def test_unknown_answer_moves_to_next_question(client):
    first = call(client, '弹簧安装板。')
    result = call(client, '不知道', first['intake_revision'])
    assert result['patent_type'] == ''
    assert result['pending_question'] == 'application_stage'


def test_form_body_repeated_by_chatflow_does_not_undo_replacement(client):
    first = call(client, '预览', materials='弹簧安装板。')
    changed = call(client, '更新材料', first['intake_revision'], materials='电路控制电压。')
    answer = call(client, '发明', changed['intake_revision'], materials='电路控制电压。')
    assert answer['patent_type'] == 'invention' and answer['technical_type'] == 'electrical'


def test_operation_replay_cannot_restore_a_reset_session(client):
    first = call(client, '弹簧安装板。', patent_type='invention', application_stage='draft')
    done = call(client, '确认', first['intake_revision'], operation_id='old-confirm')
    reset = call(client, '重置', done['intake_revision'])
    replay = call(client, '确认', first['intake_revision'], operation_id='old-confirm')
    assert replay['workspace_id'] == ''
    assert replay['intake_id'] == reset['intake_id']


def test_inferred_field_retains_exact_page_and_quote(client, tmp_path):
    first = call(client, '电机带动齿轮与传动机构，支承固定在安装板上。')
    assert first['technical_type'] == 'mechanical'
    from tools.workspace import namespace
    store = OperationStore(tmp_path / 'state')
    owner = namespace(store, client.session.storage) + ':alice:case-a'
    state = store.load(owner, 'intake:active')
    evidence = state['fields']['technical_type']['evidence'][0]
    page = next(p for p in state['bundle']['pages'] if p['id'] == evidence['page_id'])
    assert page['text'][evidence['start']:evidence['end']] == evidence['quote']
    assert evidence['source_sha256'] == page['source_sha256']


def test_quoted_prior_application_does_not_establish_current_application_type(client):
    result = call(client, '背景技术引用的文件申请发明专利。本案是一种弹簧安装板。')
    assert result['patent_type'] == ''


def test_figure_detection_is_shown_as_a_candidate_for_text_only_disclosure(client):
    first = call(client, '预览', materials='附图说明：图1为电路图，另有图2。')
    assert '待核' in first['message']


def test_model_suggestion_is_source_bound_and_never_confirms_patent_facts(client, monkeypatch):
    from patent_drawing import intake_model
    requests = []
    def classify(self, text, fingerprint):
        requests.append((text, fingerprint))
        return {'state': 'suggested', 'technical_type': 'mechanical', 'model_calls': 1}
    monkeypatch.setattr(intake_model.IntakeClassifier, 'classify', classify)
    first = call(client, '这是一个由支撑件和连接件构成的组件。')
    assert first['technical_type'] == 'mechanical' and first['model_calls'] == 1
    assert first['patent_type'] == '' and first['application_stage'] == ''
    second = call(client, '发明', first['intake_revision'])
    assert second['model_calls'] == 0 and len(requests) == 1


def test_reset_does_not_reapply_old_start_form_or_body(client):
    form = {'patent_type': 'invention', 'application_stage': 'draft', 'materials': '弹簧安装板。'}
    first = call(client, '预览', **form)
    reset = call(client, '重置', first['intake_revision'], **form)
    assert reset['patent_type'] == '' and reset['application_stage'] == ''
    next_case = call(client, '电路控制电压。', reset['intake_revision'], **form)
    assert next_case['technical_type'] == 'electrical' and next_case['patent_type'] == ''


def test_reset_with_real_chatflow_workspace_does_not_reimport_old_case(client):
    first = call(client, '弹簧安装板。', patent_type='invention', application_stage='draft')
    done = call(client, '确认', first['intake_revision'])
    reset = call(client, '重置', done['intake_revision'], workspace_id=done['workspace_id'])
    assert reset['workspace_id'] == '' and reset['patent_type'] == ''


def test_old_forward_receipt_cannot_execute_after_correction(client):
    first = call(client, '弹簧安装板。', patent_type='invention', application_stage='draft')
    done = call(client, '确认', first['intake_revision'])
    forward = call(client, '生成', done['intake_revision'], operation_id='old-forward')
    assert forward['route'] == 'forward'
    changed = call(client, '更正：已经提交申请', done['intake_revision'])
    replay = call(client, '生成', done['intake_revision'], operation_id='old-forward')
    assert replay['route'] == 'reply' and replay['workspace_id'] == ''
    assert replay['intake_revision'] == changed['intake_revision']


def test_submission_for_testing_does_not_confirm_patent_stage(client):
    first = call(client, '这个弹簧支承装置已经提交检测，准备申请发明。')
    assert first['application_stage'] == ''


def test_natural_technical_answer_and_explicit_supplement_are_retained(client, tmp_path):
    first = call(client, '有一个组件。', patent_type='invention', application_stage='draft')
    answer = call(client, '结构连接', first['intake_revision'])
    assert answer['technical_type'] == 'mechanical'
    supplement = call(client, '补充：固定架通过安装销连接托板，包含一组弹簧。', answer['intake_revision'])
    assert supplement['source_fingerprint'] != answer['source_fingerprint']
    done = call(client, '确认', supplement['intake_revision'])
    from tools.workspace import namespace
    from patent_drawing.workspace import Workspace
    store = OperationStore(tmp_path / 'state')
    owner = namespace(store, client.session.storage) + ':alice:case-a'
    text = Workspace(store, owner).read(done['workspace_id'])['bundle']['text']
    assert '有一个组件' in text and '固定架通过安装销' in text


def test_migration_with_identical_explicit_form_values_keeps_workspace(client):
    old = list(client.invoke({'action': 'receive', 'context_id': 'case-a',
        'query': '预览', 'materials': '弹簧安装板。'}))[-1].message.json_object
    result = call(client, '导出工程示意', workspace_id=old['workspace_id'],
                  patent_type='invention', application_stage='draft', technical_type='mechanical')
    assert result['workspace_id'] == old['workspace_id'] and result['route'] == 'forward'


def test_unlocated_figure_reference_cannot_commit_as_no_figures(client):
    first = call(client, '预览', materials='附图说明：图1为电路图。',
                 patent_type='invention', application_stage='draft')
    pending = call(client, '确认', first['intake_revision'])
    assert pending['workspace_id'] == '' and pending['pending_question'] == 'figure_presence'
    clarified = call(client, '没有附图', pending['intake_revision'])
    final = call(client, '确认', clarified['intake_revision'])
    assert final['workspace_id']


def test_same_material_request_replay_has_stable_identity(client):
    first = call(client, '预览', operation_id='initial-replay', materials='弹簧安装板。')
    replay = call(client, '预览', operation_id='initial-replay', materials='弹簧安装板。')
    assert replay == first


def test_technical_description_answer_becomes_source_evidence(client):
    first = call(client, '一种辅助组件。', patent_type='invention', application_stage='draft')
    answer = call(client, '固定架通过安装销连接托板，包含一组弹簧。', first['intake_revision'])
    assert answer['technical_type'] == 'mechanical'
    assert answer['source_fingerprint'] != first['source_fingerprint']


@pytest.mark.parametrize(('reply', 'supplemental'), [
    ('技术领域：软件/计算机方法', False),
    ('技术领域：软件/计算机方法。系统根据同父叶节点更新分隔键和指针。', True),
])
def test_technical_domain_reply_only_changes_the_source_when_it_adds_technical_facts(
    client, monkeypatch, tmp_path, reply, supplemental,
):
    original = '该方案处理一种节点。'
    monkeypatch.setattr('tools.workspace.read_file', lambda *args: original.encode())
    first = call(client, '预览', files=[{'filename': 'source.txt'}],
                 patent_type='invention', application_stage='draft')
    assert first['pending_question'] == 'technical_type'
    answer = call(client, reply, first['intake_revision'])
    assert answer['technical_type'] == 'computer_method'
    assert (answer['source_fingerprint'] != first['source_fingerprint']) is supplemental
    confirmed = call(client, '确认', answer['intake_revision'])
    from patent_drawing.workspace import Workspace
    from tools.workspace import namespace
    store = OperationStore(tmp_path / 'state')
    owner = namespace(store, client.session.storage) + ':alice:case-a'
    bundle = Workspace(store, owner).read(confirmed['workspace_id'])['bundle']
    assert [page['text'] for page in bundle['pages']] == ([original, reply] if supplemental else [original])


@pytest.mark.parametrize('action', ['receive', 'step', 'package', 'preflight'])
def test_downstream_node_rechecks_revision_after_interleaved_correction(client, monkeypatch, action):
    first = call(client, '弹簧安装板。', patent_type='invention', application_stage='draft')
    done = call(client, '确认', first['intake_revision'])
    call(client, '更正：已经提交申请', done['intake_revision'])
    monkeypatch.setattr('patent_drawing.workspace.Workspace.resume', lambda *a: pytest.fail('stale execution resumed'))
    result = list(client.invoke({'action': action, 'context_id': 'case-a', 'query': '生成',
        'workspace_id': done['workspace_id'], 'expected_revision': done['intake_revision']}))[-1].message.json_object
    assert result['route'] == 'reply'
    assert result['blocking_state'] == 'intake_revision_changed'
    assert result['model_calls'] == 0


@pytest.mark.parametrize('text', [
    '发明人提出一种弹簧支承安装板。',
    '发明专利公开了一种弹簧支承安装板。',
    '本案已经提交检测，准备申请发明。',
    '不准备申请发明，目前只有弹簧结构说明。',
])
def test_roles_prior_patents_and_non_application_submission_do_not_confirm_facts(client, text):
    result = call(client, text)
    assert result['application_stage'] == ''
    if '准备申请发明' not in text or text.startswith('不'):
        assert result['patent_type'] == ''


def test_attachment_caption_is_retained_as_original_user_request(client, monkeypatch, tmp_path):
    monkeypatch.setattr('tools.workspace.read_file', lambda *args: '弹簧支承安装板。'.encode())
    first = call(client, '请补充一张展示四个支承连接关系的图。', files=[{'filename': 'disclosure.txt'}])
    from tools.workspace import namespace
    store = OperationStore(tmp_path / 'state')
    state = store.load(namespace(store, client.session.storage) + ':alice:case-a', 'intake:active')
    assert '请补充一张展示四个支承连接关系的图。' in state['intake_requests']
    assert '请补充一张' not in state['bundle']['text']
    assert first['workspace_id'] == ''


@pytest.mark.parametrize('confirmation', ['确认图单', '确认图单并生成', '确认图单并生成 0123456789abcdef'])
def test_attachment_confirmation_preserves_the_confirmed_intake(client, monkeypatch, tmp_path, confirmation):
    from tools.workspace import namespace

    monkeypatch.setattr('tools.workspace.read_file', lambda *args: '电路控制电压输出。'.encode())
    files = [{'filename': 'disclosure.txt'}]
    request = '请补充一张电路控制方向图。'
    first = call(client, request, files=files, patent_type='invention', application_stage='draft')
    ready = call(client, '确认', first['intake_revision'])
    confirmed = call(client, confirmation, ready['intake_revision'], files=files)
    store = OperationStore(tmp_path / 'state')
    state = store.load(namespace(store, client.session.storage) + ':alice:case-a', 'intake:active')

    assert confirmed['workspace_id'] == ready['workspace_id']
    assert confirmed['intake_revision'] == ready['intake_revision']
    assert confirmed['route'] == 'forward'
    assert state['intake_requests'] == [request]
    assert confirmed['model_calls'] == 0


def test_supplementary_attachment_keeps_original_disclosure(client, monkeypatch, tmp_path):
    monkeypatch.setattr('tools.workspace.read_file', lambda *args: '电路检测电流。'.encode())
    first = call(client, '弹簧支承安装板。')
    updated = call(client, '补充：电气部分见新增附件。', first['intake_revision'], files=[{'filename': 'additional.txt'}])
    from tools.workspace import namespace
    store = OperationStore(tmp_path / 'state')
    state = store.load(namespace(store, client.session.storage) + ':alice:case-a', 'intake:active')
    assert '弹簧支承安装板' in state['bundle']['text'] and '电路检测电流' in state['bundle']['text']
    assert set(updated['technical_type'].split(',')) == {'mechanical', 'electrical'}


def test_confirmed_attachment_request_reaches_planner_without_becoming_source_fact(client, monkeypatch, tmp_path):
    import json
    from patent_drawing.workspace import Workspace
    from tools.workspace import namespace
    monkeypatch.setattr('tools.workspace.read_file', lambda *args: '弹簧支承安装板。'.encode())
    query = '请补充一张展示支承连接关系的图。'
    first = call(client, query, files=[{'filename': 'disclosure.txt'}],
                 patent_type='invention', application_stage='draft')
    done = call(client, '确认', first['intake_revision'])
    store = OperationStore(tmp_path / 'state')
    owner = namespace(store, client.session.storage) + ':alice:case-a'
    seen = {}
    class Capture:
        def plan(self, system, user, **kwargs):
            seen.update(json.loads(user))
            raise RuntimeError('stopped_after_request_capture')
    workspace = Workspace(store, owner, allow_models=True, text_client=Capture())
    with pytest.raises(RuntimeError, match='stopped_after_request_capture'):
        workspace.plan(done['workspace_id'], '规划')
    assert seen['intake_request'] == query
    assert query not in seen['source_bundle']['text']


def test_partial_confirmation_cannot_bind_new_fields_to_an_old_workspace(client, monkeypatch, tmp_path):
    from tools.workspace import namespace
    from patent_drawing.workspace import Workspace
    first = call(client, '弹簧支承安装板。', patent_type='invention')
    original_save = OperationStore.save
    failed = False
    def fail_final_receipt(self, owner, name, value):
        nonlocal failed
        if name == 'intake:active' and value.get('workspace_id') and not failed:
            failed = True
            raise OSError('simulated receipt interruption')
        return original_save(self, owner, name, value)
    monkeypatch.setattr(OperationStore, 'save', fail_final_receipt)
    with pytest.raises(Exception):
        call(client, '确认', first['intake_revision'], application_stage='draft')
    monkeypatch.setattr(OperationStore, 'save', original_save)
    store = OperationStore(tmp_path / 'state')
    owner = namespace(store, client.session.storage) + ':alice:case-a'
    current = store.load(owner, 'intake:active')
    done = call(client, '确认', str(current['revision']), application_stage='filed')
    actual = Workspace(store, owner).read(done['workspace_id'])
    assert actual['application_stage'] == done['application_stage'] == 'filed'


def test_empty_partial_export_returns_current_version_option_without_dispatch(client, tmp_path):
    from patent_drawing.workspace import Workspace

    received = list(client.invoke({'action': 'receive', 'context_id': 'case-a', 'query': '预览',
                                  'materials': '电路包含电源。设备外形未知。'}))[-1].message.json_object
    wid = received['workspace_id']
    owner = client.session.storage.items['patent-drawing-workspace-namespace-v3'].decode() + ':alice:case-a'
    workspace = Workspace(OperationStore(tmp_path / 'state'), owner)
    source = workspace.read(wid)['source_sha256']
    stale = '图1实际底图尚未生成并核对。'
    plan = {
        'schema_version': 'drawing-set-2', 'set_id': 'drawings', 'revision': 1, 'source_sha256': source,
        'title': '电路', 'patent_type': 'invention', 'technical_types': ['electrical'],
        'parts': [{'id': 'supply', 'label': '10', 'name': '电源'}], 'facts': [],
        'unresolved': [stale, '设备外形未知。'], 'constraints': [],
        'views': [{'id': 'overview', 'title': '总图', 'purpose': '表达电源',
                   'engine': 'vector', 'requested_type': 'block', 'delivered_type': 'block', 'parts': [],
                   'diagram': {'width': 1000, 'height': 800, 'nodes': [
                       {'id': 'supply', 'type': 'process', 'box': [200, 200, 500, 400], 'text': '电源'}], 'edges': []}}],
    }
    workspace.stage(wid, plan)
    workspace.render(wid, 'overview')
    workspace.package(wid)
    plan['revision'] = 2
    plan['views'] = [{'id': 'detail', 'title': '空间关系', 'purpose': '表达待核的接口',
                      'engine': 'image', 'requested_type': 'spatial', 'delivered_type': 'spatial', 'parts': ['supply'],
                      'expression_mode': 'relational_schematic', 'instruction': '只画已证接口'}]
    workspace.stage(wid, plan, image_budget=1)
    before = workspace.read(wid)

    messages = list(client.invoke({'action': 'receive', 'context_id': 'case-a', 'workspace_id': wid,
                                   'query': '导出已完成'}))

    assert len(messages) == 1
    result = messages[0].message.json_object
    assert result['blocking_state'] == 'needs_expression'
    assert result['blocked_view'] == 'detail'
    assert result['current_revision'] == 1 and result['can_export_current'] is True
    assert result['model_calls'] == 0 and workspace.image_dispatch_count(wid) == 0
    assert '导出' in result['message'] and workspace.read(wid) == before

    historical = list(client.invoke({'action': 'receive', 'context_id': 'case-a', 'workspace_id': wid,
                                     'query': '导出版本1'}))
    assert len(historical) == 3
    receipt = historical[-1].message.json_object
    assert receipt['deliverable_status'] == 'historical_revision'
    assert receipt['export_revision'] == 1
    assert '空间关系' not in receipt['message'] and '合同待' not in receipt['message']
    assert workspace.read(wid) == before

    latest = list(client.invoke({'action': 'receive', 'context_id': 'case-a', 'workspace_id': wid,
                                 'query': '导出'}))
    assert len(latest) == 3
    reply = latest[-1].message.json_object
    assert reply['deliverable_status'] == 'complete_draft' and reply['export_revision'] == 1
    assert '最近完整版本1' in reply['message'] and '当前修订2尚未完成' in reply['message']
    assert stale not in reply['message'] and '本图单图片调用上限需求' not in reply['message']
    assert workspace.read(wid) == before


def test_partial_export_distinguishes_saved_image_from_new_dispatch(client, tmp_path):
    from datetime import datetime, timezone

    import httpx

    from patent_drawing.client import OpenAIImagesClient
    from patent_drawing.operation_store import digest
    from patent_drawing.workspace import Workspace
    from tests.test_images import b64, picture

    requests = []

    def respond(request):
        requests.append(request.url.path)
        return httpx.Response(200, headers={'x-request-id': 'req_saved_fixture'},
                              json={'data': [{'b64_json': b64(picture())}]})

    received = list(client.invoke({'action': 'receive', 'context_id': 'case-a', 'query': '预览',
                                  'materials': '电源安装在支架上。'}))[-1].message.json_object
    wid = received['workspace_id']
    store = OperationStore(tmp_path / 'state')
    owner = client.session.storage.items['patent-drawing-workspace-namespace-v3'].decode() + ':alice:case-a'
    workspace = Workspace(store, owner, allow_models=True,
                          image_client=OpenAIImagesClient('test-key', transport=httpx.MockTransport(respond)))
    workspace.stage(wid, {
        'schema_version': 'drawing-set-2', 'set_id': 'test', 'revision': 1,
        'source_sha256': workspace.read(wid)['source_sha256'], 'title': '支架',
        'patent_type': 'invention', 'technical_types': ['mechanical'],
        'parts': [], 'facts': [],
        'views': [
            {'id': 'main', 'title': '总图', 'purpose': '表达支架与电源关系', 'engine': 'image',
             'requested_type': 'relationship', 'delivered_type': 'relationship', 'parts': [],
             'instruction': '黑白示意电源支架，不增加孔位。'},
            {'id': 'sequence', 'title': '辅助图', 'purpose': '辅助阅读', 'engine': 'vector',
             'requested_type': 'relationship', 'delivered_type': 'relationship', 'parts': [],
             'diagram': {'width': 960, 'height': 720, 'nodes': [
                 {'id': 'n1', 'type': 'process', 'box': [160, 140, 750, 320], 'text': '电源安装在支架上'}],
                 'edges': []}},
        ],
    }, image_budget=1)
    unstarted = Workspace(store, owner).preflight(wid)
    record = workspace.render(wid, 'main')['record']
    contract = workspace.read(wid)['work']['plan']['visibility_contract']['main']
    workspace.review_visibility(wid, 'main', {
        'view_id': 'main', 'base_sha256': record['base']['sha256'],
        'spec_sha256': record['base_spec_sha256'], 'contract_sha256': digest(contract),
        'reviewed_at': datetime.now(timezone.utc).isoformat(), 'reviewer': 'synthetic-tester',
        'features': [{'feature_id': feature['feature_id'], 'verdict': 'PASS', 'region': [1, 1, 20, 30],
                      'note': '本地响应夹具'} for feature in contract['features']],
    })
    before = workspace.read(wid)
    messages = list(client.invoke({'action': 'receive', 'context_id': 'case-a', 'workspace_id': wid,
                                   'query': '导出已完成'}))
    result = messages[-1].message.json_object
    gate = Workspace(store, owner).preflight(wid)

    assert unstarted['supplier_state'] == 'not_called' and '已保存' not in unstarted['message']
    assert len(messages) == 3 and result['deliverable_status'] == 'incomplete_draft'
    assert result['missing_ids'] == ['sequence']
    assert result['supplier_state'] == gate['supplier_state'] == 'response_saved'
    assert result['model_calls'] == gate['model_calls'] == 0
    assert '本次导出未新增模型调用' in result['message'] and '未生成图件：sequence' in result['message']
    assert '本次检查未新增模型调用' in gate['message'] and '已保存' in gate['message']
    assert all('尚未' not in message for message in (result['message'], gate['message']))
    assert requests == ['/v1/images/generations'] and workspace.read(wid) == before


@pytest.mark.parametrize('query', ['确认工程示意改号', '导出工程示意版本 1', '导出结构示意'])
def test_engineering_revision_commands_do_not_enter_source_text(client, query):
    first = call(client, '弹簧支承安装板。', patent_type='invention', application_stage='draft')
    confirmed = call(client, '确认', first['intake_revision'])
    forwarded = call(client, query, confirmed['intake_revision'])
    assert forwarded['route'] == 'forward'
    assert forwarded['workspace_id'] == confirmed['workspace_id']
    assert forwarded['intake_revision'] == confirmed['intake_revision']


def test_saved_planner_response_recovers_after_worker_interrupt_without_new_post(tmp_path, monkeypatch):
    import json

    import httpx

    from patent_drawing.text_client import OpenAITextClient
    from patent_drawing.workspace import Workspace

    store = OperationStore(tmp_path / 'state')
    calls = []
    source = '电路包含电源。'
    proposal = {
        'schema_version': 'drawing-set-2', 'title': '供电原理', 'patent_type': 'invention',
        'technical_types': ['electrical'], 'parts': [], 'facts': [],
        'views': [{'id': 'overview', 'title': '总图', 'purpose': '表达电源',
                   'engine': 'vector', 'requested_type': 'block', 'delivered_type': 'block', 'parts': [],
                   'diagram': {'width': 1000, 'height': 800, 'nodes': [
                       {'id': 'supply', 'type': 'process', 'box': [200, 200, 500, 400], 'text': '电源'}], 'edges': []}}],
    }
    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(200, headers={'x-request-id': 'req_fixture'}, json={
            'status': 'completed', 'output': [{'type': 'message', 'content': [
                {'type': 'output_text', 'text': json.dumps(proposal)}]}],
            'usage': {'input_tokens': 20, 'output_tokens': 30, 'total_tokens': 50},
        })

    provider = OpenAITextClient('test-key', transport=httpx.MockTransport(respond))
    workspace = Workspace(store, 'alice', allow_models=True, text_client=provider)
    wid = workspace.ingest([{'name': 'source.txt', 'blob': source.encode()}])['workspace_id']
    save = store.save
    def stop_after_provider_response(owner, name, value):
        save(owner, name, value)
        if name.endswith(':evidence') and 'response' in value:
            raise KeyboardInterrupt('simulated worker exit after saving the response')

    with monkeypatch.context() as patch:
        patch.setattr(store, 'save', stop_after_provider_response)
        with pytest.raises(KeyboardInterrupt):
            workspace.plan(wid, '规划')

    restored = Workspace(OperationStore(tmp_path / 'state'), 'alice', allow_models=False)
    assert restored.progress(wid)['resume_action'] == 'resume_saved_plan'
    result = restored.resume_planning(wid)
    assert result['plan']['title'] == '供电原理'
    assert result['request_id'] == 'req_fixture' and result['model_calls'] == 0
    assert calls == ['/v1/responses']
    assert restored.read(wid)['proposal'] == result['plan']
    assert restored.progress(wid)['resume_action'] == 'confirm_plan'
    assert calls == ['/v1/responses']


def test_directional_heat_conduction_film_is_inferred_as_materials(client):
    first = call(client, '鳞片状碳材料沿膜面方向排布，邻片接触形成面内导热网络；机械强度受配方及工艺影响。',
                 patent_type='invention', application_stage='draft')
    assert first['technical_type'] == 'chemical_material'
    assert first['model_calls'] == 0
    changed = call(client, '更正技术领域为制造工艺', first['intake_revision'])
    assert changed['technical_type'] == 'manufacturing_process'
    assert changed['intake_id'] == first['intake_id']


def test_first_generate_prepares_a_proposal_and_requires_current_figure_plan_confirmation(client, tmp_path, monkeypatch):
    import json

    import httpx

    from patent_drawing.text_client import OpenAITextClient
    from patent_drawing.workspace import Workspace

    first = call(client, '电路包括电源。', patent_type='invention', application_stage='draft')
    confirmed = call(client, '确认', first['intake_revision'])
    wid = confirmed['workspace_id']
    rev = confirmed['intake_revision']
    owner = client.session.storage.items['patent-drawing-workspace-namespace-v3'].decode() + ':alice:case-a'
    workspace = Workspace(OperationStore(tmp_path / 'state'), owner)
    proposal = {
        'schema_version': 'drawing-set-2', 'title': '供电结构', 'patent_type': 'invention',
        'technical_types': ['electrical'], 'parts': [], 'facts': [],
        'views': [{'id': 'overview', 'title': '总图', 'purpose': '表达电源',
                   'engine': 'vector', 'requested_type': 'block', 'delivered_type': 'block', 'parts': [],
                   'diagram': {'width': 1000, 'height': 800, 'nodes': [
                       {'id': 'supply', 'type': 'process', 'box': [200, 200, 500, 400], 'text': '电源'}], 'edges': []}}],
    }
    calls = []
    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={'status': 'completed', 'output': [
            {'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(proposal)}]}]})

    provider = OpenAITextClient('test-key', transport=httpx.MockTransport(respond))
    monkeypatch.setattr('patent_drawing.text_client.OpenAITextClient', lambda key: provider)
    monkeypatch.setattr('patent_drawing.operator_policy.model_execution_enabled', lambda: True)
    monkeypatch.setattr('patent_drawing.operator_policy.effective_image_budget', lambda limit: limit)
    client.runtime.credentials['openai_api_key'] = 'test-key'
    def receive(query):
        return list(client.invoke({'action': 'receive', 'context_id': 'case-a', 'workspace_id': wid,
                                  'expected_revision': rev, 'query': query}))[-1].message.json_object

    result = receive('生成')
    assert result['route'] == 'reply'
    assert workspace.read(wid)['work'] is None
    assert workspace.read(wid)['proposal']['title'] == '供电结构'
    assert '确认图单' in result['message']
    assert receive('生成')['route'] == 'reply'
    assert calls == ['/v1/responses']
    assert call(client, '确认图单并生成', rev)['route'] == 'forward'
    assert receive('确认图单并生成')['route'] == 'execute'
    assert workspace.read(wid)['work']['plan']['title'] == '供电结构'
    assert calls == ['/v1/responses']


def test_material_composition_can_return_an_evidence_bound_no_drawing_recommendation(tmp_path):
    import json

    import httpx

    from patent_drawing.text_client import OpenAITextClient
    from patent_drawing.workspace import Workspace

    store = OperationStore(tmp_path / 'state')
    source = '本方案描述合金的组成和性能。'
    recommendation = {
        'drawing_decision': 'no_drawing_recommended',
        'reason': '交底仅描述组成和性能，未提供可以据此表达的结构或试验图。',
        'evidence': [{'page_id': 'src1_p1', 'quote': source}],
    }
    def respond(request):
        return httpx.Response(200, json={'status': 'completed', 'output': [
            {'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(recommendation)}]}]})
    provider = OpenAITextClient('test-key', transport=httpx.MockTransport(respond))
    workspace = Workspace(store, 'alice', allow_models=True, text_client=provider)
    wid = workspace.ingest([{'name': 'source.txt', 'blob': source.encode()}],
                           technical_types=['chemical_material'])['workspace_id']
    recommendation['evidence'][0]['page_id'] = workspace.read(wid)['bundle']['pages'][0]['id']
    result = workspace.plan(wid, '规划')
    assert result['drawing_decision'] == 'no_drawing_recommended'
    assert result['model_calls'] == 1 and '待' in result['message']
    state = workspace.read(wid)
    assert state['work'] is None and state['proposal'] is None
    assert state['drawing_assessment']['source_sha256'] == state['source_sha256']
    assert workspace.image_dispatch_count(wid) == 0
    completed = workspace.confirm_no_drawings(wid)
    assert completed['execution_state'] == 'completed'
    assert completed['deliverable_status'] == 'no_drawings'
    assert completed['model_calls'] == 0
    assert workspace.progress(wid)['deliverable_status'] == 'no_drawings'
    assert workspace.read(wid)['drawing_assessment']['confirmed'] is True
    assert not workspace.read(wid)['current']


def test_no_drawing_confirmation_requires_an_assessment_bound_to_current_source(tmp_path):
    from patent_drawing.workspace import Workspace

    workspace = Workspace(OperationStore(tmp_path), 'owner')
    wid = workspace.ingest([{'name': 'source.txt', 'blob': b'Unreviewed source'}])['workspace_id']
    assert workspace.confirm_no_drawings(wid)['execution_state'] == 'blocked'
    state = workspace.read(wid)
    state['drawing_assessment'] = {'source_sha256': 'different', 'reason': 'An older assessment'}
    workspace.save(wid, state)
    assert workspace.confirm_no_drawings(wid)['execution_state'] == 'blocked'


def test_planning_disconnect_is_a_reviewable_unknown_and_cannot_redispatch(client, monkeypatch):
    import httpx

    from patent_drawing.text_client import OpenAITextClient

    first = call(client, '电路包括电源。', patent_type='invention', application_stage='draft')
    confirmed = call(client, '确认', first['intake_revision'])
    requests = []
    def disconnect(request):
        requests.append(request.url.path)
        raise httpx.ReadError('simulated connection loss')

    provider = OpenAITextClient('test-key', transport=httpx.MockTransport(disconnect))
    monkeypatch.setattr('patent_drawing.text_client.OpenAITextClient', lambda key: provider)
    monkeypatch.setattr('patent_drawing.operator_policy.model_execution_enabled', lambda: True)
    monkeypatch.setattr('patent_drawing.operator_policy.effective_image_budget', lambda limit: limit)
    client.runtime.credentials['openai_api_key'] = 'test-key'
    request = {'action': 'receive', 'context_id': 'case-a', 'workspace_id': confirmed['workspace_id'],
               'expected_revision': confirmed['intake_revision'], 'query': '规划'}
    first, replay = [list(client.invoke(request))[-1].message.json_object for _ in range(2)]
    for result in (first, replay):
        assert result['route'] == 'reply' and result['technical_status'] == 'UNKNOWN'
        assert result['blocking_state'] == 'planning_result_unknown'
        assert result['resume_action'] == 'inspect_original_result'
        assert result['workspace_id'] == confirmed['workspace_id']
    assert first['operation_id'] == replay['operation_id']
    assert first['model_calls'] == 1 and replay['model_calls'] == 0
    assert requests == ['/v1/responses']


@pytest.mark.parametrize('field', ['spec_sha256', 'base_spec_sha256'])
def test_view_completed_rejects_figure_from_another_spec(client, tmp_path, field):
    from patent_drawing.errors import ToolInvokeError

    from patent_drawing.workspace import Workspace

    command = {'action': 'receive', 'context_id': 'case-a'}
    received = list(client.invoke({**command, 'query': '预览', 'materials': '电源安装在支架上。'}))
    wid = received[-1].message.json_object['workspace_id']
    store = OperationStore(tmp_path / 'state')
    owner = client.session.storage.items['patent-drawing-workspace-namespace-v3'].decode() + ':alice:case-a'
    workspace = Workspace(store, owner)
    plan = {
        'schema_version': 'drawing-set-2', 'set_id': 'drawings', 'revision': 1,
        'source_sha256': workspace.read(wid)['source_sha256'], 'title': '支架',
        'patent_type': 'invention', 'technical_types': ['mechanical'],
        'parts': [], 'facts': [], 'constraints': [], 'unresolved': [],
        'views': [{
            'id': 'overview', 'title': '总图', 'purpose': '表达支架与电源关系',
            'engine': 'vector', 'requested_type': 'block', 'delivered_type': 'block', 'parts': [],
            'diagram': {'width': 1000, 'height': 800, 'nodes': [
                {'id': 'supply', 'type': 'process', 'box': [200, 200, 500, 400], 'text': '电源'},
            ], 'edges': []},
        }],
    }
    workspace.stage(wid, plan)
    original = workspace.render(wid, 'overview')
    view = {**command, 'workspace_id': wid, 'query': '查看已完成'}
    messages = list(client.invoke(view))
    assert messages[0].message.blob == original['png']
    assert messages[-1].message.json_object['model_calls'] == 0

    before = workspace.read(wid)
    key = workspace.record_key(wid, 'overview', before['work']['specs']['overview'])
    store.save(owner, key, {**original['record'], field: '0' * 64})

    with pytest.raises(ToolInvokeError, match='stale figure'):
        next(client.invoke(view))
    assert workspace.read(wid) == before
    assert workspace.image_dispatch_count(wid) == 0

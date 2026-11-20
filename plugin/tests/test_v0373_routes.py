import json
import sys
from pathlib import Path

import pytest

from patent_drawing.client import prepare_image_payload
from patent_drawing.prompts import build_prompt


def test_compiled_image_model_is_explicit_and_default_is_legacy():
    request = build_prompt({'prompt': '绘制平面轮廓'}, 'generate')
    assert json.loads(prepare_image_payload(request))['model'] == 'gpt-image-2.6-flare'
    assert json.loads(prepare_image_payload(request, model='gpt-image-2.6-sunburst'))['model'] == 'gpt-image-2.6-sunburst'


def test_dsl_runs_agent_only_on_complex_plan():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'chatflows'))
    from build_quality_chatflow import build

    graph = build(figure_routing=True, complex_agent=True)['workflow']['graph']
    nodes = {node['id']: node['data'] for node in graph['nodes']}
    assert nodes['q_complex_agent']['type'] == 'agent'
    assert nodes['q_complexity']['tool_parameters']['action']['value'] == 'route_images'
    assert nodes['q_apply_preparation']['tool_parameters']['action']['value'] == 'accept_image_preparation'
    assert any(edge['source'] == 'q_route_switch' and edge['sourceHandle'] == 'agent'
               and edge['target'] == 'q_complex_agent' for edge in graph['edges'])


def test_decision_answers_are_bound_to_complete_view_set():
    from patent_drawing.figure_routing import decision_answers
    rows = {'model': 'gpt-6-luna', 'answers': [
        {'name': 'v1', 'type': 'choice', 'choice': 'complex'},
        {'name': 'v2', 'type': 'choice', 'choice': 'standard'},
    ]}
    assert decision_answers(rows, names=('v1', 'v2'), allowed={'complex', 'standard', 'uncertain'})['choices'] == {
        'v1': 'complex', 'v2': 'standard'}
    assert decision_answers(rows, names=('v1',), allowed={'complex', 'standard'})['state'] == 'invalid_output'


def test_real_split_beam_evidence_is_bound_without_joining_distinct_paragraphs():
    from patent_drawing.spatial_brief import beam_fact_ids
    opening = '发射设备定义相对于自身天线面板的角度范围'
    following = '其中包含至少两个发射波束方向，并把该范围指示发送给接收设备。'
    facts = [{'id': 'f1', 'evidence': [{'page_id': 'p1', 'quote': opening}]},
             {'id': 'f2', 'evidence': [{'page_id': 'p1', 'quote': following}]}]
    assert beam_fact_ids({'pages': [{'id': 'p1', 'text': opening + '，' + following}]}, facts) == ['f1', 'f2']
    assert not beam_fact_ids({'pages': [{'id': 'p1', 'text': opening + '。另一个实施例，' + following}]}, facts)


def test_agent_focus_cannot_change_source_or_bypass_receipt():
    from patent_drawing.image_route import preparation
    scope = 'a' * 64
    assert preparation(json.dumps({'scope': scope, 'views': {'v1': 'occlusion'}}), scope, ['v1']) == {'v1': 'occlusion'}
    with pytest.raises(ValueError):
        preparation(json.dumps({'scope': scope, 'views': {'v1': 'draw hidden parts'}}), scope, ['v1'])


def test_signed_runtime_lists_every_imported_workspace_module():
    import ast

    plugin = Path(__file__).resolve().parents[1]
    members = set(json.loads((plugin / '_assets/runtime-members.json').read_text()))
    package = plugin / 'patent_drawing'
    omissions = set()
    for source in package.glob('*.py'):
        for node in ast.walk(ast.parse(source.read_text(), filename=str(source))):
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
                name = node.module.split('.')[0] + '.py'
                if (package / name).is_file() and 'patent_drawing/' + name not in members:
                    omissions.add((source.name, name))
    assert not omissions


def test_workspace_binds_decision_and_agent_receipts_to_current_proposal(tmp_path):
    from patent_drawing.operation_store import OperationStore
    from patent_drawing.workspace import Workspace

    class Decisions:
        calls = 0

        def classify_images(self, task, questions, capture):
            self.calls += 1
            assert [q['name'] for q in questions] == ['complex_view', 'simple_view']
            capture('request', b'test request')
            return {'state': 'selected', 'choices': {'complex_view': 'complex', 'simple_view': 'standard'}}

    class Planner:
        calls = 0

        def plan(self, system, user, *, evidence_sink):
            self.calls += 1
            data = json.loads(user)
            assert [v['id'] for v in data['views']] == ['complex_view']
            return {'text': json.dumps({'scope': data['scope'], 'views': {'complex_view': 'occlusion'}})}

    decisions, planner = Decisions(), Planner()
    w = Workspace(OperationStore(tmp_path), 'owner', allow_models=True, decision_client=decisions, text_client=planner)
    wid = w.ingest([{'name': 'source.txt', 'blob': '包含机械遮挡关系。'.encode()}])['workspace_id']
    state = w.read(wid)
    state['proposal'] = {'source_sha256': state['source_sha256'], 'revision': 1, 'parts': [], 'views': [
        {'id': view_id, 'engine': 'image', 'delivered_type': 'relationship', 'parts': []}
        for view_id in ('complex_view', 'simple_view')
    ]}
    w.save(wid, state)
    route = w.route_images(wid, '生成')
    assert route['route'] == 'agent' and route['model_calls'] == 1
    assert w.route_images(wid, '生成')['model_calls'] == 0
    assert w.read(wid)['image_route_proposal']['models'] == {
        'complex_view': 'gpt-image-2.6-sunburst', 'simple_view': 'gpt-image-2.6-flare'}
    scope = route['scope']
    fabricated = json.dumps({'scope': scope, 'views': {'complex_view': 'identity'}})
    with pytest.raises(ValueError, match='state unavailable for this owner'):
        w.accept_image_preparation(wid, scope, fabricated)
    prepared = w.prepare_complex_images(wid, scope)
    with pytest.raises(ValueError, match='原始操作回执'):
        w.accept_image_preparation(wid, scope, fabricated)
    assert w.prepare_complex_images(wid, scope) == prepared
    w.accept_image_preparation(wid, scope, json.dumps(prepared))
    assert w.route_images(wid, '生成')['route'] == 'reply'
    state = w.read(wid)
    state['proposal']['views'][0]['instruction'] = '新候选图单'
    w.save(wid, state)
    with pytest.raises(ValueError, match='已过期'):
        w.accept_image_preparation(wid, scope, json.dumps(prepared))
    assert decisions.calls == planner.calls == 1


def test_agent_bridge_checks_tenant_context_workspace_and_capability(tmp_path):
    from patent_drawing.operation_store import OperationStore
    from tools.workspace import preparation_owner

    store = OperationStore(tmp_path)
    scope = 'a' * 64
    preparation_owner(store, 'tenant-one', 'conversation', 'workspace', scope, owner='owner')
    assert preparation_owner(store, 'tenant-one', 'conversation', 'workspace', scope) == 'owner'
    for tenant, conversation, workspace, capability in [
        ('tenant-two', 'conversation', 'workspace', scope),
        ('tenant-one', 'other', 'workspace', scope),
        ('tenant-one', 'conversation', 'other', scope),
        ('tenant-one', 'conversation', 'workspace', 'b' * 64),
    ]:
        with pytest.raises(ValueError, match='复杂图准备授权'):
            preparation_owner(store, tenant, conversation, workspace, capability)

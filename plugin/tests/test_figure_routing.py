import json

import httpx
import pytest
from .test_intake import client

from patent_drawing.figure_routing import DecisionClient, FAMILIES, decision_answers
from patent_drawing.operation_store import OperationStore
from patent_drawing.workspace import Workspace


DISCLOSURE = '阀体与传感器固定连接。控制器在检测到压力过高时打开阀门，否则保持关闭。'


def answers(*needed):
    return {'model': 'gpt-6-luna', 'answers': [
        {'type': 'choice', 'name': name, 'choice': 'needed' if name in needed else 'not_needed'}
        for name in FAMILIES
    ]}


def test_official_decision_request_preserves_both_figure_families():
    def provider(request):
        assert request.method == 'POST'
        assert str(request.url) == 'https://api.openai.com/v1/decisions'
        body = json.loads(request.content)
        assert body['model'] == 'gpt-6-luna' and DISCLOSURE in body['input']
        assert {question['name'] for question in body['questions']} == set(FAMILIES)
        assert all(question['type'] == 'choice' for question in body['questions'])
        return httpx.Response(200, json=answers('structure', 'flow'), headers={'x-request-id': 'req_test'})

    captured = {}
    result = DecisionClient('test', transport=httpx.MockTransport(provider)).classify(
        {'source_text': DISCLOSURE}, captured.__setitem__)
    assert result['state'] == 'selected'
    assert result['selected'] == ['structure', 'flow']
    assert set(captured) == {'request', 'receipt', 'response'}


def test_claims_semantics_share_one_luna_request_with_figure_choices():
    body = '1. 一种装置，含两个并行输入及按条件拒绝的输出端。'
    sent = []

    def provider(request):
        payload = json.loads(request.content)
        sent.append(payload)
        assert payload['input'] == body
        reply = answers('structure', 'flow')
        for name in ('semantic_merge', 'semantic_exits', 'semantic_messages',
                     'semantic_state', 'semantic_applicability'):
            choice = ('needed' if name in {'semantic_merge', 'semantic_exits'}
                      else 'uncertain' if name == 'semantic_applicability' else 'not_needed')
            reply['answers'].append({'type': 'choice', 'name': name, 'choice': choice,
                                     'reason': '未核实内容不能进入 Sol 指令'})
        assert [q['name'] for q in payload['questions']] == [r['name'] for r in reply['answers']]
        return httpx.Response(200, json=reply)

    result = DecisionClient('test', transport=httpx.MockTransport(provider)).classify(
        body, {}.__setitem__, semantic=True)
    assert len(sent) == 1 and result['selected'] == ['structure', 'flow']
    assert result['semantic_choices']['semantic_merge'] == result['semantic_choices']['semantic_exits'] == 'needed'
    assert result['semantic_choices']['semantic_applicability'] == 'uncertain'


def test_missing_semantic_answer_cannot_invent_planning_guidance():
    reply = answers('structure')
    assert decision_answers(reply, semantic=True)['state'] == 'invalid_output'
    assert 'semantic_choices' not in decision_answers(reply, semantic=True)
    assert decision_answers(reply)['selected'] == ['structure']


@pytest.mark.parametrize('broken', ['missing', 'duplicate', 'wrong_type', 'refusal', 'unknown'])
def test_bad_decisions_cannot_partially_select_a_family(broken):
    reply = answers('structure', 'flow')
    rows = reply['answers']
    if broken == 'missing':
        rows.pop()
    elif broken == 'duplicate':
        rows[-1] = rows[0]
    elif broken == 'wrong_type':
        rows[-1]['choice'] = True
    elif broken == 'refusal':
        rows[-1] = {'type': 'refusal', 'name': rows[-1]['name']}
    else:
        rows[-1]['name'] = 'invented'
    result = decision_answers(reply)
    assert result['state'] == ('refused' if broken == 'refusal' else 'invalid_output')
    assert not result.get('selected')


def test_decision_is_reused_for_same_source_and_skipped_for_non_planning(tmp_path):
    calls = []

    def provider(request):
        calls.append(request.content)
        return httpx.Response(200, json=answers('structure', 'flow'))

    workspace = Workspace(OperationStore(tmp_path), 'owner', allow_models=True,
                          decision_client=DecisionClient('test', transport=httpx.MockTransport(provider)))
    wid = workspace.ingest([{'name': 'source.txt', 'blob': DISCLOSURE.encode()}])['workspace_id']
    for query in ('预览', '继续', '导出', '确认图单并生成'):
        assert workspace.route_figures(wid, query)['state'] == 'skipped'
    assert workspace.route_figures(wid, '规划', imported_plan=True)['state'] == 'skipped'
    first = workspace.route_figures(wid, '规划')
    again = workspace.route_figures(wid, '规划')
    assert first['model_calls'] == 1 and again['model_calls'] == 0
    assert again['operation_id'] == first['operation_id']
    assert again['selected'] == ['structure', 'flow']
    assert len(calls) == 1


def test_graph_has_one_decision_and_executes_both_instruction_nodes():
    from .test_intake_chatflow import build as base_build
    flow = base_build(figure_routing=True)['workflow']['graph']
    nodes = {node['id']: node['data'] for node in flow['nodes']}
    assert nodes['q_figure_decision']['tool_parameters']['action']['value'] == 'route_figures'
    scope = {}
    exec(nodes['q_figure_selection']['code'], scope)
    selection = scope['main']([{'workspace_id': 'w', 'state': 'selected',
                               'operation_id': 'route_1', 'selected': ['structure', 'flow']}])
    instructions = {}
    for family in FAMILIES:
        scope = {}
        exec(nodes['q_prompt_' + family]['code'], scope)
        instructions[family] = scope['main'](selection['selected'])['instruction']
    assert instructions['structure'] and instructions['flow']
    assert not instructions['sequence']
    scope = {}
    exec(nodes['q_figure_merge']['code'], scope)
    merged = json.loads(scope['main'](selection['operation_id'], **instructions)['routing_json'])
    assert set(merged['instructions']) == {'structure', 'flow'}
    assert nodes['q_receive']['tool_parameters']['figure_routing_json']['value'] == ['q_figure_merge', 'routing_json']


def routed_workspace(tmp_path, selected, planner, *, client=None):
    client = client or DecisionClient('test', transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=answers(*selected))))
    workspace = Workspace(OperationStore(tmp_path), 'owner', allow_models=True,
                          decision_client=client, text_client=planner)
    wid = workspace.ingest([{'name': 'source.txt', 'blob': DISCLOSURE.encode()}],
                           technical_types=['mechanical'])['workspace_id']
    result = workspace.route_figures(wid, '规划')
    return workspace, wid, result, {
        'operation_id': result['operation_id'],
        'instructions': {name: FAMILIES[name]['instruction'] for name in result['selected']},
    }


def test_mixed_plan_and_render_share_parts_and_export_both_views(tmp_path):
    import io
    import zipfile
    from xml.etree import ElementTree

    import pypdfium2
    from .test_source_drawing_gaps import make_plan, vector

    class Planner:
        def plan(self, system, user, **kwargs):
            context = json.loads(user)['figure_routing']
            assert context['selected'] == ['structure', 'flow']
            assert [row['family'] for row in context['per_family_instructions']] == ['structure', 'flow']
            assert '同一图单' in system
            return {'text': json.dumps(proposal)}

    workspace, wid, _, routing = routed_workspace(tmp_path, ['structure', 'flow'], Planner())
    parts = [{'id': 'valve', 'label': '1', 'name': '阀体'},
             {'id': 'sensor', 'label': '2', 'name': '传感器'}]
    structure = vector('assembly', 'assembly', parts=['valve', 'sensor'], primitives=[
        {'id': 'v', 'type': 'rect', 'part': 'valve', 'box': [140, 260, 460, 420]},
        {'id': 's', 'type': 'rect', 'part': 'sensor', 'box': [460, 300, 570, 380]},
        {'type': 'label', 'part': 'valve', 'target_shape': 'v', 'at': [120, 170], 'target': [180, 280]},
        {'type': 'label', 'part': 'sensor', 'target_shape': 's', 'at': [590, 190], 'target': [540, 315]},
    ])
    flow = vector('control', 'flowchart', parts=['valve'])
    flow['diagram'].update(nodes=[
        {'id': 'sense', 'type': 'decision', 'text': '压力过高？', 'box': [260, 70, 640, 200],
         'branches': {'yes': 'open', 'no': 'keep'}},
        {'id': 'open', 'type': 'process', 'text': '打开阀门', 'box': [70, 350, 370, 440]},
        {'id': 'keep', 'type': 'process', 'text': '保持关闭', 'box': [520, 350, 820, 440]},
    ], edges=[
        {'source': 'sense', 'target': 'open', 'branch': 'yes', 'points': [[260, 135], [220, 135], [220, 350]]},
        {'source': 'sense', 'target': 'keep', 'branch': 'no', 'points': [[640, 135], [670, 135], [670, 350]]},
    ])
    proposal = make_plan(workspace, wid, ['mechanical'], [structure, flow], parts)
    page_id = workspace.read(wid)['bundle']['pages'][0]['id']
    for index, (view, quote) in enumerate(zip(proposal['views'], DISCLOSURE.split('。')[:2]), 1):
        fact_id = f'fact_{index}'
        view.update(required_facts=[fact_id], instruction=FAMILIES['structure' if index == 1 else 'flow']['instruction'])
        proposal['facts'].append({'id': fact_id, 'status': 'confirmed', 'text': quote,
                                  'evidence': [{'page_id': page_id, 'quote': quote}]})

    result = workspace.plan(wid, '规划', figure_routing=routing)
    assert workspace.read(wid)['work'] is None
    assert workspace.plan(wid, '规划', figure_routing=routing)['model_calls'] == 0
    assert '建议图型' in result['message']
    plan = result['plan']
    assert [view['requested_type'] for view in plan['views']] == ['assembly', 'flowchart']
    assert plan['parts'] == parts and 'valve' in plan['views'][1]['parts']
    workspace.stage(wid, plan)
    for view in plan['views']:
        assert workspace.render(wid, view['id'])['model_calls'] == 0
    exported = workspace.package(wid)
    with pypdfium2.PdfDocument(exported['pdf']) as pdf, zipfile.ZipFile(io.BytesIO(exported['zip'])) as archive:
        assert len(pdf) == 2
        assert {'figures/assembly.svg', 'figures/control.svg'} <= set(archive.namelist())
        control = ElementTree.fromstring(archive.read('figures/control.svg'))
        assert any(node.text == '打开阀门' for node in control)
        assert any(node.text == '保持关闭' for node in control)
    assert workspace.package(wid, export_current=True)['zip'] == exported['zip']


def test_missing_family_blocks_confirmation_and_preserves_candidate(tmp_path):
    from .test_source_drawing_gaps import make_plan, vector

    class Planner:
        def plan(self, *args, **kwargs):
            return {'text': json.dumps(proposal)}

    workspace, wid, _, routing = routed_workspace(tmp_path, ['structure', 'flow'], Planner())
    proposal = make_plan(workspace, wid, ['mechanical'], [vector('assembly', 'assembly', texts=['阀体'])])
    result = workspace.plan(wid, '规划', figure_routing=routing)
    assert result['blocking_state'] == 'planning_coverage_incomplete'
    assert result['domain_findings'][0]['message'] == '待补方法与工艺流程图。'
    assert workspace.read(wid)['proposal'] is None


def test_foreign_or_modified_instruction_receipt_cannot_start_planning(tmp_path):
    class Planner:
        def plan(self, *args, **kwargs):
            pytest.fail('planner must not be called')

    workspace, wid, _, routing = routed_workspace(tmp_path, ['structure', 'flow'], Planner())
    other = workspace.ingest([{'name': 'same.txt', 'blob': DISCLOSURE.encode()}])['workspace_id']
    with pytest.raises(ValueError, match='当前交底'):
        workspace.plan(other, '规划', figure_routing=routing)
    with pytest.raises(ValueError, match='当前交底'):
        workspace.plan(wid, '规划：更新图型', figure_routing=routing)
    routing['instructions'].pop('flow')
    with pytest.raises(ValueError, match='逐图型'):
        workspace.plan(wid, '规划', figure_routing=routing)


@pytest.mark.parametrize(('mode', 'expected'), [
    ('reject', 'provider_rejected'), ('timeout', 'result_unknown'), ('bad_json', 'invalid_output'),
])
def test_failed_decision_is_recorded_and_stops_planning(tmp_path, mode, expected):
    calls = []

    def provider(request):
        calls.append(request)
        if mode == 'timeout':
            raise httpx.ReadTimeout('offline', request=request)
        return httpx.Response(403 if mode == 'reject' else 200, content=b'not a decision')

    class Planner:
        def plan(self, *args, **kwargs):
            pytest.fail('planner must not be called')

    client = DecisionClient('test', transport=httpx.MockTransport(provider))
    workspace, wid, decision, routing = routed_workspace(tmp_path, [], Planner(), client=client)
    assert decision['state'] == expected
    assert workspace.route_figures(wid, '规划')['model_calls'] == 0
    assert len(calls) == 1
    result = workspace.plan(wid, '规划', figure_routing=routing)
    assert result['blocking_state'] == 'figure_decision_' + expected
    assert result['route'] == 'reply'
    receipt = workspace.store.load(workspace.owner, decision['operation_id'] + ':receipt')
    assert workspace.store.get_artifact(workspace.owner, receipt['evidence']['request']) == calls[0].content
    if mode != 'timeout':
        assert workspace.store.get_artifact(workspace.owner, receipt['evidence']['response']) == b'not a decision'


def test_decisions_and_responses_share_the_same_text_budget(tmp_path):
    import sqlite3
    from dataclasses import replace
    from .test_duplicate_image_dispatch import policy_file
    from patent_drawing.operator_policy import AuditedTransport, load_policy
    from patent_drawing.text_client import OpenAITextClient

    policy = replace(load_policy(policy_file(tmp_path, 'figure_test')), limits={'text': 1, 'image': 0})
    calls = []

    def provider(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=answers('structure', 'flow'))

    transport = AuditedTransport(policy, inner=httpx.MockTransport(provider))
    decision = DecisionClient('test', transport=transport).classify({'source_text': DISCLOSURE}, lambda *_: None)
    assert decision['state'] == 'selected'
    with pytest.raises(ValueError, match='budget exhausted'):
        OpenAITextClient('test', transport=transport).plan('输出图单', DISCLOSURE)
    with sqlite3.connect(transport.db) as connection:
        assert connection.execute('select kind,state,status from calls').fetchall() == [('text', 'response_saved', 200)]
    assert calls == ['/v1/decisions']


def test_sdk_chatflow_turns_apply_both_routes_and_require_confirmation(client, monkeypatch):
    from .test_intake import call
    from .test_intake_chatflow import build
    from .test_source_drawing_gaps import vector
    from patent_drawing.text_client import OpenAITextClient

    source = '阀体和弹簧支承固定连接。压力过高则打开阀门，否则保持关闭。'
    first = call(client, source, patent_type='invention', application_stage='draft')
    confirmed = call(client, '确认', first['intake_revision'])
    wid = confirmed['workspace_id']
    rev = confirmed['intake_revision']
    assert wid
    nodes = {node['id']: node['data'] for node in build(figure_routing=True)['workflow']['graph']['nodes']}

    def code(node_id):
        scope = {}
        exec(nodes[node_id]['code'], scope)
        return scope['main']

    calls = []

    def respond(request):
        calls.append(request.url.path)
        if request.url.path == '/v1/decisions':
            assert source in json.loads(request.content)['input']
            return httpx.Response(200, json=answers('structure', 'flow'))
        assert request.url.path == '/v1/responses'
        payload = json.loads(request.content)
        context = json.loads(payload['input'][1]['content'])
        assert context['figure_routing']['selected'] == ['structure', 'flow']
        assert len(context['figure_routing']['per_family_instructions']) == 2
        plan = {'schema_version': 'drawing-set-2', 'title': '压力阀', 'patent_type': 'invention',
                'technical_types': ['mechanical'], 'parts': [], 'facts': [],
                'views': [vector('body', 'assembly', texts=['弹簧支承']),
                          vector('control', 'flowchart', texts=['压力过高时打开阀门'])]}
        plan['views'][0]['instruction'] = '表达阀体与弹簧支承的连接。'
        plan['views'][1]['instruction'] = '绘制压力过高时打开阀门的条件分支。'
        return httpx.Response(200, json={'status': 'completed', 'output': [
            {'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(plan)}]}
        ]})

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr('patent_drawing.figure_routing.DecisionClient', lambda key: DecisionClient(key, transport=transport))
    monkeypatch.setattr('patent_drawing.text_client.OpenAITextClient', lambda key: OpenAITextClient(key, transport=transport))
    monkeypatch.setattr('patent_drawing.operator_policy.model_execution_enabled', lambda: True)
    monkeypatch.setattr('patent_drawing.operator_policy.effective_image_budget', lambda limit: limit)
    client.runtime.credentials['openai_api_key'] = 'test-key'

    def tool(node_id, query, **params):
        action = nodes[node_id]['tool_parameters']['action']['value']
        return list(client.invoke({'action': action, 'context_id': 'case-a', 'workspace_id': wid,
                                   'expected_revision': rev, 'query': query, **params}))[-1].message.json_object

    assert call(client, '生成', rev)['route'] == 'forward'
    decision = tool('q_figure_decision', '生成')
    assert decision['model_calls'] == 1
    selection = code('q_figure_selection')([decision])
    specialized = {name: code('q_prompt_' + name)(selection['selected'])['instruction'] for name in FAMILIES}
    routing = code('q_figure_merge')(selection['operation_id'], **specialized)['routing_json']
    proposed = tool('q_receive', '生成', figure_routing_json=routing)
    assert code('q_result')([proposed])['route'] == 'reply'
    assert [view['requested_type'] for view in proposed['views']] == ['assembly', 'flowchart']
    assert '确认图单' in proposed['message']
    assert call(client, '确认图单并生成', rev)['route'] == 'forward'
    assert tool('q_figure_decision', '确认图单并生成')['state'] == 'skipped'
    accepted = tool('q_receive', '确认图单并生成')
    assert accepted['route'] == 'execute'
    assert calls == ['/v1/decisions', '/v1/responses']


def test_routed_flowchart_cannot_be_a_flare_image_or_omit_the_instruction():
    from patent_drawing.figure_routing import coverage_findings

    view = {'id': 'process', 'requested_type': 'flowchart', 'purpose': '说明压力判断', 'engine': 'image'}
    findings = coverage_findings({'views': [view]}, ['flow'])
    assert {item['rule'] for item in findings} == {'FIGURE-FAMILY-INSTRUCTION', 'FIGURE-FAMILY-ENGINE'}

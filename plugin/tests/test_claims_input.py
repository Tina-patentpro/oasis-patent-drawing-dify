import json

import pytest

from patent_drawing.claims_input import claims_plan, read_claims_text
from patent_drawing.figure_routing import FAMILIES, decision_payload
from patent_drawing.intake import Intake
from patent_drawing.operation_store import OperationStore
from patent_drawing.text_client import prepare_plan_payload
from patent_drawing.work_worker import WorkWorker
from patent_drawing.workspace import Workspace
from tests.test_durable_work import WorkerStopped, finish, queue_setup


BODY = '1. 一种装置，包括依次相连的第一端和第二端。\n\n2. 根据权利要求1所述的装置，其中第一端可转动。'
QUOTE = '依次相连的第一端和第二端'


def test_only_confirmed_claims_reach_both_providers():
    text, indexed = read_claims_text('权利要求书\n\n' + BODY + '\n')
    assert text == BODY
    assert text.encode()[indexed[1][0]:indexed[1][1]].decode().startswith('1. ')
    luna = decision_payload(text)
    assert luna['input'] == BODY
    sol = prepare_plan_payload('Plan figures with evidence.', text, [])
    assert sol['input'][1] == {'role': 'user', 'content': BODY}


@pytest.mark.parametrize('body', [
    '1. 一种装置。\n3. 一种装置。',
    '1. 根据权利要求2所述的方法。\n2. 一种方法。',
    BODY + '\n## 来源分析\n历史参考。',
    '说明书\n' + BODY,
])
def test_reject_ambiguous_or_invalid_claims_body(body):
    with pytest.raises(ValueError):
        read_claims_text(body)


def example():
    return {
        'views': [{'type': 'overall', 'title': '端部连接示意图', 'purpose': '表示端部连接',
                   'features': [{'text': QUOTE, 'claim': 1, 'quote': QUOTE}]}],
        'coverage': [{'claim': 1, 'figures': [1], 'reason': '结构关系'},
                     {'claim': 2, 'figures': [1], 'reason': '可同图表示'}],
        'unresolved': [],
    }


def wire_example():
    base = example()
    return {**base, 'citations': [{'c': 1, 'q': QUOTE, 'x': None}],
            'views': [{**base['views'][0], 'features': [1]}],
            'components': [], 'figure_links': [], 'relations': [], 'flow_edges': []}


def test_plan_quotes_must_be_exact_and_in_the_named_claim():
    plan = claims_plan(BODY, example())
    assert plan['views'][0]['id'] == 'fig1'
    part = plan['views'][0]['features'][0]
    assert BODY.encode()[part['start']:part['end']].decode() == QUOTE
    wrong = example()
    wrong['views'][0]['features'][0]['claim'] = 2
    with pytest.raises(ValueError):
        claims_plan(BODY, wrong)


def test_workspace_claims_only_dispatches_exact_body_and_reuses_receipt(tmp_path):
    dispatched = []

    class Decision:
        def classify(self, text, capture, *, images, semantic):
            assert not images and semantic
            dispatched.append(decision_payload(text, semantic=semantic)['input'])
            return {'state': 'selected', 'selected': ['structure'], 'uncertain': [],
                    'semantic_choices': {'semantic_merge': 'needed', 'semantic_exits': 'needed',
                                         'semantic_messages': 'not_needed', 'semantic_state': 'not_needed',
                                         'semantic_applicability': 'uncertain'},
                    'reasons': {'semantic_exits': '不可信文本哨兵'}}

    class Planner:
        def plan(self, system, text, *, images, evidence_sink, max_output_tokens, claims_wire,
                 claims_wire_contract=11):
            assert not images
            payload = prepare_plan_payload(system, text, images, max_output_tokens=max_output_tokens,
                                           claims_wire=claims_wire)
            assert payload['max_output_tokens'] == 20000 and payload['text']['format']['strict']
            assert claims_wire_contract == 14
            assert '并行路径' in system and '各独立处置' in system
            assert '每次配对' not in system and '不可信文本哨兵' not in system
            assert '作用域仍有不确定项' in system
            dispatched.append(payload['input'][1]['content'])
            return {'text': json.dumps(wire_example())}

    workspace = Workspace(OperationStore(tmp_path), 'test-owner', allow_models=True,
                          text_client=Planner(), decision_client=Decision())
    intake = Intake(workspace)
    form = {'source_mode': 'claims_only', 'query': '预览', 'materials': BODY}
    initial = intake.apply(form, [{'name': 'claims.txt', 'blob': BODY.encode()}])
    confirmed = intake.apply({'source_mode': 'claims_only', 'query': '确认',
                              'expected_revision': initial['intake_revision']})
    wid = confirmed['workspace_id']
    state = workspace.read(wid)
    state['bundle']['text'] = '交底哨兵'
    state['bundle']['pages'][0]['image'] = {'sha256': 'a' * 64}
    state['intake_request'] = '会话哨兵'
    workspace.save(wid, state)
    decision = workspace.route_figures(wid, '规划', claims_contract=14)
    route = {'operation_id': decision['operation_id'],
             'instructions': {'structure': FAMILIES['structure']['instruction']}}
    a = workspace.plan(wid, '规划', figure_routing=route, claims_contract=14)
    b = workspace.plan(wid, '规划', figure_routing=route, saved_operation=a['operation_id'], claims_contract=14)
    assert a == b
    assert a['claims_plan']['schema_version'] == 'claims-drawing-plan-14'
    assert dispatched == [BODY, BODY]


def test_claims_semantic_decision_resumes_from_saved_luna_receipt(tmp_path):
    import httpx

    from patent_drawing.figure_routing import DecisionClient

    store, queue, actor = queue_setup(tmp_path)
    intake = Intake(Workspace(store, actor.owner))
    form = {'source_mode': 'claims_only', 'query': '预览', 'materials': BODY}
    initial = intake.apply(form, [{'name': 'claims.txt', 'blob': BODY.encode()}])
    confirmed = intake.apply({'source_mode': 'claims_only', 'query': '确认',
                              'expected_revision': initial['intake_revision']})
    wid = confirmed['workspace_id']
    calls = []

    def provider(request):
        payload = json.loads(request.content)
        calls.append(('luna', payload['input']))
        selected = {'structure': 'needed', 'semantic_state': 'needed', 'semantic_applicability': 'uncertain'}
        result = [{'type': 'choice', 'name': q['name'], 'choice': selected.get(q['name'], 'not_needed')}
                  for q in payload['questions']]
        return httpx.Response(200, json={'model': 'gpt-6-luna', 'answers': result},
                              headers={'x-request-id': 'req_semantic'})

    class InterruptedDecision(DecisionClient):
        def classify(self, *args, **kwargs):
            super().classify(*args, **kwargs)
            raise WorkerStopped()

    class Planner:
        def plan(self, system, text, **options):
            calls.append(('sol', text))
            assert '转换、恢复和退出' in system and '作用域仍有不确定项' in system
            assert options['max_output_tokens'] == 20000 and options['images'] == []
            return {'text': json.dumps(wire_example())}

    admitted = queue.enqueue(actor, wid, int(confirmed['intake_revision']), 'plan',
                             query='规划', allow_models=True)
    with store.connect() as connection:
        connection.execute("UPDATE work_jobs SET payload=json_set(payload,'$.claims_contract',14) WHERE task_id=?",
                           (admitted['task_id'],))
    interrupted = WorkWorker(queue, lambda owner: Workspace(
        store, owner, allow_models=True,
        decision_client=InterruptedDecision('test', transport=httpx.MockTransport(provider))))
    with pytest.raises(WorkerStopped):
        interrupted.run_one()

    resumed = WorkWorker(queue, lambda owner: Workspace(store, owner, allow_models=True, text_client=Planner()))
    result = finish(resumed, admitted, queue, actor, wid)
    assert result['task_id'] == admitted['task_id'] and result['job_status'] == 'completed'
    assert result['claims_semantic_summary'] == '建议重点表达 状态转换与退出。\n语义待核 前提与局部触发。'
    assert queue.files(actor, wid, admitted['task_id']) == queue.files(actor, wid, admitted['task_id'])
    assert calls == [('luna', BODY), ('sol', BODY)]


def test_claim_mapping_requires_a_verified_direct_or_inherited_path():
    # A parent quote supports the inherited relation, but cannot establish
    # the child's additional rotatable feature by itself.
    raw = example()
    plan = claims_plan(BODY, raw, reviewed=True)
    assert plan['coverage'][0]['disposition'] == 'expressed'
    child = plan['coverage'][1]
    assert child['disposition'] == 'partial'
    assert child['paths'][0]['kind'] == 'inherited'
    assert child['paths'][0]['claim_path'] == [2, 1]
    assert child['paths'][0]['limitation_verified'] is False
    assert plan['status'] == 'partial'


def test_optional_unknown_preserves_schematic_readiness():
    raw = example()
    addition = '第一端可转动'
    raw['views'][0]['features'].append({'text': addition, 'quote': addition, 'claim': 2})
    raw['unresolved'] = [{'impact': 'geometry', 'views': [1], 'claims': [1],
                          'detail': '实际端面尺寸待核', 'question': '具体端面尺寸是多少？'}]
    plan = claims_plan(BODY, raw, reviewed=True)
    assert plan['status'] == 'plan_ready'
    assert plan['limitations'][0]['impact'] == 'geometry'
    raw['unresolved'][0]['impact'] = 'required'
    assert claims_plan(BODY, raw, reviewed=True)['status'] == 'partial'


def test_views_share_component_identity_without_repeating_display_labels():
    raw = example()
    first = raw['views'][0]
    first['title'] = '图1 端部连接示意图'
    root = {'name': '第一端', 'claims': [1, 2]}
    raw['components'] = [root]
    raw['views'].append({**first, 'title': '图2 端部连接示意图'})
    raw['coverage'][0]['figures'] = [1, 2]
    plan = claims_plan(BODY, raw, reviewed=True)
    assert len(plan['views']) == 1
    assert plan['views'][0]['title'] == '端部连接示意图'
    assert plan['coverage'][0]['figures'] == [1]
    assert plan['components'][0]['view_ids'] == ['fig1']


@pytest.mark.parametrize(('contract', 'has_response'), [
    (contract, has_response) for contract in range(2, 15) for has_response in (True, False)
] + [(version, case) for version in (7, 8, 9, 10, 11, 12, 13, 14) for case in ('truncated','wrong_hash','late_complete')]
  + [(version, 'invalid_structure') for version in (11, 12, 13, 14)])
def test_claims_planning_restarts_after_original_request_without_redispatch(tmp_path, has_response, contract):
    import hashlib

    store, queue, actor = queue_setup(tmp_path)
    w = Workspace(store, actor.owner)
    intake = Intake(w)
    params = {'source_mode': 'claims_only', 'query': '预览', 'materials': BODY}
    initial = intake.apply(params, [{'name': 'claims.txt', 'blob': BODY.encode()}])
    confirmed = intake.apply({'source_mode': 'claims_only', 'query': '确认',
                              'expected_revision': initial['intake_revision']})
    wid = confirmed['workspace_id']
    sends = []

    class Decision:
        def classify(self, text, capture, *, images, semantic=False):
            assert semantic == (contract >= 14)
            sends.append(('luna', text))
            return {'state': 'selected', 'selected': ['structure'], 'uncertain': [],
                    **({'semantic_choices': {'semantic_state': 'needed'}} if semantic else {})}

    class Planner:
        def plan(self, system, text, *, images, evidence_sink, max_output_tokens=24000, claims_wire=False,
                 claims_wire_contract=11):
            sends.append(('sol', text))
            assert ('none_in_source' in system) == (3 <= contract <= 10)
            assert ('condition={state:' in system) == (4 <= contract <= 10)
            assert ('同一结果枝' in system) == (contract == 5)
            assert max_output_tokens == (20000 if contract >= 11 else 36000 if contract >= 6 else 24000)
            assert claims_wire == (contract >= 11)
            assert claims_wire_contract == (contract if contract >= 13 else 11)
            assert ('转换、恢复和退出' in system) == (contract >= 14)
            evidence_sink('request', json.dumps(prepare_plan_payload(
                system, text, images, max_output_tokens=max_output_tokens, claims_wire=claims_wire)).encode())
            if has_response:
                response = {'status': 'completed', 'output': [{'type': 'message',
                    'content': [{'type': 'output_text', 'text': json.dumps(
                        {} if has_response == 'invalid_structure' else wire_example() if claims_wire else example())}]}]}
                if has_response == 'truncated':
                    response.update(status='incomplete', incomplete_details={'reason': 'max_output_tokens'})
                raw = json.dumps(response).encode()
                evidence_sink('response', raw)
                evidence_sink('receipt', json.dumps({'http_status': 200, 'request_id': 'req_saved',
                    'body_sha256': hashlib.sha256(raw).hexdigest() if has_response != 'wrong_hash' else '0' * 64}).encode())
            raise WorkerStopped()

    receipt = queue.enqueue(actor, wid, int(confirmed['intake_revision']), 'plan',
                            query='规划', allow_models=True)
    if contract != 16:
        with store.connect() as connection:
            if contract == 2:
                connection.execute("UPDATE work_jobs SET payload=json_remove(payload,'$.claims_contract') WHERE task_id=?",
                                   (receipt['task_id'],))
            else:
                connection.execute("UPDATE work_jobs SET payload=json_set(payload,'$.claims_contract',?) WHERE task_id=?",
                                   (contract,receipt['task_id']))
    running = WorkWorker(queue, lambda owner: Workspace(store, owner, allow_models=True,
        decision_client=Decision(), text_client=Planner()))
    assert running.run_one()
    with pytest.raises(WorkerStopped):
        running.run_one()
    if has_response == 'late_complete':
        # The response bytes survived, but the transport recorded a timeout
        # before the interrupted Worker committed the job checkpoint.
        with store.connect() as connection:
            updated = connection.execute(
                "UPDATE ops SET state='result_unknown' WHERE owner=? AND op LIKE 'plan_%' AND state='dispatching'",
                (store._owner(actor.owner),))
            assert updated.rowcount == 1
    restored = WorkWorker(queue, lambda owner: Workspace(store, owner))
    result = finish(restored, receipt, queue, actor, wid)
    assert sends == [('luna', BODY), ('sol', BODY)]
    complete = has_response in (True, 'late_complete')
    expected = ('completed' if complete else 'failed' if has_response in {'truncated', 'invalid_structure'}
                else 'unknown')
    assert result['job_status'] == expected
    if has_response == 'invalid_structure':
        assert result['blocking_state'] == 'planner_invalid_output'
        assert result['original_request_id'] == 'req_saved'
        assert not queue.files(actor, wid, receipt['task_id'])
    if has_response == 'truncated':
        assert result['blocking_state'] == 'planner_output_truncated'
        assert result['original_request_id'] == 'req_saved'
        assert '查看任务 ' + receipt['task_id'] in result['message']
        assert not queue.files(actor, wid, receipt['task_id'])
        ledger = store.operation_status(actor.owner, result['operation_id'])
        assert ledger['state'] == 'response_saved_unusable'
        original = store.get_artifact(actor.owner, ledger['error']['response_ref'])
        assert json.loads(original)['incomplete_details']['reason'] == 'max_output_tokens'
    if complete:
        files = queue.files(actor, wid, receipt['task_id'])
        assert files == queue.files(actor, wid, receipt['task_id'])
        value = json.loads(files['claims-plan.json'])
        assert value['schema_version'] == f'claims-drawing-plan-{contract}'
        if contract >= 3:
            assert result['claims_plan_status'] == 'partial'
            assert '关系引证' in result['message']


def test_claims_v6_normalizes_compact_features_and_reuses_exact_quote():
    from patent_drawing.claims_input import claims_system

    raw = example()
    view = raw['views'][0]
    view['features'] = [{'claim': 1, 'quote': QUOTE}, {'claim': 1, 'quote': QUOTE}]
    plan = claims_plan(BODY, raw, reviewed=True, relation_contract=6)
    feature, = plan['views'][0]['features']
    assert feature['text'] == feature['quote'] == QUOTE
    assert BODY.encode()[feature['start']:feature['end']].decode() == QUOTE
    assert 'features 各项只写 {claim,quote}' in claims_system(None, relation_contract=6)
    assert len(claims_system(None, relation_contract=6)) < len(claims_system(None, relation_contract=5))
    with pytest.raises(ValueError):
        claims_plan(BODY, raw, reviewed=True, relation_contract=5)
    view['features'][0]['text'] = '伪造引文'
    with pytest.raises(ValueError):
        claims_plan(BODY, raw, reviewed=True, relation_contract=6)


def test_claims_plan_output_limit_changes_only_when_requested():
    from patent_drawing.text_client import OpenAITextClient

    legacy = prepare_plan_payload('规划', BODY, [])
    assert legacy['max_output_tokens'] == 24000
    current = OpenAITextClient.prepare('规划', BODY, [], max_output_tokens=36000)
    assert current == {**legacy, 'max_output_tokens': 36000}


def test_large_claims_output_has_a_bounded_response_deadline(monkeypatch):
    import httpx
    from patent_drawing import text_client

    deadlines = []
    payloads = []

    def deadline(operation, seconds):
        deadlines.append(seconds)
        return operation(None)

    def provider(request):
        payloads.append(json.loads(request.content)['max_output_tokens'])
        return httpx.Response(200, json={'status': 'completed', 'output': [{
            'type': 'message', 'content': [{'type': 'output_text', 'text': '{}'}],
        }]})

    monkeypatch.setattr(text_client, 'run_with_deadline', deadline)
    planner = text_client.OpenAITextClient('unused', transport=httpx.MockTransport(provider))
    planner.plan('规划', BODY)
    planner.plan('规划', BODY, max_output_tokens=36000)
    assert payloads == [24000, 36000]
    assert deadlines == [300, 420]


def test_claims_v6_decodes_large_complete_output_with_versioned_byte_limit():
    from patent_drawing.errors import DrawingError
    from patent_drawing.text_client import decode_plan_response

    text = json.dumps({'excerpt': 'abcdefghij' * 25000})
    raw = json.dumps({'status': 'completed', 'output': [{
        'type': 'message', 'content': [{'type': 'output_text', 'text': text}],
    }]}).encode()
    assert decode_plan_response(raw, 'req_saved', max_output_tokens=36000)['text'] == text
    with pytest.raises(DrawingError, match='planner_invalid_output'):
        decode_plan_response(raw, 'req_saved')


def test_v7_link_issues_preserve_claim_and_directional_figure_scope():
    raw = example()
    raw['views'].append({**raw['views'][0], 'title': '局部图'})
    raw['figure_links'] = [{'parent': 1, 'child': 2, 'claim': 1, 'anchor': '不存在的锚点', 'quote': QUOTE}]
    checked = claims_plan(BODY,raw,reviewed=True,relation_contract=7)
    issue, = [item for item in checked['limitations'] if item.get('code')=='figure_link_unverified']
    assert issue['claims']==[1] and issue['view_ids']==['fig1','fig2']
    assert issue['figure_roles']=={'parent':'fig1','child':'fig2'}
    assert issue['link_index']==0 and not checked['figure_links']


def test_v7_review_dedup_keeps_distinct_claims_directions_and_original_candidates():
    from patent_drawing.work_worker import _claims_relation_summary

    issue = {'code':'figure_link_unverified','impact':'review','claims':[1],
             'view_ids':['fig1','fig2'],'figure_roles':{'parent':'fig1','child':'fig2'},
             'detail':'图位需确认。','question':'请核对原文。'}
    limits = [issue,dict(issue),{**issue,'claims':[2]},
              {**issue,'figure_roles':{'parent':'fig2','child':'fig1'}}]
    plan = {'schema_version':'claims-drawing-plan-7','relation_candidates':[],
            'relations':[],'limitations':limits}
    report = _claims_relation_summary(plan)
    assert sum('图位需确认' in row for row in report['message'])==3
    assert any('2 条' in row and '图位需确认' in row for row in report['message'])
    assert len(plan['limitations'])==4
    assert sorted(x['count'] for x in report['review_groups'])==[1,1,2]


def test_v7_identical_warnings_keep_opposite_relation_directions_separate():
    from patent_drawing.work_worker import _claims_relation_summary

    issue = {'impact':'review','claims':[1],'view_ids':['fig1'],'detail':'方向需确认。','question':'请核对原文。'}
    base = {'status':'rejected','reasons':[{'code':'direction_mismatch','field':'direction'}]}
    rows = [
        {**base, 'candidate_id': name, 'raw': {'subject':source,'object':target,'direction':'directed'}}
        for name,source,target in (('a','发射侧','接收侧'),('b','接收侧','发射侧'),('c','发射侧','接收侧'))
    ]
    plan = {'schema_version':'claims-drawing-plan-7','relations':[],'relation_candidates':rows,
            'limitations':[{**issue,'candidate_id':row['candidate_id']} for row in rows]}
    groups = _claims_relation_summary(plan)['review_groups']
    assert [group['references'] for group in groups] == [
        [{'candidate_id':'a'},{'candidate_id':'c'}],[{'candidate_id':'b'}]]


def test_v7_distinct_graphic_anchors_and_exact_generic_duplicates():
    from patent_drawing.work_worker import _claims_relation_summary

    common = {'impact': 'review', 'claims': [1], 'view_ids': ['fig1'],
              'detail': '原文待核。', 'question': '请核对原文。'}
    plan = {'schema_version': 'claims-drawing-plan-7', 'relations': [],
            'relation_candidates': [{'status': 'rejected', 'candidate_id': 'a'}],
            'limitations': [common, {**common, 'candidate_id': 'a'},
                            {**common, 'code': 'figure_link_unverified', 'link_index': 0, 'link_anchor': '甲'},
                            {**common, 'code': 'figure_link_unverified', 'link_index': 1, 'link_anchor': '乙'},
                            {**common, 'code': 'figure_link_unverified', 'link_index': 2, 'link_anchor': '甲'}]}
    report = _claims_relation_summary(plan)
    assert len(report['review_groups']) == 3
    assert {tuple(group['limitation_indices']) for group in report['review_groups']} == {(0, 1), (2, 4), (3,)}


def test_v8_review_summary_preserves_missing_actor_scope_and_group_indices():
    from patent_drawing.work_worker import _claims_relation_summary

    one = {'code':'flow_message_endpoints_missing', 'impact':'required','claims':[6],'view_ids':['fig1'],
           'detail':'缺少可核验的收发主体。','question':'请核对原文。'}
    plan = {'schema_version':'claims-drawing-plan-8', 'relations':[], 'relation_candidates':[],
            'limitations':[one,dict(one),{**one,'claims':[8]}]}
    groups = _claims_relation_summary(plan)['review_groups']
    assert [group['limitation_indices'] for group in groups]==[[0,1],[2]]


def test_v10_review_summary_only_merges_duplicate_condition_failures():
    from patent_drawing.work_worker import _claims_relation_summary

    common = {'impact': 'required', 'claims': [1], 'view_ids': ['fig1'],
              'detail': '指定权项未命中逐字引文。', 'question': '核对条件。'}
    issues, candidates = [], []
    for index, quote in enumerate(('压力超限', '压力超限', '转速超限')):
        ident = str(index)
        issues.append({**common, 'candidate_id': ident})
        candidates.append({'candidate_id': ident, 'source_index': index, 'status': 'rejected',
                           'reasons': [{'code': 'citation_not_found', 'field': 'condition.expression'}],
                           'raw': {'subject': '控制端', 'object': '停止输出',
                                   'condition': {'expression': {'source': {'claim': 1, 'quote': quote}}}}})
    plan = {'schema_version': 'claims-drawing-plan-10', 'relations': [],
            'relation_candidates': candidates, 'limitations': issues}
    summary = _claims_relation_summary(plan)
    assert [group['limitation_indices'] for group in summary['review_groups']] == [[0, 1], [2]]
    assert '2 组待核项' in '\n'.join(summary['message'])


def test_v9_recovers_only_complete_explicit_field_actions():
    body='1. 一种控制方法，包括：所述提示关联批次、版本和有效期。\n\n2. 根据权利要求1所述的方法，对每次参考记录ref_id、批次和时间。'
    raw={'views':[{'type':'flow','title':'字段记录','purpose':'按原文表达字段',
                  'features':[{'claim':1,'quote':'所述提示关联批次、版本和有效期'},
                              {'claim':2,'quote':'对每次参考记录ref_id、批次和时间'}]}],
         'coverage':[{'claim':i,'figures':[1],'reason':'同图'} for i in (1,2)],'unresolved':[]}
    plan=claims_plan(body,raw,reviewed=True,relation_contract=9)
    assert len(plan['relations'])==2
    assert all(row['origin']=='source_derived' for row in plan['relations'])
    assert [row['rule'] for row in plan['source_derivations']]==['explicit_field_binding','per_occurrence_record']
    assert len(plan['flow_edges'])==2
    assert all(row['raw'] for row in plan['relation_candidates'])
    raw['views'][0]['features'][1]['quote']='ref_id、批次和时间'
    assert len(claims_plan(body,raw,reviewed=True,relation_contract=9)['relations'])==1
    raw['views'][0]['features'][0]['quote']='所述提示关联批次、版本'
    assert not claims_plan(body,raw,reviewed=True,relation_contract=9)['relations']


def test_v9_review_summary_keeps_original_and_source_provenance():
    from patent_drawing.work_worker import _claims_relation_summary
    body='1. 一种处理方法，包括：所述提示关联批次、版本和有效期。'
    raw={'views':[{'type':'flow','title':'字段','purpose':'示意绑定',
                   'features':[{'claim':1,'quote':'所述提示关联批次、版本和有效期'}]}],
         'coverage':[{'claim':1,'figures':[1],'reason':'同图'}],'unresolved':[]}
    plan=claims_plan(body,raw,reviewed=True,relation_contract=9)
    summary=_claims_relation_summary(plan)
    assert 'review_groups' in summary
    assert any('原文补齐' in line for line in summary['message'])


@pytest.mark.parametrize('action', [
    '不得对每次参考记录ref_id、批次和时间',
    '禁止所述提示关联批次、版本和有效期',
    '所述提示不关联批次、版本和有效期',
    '可选地对每次参考记录ref_id、批次和时间',
])
def test_v9_source_recovery_does_not_invent_affirmative_fields(action):
    body='1. 一种处理方法，包括：'+action+'。'
    raw={'views':[{'type':'flow','title':'字段','purpose':'原文限制',
                  'features':[{'claim':1,'quote':action}]}],
         'coverage':[{'claim':1,'figures':[1],'reason':'同图'}],'unresolved':[]}
    assert not claims_plan(body,raw,reviewed=True,relation_contract=9)['relations']


@pytest.mark.parametrize('interrupted', [False, True])
@pytest.mark.parametrize('revision', [1, 2])
def test_paired_luna_rejudges_only_disputes_and_reuses_original_task(tmp_path, interrupted, revision):
    from threading import Barrier

    import httpx

    from patent_drawing.figure_routing import DecisionClient

    store, queue, actor = queue_setup(tmp_path)
    intake = Intake(Workspace(store, actor.owner))
    initial = intake.apply({'source_mode': 'claims_only', 'query': '预览', 'materials': BODY},
                           [{'name': 'claims.txt', 'blob': BODY.encode()}])
    confirmed = intake.apply({'source_mode': 'claims_only', 'query': '确认',
                              'expected_revision': initial['intake_revision']})
    wid = confirmed['workspace_id']
    admitted = queue.enqueue(actor, wid, int(confirmed['intake_revision']), 'plan',
                             query='规划', allow_models=True)
    with store.connect() as connection:
        if revision == 1:
            connection.execute("UPDATE work_jobs SET payload=json_remove(payload,'$.luna_question_revision') WHERE task_id=?",
                               (admitted['task_id'],))
        else:
            connection.execute("UPDATE work_jobs SET payload=json_set(payload,'$.luna_question_revision',2) WHERE task_id=?",
                               (admitted['task_id'],))
    baseline = decision_payload(BODY, semantic=True, question_revision=revision)
    disputed = ('semantic_messages', 'semantic_state')
    calls = []
    barrier = Barrier(2, timeout=5)

    def provider(request):
        payload = json.loads(request.content)
        assert payload['input'] == BODY
        names = [q['name'] for q in payload['questions']]
        slot = 'C' if tuple(names) == disputed else 'A' if payload == baseline else 'B'
        calls.append(slot)
        if slot != 'C':
            assert len(names) == 11
            barrier.wait()
        if revision == 2:
            prompts = {q['name']: q['instructions'] for q in payload['questions']}
            if slot == 'B':
                assert '同一对象的记录配对' in prompts['semantic_messages']
                assert '现有图型能否完整表达' in prompts['relationship']
            if slot == 'A':
                assert '传递内容' in prompts['semantic_messages']
                assert '确有另设视图的必要' in prompts['relationship']

        answer = {
            'A': {'semantic_messages': 'needed', 'semantic_state': 'needed'},
            'B': {'semantic_messages': 'not_needed', 'semantic_state': 'uncertain'},
            'C': {'semantic_messages': 'not_needed', 'semantic_state': 'not_needed'},
        }[slot]
        rows = [{'name': name, 'type': 'choice', 'choice': answer.get(name, 'not_needed'),
                 'confidence': {'structure': 0.4, 'flow': True, 'system': -0.1}.get(name, 0.9)} for name in names]
        return httpx.Response(200, json={'model': 'gpt-6-luna', 'answers': rows,
                                        'usage': {'input_tokens': 314}}, headers={'x-request-id': 'req_' + slot})

    class Decision(DecisionClient):
        def classify_ballot(self, payload, capture):
            result = super().classify_ballot(payload, capture)
            if interrupted and payload == baseline:
                raise WorkerStopped()
            return result

    worker = WorkWorker(queue, lambda owner: Workspace(
        store, owner, allow_models=True, decision_client=Decision('test', transport=httpx.MockTransport(provider))))
    if interrupted:
        with pytest.raises(WorkerStopped):
            worker.run_one()
        assert sorted(calls) == ['A', 'B']
    worker.run_one()
    job = queue.inspect(actor, wid, task_id=admitted['task_id'])
    assert job['task_id'] == admitted['task_id']
    assert job['execution_stage'] == 'planning'
    w = Workspace(store, actor.owner)
    replay = w.route_figures(wid, '继续')
    assert replay['model_calls'] == 0
    assert sorted(calls) == ['A', 'B', 'C']
    assert replay['semantic_choices']['semantic_messages'] == 'not_needed'
    assert replay['semantic_choices']['semantic_state'] == 'uncertain'
    assert replay['vote'] == {'disagreements': list(disputed), 'rejudged': list(disputed),
                              'uncertain': ['semantic_state'], 'attempts': 3}
    record = store.load(actor.owner, replay['operation_id'] + ':receipt')
    assert [row['request_id'] for row in record['ballots'].values()] == ['req_A', 'req_B', 'req_C']
    assert record['ballots']['A']['usage']['input_tokens'] == 314
    if revision == 1:
        assert 'confidence_watch' not in record and 'low_confidence' not in job['claims_vote_review']
    else:
        assert record['luna_question_revision'] == 2
        assert record['confidence_watch'] == {'threshold': 0.5, 'same_low': ['structure']}
        assert job['claims_vote_review']['low_confidence'] == 1
        assert record['ballots']['A']['confidence']['structure'] == 0.4
        assert not {'flow', 'system'} & record['ballots']['A']['confidence'].keys()


def test_luna_revised_questions_keep_other_topics_and_require_coverage_in_existing_figures():
    from patent_drawing.claims_input import claims_system

    previous = decision_payload(BODY, semantic=True)
    revised = decision_payload(BODY, semantic=True, question_revision=2)
    changed = {'relationship', 'semantic_exits', 'semantic_messages', 'semantic_state', 'semantic_merge'}
    assert revised['input'] == BODY and len(revised['questions']) == 11
    assert {a['name'] for a, b in zip(previous['questions'], revised['questions'], strict=True) if a != b} == changed
    route = {'selected': [], 'semantic_choices': {}, 'luna_question_revision': 2}
    system = claims_system(route, relation_contract=15)
    assert '关系图候选' in system and '现有图能完整表达' in system and '逐字引文' in system


@pytest.mark.parametrize(('dispute', 'limit', 'third_status'), [
    (False, 3, 200), (True, 2, 200), (True, 3, 503),
])
def test_paired_luna_keeps_agreements_and_waits_on_unresolved_questions(tmp_path, dispute, limit, third_status):
    import httpx

    from patent_drawing.figure_routing import DecisionClient, vote_decisions

    calls = []
    payload = decision_payload(BODY, semantic=True)

    def provider(request):
        body = json.loads(request.content)
        questions = body['questions']
        calls.append([q['name'] for q in questions])
        if len(questions) == 1:
            return httpx.Response(third_status, headers={'x-request-id': 'req_C'})
        sequence = 'not_needed' if dispute and body == payload else 'needed'
        rows = [{'name': q['name'], 'type': 'choice',
                 'choice': sequence if q['name'] == 'sequence' else 'needed'} for q in questions]
        rows[-1] = {'name': questions[-1]['name'], 'type': 'refusal'}
        return httpx.Response(200, json={'model': 'gpt-6-luna', 'answers': rows})

    store = OperationStore(tmp_path)
    client = DecisionClient('test', transport=httpx.MockTransport(provider))
    result, sent = vote_decisions(store, 'test-owner', 'parent', payload, client, limit)
    replay, resent = vote_decisions(store, 'test-owner', 'parent', payload, client, limit)
    assert replay == result and resent == 0
    assert len(calls) == sent == (3 if dispute and limit == 3 else 2)
    assert result['choices']['sequence'] == ('uncertain' if dispute else 'needed')
    assert result['choices']['flow'] == 'needed'
    assert result['choices']['semantic_applicability'] == 'uncertain'
    assert result['vote']['rejudged'] == (['sequence'] if sent == 3 else [])


def test_relationship_coverage_requires_verified_claim_and_figure_placement():
    from patent_drawing.claims_wire import claims_wire_format

    raw = wire_example()
    raw['relationship_coverage'] = [{'t': 'relationship_direction', 'v': 1, 'e': [1]}]
    tasks = {'relationship_direction': {'state': 'required', 'expression': 'qualitative'}}
    assert 'relationship_coverage' in claims_wire_format(16)['schema']['properties']
    okay = claims_plan(BODY, raw, reviewed=True, relation_contract=16, relationship_tasks=tasks)
    review = okay['relationship_review']
    assert review['rows'][0]['placements'][0]['view_id'] == 'fig1'
    assert review['rows'][0]['placements'][0]['citations'][0]['quote'] == QUOTE
    assert review['can_omit_independent_view']
    raw['relationship_coverage'][0]['v'] = 99
    invalid = claims_plan(BODY, raw, reviewed=True, relation_contract=16, relationship_tasks=tasks)
    assert invalid['status'] == 'partial'
    assert invalid['relationship_review']['rows'][0]['state'] == 'pending'
    assert not invalid['relationship_review']['can_omit_independent_view']


def test_relationship_ballots_use_distinct_tasks_and_do_not_drop_a_conflict(tmp_path):
    import httpx

    from patent_drawing.figure_routing import DecisionClient, vote_decisions

    payload = decision_payload(BODY, semantic=True, question_revision=3)
    calls = []

    def mock(request):
        body = json.loads(request.content)
        assert body['input'] == BODY
        questions = {q['name']:q for q in body['questions']}
        calls.append(questions)
        rows = []
        for name, q in questions.items():
            values = {o['value'] for o in q['choices']}
            choice = ('explicit' if name == 'relationship_direction' else 'not_disclosed') if 'explicit' in values else (
                'not_applicable'
                if 'not_applicable' in values else 'not_needed')
            rows.append({'type': 'choice', 'name': name, 'choice': choice})
        return httpx.Response(200, json={'model':'gpt-6-luna','answers':rows})

    client = DecisionClient('offline',transport=httpx.MockTransport(mock))
    result, sent = vote_decisions(OperationStore(tmp_path), 'owner', 'route', payload, client, 3, question_revision=3)
    assert sent == 3 and set(calls[-1]) == {'relationship_direction'}
    assert 'relationship' not in calls[0]
    assert len(calls[0]) == len(calls[1]) == 14
    assert result['relationship_tasks']['relationship_direction']['state'] == 'required'
    assert result['relationship_tasks']['relationship_boundary']['state'] == 'absent'
    assert result['choices']['relationship'] == 'uncertain'


def test_relationship_coverage_flags_duplicate_independent_view():
    raw = wire_example()
    raw['views'].append({**raw['views'][0], 'title': '端部关系重复图', 'type': 'relationship'})
    raw['relationship_coverage'] = [
        {'t': 'relationship_direction', 'v': 1, 'e': [1]},
        {'t': 'relationship_direction', 'v': 2, 'e': [1]},
    ]
    plan = claims_plan(BODY, raw, reviewed=True, relation_contract=16)
    assert plan['status'] == 'partial'
    assert plan['relationship_review']['can_omit_independent_view']
    assert plan['relationship_review']['review_independent_views'] == ['fig2']


def test_relationship_coverage_accepts_all_source_bound_view_citations():
    quotes = [QUOTE[:length] for length in range(2, 11)]
    wire = wire_example()
    wire['citations'] = [{'c': 1, 'q': quote, 'x': None} for quote in quotes]
    indices = list(range(1, len(quotes) + 1))
    wire['views'][0]['features'] = indices
    wire['relationship_coverage'] = [{'t': 'relationship_direction', 'v': 1, 'e': indices}]
    result = claims_plan(BODY, wire, reviewed=True, relation_contract=16)
    placement, = result['relationship_review']['rows'][0]['placements']
    assert [cite['quote'] for cite in placement['citations']] == quotes


@pytest.mark.parametrize('primary_cap', [20000, 25000])
@pytest.mark.parametrize('outcome', ['complete', 'recovered', 'interrupted', 'truncated_twice', 'other_incomplete'])
def test_claims_truncation_continues_once_on_the_original_task(tmp_path, outcome, primary_cap):
    from dataclasses import replace
    from hashlib import sha256
    import httpx

    from patent_drawing.figure_routing import DecisionClient
    from patent_drawing.text_client import OpenAITextClient

    store, queue, actor = queue_setup(tmp_path)
    queue.config = replace(queue.config, claims_output_caps={sha256(BODY.encode()).hexdigest(): primary_cap}
                           if primary_cap == 25000 else {})
    intake = Intake(Workspace(store, actor.owner))
    draft = intake.apply({'source_mode':'claims_only','query':'预览','materials':BODY},
                         [{'name':'claims.txt','blob':BODY.encode()}])
    x = intake.apply({'source_mode':'claims_only','query':'确认','expected_revision':draft['intake_revision']})
    wid = x['workspace_id']
    job = queue.enqueue(actor, wid, int(x['intake_revision']), 'plan', query='规划', allow_models=True)
    queue.config = replace(queue.config, claims_output_caps={})
    sent = []

    def luna(request):
        payload = json.loads(request.content)
        return httpx.Response(200,json={'model':'gpt-6-luna','answers':[
            {'type':'choice','name':q['name'],'choice':next(
                option['value'] for option in q['choices'] if option['value'] in
                {'not_needed','not_disclosed','not_applicable'})} for q in payload['questions']]})

    def sol(request):
        payload = json.loads(request.content)
        sent.append(payload)
        assert payload['input'][1] == {'role':'user','content':BODY}
        assert payload['max_output_tokens'] == (primary_cap if len(sent) == 1 else 25000)
        if outcome != 'complete' and (len(sent)==1 or outcome=='truncated_twice'):
            reason = 'content_filter' if outcome=='other_incomplete' else 'max_output_tokens'
            body={'status':'incomplete','incomplete_details':{'reason':reason},'output':[]}
        else:
            wire={**wire_example(),'relationship_coverage':[]}
            body={'status':'completed','output':[{'type':'message','content':[
                {'type':'output_text','text':json.dumps(wire)}]}]}
        return httpx.Response(200,json=body,headers={'x-request-id':f'req_{len(sent)}'})

    class InterruptedPlanner(OpenAITextClient):
        def plan(self, *args, evidence_sink, **kwargs):
            def capture(kind, raw):
                evidence_sink(kind, raw)
                if outcome=='interrupted' and len(sent)==1 and kind=='response':
                    raise WorkerStopped()
            return super().plan(*args,evidence_sink=capture,**kwargs)

    worker = WorkWorker(queue, lambda owner: Workspace(
        store, owner, allow_models=True, decision_client=DecisionClient('test',transport=httpx.MockTransport(luna)),
        text_client=InterruptedPlanner('test',transport=httpx.MockTransport(sol))))
    assert worker.run_one()
    if outcome=='interrupted':
        with pytest.raises(WorkerStopped):
            worker.run_one()
    result=finish(worker,job,queue,actor,wid)
    count=1 if outcome in {'complete','other_incomplete'} else 2
    assert result['job_status']==('failed' if outcome in {'truncated_twice','other_incomplete'} else 'completed')
    assert len(sent)==count and result['task_id']==job['task_id']
    if count==2:
        if primary_cap == 20000:
            assert sent[1] == {**sent[0], 'max_output_tokens': 25000}
            assert result['claims_recovery']['strategy'] == 'cap-1'
        else:
            assert sent[0] != sent[1] and '压缩续做' in sent[1]['input'][0]['content']
            assert result['claims_recovery']['strategy'] == 'compact-1'
        assert result['claims_recovery']['primary_request_id']=='req_1'
        assert store.operation_status(actor.owner,result['claims_recovery']['primary_operation'])['state']=='response_saved_unusable'
    assert not worker.run_one() and len(sent)==count


def test_staged_control_subject_must_bind_to_its_own_action_scope():
    from patent_drawing.claims_input import read_claims_text
    from patent_drawing.claims_relations import _find_span, _staged_control_endpoints

    subject='对同一组材料和工具分阶段实施所述加工'
    action='继续处理'
    source,ranges=read_claims_text(f'1. 一种方法，其特征在于：{subject}；仅在支撑完整时{action}。')
    blob=source.encode()
    spans={role:_find_span(blob,ranges,{'claim':1,'quote':quote})[0]
           for role,quote in [('subject',subject),('object',action),('relation',action)]}
    assert _staged_control_endpoints(blob,ranges,spans,[subject,action],1,'directed')
    assert not _staged_control_endpoints(blob,ranges,spans,[subject,action],2,'directed')
    assert not _staged_control_endpoints(blob,ranges,spans,[subject,action],1,'symmetric')

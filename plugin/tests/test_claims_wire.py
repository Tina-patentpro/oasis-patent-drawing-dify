import json
from copy import deepcopy

import pytest

from patent_drawing.claims_input import claims_plan
from patent_drawing.errors import DrawingError
from patent_drawing.text_client import OpenAITextClient, decode_plan_response


BODY = '1. 一种装置，包括依次相连的第一端和第二端。'
ACTION = '依次相连的第一端和第二端'


def proposal():
    return {
        'citations': [{'c': 1, 'q': quote, 'x': None} for quote in ('第一端', '第二端', ACTION)],
        'views': [{'type': 'structure', 'title': '端部连接图', 'purpose': '表示端部连接', 'features': [3]}],
        'coverage': [], 'components': [], 'unresolved': [], 'figure_links': [],
        'relations': [{'s': None, 'o': None, 'k': '相连', 'd': 'ordered',
                       'c': 1, 'p': [1], 'v': [1], 'r': True, 'e': [1, 2, 3], 'h': 0}],
        'flow_edges': [{'r': 1, 'k': 'transition', 'e': None}],
    }


def response(text, **extra):
    return json.dumps({'status': 'completed', 'output': [{'type': 'message',
        'content': [{'type': 'output_text', 'text': text}]}], **extra}).encode()


def test_v11_expands_references_and_derives_only_evidenced_coverage():
    raw = proposal()
    previous = deepcopy(raw)
    plan = claims_plan(BODY, raw, reviewed=True, relation_contract=11, response_sha256='e'*64)
    assert plan['schema_version'] == 'claims-drawing-plan-11'
    relation, = plan['relations']
    edge, = plan['flow_edges']
    assert relation['subject'] == '第一端' and relation['object'] == '第二端'
    assert edge['label']['quote'] == relation['evidence']['relation']['quote'] == ACTION
    assert plan['coverage'][0]['figures'] == [1]
    assert plan['wire_evidence']['relation_indices'] == [[1, 2, 3]]
    assert raw == previous


def test_v11_schema_and_hard_cap_are_bound_to_the_new_request():
    new = OpenAITextClient.prepare('图单', BODY, [], max_output_tokens=20000, claims_wire=True)
    assert new['input'][1]['content'] == BODY
    assert new['max_output_tokens'] == 20000
    fmt = new['text']['format']
    assert fmt['type'] == 'json_schema' and fmt['strict']
    assert fmt['schema']['additionalProperties'] is False
    assert fmt['schema']['properties']['relations']['items']['additionalProperties'] is False
    with pytest.raises(DrawingError):
        OpenAITextClient.prepare('图单', BODY, [], max_output_tokens=36000, claims_wire=True)
    assert OpenAITextClient.prepare('图单', BODY, [], max_output_tokens=36000)['text'] == {
        'format': {'type': 'json_object'}}


@pytest.mark.parametrize('bad_index', [0, -1, 4])
def test_v13_bad_guard_reference_only_blocks_its_own_candidate(bad_index):
    raw = proposal()
    bad = deepcopy(raw['relations'][0])
    bad['h'] = {'op': 'all', 'source': bad_index, 'items': [
        {'op': 'literal', 'source': 1, 'items': []},
        {'op': 'literal', 'source': 2, 'items': []},
    ]}
    raw['relations'].insert(0, bad)
    raw['flow_edges'].insert(1, {'r': 2, 'k': 'transition', 'e': None})
    text = json.dumps(raw)
    original = response(text)
    with pytest.raises(DrawingError, match='planner_invalid_output'):
        decode_plan_response(original, 'req_wire', max_output_tokens=20000, claims_wire=True)
    saved = decode_plan_response(original, 'req_wire', max_output_tokens=20000, claims_wire=True,
                                 claims_wire_contract=13)
    assert saved['text'] == text
    plan = claims_plan(BODY, raw, reviewed=True, relation_contract=13, response_sha256='e'*64)
    rejected, accepted = plan['relation_candidates']
    assert plan['status'] == 'partial'
    assert rejected['status'] == 'rejected' and rejected['source_index'] == 0
    assert rejected['wire_guard']['raw'] == bad['h']
    assert rejected['wire_guard']['errors'] == [{'path': 'h.source', 'value': bad_index}]
    assert rejected['claim'] == 1 and rejected['view_ids'] == ['fig1'] and rejected['model_required']
    assert plan['flow_candidates'][0]['relation_candidate_id'] == rejected['candidate_id']
    edge, = plan['flow_edges']
    assert edge['relation_id'] == accepted['candidate_id']


@pytest.mark.parametrize('defect', [None, 'extra_guard', 'exception', 'prefix_exception', 'colon_guard',
                                    'postfix_exception', 'inapplicable', 'foreign_premise',
                                    'partial_guard', 'bad_child', 'wrong_view', 'wrong_action'])
def test_v13_rebuilds_zero_root_only_from_complete_premise_and_branch(defect):
    premise = '在具有原始样本的条件下'
    guard = '来源允许且分组一致'
    action = f'仅接纳{guard}的候选'
    preceding = {'extra_guard': '仅在样本来自训练集合时，', 'prefix_exception': '除封存样本外，',
                 'colon_guard': '仅在样本来自训练集合时：'}.get(defect, '')
    following = {'exception': '，除非样本来自封存集合', 'postfix_exception': '，但封存样本除外',
                 'inapplicable': '，但对封存样本不适用'}.get(defect, '')
    body = (f'1. 一种处理方法，{premise}：获取数据。\n'
            f'2. 一种处理方法，在具有其他样本的条件下：核对数据。\n'
            f'3. 根据权利要求1所述的方法，核对输入，{preceding}{action}{following}。')
    data = proposal()
    quotes = [(1, premise), (3, action), (3, '候选'), (3, '接纳'), (3, '来源允许'),
              (2, '在具有其他样本的条件下'), (3, '核对输入')]
    data['citations'] = [{'c': c, 'q': q, 'x': action if i in {2, 3, 4} else None}
                         for i, (c, q) in enumerate(quotes)]
    data['views'][0].update(type='flow', features=[7 if defect=='wrong_view' else 2])
    data['relations'][0].update(c=3, p=[3,1], k='接纳', d='directed', e=[3,4,2], h={
        'op':'all', 'source':0, 'items':[
            {'op':'literal', 'source':6 if defect=='foreign_premise' else 1,'items':[]},
            {'op':'literal', 'source':9 if defect=='bad_child' else 5 if defect=='partial_guard' else 2,'items':[]},
        ],
    })
    if defect == 'wrong_action':
        data['relations'][0]['k'] = '拒绝'
    plan = claims_plan(body, data, reviewed=True, relation_contract=13, response_sha256='e'*64)
    item, = plan['relation_candidates']
    assert item['wire_guard']['raw'] == data['relations'][0]['h']
    if defect:
        assert item['status']=='rejected'
        assert not plan['relations'] and not plan['flow_edges']
        assert 'flow_guard_unverified' in plan['flow_candidates'][0]['reasons']
        if defect == 'bad_child':
            assert item['wire_guard']['errors'][1] == {'path':'h.items[1].source', 'value':9}
        return
    relation, = plan['relations']
    assert relation['condition']['expression']['source']['quote'] == guard
    assert [p['quote'] for p in relation['condition']['applicability']] == [premise]
    assert [p['quote'] for p in relation['condition']['terms']] == [guard]
    assert item['wire_guard']['status'] == 'recovered'
    assert plan['flow_edges'][0]['relation_id']==item['candidate_id']


@pytest.mark.parametrize('defect', [None, 'missing_conjunct', 'missing_alternative', 'colon_guard',
                                    'ancestor_premise', 'truncated_path', 'wrong_action', 'equivalent'])
def test_v13_contiguous_conjunction_requires_the_entire_local_scope(defect):
    continuation = {'missing_conjunct':'且安全锁有效','missing_alternative':'或安全锁有效'}.get(defect,'')
    action = f'在门闭合且电源稳定{continuation}时，控制端发送结果'
    body = '1. 一种处理方法，其特征在于，' + action + '。'
    if defect=='colon_guard':
        body = '1. 一种处理方法，其特征在于，仅在安全锁有效时：' + action + '。'
    claim = 2 if defect in {'ancestor_premise','truncated_path'} else 1
    if claim==2:
        body = ('1. 一种处理方法，在具有原始样本的条件下：获取数据。\n'
                '2. 根据权利要求1所述的方法，' + action + '。')
    raw = proposal()
    raw['citations'] = [{'c':claim, 'q':q, 'x':None}
                        for q in ('控制端', '结果', action, '门闭合', '电源稳定')]
    raw['views'][0].update(type='flow', features=[3])
    path = [2] if defect=='truncated_path' else [2,1] if claim==2 else [1]
    raw['relations'][0].update(c=claim,p=path,k='接收' if defect=='wrong_action' else '发送',
                              d='directed', h={'op':'all','source':0,'items':[
        {'op':'literal', 'source':4,'items':[]}, {'op':'literal','source':5,'items':[]},
    ]})
    if defect=='equivalent':
        raw['citations'].append({'c':1,'q':'在门闭合且电源稳定时','x':None})
        original = deepcopy(raw['relations'][0])
        original['h']['source'] = 6
        raw['relations'].append(original)
        raw['flow_edges'].append({'r':2,'k':'transition','e':None})
    plan = claims_plan(body, raw, reviewed=True, relation_contract=13)
    if defect and defect!='equivalent':
        assert not plan['relations']
    else:
        relation, = plan['relations']
        assert relation['condition']['operator'] == 'all'
        assert [p['quote'] for p in relation['condition']['terms']] == ['门闭合', '电源稳定']
        assert plan['relation_candidates'][0]['wire_guard']['rule'] == 'complete_local_conjunction'
        if defect=='equivalent':
            assert plan['relation_candidates'][0]['status']=='equivalent'
            assert plan['relation_candidates'][1]['candidate_id']==relation['id']
            assert all(e['relation_id']==relation['id'] for e in plan['flow_edges'])


def test_v13_different_invalid_indices_remain_separate_in_readback():
    from patent_drawing.work_worker import _claims_relation_summary

    raw = proposal()
    raw['flow_edges'] = []
    row = raw['relations'][0]
    row['h'] = {'op':'literal', 'source':-1, 'items':[]}
    other = deepcopy(row)
    other['h']['source'] = 99
    raw['relations'].append(other)
    plan = claims_plan(BODY, raw, reviewed=True, relation_contract=13)
    review = _claims_relation_summary(plan)
    assert [g['count'] for g in review['review_groups']]==[1,1]


@pytest.mark.parametrize('mutation', [
    None, 'wrong_endpoint', 'independent_premise', 'extra_predicate', 'wrong_operator',
    'negated_action', 'leading_guard', 'trailing_exception', 'trailing_inapplicable',
    'trailing_local_exception', 'wrong_direction', 'wrong_view',
])
def test_v12_recovers_only_explicit_role_and_scope_evidence(mutation):
    premise = '在具有原始样本的条件下'
    independent = '在具有其他样本的条件下'
    local = '来源允许且分组一致'
    action = f'仅接纳{local}的候选'
    leading = '仅在样本来自训练集合的条件下，' if mutation == 'leading_guard' else ''
    after_action = {
        'trailing_exception': '除非样本来自封存集合',
        'trailing_inapplicable': '当样本来自封存集合时不适用',
        'trailing_local_exception': '在样本来自封存集合时例外',
    }.get(mutation, '保存结果')
    text = (f'1. 一种处理方法，{premise}：获取数据。\n'
            f'2. 一种数据处理方法，{independent}：收集数据。\n'
            f'3. 根据权利要求1所述的方法，核对输入，'
            f'{leading}{"不得" if mutation == "negated_action" else ""}{action}，{after_action}。')
    quotes = [(3, action), (1, premise), (3, local), (3, '核对输入'), (2, independent)]
    raw = proposal()
    raw['citations'] = [{'c': c, 'q': q, 'x': None} for c, q in quotes]
    raw['views'][0].update(type='flow', features=[4 if mutation == 'wrong_view' else 1])
    row = raw['relations'][0]
    row.update(s='候选H', o='接纳项', k='接纳', d='directed', c=3, p=[3, 1],
               e=[4 if mutation == 'wrong_endpoint' else 1, 1, 1],
               h={'op': 'any' if mutation == 'wrong_operator' else 'all', 'source': 1, 'items': [
                   {'op': 'literal', 'source': 5 if mutation == 'independent_premise' else 2, 'items': []},
                   {'op': 'literal', 'source': 4 if mutation == 'extra_predicate' else 3, 'items': []},
               ]})
    if mutation == 'wrong_direction':
        row['s'], row['o'] = row['o'], row['s']
    old = claims_plan(text, raw, reviewed=True, relation_contract=11, response_sha256='e'*64)
    new = claims_plan(text, raw, reviewed=True, relation_contract=12, response_sha256='e'*64)
    before, = old['relation_candidates']
    after, = new['relation_candidates']
    assert before['candidate_id'] == after['candidate_id']
    assert before['raw'] == after['raw']
    assert before['status'] == 'rejected'
    if mutation:
        assert after == before
        assert not new['relations']
        return
    relation, = new['relations']
    assert (relation['subject'], relation['object']) == ('候选', '接纳')
    condition = relation['condition']
    assert condition['expression']['source']['quote'] == local
    assert [p['quote'] for p in condition['applicability']] == [premise]
    assert [p['quote'] for p in condition['terms']] == [local]
    assert after['scope_recovery']['original_evidence'] == before['evidence']
    assert after['scope_recovery']['original_reasons'] == before['reasons']


@pytest.mark.parametrize('action_case', ['merge', 'prohibited_merge', 'target_excluded'])
def test_v12_paired_paths_require_the_exact_affirmative_action(action_case):
    premise = '在具有原始样本的条件下'
    participation = '并不结合' if action_case == 'target_excluded' else '并结合'
    related = f'基于上下文C的依存关系表示{participation}目标T进行编码的目标相关编码路径h(C,T)'
    general = '不输入当前T指示的一般编码路径g(C)分别生成表示'
    merge = '将两路表示连接为z'
    prohibited = merge + '作为唯一连接方式'
    body = (f'1. 一种处理方法，{premise}：由{related}及{general}，{merge}，'
            f'另一步骤不得{prohibited}。')
    action = {'merge': merge, 'prohibited_merge': prohibited, 'target_excluded': related}[action_case]
    raw = proposal()
    raw['citations'] = [{'c': 1, 'q': action, 'x': f'{merge}，' if action_case == 'merge' else None},
                        {'c': 1, 'q': related, 'x': None}, {'c': 1, 'q': premise, 'x': None}]
    if action_case == 'merge':
        raw['citations'].append({'c': 1, 'q': f'{merge}，', 'x': None})
    raw['views'][0].update(type='flow', features=[4 if action_case == 'merge' else 1])
    row = raw['relations'][0]
    row.update(s='h(C,T)表示', o='z', k='连接', d='directed', e=[2, 1, 1],
               h={'op': 'literal', 'source': 3, 'items': []})
    if action_case == 'target_excluded':
        row.update(s='C的依存关系表示与T', o='h(C,T)表示', k='结合目标编码生成')
    old = claims_plan(body, raw, reviewed=True, relation_contract=11, response_sha256='e'*64)
    new = claims_plan(body, raw, reviewed=True, relation_contract=12, response_sha256='e'*64)
    assert not old['relations']
    if action_case == 'merge':
        relation, = new['relations']
        assert relation['evidence']['relation']['quote'] == merge
        assert new['flow_edges'][0]['relation_id'] == relation['id']
    else:
        assert not new['relations']
        assert not new['flow_edges']


@pytest.mark.parametrize('basis', ['依据', '不依据'])
def test_v12_distinguishes_calibration_basis_from_its_exclusion(basis):
    premise = '在具有原始样本的条件下'
    cost = '漏放和误拒成本'
    thresholds = '置信阈值τ及类间差额γ'
    action = f'在验证集合{basis}{cost}选择并冻结{thresholds}'
    body = (f'1. 一种处理方法，{premise}：获取数据。\n'
            f'2. 根据权利要求1所述的方法，{action}。')
    raw = proposal()
    raw['citations'] = [{'c': 2, 'q': action, 'x': None}, {'c': 1, 'q': premise, 'x': None}]
    raw['views'][0].update(type='flow', features=[1])
    raw['relations'][0].update(s='验证集合及'+cost, o=thresholds, k='选择并冻结', d='directed',
                               c=2, p=[2, 1], e=[1, 1, 1],
                               h={'op': 'literal', 'source': 2, 'items': []})
    plan = claims_plan(body, raw, reviewed=True, relation_contract=12, response_sha256='e'*64)
    assert bool(plan['relations']) == (basis == '依据')
    assert bool(plan['flow_edges']) == (basis == '依据')


@pytest.mark.parametrize('fault', ['unknown_property', 'bad_index', 'bool_index', 'duplicate_key', 'runaway_key'])
def test_v11_rejects_malformed_provider_output_with_original_bytes(fault):
    raw = proposal()
    if fault == 'unknown_property':
        raw['relations'][0]['unexpected'] = 'long text'
    if fault in {'bad_index', 'bool_index'}:
        raw['relations'][0]['e'][0] = 99 if fault == 'bad_index' else True
    text = json.dumps(raw)
    if fault == 'duplicate_key':
        text = text.replace('"citations":', '"citations":[],"citations":', 1)
    if fault == 'runaway_key':
        text = text[:-1] + ',"' + '__NO'*200
    blob = response(text)
    with pytest.raises(DrawingError) as exc:
        decode_plan_response(blob, 'req_wire', max_output_tokens=20000, claims_wire=True)
    assert exc.value.category == 'planner_invalid_output'
    assert exc.value._response_body == blob
    assert exc.value.request_id == 'req_wire'


def test_current_wire_distinguishes_literal_occurrences_and_aliases_only_the_proven_span():
    from patent_drawing.claims_relations import RELATION_CONTRACT

    body = '1. 一种装置，包括第一端连接端口，第二端连接端口。'
    raw = proposal()
    raw.update(relations=[], flow_edges=[], relationship_coverage=[])
    raw['citations'] = [
        {'c':1,'q':'第一端连接端口','x':None},
        {'c':1,'q':'端口','x':'第一端连接端口'},
        {'c':1,'q':'端口','x':'第二端连接端口'},
    ]
    raw['views'][0]['features'] = [1,2,3,2]
    plan = claims_plan(body, raw, reviewed=True, relation_contract=RELATION_CONTRACT)
    view, = plan['views']
    long, first, second = view['features']
    alias, = view['citation_display']['aliases']
    assert alias == {'feature_id':first['id'],'anchor_id':long['id'],
                     'start':first['start'] - long['start'],'end':first['end'] - long['start']}
    assert view['citation_display']['anchor_ids'] == [long['id'],second['id']]
    assert view['citation_display']['logical_count'] == 3
    assert long['quote'].encode()[alias['start']:alias['end']].decode() == first['quote']
    assert not first['location_ambiguous'] and not second['location_ambiguous']


def test_current_wire_holds_only_the_oversized_views_and_keeps_original_placements():
    from patent_drawing.claims_relations import RELATION_CONTRACT

    many = [f'测量样本{i:03d}' for i in range(97)]
    raw = proposal()
    raw['relationship_coverage'] = [{'t':'relationship_direction','v':3,'e':[4]}]
    raw['citations'].extend({'c':2,'q':quote,'x':None} for quote in many)
    raw['views'] = [dict(raw['views'][0], title=f'视图{i}', features=[3]) for i in range(1,6)]
    raw['views'][2]['features'] = list(range(4,101))
    raw['views'][3]['features'] = [3,4]
    raw['relations'][0]['v'] = [3,4]
    raw['relations'].append({**raw['relations'][0], 'v':[3]})
    raw['figure_links'].append({'parent':3,'child':4,'anchor':'第一端','claim':1,'quote':ACTION})
    saved = deepcopy(raw)
    body = BODY + '\n2. 根据权利要求1所述的装置，' + '、'.join(many) + '。'
    output = claims_plan(body, raw, reviewed=True, relation_contract=RELATION_CONTRACT, response_sha256='a'*64)
    assert output['status'] == 'partial'
    assert [v['id'] for v in output['views']] == ['fig1','fig2','fig4','fig5']
    mixed, blocked = output['relation_candidates']
    assert mixed['view_ids'] == ['fig4'] and mixed['pending_view_ids'] == ['fig3']
    assert blocked['status'] == 'rejected' and blocked['pending_view_ids'] == ['fig3']
    assert output['relations'][0]['view_ids'] == output['flow_edges'][0]['view_ids'] == ['fig4']
    assert output['coverage'][1]['disposition'] == 'partial'
    assert not output['figure_links']
    held, = output['view_limit_issues']
    assert held['logical_count'] == 97 and held['limit'] == 96
    assert held['response_sha256'] == 'a'*64 and held['wire_citation_indices'] == list(range(4,101))
    assert held['figure_links'] == saved['figure_links']
    assert raw == saved

    raw['views'][2]['features'][-1] = 4
    restored = claims_plan(body, raw, reviewed=True, relation_contract=RELATION_CONTRACT)
    assert [v['id'] for v in restored['views']] == ['fig1','fig2','fig3','fig4','fig5']
    assert len(restored['views'][2]['features']) == 96
    assert not restored.get('view_limit_issues')


@pytest.mark.parametrize('unknown_pointer', [False, True])
def test_oversized_view_can_hold_an_additional_unverified_reference(unknown_pointer):
    from patent_drawing.claims_relations import RELATION_CONTRACT

    labels = [f'样本{i:03}' for i in range(97)]
    body = BODY + '；' + '、'.join(labels)
    raw = proposal()
    raw.update(relationship_coverage=[], relations=[], flow_edges=[])
    raw['citations'].extend({'c':1,'q':label,'x':None} for label in labels)
    raw['citations'].append({'c':1,'q':'原文没有这一句','x':None})
    raw['views'].append({**raw['views'][0], 'title':'样本记录',
                         'features':list(range(4,101)) + [999 if unknown_pointer else 101]})
    plan = claims_plan(body,raw,reviewed=True,relation_contract=RELATION_CONTRACT)
    assert [v['id'] for v in plan['views']] == ['fig1']
    held, = plan['view_limit_issues']
    assert held['logical_count']==97 and held['rejected_features']
    assert held['rejected_features'][0]['reason']==(
        'citation_index_out_of_range' if unknown_pointer else 'citation_not_found')


def test_ambiguous_occurrence_does_not_consume_a_verified_citation_slot():
    from patent_drawing.claims_relations import RELATION_CONTRACT

    labels = [f'样本{i:03}' for i in range(96)]
    body = BODY + '；' + '、'.join(labels) + '；第一端用于通信。'
    raw = proposal()
    raw.update(relationship_coverage=[], relations=[], flow_edges=[])
    raw['citations'].extend({'c':1,'q':label,'x':None} for label in labels)
    raw['views'][0]['features'] = [*range(4,100),1]
    plan = claims_plan(body,raw,reviewed=True,relation_contract=RELATION_CONTRACT)
    view, = plan['views']
    assert not plan.get('view_limit_issues')
    assert len(view['features']) == 97
    assert sum(f['location_ambiguous'] for f in view['features']) == 1
    assert view['citation_display']['logical_count'] == 96
    assert view['citation_display']['pending_count'] == 1

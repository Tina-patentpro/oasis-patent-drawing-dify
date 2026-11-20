from copy import deepcopy

import pytest

from patent_drawing.claims_input import claims_plan
from tests.test_claims_relations import ref


def proposal(body, relations, *, second_claim=False):
    return {
        'views': [{'type': 'flow', 'title': '关系与去向', 'purpose': '结构化表达已知关系',
                   'features': [{**row['evidence']['relation'], 'text': row['evidence']['relation']['quote']}
                                for row in relations]}],
        'coverage': [{'claim': 1, 'figures': [1], 'reason': '同图表达'}] + (
            [{'claim': 2, 'figures': [], 'reason': '仅约定状态名称'}] if second_claim else []),
        'unresolved': [],
        'relations': relations,
    }


def literal(quote, *, context=None):
    return {'op': 'literal', 'source': ref(quote, **({'context': context} if context else {}))}


def condition(expression):
    return {'state': 'stated', 'expression': expression}


def relation(subject, obj, quote, *, guard=None, kind='原文关系', direction='directed'):
    return {
        'claim': 1, 'views': [1], 'required': True, 'claim_path': [1],
        'subject': subject, 'object': obj, 'kind': kind, 'direction': direction,
        'evidence': {'subject': ref(subject, context=quote), 'object': ref(obj, context=quote),
                     'relation': ref(quote)},
        'condition': condition(guard) if guard else {'state': 'none_in_source', 'operator': 'none', 'terms': []},
    }


def plan(body, raw):
    return claims_plan(body, raw, reviewed=True, relation_contract=4, response_sha256='c'*64)


@pytest.mark.parametrize('guard', ['不将接合部分挤出', '无独立保护事件', '参考或状态不可用', '状态为未知'])
def test_literal_conditions_keep_exact_original_predicate(guard):
    phrase = f'在{guard}时，控制端触发处理器'
    body = '1. 一种控制方法，' + phrase + '。'
    output = plan(body, proposal(body, [relation('控制端', '处理器', phrase, guard=literal(guard))]))
    item, = output['relations']
    assert item['condition']['expression']['source']['quote'] == guard
    assert item['condition']['expression']['op'] == 'literal'
    term, = item['condition']['terms']
    assert body.encode()[term['start']:term['end']].decode() == guard


def test_explicit_scope_cannot_turn_source_or_into_and():
    phrase = '参考缺失或质量不合格时，控制端触发处理器'
    body = '1. 一种控制方法，' + phrase + '。'
    expr = {'op':'all', 'source':ref('参考缺失或质量不合格'),
            'items':[literal('参考缺失'), literal('质量不合格')]}
    output = plan(body, proposal(body, [relation('控制端', '处理器', phrase, guard=expr)]))
    assert not output['relations']
    assert 'condition_operator_mismatch' in {reason['code'] for reason in output['relation_candidates'][0]['reasons']}


def test_no_synthetic_negation_of_an_original_literal():
    phrase = '保护事件发生时，控制端终止处理器'
    body = '1. 一种控制方法，' + phrase + '。'
    expr = {'op':'not', 'source':ref('保护事件发生'), 'items':[literal('保护事件发生')]}
    output = plan(body, proposal(body, [relation('控制端', '处理器', phrase, guard=expr)]))
    assert output['relations'] == []
    assert any(x['code']=='condition_polarity_mismatch' for x in output['relation_candidates'][0]['reasons'])


def test_distinct_owners_have_stable_independent_queue_instances():
    phrase = '在终端切换账号时，将不同账号的待同步队列相互隔离'
    body = '1. 一种同步方法，' + phrase + '。'
    row = relation('待同步队列','待同步队列',phrase,guard=literal('在终端切换账号时'),direction='symmetric')
    first = plan(body, proposal(body,[row]))
    again = plan(body, proposal(body,[deepcopy(row)]))
    item, = first['relations']
    assert item['subject_id'] != item['object_id']
    assert [item['subject_id'], item['object_id']] == [again['relations'][0][k] for k in ('subject_id','object_id')]
    bindings = [x for x in first['instances'] if x.get('owner_id')]
    assert {x['id'] for x in bindings} == {item['subject_id'],item['object_id']}
    assert len({x['owner_id'] for x in bindings}) == 2


def test_a_true_self_loop_stays_shared():
    text = '控制端对控制端执行重试'
    body = '1. 一种处理方法，' + text + '。'
    row = relation('控制端','控制端',text)
    row['evidence']['subject'] = ref('控制端',context='控制端对')
    row['evidence']['object'] = ref('控制端',context='对控制端执行')
    result, = plan(body,proposal(body,[row]))['relations']
    assert result['subject_id']==result['object_id']


def state_machine():
    data = [('输入全部有效', '受限补偿', 'TRACK'),
            ('窗口不足', '单位增益', 'BYPASS'),
            ('发生保护事件', '无候选指令', 'NO_COMMAND')]
    phrases = [f'当{state}时，控制器输出{output}' for state,output,_ in data]
    body = '1. 一种控制方法，' + '；'.join(phrases) + '。\n2. 权利要求1的方法，输出包状态为TRACK或BYPASS或NO_COMMAND。'
    relations = [relation('控制器', output, phrase, guard=literal(state))
                 for (state,output,_),phrase in zip(data,phrases)]
    raw = proposal(body, relations, second_claim=True)
    raw['flow_edges'] = [{'relation':i, 'role':'branch','label':ref(mode,2)}
                         for i,(_,_,mode) in enumerate(data,1)]
    return body,raw


def test_three_modes_are_edges_or_the_specific_missing_exit_is_reported():
    body,raw = state_machine()
    result=plan(body,raw)
    assert [x['label']['quote'] for x in result['flow_edges']] == ['TRACK','BYPASS','NO_COMMAND']
    assert all(x['source_id'] and x['target_id'] and x['condition']['terms'] for x in result['flow_edges'])
    raw['flow_edges'].pop()
    missing=plan(body,raw)
    assert missing['status']=='partial'
    assert any('NO_COMMAND' in x['detail'] for x in missing['limitations'])


@pytest.mark.parametrize('sender,recipient,wanted', [
    ('接收侧','发射侧',True), ('发射侧','接收侧',False)
])
def test_feedback_must_use_its_independent_return_direction(sender,recipient,wanted):
    phrase = '发射侧向接收侧发送参考，接收侧经b0向发射侧返回反馈'
    body = '1. 一种测量方法，' + phrase + '。'
    row=relation(sender,recipient,phrase,kind='返回反馈')
    row['evidence']['subject']=ref(sender,context='接收侧经b0' if sender=='接收侧' else '发射侧向')
    row['evidence']['object']=ref(recipient,context='向发射侧返回' if recipient=='发射侧' else '向接收侧发送')
    raw=proposal(body,[row])
    raw['flow_edges']=[{'relation':1,'role':'message','label':ref('反馈')}]
    assert bool(plan(body,raw)['flow_edges']) is wanted


def test_unreachable_channel_cannot_open_wide_search():
    phrase = '通路不可达时，控制端终止处理；通路可达时，控制端进入宽搜'
    body = '1. 一种测量方法，' + phrase + '。'
    edge = relation('控制端','宽搜','通路可达时，控制端进入宽搜',guard=literal('通路不可达'),kind='进入宽搜')
    raw=proposal(body,[edge])
    raw['flow_edges']=[{'relation':1,'role':'branch','label':ref('宽搜')}]
    result=plan(body,raw)
    assert result['flow_edges']==[]
    assert any('宽搜' in x['detail'] for x in result['limitations'])


def test_expression_cannot_omit_alternative_at_end_of_cited_source():
    phrase = '门关闭且模式合格或急停发生时，控制端触发处理器'
    body = '1. 一种控制方法，' + phrase + '。'
    expr = {'op':'all', 'source':ref('门关闭且模式合格或急停发生'),
            'items':[literal('门关闭'),literal('模式合格')]}
    output=plan(body,proposal(body,[relation('控制端','处理器',phrase,guard=expr)]))
    assert not output['relations']


def test_effect_cannot_take_opposite_guard_from_another_clause():
    phrase = '无保护事件时，控制端触发处理器'
    body = '1. 一种控制方法，' + phrase + '；发生保护事件时，控制端停止处理器。'
    output=plan(body,proposal(body,[relation('控制端','处理器',phrase,guard=literal('发生保护事件'))]))
    assert not output['relations']
    assert any(x['code']=='condition_scope_mismatch' for x in output['relation_candidates'][0]['reasons'])


def test_swapped_mode_labels_do_not_validate_via_a_broad_quote():
    body,raw=state_machine()
    clause=body.split('。')[0].removeprefix('1. 一种控制方法，')
    raw['views'][0]['features']=[{**ref(clause),'text':clause}]
    for row in raw['relations']:
        row['evidence']['relation']=ref(clause)
    left,right=raw['flow_edges'][:2]
    left['label'],right['label']=right['label'],left['label']
    output=plan(body,raw)
    assert {x['label']['quote'] for x in output['flow_edges']}=={'NO_COMMAND'}


def test_feedback_word_does_not_invert_explicit_source_direction():
    phrase = '发射侧向接收侧发送参考和反馈'
    body = '1. 一种测量方法，' + phrase + '。'
    row=relation('接收侧','发射侧',phrase,kind='返回反馈')
    raw=proposal(body,[row])
    raw['flow_edges']=[{'relation':1,'role':'message','label':ref('反馈')}]
    assert plan(body,raw)['flow_edges']==[]


@pytest.mark.parametrize('quote,guard,role,expected', [
    ('b0通路不可达时，控制端退出宽搜','b0通路不可达','exit',True),
    ('预算耗尽后，控制端不再进入宽搜并保持失败','预算耗尽','branch',False),
])
def test_wide_search_exit_and_budgets_follow_original_verb(quote,guard,role,expected):
    body='1. 一种测量方法，'+quote+'。'
    row=relation('控制端','宽搜',quote,guard=literal(guard),kind=role)
    raw=proposal(body,[row])
    raw['flow_edges']=[{'relation':1,'role':role,'label':ref('宽搜')}]
    assert bool(plan(body,raw)['flow_edges']) is expected


def test_independent_account_systems_do_not_share_symbolic_instances():
    clauses=['甲系统将不同账号的待同步队列相互隔离','乙系统将不同账号的待同步队列相互隔离']
    body='1. 一种方法，甲系统和乙系统使用互不相通的账号体系；'+'；'.join(clauses)+'。'
    rows=[relation('待同步队列','待同步队列',text,kind='隔离',direction='symmetric') for text in clauses]
    result=plan(body,proposal(body,rows))
    first,second=result['relations']
    assert not {first['subject_id'],first['object_id']} & {second['subject_id'],second['object_id']}
    assert len(result['instances'])==8


def plan_v5(body, raw):
    return claims_plan(body, raw, reviewed=True, relation_contract=5, response_sha256='c'*64)


def test_separate_region_guards_inherit_only_the_demonstrated_shared_scope():
    shared = '在已辨别两处需求差异的条件下'
    local = '在需要供料的区域'
    other = '在不宜供料的区域'
    text = f'{shared}，沿轨道划分区域，{local}保持开口，{other}移除开口'
    body = f'1. 一种处理方法，包括：{text}。'
    child_terms = [literal(shared), literal(local)]
    guard = {'op': 'all', 'source': ref(text.split('，'+other)[0]), 'items': child_terms}
    row = relation('需要供料的区域', '开口', text, guard=guard)
    row['evidence']['object'] = ref('开口', context='保持开口')
    item, = plan_v5(body, proposal(body, [row]))['relations']
    assert [term['quote'] for term in item['condition']['terms']] == [shared, local]
    guard['items'][1] = literal(other)
    guard['source'] = ref(text)
    assert plan_v5(body, proposal(body, [row]))['relations'] == []
    row['condition'] = condition(literal(local))
    assert plan_v5(body, proposal(body, [row]))['relations'] == []


def test_common_claim_preamble_can_supply_two_steps_with_no_local_condition():
    preamble = '在授权输入和已校准模型可用的条件下'
    clause = '将两路表示连接为z并由z生成候选句'
    body = f'1. 一种处理方法，其特征在于，{preamble}：{clause}。'
    join = relation('两路表示', 'z', clause)
    join['evidence']['object'] = ref('z', context='连接为z')
    send = relation('z', '候选句', clause)
    send['evidence']['subject'] = ref('z', context='由z生成')
    output = plan_v5(body, proposal(body, [join, send]))
    assert len(output['relations']) == 2
    for item, candidate in zip(output['relations'], output['relation_candidates']):
        assert candidate['raw']['condition']['state'] == 'none_in_source'
        assert item['condition']['terms'][0]['quote'] == preamble
        assert item['condition']['terms'][0]['scope'] == 'preamble'
    body = body.replace(preamble+'：', preamble+'，执行初始化；')
    assert all(not item['condition']['terms'] for item in plan_v5(body, proposal(body, [join, send]))['relations'])


def test_v5_account_instance_scope_includes_independent_claim_root():
    phrase = '终端切换账号时，同步系统将各账号的待同步队列隔离'
    first = '1. 一种处理方法，包括：' + phrase + '。'
    second = '2. 一种处理系统，包括：' + phrase + '。'
    body = first + '\n' + second
    row = relation('待同步队列', '待同步队列', phrase, guard=literal('终端切换账号时'),
                   direction='symmetric')
    other = deepcopy(row)
    other.update(claim=2, claim_path=[2])
    for span in other['evidence'].values():
        span['claim'] = 2
    other['condition']['expression']['source']['claim'] = 2
    raw = proposal(body, [row, other])
    raw['coverage'].append({'claim': 2, 'figures': [1], 'reason': '独立系统范围'})
    result = plan_v5(body, raw)
    assert len(result['relations']) == 2
    subjects = {r['subject_id'] for r in result['relations']}
    assert len(subjects) == 2
    assert len({item['owner_id'] for item in result['instances'] if 'owner_id' in item}) == 4


def test_v5_implicit_feedback_binds_receiver_role_and_separate_transport():
    start = '一种发射侧参考测量协调方法，在发射侧与接收侧具有可达控制通路b0的条件下：'
    quote = '通过b0协调有限的参考配对并按配对发送参考，接收经b0返回的与episode、q、m对应的测量指标及有效性反馈'
    body = f'1. {start}{quote}。'
    send = relation('发射侧', '接收侧', quote, kind='发送参考')
    back = relation('接收侧', '发射侧', quote, kind='返回反馈')
    for row in (send, back):
        row['evidence']['subject'] = ref(row['subject'], context='发射侧与接收侧')
        row['evidence']['object'] = ref(row['object'], context='发射侧与接收侧')
    raw = proposal(body, [send, back])
    raw['flow_edges'] = [
        {'relation': 1, 'role': 'message', 'label': ref('按配对发送参考')},
        {'relation': 2, 'role': 'message', 'label': ref('接收经b0返回的与episode、q、m对应的测量指标及有效性反馈')},
    ]
    first, second = plan_v5(body, raw)['flow_edges']
    assert [first['message_kind'], second['message_kind']] == ['reference', 'feedback']
    assert first['source_id'] == second['target_id'] and first['target_id'] == second['source_id']
    assert first.get('transport') is None
    assert second['transport']['quote'] == 'b0'
    raw['flow_edges'][1]['relation'] = 1
    assert len(plan_v5(body, raw)['flow_edges']) == 1


def test_v5_derives_upstream_guard_only_from_an_adjacent_consumed_variable():
    gate = '仅在采样窗口有效时'
    formula = '按y=f(a)计算目标量'
    update = '以y更新输出量并送往控制链'
    body = f'1. 一种处理方法，包括：{gate}，{formula}；{update}。'
    row = relation('y', '控制链', update)
    result, = plan_v5(body, proposal(body, [row]))['relations']
    assert result['condition']['terms'][0]['quote'] == gate
    assert result['condition']['terms'][0]['scope'] == 'upstream'
    assert all(not item['condition']['terms'] for item in plan_v5(
        body.replace('y=f(a)', 'p=f(a)'), proposal(body, [row]))['relations'])


def test_v5_repeated_endpoint_resolves_only_inside_unique_relation_source():
    first = '完成复核时更新候选标志'
    active = '保护发生时，清除候选标志并停止输出'
    body = f'1. 一种处理方法，包括：{first}；{active}。'
    row = relation('候选标志', '停止输出', active, guard=literal('保护发生时'))
    row['evidence']['subject'] = ref('候选标志')
    result = plan_v5(body, proposal(body, [row]))
    item, = result['relations']
    assert item['evidence']['subject']['context_start'] == item['evidence']['relation']['start']
    assert result['relation_candidates'][0]['raw']['evidence']['subject'] == ref('候选标志')
    row['evidence']['subject'] = ref('候选标志', context=first)
    assert not plan_v5(body, proposal(body, [row]))['relations']


def test_v5_labeled_no_command_can_be_an_exit():
    body, raw = state_machine()
    raw['flow_edges'][-1]['role'] = 'exit'
    output = plan_v5(body, raw)
    assert [edge['role'] for edge in output['flow_edges']] == ['branch', 'branch', 'exit']
    assert not any('NO_COMMAND' in issue['detail'] for issue in output['limitations'])


def test_v5_mode_labels_bind_guard_edges_along_the_declared_claim_path():
    body, raw = state_machine()
    for edge, guard in zip(raw['flow_edges'], ('输入全部有效', '窗口不足', '发生保护事件')):
        edge['label'] = ref(guard)
    raw['flow_edges'][-1]['role'] = 'exit'
    output = plan_v5(body, raw)
    assert [edge['mode_binding']['quote'] for edge in output['flow_edges']] == ['TRACK', 'BYPASS', 'NO_COMMAND']
    assert all(edge['mode_binding']['claim'] == 2 for edge in output['flow_edges'])
    assert not any('缺少有条件的输出边' in issue['detail'] for issue in output['limitations'])


@pytest.mark.parametrize('phrase', ['终端切换账号时，隔离各账号的待同步队列',
                                     '终端切换账号时，同步系统将各账号的待同步队列隔离'])
def test_v5_missing_independent_ownership_is_reported_even_with_text_coverage(phrase):
    body = '1. 一种同步方法，包括：' + phrase + '。\n2. 一种同步系统，包括：' + phrase + '。'
    row = relation('待同步队列', '待同步队列', phrase, guard=literal('终端切换账号时'),
                   direction='symmetric')
    raw = proposal(body, [row])
    raw['views'][0]['features'].append({'claim': 2, 'quote': phrase, 'text': phrase})
    raw['coverage'].append({'claim': 2, 'figures': [1], 'reason': '系统同图表达'})
    result = plan_v5(body, raw)
    assert result['coverage'][1]['disposition'] == 'expressed'
    issue, = [row for row in result['limitations'] if row.get('code') == 'owner_scope_missing']
    assert issue['claims'] == [2] and issue['view_ids'] == ['fig1']
    assert result['status'] == 'partial'


def test_v5_later_sibling_guard_and_effect_cannot_certify_an_earlier_branch():
    text = '在温度正常时，控制器输出受限补偿，在发生保护事件时，控制器输出无候选指令'
    body = '1. 一种处理方法，包括：'+text+'。\n2. 权利要求1的方法，输出包状态为TRACK或BYPASS或NO_COMMAND。'
    row = relation('控制器', '受限补偿', text, guard=literal('在发生保护事件时'))
    row['evidence']['subject'] = ref('控制器', context='控制器输出受限补偿')
    raw = proposal(body, [row], second_claim=True)
    raw['flow_edges'] = [{'relation':1, 'role':'branch', 'label':ref('TRACK',2)}]
    assert not plan_v5(body, raw)['relations']
    row['condition'] = condition(literal('在温度正常时'))
    raw['flow_edges'][0]['label'] = ref('NO_COMMAND',2)
    output = plan_v5(body, raw)
    assert len(output['relations']) == 1
    assert not output['flow_edges']


@pytest.mark.parametrize('statement', ['接收侧按配对向发射侧发送参考', '不得按配对发送参考'])
def test_v5_implicit_role_never_overrides_an_explicit_or_forbidden_message(statement):
    header = '一种发射侧参考测量协调方法，在发射侧与接收侧具有可达控制通路b0的条件下：'
    quote = statement+'，接收经b0返回的有效性反馈'
    body = '1. '+header+quote+'。'
    row = relation('发射侧','接收侧',quote,kind='发送参考')
    for role in ('subject', 'object'):
        row['evidence'][role] = ref(row[role], context='发射侧与接收侧')
    raw = proposal(body,[row])
    raw['flow_edges'] = [{'relation':1,'role':'message','label':ref(statement)}]
    assert not plan_v5(body, raw)['flow_edges']


def test_v5_claim_preamble_applies_to_later_steps_but_not_an_explicit_unavailable_exit():
    gate = '在授权输入和已校准模型可用的条件下'
    generate = '由z生成候选句'
    exit_clause = '模型或校准不可用时停用增强并保留原模型'
    body = f'1. 一种处理方法，其特征在于，{gate}：将两路表示连接为z；{generate}；{exit_clause}。'
    row = relation('z','候选句',generate)
    unavailable = relation('模型','原模型',exit_clause,guard=literal('模型或校准不可用时'))
    unavailable['evidence']['subject'] = ref('模型',context='模型或校准')
    first, second = plan_v5(body, proposal(body, [row, unavailable]))['relations']
    assert first['condition']['terms'][0]['quote'] == gate
    assert [term['quote'] for term in second['condition']['terms']] == ['模型或校准不可用时']


def test_v5_compound_local_guard_cannot_lose_the_protection_constraint():
    text = '仅在基础指令有效且无独立保护时，控制器输出受限补偿'
    body = '1. 一种处理方法，包括：'+text+'。\n2. 权利要求1的方法，输出包状态为TRACK或BYPASS或NO_COMMAND。'
    row = relation('控制器','受限补偿',text,guard=literal('基础指令有效'))
    raw = proposal(body,[row],second_claim=True)
    raw['flow_edges'] = [{'relation':1,'role':'branch','label':ref('TRACK',2)}]
    assert not plan_v5(body, raw)['flow_edges']
    row['condition'] = condition({'op':'all','source':ref('基础指令有效且无独立保护'),
                                  'items':[literal('基础指令有效'), literal('无独立保护')]})
    assert len(plan_v5(body, raw)['flow_edges']) == 1


@pytest.mark.parametrize('single', ['基础指令失效', '任务时间不可用', '参数无效', '发生独立保护事件'])
def test_v5_exit_effect_keeps_all_source_alternatives_after_an_intermediate_action(single):
    guard = '基础指令失效、任务时间不可用、参数无效或者发生独立保护事件时'
    text = f'{guard}，清除窗口和上限状态，使本周期的候选指令无效且不输出新候选'
    body = '1. 一种控制方法，包括：'+text+'。'
    row = relation('候选指令','新候选',text,guard=literal(guard))
    assert len(plan_v5(body, proposal(body,[row]))['relations']) == 1
    row['condition'] = condition(literal(single))
    assert not plan_v5(body, proposal(body,[row]))['relations']


def plan_v7(body, raw):
    return claims_plan(body, raw, reviewed=True, relation_contract=7, response_sha256='c'*64)


@pytest.mark.parametrize(('head','op','items','separator'), [
    ('在具有', 'all', ['授权输入', '独立索引', '校准模型'], '及'),
    ('发现', 'any', ['薄片', '持续脱落', '碎屑'], '或'),
])
def test_v7_source_wrapped_enumeration_keeps_every_item(head, op, items, separator):
    gate = head + '、'.join(items[:-1]) + separator + items[-1] + ('的条件下' if op == 'all' else '时')
    effect = '控制器执行检查'
    body = f'1. 一种处理方法，包括：{gate}，{effect}。'
    row = relation('控制器', '检查', effect, guard={'op': op, 'source': ref(gate),
                   'items': [literal(item) for item in items]})
    raw = proposal(body, [row])
    checked, = plan_v7(body, raw)['relations']
    assert checked['condition']['operator'] == op
    assert [item['quote'] for item in checked['condition']['terms']] == items
    row['condition']['expression']['items'].pop(1)
    assert not plan_v7(body, raw)['relations']


def test_v7_topic_scope_stays_with_matched_action_and_parallel_effect():
    topic = '对于相同账号和操作号的请求'
    guard = '在命中已保存记录时'
    one = '服务端不再应用操作'
    two = '并由服务端返回已保存结果'
    phrase = f'{topic}，{guard}{one}，{two}'
    body = f'1. 一种处理方法，包括：{phrase}。'
    expr = {'op': 'all', 'source': ref(topic + '，' + guard), 'items': [literal(topic), literal(guard)]}
    for quote, effect in [(one, '应用操作'), (two, '已保存结果')]:
        row = relation('服务端', effect, quote, guard=deepcopy(expr))
        raw = proposal(body, [row])
        item, = plan_v7(body, raw)['relations']
        assert [t['quote'] for t in item['condition']['terms']] == [topic, guard]
        row['condition']['expression']['items'][0] = literal('相同账号')
        assert not plan_v7(body, raw)['relations']


def test_v7_region_branch_retains_source_proven_common_guard():
    shared = '在已辨别两处需求差异的条件下'
    local = '在需要供料的区域'
    sibling = '在不宜供料的区域'
    body = f'1. 一种处理方法，包括：{shared}，沿轨道划分区域，{local}保持开口，{sibling}移除开口。'
    row = relation('需要供料的区域', '开口', local + '保持开口', guard=literal(local))
    raw = proposal(body, [row])
    item, = plan_v7(body, raw)['relations']
    assert {span['quote'] for span in item['condition']['terms']} == {local, shared}
    row['condition'] = condition(literal(sibling))
    assert not plan_v7(body, raw)['relations']


def test_v7_qualifying_check_precedes_a_different_later_guarded_action():
    check = '终端核对操作归属及处理阶段'
    body = f'1. 一种处理方法，包括：{check}，仅在确认表示业务结果已持久化后移除对应覆层。'
    row = relation('终端','处理阶段',check)
    raw = proposal(body,[row])
    assert len(plan_v7(body,raw)['relations']) == 1
    revised = f'1. 一种处理方法，包括：仅在本周期有效时，{check}，仅在确认表示业务结果已持久化后移除对应覆层。'
    assert not plan_v7(revised,raw)['relations']


def test_v7_feedback_retains_validity_and_concrete_return_channel():
    header = '一种发射侧测量协调方法，在发射侧与接收侧具有可达控制通路b0的条件下：'
    send = '按配对发送参考'
    feedback = '接收经b0返回的与episode、q、m对应的测量指标及有效性反馈'
    quote = f'通过b0协调有限的配对并{send}，{feedback}，仅采用同一episode内的有效配对反馈'
    body = f'1. {header}{quote}。'
    one = relation('发射侧', '接收侧', quote, kind='发送参考')
    two = relation('接收侧', '发射侧', quote, kind='返回反馈')
    for row in (one,two):
        for key in ('subject','object'):
            row['evidence'][key] = ref(row[key], context='发射侧与接收侧')
    raw = proposal(body,[one,two])
    raw['flow_edges'] = [{'relation':1,'role':'message','label':ref(send)},
                         {'relation':2,'role':'message','label':ref(feedback)}]
    first, second = plan_v7(body, raw)['flow_edges']
    assert 'transport' not in first and second['transport']['quote'] == 'b0'
    assert first['source_id'] == second['target_id'] and first['target_id'] == second['source_id']
    raw['flow_edges'][1]['label']['quote'] = feedback.removesuffix('及有效性反馈')
    assert [edge['message_kind'] for edge in plan_v7(body,raw)['flow_edges']] == ['reference']


def test_v7_hint_with_reference_scheduling_does_not_become_reference():
    header = '一种发射侧测量协调方法，在发射侧与接收侧具有可达控制通路b0的条件下：'
    quote = '经b0向接收侧发送范围提示并关联参考配对调度'
    body = f'1. {header}{quote}。'
    row = relation('发射侧','接收侧',quote,kind='提示')
    for role in ('subject','object'):
        row['evidence'][role] = ref(row[role], context='发射侧与接收侧')
    raw = proposal(body,[row])
    raw['flow_edges'] = [{'relation':1,'role':'message','label':ref(quote)}]
    edge, = plan_v7(body,raw)['flow_edges']
    assert edge['message_kind']=='hint' and edge['transport']['quote']=='b0'


def test_v7_combined_message_and_hint_cannot_satisfy_required_reference():
    header = '一种发射侧协调方法，在发射侧与接收侧具有可达控制通路b0的条件下：'
    hint = '经b0向接收侧发送范围提示并关联参考配对调度'
    reference = '按配对发送参考'
    feedback = '接收经b0返回有效性反馈'
    combined = reference+'，'+feedback
    body = f'1. {header}{hint}；{combined}，仅采用同一轮次的有效配对反馈。'
    rows=[relation('发射侧','接收侧',hint,kind='提示'),
          relation('接收侧','发射侧',combined,kind='返回反馈')]
    for row in rows:
        for role in ('subject','object'):
            row['evidence'][role]=ref(row[role],context='发射侧与接收侧')
    raw=proposal(body,rows)
    raw['flow_edges']=[{'relation':1,'role':'message','label':ref(hint)},
                       {'relation':2,'role':'message','label':ref(combined)}]
    first=plan_v7(body,raw)
    assert [r['message_kind'] for r in first['flow_edges']]==['hint']
    raw['flow_edges'][1]['label']=ref(feedback)
    second=plan_v7(body,raw)
    assert [r['message_kind'] for r in second['flow_edges']]==['hint','feedback']
    assert any(r['detail']=='参考缺少独立的有向关系。' for r in second['limitations'])
    assert second['flow_edges'][1]['feedback_acceptance']['quote']=='仅采用同一轮次的有效配对反馈'


def test_v7_coordinated_branch_with_its_own_guard_rejects_prior_trigger():
    topic = '对于同一账号的请求'
    good = '在校验通过时'
    bad = '在校验失败时'
    action = '服务端清理任务缓存'
    body = f'1. 一种处理方法，包括：{topic}，{good}服务端保存当前数据，同时{bad}{action}。'
    raw_guard = {'op':'all','source':ref(topic+'，'+good),'items':[literal(topic),literal(good)]}
    row = relation('服务端','任务缓存',action,guard=raw_guard)
    raw = proposal(body,[row])
    assert not plan_v7(body,raw)['relations']
    row['condition']=condition(literal(bad))
    assert len(plan_v7(body,raw)['relations'])==1
    body = f'1. 一种处理方法，包括：{topic}，同时仅{bad}{action}。'
    row['condition']={'state':'none_in_source','operator':'none','terms':[]}
    assert not plan_v7(body,raw)['relations']


def test_v7_topic_cannot_replace_a_local_release_condition():
    topic='对于同一终端的前后两项操作'
    local='在核对前项归属及确认阶段后'
    body=f'1. 一种处理方法，包括：{topic}，{local}终端再释放后项操作。'
    row=relation('终端','后项操作','终端再释放后项操作',guard=literal(topic))
    assert not plan_v7(body,proposal(body,[row]))['relations']


def test_v7_legacy_shape_cannot_drop_alternative_triggers():
    guard='发现薄片、持续脱落或碎屑时'
    body=f'1. 一种处理方法，包括：{guard}，控制器停止处理。'
    row=relation('控制器','处理','控制器停止处理')
    row['condition']={'state':'stated','operator':'single',
                      'terms':[{'claim':1,'quote':'薄片','polarity':'positive'}]}
    assert not plan_v7(body,proposal(body,[row]))['relations']


def test_v7_release_and_its_negative_alternative_share_only_the_same_topic():
    topic='对于同一终端的前后两项操作'
    active='在核对前项归属及确认阶段后'
    blocked='前项超时或阶段未达时'
    body=f'1. 一种处理方法，包括：{topic}，{active}再释放后项操作；{blocked}，不释放后项操作。'
    row=relation('前项','后项操作',blocked+'，不释放后项操作',guard=literal(blocked))
    row['evidence']['object']=ref('后项操作',context='不释放后项操作')
    verified,=plan_v7(body,proposal(body,[row]))['relations']
    assert {t['quote'] for t in verified['condition']['terms']}=={topic,blocked}


def test_v7_message_uses_the_action_containing_its_exact_label():
    hint = '接收侧向发射侧发送范围提示并关联参考调度'
    send = '发射侧向接收侧按配对发送参考'
    body = f'1. 一种测量协调方法，包括：{hint}，{send}。'
    row = relation('接收侧', '发射侧', hint+'，'+send)
    row['evidence']['subject'] = ref('接收侧', context=hint)
    row['evidence']['object'] = ref('发射侧', context=hint)
    raw = proposal(body, [row])
    raw['flow_edges'] = [{'role':'message','relation':1,'label':ref('按配对发送参考')}]
    plan = claims_plan(body, raw, reviewed=True, relation_contract=7)
    assert not plan['flow_edges']
    assert 'flow_direction_mismatch' in plan['flow_candidates'][0]['reasons']


def plan_v8(body, raw):
    return claims_plan(body, raw, reviewed=True, relation_contract=8, response_sha256='c'*64)


@pytest.mark.parametrize(('local','effect'), [
    ('崩溃恢复时','重放未确认项'),
    ('崩溃恢复时','，重放未确认项'),
    ('在崩溃恢复时','，终端重放未确认项'),
])
def test_v8_independent_recovery_cannot_borrow_an_explicit_sibling_topic(local, effect):
    left = '对于冲突待选的结果'
    action = local + effect
    body = f'1. 一种处理方法，包括：{left}，终端保留未确认意图，{action}。'
    row = relation('崩溃恢复', '未确认项', action, guard=literal(local))
    raw = proposal(body, [row])
    checked, = plan_v8(body, raw)['relations']
    assert [term['quote'] for term in checked['condition']['terms']] == [local]
    row['condition'] = condition(literal(left))
    assert not plan_v8(body, raw)['relations']


def test_v8_admission_never_drops_a_required_condition_or_borrows_other_scope():
    preamble = '在具有授权资料、独立标注，以及完成校准的模型的条件下'
    gate = '仅接纳归属准确、证据充分、来源允许且无冲突的候选'
    body = f'1. 一种候选处理方法，{preamble}：{gate}。'
    def guard(terms):
        return {'op':'all','source':ref(gate),'items':[literal(value) for value in terms]}
    row = relation('候选', '接纳', gate, guard=guard([preamble, '归属准确', '证据充分', '来源允许且无冲突']))
    raw = proposal(body, [row])
    checked, = plan_v8(body, raw)['relations']
    assert [term['quote'] for term in checked['condition']['applicability']] == [preamble]
    row['condition'] = condition(guard([preamble, '归属准确', '来源允许且无冲突']))
    assert not plan_v8(body, raw)['relations']


def test_v8_source_region_requires_the_branch_to_be_inside_its_original_quote():
    shared = '在已辨别两处需求差异的条件下'
    local = '在需要供料的区域'
    sibling = '在不宜供料的区域'
    body = f'1. 一种加工方法，包括：{shared}，沿行程划分，{local}保持开口，{sibling}移除开口。'
    row = relation('需要供料的区域', '保持开口', local + '保持开口', guard=literal(shared))
    raw = proposal(body, [row])
    checked, = plan_v8(body, raw)['relations']
    assert [term['quote'] for term in checked['condition']['terms']] == [local, shared]
    row['condition'] = condition(literal(sibling))
    assert not plan_v8(body, raw)['relations']
    row['condition'] = condition(literal(shared))
    row['evidence']['relation'] = ref('保持开口')
    raw['views'][0]['features'] = [{**ref('保持开口'), 'text':'保持开口'}]
    assert not plan_v8(body, raw)['relations']


def test_v8_admission_rejects_a_single_eligible_predicate():
    preamble = '在具有授权资料、独立标注，以及完成校准的模型的条件下'
    gate = '仅接纳归属准确、证据充分、来源允许且无冲突的候选'
    body = f'1. 一种候选处理方法，{preamble}：{gate}。'
    row = relation('候选', '接纳', gate, guard=literal('归属准确'))
    raw = proposal(body, [row])
    assert not plan_v8(body, raw)['relations']
    row['condition'] = {'state':'none_in_source','operator':'none','terms':[]}
    assert not plan_v8(body, raw)['relations']
    row['condition'] = condition(literal(gate))
    assert len(plan_v8(body, raw)['relations']) == 1


def test_v8_initial_access_does_not_replace_runtime_reachability():
    preamble = '在具备可达控制通路b0的条件下'
    failure = '提示过期或配对失配时'
    reachable = '仅在b0仍可达条件下'
    quote = f'{failure}，{reachable}终端请求新提示并转入有预算的基线宽搜'
    body = f'1. 一种处理方法，{preamble}：初始获取提示；{quote}，预算耗尽保持失败。'
    row = relation('终端','有预算的基线宽搜',quote,guard=literal(failure))
    raw = proposal(body, [row])
    raw['flow_edges'] = [{'relation':1,'role':'branch','label':ref('转入有预算的基线宽搜')}]
    assert not plan_v8(body, raw)['flow_edges']
    row['condition'] = condition({'op':'all','source':ref(f'{failure}，{reachable}'),
                                  'items':[literal(failure),literal(reachable)]})
    checked, = plan_v8(body, raw)['flow_edges']
    assert {item['quote'] for item in checked['condition']['terms']} == {preamble,failure,reachable}


@pytest.mark.parametrize('explicit_local', [False,True])
def test_v8_regional_subject_cannot_be_borrowed_from_sibling_action(explicit_local):
    shared = '在已辨别两处需求差异的条件下'
    local = '在需要供料的区域'
    sibling = '在不宜供料的区域'
    quote = f'{shared}，沿行程划分，{local}保持开口，{sibling}移除开口'
    body = f'1. 一种加工方法，包括：{quote}。'
    row = relation('不宜供料的区域', '保持开口', quote, guard=literal(local if explicit_local else shared))
    assert not plan_v8(body, proposal(body,[row]))['relations']


@pytest.mark.parametrize('quote', [
    '仅接纳归属准确、证据充分、来源允许且无冲突的候选',
    '归属准确、证据充分、来源允许且无冲突的候选',
    '仅接纳归属准确、证据充分、来源允许且无冲突',
    '归属准确、证据充分、来源允许且无冲突',
])
def test_v9_admission_boundaries_keep_all_predicates(quote):
    gate='仅接纳归属准确、证据充分、来源允许且无冲突的候选'
    body=f'1. 一种处理方法，包括：{gate}。'
    row=relation('候选','接纳',gate,guard=literal(quote))
    checked=claims_plan(body,proposal(body,[row]),reviewed=True,relation_contract=9)
    assert len(checked['relations'])==1
    row['condition']=condition(literal('归属准确'))
    assert not claims_plan(body,proposal(body,[row]),reviewed=True,relation_contract=9)['relations']


def test_v9_topic_predicate_keeps_exact_applicable_object():
    topic='对于前置依赖未满足的修改操作'
    action='服务端暂存该修改操作'
    body=f'1. 一种处理方法，包括：{topic}，{action}。'
    row=relation('服务端','该修改操作',topic+'，'+action,guard=literal('前置依赖未满足'))
    item,=claims_plan(body,proposal(body,[row]),reviewed=True,relation_contract=9)['relations']
    assert any(t['quote']==topic for t in item['condition']['applicability'])
    row['condition']=condition(literal('依赖未满足'))
    assert not claims_plan(body,proposal(body,[row]),reviewed=True,relation_contract=9)['relations']


@pytest.mark.parametrize('receiver_prefix', ['', '发射侧'])
def test_v9_rebinds_only_matching_roles_from_same_claim_preamble(receiver_prefix):
    header='一种发射侧参考测量协调方法，在发射侧与接收侧具有可达控制通路b0的条件下：'
    hint='提示供接收侧核验版本'
    send='按配对发送参考'
    feedback=receiver_prefix+'接收经b0返回的与episode、q、m对应的指标及有效性反馈'
    body=f'1. {header}{hint}；{send}，{feedback}，仅采用同一episode内的有效配对反馈。'
    rows=[relation('发射侧','接收侧',send,kind='参考'),
          relation('接收侧','发射侧',feedback,kind='回传指标')]
    for row in rows:
        for name in ('subject','object'):
            row['evidence'][name] = ref('发射侧参考测量协调方法' if row[name]=='发射侧' else hint)
    raw=proposal(body,rows)
    raw['flow_edges']=[{'relation':1,'role':'message','label':ref(send)},
                       {'relation':2,'role':'message','label':ref(feedback)}]
    plan=claims_plan(body,raw,reviewed=True,relation_contract=9)
    assert [edge['message_kind'] for edge in plan['flow_edges']]==['reference','feedback']
    assert all(row['evidence_rebindings']['original'] for row in plan['relation_candidates'])
    assert 'transport' not in plan['flow_edges'][0]
    assert plan['flow_edges'][1]['transport']['quote']=='b0'
    row=rows[1]
    row['subject'],row['object']=row['object'],row['subject']
    row['evidence']['subject'],row['evidence']['object']=row['evidence']['object'],row['evidence']['subject']
    checked=claims_plan(body,raw,reviewed=True,relation_contract=9)
    assert not any(edge.get('message_kind')=='feedback' for edge in checked['flow_edges'])


def test_v9_preamble_cannot_override_an_explicit_sender():
    header='一种发射侧参考测量协调方法，在发射侧与接收侧具有可达控制通路b0的条件下：'
    hint='提示供接收侧核验'
    send='接收侧按配对发送参考'
    body=f'1. {header}{hint}；{send}。'
    row=relation('发射侧','接收侧',send,kind='参考')
    row['evidence']['subject']=ref('发射侧参考测量协调方法')
    row['evidence']['object']=ref(hint)
    raw=proposal(body,[row]);raw['flow_edges']=[{'relation':1,'role':'message','label':ref(send)}]
    result=claims_plan(body,raw,reviewed=True,relation_contract=9)
    assert not result['relations'] and not result['flow_edges']


@pytest.mark.parametrize('form, accepted', [
    ('nominal', True), ('bare', True), ('literal', True),
    ('omitted', False), ('all', False), ('foreign', False),
])
def test_v10_pending_alternatives_preserve_the_complete_source(form, accepted):
    predicates = ['未知', '低置信', '归属歧义']
    bare = '未知、低置信或归属歧义'
    subject = bare + '候选'
    quote = f'将{subject}保留待查'
    body = f'1. 一种过滤方法，包括：{quote}。'
    expression = {'op': 'any', 'source': ref(subject), 'items': [literal(x) for x in predicates]}
    if form == 'bare':
        expression['source'] = ref(bare)
    elif form == 'literal':
        expression = literal(subject)
    elif form == 'omitted':
        expression['items'].pop()
    elif form == 'all':
        expression['op'] = 'all'
    elif form == 'foreign':
        expression['items'][0]['source']['claim'] = 2
    row = relation(subject, '保留待查', quote, guard=expression)
    raw = proposal(body, [row])
    raw['flow_edges'] = [{'role': 'branch', 'relation': 1, 'label': ref('保留待查')}]
    checked = claims_plan(body, raw, reviewed=True, relation_contract=10)
    assert bool(checked['relations']) == bool(checked['flow_edges']) == accepted


@pytest.mark.parametrize('form, accepted', [
    ('role_context', True), ('unique', True), ('invalid_quote', False),
    ('repeated', False), ('reversed', False), ('stated_reverse', False), ('earlier_branch', False),
    ('negated_action', False),
])
def test_v10_action_context_reanchor_requires_one_compatible_action(form, accepted):
    guard = '在发射侧与接收侧具有可达控制通路b0的条件下'
    header = f'一种发射侧参考测量方法，{guard}：'
    action = '按配对发送参考'
    statement = '接收侧' + action if form == 'stated_reverse' else action
    if form == 'earlier_branch':
        statement = '在禁用状态下进入空闲；' + action
    if form == 'negated_action':
        statement = '不得' + action
    if form == 'repeated':
        statement += '；' + action
    body = f'1. {header}{statement}。'
    source, target = ('接收侧', '发射侧') if form == 'reversed' else ('发射侧', '接收侧')
    row = relation(source, target, action, kind='参考发送', guard=literal(guard))
    raw = proposal(body, [row])
    for role in ('subject', 'object'):
        row['evidence'][role] = ref(row[role], context=guard)
    if form != 'unique':
        row['evidence']['relation']['context'] = guard
    if form == 'earlier_branch':
        row['evidence']['relation']['context'] += '：在禁用状态下进入空闲'
    if form == 'invalid_quote':
        row['evidence']['relation']['quote'] = '按配对发送参考信号'
    raw['flow_edges'] = [{'role': 'message', 'relation': 1, 'label': ref(action)}]
    checked = claims_plan(body, raw, reviewed=True, relation_contract=10)
    assert bool(checked['relations']) == bool(checked['flow_edges']) == accepted
    if form == 'role_context':
        saved = checked['relation_candidates'][0]
        assert saved['raw']['evidence']['relation']['context'] == guard
        assert saved['action_citation_rebinding']['corrected']['quote'] == action


@pytest.mark.parametrize('form, accepted', [
    ('split', True), ('complete', True), ('missing_gate', False),
    ('wrong_member', False), ('wrong_scope', False), ('negated_list', False),
])
def test_v10_split_tree_reconstructs_only_the_source_guard(form, accepted):
    predicates = ['状态过期', '数据为空', '验收失败']
    alternative = '状态过期、数据为空或验收失败时'
    if form == 'negated_list':
        alternative = '不在' + alternative
    gate = '仅在b0仍可达条件下'
    whole = alternative + '，' + gate
    effect = '请求新提示并转入有限预算的基线宽搜'
    quote = whole + effect
    body = f'1. 一种协调方法，包括：{quote}。'
    member = predicates[1] if form == 'negated_list' else predicates[0]
    first = literal(member)
    if form == 'complete':
        first = {'op': 'any', 'source': ref(alternative), 'items': [literal(x) for x in predicates]}
    elif form == 'wrong_member':
        first = literal(predicates[1])
    expression = {'op': 'all', 'source': ref(whole), 'items': [first, literal(gate)]}
    if form == 'missing_gate':
        expression['items'].pop()
    elif form == 'wrong_scope':
        expression['items'][1]['source']['claim'] = 2
    row = relation(member, effect, quote, guard=expression)
    raw = proposal(body, [row])
    raw['flow_edges'] = [{'role': 'branch', 'relation': 1, 'label': ref(effect)}]
    checked = claims_plan(body, raw, reviewed=True, relation_contract=10)
    assert bool(checked['relations']) == bool(checked['flow_edges']) == accepted
    if accepted:
        assert [t['quote'] for t in checked['flow_edges'][0]['condition']['terms']] == [*predicates, gate]


def test_v10_legacy_candidate_with_missing_citation_cannot_abort_plan():
    body = '1. 一种处理方法，控制器保存结果。'
    row = relation('控制器', '结果', '控制器保存结果')
    raw = proposal(body, [row])
    raw['relations'].append({'claim': 1, 'views': [1], 'condition': '', 'quote': '错误引文'})
    checked = claims_plan(body, raw, reviewed=True, relation_contract=10)
    assert len(checked['relations']) == 1
    assert {r['code'] for r in checked['relation_candidates'][1]['reasons']} >= {
        'citation_not_found', 'legacy_evidence_incomplete'}
@pytest.mark.parametrize('valid', [True, False])
def test_shared_enumeration_applies_to_two_actions_only_with_exact_source(valid):
    predicates = ['提示过期', '姿态改版', '空K', '配对失配', '参考未收到', '测量质量不合格']
    triggers = '、'.join(predicates[:-1]) + '或' + predicates[-1]
    root = f'{triggers}时，仅在b0仍可达条件下请求新提示并转入有限预算的基线宽搜'
    body = f'1. 一种协调方法，包括：{root}；预算耗尽保持失败。'
    guard = {'op': 'all', 'source': ref(root),
             'items': [literal(triggers), literal('b0仍可达' if valid else '预算耗尽')]}
    rows = [relation(triggers, target, root, guard=guard, kind=kind)
            for kind, target in [('请求', '新提示'), ('转入', '有限预算的基线宽搜')]]
    for row in rows:
        row['evidence']['object'] = ref(root)
    raw = proposal(body, rows)
    raw['flow_edges'] = [{'role': role, 'relation': i, 'label': ref(root)}
                         for i, role in [(1, 'message'), (2, 'branch')]]
    from patent_drawing.claims_input import _reviewed_plan, read_claims_text

    actual = _reviewed_plan(*read_claims_text(body), raw, 23, 'd'*64)
    if not valid:
        assert not actual['relations']
        assert not actual['flow_edges']
        return
    assert [edge['label']['quote'] for edge in actual['flow_edges']] == [
        '请求新提示', '转入有限预算的基线宽搜']
    for edge in actual['flow_edges']:
        assert edge['role'] == 'branch'
        expr = edge['condition']['expression']
        assert expr['op'] == 'all'
        assert [node['op'] for node in expr['items']] == ['any', 'literal']
        assert [term['quote'] for term in edge['condition']['terms']] == [
            *predicates, '仅在b0仍可达条件下']
    assert actual['relation_candidates'][0]['raw']['condition']['expression'] == guard


def test_duplicate_flow_placement_preserves_proofs_and_spatial_view():
    from patent_drawing.claims_flow import consolidate_flow_placements

    guard = {'claim': 2, 'start': 10, 'end': 19, 'quote': '在前提满足时'}
    action = {'claim': 2, 'start': 19, 'end': 25, 'quote': '继续'}
    event = {'claim': 2, 'start': 1, 'end': 10, 'quote': '处理步骤'}
    evidence = {'subject': event, 'object': action, 'relation': action}
    condition = {'terms': [guard], 'applicability': []}
    relation = {'id': 'rel', 'evidence': evidence, 'condition': condition, 'view_ids': ['fig1', 'fig2']}
    edge = {'id': 'edge', 'relation_id': 'rel', 'role': 'branch', 'label': action,
            'view_ids': ['fig1', 'fig2'], 'condition': condition}
    spatial_evidence = {'claim': 4, 'start': 45, 'end': 50, 'quote': '分区'}
    views = [
        {'id': 'fig1', 'type': 'flow', 'features': [guard, action, event]},
        {'id': 'fig2', 'type': 'relationship', 'features': [guard, action, event, spatial_evidence]},
    ]
    saved_views = deepcopy(views)
    migrations = consolidate_flow_placements([relation], [edge], views)
    assert relation['view_ids'] == edge['view_ids'] == ['fig1']
    record, = migrations
    assert (record['relation_id'], record['from'], record['to']) == ('rel', 'fig2', 'fig1')
    assert {(item['claim'], item['start'], item['end']) for item in record['citations']} == {
        (2, 1, 10), (2, 10, 19), (2, 19, 25)}
    assert views == saved_views

    relation['view_ids'] = edge['view_ids'] = ['fig1', 'fig2']
    views[0]['features'].remove(guard)
    assert consolidate_flow_placements([relation], [edge], views) == []
    assert edge['view_ids'] == ['fig1', 'fig2']


@pytest.mark.parametrize('actor', ['接收设备', '发射设备'])
@pytest.mark.parametrize('compact', [False, True])
@pytest.mark.parametrize('short_actor_citation', [False, True])
def test_configured_actor_keeps_both_shared_guard_actions(actor, compact, short_actor_citation):
    from patent_drawing.claims_input import _reviewed_plan, read_claims_text

    declaration = '所述接收设备在与发射设备保持在线的条件下'
    triggers = '参考未收到、状态过期或数据异常时'
    gate = '仅在控制通道仍可达条件下'
    actions = '请求新提示并转入有限预算的基线宽搜'
    statement = f'{triggers}，{gate}{actions}'
    source = f'1. 一种接收设备，{declaration}，被配置为：接收数据；{statement}，预算耗尽保持失败。'
    combined_guard = f'{triggers}，{gate}'
    guard = (literal(combined_guard) if compact else
             {'op': 'all', 'source': ref(statement), 'items': [literal(triggers), literal(gate)]})
    rows = [relation(actor, target, statement, guard=guard, kind=kind) for kind, target in
            (('请求', '新提示'), ('转入', '有限预算的基线宽搜'))]
    for row in rows:
        row['evidence'].update(subject=ref('所述接收设备' if short_actor_citation else declaration),
                               object=ref(statement))
    raw = proposal(source, rows)
    raw['flow_edges'] = [{'role': 'message' if compact and index == 1 else 'branch',
                          'relation': index, 'label': ref(combined_guard if compact else statement)}
                         for index in (1, 2)]
    plan = _reviewed_plan(*read_claims_text(source), raw, 23, 'e'*64)
    if actor == '发射设备':
        assert not plan['relations'] and not plan['flow_edges']
        return
    assert [edge['label']['quote'] for edge in plan['flow_edges']] == ['请求新提示', '转入有限预算的基线宽搜']
    assert all(r['subject'] == actor and r['condition']['operator'] == 'all' for r in plan['relations'])
    assert all(r['condition']['expression']['items'][0]['op'] == 'any' for r in plan['relations'])


@pytest.mark.parametrize('local_guard', ['', '仅在备用通路可用时'])
def test_ancestor_preamble_is_applicability_without_overriding_local_guard(local_guard):
    from patent_drawing.claims_input import _reviewed_plan, read_claims_text

    preamble = '限于通道可用的条件'
    action = '控制器保存配对结果'
    body = (f'1. 一种处理方法，{preamble}：接收数据。\n'
            f'2. 根据权利要求1所述的方法，{local_guard}{action}。')
    guard = {'op': 'literal', 'source': ref(preamble)}
    row = relation('控制器', '配对结果', action, guard=guard, kind='保存')
    row.update(claim=2, claim_path=[2,1])
    for citation in row['evidence'].values():
        citation['claim'] = 2
    action_anchor = {'claim': 2, 'quote': action}
    raw = proposal(body, [row], second_claim=True)
    raw['flow_edges'] = [{'role': 'transition', 'relation': 1, 'label': action_anchor}]
    plan = _reviewed_plan(*read_claims_text(body), raw, 23, 'f'*64)
    if local_guard:
        assert not plan['relations'] and not plan['flow_edges']
        return
    accepted, = plan['relations']
    assert accepted['condition']['terms'] == []
    applicability, = accepted['condition']['applicability']
    assert (applicability['scope'], applicability['claim'], applicability['quote']) == (
        'ancestor_preamble', 1, preamble)
    assert plan['relation_candidates'][0]['raw'] == row
    assert len(plan['flow_edges']) == 1


@pytest.mark.parametrize('actor_in_title, current_actor, accepted', [
    ('接收侧', '', True),
    ('接收侧与发射侧', '', False),
    ('接收侧', '发射设备', False),
])
@pytest.mark.parametrize('full_title_cite', [False, True])
def test_dependent_method_actor_needs_an_unambiguous_parent_and_no_local_reassignment(
        actor_in_title, current_actor, accepted, full_title_cite):
    from patent_drawing.claims_input import _reviewed_plan, read_claims_text

    cause = '版本失配'
    clause = f'{cause}则{current_actor}拒绝提示'
    source = (f'1. 一种{actor_in_title}处理方法，其特征在于，接收数据。\n'
              f'2. 根据权利要求1所述的方法，其特征在于，{clause}。')
    row = relation('接收侧', '提示', clause, kind='拒绝')
    row.update(claim=2, claim_path=[2, 1], condition=condition({'op': 'literal', 'source': ref(cause, claim=2)}))
    title_citation = actor_in_title + '处理方法' if full_title_cite else '接收侧'
    row['evidence'] = {'subject': ref(title_citation), 'object': ref(clause, claim=2),
                       'relation': ref(clause, claim=2)}
    raw = proposal(source, [row], second_claim=True)
    raw['flow_edges'] = [{'role': 'branch', 'relation': 1, 'label': ref(cause, claim=2)}]
    output = _reviewed_plan(*read_claims_text(source), raw, 23, 'd'*64)
    assert bool(output['relations']) is accepted
    assert bool(output['flow_edges']) is accepted
    if accepted:
        item, = output['relation_candidates']
        assert item['evidence_rebindings']['rule'] == 'direct_dependent_method_actor'
        assert item['raw'] == row


@pytest.mark.parametrize('action_prefix, endpoint_context, accepted', [
    ('', None, True),
    ('禁止', None, False),
    ('接收侧向发射侧', None, False),
    ('', '不在原文内', False),
])
def test_bare_message_actor_anchors_to_preamble_only_for_supported_direction(
        action_prefix, endpoint_context, accepted):
    from patent_drawing.claims_input import _reviewed_plan, read_claims_text

    preamble = '在发射侧与接收侧具有可达控制通路的条件下'
    action = '按配对发送参考'
    source = f'1. 一种发射侧测量方法，其特征在于，{preamble}：{action_prefix}{action}。'
    row = relation('发射侧', '接收侧', action, guard=literal(preamble), kind=action)
    row['evidence'].update(subject=ref('发射侧', **({'context': endpoint_context} if endpoint_context else {})),
                           object=ref(preamble))
    raw = proposal(source, [row])
    raw['flow_edges'] = [{'role': 'message', 'relation': 1, 'label': ref(action)}]
    output = _reviewed_plan(*read_claims_text(source), raw, 23, 'e'*64)
    assert bool(output['relations']) is accepted
    assert bool(output['flow_edges']) is accepted
    if accepted:
        item, = output['relation_candidates']
        assert item['endpoint_citation_rebindings']['subject']['corrected']['quote'] == '发射侧'


@pytest.mark.parametrize('selector, accepted', [('active', True), ('sibling', False)])
def test_regional_conjunction_keeps_only_the_active_region_when_leaf_cites_its_whole_action(
        selector, accepted):
    from patent_drawing.claims_input import _reviewed_plan, read_claims_text

    shared = '在已辨别两处需求差异的条件下'
    local = '在需要供料的区域'
    active = local + '保持开口'
    sibling = '在不宜供料的区域移除开口'
    passage = f'{shared}，沿行程划分，{active}，{sibling}'
    body = f'1. 一种处理方法，包括：{passage}。'
    guard = {'op': 'all', 'source': ref(passage),
             'items': [literal(shared), literal(active if selector == 'active' else sibling)]}
    row = relation('需要供料的区域', '开口', active, guard=guard, kind='保持')
    row['evidence']['object'] = ref(active)
    output = _reviewed_plan(*read_claims_text(body), proposal(body, [row]), 23, 'b'*64)
    assert bool(output['relations']) is accepted
    if accepted:
        result, = output['relations']
        assert [term['quote'] for term in result['condition']['terms']] == [local, shared]
        assert result['condition']['expression']['source']['quote'] == local
        assert output['relation_candidates'][0]['raw'] == row


@pytest.mark.parametrize('count, accepted', [(68, True), (95, True), (96, True), (97, False)])
def test_current_reviewed_view_keeps_large_verified_reference_lists_within_its_bound(count, accepted):
    from patent_drawing.claims_input import _reviewed_plan, read_claims_text

    features = [f'测量配对记录{i}的时刻和序号' for i in range(1, count + 1)]
    body = f'1. 一种处理方法，{"；".join(features)}。'
    raw = {
        'views': [{'type': 'sequence', 'title': '配对时序', 'purpose': '保留逐次记录',
                   'features': [ref(quote) for quote in features]}],
        'coverage': [{'claim': 1, 'figures': [1], 'reason': '同图表达'}],
        'unresolved': [],
    }
    result = _reviewed_plan(*read_claims_text(body), raw, 23, 'e'*64)
    if not accepted:
        assert result['status'] == 'partial' and not result['views']
        assert result['view_limit_issues'][0]['view_id'] == 'fig1'
        assert result['view_limit_issues'][0]['logical_count'] == 97
        assert result['coverage'][0]['disposition'] == 'unverified'
        return
    assert [f['quote'] for f in result['views'][0]['features']] == features
    with pytest.raises(ValueError, match='特征清单无效'):
        _reviewed_plan(*read_claims_text(body), raw, 17, 'e'*64)

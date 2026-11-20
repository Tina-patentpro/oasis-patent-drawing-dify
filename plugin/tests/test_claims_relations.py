from copy import deepcopy
import hashlib

import pytest

from patent_drawing.claims_input import claims_plan


BODY = ("1. 一种装置，含甲端和乙端。甲端连接乙端；仅在盖板闭合时。"
        "\n2. 根据权利要求1所述的装置，其中采用隔离门。"
        "\n3. 一种装置，含第三端与第四端；仅在闸门开启时。"
        "\n4. 根据权利要求2或3所述的装置，其中设有继电器。")
RESPONSE_SHA = 'b' * 64


def ref(quote, claim=1, **kwargs):
    return {'claim': claim, 'quote': quote, **kwargs}


def candidate():
    link = ref('甲端连接乙端')
    return {
        'views': [{'type': 'structure', 'title': '连接关系', 'purpose': '表示连接与条件',
                   'features': [{**link, 'text': link['quote']}]}],
        'coverage': [{'claim': n, 'figures': [1] if n == 1 else [],
                      'reason': '同版核对'} for n in range(1, 5)],
        'unresolved': [],
        'relations': [{
            'claim': 1, 'views': [1], 'required': True, 'kind': 'connects',
            'subject': '甲端', 'object': '乙端', 'direction': 'directed',
            'claim_path': [1],
            'evidence': {'subject': ref('甲端', context=link['quote']),
                         'object': ref('乙端', context=link['quote']), 'relation': link},
            'condition': {'state': 'stated', 'operator': 'single',
                          'terms': [{**ref('仅在盖板闭合时'), 'polarity': 'positive'}],
                          'description': '闭盖方可进行'}},
        ],
    }


def current(raw, body=BODY):
    return claims_plan(body, raw, reviewed=True, relation_contract=3, response_sha256=RESPONSE_SHA)


def test_independent_condition_and_anchors_use_exact_utf8_spans():
    plan = current(candidate())
    assert plan['schema_version'] == 'claims-drawing-plan-3'
    relation, = plan['relations']
    assert relation['view_ids'] == ['fig1']
    assert relation['condition']['terms'][0]['quote'] == '仅在盖板闭合时'
    source = BODY.encode()
    assert plan['claims_sha256'] == hashlib.sha256(source).hexdigest()
    for value in [*relation['evidence'].values(), *relation['condition']['terms']]:
        assert source[value['start']:value['end']].decode() == value['quote']
    assert plan['relation_candidates'][0]['response_sha256'] == RESPONSE_SHA
    assert plan['status'] == 'plan_ready'


@pytest.mark.parametrize('change,reason', [
    ('wrong_claim', 'citation_not_found'),
    ('repeated', 'citation_ambiguous'),
    ('reversed_negation', 'condition_polarity_mismatch'),
    ('unquoted', 'citation_not_found'),
    ('missing_direction', 'direction_unverified'),
    ('outside_condition', 'cross_branch'),
])
def test_rejected_candidate_preserves_normalized_and_original_context(change, reason):
    raw = candidate()
    row, = raw['relations']
    if change == 'wrong_claim':
        row['condition']['terms'][0]['claim'] = 2
    if change == 'repeated':
        row['evidence']['subject'].pop('context')
    if change == 'reversed_negation':
        row['condition']['terms'][0]['polarity'] = 'negative'
    if change == 'unquoted':
        row['condition']['terms'][0]['quote'] = '闭盖方可进行'
    if change == 'missing_direction':
        row.pop('direction')
    if change == 'outside_condition':
        row['condition']['terms'][0].update(ref('仅在闸门开启时', 3))
    plan = current(raw)
    failure, = plan['relation_candidates']
    assert reason in {item['code'] for item in failure['reasons']}
    assert failure['status'] == 'rejected'
    assert failure['raw'] == row
    assert failure['model_required'] is True
    assert failure['claim'] == 1 and failure['view_ids'] == ['fig1']
    assert failure['source_index'] == 0 and failure['response_sha256'] == RESPONSE_SHA
    assert plan['status'] == 'partial'
    assert not plan['relations'] and len(plan['views']) == 1


def test_rejects_cherry_picked_citations_from_mutually_exclusive_parents():
    raw = candidate()
    row, = raw['relations']
    row['claim'] = 4
    row['claim_path'] = [4, 2, 1]
    row['condition']['terms'][0].update(ref('仅在闸门开启时', 3))
    item, = current(raw)['relation_candidates']
    assert item['status'] == 'rejected'
    assert 'cross_branch' in {reason['code'] for reason in item['reasons']}


def test_rejected_invalid_view_retains_valid_remapped_reference_and_bad_raw_value():
    raw = candidate()
    raw['views'].append(deepcopy(raw['views'][0]))
    raw['relations'][0]['views'] = [2, 3, True]
    raw['relations'][0]['required'] = 'true'
    plan = current(raw)
    item, = plan['relation_candidates']
    assert item['model_required'] is None
    assert item['raw']['views'] == [2, 3, True] and item['raw']['required'] == 'true'
    assert item['view_ids'] == ['fig1']
    assert {r['code'] for r in item['reasons']} >= {'invalid_view', 'required_unknown'}


def test_duplicates_preserve_receipt_and_model_optional_alone_does_not_clear_review():
    raw = candidate()
    row, = raw['relations']
    optional = {**row, 'required': False, 'views': [2]}
    raw['relations'] += [deepcopy(row), optional]
    plan = current(raw)
    first, duplicate, rejected = plan['relation_candidates']
    assert [first['status'], duplicate['status'], rejected['status']] == ['accepted', 'equivalent', 'rejected']
    assert duplicate['equivalent_to'] == first['candidate_id']
    assert len({x['candidate_id'] for x in plan['relation_candidates']}) == 3
    assert rejected['model_required'] is False and plan['status'] == 'partial'
    assert plan['limitations'][-1]['impact'] == 'review'


def test_replay_legacy_candidate_keeps_scope_and_cites_its_full_original_row():
    raw = candidate()
    row = {'subject': '甲端', 'object': '乙端', 'kind': '连接', 'condition': '盖板开启时',
           'claim': 1, 'quote': '甲端连接乙端', 'views': [1], 'required': True}
    raw['relations'] = [row]
    legacy = claims_plan(BODY, raw, reviewed=True)
    assert legacy['schema_version'] == 'claims-drawing-plan-2'
    v3 = current(raw)
    item, = v3['relation_candidates']
    assert item['raw'] == row and item['model_required'] is True
    assert item['view_ids'] == ['fig1'] and item['status'] == 'rejected'
    assert 'legacy_evidence_incomplete' in {r['code'] for r in item['reasons']}


def test_detects_explicit_source_to_destination_arrow_reversed():
    body = BODY.replace('甲端连接乙端', '甲端向乙端发送消息')
    raw = candidate()
    view = raw['views'][0]['features'][0]
    view.update(text='甲端向乙端发送消息', quote='甲端向乙端发送消息')
    row = raw['relations'][0]
    row.update(subject='乙端', object='甲端')
    row['evidence'] = {'subject': ref('乙端', context=view['quote']),
                       'object': ref('甲端', context=view['quote']), 'relation': ref(view['quote'])}
    item, = current(raw, body)['relation_candidates']
    assert 'direction_mismatch' in {reason['code'] for reason in item['reasons']}


def test_detects_explicit_or_misread_as_joint_requirement():
    body = BODY.replace('仅在盖板闭合时', '仅在盖板闭合或闸门开启时')
    raw = candidate()
    raw['relations'][0]['condition'].update(operator='all', terms=[
        {**ref('盖板闭合'), 'polarity': 'positive'},
        {**ref('闸门开启', context='仅在盖板闭合或闸门开启时'), 'polarity': 'positive'},
    ])
    item, = current(raw, body)['relation_candidates']
    assert 'condition_operator_mismatch' in {reason['code'] for reason in item['reasons']}


def test_present_in_other_clause_does_not_ground_a_relation_endpoint():
    body = BODY.replace('；仅在盖板闭合时。', '；仅在盖板闭合时。丙端连接丁端。')
    raw = candidate()
    row = raw['relations'][0]
    row['object'] = '丁端'
    row['evidence']['object'] = ref('丁端')
    item, = current(raw, body)['relation_candidates']
    assert item['status'] == 'rejected'
    assert 'endpoint_unverified' in {reason['code'] for reason in item['reasons']}


def test_explicit_adjacent_source_condition_cannot_be_marked_absent():
    raw = candidate()
    raw['relations'][0]['condition'] = {'state': 'none_in_source', 'operator': 'none', 'terms': []}
    item, = current(raw)['relation_candidates']
    assert item['status'] == 'rejected'
    assert 'condition_unverified' in {reason['code'] for reason in item['reasons']}

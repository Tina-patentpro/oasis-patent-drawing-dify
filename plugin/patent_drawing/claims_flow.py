"""Bind explicit flow edges to verified relation, condition and claims evidence."""

import re
from collections import defaultdict

from .claims_relations import _find_span, _stated_message_pair, source_clause
from .operation_store import digest


ROLES = {'branch', 'message', 'transition', 'exit'}
MODE_LIST = re.compile(r'(?<![A-Za-z0-9_])[A-Z][A-Z_]{2,}(?:\s*或\s*[A-Z][A-Z_]{2,}){2,}(?![A-Za-z0-9_])')
TX = re.compile(r'发射(?:侧|端|设备)')
RX = re.compile(r'接收(?:侧|端|设备)')
MODE_EFFECTS = {'TRACK': r'受限|限幅|修正v0', 'BYPASS': r'单位增益|旁路',
                'NO_COMMAND': r'无候选|不输出|候选[^。；，]+无效'}


def _mode_requirements(source, ranges):
    for claim, (start, end) in ranges.items():
        clause = source[start:end].decode()
        for match in MODE_LIST.finditer(clause):
            if '状态' in clause[max(0, match.start()-16):match.end()+12]:
                yield claim, tuple(s.strip() for s in match[0].split('或'))


def _can_label_from(claim, relation, parents):
    if claim in relation['claim_path']:
        return True
    # A dependent claim may name a mode that the parent has already specified.
    # Such a label proves vocabulary only, never the parent edge or guard.
    pending = [claim]
    visited = set()
    while pending:
        current = pending.pop()
        if current == relation['claim']:
            return True
        if current not in visited:
            visited.add(current)
            pending.extend(parents[current])
    return False


def _actor(text):
    if TX.search(text) and not RX.search(text):
        return 'tx'
    if RX.search(text) and not TX.search(text):
        return 'rx'
    return None


def _source_message_pair(quote, token, *, two_sides=False):
    actor_name = r'(?:发射|接收)(?:侧|端|设备)'
    explicit = re.compile(r'(?P<source>' + actor_name + r')(?:[^，。；向]{0,30})向(?:所述)?'
                          r'(?P<target>' + actor_name + r')[^，。；]{0,20}(?:发送|返回|反馈|传送)')
    for statement in re.split(r'[，。；,;]', quote):
        if token not in statement:
            continue
        if found := explicit.search(statement):
            return _actor(found['source']), _actor(found['target'])
        actors = list(re.finditer(actor_name, statement))
        verb = re.search(r'返回|发送|传送', statement)
        if len(actors) == 1 and verb and actors[0].end() <= verb.start():
            active = _actor(actors[0][0])
            if active and (two_sides or TX.search(quote) and RX.search(quote)):
                return active, ('rx' if active == 'tx' else 'tx')
    return None


def _message_matches(relation, label):
    quote = relation['evidence']['relation']['quote']
    actual = (_actor(relation['subject']), _actor(relation['object']))
    tokens = ('返回', '反馈') if ('反馈' in label or '返回' in label) else ('参考',)
    if any(token in label for token in tokens) and any(actual):
        pairs = [_source_message_pair(quote, token) for token in tokens]
        return None not in actual and actual in pairs
    return label in quote


def _measurement_message_without_roles(relation, label, source, ranges):
    endpoints = [relation[name].removeprefix('所述') for name in ('subject', 'object')]
    if all(TX.fullmatch(name) or RX.fullmatch(name) for name in endpoints):
        return False
    start, end = ranges[label['claim']]
    claim = source[start:end].decode()
    label_text = label['quote']
    action = re.search(r'发送(?:所述)?参考|(?:返回|回传)[^，。；]{0,110}(?:反馈|指标|measurement_validity)',
                       label_text)
    return bool(action and TX.search(claim) and RX.search(claim) and '发送参考' in claim)


def _source_exclusion(relation, label):
    quote = label['quote']
    evidence = relation['evidence']['relation']
    if (label['claim'] != relation['claim'] or label['claim'] != evidence['claim']
            or not evidence['start'] <= label['start'] < label['end'] <= evidence['end']):
        return None
    match = re.fullmatch(r'(?P<subject>[^，。；]{1,100})不(?:得)?用于(?P<effect>[^，。；]{1,100})[。；]?', quote)
    if not match or match['subject'] != relation['subject'] or relation['object'] not in match['effect']:
        return None
    return label


def _message_evidence(relation, label, source, ranges, *, contract=5):
    quote = relation['evidence']['relation']
    text = label['quote']
    actual = (_actor(relation['subject']), _actor(relation['object']))
    role = ('feedback' if '反馈' in text or '返回' in text
            else 'reference' if '参考' in text else 'hint' if '提示' in text else None)
    if contract >= 7 and role == 'reference' and re.search(r'发送[^，。；]{0,40}提示', text):
        if not re.search(r'发送参考', text):
            role = 'hint'
    if contract >= 7 and role == 'feedback' and re.search(r'发送(?:所述)?参考', text):
        return None
    if role is None or actual == (None, None):
        return {} if _message_matches(relation, text) else None
    if label['claim'] != quote['claim'] or not quote['start'] <= label['start'] < label['end'] <= quote['end']:
        return None
    explicit = _source_message_pair(text, '返回' if role == 'feedback' and '返回' in text
                                    else {'feedback':'反馈','reference':'参考','hint':'提示'}[role])
    if explicit and explicit != actual:
        return None
    if re.search(r'(?:不得|禁止|不再|未)(?:[^，；。]{0,24})(?:发送|返回|传送)', text):
        return None
    acceptance = None
    if contract >= 7 and role == 'feedback':
        _, right = source_clause(source, ranges, label)
        tail = source[label['end']:right].decode()
        remaining = re.split(r'[，,。；]', tail, maxsplit=1)[0]
        if re.search(r'有效性(?:反馈)?|measurement_validity', remaining):
            return None
        following = re.match(r'^[^，,。；]*[，,](?P<guard>仅(?:采用|使用|接受)[^，,；。]{1,96}有效[^，,；。]{0,30}反馈)(?:[，,。；]|$)', tail)
        if following:
            index = label['end'] + len(tail[:following.start('guard')].encode())
            guard = following['guard']
            acceptance = {'claim': label['claim'], 'quote': guard, 'start': index,
                          'end': index + len(guard.encode())}

    if contract >= 7:
        left, right = source_clause(source, ranges, label)
        delimiters = re.compile(r'[，,。；]')
        before = delimiters.split(source[left:label['start']].decode())[-1]
        after = delimiters.split(source[label['end']:right].decode(), maxsplit=1)[0]
        action = before + text + after
        token = ('返回' if '返回' in text else '反馈') if role == 'feedback' else {'reference':'参考','hint':'提示'}[role]
        bound_sides = TX.search(quote['quote']) and RX.search(quote['quote'])
        pair = _source_message_pair(action, token, two_sides=bool(bound_sides))
        if contract >= 9 and not pair:
            pair = _stated_message_pair(action)
        if pair and pair != actual:
            return None
        matched = pair == actual
    else:
        matched = (_message_matches(relation, text) if role != 'hint'
                   else actual == _source_message_pair(quote['quote'], '提示'))
    if not matched and actual in {('tx', 'rx'), ('rx', 'tx')} and label['claim'] == quote['claim']:
        start, end = ranges[quote['claim']]
        claim_text = source[start:end].decode()
        header = re.split('[，：。；]', claim_text, maxsplit=1)[0]
        actor_cited = {item['claim'] for item in (relation['evidence']['subject'], relation['evidence']['object'])}
        if (TX.search(header) and RX.search(claim_text) and actor_cited == {quote['claim']}
                and text in quote['quote']):
            # A transmitting-side claim may receive an implicit return.
            expected = (('rx', 'tx') if role == 'feedback' and re.search(r'接收经[^，；]{1,16}返回', text)
                        else ('tx', 'rx') if role == 'reference' and '发送参考' in text
                        and ('配对' in quote['quote'] or '接收候选' in claim_text)
                        else ('tx', 'rx') if contract >= 7 and role == 'hint'
                        and re.search(r'向(?:所述)?接收(?:侧|端|设备)发送[^，。；]{0,40}提示', action) else None)
            matched = actual == expected
    if not matched:
        return None

    details = {'message_kind': role}
    if acceptance:
        details['feedback_acceptance'] = acceptance
    if label['claim'] == quote['claim']:
        transport = re.compile(r'(?:经(?:由)?|通过|沿)([A-Za-z][A-Za-z0-9_-]*)(?:直接)?'
                               r'(?:向(?:所述)?(?:发射|接收)(?:侧|端|设备))?(?:返回|发送|传送)')
        matches = list(transport.finditer(text))
        if len(matches) == 1:
            result = _find_span(source, ranges, {'claim': label['claim'],'quote': matches[0][1], 'context': text})
            if not result[1]:
                details['transport'] = result[0]
    return details


def _effect_clause(relation, source, ranges):
    anchor = relation['evidence']['object']
    left, right = source_clause(source, ranges, anchor)
    boundaries = re.compile(r'[，,。；]')
    before = boundaries.split(source[left:anchor['start']].decode())[-1]
    after = boundaries.split(source[anchor['end']:right].decode(), maxsplit=1)[0]
    return before + anchor['quote'] + after


def _guard_failure(relation, label, role, source, ranges, *, contract=4):
    condition = relation['condition']
    terms = [span['quote'] for span in condition['terms']]
    anchor = relation['evidence']['object']
    left, right = source_clause(source, ranges, anchor)
    action = source[left:right].decode()
    if (contract >= 8 and role == 'exit'
            and re.search(r'供(?:可选的?)?下游|不参与', label)
            and not re.search(r'终止|退出|停止|结束|保持失败', label)):
        return 'flow_exit_unverified'
    if role in ('branch', 'exit') and not terms:
        return 'flow_guard_missing'
    if '宽搜' in label and role == 'branch':
        if any('不可达' in term or '丢失' in term or '预算耗尽' in term for term in terms):
            return 'flow_guard_mismatch'
        if re.search(r'不(?:再|得|应)?(?:进入|开展|执行|转入)宽搜', action):
            return 'flow_guard_mismatch'
        if '可达' in action and not any('可达' in term for term in terms):
            return 'flow_guard_mismatch'
        if terms and all('误差界未知' in term for term in terms):
            return 'flow_guard_mismatch'
    if '宽搜' in label and role == 'exit' and not re.search(r'退出|终止|停止|保持失败', action):
        return 'flow_guard_mismatch'
    if label in MODE_EFFECTS:
        effect = _effect_clause(relation, source, ranges) if contract >= 5 else action
        if not re.search(MODE_EFFECTS[label], effect):
            return 'flow_guard_mismatch'
    return None


def _mode_binding(role, label, relation, declarations, parents, source, ranges):
    if role not in ('branch', 'exit'):
        return None
    if label['quote'] in MODE_EFFECTS:
        return label
    effect = _effect_clause(relation, source, ranges)
    matches = [mode for mode, pattern in MODE_EFFECTS.items() if re.search(pattern, effect)]
    if len(matches) != 1 or role == 'exit' and matches[0] != 'NO_COMMAND':
        return None
    mode, = matches
    exact = [_find_span(source, ranges, {'claim': claim, 'quote': mode})[0]
             for claim, values in declarations if mode in values and _can_label_from(claim, relation, parents)]
    return exact[0] if len(exact) == 1 else None


def _edge_issue(item, detail):
    return {'impact': 'required', 'claims': item['claims'], 'view_ids': item['view_ids'],
            'flow_candidate_id': item['id'], 'detail': detail, 'question': '请核对该边的原文、方向或适用条件。'}


def consolidate_flow_placements(relations, edges, views):
    """Put an already verified guarded flow on its unique source-bound flow view."""
    by_id = {view['id']: view for view in views}
    grouped = defaultdict(list)
    for edge in edges:
        grouped[edge['relation_id']].append(edge)

    def supported(view, proof):
        return any(not feature.get('location_ambiguous') and feature['claim'] == proof['claim']
                   and feature['start'] <= proof['start'] < proof['end'] <= feature['end']
                   for feature in view['features'])

    migrated = []
    for relation in relations:
        related = grouped.get(relation['id'])
        if not related or any(edge.get('role') == 'message' for edge in related):
            continue
        ids = relation['view_ids']
        flows = [v for v in ids if by_id[v]['type'] == 'flow']
        duplicates = [v for v in ids if by_id[v]['type'] == 'relationship']
        if len(flows) != 1 or not duplicates:
            continue
        destination, = flows
        guard = relation['condition']
        proofs = {(proof['claim'], proof['start'], proof['end']): proof for proof in (
            *relation['evidence'].values(), *guard.get('terms', []), *guard.get('applicability', []),
            *(edge['label'] for edge in related),
        )}
        if not all(supported(by_id[destination], proof) for proof in proofs.values()):
            continue
        remaining = [v for v in ids if v not in duplicates]
        relation['view_ids'] = remaining
        for edge in related:
            edge['view_ids'] = remaining
        migrated.extend({'relation_id': relation['id'], 'from': v, 'to': destination,
                         'citations': list(proofs.values())} for v in duplicates)
    return migrated


def verified_flow(rows, relations, candidates, source, ranges, parents, response_sha, *, contract=4):
    edges, originals, issues, present = [], [], [], defaultdict(list)
    by_id = {item['id']: item for item in relations}
    declarations = list(_mode_requirements(source, ranges))
    for index, raw in enumerate(rows):
        data = raw if isinstance(raw, dict) else {}
        role, number = data.get('role'), data.get('relation')
        prior = candidates[number - 1] if type(number) is int and 1 <= number <= len(candidates) else {}
        relation = by_id.get(prior.get('equivalent_to') or prior.get('candidate_id'))
        ident = 'edge-' + digest([response_sha, index, raw, prior.get('candidate_id')])[:20]
        known_claim = prior.get('claim')
        record = {'id': ident, 'raw': raw, 'source_index': index, 'relation_candidate_id': prior.get('candidate_id'),
                  'claims': [known_claim] if known_claim else [], 'view_ids': prior.get('view_ids', []),
                  'response_sha256': response_sha,
                  'status': 'rejected', 'reasons': []}
        originals.append(record)
        label, error = _find_span(source, ranges, data.get('label'))
        if relation and label and role in {'message', 'branch'} and (action := relation.get('guarded_action')):
            guard = relation['condition'].get('expression', {}).get('source')
            if label in (relation['evidence']['relation'], action, guard) and (
                    role == 'branch' or not _message_evidence(relation, label, source, ranges, contract=contract)):
                label, role = action, 'branch'
                record['label_rebinding'] = {'quote': action['quote'], 'role': role}
        title = label['quote'] if label else str((data.get('label') or {}).get('quote', '')) if isinstance(data.get('label'), dict) else ''
        reasons = record['reasons']
        if (not relation or not isinstance(role, str) or role not in ROLES
                or relation['direction'] not in {'directed', 'ordered'}):
            reasons.append('flow_relation_unverified')
            if not relation and prior.get('wire_guard', {}).get('status') == 'unresolved':
                reasons.append('flow_guard_unverified')
        if error:
            reasons.append(error)
        elif relation and not _can_label_from(label['claim'], relation, parents):
            reasons.append('cross_branch')
        message = {}
        if relation and label and role == 'message':
            if contract >= 8 and _measurement_message_without_roles(relation, label, source, ranges):
                reasons.append('flow_message_endpoints_missing')
            if contract >= 5:
                message = _message_evidence(relation, label, source, ranges, contract=contract)
            elif not _message_matches(relation, title):
                message = None
            if message is None:
                reasons.append('flow_direction_mismatch')
        failure = _guard_failure(relation, title, role, source, ranges, contract=contract) if relation and label else None
        exclusion = (_source_exclusion(relation, label) if contract >= 8 and relation and label
                     and role == 'branch' and failure == 'flow_guard_missing' else None)
        if failure and not exclusion:
            reasons.append(failure)
        if reasons:
            detail = (f'流程边{title}缺少可核验的收发主体。'
                      if 'flow_message_endpoints_missing' in reasons else f'流程边{title}的方向、出处或条件待核。')
            if 'flow_guard_unverified' in reasons:
                detail = f'流程边{title}的原关系守卫缺证。'
            issues.append(_edge_issue(record, detail))
            continue
        record['status'] = 'accepted'
        item = {'id': ident, 'role': role, 'label': label, 'source_id': relation['subject_id'],
                'target_id': relation['object_id'], 'claim_path': relation['claim_path'],
                'view_ids': relation['view_ids'], 'relation_id': relation['id'],
                'condition': relation['condition'], **message}
        if exclusion:
            item['source_exclusion'] = exclusion
        edges.append(item)
        if contract >= 5:
            if bound := _mode_binding(role, label, relation, declarations, parents, source, ranges):
                item['mode_binding'] = bound
                present[bound['quote']].append(item)
        elif role == 'branch':
            present[title].append(item)

    for claim, modes in declarations:
        # This check is an omission signal. The case acceptance matrix still
        # checks the full truth table and scope of every actual outlet.
        usable = {mode: [edge for edge in present[mode] if edge.get('mode_binding', edge['label'])['claim'] == claim] for mode in modes}
        for mode, outgoing in usable.items():
            if not outgoing:
                issues.append({'impact': 'required', 'claims': [claim], 'view_ids': [],
                               'detail': f'已列出的 {mode} 缺少有条件的输出边。',
                               'question': '该模式在主图从何处进入，输出及状态去向是什么？'})
        signatures = defaultdict(set)
        for mode, outgoing in usable.items():
            for edge in outgoing:
                guard = edge['condition']
                sig = (guard.get('operator'), tuple(sorted((t['claim'], t['start'], t['end']) for t in guard['terms'])))
                signatures[sig].add(mode)
        for states in signatures.values():
            if len(states) > 1:
                issues.append({'impact': 'required', 'claims': [claim], 'view_ids': [],
                               'detail': '同一守卫通向不同输出模式 ' + '／'.join(sorted(states)) + '。',
                               'question': '各模式的区别条件及优先级是什么？'})

    # Require both messages when the original claims state both with actors.
    for claim, (start, end) in ranges.items():
        clause = source[start:end].decode()
        if TX.search(clause) and RX.search(clause) and '发送参考' in clause and '返回' in clause:
            for token in ('参考', '反馈'):
                if contract >= 7:
                    kind = {'参考': 'reference', '反馈': 'feedback'}[token]
                    present_message = any(edge.get('message_kind') == kind and edge['label']['claim'] == claim
                                          and by_id[edge['relation_id']]['claim'] == claim for edge in edges)
                else:
                    present_message = any(edge['role']=='message' and (token in edge['label']['quote'] or
                           token == '反馈' and '返回' in edge['label']['quote'])
                           and claim in edge['claim_path'] for edge in edges)
                if not present_message:
                    issues.append({'impact': 'required', 'claims': [claim], 'view_ids': [],
                                   'detail': f'{token}缺少独立的有向关系。',
                                   'question': '哪一方发送，哪一方接收，经何通路？'})
    return edges, originals, issues

"""Recover explicit source roles and guards from rejected scope-shaped candidates."""

import re


_PATH_PAIR = re.compile(
    r'(?:^|[，：:])'
    r'(?P<related_action>(?:由)?(?:基于上下文|从)(?P<input>[A-Z])的依存关系表示'
    r'(?:并)?结合(?:目标)?(?P<target>[A-Z])(?:进行编码的|编码得到的)'
    r'目标相关(?:编码)?路径(?P<related>[A-Za-z]\([A-Z],[A-Z]\)))及'
    r'(?P<general_action>不输入当前(?P=target)指示的一般(?:编码)?路径'
    r'(?P<general>[A-Za-z]\([A-Z]\))分别生成表示)，'
    r'(?P<merge_action>将两路表示连接为(?P<merged>[A-Za-z])'
    r'(?:并由连接后的生成器产生关于(?:目标)?(?P=target)的候选事实句[A-Z])?)(?=$|[，,])'
)
_ADMISSION = re.compile(r'仅接纳(?P<guard>[^，；。]+)的候选')
_REJECTION = re.compile(r'拒绝(?P<guard>矛盾(?:或指向其他目标的)?候选)')
_PENDING = re.compile(r'(?:并)?将(?P<guard>[^，；。]+候选)保留待查')
_CALIBRATION = re.compile(r'在验证集合依据(?P<basis>[^，；。]+?)选择并冻结(?P<effect>[^，；。]+)')
_THRESHOLD = re.compile(r'(?P<input>[^，；。]+?)(?P<guard>均达到[^，；。]+?界限)才满足(?:所述)?(?P<effect>[^，；。]+)')
_BASELINE = re.compile(r'(?P<dataset>[A-Za-z][A-Za-z0-9_]*)为空时第二项为零并退回(?P<baseline>[^，；。]+基线)')
_EMPTY_TERM = re.compile(
    r'(?P<guard>(?:[A-Za-z][A-Za-z0-9_]*|弱正例集合)为空(?:时|则))'
    r'(?:第二项为零|增强损失为零)(?:并退回[^，；。]+基线)?'
)
_GROUP = re.compile(r'使(?P<input>[^，；。]+)保持在同一集合')
_ACTION_BOUNDARY = re.compile(r'(?:[，：:]|[，：:]并)\s*$')
_FOLLOWING_SEGMENT = re.compile(r'^\s*[，,；;]\s*(?:但(?:是)?|且|并且)?([^，,；;。\n]+)')
_CONDITION_INTRO = re.compile(r'^(?:仅在|仅当|只有|除非|否则|若|如果|条件为|前提为|前提是|例外)')
_SCOPE_EXCEPTION = re.compile(r'除[^，,：:；。]{1,100}(?:之外|以外|外)|除外|例外|不适用|不在此限')
_LEADING_SCOPE = re.compile(
    r'(?:^|[，,；;])\s*(?:(?:但)?(?:仅在|仅当|只有|若|如果|除非|否则)[^，,：:]*|'
    r'[^，,：:]+(?:条件下|情况下|时))\s*[，,](?:并)?\s*$'
)


def _direct_preamble(blob, ranges, claim):
    from .claims_relations import _ALTERNATE_PREAMBLE, _find_span, _PREAMBLE, CLAUSE_BREAK

    start, end = ranges[claim]
    text = blob[start:end].decode()
    m = _PREAMBLE.search(text) or _ALTERNATE_PREAMBLE.search(text)
    if not m or CLAUSE_BREAK.search(text[:m.start('guard')]):
        return None
    return _find_span(blob, ranges, {'claim': claim, 'quote': m['guard']})[0]


def _roundtrip_guard(row, blob, ranges, scope, action, expected, variants):
    """Preserve an explicit premise separately from an effect's literal guard."""
    from .claims_relations import _find_span

    condition = row.get('condition')
    expr = condition.get('expression') if isinstance(condition, dict) else None
    if not isinstance(expr, dict):
        return None
    condition_source, _ = _find_span(blob, ranges, expr.get('source'))
    if not condition_source:
        return None
    allowed = {}
    for claim in scope:
        preamble = _direct_preamble(blob, ranges, claim)
        if preamble:
            allowed[(claim, preamble['start'], preamble['end'])] = preamble

    def premise(span):
        return allowed.get((span['claim'], span['start'], span['end']))

    def local(span):
        return (span['claim'] == action['claim']
                and action['start'] <= span['start'] < span['end'] <= action['end']
                and span['quote'] in variants)

    previous = None
    if expr.get('op') == 'literal' and not expr.get('items'):
        previous = premise(condition_source)
        if not previous and not (expected and local(condition_source)):
            return None
        # A proposed preamble alone can omit only a predicate explicit in this action.
    elif expr.get('op') == 'all' and expected:
        children = expr.get('items')
        if not (isinstance(children, list) and len(children) == 2 and local(condition_source)):
            return None
        spans = []
        for node in children:
            if not (isinstance(node, dict) and node.get('op') == 'literal' and not node.get('items')):
                return None
            span, _ = _find_span(blob, ranges, node.get('source'))
            if not span:
                return None
            spans.append(span)
        previous = premise(spans[0])
        if not previous or not local(spans[1]):
            return None
    else:
        return None
    return {'premise': previous}


def _isolated_action(blob, ranges, action):
    from .claims_relations import CLAUSE_BREAK, _segment_guard, source_clause

    left, right = source_clause(blob, ranges, action)
    claim_start, claim_end = ranges[action['claim']]
    before = blob[left:action['start']].decode()
    after = blob[action['end']:right].decode()
    if ((before and not _ACTION_BOUNDARY.search(before))
            or (after and not after.lstrip().startswith(('，', ',')))
            or _LEADING_SCOPE.search(before) or _SCOPE_EXCEPTION.search(after)):
        return False
    prefix = re.sub(r'[，,：:](?:并)?\s*$', '', before)
    adjacent = re.split(r'[，,：:]', prefix)[-1].strip()
    if not before and left > claim_start:
        earlier = blob[claim_start:left].decode()
        if earlier.endswith(('；', ';')):
            adjacent = CLAUSE_BREAK.split(earlier[:-1])[-1].strip()
    own_preamble = _direct_preamble(blob, ranges, action['claim'])
    declared = (own_preamble and before.endswith(('：', ':')) and own_preamble['quote'] == adjacent)
    if adjacent and not declared and (_SCOPE_EXCEPTION.search(adjacent) or _CONDITION_INTRO.match(adjacent)
                                      or _segment_guard(adjacent, contract=13)[0]):
        return False
    tail = _FOLLOWING_SEGMENT.match(blob[action['end']:claim_end].decode())
    if tail:
        segment = tail[1].strip()
        if (_SCOPE_EXCEPTION.search(segment) or _CONDITION_INTRO.match(segment)
                or _segment_guard(segment, contract=13)[0]):
            return False
    return True


def _action_guard(quote, kind, target):
    if (match := _ADMISSION.fullmatch(quote)) and kind in {'接纳', '仅接纳'}:
        return match['guard']
    if (match := _REJECTION.fullmatch(quote)) and kind in {'拒绝', quote}:
        return match['guard']
    if (match := _PENDING.fullmatch(quote)) and kind == '保留待查':
        return match['guard']
    if (match := _THRESHOLD.fullmatch(quote)) and (
        kind in {'满足', '均达界限才满足'} or kind == match['guard']+'才满足'
    ):
        return match['input'] + match['guard']
    if match := _EMPTY_TERM.fullmatch(quote):
        if (kind in {'置零', '置为', '置为零'} and target in {'零', '第二项为零', '增强损失为零'}
                or kind in {'退回', '为空时退回', '为空则退回'}
                and target and quote.endswith('退回'+target) and target.endswith('基线')):
            return match['guard']
    return None


def recover_guard_root(row, blob, ranges, parents):
    """Prove a missing all-root from the source and its complete, cited children."""
    from .claims_relations import _applicable_source_scopes, _expression, _find_span, _path

    expr = row['condition']['expression']
    children = expr.get('items', [])
    if (row['wire_guard']['errors'] != [{'path': 'h.source', 'value': 0}]
            or expr['op'] != 'all' or not 2 <= len(children) <= 12
            or any(child['op'] != 'literal' or child.get('items') for child in children)):
        return None
    path = _path(row, row['claim'], parents)
    action, _ = _find_span(blob, ranges, row['evidence']['relation'])
    if (not path or parents.get(path[-1]) or not action or action['claim'] != row['claim']
            or not _isolated_action(blob, ranges, action)):
        return None
    children_spans = [_find_span(blob, ranges, child['source'])[0] for child in children]
    if not all(span and span['claim'] in path for span in children_spans):
        return None
    first, last = children_spans[0], children_spans[-1]
    guard = _action_guard(action['quote'], row['kind'], row['object'])
    preambles = [span for claim in path if (span := _direct_preamble(blob, ranges, claim))]
    if (len(children) == 2 and _direct_preamble(blob, ranges, first['claim']) == first
            and preambles == [first]
            and (first['claim'] != action['claim'] or first['end'] <= action['start'])
            and guard and last['claim'] == action['claim']
            and action['start'] <= last['start'] < last['end'] <= action['end']
            and last['quote'] in (guard, action['quote'])):
        context = blob[action['context_start']:action['context_end']].decode()
        source = {'claim': action['claim'], 'quote': guard, 'context': context}
        if _find_span(blob, ranges, source)[0]:
            return {'op': 'literal', 'source': source}, first, 'preamble_and_local_predicate'

    # A contiguous conjunction needs a source-declared local scope. The
    # literal children must cover it completely, including its connectors.
    if any(span['claim'] != action['claim'] for span in preambles):
        return None
    object_span, _ = _find_span(blob, ranges, row['evidence']['object'])
    anchor = object_span if object_span and action['start'] <= object_span['start'] < action['end'] else action
    scopes = _applicable_source_scopes(blob, ranges, anchor, contract=13)
    for scope in scopes:
        if (scope['scope'] != 'local' or scope['claim'] != action['claim']
                or not all(span['claim'] == scope['claim'] for span in children_spans)
                or not scope['start'] <= first['start'] < last['end'] <= scope['end'] < action['end']):
            continue
        effect = blob[scope['end']:action['end']].decode().lstrip('，, \t')
        if effect != row['subject'] + row['kind'] + row['object']:
            continue
        candidate = {**expr, 'source': {'claim': scope['claim'], 'quote': scope['quote']}}
        errors = []
        resolved = _expression(candidate, 'h', set(path), lambda spec, _: _find_span(blob, ranges, spec)[0],
                               lambda *error: errors.append(error), blob, [], contract=13)
        if (resolved and not errors and resolved['source']['start'] == scope['start']
                and resolved['source']['end'] == scope['end']):
            return candidate, None, 'complete_local_conjunction'
    return None


def _paired_roles(row, pair, action, clause_start, statement):
    """Bind two declared encoders to the same stated merge, not by names alone."""
    if not pair:
        return None
    if pair['related'].split('(', 1)[1] != f"{pair['input']},{pair['target']})":
        return None
    if pair['general'].split('(', 1)[1] != f"{pair['input']})":
        return None

    def cited(group, *, permit_omitted_prefix=False, permit_short_merge=False):
        start, end = pair.span(group)
        if permit_omitted_prefix and pair[group].startswith('由') and not action['quote'].startswith('由'):
            start += 1
        if permit_short_merge and action['quote'] == '将两路表示连接为'+pair['merged']:
            end = start + len(action['quote'])
        return (action['start'] == clause_start + len(statement[:start].encode())
                and action['end'] == clause_start + len(statement[:end].encode()))

    s, o, kind = row['subject'], row['object'], row['kind']
    inp, related, general, merged = (pair[k] for k in ('input', 'related', 'general', 'merged'))
    source_label = next((q for q in ('上下文'+inp, inp+'的依存关系表示') if q in pair[0]), None)
    if not source_label:
        return None
    general_start = '不输入当前' + pair['target'] + '指示'
    if (o in (related, related+'表示') and kind == '结合目标编码生成'
            and s in (source_label, inp, inp+'的依存关系表示与'+pair['target'])
            and cited('related_action', permit_omitted_prefix=True)):
        return source_label, related, None, 'declared_related_path'
    if (o in (general, general+'表示') and kind == general_start+'而编码生成'
            and s in (source_label, inp) and cited('general_action')):
        return source_label, general, None, 'declared_general_path'
    if o == merged and kind == '连接' and cited('merge_action', permit_short_merge=True):
        for node in (related, general):
            if s in (node, node+'表示'):
                return node, merged, None, 'declared_path_merge'
    return None


def _local_roles(row, action):
    """Select roles only when the action and proposed direction have explicit support."""
    s, o, k = (row[name] for name in ('subject', 'object', 'kind'))
    if (m := _ADMISSION.fullmatch(action)) and k == '接纳' and s in ('候选', '候选H') and o in ('接纳', '接纳项'):
        return '候选', '接纳', m['guard'], 'explicit_admission'
    if (m := _REJECTION.fullmatch(action)) and k == action and s in ('候选', '候选H', m['guard']) and o in ('拒绝', '拒绝出口'):
        return m['guard'], '拒绝', m['guard'], 'explicit_rejection'
    if (m := _PENDING.fullmatch(action)) and k == '保留待查' and s in ('候选', '候选H', m['guard']) and o in ('待查出口', '保留待查'):
        return m['guard'], '保留待查', m['guard'], 'explicit_pending'
    if (m := _CALIBRATION.fullmatch(action)) and k == '选择并冻结' and o == m['effect'] and s in (m['basis'], '验证集合及'+m['basis']):
        return m['basis'], m['effect'], None, 'calibrated_threshold_selection'
    if (m := _THRESHOLD.fullmatch(action)) and k.startswith(m['guard']+'才满足') and s == m['input'] and o in (m['effect'], '满足'+m['effect']):
        return m['input'], m['effect'], m['input']+m['guard'], 'explicit_joint_thresholds'
    if m := _BASELINE.fullmatch(action):
        if k == '置零' and s in ('增强损失项', m['dataset']) and o in ('零', '第二项为零'):
            return m['dataset'], '第二项为零', m['dataset']+'为空时', 'explicit_zero_baseline_term'
        if k == '退回'+m['baseline'] and s in ('任务训练', m['dataset']) and o in (m['baseline'], m['baseline']+'出口'):
            return m['dataset'], '退回'+m['baseline'], m['dataset']+'为空时', 'explicit_baseline_return'
    if (m := _GROUP.fullmatch(action)) and s == m['input'] and k == '保持在同一集合' and (
        o == '同一集合' or o == '预先划分的训练、验证和测试集合'
    ):
        return s, '同一集合', None, 'explicit_group_preservation'
    if k == '记录' and action.startswith('记录') and o == action[2:] and (
        s == '记录' or s == '候选H' and 'H' in o.split('、')
    ):
        return '记录', o, None, 'explicit_record_fields'
    return None


def recover_ancestor_applicability(row, item, blob, ranges, parents):
    """Keep a verbatim ancestor preamble outside the isolated action's local guard."""
    from .claims_relations import _applicable_source_scopes, _find_span, _path

    if item['reasons'] != [{'code': 'condition_scope_mismatch', 'field': 'condition.0'}]:
        return None
    claim = item['claim']
    path = _path(row, claim, parents)
    action = item['evidence'].get('relation')
    expr = row['condition'].get('expression')
    if (not path or not action or action['claim'] != claim
            or not expr or expr.get('op') != 'literal' or expr.get('items')
            or any(parents[child] != [parent] for child, parent in zip(path, path[1:]))):
        return None
    premise, _ = _find_span(blob, ranges, expr['source'])
    if (not premise or premise['claim'] not in path[1:]
            or _direct_preamble(blob, ranges, premise['claim']) != premise
            or _applicable_source_scopes(blob, ranges, action)
            or not _isolated_action(blob, ranges, action)):
        return None
    corrected = {**row, 'condition': {'state': 'none_in_source', 'operator': 'none', 'terms': []}}
    return corrected, 'ancestor_preamble_applicability', premise


def recover_scope_candidate(row, item, blob, ranges, parents):
    from .claims_relations import _find_span, _path, _segment_guard, source_clause

    if not (isinstance(row, dict) and row.get('direction') == 'directed'
            and all(isinstance(row.get(k), str) for k in ('subject', 'object', 'kind'))
            and item['reasons'] and all(
                r['code'] in {'endpoint_unverified', 'condition_unverified', 'condition_scope_mismatch'}
                for r in item['reasons']
            )):
        return None
    evidence = item['evidence']
    action = evidence.get('relation')
    scope = _path(row, item['claim'], parents)
    if not action or action['claim'] != item['claim'] or not scope:
        return None
    _, claim_end = ranges[action['claim']]
    following = _FOLLOWING_SEGMENT.match(blob[action['end']:claim_end].decode())
    if following:
        segment = following[1].strip()
        if _CONDITION_INTRO.match(segment) or _segment_guard(segment, contract=12)[0]:
            return None
    if any(name not in evidence or evidence[name]['claim'] != action['claim'] for name in ('subject', 'object')):
        return None
    left, right = source_clause(blob, ranges, action)
    statement = blob[left:right].decode()
    pairs = list(_PATH_PAIR.finditer(statement))
    pair = pairs[0] if len(pairs) == 1 else None
    roles = _paired_roles(row, pair, action, left, statement)
    before = blob[left:action['start']].decode()
    after = blob[action['end']:right].decode()
    if _LEADING_SCOPE.search(before):
        return None
    if roles is None and (not before or _ACTION_BOUNDARY.search(before)) and (
        not after or after.lstrip().startswith(('，', ','))
    ):
        roles = _local_roles(row, action['quote'])
    if roles is None:
        return None
    subject, target, guard, rule = roles
    variants = {action['quote'], guard} if guard else set()
    checked = _roundtrip_guard(row, blob, ranges, scope, action, guard, variants)
    if checked is None:
        return None
    previous = checked['premise']
    result = {**row, 'subject': subject, 'object': target, 'evidence': dict(row['evidence'])}
    for role, name in (('subject', subject), ('object', target)):
        original = evidence[role]
        citation = original['quote']
        if citation != action['quote'] and name not in citation:
            grouping = (role == 'object' and rule == 'explicit_group_preservation'
                        and previous == original
                        and '按source_group划分的训练、验证和测试集合' in citation)
            record = (role == 'subject' and rule == 'explicit_record_fields'
                      and original['end'] <= action['start']
                      and row['subject'].startswith('候选')
                      and re.search(r'产生关于[^，；。]*的候选事实句' + re.escape(row['subject'][2:]), citation))
            if not (grouping or record):
                return None
        # Prefer an exact occurrence inside the already verified action. A
        # declared paired input or merge may reference its sibling declaration.
        action_context = blob[action['context_start']:action['context_end']].decode()
        contexts = (action_context, pair[0] if pair and rule.startswith('declared_') else '', statement)
        for context in contexts:
            if context.count(name) != 1:
                continue
            spec = {'claim': item['claim'], 'quote': name, 'context': context}
            span, _ = _find_span(blob, ranges, spec)
            if span and left <= span['start'] < span['end'] <= right:
                result['evidence'][role] = spec
                break
        else:
            return None
    if guard or previous and previous['claim'] == item['claim']:
        spec = {'claim': item['claim'], 'quote': guard or previous['quote']}
        if guard:
            spec['context'] = action['quote']
        result['condition'] = {'state': 'stated', 'expression': {'op': 'literal', 'source': spec}}
    else:
        result['condition'] = {'state': 'none_in_source', 'operator': 'none', 'terms': []}
    return result, rule, previous

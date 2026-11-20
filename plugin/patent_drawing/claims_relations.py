"""Validate proposed relations against the confirmed claims and retain their provenance."""

import hashlib
import re

from .operation_store import digest


RELATION_CONTRACT = 23
ROLES = ('subject', 'object', 'relation')
DIRECTIONS = {'directed', 'symmetric', 'ordered'}
NEGATIVE = re.compile(r'不得|不(?:能|予|启|满|适|受|在|允许|通过|同|存|提|释|移|继|接|变)|未|无效|失败|失配')
EXCEPTION = re.compile(r'除非|除外|例外|否则')
CONDITIONAL = re.compile(r'仅在|仅当|只有|当.{1,80}时|情况下|条件下|否则|除非|若')
CLAUSE_BREAK = re.compile(r'[。；;\n]')
FOLLOWING_CONDITION = re.compile(r'^\s*(?:仅在|仅当|只有|除非|否则|若|条件为)')
REASONS = {
    'figure_citation_limit': '原图位引文超过单图上限',
    'citation_not_found': '指定权项未命中逐字引文',
    'citation_ambiguous': '原文引文出现多次，需更长上下文',
    'endpoint_unverified': '关系端点缺乏同一语境的来源',
    'cross_branch': '引证未落在同一适用权项链',
    'direction_unverified': '关系类型或方向未明确',
    'direction_mismatch': '关系方向与原文的明确指向不符',
    'condition_unverified': '适用条件及组合方式缺证',
    'guard_reference_out_of_range': '守卫引证编号超出范围，需核对原条件',
    'condition_scope_mismatch': '条件来自另一分支或作用域',
    'condition_polarity_mismatch': '条件极性与原文不一致',
    'condition_operator_mismatch': '条件组合与原文连词不一致',
    'invalid_claim': '适用权项未指定或超出正文编号',
    'invalid_view': '候选图号无效或缺失',
    'view_evidence_missing': '图内特征与关系引文未建立对应',
    'required_unknown': '模型未明确必需属性',
    'legacy_evidence_incomplete': '旧响应未提供分角色的引证与方向',
}


def part_id(label):
    return 'part-' + hashlib.sha256(label.encode()).hexdigest()[:16]


def _find_span(blob, ranges, spec):
    if not isinstance(spec, dict):
        return None, 'citation_not_found'
    claim, quote = spec.get('claim'), spec.get('quote')
    context = spec.get('context', quote)
    if (type(claim) is not int or claim not in ranges or not isinstance(quote, str) or not quote.strip()
            or not isinstance(context, str) or not context.strip()):
        return None, 'citation_not_found'
    start, end = ranges[claim]
    quote_bytes = quote.encode()
    context_bytes = context.encode()
    anchor = blob.find(context_bytes, start, end)
    within = context_bytes.find(quote_bytes)
    if anchor < 0 or within < 0:
        return None, 'citation_not_found'
    if (blob.find(context_bytes, anchor + 1, end) >= 0
            or context_bytes.find(quote_bytes, within + 1) >= 0):
        return None, 'citation_ambiguous'
    offset = anchor + within
    return {'claim': claim, 'quote': quote, 'start': offset, 'end': offset + len(quote_bytes),
            'context_start': anchor, 'context_end': anchor + len(context_bytes)}, None


def _path(row, claim, parents):
    path = row.get('claim_path', [claim])
    if (isinstance(path, list) and path and path[0] == claim
            and all(type(n) is int and n in parents for n in path)
            and all(parent in parents[child] for child, parent in zip(path, path[1:]))):
        return path
    return []


def _relation_context(source, ranges, relation):
    start, end = ranges[relation['claim']]
    left = CLAUSE_BREAK.split(source[start:relation['start']].decode())[-1]
    right = CLAUSE_BREAK.split(source[relation['end']:end].decode(), maxsplit=2)
    statement = left + relation['quote'] + right[0]
    following = right[1] if len(right) > 1 else ''
    return statement, bool(FOLLOWING_CONDITION.match(following))


def source_clause(blob, ranges, anchor):
    start, end = ranges[anchor['claim']]
    left = CLAUSE_BREAK.split(blob[start:anchor['start']].decode())[-1].encode()
    right = CLAUSE_BREAK.split(blob[anchor['end']:end].decode(), maxsplit=1)[0].encode()
    return anchor['start'] - len(left), anchor['end'] + len(right)


def _separate_later_action(blob, ranges, anchor):
    left, right = source_clause(blob, ranges, anchor)
    after = blob[anchor['end']:right].decode()
    before_comma, separator, following = after.partition('，')
    if not separator or not following.startswith(('仅在', '仅当')):
        return None
    guard = _GUARD_PREFIX.match(following) or _AFTER_CHECK.match(following)
    if not guard or not following[guard.end():].strip('，, '):
        return None
    return blob[left:anchor['end']].decode() + before_comma, False


def _guard_scope(blob, ranges, anchor, term):
    if term['claim'] == anchor['claim']:
        start, end = source_clause(blob, ranges, anchor)
        if start <= term['start'] and term['end'] <= end:
            return True
        remaining = blob[end:ranges[anchor['claim']][1]].decode()
        following = CLAUSE_BREAK.split(remaining, maxsplit=2)
        if len(following) > 1 and FOLLOWING_CONDITION.match(following[1]):
            offset = end + len(remaining[0].encode())
            if offset <= term['start'] and term['end'] <= offset + len(following[1].encode()):
                return True
    # A preamble can declare applicability for all later steps. Other cited
    # conditions must stay in the source clause for the effect they gate.
    start, end = ranges[term['claim']]
    clause = blob[start:end].decode()
    preamble = re.search(r'(?:包括|被配置为|其特征在于)[：:]', clause)
    return (bool(preamble) and term['end'] <= start + len(clause[:preamble.start()].encode())
            and bool(CONDITIONAL.search(term['quote'])))


_TIME_GUARD_END = r'时(?!间|序|效|刻|窗|期|限|点|基|钟|域|长|隙|标|延|段|机|态|差|频)'
_GUARD_PREFIX = re.compile(r'^(?:(?:仅在|仅当|在|当)[^，：；。]{1,420}?(?:条件下|情况下|' + _TIME_GUARD_END +
                           r')|在[^，：；。]{1,120}?的区域|(?!同时|时间|时序)[^，：；。]{2,420}?' +
                           _TIME_GUARD_END + ')')
_CAUSAL_EFFECT = re.compile(r'^(?:使|令|从而|以使|致使)')
_PREAMBLE = re.compile(r'(?:^|[，：])(?P<guard>在[^，：；。]{1,420}?(?:条件下|情况下))[：:]')
_ALTERNATE_PREAMBLE = re.compile(
    r'(?:^|[，：])(?P<guard>限于[^：；。]{1,420}?的条件|[^，：；。]{0,24}在[^：；。]{1,420}?'
    r'(?:条件下|情况下)，被配置为|在[^：；。]{1,420}?(?:条件下|情况下))[：:]')
_INDEPENDENT_RECOVERY = re.compile(r'^(?:在)?(?:崩溃|故障|断电|重启)恢复(?:时|后)[^，；。]{0,12}'
                                   r'(?:重放|重试|重新执行)')
_REGION_GUARD = re.compile(r'^在[^，：；。]{1,120}的区域$')
_ADMISSION_GUARD = re.compile(r'^仅接纳[^，：；。]{1,300}的候选$')
_LOCAL_UNAVAILABLE = re.compile(r'(?:不可达|丢失|失效|不可用)(?:时|则)?[^，；。]{0,20}(?:退出|终止|停用|回退)')

_COMPARISON = re.compile(r'(?:进行|执行|实施|作)(?:所述)?(?:比较|判别|核验)')
_TOPIC = re.compile(r'^对于[^，：；。]{2,200}$')
_AFTER_CHECK = re.compile(r'^(?:仅在|在)(?:核对|确认|收到|完成|达到)[^，：；。]{2,200}?后')
_COORDINATED_EFFECT = re.compile(r'^(?:并由|并(?:依据|根据|返回|保存|检查|核对|保留)|同时)')
_COORDINATED_PREFIX = re.compile(r'^(?:同时|并且|并)\s*')
_NEGATED_ACTION = re.compile(r'(?:^|[，,])\s*不(?P<action>(?:释放|采用|应用|输出|触发|保存|执行|启用|进入|继续)[^，。；]{2,60})$')


def _segment_guard(text, *, contract):
    shift = 0
    if contract >= 7 and (prefix := _COORDINATED_PREFIX.match(text)):
        shift = prefix.end()
    rest = text[shift:]
    if contract >= 8 and _ADMISSION_GUARD.fullmatch(rest):
        return rest, shift
    if contract >= 7 and re.fullmatch(r'[^，：；。]{2,320}(?:或者|或)[^，：；。]{1,100}时', rest):
        return rest, shift
    match = _GUARD_PREFIX.match(rest)
    if contract >= 7 and not match:
        match = _AFTER_CHECK.match(rest)
    return (match[0], shift) if match else ('', 0)


def _preceding_alternative_topic(blob, claim_start, left, clause):
    negative = _NEGATED_ACTION.search(clause)
    prefix = blob[claim_start:left].decode()
    if not negative or not prefix.endswith(('；', ';')):
        return None
    prior = CLAUSE_BREAK.split(prefix[:-1])[-1]
    action = negative['action']
    if not re.search(r'(?:再|才|方可)' + re.escape(action) + r'$', prior):
        return None
    topic = re.search(r'(?:^|[，：:])(?P<text>对于[^，：；。]{2,200})，', prior)
    if not topic:
        return None
    start = left - len((prior[topic.start('text'):] + prefix[-1]).encode())
    return start, topic['text']


def _applicable_source_scopes(blob, ranges, anchor, *, contract=5):
    """Locate source-declared scopes, retaining their exact source ranges."""
    claim = anchor['claim']
    claim_start, claim_end = ranges[claim]

    def span(start, quote, scope):
        return {'claim': claim, 'quote': quote, 'start': start, 'end': start + len(quote.encode()),
                'context_start': start, 'context_end': start + len(quote.encode()), 'scope': scope}

    claim_text = blob[claim_start:claim_end].decode()
    left, right = source_clause(blob, ranges, anchor)
    clause = blob[left:right].decode()
    scopes = []
    preamble = _PREAMBLE.search(claim_text)
    if contract >= 8 and not preamble:
        preamble = _ALTERNATE_PREAMBLE.search(claim_text)
    if preamble:
        quote = preamble['guard']
        start = claim_start + len(claim_text[:preamble.start('guard')].encode())
        exception = (left > start and re.match(
            r'^(?:否则|除非|[^，；]{1,70}?(?:不可用|丢失|失效)(?:时|则)?)[^；]{0,80}'
            r'(?:停用|退出|终止|回退|保持失败|宽搜)', clause))
        if contract >= 8 and left > start:
            adjacent = re.split(r'[，,]', blob[left:anchor['start']].decode())[-1]
            after = re.split(r'[，,]', blob[anchor['end']:right].decode(), maxsplit=1)[0]
            exception = exception or _LOCAL_UNAVAILABLE.search(adjacent + anchor['quote'] + after)
        if (start + len(quote.encode()) < anchor['start'] and not exception
                and not CLAUSE_BREAK.search(claim_text[:preamble.start('guard')])):
            scopes.append(span(start, quote, 'preamble'))

    # Include the part before an endpoint even if the relation cites the whole sentence.
    matches = list(re.finditer(r'[^，,：:]+', clause))
    segments = [(left + len(clause[:m.start()].encode()), m[0].strip()) for m in matches]
    position = next((i for i, (start, quote) in enumerate(segments)
                     if start <= anchor['start'] < start + len(quote.encode())), None)
    if position is None:
        return scopes
    guards = []
    topic = None
    scoped_coordination = (contract >= 7 and _COORDINATED_PREFIX.match(segments[position][1])
                            and _segment_guard(segments[position][1], contract=contract)[0])
    for i, (start, quote) in enumerate(segments[:position + 1]):
        if contract >= 7 and i < position and _TOPIC.fullmatch(quote):
            topic = span(start, quote, 'topic')
            continue
        candidate, shift = _segment_guard(quote, contract=contract)
        if not candidate or contract >= 8 and i != position and _ADMISSION_GUARD.fullmatch(candidate):
            continue
        start += len(quote[:shift].encode())
        if any(entry['start'] == start and entry['quote'] == candidate for entry in scopes):
            continue
        standalone = candidate == quote[shift:]
        previous_comparison = (standalone and candidate.startswith('仅在') and i + 1 < position
                               and _COMPARISON.search(segments[i+1][1]))
        shared_region = (standalone and candidate.endswith(('条件下', '情况下')) and i + 1 < position
                         and not _segment_guard(segments[i+1][1], contract=contract)[0]
                         and any(_segment_guard(text, contract=contract)[0] for _, text in segments[i+2:position+1]))
        same_effect = (standalone and i + 1 < position and _CAUSAL_EFFECT.match(segments[position][1])
                       and not any(_segment_guard(text, contract=contract)[0] for _, text in segments[i+1:position+1]))
        linked_effect = (contract >= 7 and i == position - 1 and not standalone
                         and _COORDINATED_EFFECT.match(segments[position][1])
                         and not _segment_guard(segments[position][1], contract=contract)[0])
        if previous_comparison or shared_region:
            scopes.append(span(start, candidate, 'upstream'))
        elif (i == position or standalone and (i == position - 1 or same_effect) or linked_effect) and not (
                i < position and scoped_coordination):
            guards.append(span(start, candidate, 'local'))
    if contract >= 7 and not topic and guards:
        if earlier := _preceding_alternative_topic(blob, claim_start, left, clause):
            topic = span(*earlier, 'topic')
    if topic:
        action = segments[position][1]
        if position and guards and guards[-1]['quote'] == segments[position - 1][1]:
            action = guards[-1]['quote'] + action
        if contract >= 8 and _INDEPENDENT_RECOVERY.match(action):
            topic['scope'] = 'excluded_topic'
        scopes.append(topic)
    scopes.extend(guards)
    previous = _preceding_calculation(blob, claim_start, left, clause)
    if previous:
        start, quote = previous
        scopes.append(span(start, quote, 'calculation'))
    return scopes


def _preceding_calculation(blob, claim_start, clause_start, clause):
    prefix = blob[claim_start:clause_start].decode()
    if not prefix.endswith(('；', ';')) or re.match(r'^(?:若|当|在|仅在|否则)', clause.lstrip()):
        return None
    previous = re.split(r'[。；;\n：:]', prefix[:-1])[-1]
    gate = re.match(r'^(仅在[^，；]{2,420}?时)[，,]', previous)
    declared = re.findall(r'(?:计算|以|按)([A-Za-z][A-Za-z0-9_]*)=', previous)
    if gate and any(re.search(r'(?<![A-Za-z0-9_])' + re.escape(name) + r'(?![A-Za-z0-9_])', clause)
                    for name in declared):
        return clause_start - len((previous + prefix[-1]).encode()), gate[1]
    return None


def _scope_for(term, scopes):
    return next((s for s in scopes if s['claim'] == term['claim']
                 and s['start'] <= term['start'] < term['end'] <= s['end']), None)


def _source_literal(span):
    leaf = dict(span)
    leaf.pop('scope', None)
    return {'op': 'literal', 'source': leaf}


def _adjacent_necessary_guards(first, second, blob):
    return (first['scope'] == second['scope'] == 'local' and first['quote'].endswith('时')
            and '或' in first['quote'] and second['quote'].startswith(('仅在', '只有'))
            and second['quote'].endswith(('条件下', '情况下'))
            and blob[first['end']:second['start']].decode().strip() in ('，', ','))


def _regional_subject_scope(blob, ranges, subject, *, contract):
    left, right = source_clause(blob, ranges, subject)
    before = re.split(r'[，,:：]', blob[left:subject['start']].decode())[-1]
    after = re.split(r'[，,:：]', blob[subject['end']:right].decode(), maxsplit=1)[0]
    guard, shift = _segment_guard(before + subject['quote'] + after, contract=contract)
    if _REGION_GUARD.fullmatch(guard):
        return subject['start'] - len(before.encode()) + len(before[:shift].encode())
    return None


def _scoped_conjunction(node, find, scopes, blob, *, contract=5):
    if not isinstance(node, dict) or node.get('op') != 'all':
        return None
    items = node.get('items')
    if not isinstance(items, list) or not 2 <= len(items) <= 12:
        return None
    if not all(isinstance(item, dict) and (item.get('op') == 'literal' and item.get('items', []) == []
               or contract >= 7 and item.get('op') in ('all', 'any')) for item in items):
        return None
    source = find(node.get('source'), 'condition.expression')
    if not source:
        return None
    terms = []
    covered = []
    expressions = []
    for index, item in enumerate(items):
        role = f'condition.expression.{index}'
        term = find(item.get('source'), role)
        scope = _scope_for(term, scopes) if term else None
        if (not scope or scope in covered or source['claim'] != term['claim']
                or not source['start'] <= term['start'] < term['end'] <= source['end']):
            return None
        if expressions and expressions[-1]['source']['end'] > term['start']:
            return None
        # A compound source predicate must remain intact.
        if term['quote'] not in (scope['quote'], re.sub(r'^(仅在|仅当|在|当)', '', scope['quote']).removesuffix('时')):
            return None
        if item['op'] == 'literal':
            part = {**term, 'polarity': 'as_written', 'scope': scope['scope']}
            terms.append(part)
            expressions.append(_source_literal(part))
        elif scope['scope'] == 'local':
            errors = []
            leaves = []
            expression = _expression(item, role, {term['claim']}, find, lambda *error: errors.append(error),
                                     blob, leaves, contract=contract)
            if errors or not expression:
                return None
            expressions.append(expression)
            terms.extend({**leaf, 'scope': scope['scope']} for leaf in leaves)
        else:
            return None
        covered.append(scope)
    kinds = {entry['scope'] for entry in covered}
    broad = {'upstream', 'topic'} if contract >= 7 else {'upstream'}
    adjacent_guards = False
    if contract >= 8 and len(covered) == 2 and kinds == {'local'}:
        first, second = covered
        adjacent_guards = (source['start'] == first['start'] and source['end'] == second['end']
                           and _adjacent_necessary_guards(first, second, blob))
    if 'local' not in kinds or not (broad & kinds or adjacent_guards):
        return None
    return {'op': 'all', 'source': source, 'items': expressions}, terms



def _expression(node, role, scope, find, add, blob, terms, depth=0, *, contract=4, candidate_tail=False):
    if depth > 6 or not isinstance(node, dict):
        add('condition_unverified', role)
        return None
    op, children = node.get('op'), node.get('items', [])
    source = find(node.get('source'), role)
    if not source:
        return None
    if source['claim'] not in scope:
        add('cross_branch', role)
    if op == 'literal' and children == []:
        terms.append({**source, 'polarity': 'as_written'})
        return {'op': op, 'source': source}
    if (op not in ('not', 'all', 'any', 'exception') or not isinstance(children, list)
            or not (1 <= len(children) <= 12)
            or (len(children) != 1 if op == 'not' else len(children) < 2)):
        add('condition_unverified', role)
        return None
    items = [_expression(child, f'{role}.{index}', scope, find, add, blob, terms, depth + 1, contract=contract)
             for index, child in enumerate(children)]
    if any(item is None for item in items):
        return None
    scopes = [item['source'] for item in items]
    if any(span['claim'] != source['claim'] or not source['start'] <= span['start'] < span['end'] <= source['end']
           for span in scopes):
        add('condition_unverified', role)
        return None
    head = blob[source['start']:scopes[0]['start']].decode().strip(' ，、（(')
    tail = blob[scopes[-1]['end']:source['end']].decode().strip(' ，、。）)')
    admission = contract >= 7 and op == 'all' and head == '仅接纳' and tail == '的候选'
    nominal = candidate_tail and op == 'any' and not head and tail in ('候选', '的候选')
    if not admission and not nominal and tail not in ('', '时', '的条件下', '条件下', '后'):
        add('condition_operator_mismatch', role)
    if op == 'not':
        term = scopes[0]
        left = blob[source['start']:term['start']].decode().strip()
        if left not in ('不', '未', '无', '非', '没有', '不存在', '未发生'):
            add('condition_polarity_mismatch', role)
    else:
        permitted_head = ('', '当', '在', '仅在', '仅当', '若', '如果')
        if contract >= 7:
            if op == 'all':
                permitted_head += ('在具有',)
            elif op == 'any':
                permitted_head += ('发现',)
        if not admission and head not in permitted_head and not (op == 'exception' and head == '除非'):
            add('condition_operator_mismatch', role)
        adjacent = zip(scopes, scopes[1:])
        separators = []
        for previous, following in adjacent:
            if previous['end'] >= following['start']:
                add('condition_operator_mismatch', role)
            separators.append(blob[previous['end']:following['start']].decode().strip(' ，、'))
        connectors = {
            'all': {'和', '且', '并且', '及', '以及', '并', '同时'},
            'any': {'或', '或者', '或是'},
            'exception': {'除非', '除外', '否则', '例外'},
        }
        last_group = (contract >= 8 and admission and items[-1]['op'] == 'literal'
                      and re.search(r'且(?![^，、]*或)', scopes[-1]['quote'])
                      and all(blob[a['end']:b['start']].decode().strip() in ('、', '，')
                              for a, b in zip(scopes, scopes[1:])))
        if not last_group and not any(separator in connectors[op] for separator in separators):
            add('condition_operator_mismatch', role)
        cross = (set().union(*connectors.values()) - connectors[op])
        if any(separator in cross for separator in separators):
            add('condition_operator_mismatch', role)
        if contract >= 7 and any(separator and separator not in connectors[op] for separator in separators):
            add('condition_operator_mismatch', role)
    return {'op': op, 'source': source, 'items': items}


def _admission_local_tree(node, scopes, blob, ranges):
    if not isinstance(node, dict) or node.get('op') != 'all':
        return node
    children = node.get('items')
    if not isinstance(children, list) or len(children) < 3:
        return node
    source, _ = _find_span(blob, ranges, node.get('source'))
    if not source or not (source['quote'].startswith('仅接纳') and source['quote'].endswith('的候选')):
        return node
    first = children[0]
    if not isinstance(first, dict) or first.get('op') != 'literal' or first.get('items', []) != []:
        return node
    span, _ = _find_span(blob, ranges, first.get('source'))
    scope = _scope_for(span, scopes) if span else None
    if (scope and scope['scope'] == 'preamble' and span['quote'] == scope['quote']
            and span['claim'] == source['claim'] and span['end'] <= source['start']):
        return {**node, 'items': children[1:]}
    return node


def _source_region_condition(node, scopes, blob, ranges, anchor, relation, *, allow_action_leaf=False):
    if not isinstance(node, dict) or not anchor or not relation or anchor['claim'] != relation['claim']:
        return None
    compound = allow_action_leaf and node.get('op') == 'all'
    if compound:
        children = node.get('items')
        if not isinstance(children, list) or len(children) != 2 or not all(
                isinstance(child, dict) and child.get('op') == 'literal' and not child.get('items') for child in children):
            return None
        prior, member = children
        span, _ = _find_span(blob, ranges, prior.get('source'))
        branch, _ = _find_span(blob, ranges, member.get('source'))
        whole, _ = _find_span(blob, ranges, node.get('source'))
        left, right = source_clause(blob, ranges, relation)
        if not (span and branch and whole and whole['claim'] == branch['claim'] == span['claim'] == relation['claim']
                and left <= whole['start'] == span['start'] < span['end'] <= branch['start']
                and branch['start'] == relation['start'] and branch['end'] == relation['end'] <= whole['end'] <= right):
            return None
    else:
        if node.get('op') != 'literal' or node.get('items', []) != []:
            return None
        span, _ = _find_span(blob, ranges, node.get('source'))
    shared = _scope_for(span, scopes) if span else None
    locals_ = [entry for entry in scopes if entry['scope'] == 'local']
    if not (shared and shared['scope'] == 'upstream' and span['quote'] == shared['quote'] and len(locals_) == 1):
        return None
    local, = locals_
    start = local['end'] if compound and anchor['start'] == local['start'] else anchor['start']
    if (not _REGION_GUARD.fullmatch(local['quote'])
            or local['claim'] != relation['claim']
            or not relation['start'] <= local['start'] < local['end'] <= start < relation['end']
            or re.search(r'[，,；;。]', blob[local['end']:start].decode())):
        return None
    proof = {**local, 'polarity': 'as_written'}
    return {'state': 'stated', 'operator': 'literal', 'terms': [proof],
            'expression': _source_literal(local), 'origin': 'source_region'}


def _coordinated_guard_action(row, blob, ranges, relation):
    if not relation or row.get('direction') not in {'directed', 'ordered'}:
        return None
    kind, target = row.get('kind'), row.get('object')
    if not all(isinstance(part, str) and part for part in (kind, target)):
        return None
    quote = relation['quote']
    alternatives, comma, rest = quote.partition('，')
    gate = _GUARD_PREFIX.match(rest)
    if not (comma and alternatives.endswith('时') and gate and gate[0].startswith(('仅在', '只有'))):
        return None
    if CLAUSE_BREAK.search(quote) or re.search(r'[，,：:]', rest):
        return None
    prefix = alternatives + comma + gate[0]
    actions = re.split('并(?:且)?', rest[gate.end():])
    action = kind + target
    if len(actions) < 2 or any(not part for part in actions) or actions.count(action) != 1:
        return None
    span, _ = _find_span(blob, ranges, {'claim': relation['claim'], 'quote': action, 'context': quote})
    guard_end = relation['start'] + len(prefix.encode())
    if span and guard_end <= span['start']:
        return span, guard_end
    return None


def _configured_action_owner(source, ranges, subject, label, action):
    """An explicitly configured device can own a later locally guarded action."""
    from .claims_scope_recovery import _direct_preamble

    if not subject or subject['claim'] != action['claim'] or not isinstance(label, str) or not label:
        return False
    head = _direct_preamble(source, ranges, action['claim'])
    if not head or not head['start'] <= subject['start'] < subject['end'] <= head['end'] < action['start']:
        return False
    declaration = re.search(r'(?:^|，)(所述' + re.escape(label)
                            + r'在[^，；。]+(?:条件下|情况下))，被配置为$', head['quote'])
    if not declaration:
        return False
    role = '所述' + label
    declared_at = head['start'] + len(head['quote'][:declaration.start(1)].encode())
    return (declaration[1] in subject['quote'] or
            subject['quote'] == role and subject['start'] == declared_at)


def _split_alternative_guard(row, node, scope, scopes, blob, ranges, anchor, relation, coordinated=None, configured_owner=False):
    """Rebuild a shared guard only when the original tree proves one exact member."""
    if not (isinstance(node, dict) and anchor and relation):
        return None
    local = [item for item in scopes if item['scope'] == 'local']
    if len(local) != 2 or not _adjacent_necessary_guards(*local, blob):
        return None
    alternatives, gate = local
    whole, _ = _find_span(blob, ranges, node.get('source'))
    if not (whole and whole['claim'] == relation['claim'] == anchor['claim']
            and whole['claim'] in scope and relation['start'] <= whole['start'] == alternatives['start']):
        return None
    whole_source = node['source']
    trigger = None
    if node.get('op') == 'literal' and not node.get('items'):
        if not (coordinated and configured_owner and whole['end'] == gate['end'] == coordinated[1]):
            return None
        gate_source = {'op': 'literal', 'source': {**whole_source, 'quote': gate['quote'], 'context': whole['quote']}}
    else:
        children = node.get('items')
        if node.get('op') != 'all' or not isinstance(children, list) or len(children) != 2 or not all(
                isinstance(item, dict) and item.get('op') == 'literal' and item.get('items', []) == []
                for item in children):
            return None
        trigger, guard = (_find_span(blob, ranges, item.get('source'))[0] for item in children)
        if not (trigger and guard and whole['claim'] == trigger['claim'] == guard['claim']
                and (row.get('subject') == trigger['quote'] or configured_owner)):
            return None
        gate_source = children[1]
        if coordinated and whole == relation and coordinated[1] == gate['end']:
            if (trigger['start'] != alternatives['start']
                    or trigger['quote'] not in (alternatives['quote'], alternatives['quote'].removesuffix('时'))
                    or not gate['start'] <= guard['start'] < guard['end'] <= gate['end']
                    or guard['quote'] not in {gate['quote'], re.sub(r'^(仅在|只有)', '', gate['quote'])
                                              .removesuffix('条件下').removesuffix('情况下')}):
                return None
            base = {'claim': whole['claim'], 'context': whole['quote']}
            whole_source = {**base, 'quote': blob[whole['start']:gate['end']].decode()}
            gate_source = {'op': 'literal', 'source': {**base, 'quote': gate['quote']}}
        elif (whole['end'] != gate['end'] or gate['end'] != anchor['start']
              or anchor['end'] > relation['end'] or (guard['start'], guard['end']) != (gate['start'], gate['end'])):
            return None
    # The source must be a plain enumeration ending with one explicit OR.
    enumeration = re.fullmatch(r'([^、或且，；。]+(?:、[^、或且，；。]+)+)(?:或者|或)([^、或且，；。]+)时',
                               alternatives['quote'])
    if not enumeration:
        return None
    predicates = enumeration[1].split('、') + [enumeration[2]]
    if re.match(r'^(?:不在|不包括|排除|除非|除了|除去|并非|不是|除.*之外|非以下)', predicates[0]):
        return None
    if any(EXCEPTION.search(predicate) for predicate in predicates):
        return None
    if len(predicates) != len(set(predicates)) or len(predicates) > 12:
        return None
    base = {'claim': whole['claim'], 'context': alternatives['quote']}
    offsets = set()
    for quote in predicates:
        leaf, _ = _find_span(blob, ranges, {**base, 'quote': quote})
        if not leaf:
            return None
        offsets.add((leaf['start'], leaf['end']))
    if trigger and whole_source is node['source'] and (trigger['start'], trigger['end']) not in offsets:
        return None
    return {'op': 'all', 'source': whole_source, 'items': [
        {'op': 'any', 'source': {'claim': whole['claim'], 'quote': alternatives['quote']},
         'items': [{'op': 'literal', 'source': {**base, 'quote': q}} for q in predicates]},
        gate_source,
    ]}


def _condition(row, scope, find, add, context, blob, *, contract=3, source_scopes=(), ranges=None, anchor=None,
               relation=None, coordinated=None, configured_owner=False):
    cond = row.get('condition')
    if not isinstance(cond, dict):
        add('condition_unverified', 'condition')
        return None
    state, operator, terms = cond.get('state'), cond.get('operator'), cond.get('terms')
    node = cond.get('expression')
    if contract >= 8 and state == 'stated':
        if region := _source_region_condition(node, source_scopes, blob, ranges, anchor, relation,
                                              allow_action_leaf=contract == RELATION_CONTRACT):
            return region
        node = _admission_local_tree(node, source_scopes, blob, ranges)
    if contract >= 5:
        transferable = {'preamble', 'calculation'}
        if contract >= 7 and context and not (CONDITIONAL.search(context[0]) or context[1]):
            transferable.add('topic')
        if (state == 'none_in_source' and operator == 'none' and terms == []
                and len(source_scopes) == 1 and source_scopes[0]['scope'] in transferable):
            entry = source_scopes[0]
            term = {**entry, 'polarity': 'as_written'}
            if term['scope'] == 'calculation':
                term['scope'] = 'upstream'
            return {'state': 'stated', 'operator': 'literal',
                    'terms': [term], 'expression': _source_literal(entry),
                    'origin': 'source_' + entry['scope']}
        if state == 'stated':
            rebuilt = (_split_alternative_guard(row, node, scope, source_scopes, blob, ranges, anchor, relation,
                                               coordinated, configured_owner)
                       if contract >= 10 else None)
            if rebuilt:
                node = rebuilt
            scoped = _scoped_conjunction(node, find, source_scopes, blob, contract=contract)
            if scoped:
                expression, found = scoped
                return {'state': state, 'operator': 'all', 'terms': found,
                        'expression': expression, 'origin': 'source_alternative' if rebuilt else 'source_scopes'}
    if contract >= 4 and 'expression' in cond:
        found = []
        if state != 'stated':
            add('condition_unverified', 'condition')
        candidate_tail = False
        if contract >= 10 and relation and anchor and isinstance(node, dict):
            nominal, _ = _find_span(blob, ranges, node.get('source'))
            candidate_tail = bool(nominal and nominal['claim'] == anchor['claim'] == relation['claim']
                                  and relation['start'] <= nominal['start'] < nominal['end'] == anchor['start']
                                  and anchor['end'] <= relation['end']
                                  and nominal['quote'] == row.get('subject')
                                  and not CLAUSE_BREAK.search(relation['quote']))
        expression = _expression(node, 'condition.expression', scope, find, add, blob, found,
                                 contract=contract, candidate_tail=candidate_tail)
        return {'state': state, 'operator': (expression or {}).get('op'), 'terms': found, 'expression': expression}
    sources = []
    if isinstance(terms, list):
        for i, term in enumerate(terms):
            role = f'condition.{i}'
            span = find(term, role)
            if not span:
                continue
            quote = span['quote']
            polarity = term.get('polarity')
            if polarity not in ('positive', 'negative', 'exception'):
                add('condition_unverified', role)
            elif contract == 3 and ((polarity == 'negative' and not NEGATIVE.search(quote))
                  or (polarity == 'exception' and not EXCEPTION.search(quote))
                  or (polarity == 'positive' and (NEGATIVE.search(quote) or EXCEPTION.search(quote)))):
                add('condition_polarity_mismatch', role)
            if span['claim'] not in scope:
                add('cross_branch', role)
            sources.append({**span, 'polarity': polarity if contract == 3 else 'as_written'})
    if state == 'none_in_source':
        admission_required = contract >= 8 and any(s['scope']=='local' and _ADMISSION_GUARD.fullmatch(s['quote'])
                                                    for s in source_scopes)
        if (terms != [] or operator != 'none' or admission_required
                or context and (CONDITIONAL.search(context[0]) or context[1])):
            add('condition_unverified', 'condition')
    elif state == 'stated':
        if (not sources or not isinstance(terms, list) or len(sources) != len(terms)
                or not ((operator == 'single' and len(sources) == 1)
                        or (operator in ('all', 'any', 'exception') and len(sources) >= 2))):
            add('condition_unverified', 'condition')
        if operator in ('all', 'any'):
            ordered = sorted(sources, key=lambda ref: ref['start'])
            for before, after in zip(ordered, ordered[1:]):
                if before['claim'] != after['claim'] or before['end'] > after['start']:
                    continue
                separator = blob[before['end']:after['start']].decode().strip(' ，、')
                conflicting = ('或', '或者') if operator == 'all' else ('且', '并且', '以及')
                if separator in conflicting:
                    add('condition_operator_mismatch', 'condition')
    else:
        add('condition_unverified', 'condition')
    return {'state': state, 'operator': operator, 'terms': sources,
            'description': cond.get('description', '') if isinstance(cond.get('description'), str) else ''}


def _view_support(view, relation):
    return any(
        not feature['location_ambiguous'] and feature['claim'] == relation['claim']
        and feature['start'] <= relation['start'] and relation['end'] <= feature['end']
        for feature in view['features']
    )


def _message_endpoints_from_claim(source, ranges, evidence, labels, *, contract=7, role_context=None):
    if len(labels) != 2 or not all(isinstance(v, str) for v in labels):
        return False
    tx, rx = r'发射(?:侧|端|设备)', r'接收(?:侧|端|设备)'
    roles = tuple('tx' if re.fullmatch(tx, value) else 'rx' if re.fullmatch(rx, value) else ''
                  for value in labels)
    if roles not in {('tx', 'rx'), ('rx', 'tx')} or not {'subject', 'object', 'relation'} <= set(evidence):
        return False
    relation = evidence['relation']
    claim = relation['claim']
    start, end = ranges[claim]
    claim_text = source[start:end].decode()
    header = re.split(r'[，：。；]', claim_text, maxsplit=1)[0]
    preamble = _PREAMBLE.search(claim_text)
    if contract >= 8 and not preamble:
        preamble = _ALTERNATE_PREAMBLE.search(claim_text)
    if not preamble or not re.search(tx, header) or not re.search(rx, preamble['guard']):
        return False
    first = start + len(claim_text[:preamble.start('guard')].encode())
    last = start + len(claim_text[:preamble.end('guard')].encode())
    if role_context and not first <= role_context['start'] < role_context['end'] <= last:
        return False
    if not all(anchor['claim'] == claim and first <= anchor['start'] < anchor['end'] <= last
               for anchor in (evidence['subject'], evidence['object'])):
        return False
    action = relation['quote']
    if roles == ('rx', 'tx'):
        return bool(re.search(r'接收经[^，；]{1,16}返回[^；。]*反馈', action))
    return bool(re.search(r'配对发送参考|向(?:所述)?' + rx + r'发送[^，；]{0,40}提示', action))


def _stated_message_pair(action):
    actor = r'(?:发射|接收)(?:侧|端|设备)'
    def side(value):
        return 'tx' if value.startswith('发射') else 'rx'
    explicit = re.search(r'(?P<source>'+actor+r')[^，。；]{0,20}?向(?:所述)?(?P<target>'+actor+r')'
                         r'[^，。；]{0,24}?(?:发送|返回|回传)', action)
    if explicit:
        return side(explicit['source']), side(explicit['target'])
    recipient = re.search(r'(?:向(?:所述)?(?P<target>'+actor+r')[^，。；]{0,24}?发送|'
                          r'(?P<receiver>'+actor+r')接收经[^，。；]{1,16}返回)', action)
    if recipient:
        target = side(recipient['target'] or recipient['receiver'])
        return ('rx' if target=='tx' else 'tx'), target
    active = re.search(r'(?P<source>'+actor+r')[^，。；]{0,24}?(?:发送|返回|回传)', action)
    if active and len(re.findall(actor,action)) == 1:
        source = side(active['source'])
        return source, ('rx' if source=='tx' else 'tx')
    return None


def _rebind_message_actors(source, ranges, evidence, labels, direction):
    relation = evidence.get('relation')
    if not relation or direction != 'directed' or not all(isinstance(s,str) for s in labels):
        return None
    if not all(key in evidence for key in ('subject','object')):
        return None
    claim = relation['claim']
    if any(evidence[key]['claim'] != claim or label not in evidence[key]['quote']
           for key,label in zip(('subject','object'),labels)):
        return None
    start,end = ranges[claim]
    text = source[start:end].decode()
    preamble = _PREAMBLE.search(text) or _ALTERNATE_PREAMBLE.search(text)
    if not preamble:
        return None
    wanted = tuple('tx' if label.startswith('发射') else 'rx' for label in labels)
    for statement in re.split(r'[，,。；]', relation['quote']):
        pair = _stated_message_pair(statement)
        if pair and pair != wanted:
            return None
    updated = dict(evidence)
    original = {}
    for key,label in zip(('subject','object'),labels):
        if not re.fullmatch(r'(?:发射|接收)(?:侧|端|设备)', label) or preamble['guard'].count(label) != 1:
            return None
        offset = start + len(text[:preamble.start('guard')].encode())
        offset += len(preamble['guard'][:preamble['guard'].index(label)].encode())
        original[key] = evidence[key]
        updated[key] = {'claim':claim,'quote':label,'start':offset,'end':offset+len(label.encode()),
                        'context_start':offset,'context_end':offset+len(label.encode())}
    if not _message_endpoints_from_claim(source,ranges,updated,labels,contract=9):
        return None
    return updated, original


def _message_role_without_context(source, ranges, spec, role, relation, labels, direction):
    """Resolve a bare role against the exact same-claim message preamble."""
    if (not relation or direction != 'directed' or not isinstance(spec, dict)
            or spec.get('claim') != relation['claim'] or 'context' in spec
            or not all(isinstance(label, str) and re.fullmatch(r'(?:发射|接收)(?:侧|端|设备)', label)
                       for label in labels)
            or spec.get('quote') != labels[ROLES.index(role)]):
        return None
    from .claims_scope_recovery import _direct_preamble

    claim = relation['claim']
    declaration = _direct_preamble(source, ranges, claim)
    if not declaration:
        return None
    endpoints = {}
    for name, label in zip(ROLES, labels):
        anchor, _ = _find_span(source, ranges, {'claim': claim, 'quote': label, 'context': declaration['quote']})
        if not anchor:
            return None
        endpoints[name] = anchor
    trial = {**endpoints, 'relation': relation}
    if not _message_endpoints_from_claim(source, ranges, trial, labels, contract=RELATION_CONTRACT):
        return None
    left, right = source_clause(source, ranges, relation)
    before = re.split(r'[，,：:]', source[left:relation['start']].decode())[-1]
    after = re.split(r'[，,：:]', source[relation['end']:right].decode(), maxsplit=1)[0]
    action = before + relation['quote'] + after
    if re.search(r'(?:不得|不再|不能|禁止|未(?:曾)?|不)(?:按配对|经[^，；]{1,16})?'
                 r'(?:发送|接收|返回|回传)', action):
        return None
    explicit = _stated_message_pair(action)
    expected = tuple('tx' if label.startswith('发射') else 'rx' for label in labels)
    return endpoints[role] if not explicit or explicit == expected else None


def _dependent_method_actor(source, ranges, parents, path, evidence, labels, direction):
    """A directly dependent method keeps the only actor declared by its title."""
    subject, target, action = (evidence.get(role) for role in ROLES)
    if (direction != 'directed' or len(path or []) != 2 or not all((subject, target, action))
            or action['claim'] != path[0] or parents[path[0]] != [path[1]]
            or subject['claim'] != path[1] or target['claim'] != path[0]
            or not isinstance(labels[0], str) or labels[0] not in subject['quote']
            or not isinstance(labels[1], str) or not labels[1]
            or not re.fullmatch(r'(?:发射|接收)(?:侧|端|设备)', labels[0])):
        return False
    parent_start = ranges[path[1]][0]
    parent_title = re.split(r'[，,：:；。]', source[parent_start:ranges[path[1]][1]].decode(), maxsplit=1)[0]
    if (not parent_title.endswith('方法') or parent_title.count(labels[0]) != 1
            or len(re.findall(r'(?:发射|接收)(?:侧|端|设备)', parent_title)) != 1
            or not parent_start <= subject['start'] < subject['end'] <= parent_start + len(parent_title.encode())):
        return False
    own_start = ranges[path[0]][0]
    before = source[own_start:action['start']].decode()
    if not re.match(r'^[1-9][0-9]*[.．、]\s*(?:根据|如)权利要求\s*' + str(path[1]) + r'所述的(?:[^，,]{0,25})方法[，,]', before):
        return False
    left, right = source_clause(source, ranges, action)
    if not left <= target['start'] < target['end'] <= right:
        return False
    effect = re.fullmatch(r'[^，,。；：:]{2,100}?(?:则|时)[，,]?(?P<effect>[^，,。；：:]{2,100})', action['quote'])
    if not effect or labels[1] not in effect['effect']:
        return False
    surrounding = re.split(r'[，,：:]', source[left:action['start']].decode())[-1]
    surrounding += action['quote'] + re.split(r'[，,：:]', source[action['end']:right].decode(), maxsplit=1)[0]
    return surrounding.strip() == action['quote'] and not re.search(
        r'(?:发射|接收)(?:侧|端|设备)|(?:由|令|使)[^，,。；]{1,30}(?:执行|拒绝|接收|发送)', surrounding)


def _reanchor_message_action(source, ranges, specs, claim, labels, direction, figures, views):
    spec = specs.get('relation')
    if not isinstance(spec, dict) or spec.get('claim') != claim or direction != 'directed':
        return None
    quote, context = spec.get('quote'), spec.get('context')
    if not isinstance(quote, str) or not isinstance(context, str) or not quote or quote in context:
        return None
    original, _ = _find_span(source, ranges, {'claim': claim, 'quote': context})
    action, _ = _find_span(source, ranges, {'claim': claim, 'quote': quote})
    if not original or not action or original['end'] > action['start'] or not figures:
        return None
    if not all(_view_support(views[n], action) for n in figures):
        return None
    evidence = {'relation': action}
    for role, label in zip(ROLES[:2], labels):
        endpoint, _ = _find_span(source, ranges, specs.get(role))
        if not (endpoint and isinstance(label, str) and endpoint['quote'] == label
                and endpoint['claim'] == claim
                and original['start'] <= endpoint['start'] < endpoint['end'] <= original['end']):
            return None
        evidence[role] = endpoint
    reference = re.search(r'配对发送参考', quote)
    feedback = re.search(r'接收经[^，；]{1,16}返回[^；。]*有效性反馈', quote)
    if bool(reference) == bool(feedback) or not _message_endpoints_from_claim(
        source, ranges, evidence, labels, contract=10, role_context=original
    ):
        return None
    wanted = ('tx', 'rx') if reference else ('rx', 'tx')
    actual = tuple('tx' if label.startswith('发射') else 'rx' for label in labels)
    left, right = source_clause(source, ranges, action)
    before = re.split(r'[，,：:]', source[left:action['start']].decode())[-1]
    after = re.split(r'[，,：:]', source[action['end']:right].decode(), maxsplit=1)[0]
    statement = before + quote + after
    if re.search(r'(?:不得|不再|不能|禁止|未(?:曾)?|不)(?:按配对|经[^，；]{1,16})?'
                 r'(?:发送|接收|返回|回传)', statement):
        return None
    explicit = _stated_message_pair(statement)
    if actual != wanted or explicit and explicit != wanted:
        return None
    return action, original


def _staged_control_endpoints(source, ranges, evidence, labels, claim, direction):
    subject, target, action = (evidence.get(role) for role in ROLES)
    if (direction != 'directed' or not claim or not all(
            anchor and anchor['claim'] == claim for anchor in (subject, target, action))
            or not all(isinstance(label, str) and label for label in labels)
            or not (labels[0] in subject['quote'] and labels[1] in target['quote'])
            or not re.match(r'(?:对|针对)同一[^。；]{1,60}分阶段实施', subject['quote'])):
        return False
    left, right = source_clause(source, ranges, action)
    if not (subject['end'] < left <= target['start'] < target['end'] <= right):
        return False
    clause = source[left:right].decode().strip()
    intervening = source[subject['end']:left].decode()
    return (bool(re.match(r'(?:仅在|仅当|当|若|发现)', clause))
            and 1 <= intervening.count('；') <= 2
            and not re.search(r'。|另(?:行|一|对)|改为|转入|更换', intervening))


def _relation(row, index, response_sha, source, ranges, parents, view_numbers, views, contract, source_sha, held_numbers):
    data = row if isinstance(row, dict) else {}
    reasons = []
    evidence = {}

    def add(code, role):
        reasons.append({'code': code, 'field': role})

    def find(spec, role):
        span, error = _find_span(source, ranges, spec)
        if (contract >= 10 and role == 'relation' and error == 'citation_not_found'
                and data.get('direction') == 'directed'):
            correction = _reanchor_message_action(
                source, ranges, specs, claim, [data.get(k) for k in ROLES[:2]],
                data.get('direction'), figures, views)
            if correction:
                span, original = correction
                item['action_citation_rebinding'] = {
                    'original_context': original, 'rejected_reason': error,
                    'corrected': span, 'rule': 'same_claim_message_action',
                }
                error = None
        if contract >= 5 and role in ROLES[:2] and isinstance(spec, dict):
            scope = evidence.get('relation')
            quote = spec.get('quote')
            if scope and isinstance(quote, str) and spec.get('claim') == scope['claim']:
                if error == 'citation_ambiguous' and spec.get('context', quote) == quote:
                    span, error = _find_span(source, ranges, {**spec, 'context': scope['quote']})
                if span and quote in scope['quote']:
                    left, right = source_clause(source, ranges, scope)
                    if not left <= span['start'] < span['end'] <= right:
                        add('endpoint_unverified', role)
        if error and contract == RELATION_CONTRACT and role in ROLES[:2]:
            recovered = _message_role_without_context(
                source, ranges, spec, role, evidence.get('relation'), [data.get(k) for k in ROLES[:2]],
                data.get('direction'))
            if recovered:
                item.setdefault('endpoint_citation_rebindings', {})[role] = {
                    'rejected_reason': error, 'corrected': recovered, 'rule': 'same_claim_message_preamble',
                }
                span, error = recovered, None
        if error:
            add(error, role)
        else:
            evidence[role] = span
        return span

    claim, model_required = data.get('claim'), data.get('required')
    if type(claim) is not int or claim not in ranges:
        add('invalid_claim', 'claim')
        claim = None
    if type(model_required) is not bool:
        add('required_unknown', 'required')
        model_required = None
    raw_views = data.get('views')
    valid_input = isinstance(raw_views, list) and all(
        type(i) is int and (i in view_numbers or i in held_numbers) for i in raw_views)
    figures = list(dict.fromkeys(view_numbers[i] for i in raw_views if type(i) is int and i in view_numbers)) if isinstance(raw_views, list) else []
    pending = list(dict.fromkeys(f'fig{i}' for i in raw_views if type(i) is int and i in held_numbers)) if isinstance(raw_views, list) else []
    if not valid_input or not figures and not pending:
        add('invalid_view', 'views')
    elif not figures:
        add('figure_citation_limit', 'views')
    item = {'candidate_id': 'rel-' + digest([response_sha, index, row])[:24], 'source_index': index,
            'response_sha256': response_sha, 'raw': row, 'claim': claim, 'model_required': model_required,
            'view_ids': [f'fig{i}' for i in figures], 'evidence': evidence, 'reasons': reasons,
            'status': 'rejected', 'necessity_status': 'unreviewed'}
    if pending:
        item['pending_view_ids'] = pending
    if 'evidence' not in data and 'direction' not in data and isinstance(data.get('condition'), str):
        span = find({'claim': data.get('claim'), 'quote': data.get('quote')}, 'relation')
        if span and data['condition'] and data['condition'] in span['quote']:
            find({'claim': claim, 'quote': data['condition'], 'context': span['quote']}, 'condition.0')
        add('legacy_evidence_incomplete', 'evidence')
        return item, None

    path = _path(data, claim, parents)
    if not path:
        add('cross_branch', 'claim_path')
    specs = data.get('evidence')
    if not isinstance(specs, dict):
        specs = {}
    for role in (('relation', 'subject', 'object') if contract >= 5 else ROLES):
        span = find(specs.get(role), role)
        if span and span['claim'] not in path:
            add('cross_branch', role)
    relation = evidence.get('relation')
    direction, kind = data.get('direction'), data.get('kind')
    if (not isinstance(direction, str) or direction not in DIRECTIONS
            or not isinstance(kind, str) or not kind.strip()):
        add('direction_unverified', 'direction')
    labels = [data.get(role) for role in ROLES[:2]]
    for role, label in zip(ROLES[:2], labels):
        if not isinstance(label, str) or not label or label not in evidence.get(role, {}).get('quote', ''):
            add('endpoint_unverified', role)
    correction = (_rebind_message_actors(source,ranges,evidence,labels,direction)
                  if contract >= 9 and relation else None)
    if correction:
        rebound, original = correction
        item['evidence_rebindings'] = {'original':original,'rule':'same_claim_preamble_roles'}
        evidence.update(rebound)
    coordinated = _coordinated_guard_action(data, source, ranges, relation) if contract == RELATION_CONTRACT else None
    configured_owner = bool(coordinated and _configured_action_owner(
        source, ranges, evidence.get('subject'), labels[0], coordinated[0]))
    context = _relation_context(source, ranges, relation) if relation else None
    if context and not all(isinstance(label, str) and label and label in context[0] for label in labels):
        supported_message = (contract >= 7 and direction == 'directed' and isinstance(kind, str)
                             and (correction or re.search(r'参考|反馈|提示', kind)
                             and _message_endpoints_from_claim(source, ranges, evidence, labels, contract=contract)))
        staged = contract >= 17 and _staged_control_endpoints(source, ranges, evidence, labels, claim, direction)
        inherited_actor = contract == RELATION_CONTRACT and _dependent_method_actor(
            source, ranges, parents, path, evidence, labels, direction)
        if inherited_actor:
            item['evidence_rebindings'] = {'original': dict(evidence), 'rule': 'direct_dependent_method_actor'}
        elif staged:
            item['evidence_rebindings'] = {'original': dict(evidence), 'rule': 'same_claim_staged_control'}
        elif configured_owner:
            item['evidence_rebindings'] = {'original': dict(evidence), 'rule': 'configured_action_owner'}
        elif not supported_message:
            add('endpoint_unverified', 'relation')
    compound_message = (contract >= 4 and relation and isinstance(kind, str)
                        and '发送' in relation['quote'] and '返回' in relation['quote']
                        and ('参考' in kind or '反馈' in kind))
    if (relation and direction == 'directed' and not compound_message
            and all(isinstance(label, str) and label for label in labels)):
        subject, target = map(re.escape, labels)
        if re.search(target + r'向(?:所述)?' + subject + r'(?:发送|传输|输出|反馈|传递|提供)', relation['quote']):
            add('direction_mismatch', 'direction')
    anchor = evidence.get('object') or relation
    if contract >= 5 and relation and anchor and anchor['claim'] == relation['claim'] and anchor['end'] <= relation['start']:
        anchor = relation
    if coordinated:
        anchor = coordinated[0]
    scopes = _applicable_source_scopes(source, ranges, anchor, contract=contract) if contract >= 5 and anchor else ()
    if contract >= 7 and anchor and not scopes and context and not context[1]:
        context = _separate_later_action(source, ranges, anchor) or context
    subject_span = evidence.get('subject')
    if contract >= 8 and subject_span and anchor and subject_span['claim'] == anchor['claim']:
        region = next((entry for entry in scopes if entry['scope']=='local' and _REGION_GUARD.fullmatch(entry['quote'])), None)
        if region:
            subject_region = _regional_subject_scope(source, ranges, subject_span, contract=contract)
            if subject_region is not None and subject_region != region['start']:
                add('condition_scope_mismatch', 'subject')
    condition = _condition(data, path, find, add, context, source, contract=contract, source_scopes=scopes,
                           ranges=ranges, anchor=anchor, relation=relation, coordinated=coordinated,
                           configured_owner=configured_owner)
    if condition and condition.get('origin') == 'source_alternative':
        item['condition_rebinding'] = {
            'rule': 'shared_alternative_guard', 'rejected_reason': 'condition_operator_mismatch',
            'corrected': condition['expression'],
        }
    if (contract >= 4 and condition and isinstance(data.get('condition'), dict) and anchor
            and ('expression' in data['condition'] or contract >= 7 and condition['state'] == 'stated')):
        locals_ = [entry for entry in scopes if entry['scope'] == 'local']
        local = locals_[-1] if locals_ else None
        double_gate = contract >= 8 and len(locals_) > 1 and _adjacent_necessary_guards(*locals_[-2:], source)
        for index, term in enumerate(condition['terms']):
            if contract >= 8 and any(entry['scope'] == 'excluded_topic' and term['claim'] == entry['claim']
                                     and term['start'] < entry['end'] and entry['start'] < term['end']
                                     for entry in scopes):
                add('condition_scope_mismatch', f'condition.{index}')
            applicable = _scope_for(term, scopes)
            if not applicable and not _guard_scope(source, ranges, anchor, term):
                add('condition_scope_mismatch', f'condition.{index}')
            if (contract >= 5 and local and not applicable
                    and (term['end'] <= local['start'] or term['start'] >= anchor['end'])):
                add('condition_scope_mismatch', f'condition.{index}')
        if contract >= 5:
            for entry in scopes:
                shared = {'preamble', 'upstream'} | ({'topic'} if contract >= 7 else set())
                if entry['scope'] not in shared and not (
                        entry['scope'] == 'local' and (any(x['scope'] == 'upstream' for x in scopes)
                            or contract >= 7 and any(x['scope'] == 'topic' for x in scopes)
                            or contract >= 8 and (_ADMISSION_GUARD.fullmatch(entry['quote'])
                                                  or double_gate and entry in locals_[-2:])
                            or re.search(r'且|或者|或|以及', entry['quote']))):
                    continue
                expression_source = (condition.get('expression') or {}).get('source')
                proof = [*condition['terms'], *([expression_source] if expression_source else [])]
                bare = re.sub(r'^(仅在|仅当|在|当)', '', entry['quote']).removesuffix('时')
                if contract >= 8 and _ADMISSION_GUARD.fullmatch(entry['quote']):
                    bare = entry['quote'].removeprefix('仅接纳').removesuffix('的候选')
                variants = {bare}
                if contract >= 9 and _ADMISSION_GUARD.fullmatch(entry['quote']):
                    variants.update((entry['quote'].removeprefix('仅接纳'), entry['quote'].removesuffix('的候选')))
                if not any(t['claim'] == entry['claim'] and t['start'] <= entry['start'] and t['end'] >= entry['end']
                           or _scope_for(t, scopes) == entry and t['quote'] in variants
                           for t in proof):
                    has_local = local and any(t['claim'] == local['claim'] and t['start'] <= local['start']
                                              and t['end'] >= local['end'] for t in proof)
                    topic_predicate = False
                    if contract >= 9 and entry['scope'] == 'topic':
                        declared = re.fullmatch(r'对于(?P<guard>[^，；。]{2,100})的(?:修改操作|请求|候选|数据)', entry['quote'])
                        topic_predicate = bool(declared and any(t['claim']==entry['claim']
                            and entry['start'] <= t['start'] < t['end'] <= entry['end']
                            and t['quote']==declared['guard'] for t in condition['terms']))
                    if entry['scope'] == 'preamble' or topic_predicate or (contract >= 7 and entry['scope'] in shared and has_local):
                        prior = {**entry, 'polarity': 'as_written'}
                        condition.setdefault('applicability', []).append(prior)
                        condition['terms'].append(prior)
                    else:
                        add('condition_scope_mismatch', f'condition.{entry["scope"]}')
    instances = []
    if (contract >= 4 and relation and all(isinstance(label, str) and label for label in labels)
            and labels[0] == labels[1] and '隔离' in relation['quote']):
        root = path[-1] if path else claim
        if contract >= 5:
            remaining = set(path)
            while len(parents.get(root, ())) == 1:
                parent, = parents[root]
                if parent in remaining:
                    break
                remaining.add(parent)
                root = parent
        instances = _isolation_instances(source_sha, labels[0], relation, root, contract=contract)
        if not instances:
            add('endpoint_unverified', 'distinct_instances')
    if relation:
        for number in figures:
            if not _view_support(views[number], relation):
                add('view_evidence_missing', f'fig{number}')
    if reasons:
        return item, None

    item['status'] = 'accepted'
    instance_ids = [i['id'] for i in instances if 'owner_id' in i] or [part_id(label) for label in labels]
    return item, {
        'id': item['candidate_id'], 'claim': claim, 'claim_path': path,
        'subject': labels[0], 'subject_id': instance_ids[0],
        'object': labels[1], 'object_id': instance_ids[1],
        **({'instances': instances} if instances else {}),
        'kind': kind, 'direction': direction, 'condition': condition,
        **({'guarded_action': anchor} if coordinated and condition.get('origin') == 'source_alternative' else {}),
        'evidence': {role: evidence[role] for role in ROLES}, 'view_ids': item['view_ids'],
        'model_required': model_required, 'necessity_status': 'unreviewed',
        'semantic_review': 'pending',
    }


def _isolation_instances(source_sha, label, citation, owner_claim, *, contract=4):
    quote = citation['quote']
    match = re.search(r'(?:不同|各)([一-鿿A-Za-z]{1,16}?)的([^。；，,]{1,40})', quote)
    if not match or '隔离' not in quote:
        return []
    owner_type = match[1]
    prefix = quote[:match.start()]
    owner_scope = re.search(r'([一-鿿A-Za-z0-9]{1,24}(?:系统|平台|租户|终端))(?:将|把|对|分别将)\s*$', prefix)
    scope_id = f'claim:{owner_claim}'
    if owner_scope:
        scope_id = f'{scope_id}/{owner_scope[1]}' if contract >= 5 else owner_scope[1]
    # Two symbolic members express the source's different-owner constraint.
    # Their letters are display roles and do not state a cardinality.
    member_type = match[2].removesuffix('相互隔离').removesuffix('彼此隔离')
    if contract >= 5:
        member_type = member_type.removesuffix('隔离')
    if not member_type or '隔离' in member_type or not (label in member_type or member_type in label):
        return []
    out = []
    for role in ('A', 'B'):
        scope = [source_sha, scope_id, owner_type, member_type, role]
        owner_id = 'instance-' + digest([*scope, 'owner'])[:20]
        out.extend([
            {'id': owner_id, 'type': owner_type, 'name': f'{owner_type} {role}',
             'role': role, 'scope': scope_id, 'citation': citation},
            {'id': 'instance-' + digest([*scope, 'member'])[:20], 'type': member_type,
             'name': f'{member_type} {role}', 'role': role, 'scope': scope_id, 'owner_id': owner_id,
             'citation': citation},
        ])
    return out


def _equivalence_key(relation):
    spans = tuple((r['claim'], r['start'], r['end']) for r in relation['evidence'].values())
    condition = relation['condition']
    terms = tuple((r['claim'], r['start'], r['end'], r['polarity']) for r in condition['terms'])
    ancestors = tuple((r['claim'], r['start'], r['end']) for r in condition.get('applicability', ())
                      if r['scope'] == 'ancestor_preamble')
    return (relation['claim'], tuple(relation['claim_path']), relation['subject_id'], relation['object_id'],
            relation['kind'], relation['direction'], condition['state'], condition['operator'],
            spans, terms, tuple(sorted(relation['view_ids'])), relation['model_required'],
            digest(condition['expression']) if 'expression' in condition else None, ancestors)


def _brief(item):
    raw = item['raw']
    if not isinstance(raw, dict):
        return '该候选关系'
    fields = (raw.get(key) for key in ('subject', 'kind', 'object'))
    return '／'.join(text.strip()[:35] for text in fields if isinstance(text, str) and text.strip()) or '该候选关系'


def reviewed_relations(rows, source, ranges, parents, view_numbers, views, response_sha, *, contract=3, held_numbers=()):
    relations, candidates, limitations, seen = [], [], [], {}
    recovered_guards = []
    views_by_number = {view.get('number', number):view for number,view in enumerate(views, 1)}
    source_sha = hashlib.sha256(source).hexdigest()

    def accept(item, relation):
        key = _equivalence_key(relation)
        if key in seen:
            item.update(status='equivalent', equivalent_to=seen[key])
        else:
            seen[key] = item['candidate_id']
            relations.append(relation)

    for index, row in enumerate(rows):
        wire_guard = row.get('wire_guard') if contract >= 13 and isinstance(row, dict) else None
        checked_row = {**row, 'condition': {'state': 'unknown'}} if wire_guard else row
        item, relation = _relation(checked_row, index, response_sha, source, ranges, parents, view_numbers,
                                   views_by_number, contract, source_sha, held_numbers)
        if wire_guard:
            item['raw'] = row
            item['wire_guard'] = {**wire_guard, 'status': 'unresolved'}
            item['reasons'].extend({'code': 'guard_reference_out_of_range', 'field': e['path']}
                                   for e in wire_guard['errors'])
            from .claims_scope_recovery import recover_guard_root

            if guard := recover_guard_root(row, source, ranges, parents):
                expression, premise, rule = guard
                corrected = {**row, 'condition': {'state': 'stated', 'expression': expression}}
                verified, relation = _relation(corrected, index, response_sha, source, ranges, parents,
                                               view_numbers, views_by_number, contract, source_sha, held_numbers)
                if relation:
                    condition = relation['condition']
                    if premise:
                        applies = {**premise, 'scope': 'preamble' if premise['claim'] == relation['claim']
                                   else 'ancestor_preamble', 'polarity': 'as_written'}
                        applicability = condition.setdefault('applicability', [])
                        key = ('claim', 'start', 'end')
                        if not any(all(p[k] == premise[k] for k in key) for p in applicability):
                            applicability.append(applies)
                        condition['terms'] = [p for p in condition['terms']
                                              if not all(p[k] == premise[k] for k in key)]
                    item['wire_guard'].update(status='recovered', rule=rule, original_reasons=item['reasons'],
                                              condition=condition)
                    item.update(status='accepted', evidence=verified['evidence'], reasons=[])
                    relation['id'] = item['candidate_id']
                else:
                    item['wire_guard']['verification_reasons'] = verified['reasons']
        if not wire_guard and relation is None and contract >= 12:
            from .claims_scope_recovery import recover_ancestor_applicability, recover_scope_candidate

            recovery = (recover_ancestor_applicability(row, item, source, ranges, parents)
                        if contract == RELATION_CONTRACT else None)
            if not recovery:
                recovery = recover_scope_candidate(row, item, source, ranges, parents)
            if recovery:
                corrected, rule, premise = recovery
                verified, relation = _relation(corrected, index, response_sha, source, ranges, parents,
                                               view_numbers, views_by_number, contract, source_sha, held_numbers)
                if relation:
                    condition = relation['condition']
                    if premise and premise['claim'] != relation['claim']:
                        condition.setdefault('applicability', []).append({
                            **premise, 'scope': 'ancestor_preamble', 'polarity': 'as_written',
                        })
                    item.update(
                        status='accepted', evidence=verified['evidence'], reasons=[],
                        scope_recovery={'rule': rule, 'original_reasons': item['reasons'],
                                        'original_evidence': item['evidence'],
                                        'applicability': condition.get('applicability', [])})
                    relation['id'] = item['candidate_id']
        candidates.append(item)
        if relation:
            if wire_guard:
                recovered_guards.append((item, relation))
            else:
                accept(item, relation)
        else:
            claim = item['claim']
            model_required = item['model_required']
            reasons = dict.fromkeys(REASONS[reason['code']] for reason in item['reasons'])
            limitations.append({
                'impact': 'required' if model_required is True else 'review',
                'claims': [claim] if claim else [], 'view_ids': item['view_ids'],
                'candidate_id': item['candidate_id'], 'model_required': model_required,
                'detail': '；'.join(reasons) + '。',
                'question': f'哪段权利要求支持“{_brief(item)}”及其适用条件？',
            })
    for item, relation in recovered_guards:
        accept(item, relation)
    return relations, candidates, limitations

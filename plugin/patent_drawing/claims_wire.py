"""Version 11 model wire format. The reviewed diagram uses the existing structure."""
import json
from copy import deepcopy


def _object(**properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties),
            'additionalProperties': False}


def _array(items):
    return {'type': 'array', 'items': items}


def _enum(*values):
    return {'type': 'string', 'enum': list(values)}


def _optional(schema):
    return {'anyOf': [schema, {'type': 'null'}]}


def _schema(contract=11):
    from .claims_input import IMPACTS, VIEW_TYPES
    from .claims_flow import ROLES
    from .claims_relations import DIRECTIONS

    number, string = {'type': 'integer'}, {'type': 'string'}
    numbers = _array(number)
    guard = {'$ref': '#/$defs/guard'}
    root = _object(
        citations=_array(_object(c=number, q=string, x=_optional(string))),
        views=_array(_object(type=_enum(*sorted(VIEW_TYPES)), title=string, purpose=string, features=numbers)),
        relations=_array(_object(s=_optional(string), o=_optional(string), k=string,
            d=_enum(*sorted(DIRECTIONS)), c=number, p=numbers, v=numbers,
            r={'type': 'boolean'}, e=numbers,
            h={'anyOf': [{'type': 'integer', 'enum': [0]}, {'type': 'null'}, guard]})),
        flow_edges=_array(_object(r=number, k=_enum(*sorted(ROLES)), e=_optional(number))),
        coverage=_array(_object(claim=number, figures=numbers, reason=string)),
        components=_array(_object(name=string, claims=numbers)),
        figure_links=_array(_object(parent=number, child=number, anchor=string, claim=number, quote=string)),
        unresolved=_array(_object(impact=_enum(*sorted(IMPACTS)), detail=string, question=string,
                                  views=numbers, claims=numbers)),
    )
    root['$defs'] = {'guard': _object(op=_enum('literal', 'all', 'any', 'not', 'exception'),
                                     source=number, items=_array(guard))}
    if contract >= 16:
        from .figure_routing import RELATIONSHIP_TOPICS

        root['properties']['relationship_coverage'] = _array(
            _object(t=_enum(*RELATIONSHIP_TOPICS), v=number, e=numbers))
        root['required'].append('relationship_coverage')
    if contract >= 17:
        fields = root['properties']
        order = ['citations', 'views', 'relationship_coverage', 'relations', 'flow_edges',
                 'coverage', 'components', 'figure_links', 'unresolved']
        root['properties'] = {name: fields[name] for name in order}
        root['required'] = order
    return root


def claims_wire_format(contract=11):
    version = 17 if contract >= 17 else 16 if contract >= 16 else 11
    return {'type': 'json_schema', 'name': f'claims_figure_plan_v{version}', 'strict': True,
            'schema': _schema(contract)}


def _check(value, schema, definitions, depth=0):
    if depth > 30:
        return False
    if '$ref' in schema:
        return _check(value, definitions[schema['$ref'].rsplit('/', 1)[-1]], definitions, depth+1)
    if 'anyOf' in schema:
        return any(_check(value, choice, definitions, depth+1) for choice in schema['anyOf'])
    kind = schema['type']
    if type(value) is not {'object': dict, 'array': list, 'integer': int, 'string': str,
                           'null': type(None), 'boolean': bool}[kind]:
        return False
    if 'enum' in schema and value not in schema['enum']:
        return False
    if kind == 'object':
        props = schema['properties']
        return value.keys() == props.keys() and all(
            _check(field, props[name], definitions, depth+1) for name, field in value.items())
    if kind == 'array':
        return all(_check(field, schema['items'], definitions, depth+1) for field in value)
    if kind == 'string':
        return len(value) <= 4000
    return True


def load_wire_plan(text, *, contract=11):
    def unique(pairs):
        output = dict(pairs)
        if len(output) != len(pairs):
            raise ValueError('图单有重复字段。')
        return output
    value = json.loads(text, object_pairs_hook=unique)
    # Check citation and flow indices too, before committing the usable receipt.
    expand_wire_plan(value, contract=contract)
    return value


def expand_wire_plan(wire, *, contract=11):
    """Reconstruct the existing plan, without matching or inventing source text."""
    schema = _schema(contract)
    if not _check(wire, schema, schema['$defs']):
        raise ValueError('紧凑图单字段、类型或长度不合格。')
    citations = [{'claim': row['c'], 'quote': row['q'],
                  **({'context': row['x']} if row['x'] is not None else {})}
                 for row in wire['citations']]

    def ref(index):
        if not 1 <= index <= len(citations):
            raise ValueError('紧凑图单的引证编号超出范围。')
        return dict(citations[index-1])

    def expression(node, errors, path='h'):
        index = node['source']
        if contract >= 13 and not 1 <= index <= len(citations):
            errors.append({'path': path+'.source', 'value': index})
            proof = None
        else:
            proof = ref(index)
        result = {'op': node['op'], 'source': proof}
        if node['items']:
            result['items'] = [expression(child, errors, f'{path}.items[{i}]')
                               for i, child in enumerate(node['items'])]
        return result

    from .claims_relations import RELATION_CONTRACT

    views = []
    for view in wire['views']:
        features = [{'invalid_citation_index':index} if contract == RELATION_CONTRACT
                    and not 1 <= index <= len(citations) else ref(index) for index in view['features']]
        # A feature's original placement is still checked by the existing verifier.
        if contract != RELATION_CONTRACT:
            features = [{'claim':f['claim'], 'quote':f['quote']} for f in features]
        views.append({**view, 'features':features})
    relations = []
    for row in wire['relations']:
        if len(row['e']) != 3:
            raise ValueError('每条关系须依次引用主体、客体和动作。')
        evidence = dict(zip(('subject', 'object', 'relation'), map(ref, row['e'])))
        guard = row['h']
        guard_errors = []
        if guard == 0:
            condition = {'state': 'none_in_source', 'operator': 'none', 'terms': []}
        elif guard is None:
            condition = {'state': 'unknown'}
        else:
            condition = {'state': 'stated', 'expression': expression(guard, guard_errors)}
        relations.append({'subject': evidence['subject']['quote'] if row['s'] is None else row['s'],
                          'object': evidence['object']['quote'] if row['o'] is None else row['o'],
                          'kind': row['k'], 'direction': row['d'], 'claim': row['c'], 'claim_path': row['p'],
                          'views': row['v'], 'required': row['r'], 'evidence': evidence, 'condition': condition,
                          **({'wire_guard': {'raw': deepcopy(guard), 'errors': guard_errors}}
                             if guard_errors else {})})
    edges = []
    for row in wire['flow_edges']:
        number = row['r']
        if not 1 <= number <= len(relations):
            raise ValueError('紧凑图单的流程编号超出范围。')
        label = (relations[number-1]['evidence']['relation'] if row['e'] is None else ref(row['e']))
        edges.append({'relation': number, 'role': row['k'], 'label': label})
    return {**{key: deepcopy(wire[key]) for key in ('coverage', 'unresolved', 'components', 'figure_links')},
            'views': views, 'relations': relations, 'flow_edges': edges}


def relationship_coverage(wire, tasks, source, ranges, view_remap, views, *, held_numbers=()):
    """Keep source-bound placements and expose missing or redundant coverage."""
    from .claims_relations import _find_span
    from .figure_routing import RELATIONSHIP_TOPICS

    grouped = {name: [] for name in RELATIONSHIP_TOPICS}
    rejected = []
    verified_views = {view['number']:view for view in views}
    for row in wire['relationship_coverage']:
        view, indices = row['v'], row['e']
        if view in held_numbers:
            rejected.append({**row,'reason':'figure_citation_limit'})
            continue
        if view not in view_remap or not indices or not all(
                1 <= n <= len(wire['citations']) and n in wire['views'][view-1]['features'] for n in indices):
            rejected.append({**row, 'reason': 'invalid_view_or_citation'})
            continue
        checked = []
        for number in dict.fromkeys(indices):
            ref = wire['citations'][number-1]
            citation = {'claim': ref['c'], 'quote': ref['q']}
            if ref['x'] is not None:
                citation['context'] = ref['x']
            span, _ = _find_span(source, ranges, citation)
            if span is None:
                break
            checked.append({'citation_index': number, **span})
        else:
            actual = verified_views[view_remap[view]]
            placement = {'view_id': actual['id'], 'number': actual['number'],
                         'type': actual['type'], 'citations': checked}
            if placement not in grouped[row['t']]:
                grouped[row['t']].append(placement)
            continue
        rejected.append({**row, 'reason': 'citation_not_verified'})

    rows, issues = [], []
    rejected_topics = {item['t'] for item in rejected}
    for name, (label, _) in RELATIONSHIP_TOPICS.items():
        task = tasks.get(name, {})
        placements = grouped[name]
        state = ('covered' if placements else
                 'pending' if task.get('state', 'absent') != 'absent' or name in rejected_topics else 'absent')
        rows.append({'topic': name, 'label': label, 'luna': task, 'state': state, 'placements': placements})
        if state == 'pending':
            issues.append({'code': 'relationship_coverage_missing', 'impact': 'required',
                           'claims': [], 'view_ids': [], 'detail': f'{label}缺少已核图位和原文引证。',
                           'question': '请核对关系对应的视图、权项和逐字引文。'})

    repeated = []
    for view in views:
        if view['type'] != 'relationship':
            continue
        attached = [(row, place) for row in rows for place in row['placements']
                    if place['view_id'] == view['id']]
        duplicate = attached and all(
            any(other['type'] != 'relationship' and all(
                any((cite['claim'], cite['start'], cite['end']) ==
                    (proof['claim'], proof['start'], proof['end']) for proof in other['citations'])
                for cite in place['citations']) for other in row['placements'])
            for row, place in attached)
        if not attached or duplicate:
            repeated.append(view['id'])
            issues.append({'code': 'relationship_view_unverified', 'impact': 'review',
                           'claims': sorted({f['claim'] for f in view['features']}), 'view_ids': [view['id']],
                           'detail': '该独立关系图缺少已核的独立表达任务。',
                           'question': '请核对实际图位能否合并，或补上本图的独立覆盖依据。'})
    can_omit = all(row['state'] == 'absent' or any(
        p['type'] != 'relationship' for p in row['placements']) for row in rows)
    return {'rows': rows, 'rejected': rejected, 'review_independent_views': repeated,
            'can_omit_independent_view': can_omit}, issues


def derive_coverage(raw, blob, ranges):
    """Fill omitted coverage using unambiguous features; preserve explicit rows."""
    from .claims_relations import _find_span

    already = {row['claim'] for row in raw['coverage']}
    derived = {}
    for number, view in enumerate(raw['views'], 1):
        for feature in view['features']:
            span, _ = _find_span(blob, ranges, feature)
            if span:
                derived.setdefault(span['claim'], set()).add(number)
    for claim in ranges:
        if claim in already:
            continue
        figs = sorted(derived.get(claim, ()))
        raw['coverage'].append({'claim': claim, 'figures': figs,
                                'reason': '按图内逐字引证归集。' if figs else '暂无可核图内引证。'})
        if not figs:
            raw['unresolved'].append({'impact': 'required', 'claims': [claim], 'views': [],
                                     'detail': '权项未取得可核图内引证或明确的无需附图理由。',
                                     'question': '请核对该权项的图位或原文依据。'})

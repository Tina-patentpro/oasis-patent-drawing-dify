"""Source existence, drawable geometry and visual proof are distinct states."""
import copy
import math
import re
from .cn_policy import domain_drawing_obligations
from .diagrams import (ARROW_LENGTH, TOKEN, _crosses, _inside_shape, _overlap, _route, _text_box,
                       edge_geometry_fits, resolve, split_drafting_notes)
from .natural_views import role_parts
from .operation_store import canonical, digest
from .source_contract import BEAM_MESSAGES, MODE_COMPONENTS, compact, drawing_relationships, validate_facts
from .spatial_brief import MATERIAL_HEAT_PATH, requirements

_UNKNOWN=re.compile(r'待确认|未确定|不确定|未给出|未知|缺少|未规定')
_PHYSICAL=re.compile(r'弹簧|安装销|通道|配合|导电连接|补偿连接|PCB|托板|剖视|接地孔')

_INTERLOCK_POSITION = re.compile(r'第([一二12])(?:个)?锁定(?:位置|状态)|中间位置|检修位置')
_CONTROL_ROD = re.compile(r'第?([一二12])(?:号)?控制杆|控制杆([一二12])(?:号)?')
_ROD_BLOCKED = re.compile(r'阻挡|挡住|受阻|阻止|锁止|断开|禁止闭合|不允许闭合|不可闭合')
_ROD_ALLOWED = re.compile(r'让行|可闭合|允许.{0,5}闭合')
_SECTION_MARKS = ('贯穿电极', '导电图案', '导电柱', '芯片连接端子')
_POSITION_NUMBERS = str.maketrans('12', '一二')
_PACKAGE_STEPS = (('再分布结构', r'形成|制备|制作'), ('导电柱', r'设置|形成|制备|制作'), ('热压', r'接合|键合|连接'))
_FILM_ORIENTATION = re.compile(r'沿膜面(?:方向)?取向|膜面取向|面内取向')
_FLAKE_CONTACT = re.compile(r'(?:彼此|相互)接触|(?:邻片|邻接鳞片|相邻鳞片)[^，,]{0,8}接触')
_MATERIAL_NAME_REFERENCE = re.compile(r'\{\{([A-Za-z0-9_-]+)\.name\}\}')
_MATERIAL_PARALLEL_NODES = {
    'orientation': re.compile(
        r'(?:(?:鳞片(?:状碳材料)?(?:的)?)?长轴)?(?:主要)?(?:沿膜面(?:方向)?取向|膜面取向|面内取向)'
        r'(?:利用面内导热特性)?'),
    'contact': re.compile(r'(?:鳞片状碳材料)?(?:邻片|(?:邻接|相邻)的?鳞片(?:状碳材料)?)(?:彼此|相互)?接触'),
    'network': re.compile(
        r'(?:利用鳞片(?:状碳材料)?(?:的)?面内导热特性形成|在导热膜内形成|形成)?连续(?:面内)?传热网络'),
    'spreading': re.compile(
        r'(?:预期)?(?:(?:局部)?热量沿膜面(?:方向)?扩散|面内热扩散)'
        r'(?:（(?:预期作用|定性机理)）|\((?:预期作用|定性机理)\))?'),
}
_INTERLOCK_SHAPES = {
    '第一控制杆': 'rect', '第二控制杆': 'rect', '阻挡边缘': 'polyline',
    '第一接触边缘': 'polyline', '第二接触边缘': 'polyline',
}


def _interlock_groups(views, parts):
    groups = {'一': [], '二': [], '中': []}
    for view in views:
        diagram, _ = split_drafting_notes(view['diagram'])
        texts = [(row, True) for row in diagram.get('primitives', []) if row['type'] == 'text']
        texts += [(row, False) for row in diagram.get('nodes', [])]
        active = None
        for row, primitive in texts:
            size = row.get('font_size', 26)
            for index, line in enumerate(resolve(row['text'], parts).splitlines()):
                box = None
                if primitive:
                    at = (row['at'][0], row['at'][1] + index * (size + 7))
                    box = _text_box(line, at, size, row.get('anchor', 'start'))
                line = compact(line)
                positions = list(_INTERLOCK_POSITION.finditer(line))
                if len(positions) > 1:
                    active = None
                    continue
                if positions:
                    active = (positions[0].group(1) or '中').translate(_POSITION_NUMBERS)
                if active:
                    groups[active].append((line, box, size, view['id']))
    return groups


def _interlock_actions(clause):
    found = set()
    blocked = _ROD_BLOCKED.search(clause)
    allowed = _ROD_ALLOWED.search(clause)
    if blocked:
        found.add('blocked')
    if allowed and not (blocked and blocked.start() <= allowed.start() < blocked.end()):
        found.add('allowed')
    return found


def _adjacent_rod_text(rod_box, status_box, size):
    rod_center = (rod_box[0] + rod_box[2]) / 2
    status_center = (status_box[0] + status_box[2]) / 2
    below = abs(rod_center - status_center) <= size and 0 <= status_box[1] - rod_box[3] <= 2 * size
    alongside = abs(status_box[1] - rod_box[1]) <= size / 2 and 0 <= status_box[0] - rod_box[2] <= 2 * size
    return below or alongside


def _rod_actions(texts):
    actions = {'一': set(), '二': set()}
    unpaired_rods = []
    for text, box, size, view in texts:
        rods = list(_CONTROL_ROD.finditer(text))
        for index, rod in enumerate(rods):
            role = (rod.group(1) or rod.group(2)).translate(_POSITION_NUMBERS)
            clause = text[rod.end():rods[index + 1].start() if index + 1 < len(rods) else len(text)]
            actions[role].update(_interlock_actions(clause))
            if box is not None and _CONTROL_ROD.fullmatch(text.rstrip('：:')):
                unpaired_rods.append((role, box, size, view))
    for text, box, size, view in texts:
        if box is None or _CONTROL_ROD.search(text) or not (_ROD_BLOCKED.match(text) or _ROD_ALLOWED.match(text)):
            continue
        nearby = [role for role, rod_box, rod_size, rod_view in unpaired_rods
                  if rod_view == view and _adjacent_rod_text(rod_box, box, max(rod_size, size))]
        if len(nearby) == 1:
            actions[nearby[0]].update(_interlock_actions(text))
    return actions


def _interlock_shape_groups(views, parts):
    for view in views:
        primitives = view['diagram'].get('primitives', [])
        headings = []
        for item in primitives:
            if item['type'] != 'text':
                continue
            text = compact(resolve(item['text'], parts))
            match = _INTERLOCK_POSITION.fullmatch(text)
            if match:
                x1, y, x2, _ = _text_box(text, item['at'], item.get('font_size', 26), item.get('anchor', 'start'))
                headings.append(((match.group(1) or '中').translate(_POSITION_NUMBERS), (x1 + x2) / 2, y))
        if not headings or len({position for position, _, _ in headings}) != len(headings):
            continue
        if len(headings) > 1 and (max(y for _, _, y in headings) - min(y for _, _, y in headings) > 35
                                  or len({x for _, x, _ in headings}) != len(headings)):
            continue

        def panel(x):
            candidates = sorted((abs(x - center), role) for role, center, _ in headings)
            return candidates[0][1] if len(candidates) == 1 or candidates[0][0] < candidates[1][0] else None

        marked = {item['target_shape'] for item in primitives if item['type'] == 'label'}
        groups = {position: {} for position, _, _ in headings}
        for item in primitives:
            if item.get('id') not in marked:
                continue
            name = compact(parts[item['part']]['name'])
            if _INTERLOCK_SHAPES.get(name) != item['type'] or item.get('arrow'):
                continue
            xs = item['box'][::2] if item['type'] == 'rect' else [point[0] for point in item['points']]
            position = panel(min(xs))
            if position and position == panel(max(xs)):
                groups[position].setdefault(name, []).append(item)
        yield from groups.items()


def _touches_rod_front(a, b, box):
    left, top, right, _ = box
    if a[1] == b[1]:
        return a[1] == top and min(max(a[0], b[0]), right) > max(min(a[0], b[0]), left)
    if not min(a[1], b[1]) <= top <= max(a[1], b[1]):
        return False
    x = a[0] + (b[0] - a[0]) * (top - a[1]) / (b[1] - a[1])
    return left < x < right


def _interlock_travel(position, shapes):
    rod_names = ['第一控制杆', '第二控制杆']
    names = [*rod_names, '阻挡边缘']
    if position != '中':
        allowed = 1 if position == '一' else 0
        names.append(['第一接触边缘', '第二接触边缘'][allowed])
    if any(len(shapes.get(name, [])) != 1 for name in names):
        return False
    first, second, blocking, *guide = [shapes[name][0] for name in names]
    rods = [first['box'], second['box']]
    if _overlap(*rods):
        return False
    segments = list(zip(blocking['points'], blocking['points'][1:]))
    if any(_crosses(a, b, box) for box in rods for a, b in segments):
        return False
    stopped = [any(_touches_rod_front(a, b, box) for a, b in segments) for box in rods]
    if position == '中':
        return all(stopped)
    if stopped[allowed] or not stopped[1 - allowed]:
        return False
    left, top, right, _ = rods[allowed]
    if any(_crosses(a, b, [left, -1, right, top + 0.2]) for a, b in segments):
        return False
    xs, ys = zip(*guide[0]['points'])
    return len(set(xs)) == 1 and (xs[0] <= left or xs[0] >= right) and min(ys) < top


def _reachable_nodes(diagram):
    links = {node['id']: set() for node in diagram.get('nodes', [])}
    for edge in diagram.get('edges', []):
        links[edge['source']].add(edge['target'])
    reachable = {}
    for start in links:
        pending = list(links[start])
        visited = set()
        while pending:
            node = pending.pop()
            if node not in visited:
                visited.add(node)
                pending.extend(links[node])
        reachable[start] = visited
    return reachable


def _package_process_visible(view, parts):
    diagram = view['diagram']
    nodes = {node['id']: compact(resolve(node['text'], parts)) for node in diagram.get('nodes', [])}
    step_nodes = [
        [ident for ident, text in nodes.items() if subject in text and re.search(action, text)]
        for subject, action in _PACKAGE_STEPS
    ]
    reachable = _reachable_nodes(diagram)
    return any(len({first, middle, last}) == 3 and middle in reachable[first] and last in reachable[middle]
               and first not in reachable[middle] and middle not in reachable[last]
               for first in step_nodes[0] for middle in step_nodes[1] for last in step_nodes[2])


def _film_conditions_joined(bundle, page_id):
    page = next(page for page in bundle['pages'] if page['id'] == page_id)
    for clause in re.split(r'[。；;！？!?]', compact(page.get('text', ''))):
        orientation = _FILM_ORIENTATION.search(clause)
        contact = _FLAKE_CONTACT.search(clause)
        if orientation and contact:
            first, second = sorted((orientation, contact), key=lambda match: match.start())
            between = clause[first.end():second.start()]
            if not re.search(r'先|再|随后|之后|然后', clause) and re.search(r'并|且|和|以及', between):
                return True
    return False


def _orders_film_conditions(view, parts):
    diagram = view['diagram']
    orientation, contact = [], []
    for node in diagram.get('nodes', []):
        label = compact(resolve(node['text'], parts))
        has_orientation = bool(_FILM_ORIENTATION.search(label))
        has_contact = bool(_FLAKE_CONTACT.search(label))
        if has_orientation and not has_contact:
            orientation.append(node['id'])
        if has_contact and not has_orientation:
            contact.append(node['id'])
    if not orientation or not contact:
        return False
    reachable = _reachable_nodes(diagram)
    return any(b in reachable[a] or a in reachable[b] for a in orientation for b in contact)


def _material_heat_bindings(bundle, plan):
    if bundle.get('figure_presence') != 'absent' or 'chemical_material' not in plan.get('technical_types', []):
        return {}
    return {view['id']: set(feature['source_fact_ids'])
            for view in plan['views'] if view['engine'] == 'image'
            for feature in requirements(bundle, plan, view)['features'] if feature['id'] == MATERIAL_HEAT_PATH}


def _material_parallel_view(view, bindings):
    required = set(view.get('required_facts', []))
    return (view.get('engine') == 'vector' and view.get('delivered_type') in {'relationship', 'block_diagram'}
            and len(view.get('diagram', {}).get('nodes', [])) == 4
            and any(ids and ids <= required for ids in bindings.values()))


def material_parallel_heat(view, plan, *, bindings=None):
    """Recognize the four supported diagram roles and check their directed edges."""
    if bindings is None:
        bindings = {ident: set(feature['source_fact_ids'])
                    for ident, contract in plan.get('visibility_contract', {}).items()
                    for feature in contract['features']
                    if feature.get('feature_id') == MATERIAL_HEAT_PATH and feature.get('source_fact_ids')}
    if not _material_parallel_view(view, bindings):
        return None
    diagram = view['diagram']
    nodes = diagram['nodes']
    edges = diagram.get('edges', [])
    if any(edge.get('label') for edge in edges) or any(item.get('arrow') for item in diagram.get('primitives', [])):
        return None
    parts = {part['id']: part for part in plan['parts']}
    labels = {node['id']: compact(resolve(node['text'], parts)) for node in nodes}
    roles = {role: [ident for ident, label in labels.items() if pattern.fullmatch(label)]
             for role, pattern in _MATERIAL_PARALLEL_NODES.items()}
    if any(len(ids) != 1 for ids in roles.values()):
        return None
    orientation, contact, network, spreading = (roles[role][0] for role in _MATERIAL_PARALLEL_NODES)
    expected = {(orientation, network), (contact, network), (network, spreading)}
    return len(edges) == len(expected) and {(edge['source'], edge['target']) for edge in edges} == expected


def normalize_material_relationship_marks(bundle, plan, *, bindings=None):
    """Retain the verified graph and remove proposal-only marks on its abstract nodes."""
    if (plan.get('application_stage', 'draft') != 'draft' or plan.get('scene')
            or plan.get('specification_labels') or plan.get('specification_labels_complete')):
        return plan, None
    if bindings is None:
        bindings = _material_heat_bindings(bundle, plan)
    if not bindings:
        return plan, None
    rewritten = copy.deepcopy(plan)
    parts = {part['id']: part for part in rewritten['parts']}
    abstract_parts = {
        ident: part['name'] for ident, part in parts.items()
        if set(part) == {'id', 'label', 'name'}
        and any(pattern.fullmatch(compact(part['name'])) for pattern in _MATERIAL_PARALLEL_NODES.values())
    }
    edits = []
    eligible_views = set()
    for view in rewritten['views']:
        if not material_parallel_heat(view, plan, bindings=bindings):
            continue
        eligible_views.add(view['id'])
        for node in view['diagram']['nodes']:
            def inline(match):
                ident = match[1]
                if ident not in abstract_parts or ident not in view['parts']:
                    return match[0]
                edits.append({'view_id': view['id'], 'node_id': node['id'], 'part_id': ident})
                return abstract_parts[ident]
            node['text'] = _MATERIAL_NAME_REFERENCE.sub(inline, node['text'])
    if not edits:
        return plan, None
    removed = {edit['part_id'] for edit in edits}
    removed_marks = {parts[ident]['label'] for ident in removed}
    if any(removed_marks.intersection(map(str, view.get('labels', []))) for view in plan['views']):
        return plan, None
    rewritten['parts'] = [part for part in rewritten['parts'] if part['id'] not in removed]
    for view in rewritten['views']:
        if view.get('parts') and (view['id'] in eligible_views or view['id'] in bindings):
            view['parts'] = [ident for ident in view['parts'] if ident not in removed]
    # A remaining use may be an annotation, an explicit label reference or an
    # actual part used in another view. Preserve the input in that case.
    references = canonical(rewritten)
    if any(re.search(r'(?<![A-Za-z0-9_-])' + re.escape(ident) + r'(?![A-Za-z0-9_-])', references)
           for ident in removed):
        return plan, None
    return rewritten, {'input_sha256': digest(plan), 'output_sha256': digest(rewritten),
                       'removed': [part for part in plan['parts'] if part['id'] in removed], 'nodes': edits}


def material_relationship_mark_notice(bundle, plan):
    if plan.get('specification_labels') or plan.get('specification_labels_complete'):
        return False
    bindings = _material_heat_bindings(bundle, plan)
    return bool(bindings) and any(material_parallel_heat(view, plan, bindings=bindings)
                                  for view in plan['views'])


CONTROL_VIEWS = {'block', 'block_diagram', 'flowchart', 'process_flow', 'relationship', 'state'}
NETWORK_VIEWS = {'block', 'block_diagram', 'topology', 'relationship'}
SEQUENCE_VIEWS = {'sequence', 'interaction'}
BEAM_FLOW_VIEWS = {'flowchart', 'process_flow'}
_REFERENCE_MEASUREMENT = re.compile(r'接收(?:参考信号)?(?:并|且|和|与|及|后)(?:进行)?测量')


def view_nodes(view, parts):
    """Index real node text resolved from the stable part register."""
    return {node['id']: compact(resolve(node['text'], parts))
            for node in view.get('diagram', {}).get('nodes', ())}


def _role_instances(view, parts, names, *, exact):
    nodes = view.get('diagram', {}).get('nodes', ())
    texts = view_nodes(view, parts)
    roles = {}
    for name in names:
        part_ids = [pid for pid in view.get('parts', ()) if compact(parts[pid]['name']) == name]
        if len(part_ids) != 1:
            continue
        pid = part_ids[0]
        printed = {name, name + parts[pid]['label']}
        matches = [node for node in nodes if node['type'] in {'process', 'actor', 'decision'}
                   and (texts[node['id']] in printed if exact else name in texts[node['id']])
                   and (not TOKEN.findall(node['text']) or (pid, 'name') in TOKEN.findall(node['text']))]
        if matches:
            roles[name] = (pid, matches)
    return roles


def role_nodes(view, parts, names, *, exact=False):
    return {name: (part_id, nodes[0]['id']) for name, (part_id, nodes) in
            _role_instances(view, parts, names, exact=exact).items() if len(nodes) == 1}


def communication_link_groups(view, parts, relation):
    """Bind each link to the corresponding nodes of an explicitly disclosed repeated role."""
    if view.get('delivered_type') not in NETWORK_VIEWS or view.get('source_region_id'):
        return None
    names = (relation['source_name'], relation['target_name'])
    repeatable = relation.get('repeated_roles', ())
    roles = _role_instances(view, parts, names, exact=True)
    if len(roles) != 2:
        return None
    endpoints = []
    for name in names:
        part_id, nodes = roles[name]
        if len(nodes) > 1 and (name not in repeatable or any(
                (part_id, 'name') not in TOKEN.findall(node['text']) for node in nodes)):
            return None
        endpoints.append({node['id']: (part_id, node['id']) for node in nodes})
    left, right = endpoints
    groups = {}
    for index, edge in enumerate(view['diagram'].get('edges', ())):
        start, end = edge['source'], edge['target']
        if start in right and end in left:
            start, end = end, start
        if start not in left or end not in right:
            continue
        label = edge.get('label')
        if label and re.search('供电|主母线|电力', resolve(label, parts)):
            continue
        groups.setdefault((left[start], right[end]), []).append(index)
    first = {pair[0][1] for pair in groups}
    second = {pair[1][1] for pair in groups}
    complete = first == left.keys() and second == right.keys()
    if len(repeatable) == 2:
        complete = complete and len(groups) == len(first) == len(second)
    return groups, complete


def pwm_nodes(view, parts):
    modes = role_nodes(view, parts, MODE_COMPONENTS[:1], exact=True)
    outputs = role_nodes(view, parts, MODE_COMPONENTS[1:])
    return modes.get(MODE_COMPONENTS[0]), outputs.get(MODE_COMPONENTS[1])


def _beam_message_label(parts, edge):
    label = edge.get('label')
    return compact(resolve(label, parts)) if label else ''


def _beam_actors(view, parts):
    roles = role_nodes(view, parts, ('发射设备', '接收设备'))
    by_id = {item['id']: item for item in view['diagram'].get('nodes', ())}
    return {role: node_id for role, (_, node_id) in roles.items()
            if compact(resolve(by_id[node_id]['text'].splitlines()[0], parts)).startswith(role)}


def beam_message_intent(view, parts, edge):
    """Resolve only source-defined range, reference and feedback endpoints."""
    label = _beam_message_label(parts, edge)
    if label not in BEAM_MESSAGES:
        return None
    values = view_nodes(view, parts)
    source, target = values.get(edge['source'], ''), values.get(edge['target'], '')
    if view.get('delivered_type') == 'relationship':
        actors = _beam_actors(view, parts)
        if len(actors) != 2:
            return False
        tx, rx = (actors[role] for role in ('发射设备', '接收设备'))
        return (edge['source'], edge['target']) == ((rx, tx) if label == '测量反馈' else (tx, rx))
    if label == '范围指示':
        return bool(re.search('定义.*范围|发送.*范围指示', source)
                    and re.search('选择.*波束|接收.*范围指示', target))
    if label == '参考信号':
        return '发送参考信号' in source and bool(_REFERENCE_MEASUREMENT.search(target))
    return bool(('回传测量反馈' in source or _REFERENCE_MEASUREMENT.search(source))
                and ('接收测量反馈' in target or target.startswith(('发射设备', '发射端')))
                and not re.search('定义|发送', target))


def _beam_geometry(view, parts, *, require_marks=False):
    primitives = view['diagram'].get('primitives', ())
    roles = role_parts({'parts': [parts[pid] for pid in view['parts']]},
                       ('天线面板', '角度范围', '发射波束方向'))
    if not roles:
        return False
    panel, angle, beam = roles.values()
    panels = [item for item in primitives if item.get('part') == panel and item['type'] == 'rect']
    rays = {
        role: [item for item in primitives if item.get('part') == role and item['type'] == 'polyline'
               and len(item['points']) == 2 and bool(item.get('arrow')) == (role == beam)]
        for role in (angle, beam)
    }
    if len(panels) != 1 or len(rays[angle]) != 2 or len(rays[beam]) < 2:
        return False
    shape = panels[0]
    lines = rays[angle] + rays[beam]
    origin = lines[0]['points'][0]
    left, top, right, bottom = shape['box']
    if not (origin[0] in (left, right) and top < origin[1] < bottom):
        return False
    direction = 1 if origin[0] == right else -1
    if any(line['points'][0] != origin or direction * (line['points'][1][0] - origin[0]) <= 0
           for line in lines):
        return False
    slopes = [(line['points'][1][1] - origin[1]) / abs(line['points'][1][0] - origin[0]) for line in lines]
    low, high = sorted(slopes[:2])
    if not all(low < slope < high for slope in slopes[2:]) or len(set(slopes[2:])) != len(slopes[2:]):
        return False
    if not require_marks:
        return True
    shapes = {item['id']: item for item in [shape, *lines] if item.get('id')}
    marks = {item['part'] for item in primitives if item['type'] == 'label'
             and (target := shapes.get(item.get('target_shape'))) and target['part'] == item['part']
             and _inside_shape(item['target'], target) and parts[item['part']]['label']}
    return marks == {panel, angle, beam}


def _beam_flow_dependencies(view, parts):
    graph = view['diagram']
    texts = view_nodes(view, parts)
    candidates = [[nid for nid, value in texts.items() if re.search(pattern, value)] for pattern in (
        '选择.*波束', _REFERENCE_MEASUREMENT, '回传测量反馈', '发送参考信号')]
    if not all(len(group) == 1 for group in candidates[:3]):
        return False, False
    select, measure, feedback = [group[0] for group in candidates[:3]]
    reachable = _reachable_nodes(graph)
    ordered = (len({select, measure, feedback}) == 3 and measure in reachable[select]
               and feedback in reachable[measure] and select not in reachable[measure]
               and measure not in reachable[feedback])
    waiting = any(re.search(r'等待.{0,10}(?:选束|波束|确认|握手)|收到选束完成', value) for value in texts.values())
    waiting |= any(sender in reachable[select] or sender in reachable[measure] for sender in candidates[3])
    return ordered, waiting


def _check_beam_views(bundle, plan, parts, views, report):
    geometry = [view for view in plan['views']
                if view.get('delivered_type') in {'relationship', 'state'} and
                all(any(term in parts[pid]['name'] for pid in view['parts'])
                    for term in ('天线面板', '角度范围', '波束方向'))]
    roles = {}
    for view in views:
        diagram = view['diagram']
        kind = view.get('delivered_type')
        unsafe_message = False
        if kind in SEQUENCE_VIEWS | BEAM_FLOW_VIEWS | {'relationship'}:
            by_id = {node['id']: node for node in diagram.get('nodes', ())}
            valid = []
            for edge in diagram.get('edges', ()):
                intent = beam_message_intent(view, parts, edge)
                if intent is None:
                    continue
                source, target = by_id.get(edge['source']), by_id.get(edge['target'])
                if not intent or edge.get('bidirectional'):
                    unsafe_message = True
                    report('SEQUENCE-EDGE-INTENT', '请确认该消息实际收发节点。测量反馈的已知目标是发射设备。',
                           view_id=view['id'], edge=[edge['source'], edge['target']])
                if not source or not target or not edge_geometry_fits(edge.get('points', _route(source, target)), source, target,
                                          list(by_id.values())):
                    unsafe_message = True
                    report('SEQUENCE-EDGE-BOUNDARY', '连线须进入对应节点边界，且箭尖及路径不可穿过节点。',
                           view_id=view['id'], edge=[edge['source'], edge['target']])
                elif intent and not edge.get('bidirectional'):
                    valid.append(_beam_message_label(parts, edge))
            complete = not unsafe_message and sorted(valid) == sorted(BEAM_MESSAGES)
            if kind == 'relationship' and complete and len(diagram['edges']) == len(BEAM_MESSAGES):
                roles['设备交互'] = view['id']
            ordered, waiting = _beam_flow_dependencies(view, parts) if kind != 'relationship' else (False, False)
            if waiting:
                report('SEQUENCE-EDGE-DEPENDENCY', '交底未给出发送参考信号须等待选束、握手或测量的依赖。',
                       view_id=view['id'])
            if kind in SEQUENCE_VIEWS and complete and ordered and not waiting:
                roles['消息次序'] = view['id']
            if kind in BEAM_FLOW_VIEWS and ordered and not unsafe_message and not waiting:
                roles['接收与测量'] = view['id']
    for view in geometry:
        natural_vector = bundle.get('figure_presence') == 'absent' and view['engine'] == 'vector'
        if natural_vector and (view['id'] in roles.values() or not _beam_geometry(view, parts)):
            report('BEAM-ANGLE-GEOMETRY', '相对角度需要独立图件，画出面板、两条范围边界和至少两条内部方向线。',
                   view_id=view['id'])
            continue
        needs_marks = natural_vector and plan.get('part_mark_mode') == 'numbered'
        if needs_marks and not _beam_geometry(view, parts, require_marks=True):
            report('BEAM-ANGLE-MARKS', '角度图须将面板、角度范围及发射方向的标号引至对应线条或轮廓。',
                   view_id=view['id'], missing_components=['天线面板', '角度范围', '发射波束方向'])
        else:
            roles.setdefault('相对角度', view['id'])
    missing = [role for role in ('相对角度', '设备交互', '消息次序', '接收与测量') if role not in roles]
    if missing:
        report('BEAM-VIEW-COVERAGE', '尚缺以下来源视图：' + '、'.join(missing) + '。', missing_views=missing)
    return [{'view_id': fid, 'role': role} for role, fid in roles.items()]


def _reciprocal_paths(view, edges):
    if len(edges) == 1:
        return edges[0].get('bidirectional') is True
    if len(edges) != 2 or any(edge.get('bidirectional') for edge in edges):
        return False
    first, second = edges
    if first['source'] != second['target'] or first['target'] != second['source']:
        return False
    nodes = {node['id']: node for node in view['diagram']['nodes']}
    routes = []
    for edge in edges:
        source, target = nodes[edge['source']], nodes[edge['target']]
        route = edge.get('points', _route(source, target))
        if not edge_geometry_fits(route, source, target, list(nodes.values())):
            return False
        routes.append(route)
    a, b = routes
    return math.dist(a[0], b[-1]) >= ARROW_LENGTH and math.dist(a[-1], b[0]) >= ARROW_LENGTH


def relationship_coverage(bundle, plan, technical_types):
    """Bind visible relations to stable nodes, exact source locators and a plan revision."""
    relationships = drawing_relationships(bundle, technical_types, bind_instances=True)
    views = [v for v in plan.get('views', ()) if v.get('engine') == 'vector']
    parts = {part['id']: part for part in plan.get('parts', ())}
    findings, bindings = [], []

    disclosed_edges = set()
    plan_sha256 = digest(plan) if relationships else None
    for relation in relationships:
        def report(rule, message, **scope):
            findings.append({'rule': rule, 'verdict': 'FAIL', 'message': message,
                             'page_id': relation['evidence'][0]['page_id'],
                             'relationship_id': relation['id'], **scope})

        placements = []
        if relation['kind'] == 'beam':
            placements = _check_beam_views(bundle, plan, parts, views, report)
        elif relation['kind'] == 'communication':
            pair = (relation['source_name'], relation['target_name'])
            for view in views:
                matched = communication_link_groups(view, parts, relation)
                if matched is None:
                    continue
                groups, complete = matched
                if not complete:
                    report('COMMUNICATION-DIRECTION', '请核对该图所示通信节点的逐列对应和往返路径。',
                           view_id=view['id'])
                for (left, right), indices in groups.items():
                    disclosed_edges.update((view['id'], index) for index in indices)
                    edges = [view['diagram']['edges'][index] for index in indices]
                    if _reciprocal_paths(view, edges):
                        placements.append({'view_id': view['id'], 'part_ids': [left[0], right[0]],
                                           'node_ids': [left[1], right[1]]})
                    else:
                        report('COMMUNICATION-DIRECTION', '该通信链路两端须有可辨的相反箭尖或两支独立箭线。',
                               view_id=view['id'], part_ids=[left[0], right[0]])
            if not placements and not any(f.get('relationship_id') == relation['id'] for f in findings):
                report('COMMUNICATION-DIRECTION', '待补可核通信图的两端关系：' + '、'.join(pair) + '。')
        else:
            eligible = [view for view in views if view.get('delivered_type') in CONTROL_VIEWS
                        and not view.get('source_region_id') and any(
                            'PWM' in text for text in view_nodes(view, parts).values())]
            for view in eligible:
                mode, output = pwm_nodes(view, parts)
                nodes = {node['id']: node for node in view['diagram'].get('nodes', ())}
                mode_edges = [edge for edge in view['diagram'].get('edges', ())
                              if mode and output and edge['source'] == mode[1]
                              and edge['target'] == output[1] and not edge.get('bidirectional')]
                if len(mode_edges) == 1 and mode[1] != output[1]:
                    start, end = nodes[mode[1]], nodes[output[1]]
                    attached = edge_geometry_fits(mode_edges[0].get('points', _route(start, end)),
                                                  start, end, list(nodes.values()))
                else:
                    attached = False
                if attached:
                    placements.append({'view_id': view['id'], 'part_ids': [mode[0], output[0]],
                                       'node_ids': [mode[1], output[1]]})
                else:
                    report('PWM-MODE-INFLUENCE', '图内须以独立模式判定节点指向 PWM。判定参数和切换阈值仍待核。',
                           view_id=view['id'])
            if not eligible:
                report('PWM-MODE-INFLUENCE', '需要可见的独立模式判定及其对 PWM 的单向作用。')
        bindings.append({**relation, 'placements': placements, 'revision': plan.get('revision'),
                         'plan_sha256': plan_sha256, 'source_sha256': plan.get('source_sha256')})
    for view in views:
        for index, edge in enumerate(view.get('diagram', {}).get('edges', ())):
            if edge.get('bidirectional') and (view['id'], index) not in disclosed_edges:
                findings.append({'rule': 'DIRECTION-SOURCE', 'verdict': 'FAIL', 'view_id': view['id'],
                                 'edge': [edge['source'], edge['target']],
                                 'message': '双向箭线缺对应的交底往返关系，请核对来源位置。'})
    return findings, bindings


def _visible_edges(view):
    diagram = view['diagram']
    nodes = {row['id']: row for row in diagram.get('nodes', ())}
    return {(edge['source'], edge['target']) for edge in diagram.get('edges', ())
            if not edge.get('bidirectional') and (start := nodes.get(edge['source']))
            and (end := nodes.get(edge['target']))
            and edge_geometry_fits(edge.get('points', _route(start, end)), start, end, list(nodes.values()))}


def _account_isolation(view, parts):
    if view.get('delivered_type') not in {'flowchart', 'block_diagram', 'relationship'}:
        return False
    nodes = view['diagram'].get('nodes', ())
    if len(nodes) != 4 or any(node['type'] not in {'actor', 'process'} for node in nodes):
        return False
    edges = _visible_edges(view)
    if len(edges) != 2 or len(view['diagram'].get('edges', ())) != 2 or len({item for pair in edges for item in pair}) != 4:
        return False
    texts = view_nodes(view, parts)
    refs = {node['id']: {pid for pid, field in TOKEN.findall(node['text']) if field == 'name'} for node in nodes}
    groups = []
    for account, queue in edges:
        marker = re.search(r'([甲乙AB12])$', texts[account])
        if not marker or len(refs[account]) != 1 or len(refs[queue]) != 2 or not refs[account] < refs[queue]:
            return False
        account_part = next(iter(refs[account]))
        queue_part = next(iter(refs[queue] - refs[account]))
        if (not re.search('账号|账户|帐户', parts[account_part]['name']) or '队列' not in parts[queue_part]['name']
                or marker[1] + '的' not in texts[queue]):
            return False
        groups.append((marker[1], refs[account], refs[queue]))
    if groups[0][0] == groups[1][0]:
        return False
    first, second = groups[0][1], groups[1][1]
    if first != second and {parts[next(iter(ids))]['name'] for ids in (first, second)} != {'任一账号', '其他账号'}:
        return False
    return groups[0][2] - first == groups[1][2] - second


def account_isolation_candidate(view, parts):
    names = {parts[pid]['name'] for pid in view['parts']}
    account_names = {'账号', '任一账号', '其他账号'}
    queue_names = {'本地待同步队列', '离线待同步队列', '待同步队列'}
    return ('隔离' in view['title'] and bool(names & account_names) and bool(names & queue_names)
            and names <= account_names | queue_names | {'终端'})


def _sync_flow_roles(view, parts):
    texts = view_nodes(view, parts)
    diagram = view['diagram']
    visible = _visible_edges(view)
    reachable = _reachable_nodes({'nodes': diagram.get('nodes', ()),
                                  'edges': [{'source': a, 'target': b} for a, b in visible]})

    def ids(pattern):
        return [ident for ident, value in texts.items() if re.search(pattern, value)]

    def follows(source, target):
        return any(a != b and b in reachable[a] for a in ids(source) for b in ids(target))

    def confirms_queue_removal():
        gates = set(ids('收到.*确认'))
        remove = set(ids('移除.*队列项'))
        retain = set(ids('保留.*队列项'))
        for node in diagram['nodes']:
            if node['type'] != 'decision' or node['id'] not in gates:
                continue
            yes, no = (node['branches'][branch] for branch in ('yes', 'no'))
            if (node['id'], yes) not in visible or (node['id'], no) not in visible:
                continue
            yes_path, no_path = reachable[yes] | {yes}, reachable[no] | {no}
            if remove & yes_path and retain & no_path and not (remove & no_path or retain & yes_path):
                return True
        return False

    roles = []
    if _account_isolation(view, parts):
        roles.append('sync_account_isolation')
    if view.get('delivered_type') != 'flowchart':
        return roles
    if (follows('写入.*任务ID.*操作ID.*本地序号', '上传')
            and ids('本地序号(?:仅|只)在本终端内递增')
            and follows('上传', '按操作ID.*重复')
            and follows('按操作ID.*重复', '返回既有结果.*不重复应用')
            and confirms_queue_removal()
            and not ids('接收(?:并|和)应用')):
        roles.append('sync_acknowledgment')
    if follows('拉取.*之后的变更', '应用.*不得覆盖.*未获确认(?:的)?(?:本地|离线)?(?:待同步)?队列(?!项)'):
        roles.append('sync_incremental')
    if (follows('(?:同一字段|同字段).*并发|并发.*同一字段', '保留.*两个版本')
            and follows('保留.*两个版本', '提示.*选择')):
        roles.append('sync_field_conflict')
    return roles


def _cabinet_zones(view, parts):
    roles = role_parts({'parts': [parts[pid] for pid in view['parts']]},
                       ('电气连接柜', '供电列', '连接列', '监测控制单元'))
    if not roles:
        return False
    cabinet, power, connection, monitor = roles.values()
    primitives = view['diagram'].get('primitives', ())
    rectangles = {item['part']: item['box'] for item in primitives if item['type'] == 'rect' and item.get('part')}
    if not {cabinet, power, connection} <= rectangles.keys() or not all(
            any(item['type'] == 'text' and (part_id, 'name') in TOKEN.findall(item['text'])
                and _inside_shape(item['at'], {'type': 'rect', 'box': rectangles[part_id]})
                for item in primitives) for part_id in (cabinet, power, connection)):
        return False

    def contains(parent, child):
        x, y, right, bottom = parent
        a, b, c, d = child
        return x < a < c < right and y < b < d < bottom

    graph = view['diagram']
    nodes = {item['id']: item for item in graph['nodes']}
    monitor_node = [node for node in nodes.values() if (monitor, 'name') in TOKEN.findall(node['text'])]
    if len(monitor_node) != 1:
        return False
    unit = monitor_node[0]
    texts = view_nodes(view, parts)
    power_connected = any(target == unit['id'] and contains(rectangles[power], nodes[source]['box'])
                          and '供电' in texts[source] for source, target in _visible_edges(view))
    communications = [item['box'] for item in nodes.values() if '通信往返' in texts[item['id']]]
    linked = any(item['type'] == 'polyline' and not item.get('arrow') and any(
        _inside_shape(item['points'][0], {'type': 'rect', 'box': unit['box']})
        and _inside_shape(item['points'][-1], {'type': 'rect', 'box': other}) for other in communications)
        for item in primitives)
    return (contains(rectangles[cabinet], rectangles[power]) and contains(rectangles[cabinet], rectangles[connection])
            and not _overlap(rectangles[power], rectangles[connection])
            and contains(rectangles[connection], unit['box']) and power_connected and linked)


def _cabinet_supply_views(views, parts):
    required = {'保护单元', '监测控制单元', '负载', '供电板'}
    for view in views:
        if view.get('delivered_type') not in {'block_diagram', 'circuit', 'relationship'}:
            continue
        roles = role_nodes(view, parts, (*sorted(required), '主母线', '辅助供电轨'))
        texts = view_nodes(view, parts)
        functional = [ident for ident, label in texts.items() if label == '主供电']
        if '主母线' not in roles and len(functional) == 1:
            roles['主供电'] = (None, functional[0])
        origin = '主母线' if '主母线' in roles else '主供电'
        if not required <= roles.keys() or origin not in roles:
            continue
        pairs = {
            (origin, '保护单元'), ('保护单元', '监测控制单元'), ('保护单元', '负载'),
        }
        if '辅助供电轨' in roles:
            pairs.update({('供电板', '辅助供电轨'), ('辅助供电轨', '监测控制单元')})
        else:
            pairs.add(('供电板', '监测控制单元'))
        expected = {(roles[start][1], roles[end][1]) for start, end in pairs}
        actual = _visible_edges(view)
        graph = view['diagram']
        yield view['id'], '辅助供电轨' in roles, actual == expected and len(graph['edges']) == len(expected)


def source_view_coverage(obligations, plan):
    """Qualitative source obligations are covered only by eligible graphic objects."""
    views = [view for view in plan['views'] if view['engine'] == 'vector' and not view.get('source_region_id')]
    parts = {part['id']: part for part in plan['parts']}
    checks = {
        'sync_acknowledgment': ('SYNC-ACKNOWLEDGMENT', '待补终端本地入队、去重及收到确认才移除的独立流程图。'),
        'sync_incremental': ('SYNC-INCREMENTAL', '待补增量拉取、应用和未确认队列保护的独立流程图。'),
        'sync_field_conflict': ('SYNC-FIELD-CONFLICT', '待补同字段冲突保留两个版本并提示选择的独立流程图。'),
        'sync_account_isolation': ('SYNC-ACCOUNT-ISOLATION', '待补独立的各账号与各自队列的隔离视图。'),
        'cabinet_functional_zones': ('CABINET-FUNCTIONAL-ZONES', '待补供电列、连接列与监测控制单元的柜内功能分区图。'),
    }
    scopes = {key: [] for key in checks if key in obligations}
    if any(key.startswith('sync_') for key in scopes):
        for view in views:
            roles = _sync_flow_roles(view, parts)
            if len(roles) == 1 and roles[0] in scopes:
                scopes[roles[0]].append(view['id'])
    if 'cabinet_functional_zones' in scopes:
        scopes['cabinet_functional_zones'] = [view['id'] for view in views if _cabinet_zones(view, parts)]
    findings = []
    placements = []
    for ident, view_ids in scopes.items():
        row = obligations[ident]
        placements.append({**row, 'requirement_id': ident, 'covered_views': view_ids,
                           'coverage_proof': 'verified_vector_geometry' if view_ids else 'absent',
                           'visual_verdict': 'UNKNOWN'})
        if not view_ids:
            rule, message = checks[ident]
            findings.append({'rule': rule, 'verdict': 'FAIL', 'requirement_id': ident, 'page_id': row['page_id'],
                             'message': message, 'missing_views': [ident]})

    isolation = 'sync_account_isolation'
    if isolation in scopes:
        for view in views:
            if account_isolation_candidate(view, parts) and view['id'] not in scopes[isolation]:
                findings.append({'rule': checks[isolation][0], 'verdict': 'FAIL', 'requirement_id': isolation,
                                 'page_id': obligations[isolation]['page_id'], 'view_id': view['id'],
                                 'message': '隔离图须显示每个账号到本账号队列的独立归属关系。'})

    rail = obligations.get('cabinet_auxiliary_rail')
    if rail:
        gates = list(_cabinet_supply_views(views, parts))
        confirmed = obligations.get('cabinet_rail_path')
        verified = [ident for ident, has_rail, valid in gates if valid and has_rail == bool(confirmed)]
        invalid = [ident for ident, has_rail, valid in gates if not valid or has_rail != bool(confirmed)]
        for view_id in invalid:
            findings.append({'rule': 'CABINET-SUPPLY-DIRECTION', 'verdict': 'FAIL', 'view_id': view_id,
                             'page_id': rail['page_id'], 'requirement_id': rail['id'],
                             'message': '电源节点或单向供电连接与当前已核来源不一致，请核对端点和方向。'})
        if not verified and not invalid:
            findings.append({'rule': 'CABINET-SUPPLY-DIRECTION', 'verdict': 'FAIL',
                             'page_id': rail['page_id'], 'requirement_id': rail['id'],
                             'message': '待补有明确端点的主供电及辅助供电关系图。'})
        if not confirmed:
            scope = {'view_id': verified[0]} if verified else {}
            findings.append({'rule': 'CABINET-AUXILIARY-RAIL', 'verdict': 'UNKNOWN', 'page_id': rail['page_id'],
                             'requirement_id': rail['id'], 'blocks_view': False, **scope,
                             'missing_components': ['辅助供电轨接入端', '辅助供电轨出线端', '供电方向及接线关系'],
                             'message': '辅助供电轨两端、供电方向和接线关系待原理图或具名来源核对。'})
        placements.append({**(confirmed or rail), 'requirement_id': rail['id'],
                           'covered_views': verified if confirmed else [], 'engineering_views': verified,
                           'coverage_proof': 'verified_vector_geometry' if confirmed and verified else 'source_pending',
                           'visual_verdict': 'UNKNOWN'})
    return findings, placements


def source_drawing_gaps(bundle, plan, technical_types):
    """Identify missing visible source obligations after normal diagram validation."""
    obligations = {row['id']: row for row in domain_drawing_obligations(bundle, technical_types)}
    views = [view for view in plan['views'] if view['engine'] == 'vector']
    parts = {part['id']: part for part in plan['parts']}
    gaps = []

    def report(ident, rule, message, **scope):
        gaps.append({'rule': rule, 'verdict': 'FAIL', 'page_id': obligations[ident]['page_id'],
                     'message': message, **scope})

    reversed_positions = 'interlock_reversed_positions'
    both_open = 'interlock_both_open'
    if reversed_positions in obligations or both_open in obligations:
        groups = _interlock_groups(views, parts)
        if reversed_positions in obligations:
            for position, expected in [('一', {'一': {'blocked'}, '二': {'allowed'}}),
                                       ('二', {'一': {'allowed'}, '二': {'blocked'}})]:
                if _rod_actions(groups[position]) != expected:
                    report(reversed_positions, 'INTERLOCK-STATE',
                           '第' + position + '锁定位置缺可辨认的两控制杆阻挡与让行关系。')
        if both_open in obligations and not any(
                re.search(r'双断|两(?:个)?控制杆.{0,12}(?:均断开|都断开|均受阻)|两个开关.{0,8}(?:均|都)断开', line)
                for line, *_ in groups['中']):
            report(both_open, 'INTERLOCK-STATE', '中间位置缺可辨认的双断状态。')

    travel_rules = {'一': 'interlock_reversed_travel', '二': 'interlock_reversed_travel', '中': 'interlock_middle_travel'}
    if any(rule in obligations for rule in travel_rules.values()):
        verified = {position for position, shapes in _interlock_shape_groups(views, parts)
                    if position in travel_rules and _interlock_travel(position, shapes)}
        for position, rule in travel_rules.items():
            if rule in obligations and position not in verified:
                if position == '中':
                    message = '中间位置须以有引线的阻挡边缘接触两杆顶边，且线段不得穿入杆体。'
                else:
                    message = f'第{position}锁定位置须以有引线的边缘接触受阻杆顶边，并保留另一杆的向上让行路径。'
                report(rule, 'INTERLOCK-GEOMETRY', message)

    chain = 'visible_conductive_chain'
    if chain in obligations:
        sections = [view for view in views if view['delivered_type'] in {'layer_section', 'section'}]
        if sections or (bundle['figure_presence'] == 'absent' and len(views) == len(plan['views'])):
            seen = {primitive['part'] for view in sections for primitive in view['diagram'].get('primitives', [])
                    if primitive['type'] == 'label'}
            missing = [name for name in _SECTION_MARKS if not any(
                name in compact(part['name']) for ident, part in parts.items() if ident in seen)]
            if missing:
                report(chain, 'SECTION-VISIBLE-MARK', '分层剖面缺实际形状引线标号：' + '、'.join(missing) + '。',
                       missing_components=missing)
    process = 'package_process_order'
    if process in obligations and not any(_package_process_visible(view, parts) for view in views
                                          if view['delivered_type'] in {'process_flow', 'flowchart'}):
        report(process, 'PACKAGE-PROCESS', '工艺流程缺再分布结构形成、导电柱设置及热压接合的实际顺序连线。')
    film = 'in_plane_heat_network'
    if film in obligations and _film_conditions_joined(bundle, obligations[film]['page_id']):
        bindings = _material_heat_bindings(bundle, plan)
        for view in views:
            if view['delivered_type'] in {'relationship', 'block_diagram'} and _orders_film_conditions(view, parts):
                report(film, 'MATERIAL-PARALLEL-CONDITIONS',
                       '图 ' + view['id'] + ' 将膜面取向和邻片接触画成串联。两者是共同条件，'
                       '请用并列条件或二维方向示意表达面内传热，并区分厚度方向。',
                       view_id=view['id'])
            elif _material_parallel_view(view, bindings):
                direction = material_parallel_heat(view, plan, bindings=bindings)
                if direction is False:
                    report(film, 'MATERIAL-HEAT-EFFECT-DIRECTION',
                           '图 ' + view['id'] + ' 须由两项并列条件分别指向传热网络，再由网络指向面内扩散。',
                           view_id=view['id'])
                elif direction is None:
                    report(film, 'MATERIAL-HEAT-ROLE-UNVERIFIED',
                           '图 ' + view['id'] + ' 暂无法核对四个节点，请明确标注膜面取向、邻片接触、连续传热网络及面内热扩散。',
                           view_id=view['id'])
        _, marks = normalize_material_relationship_marks(bundle, plan, bindings=bindings)
        if marks:
            report(film, 'MATERIAL-RELATION-MARKS',
                   '请把取向、接触、传热关系和扩散作用保留为图中文字，并从部件表移除对应的规划标号后重新导入。',
                   view_id=marks['nodes'][0]['view_id'])
    gaps.extend(source_view_coverage(obligations, plan)[0])
    gaps.extend(relationship_coverage(bundle, plan, technical_types)[0])
    return gaps

def coverage_requirements(bundle,plan):
    rows=[]
    for fact in plan.get('facts',[]):
        text=fact.get('text','');refs=fact.get('evidence',[])
        valid=fact.get('status')=='confirmed'and not any(x['verdict']=='FAIL'for x in validate_facts(bundle,[fact]))
        state='drawable_candidate'if valid else'conflict'if fact.get('status')=='conflict'else'not_confirmed'
        if valid and _UNKNOWN.search(text+' '+' '.join(e.get('quote','')for e in refs)):state='exists_geometry_unknown'
        required=['spatial','structure']if _PHYSICAL.search(text)else['flowchart']if set(plan.get('technical_types',[]))&{'computer_method','communication'}else['source_review']
        covered=[v['id']for v in plan.get('views',[])if fact['id']in v.get('required_facts',[])]
        rows.append({'fact_id':fact['id'],'source_refs':refs,'evidence_state':state,'required_views':required,
            'covered_views':covered,'coverage_proof':'declared_only','visual_verdict':'UNKNOWN',
            'missing_input':'需明确相关接口形式、位置、隐藏几何或原始依据，不能由存在推断可绘。'if state!='drawable_candidate'else'需核对实际图件可见性及图文对应。'if not covered else'实际输出尚待视觉与专业核对。'})
    rows.extend(relationship_coverage(bundle, plan, plan.get('technical_types', ()))[1])
    obligations = {row['id']: row for row in domain_drawing_obligations(bundle, plan.get('technical_types', ()))}
    rows.extend(source_view_coverage(obligations, plan)[1])
    return rows


def change_impact(old,new):
    prior={p['id']:p for p in old.get('parts',[])};out=[]
    for p in new.get('parts',[]):
        a=prior.get(p['id'])
        if a and (a.get('label')!=p.get('label')or a.get('name')!=p.get('name')):
            out.append({'part_id':p['id'],'old_label':a.get('label'),'new_label':p.get('label'),'old_name':a.get('name'),'new_name':p.get('name'),
                'affected_views':[v['id']for v in new.get('views',[])if p['id']in v.get('parts',[])],
                'specification_sync':'UNKNOWN','source_modified':False,'followup':'同步检查正文、附图说明、标记表与图号引用；原交底不自动改写。'})
    return out

"""Deterministic qualitative figures from located disclosure relations."""
import copy

from .diagrams import TOKEN, _route, edge_geometry_fits, resolve, validate_diagram
from .errors import DrawingError
from .source_contract import compact
from .spatial_brief import panel_range_diagram


def role_parts(plan, roles):
    bound = {}
    for role in roles:
        matches = [part['id'] for part in plan['parts'] if compact(part['name']) == role]
        if len(matches) != 1:
            return {}
        bound[role] = matches[0]
    return bound


def name(part):
    return '{{' + part + '.name}}'


def node(ident, text, box, *, kind='process'):
    return {'id': ident, 'text': text, 'box': box, 'type': kind}


def edge(source, target, **fields):
    return {'source': source, 'target': target, **fields}


def view(ident, kind, title, purpose, parts, nodes, edges, *, width=1300, height=850, primitives=()):
    return {'id': ident, 'title': title, 'purpose': purpose, 'instruction': purpose,
            'engine': 'vector', 'requested_type': kind, 'delivered_type': kind,
            'parts': list(dict.fromkeys(parts)), 'depends_on': [], 'required_facts': [],
            'diagram': {'width': width, 'height': height, 'nodes': nodes,
                        'edges': edges, 'primitives': list(primitives)}}


def append_view(plan, candidate, evidence):
    if len(plan['views']) >= 24 or any(item['id'] == candidate['id'] for item in plan['views']):
        return None
    fact_id = bind_evidence(plan, candidate, evidence)
    plan['views'].append(candidate)
    return {'view_id': candidate['id'], 'source_fact_id': fact_id, 'action': 'add_qualitative_view'}


def bind_evidence(plan, view, evidence, *, suffix=''):
    base = view['id'][:42] + '_source' + suffix
    ident = base
    existing = {row['id']: row for row in plan['facts']}
    index = 1
    facts = '。'.join(compact(row['quote']) for row in evidence)
    while ident in existing and (existing[ident]['text'] != facts or existing[ident]['evidence'] != evidence):
        index += 1
        ident = f'{base}_{index}'
    if ident not in existing:
        plan['facts'].append({'id': ident, 'status': 'confirmed', 'text': facts, 'evidence': evidence})
    if ident not in view['required_facts']:
        view['required_facts'].append(ident)
    return ident


def account_view(plan):
    roles = role_parts(plan, ('账号', '本地待同步队列'))
    if roles:
        first, second = (name(roles['账号']) + suffix for suffix in ('甲', '乙'))
        queue = roles['本地待同步队列']
    else:
        roles = role_parts(plan, ('任一账号', '其他账号', '本地待同步队列'))
        if not roles:
            return None
        first, second = name(roles['任一账号']) + '甲', name(roles['其他账号']) + '乙'
        queue = roles['本地待同步队列']
    nodes = [
        node('account_a', first, [90, 155, 390, 275]),
        node('queue_a', first + '的' + name(queue), [750, 155, 1220, 275]),
        node('account_b', second, [90, 475, 390, 595]),
        node('queue_b', second + '的' + name(queue), [750, 475, 1220, 595]),
    ]
    return view('account_queue_isolation', 'flowchart', '账号与队列隔离关系图',
                '不同账号分别对应各自的待同步队列，图中用甲、乙区分账号，账号切换时保持队列隔离。',
                roles.values(), nodes,
                [edge('account_a', 'queue_a', label='归属', label_at=[545, 194]),
                 edge('account_b', 'queue_b', label='归属', label_at=[545, 514])], height=720)


def cabinet_view(plan):
    roles = role_parts(plan, ('电气连接柜', '供电列', '连接列', '监测控制单元'))
    if not roles:
        return None
    cabinet, power, connection, monitor = roles.values()
    boxes = [
        ('cabinet_boundary', cabinet, [55, 65, 1245, 770]),
        ('supply_boundary', power, [110, 185, 500, 570]),
        ('connection_boundary', connection, [765, 185, 1190, 570]),
    ]
    primitives = []
    for ident, part, (left, top, right, bottom) in boxes:
        primitives.extend([
            {'id': ident, 'type': 'rect', 'part': part, 'fill': 'none', 'box': [left, top, right, bottom]},
            {'type': 'text', 'at': [left + 26, top + 30], 'text': name(part), 'font_size': 30},
        ])
    primitives.append({'type': 'polyline', 'id': 'communication_association',
                       'points': [[977, 470], [977, 635]], 'stroke_width': 3})
    nodes = [
        node('supply_function', '主供电功能', [155, 350, 450, 470]),
        node('monitor', name(monitor), [810, 350, 1144, 470]),
        node('communication_function', '通信往返路径', [810, 635, 1144, 725]),
    ]
    return view('cabinet_functional_zones', 'block_diagram', '柜内功能分区示意图',
                '外框表示电气连接柜，内框分别表示供电列和连接列。连接列内的监测控制单元关联供电和通信功能。'
                '箭线与连接线只表示功能关系，具体供电及通信路径分别见对应附图。',
                roles.values(), nodes, [edge('supply_function', 'monitor', label='供电', label_at=[590, 365])],
                primitives=primitives)



def functional_supply_view(plan, candidate, known_path):
    roles = role_parts(plan, ('保护单元', '监测控制单元', '负载', '供电板'))
    if not roles:
        return None
    p, m, load, board = map(name, roles.values())
    nodes = [
        node('supply', '主供电', [75, 220, 315, 320]),
        node('protection', p, [475, 220, 744, 320]),
        node('monitor', m, [938, 220, 1230, 320]),
        node('load', load, [470, 60, 748, 160]),
        node('board', board, [75, 365, 315, 465]),
    ]
    edges = [edge('supply', 'protection'), edge('protection', 'monitor'), edge('protection', 'load')]
    start = 'board'
    auxiliary = '供电板提供辅助供电。'
    if known_path:
        rail = role_parts(plan, ('辅助供电轨',))
        if not rail:
            return None
        roles.update(rail)
        start = 'rail'
        nodes.append(node(start, name(rail['辅助供电轨']), [475, 365, 744, 465]))
        edges.append(edge('board', start))
        auxiliary = '供电板输出端经辅助供电轨向监测控制单元的辅助电源输入端供电。'
    x = 744 if known_path else 315
    edges.append(edge(start, 'monitor', points=[[x, 415], [1084, 415], [1084, 320]]))
    return view(candidate['id'], 'block_diagram', '供电功能关系示意图',
                '主供电经保护单元送至监测控制单元和负载。' + auxiliary +
                '箭线表示已核供电关系，主母线实际接线待补证。',
                roles.values(), nodes, edges, height=670)

def auxiliary_rail_projection(plan, known_path):
    names = ('辅助供电轨', '供电板', '监测控制单元')
    roles = role_parts(plan, names)
    if not roles:
        return []
    rail, board, monitor = (roles[role] for role in names)
    changes = []
    for candidate in plan['views']:
        if candidate['engine'] != 'vector' or candidate.get('source_region_id') or rail not in candidate['parts']:
            continue
        graph = copy.deepcopy(candidate['diagram'])
        nodes = graph['nodes']
        by_id = {item['id']: item for item in nodes}

        def nodes_for(part):
            return [item for item in nodes if (part, 'name') in TOKEN.findall(item['text'])
                    or compact(resolve(item['text'], {row['id']: row for row in plan['parts']}))
                    == next(row['name'] for row in plan['parts'] if row['id'] == part)]

        rail_nodes, boards, monitors = (nodes_for(part) for part in (rail, board, monitor))
        if len(rail_nodes) != 1:
            continue
        rail_node = rail_nodes[0]
        rail_id = rail_node['id']
        if known_path:
            if len(boards) != 1 or len(monitors) != 1 or any(
                    rail_id in (item['source'], item['target']) for item in graph['edges']):
                continue
            board_node, monitor_node = boards[0], monitors[0]
            direct = [item for item in graph['edges'] if item['source'] == board_node['id']
                      and item['target'] == monitor_node['id'] and item.get('label') == '辅助供电'
                      and not item.get('bidirectional')]
            if len(direct) != 1:
                continue
            right = graph['width'] - 65
            route = _route(rail_node, monitor_node)
            if not edge_geometry_fits(route, rail_node, monitor_node, nodes):
                r, m = rail_node['box'], monitor_node['box']
                ry, my = (r[1] + r[3]) / 2, (m[1] + m[3]) / 2
                route = [[r[2], ry], [right, ry], [right, my], [m[2], my]]
            index = graph['edges'].index(direct[0])
            graph['edges'][index:index + 1] = [
                edge(board_node['id'], rail_id),
                edge(rail_id, monitor_node['id'], points=route),
            ]
            projection = {**candidate, 'diagram': graph}
            if not all(edge_geometry_fits(item.get('points', _route(by_id[item['source']], by_id[item['target']])),
                                          by_id[item['source']], by_id[item['target']], nodes)
                       for item in graph['edges'][index:index + 2]):
                continue
            action = 'show_source_bound_rail'
        else:
            if rail_node.get('type') != 'process' or any(item.get('part') == rail for item in graph['primitives']):
                continue
            # Keep independently supported supply edges; unsupported rail connectors
            # stay in the immutable model response and localized coverage receipt.
            graph['nodes'] = [item for item in nodes if item['id'] != rail_id]
            graph['edges'] = [item for item in graph['edges'] if rail_id not in (item['source'], item['target'])]
            projection = {**candidate, 'diagram': graph, 'parts': [pid for pid in candidate['parts'] if pid != rail]}
            action = 'withhold_unbound_rail'
        try:
            validate_diagram(plan, projection)
        except DrawingError:
            continue
        candidate.update(projection)
        change = {'view_id': candidate['id'], 'part_id': rail, 'action': action}
        if known_path:
            change['source_fact_id'] = bind_evidence(plan, candidate, known_path, suffix='_rail')
        changes.append(change)
    return changes


def beam_views(plan):
    roles = role_parts(plan, ('发射设备', '接收设备', '参考信号', '测量反馈'))
    if not roles:
        return ()
    tx, rx, reference, feedback = (name(pid) for pid in roles.values())
    optional = role_parts(plan, ('接收波束候选',))
    roles.update(optional)
    select = name(optional['接收波束候选']) if optional else '接收波束候选'
    interaction = view(
        'beam_device_interaction', 'relationship', '发射设备与接收设备交互图',
        '发射设备向接收设备发送范围指示和参考信号，接收设备向发射设备回传测量反馈。',
        [roles['发射设备'], roles['接收设备']],
        [node('transmitter', tx, [90, 250, 390, 505], kind='actor'),
         node('receiver', rx, [910, 250, 1210, 505], kind='actor')],
        [edge('transmitter', 'receiver', label='范围指示', points=[[390, 300], [910, 300]], label_at=[590, 261]),
         edge('transmitter', 'receiver', label='参考信号', points=[[390, 380], [910, 380]], label_at=[590, 341]),
         edge('receiver', 'transmitter', label='测量反馈', points=[[910, 460], [390, 460]], label_at=[590, 421])],
        height=700,
    )
    nodes = [
        node('send_range', '发送范围指示', [80, 120, 485, 235]),
        node('select_beam', '根据范围选择' + select, [820, 120, 1220, 235]),
        node('send_reference', '发送' + reference, [80, 365, 485, 480]),
        node('measure', '使用选定波束接收\n' + reference + '并测量', [820, 365, 1220, 480]),
        node('receive_feedback', '接收' + feedback, [80, 610, 485, 725]),
        node('send_feedback', '回传' + feedback, [820, 610, 1220, 725]),
    ]
    sequence = view(
        'beam_message_dependencies', 'sequence', '波束消息逻辑次序图',
        '范围指示用于选择接收波束，参考信号和选定波束共同用于接收测量，测量后回传反馈。'
        '箭线表示已披露的依赖，节点的纵向位置不表示空口时间或等待条件。',
        roles.values(), nodes,
        [edge('send_range', 'select_beam', label='范围指示', label_at=[590, 133]),
         edge('send_reference', 'measure', label=reference, label_at=[590, 378]),
         edge('select_beam', 'measure'), edge('measure', 'send_feedback'),
         edge('send_feedback', 'receive_feedback', label=feedback, label_at=[590, 623])],
        primitives=[
            {'type': 'text', 'at': [282, 55], 'text': tx, 'anchor': 'middle', 'font_size': 28},
            {'type': 'text', 'at': [1020, 55], 'text': rx, 'anchor': 'middle', 'font_size': 28},
        ],
    )
    measurement = view(
        'beam_receive_measurement', 'flowchart', '接收选束与测量流程图',
        '接收设备依据范围指示选择波束，使用选定波束接收参考信号并测量，再回传测量反馈。',
        [roles['接收设备'], roles['参考信号'], roles['测量反馈'], *optional.values()],
        [node('select', rx + '根据范围选择' + select, [285, 75, 1020, 185]),
         node('measure', '接收并测量（选定波束，' + reference + '）', [285, 268, 1020, 378]),
         node('feedback', '回传' + feedback, [285, 460, 1020, 570])],
        [edge('select', 'measure'), edge('measure', 'feedback')], height=645)
    return interaction, sequence, measurement



def remove_angle_inventory_only_transmitter(plan, angle):
    tx = role_parts(plan, ('发射设备',)).get('发射设备')
    if tx not in angle['parts'] or not angle.get('diagram') or angle.get('source_region_id'):
        return False
    graph = angle['diagram']
    objects = graph['nodes'] + graph['edges'] + graph['primitives']
    if any(item.get('part') == tx
           or any(ref == tx for field in ('text', 'label') for ref, _ in TOKEN.findall(item.get(field, '')))
           for item in objects):
        return False
    if any(item.get('part_id') == tx for item in angle.get('annotations', [])):
        return False
    angle['parts'].remove(tx)
    return True

def beam_panel_marks(plan, view_ids):
    parts = {part['id']: part for part in plan['parts']}
    changes = []
    for view_ in plan['views']:
        if view_['id'] not in view_ids or not view_.get('diagram'):
            continue
        primitives = view_['diagram']['primitives']
        if len(primitives) != 5 or not all(parts.get(item.get('part'), {}).get('label') for item in primitives):
            continue
        panel, angle, beam = (primitives[i]['part'] for i in (0, 1, 3))
        if view_['diagram'] != panel_range_diagram(panel, angle, beam):
            continue
        for index, ident, target, at in [
            (0, 'panel_outline', [200, 255], [175, 163]),
            (1, 'range_boundary', [930, 209], [970, 132]),
            (3, 'beam_direction', [850, 336], [865, 271]),
        ]:
            shape = primitives[index]
            shape['id'] = ident
            primitives.append({'type': 'label', 'part': shape['part'], 'at': at,
                               'target': target, 'target_shape': ident, 'font_size': 34})
            changes.append({'view_id': view_['id'], 'part_id': shape['part'], 'target': ident})
    return changes

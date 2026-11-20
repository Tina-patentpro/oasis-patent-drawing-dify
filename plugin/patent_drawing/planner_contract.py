"""Auditable representation/layout adaptation, never technical-plan repair."""
import copy,json,hashlib,math,re
from .coverage import (CONTROL_VIEWS, SEQUENCE_VIEWS, account_isolation_candidate, beam_message_intent, communication_link_groups,
                       _cabinet_supply_views, pwm_nodes, relationship_coverage, source_view_coverage, view_nodes)
from .diagrams import (_crosses, _overlap, _point, _route, _text_box, edge_geometry_fits,
                       reanchor_edge, resolve, validate_diagram)
from .errors import DrawingError
from .natural_views import account_view, append_view, auxiliary_rail_projection, beam_views, cabinet_view, functional_supply_view, bind_evidence, remove_angle_inventory_only_transmitter, beam_panel_marks
from .source_contract import BEAM_MESSAGES, MODE_COMPONENTS, compact, drawing_relationships, source_view_evidence, validate_facts


def complete_source_views(bundle, plan, technical_types):
    """Apply bounded local source geometry before candidate review and number binding."""
    if bundle.get('figure_presence') != 'absent':
        return []
    sources = source_view_evidence(bundle, technical_types)
    obligations = {ident: {'id': ident, 'page_id': evidence[0]['page_id'], 'evidence': evidence}
                   for ident, evidence in sources.items()}
    changes = []
    for view in plan['views']:
        if view['engine'] != 'vector' or 'sync_acknowledgment' not in sources:
            continue
        for node in view['diagram'].get('nodes', ()):
            before = node['text']
            if re.search(r'接收(?:并|和)应用\s*非重复操作', before):
                node['text'] = re.sub(r'接收(?:并|和)应用', '接收', before)
                changes.append({'view_id': view['id'], 'node_id': node['id'], 'before': before,
                                'after': node['text'], 'evidence': sources['sync_acknowledgment'],
                                'action': 'limit_nonrepeat_to_source'})
    gaps, _ = source_view_coverage(obligations, plan)
    missing = {item['requirement_id'] for item in gaps}
    if 'sync_account_isolation' in missing and (candidate := account_view(plan)):
        parts = {p['id']: p for p in plan['parts']}
        existing_views = [row for row in plan['views'] if row['engine'] == 'vector'
                          and not row.get('source_region_id') and account_isolation_candidate(row, parts)]
        if len(existing_views) == 1 and not any(existing_views[0]['diagram'][key] for key in ('nodes', 'edges')):
            existing = existing_views[0]
            candidate.update(id=existing['id'], depends_on=existing['depends_on'])
            fact_id = bind_evidence(plan, candidate, sources['sync_account_isolation'])
            existing.update(candidate)
            changes.append({'view_id': candidate['id'], 'source_fact_id': fact_id,
                            'action': 'draw_account_ownership'})
        elif not existing_views and (change := append_view(plan, candidate, sources['sync_account_isolation'])):
            changes.append(change)
    if ('cabinet_functional_zones' in missing and (candidate := cabinet_view(plan))
            and (change := append_view(plan, candidate, sources['cabinet_functional_zones']))):
        changes.append(change)
    if 'cabinet_auxiliary_rail' in sources:
        known_path = sources.get('cabinet_rail_path')
        changes.extend(auxiliary_rail_projection(plan, known_path))
        parts = {p['id']: p for p in plan['parts']}
        eligible = [v for v in plan['views'] if v['engine'] == 'vector' and not v.get('source_region_id')]
        gates = list(_cabinet_supply_views(eligible, parts))
        failed = [fid for fid, has_rail, valid in gates if not valid or has_rail != bool(known_path)]
        if (len(failed) == 1 and len(gates) == 1 and 'cabinet_functional_zones' in sources):
            existing = next(v for v in eligible if v['id'] == failed[0])
            if candidate := functional_supply_view(plan, existing, known_path):
                evidence = sources['cabinet_functional_zones'] + sources['cabinet_auxiliary_rail'] + (known_path or [])
                fact_id = bind_evidence(plan, candidate, evidence)
                existing.update(candidate)
                changes.append({'view_id': existing['id'], 'source_fact_id': fact_id,
                                'action': 'draw_source_bound_supply_functions'})
        constraint = '柜内图仅表示有据的功能分区。实际安装坐标、列数、容量、柜体尺寸、剖面和布线位置待补证。'
        if constraint not in plan['unresolved'] and len(plan['unresolved']) < 80:
            plan['unresolved'].append(constraint)
    beam = next((row for row in drawing_relationships(bundle, technical_types) if row['kind'] == 'beam'), None)
    if beam:
        eligible = {view['id'] for view in plan['views']
                    if view['engine'] == 'vector' and view.get('delivered_type') in {'relationship', 'state'}
                    and not view.get('source_region_id')}
        for mark in beam_panel_marks(plan, eligible):
            changes.append({**mark, 'action': 'mark_source_angle_geometry'})
        _, bindings = relationship_coverage(bundle, plan, technical_types)
        roles = {item['role']: item['view_id'] for row in bindings if row['id'] == beam['id']
                 for item in row['placements']}
        if '相对角度' in roles:
            angle = next(view for view in plan['views'] if view['id'] == roles['相对角度'])
            if remove_angle_inventory_only_transmitter(plan, angle):
                changes.append({'view_id': angle['id'], 'action': 'align_angle_inventory_to_geometry'})
            for role, candidate in zip(('设备交互', '消息次序', '接收与测量'), beam_views(plan)):
                if role not in roles and (change := append_view(plan, candidate, beam['evidence'])):
                    changes.append(change)
    return changes


def coating_pore_section(roles):
    """Qualitative section with a real gap in the top boundary."""
    substrate, coating, open_pore, closed_pore = (
        roles[k] for k in ('substrate', 'coating', 'open_pore', 'covered_pore')
    )
    primitives = [
        {'id': 'substrate', 'type': 'rect', 'part': substrate, 'box': [205, 450, 1070, 610]},
        {'id': 'coating', 'type': 'polyline', 'part': coating,
         'points': [[335, 260], [205, 260], [205, 450], [1070, 450], [1070, 260], [425, 260]]},
        {'id': 'open_pore', 'type': 'polyline', 'part': open_pore,
         'points': [[335, 260], [335, 300], [357, 350], [410, 350], [425, 300], [425, 260]]},
        {'id': 'covered_pore', 'type': 'ellipse', 'part': closed_pore, 'box': [745, 312, 845, 378]},
    ]
    for part, target, at, end in (
        (substrate, 'substrate', [1150, 530], [1070, 542]),
        (coating, 'coating', [95, 323], [205, 330]),
        (open_pore, 'open_pore', [278, 145], [335, 279]),
        (closed_pore, 'covered_pore', [920, 158], [832, 326]),
    ):
        primitives.append({'type': 'label', 'part': part, 'target_shape': target,
                           'at': at, 'target': end, 'font_size': 36})
    return {'width': 1320, 'height': 730, 'nodes': [], 'edges': [], 'primitives': primitives}


def bind_source_coating_sections(bundle, plan):
    if 'manufacturing_process' not in plan.get('technical_types', ()):
        return []
    facts = {f['id']: f for f in plan.get('facts', ())
             if f.get('status') == 'confirmed' and not validate_facts(bundle, [f])}
    parts = {p['id']: p['name'] for p in plan.get('parts', ())}
    changed = []
    for view in plan['views']:
        if (view['engine'] != 'vector' or view.get('delivered_type') != 'layer_section'
                or view.get('source_region_id') or view.get('depends_on')):
            continue
        quotes = [compact(row['quote']) for fid in view.get('required_facts', ()) if fid in facts
                  for row in facts[fid]['evidence']]
        if not (any('缸孔内表面' in quote and '铁系喷镀覆膜' in quote for quote in quotes)
                and any('部分表面孔隙' in quote and '被流动材料覆盖' in quote and '储油' in quote for quote in quotes)):
            continue
        role_terms = {'substrate': '缸膛基体', 'coating': '铁系喷镀覆膜',
                      'open_pore': '保留的孔隙', 'covered_pore': '被流动材料覆盖的孔隙'}
        candidates = {role: [pid for pid in view['parts'] if term == parts.get(pid)]
                      for role, term in role_terms.items()}
        if any(len(ids) != 1 for ids in candidates.values()) or len(view['parts']) != len(candidates):
            continue
        diagram = coating_pore_section({role: ids[0] for role, ids in candidates.items()})
        if view['diagram'] != diagram:
            view['diagram'] = diagram
            changed.append(view['id'])
    return changed


def bind_material_heat_schematic(bundle, plan, query=''):
    """Use a qualitative diagram only when the cited text supports both paths."""
    if ('chemical_material' not in plan.get('technical_types', ())
            or bundle.get('figure_presence') != 'absent'
            or re.search(r'Flare|Sunburst|位图|原图', query, flags=re.IGNORECASE)):
        return []
    facts = {row['id']: row for row in plan.get('facts', ())
             if row['status'] == 'confirmed' and not validate_facts(bundle, [row])}
    parts = {part['id']: part['name'] for part in plan.get('parts', ())}
    changed = []
    for view in plan['views']:
        if (view['engine'] != 'image' or view.get('requested_type') != 'relationship'
                or view.get('delivered_type') != 'relationship' or view.get('source_region_id')
                or view.get('depends_on') or view.get('annotations')):
            continue
        quotes = [compact(evidence['quote']) for fid in view.get('required_facts', ()) if fid in facts
                  for evidence in facts[fid]['evidence']]
        if not (any('邻接鳞片彼此接触' in quote and '沿膜面方向取向' in quote for quote in quotes)
                and any('传热网络' in quote and '局部热量沿膜面扩散' in quote for quote in quotes)):
            continue
        film = [pid for pid in view['parts'] if parts.get(pid) == '导热膜']
        flakes = [pid for pid in view['parts'] if parts.get(pid) == '鳞片状碳材料']
        if len(film) != 1 or len(flakes) != 1 or len(view['parts']) != 2:
            continue
        film, flake = film[0], flakes[0]
        root = [700, 380]
        primitives = [
            {'type': 'rect', 'id': 'film', 'part': film, 'box': [200, 258, 1200, 500]},
            {'type': 'polyline', 'part': flake, 'points': [[290, 366], [530, 366], [566, 406], [326, 406], [290, 366]]},
            {'type': 'polyline', 'id': 'flake_center', 'part': flake,
             'points': [[515, 366], [880, 366], [916, 406], [551, 406], [515, 366]]},
            {'type': 'polyline', 'part': flake, 'points': [[861, 366], [1110, 366], [1146, 406], [897, 406], [861, 366]]},
            {'type': 'polyline', 'points': [root, [376, 380]], 'arrow': True},
            {'type': 'polyline', 'points': [root, [1020, 380]], 'arrow': True},
            {'type': 'ellipse', 'box': [695, 375, 705, 385], 'fill': 'black'},
            {'type': 'polyline', 'points': [[300, 692], [670, 692]], 'arrow': True},
            {'type': 'polyline', 'points': [[300, 692], [300, 558]], 'arrow': True},
            {'type': 'text', 'at': [715, 675], 'text': '膜面方向', 'font_size': 38},
            {'type': 'text', 'at': [95, 565], 'text': '厚度方向', 'font_size': 38},
            {'type': 'label', 'part': film, 'target_shape': 'film', 'at': [130, 150],
             'target': [200, 258], 'font_size': 42},
            {'type': 'label', 'part': flake, 'target_shape': 'flake_center', 'at': [560, 151],
             'target': [615, 366], 'font_size': 42},
        ]
        view.update(engine='vector', diagram={'width': 1400, 'height': 820, 'primitives': primitives})
        changed.append(view['id'])
    return changed


def bind_disclosed_communication(bundle, plan, technical_types):
    relations = [row for row in drawing_relationships(bundle, technical_types, bind_instances=True)
                 if row['kind'] == 'communication']
    if not relations:
        return []
    parts = {p['id']: p for p in plan['parts']}
    adjusted = []
    for view in plan['views']:
        if view['engine'] != 'vector':
            continue
        diagram = copy.deepcopy(view['diagram'])
        for relation in relations:
            matched = communication_link_groups(view, parts, relation)
            if matched is None or not matched[1]:
                continue
            groups, _ = matched
            for indices in groups.values():
                if len(indices) != 1:
                    continue
                index = indices[0]
                edge = diagram['edges'][index]
                source, target = edge['source'], edge['target']
                if edge.get('bidirectional') or sum(
                        {item['source'], item['target']} == {source, target} for item in diagram['edges']) != 1:
                    continue
                edge['bidirectional'] = True
                try:
                    validate_diagram(plan, {**view, 'diagram': diagram})
                except DrawingError:
                    edge.pop('bidirectional')
                else:
                    adjusted.append({'view_id': view['id'], 'edge_index': index,
                                     'source': source, 'target': target})
        view['diagram'] = diagram
    return adjusted


def bind_disclosed_pwm_decision(bundle, plan, technical_types=None):
    """Draw the source-backed influence on PWM while retaining the existing graph."""
    types = plan.get('technical_types', ()) if technical_types is None else technical_types
    if not any(row['kind'] == 'pwm' for row in drawing_relationships(bundle, types)):
        return []
    modes = [part for part in plan['parts'] if compact(part['name']) == MODE_COMPONENTS[0]]
    if len(modes) != 1 or not modes[0]['label']:
        return []
    mode = modes[0]['id']
    parts = {part['id']: part for part in plan['parts']}
    adjusted = []
    for view in plan['views']:
        if (view['engine'] != 'vector' or view.get('delivered_type') not in CONTROL_VIEWS
                or view.get('source_region_id')):
            continue
        existing, output = pwm_nodes(view, parts)
        if not output:
            continue
        diagram = copy.deepcopy(view['diagram'])
        nodes = {node['id']: node for node in diagram['nodes']}
        edges = diagram['edges']
        target = nodes[output[1]]
        if target['type'] == 'decision':
            continue
        if existing:
            source = nodes[existing[1]]
            if source['type'] == 'decision' or any(
                    edge['source'] == source['id'] and edge['target'] == target['id'] for edge in edges):
                continue
            candidates = [source]
        else:
            # Any differently worded existing judgment presents a competing
            # interpretation. It needs review before a new node can be added.
            if any(re.search('调制模式判定|判断调制模式|选择PWM模式', value)
                   for value in view_nodes(view, parts).values()):
                continue
            node_id = 'disclosed_pwm_mode'
            if node_id in nodes:
                continue
            x, y, right, bottom = target['box']
            cx, cy = (x + right) / 2, (y + bottom) / 2
            boxes = [[right + gap, cy - 48, right + gap + 285, cy + 48] for gap in (85, 150)]
            boxes += [[x - gap - 285, cy - 48, x - gap, cy + 48] for gap in (85, 150)]
            boxes += [[cx - 142, y - gap - 96, cx + 143, y - gap] for gap in (85, 150)]
            boxes += [[cx - 142, bottom + gap, cx + 143, bottom + gap + 96] for gap in (85, 150)]
            candidates = [{'id': node_id, 'type': 'process', 'box': box,
                           'text': '{{' + mode + '.name}} {{' + mode + '.label}}'} for box in boxes]
        for source in candidates:
            points = _route(source, target)
            additional = [] if existing else [source]
            if not edge_geometry_fits(points, source, target, [*nodes.values(), *additional]):
                continue
            candidate = {**view, 'parts': list(dict.fromkeys([*view['parts'], mode])),
                         'diagram': {**diagram, 'nodes': [*nodes.values(), *additional],
                                     'edges': [*edges, {'source': source['id'], 'target': target['id']}]}}
            try:
                validate_diagram(plan, candidate)
            except DrawingError:
                continue
            view.update(candidate)
            adjusted.append(view['id'])
            break
    return adjusted


def bind_sequence_boundaries(bundle, plan, technical_types):
    """Repair a source-resolved message without changing its declared actors."""
    if not any(row['kind'] == 'beam' for row in drawing_relationships(bundle, technical_types)):
        return []
    parts = {p['id']: p for p in plan['parts']}
    changes = []
    for view in plan['views']:
        if view['engine'] != 'vector' or view.get('delivered_type') not in SEQUENCE_VIEWS:
            continue
        diagram = view['diagram']
        nodes = {node['id']: node for node in diagram['nodes']}
        view_changes = []
        for index, edge in enumerate(diagram['edges']):
            if not beam_message_intent(view, parts, edge):
                continue
            source, target = nodes[edge['source']], nodes[edge['target']]
            points = edge.get('points', _route(source, target))
            corrected = reanchor_edge(points, source, target, list(nodes.values()))
            if corrected is None or corrected == points:
                continue
            view_changes.append({'view_id': view['id'], 'edge_index': index,
                                 'source': edge['source'], 'target': edge['target'],
                                 'before': points, 'after': corrected})
            edge['points'] = corrected
        if view_changes:
            for move in place_labels(diagram, parts):
                view_changes.append({'view_id': view['id'], **move})
            changes.extend(view_changes)
    return changes


def bind_existing_technical_objects(bundle, plan):
    """Add labels to existing linework or an unambiguous operative node."""
    facts = {row['id']: row for row in plan.get('facts', ()) if row['status'] == 'confirmed'
             and not validate_facts(bundle, [row])}
    part_names = {part['id']: part['name'] for part in plan['parts']}
    changes = []
    for view in plan['views']:
        if view.get('engine') != 'vector':
            continue
        diagram = view['diagram']
        cited = ' '.join(compact(ev['quote']) for fid in view.get('required_facts', ()) if fid in facts
                         for ev in facts[fid]['evidence'])
        roles = {part_names[pid]: pid for pid in view['parts']}
        if ('electrical' in plan['technical_types'] and '基准' in cited and '短路' in cited
                and view.get('delivered_type') == 'flowchart' and roles.get('电流比较判定')):
            candidates = [node for node in diagram.get('nodes', [])
                          if node['type'] == 'process' and '与对应基准比较' in node['text']]
            if len(candidates) == 1:
                node = candidates[0]
                pid = roles['电流比较判定']
                token = '{{' + pid + '.label}}'
                if token not in node['text']:
                    node['text'] += '（' + token + '）'
                    changes.append({'view_id': view['id'], 'part_id': pid, 'target': node['id']})
        if ('semiconductor' in plan['technical_types'] and view.get('delivered_type') == 'layer_section'
                and '导电图案' in cited and '绝缘层' in cited and '再分布结构' in cited
                and (group := roles.get('再分布结构'))):
            scene = diagram.get('primitives', [])
            if not any(item.get('type') == 'label' and item['part'] == group for item in scene):
                members = [item for item in scene if item.get('part') in
                           {roles.get('导电图案'), roles.get('绝缘层')}
                           and item['type'] in {'rect', 'polyline'} and item.get('part')]
                extents = [item.get('points') or [item['box'][:2], item['box'][2:]] for item in members]
                if extents and {item['part'] for item in members} == {roles.get('导电图案'), roles.get('绝缘层')}:
                    points = [p for extent in extents for p in extent]
                    right = max(p[0] for p in points)
                    top, bottom = min(p[1] for p in points), max(p[1] for p in points)
                    if right + 126 < diagram['width'] and bottom - top >= 28:
                        target = [right + 24, (top + bottom) // 2]
                        bracket = {'type': 'polyline', 'id': 'rdl_group_extent', 'part': group,
                                   'points': [[right, top], [right+24, top], [right+24, bottom], [right, bottom]]}
                        label = {'type': 'label', 'part': group, 'target_shape': bracket['id'],
                                 'at': [right+68, target[1]-16], 'target': target, 'font_size': 32}
                        candidate = {**view, 'diagram': {**diagram, 'primitives': [*scene, bracket, label]}}
                        try:
                            validate_diagram(plan, candidate)
                        except DrawingError:
                            pass
                        else:
                            scene.extend([bracket, label])
                            changes.append({'view_id': view['id'], 'part_id': group, 'target': bracket['id']})
        if ('semiconductor' in plan['technical_types'] and view.get('delivered_type') == 'relationship'
                and '封装基板' in cited and ('热压' in cited or '底填' in cited)):
            scene = diagram.get('primitives', [])
            for name in ('再分布结构', '芯片连接端子', '中介基板'):
                pid = roles.get(name)
                if not pid or any(item.get('type') == 'label' and item['part'] == pid for item in scene):
                    continue
                lines = [item for item in scene if item.get('type') == 'polyline' and item.get('part') == pid
                         and item.get('id') and len(item['points']) == 2 and item['points'][0][1] == item['points'][1][1]]
                if len(lines) != 1:
                    continue
                line = lines[0]
                left = min(p[0] for p in line['points'])
                height = line['points'][0][1]
                label = {'type': 'label', 'part': pid, 'target_shape': line['id'],
                         'target': [left, height], 'at': [max(30, left - 120), height - 20], 'font_size': 34}
                candidate = {**view, 'diagram': {**diagram, 'primitives': [*scene, label]}}
                try:
                    validate_diagram(plan, candidate)
                except DrawingError:
                    continue
                scene.append(label)
                changes.append({'view_id': view['id'], 'part_id': pid, 'target': line['id']})
            underfill = roles.get('底部填充')
            if (underfill and '中介' in cited and '之间' in cited
                    and not any(item.get('type') == 'label' and item['part'] == underfill for item in scene)):
                bounds = [item for item in scene if item.get('type') == 'polyline'
                          and item.get('part') == underfill and len(item['points']) == 2
                          and item['points'][0][1] == item['points'][1][1]]
                if len(bounds) == 2:
                    upper, lower = sorted(bounds, key=lambda line: line['points'][0][1])
                    left, right = sorted(p[0] for p in upper['points'])
                    top, bottom = upper['points'][0][1], lower['points'][0][1]
                    if left >= 150 and top + 25 <= bottom and sorted(p[0] for p in lower['points']) == [left, right]:
                        target = [left - 24, (top + bottom) // 2]
                        bracket = {'type': 'polyline', 'id': 'optional_underfill_extent', 'part': underfill,
                                   'points': [[left, top], [left-24, top], [left-24, bottom], [left, bottom]]}
                        label = {'type': 'label', 'part': underfill, 'target_shape': bracket['id'],
                                 'at': [left-125, target[1]-15], 'target': target, 'font_size': 34}
                        candidate = {**view, 'diagram': {**diagram, 'primitives': [*scene, bracket, label]}}
                        try:
                            validate_diagram(plan, candidate)
                        except DrawingError:
                            pass
                        else:
                            scene.extend([bracket, label])
                            changes.append({'view_id':view['id'], 'part_id':underfill, 'target':bracket['id']})
            # Connector and dielectric are not drawn in this overview. Keep
            # them on the neighboring section where their anchors exist.
            omitted = [roles[name] for name in ('导电柱', '中介绝缘层') if name in roles and not any(
                       item.get('part') == roles[name] or '{{' + roles[name] + '.name}}' in item.get('text', '') for item in scene)]
            if omitted and all(any(other.get('delivered_type') == 'layer_section' and any(
                    item.get('type') == 'label' and item.get('part') == pid
                    for item in other.get('diagram', {}).get('primitives', ())) for other in plan['views']) for pid in omitted):
                view['parts'] = [pid for pid in view['parts'] if pid not in omitted]
                changes.append({'view_id': view['id'], 'registered_in_other_view': omitted})
    return changes


def normalize_proposal(raw):
    if not isinstance(raw,dict):raise ValueError('planner must return object')
    p=copy.deepcopy(raw);changes=[]
    for field in ['constraints','unresolved']:
        rows=p.get(field,[])
        if not isinstance(rows,list)or len(rows)>80:raise ValueError('constraint/unknown capacity')
        converted=[]
        for i,row in enumerate(rows):
            if isinstance(row,dict)and isinstance(row.get('text'),str):
                value=json.dumps(row,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
                changes.append({'path':field+'/'+str(i),'action':'lossless_object_to_json_string'})
            elif isinstance(row,str):value=row
            else:raise ValueError('constraint/unknown must be text or text record')
            if len(value)>1000:raise ValueError('constraint/unknown capacity')
            converted.append(value)
        p[field]=converted
    scene=p.get('scene')
    if scene is None and 'scene'in p:
        p.pop('scene');changes.append({'path':'scene','action':'null_to_absence'})
    elif isinstance(scene,dict)and 'instances'not in scene:
        relations=scene.get('relations',[])
        if isinstance(relations,list)and not any(isinstance(r,dict)and set(r)&{'type','subjects','object','base','bounds'}for r in relations):
            notes=p.setdefault('planning_notes',{})
            if not isinstance(notes,dict)or 'legacy_scene'in notes:raise ValueError('planning metadata collision')
            notes['legacy_scene']=p.pop('scene');changes.append({'path':'scene','action':'retain_non_geometric_metadata_in_planning_notes'})
    parts={x['id']:x for x in p.get('parts',[])}
    for v in p.get('views',[]):
        if v.get('engine')=='vector':
            for move in place_labels(v['diagram'],parts):changes.append({'path':'views/'+v['id']+'/diagram/edges/'+str(move.pop('edge')),**move})
            padding = extend_canvas_for_text(v, parts)
            if padding:
                changes.append({'path': 'views/'+v['id']+'/diagram', **padding})
    def sha(obj):return hashlib.sha256(json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    return p,{'adapter':'planner-representation-v1','input_sha256':sha(raw),'output_sha256':sha(p),'changes':changes,'semantic_rewrite':False}


def extend_canvas_for_text(view, parts):
    """Keep relative geometry and placement when text needs trailing space."""
    diagram = view['diagram']
    original = (diagram['width'], diagram['height'])
    if any(type(limit) is not int or not 600 <= limit <= 2400 for limit in original):
        return None
    width, height = original
    for item in diagram.get('primitives', []):
        if item.get('type') != 'text':
            continue
        at = item.get('at')
        size = item.get('font_size', 26)
        if (not isinstance(at, list) or len(at) != 2
                or any(type(n) not in (int, float) or not math.isfinite(n) for n in at)
                or type(size) is not int or not 14 <= size <= 52):
            continue
        value = resolve(item['text'], parts)
        left, top, right, bottom = _text_box(value, at, size, item.get('anchor', 'start'))
        if left < 0 or top < 0:
            return None
        width = max(width, math.ceil(right))
        height = max(height, math.ceil(bottom))
    if (width, height) == original or max(width, height) > 2400:
        return None
    try:
        validate_diagram({'parts': list(parts.values())}, view)
    except DrawingError as error:
        if error.category != 'set_contract_error' or error.public_message != '文字超出画布':
            raise
    else:
        return None
    diagram.update(width=width, height=height)
    return {'action': 'extend_canvas_for_text', 'before': list(original), 'after': [width, height]}


def place_labels(diagram,parts):
    """Only move label_at near its actual polyline; nodes/edges unchanged."""
    if not isinstance(diagram,dict)or len(diagram.get('nodes',[]))>48 or len(diagram.get('edges',[]))>96:raise ValueError('diagram layout capacity')
    nodes={n['id']:n for n in diagram.get('nodes',[])};occupied=[];moves=[]
    width,height=diagram['width'],diagram['height']
    edges = diagram.get('edges', [])
    paths = []
    for edge in edges:
        points = edge.get('points', _route(nodes[edge['source']], nodes[edge['target']]))
        if not isinstance(points, list) or not 2 <= len(points) <= 30:
            raise ValueError('invalid edge path before label layout')
        paths.append([_point(point, width, height) for point in points])
    segments = [(a, b) for points in paths for a, b in zip(points, points[1:])]
    for index,(e,points) in enumerate(zip(edges,paths)):
        decision = nodes[e['source']]['type'] == 'decision'
        label=('是'if e.get('branch')=='yes'else'否')if decision else resolve(e['label'],parts)if e.get('label')else''
        if not label:continue
        start,end=points[:2]
        old=e.get('label_at')
        if old is not None and (not isinstance(old,list)or len(old)!=2 or any(type(x)not in(int,float)or not math.isfinite(x)for x in old)or not(0<=old[0]<=width and 0<=old[1]<=height)):raise ValueError('label coordinates must be valid finite 2D points before layout')
        at=old or[(start[0]+end[0])/2,(start[1]+end[1])/2-22]
        def safe(point):
            box=_text_box(label,point,24)
            left, top, right, bottom = box
            # Reserve clearance for the arrowhead as well as the line.
            clearance = (left - 7, top - 7, right + 7, bottom + 7)
            return (0<=box[0]<box[2]<=width and 0<=box[1]<box[3]<=height
                    and not any(_overlap(box,n['box'])for n in nodes.values())
                    and not any(_overlap(box,other)for other in occupied)
                    and (not decision or not any(_crosses(a,b,clearance) for a,b in segments)))
        if not safe(at):
            candidates=[]
            # A short first segment can force the label onto the next stroke.
            # Search along this branch's own route without moving the line.
            route = points if decision else points[:2]
            for start,end in zip(route,route[1:]):
                for ratio in [0.5,0.25,0.75]:
                    x=start[0]+(end[0]-start[0])*ratio;y=start[1]+(end[1]-start[1])*ratio
                    candidates += [[x+dx,y+dy]for dx,dy in [(12,-34),(-36,-34),(12,8),(-36,8),(0,-50),(0,16),(-60,-22),(30,-22)]]
            if decision:
                candidates.sort(key=lambda p: (p[0] - at[0]) ** 2 + (p[1] - at[1]) ** 2)
            at=next((candidate for candidate in candidates if safe(candidate)),None)
            if at is None:raise ValueError('edge label has no safe local position; geometry requires review')
            e['label_at']=at;moves.append({'edge':index,'action':'move_label_only','before':old,'after':at})
        occupied.append(_text_box(label,at,24))
    return moves

import copy
import json

import pytest

from patent_drawing.errors import DrawingError, PlannerResultUnknown
from patent_drawing.diagrams import validate_diagram
from patent_drawing.operation_store import OperationStore
from patent_drawing.planner_contract import normalize_proposal
from patent_drawing.workspace import Workspace


INTERLOCK = ('配电盘内有机械互锁和电路。第一锁定位置阻挡第一控制杆并允许第二控制杆闭合。'
             '第二锁定位置两个控制杆的阻挡、让行关系反转。中间位置阻挡两个控制杆并保持双断。')
PACKAGE = ('中介基板带垂直贯穿电极。再分布结构包括导电图案和绝缘层。'
           '导电柱侧部设中介绝缘层。芯片连接端子接触导电柱。'
           '封装基板和中介结构之间可选底部填充。')
FILM = ('拟构建面内导热路径。使邻接鳞片彼此接触，并使其长轴主要沿膜面方向取向。'
        '相互接触形成连续传热网络，把局部热量沿膜面扩散，而不是主要依赖厚度方向导热。')


def make_plan(workspace, wid, technical_types, views, parts=()):
    return {'schema_version': 'drawing-set-2', 'set_id': 'source_fixture', 'revision': 1,
            'source_sha256': workspace.read(wid)['source_sha256'], 'title': '来源图组',
            'patent_type': 'invention', 'technical_types': technical_types, 'parts': list(parts),
            'facts': [], 'views': views, 'constraints': [], 'unresolved': []}


def vector(fid, kind, texts=(), parts=(), primitives=()):
    return {'id': fid, 'title': fid, 'purpose': '表达实际关系', 'engine': 'vector',
            'requested_type': kind, 'delivered_type': kind, 'parts': list(parts),
            'diagram': {'width': 900, 'height': 700, 'nodes': [], 'edges': [],
                        'primitives': [*primitives, *[
                            {'type': 'text', 'at': [130, 120 + i * 40], 'text': text}
                            for i, text in enumerate(texts)]]}}


def test_natural_flow_moves_a_branch_label_clear_of_its_line():
    view = vector('flow', 'flowchart')
    diagram = view['diagram']
    diagram.update(width=1400, height=1750, nodes=[
        {'id': 'question', 'type': 'decision', 'box': [430, 1260, 870, 1460],
         'text': '是否已确认？', 'branches': {'yes': 'done', 'no': 'keep'}},
        {'id': 'done', 'type': 'process', 'box': [60, 1520, 520, 1620], 'text': '更新记录'},
        {'id': 'keep', 'type': 'process', 'box': [800, 1520, 1280, 1620], 'text': '保留原记录'},
    ], edges=[
        {'source': 'question', 'target': 'done', 'branch': 'yes',
         'points': [[430, 1360], [290, 1360], [290, 1520]], 'label_at': [320, 1320]},
        {'source': 'question', 'target': 'keep', 'branch': 'no',
         'points': [[650, 1460], [650, 1490], [1040, 1490], [1040, 1520]], 'label_at': [740, 1450]},
    ])
    raw = {'parts': [], 'views': [view]}
    adjusted, evidence = normalize_proposal(raw)
    result = adjusted['views'][0]
    rendered_scene = validate_diagram(adjusted, result)
    yes, no = rendered_scene['edges']
    assert yes['label'] == '是' and no['label'] == '否'
    assert yes['label_at'] == [320, 1320]
    x, y = no['label_at']
    assert 665 <= x <= 1010 and (y + 31 < 1490 or y > 1490)
    assert raw['views'][0]['diagram']['edges'][1]['label_at'] == [740, 1450]
    assert result['diagram']['nodes'] == diagram['nodes']
    for edge, original in zip(result['diagram']['edges'], diagram['edges']):
        assert {k: v for k, v in edge.items() if k != 'label_at'} == {
            k: v for k, v in original.items() if k != 'label_at'}
    assert evidence['changes'] == [{
        'path': 'views/flow/diagram/edges/1', 'action': 'move_label_only',
        'before': [740, 1450], 'after': [x, y],
    }]


@pytest.mark.parametrize('chain', [True, False])
def test_film_relationship_keeps_orientation_and_contact_as_joint_conditions(tmp_path, chain):
    class Planner:
        def plan(self, system, user, **kwargs):
            assert json.loads(user)['technical_obligations'][0]['id'] == 'in_plane_heat_network'
            return {'text': json.dumps(proposal, ensure_ascii=False)}

    workspace = Workspace(OperationStore(tmp_path), 'owner', allow_models=True, text_client=Planner())
    types = ['chemical_material']
    wid = workspace.ingest([{'name': 'film.txt', 'blob': FILM.encode()}], technical_types=types)['workspace_id']
    view = vector('heat', 'relationship')
    positions = ([[120, 90, 780, 190], [120, 270, 780, 370], [120, 450, 780, 550]] if chain
                 else [[75, 90, 410, 190], [490, 90, 825, 190], [220, 390, 680, 500]])
    view['diagram']['nodes'] = [
        {'id': ident, 'text': text, 'type': 'process', 'box': box}
        for ident, text, box in zip(
            ['orientation', 'contact', 'heat'],
            ['邻接鳞片沿膜面取向', '鳞片彼此接触', '局部热量沿膜面扩散'],
            positions,
        )
    ]
    sources = [('orientation', 'contact'), ('contact', 'heat')] if chain else [
        ('orientation', 'heat'), ('contact', 'heat')]
    view['diagram']['edges'] = [{'source': start, 'target': end} for start, end in sources]
    proposal = make_plan(workspace, wid, types, [view])
    result = workspace.plan(wid, '规划一套专利附图。')
    offline = Workspace(workspace.store, workspace.owner)
    if chain:
        assert result['blocking_state'] == 'planning_coverage_incomplete'
        assert result['domain_findings'][0]['rule'] == 'MATERIAL-PARALLEL-CONDITIONS'
        assert result['domain_findings'][0]['view_id'] == 'heat'
        assert workspace.read(wid)['work'] is None
        with pytest.raises(ValueError, match='MATERIAL-PARALLEL-CONDITIONS'):
            offline.stage(wid, proposal)
    else:
        assert result['plan']['views'] == proposal['views']
        assert offline.stage(wid, proposal)['required_image_calls'] == 0


@pytest.mark.parametrize(('query', 'full_evidence', 'context_name', 'should_use_vector'), [
    ('规划一套专利附图，用一张图片示意面板与角范围的关系。', True, None, True),
    ('规划一套专利附图，指定用 Flare 生成位图。', True, None, False),
    ('规划一套专利附图，给图一加引出线。', True, None, False),
    ('规划一套专利附图，用一张图片示意面板与角范围的关系。', False, None, False),
    ('规划一套专利附图。', '，并把该范围指示发送给接收设备。', None, True),
    ('规划一套专利附图。', '，但这一角范围仅用于备选方案，实际配置尚未确认。', None, False),
    ('规划一套专利附图，用图片示意面板和波束范围。', True, '发射设备', True),
    ('规划一套专利附图，绘制发射设备的外形。', True, '发射设备', False),
    ('规划一套专利附图，用图片示意面板和波束范围。', True, '接收设备', False),
])
def test_natural_panel_plan_chooses_source_bound_vector(tmp_path, query, full_evidence, context_name, should_use_vector):
    import io
    import zipfile
    from xml.etree import ElementTree

    from PIL import Image
    from patent_drawing.visibility import panel_beam_issue

    calls = []

    class Planner:
        def plan(self, system, user, **kwargs):
            calls.append(json.loads(user)['query'])
            return {'text': json.dumps(raw, ensure_ascii=False)}

    workspace = Workspace(OperationStore(tmp_path), 'owner', allow_models=True, text_client=Planner())
    claim = '发射设备定义相对于自身天线面板的角度范围，其中包含至少两个发射波束方向'
    quote = (claim + (full_evidence if isinstance(full_evidence, str) else '。')
             if full_evidence else '发射设备描述相对于天线面板的角度范围和波束方向。')
    wid = workspace.ingest([{'name': 'communication.txt', 'blob': quote.encode()}],
                           technical_types=['communication'])['workspace_id']
    state = workspace.read(wid)
    part_ids = ['p', 'r', 'b']
    parts = [{'id': pid, 'label': str(i), 'name': name} for i, (pid, name) in enumerate(
        zip(part_ids, ['天线面板', '角度范围', '发射波束方向']), 1)]
    if context_name:
        part_ids.insert(0, 'context')
        parts.append({'id': 'context', 'label': '4', 'name': context_name})
    other = vector('sequence', 'sequence', texts=['设备间关系'])
    raw = make_plan(workspace, wid, ['communication'], [
        {'id': 'range', 'engine': 'image', 'requested_type': 'relationship', 'delivered_type': 'relationship',
         'purpose': '表达面板相对范围与波束方向。', 'instruction': '画定性范围图。',
         'parts': part_ids, 'required_facts': ['known']},
        other,
    ], parts)
    raw['facts'] = [{'id': 'known', 'status': 'confirmed', 'text': quote,
                     'evidence': [{'page_id': state['bundle']['pages'][0]['id'], 'quote': quote}]}]
    result = workspace.plan(wid, query)
    plan = result['plan']
    assert calls == [query]
    assert workspace.plan(wid, query)['plan'] == plan and calls == [query]
    assert plan['views'][1] == other and plan['facts'] == raw['facts'] and plan['parts'] == parts
    assert plan['views'][0]['parts'] == part_ids
    assert plan['views'][0]['engine'] == ('vector' if should_use_vector else 'image')
    if not should_use_vector:
        return

    assert result['adaptation']['source_bound_rendering']['changes'][0]['source_fact_ids'] == ['known']
    assert workspace.stage(wid, plan, image_budget=0)['required_image_calls'] == 0
    assert all(workspace.render(wid, fid)['model_calls'] == 0 for fid in ('range', 'sequence'))
    with zipfile.ZipFile(io.BytesIO(workspace.package(wid)['zip'])) as archive:
        svg = ElementTree.fromstring(archive.read('figures/range.svg'))
        image_bytes = archive.read('figures/range.png')
        assert '非比例' in archive.read('figure-descriptions.txt').decode()
        if context_name:
            assert '矩形表示发射设备的天线面板' in archive.read('figure-descriptions.txt').decode()
    rays = [node.attrib['points'].split() for node in svg if node.tag.endswith('polyline')]
    heads = [node.attrib['points'].split() for node in svg if node.tag.endswith('polygon')]
    assert len(rays) == 4 and len(heads) == 2 and len({line[0] for line in rays}) == 1
    def point(token):
        return [round(float(n)) for n in token.split(',')]
    geometry = {'panel': plan['views'][0]['diagram']['primitives'][0]['box'], 'origin': point(rays[0][0]),
                'boundaries': [point(ray[-1]) for ray in rays[:2]],
                'beams': [{'tip': point(head[0]), 'head': [point(p) for p in head[1:]]} for head in heads]}
    with Image.open(io.BytesIO(image_bytes)) as image:
        assert panel_beam_issue({'region': [0, 0, *image.size], 'beam_geometry': geometry}, image.size, image,
                                allow_midpoints=True, require_full_panel=True, require_clean_base=True,
                                require_outward=True, require_connected_root=True) is None


def test_interlock_sensor_only_is_rejected_and_actual_state_labels_pass(tmp_path):
    workspace = Workspace(OperationStore(tmp_path), 'owner')
    wid = workspace.ingest([{'name': 'interlock.txt', 'blob': INTERLOCK.encode()}])['workspace_id']
    states = [
        vector('first', 'state', ('第一锁定位置', '第一控制杆：阻挡，保持断开', '第二控制杆：让行，可闭合')),
        vector('second', 'state', ('第二锁定位置', '第一控制杆：让行，可闭合', '第二控制杆：阻挡，保持断开')),
        vector('middle', 'state', ('中间位置', '两个控制杆受阻，保持双断')),
    ]
    proposal = make_plan(workspace, wid, ['mechanical', 'electrical'],
                         [vector('sensor', 'relationship', ('凸轮接触开关',))])
    with pytest.raises(ValueError, match='INTERLOCK-STATE'):
        workspace.stage(wid, proposal)
    with pytest.raises(ValueError, match='INTERLOCK-STATE'):
        workspace.stage(wid, {**proposal, 'technical_types': ['electrical']})
    without_middle = {**proposal, 'views': states[:2]}
    with pytest.raises(ValueError, match='中间'):
        workspace.stage(wid, without_middle)
    assert workspace.stage(wid, {**proposal, 'views': states})['model_calls'] == 0


def test_interlock_accepts_aligned_separate_labels_but_not_cross_panel_pairing(tmp_path):
    workspace = Workspace(OperationStore(tmp_path), 'owner')
    wid = workspace.ingest([{'name': 'interlock.txt', 'blob': INTERLOCK.encode()}])['workspace_id']
    texts = []
    for title, columns, status in [
        ('第一锁定位置', (100, 230), ('阻挡/断开', '让行/可闭合')),
        ('第二锁定位置', (370, 500), ('让行/可闭合', '阻挡/断开')),
    ]:
        texts.append({'type': 'text', 'at': [sum(columns)//2, 65], 'anchor': 'middle', 'text': title})
        for column, rod, action in zip(columns, ('第一控制杆', '第二控制杆'), status):
            texts.extend([
                {'type': 'text', 'at': [column, 450], 'anchor': 'middle', 'text': rod},
                {'type': 'text', 'at': [column, 490], 'anchor': 'middle', 'text': action},
            ])
    texts.extend([
        {'type': 'text', 'at': [700, 65], 'text': '中间位置'},
        {'type': 'text', 'at': [700, 490], 'text': '双断'},
    ])
    view = vector('states', 'state', primitives=texts)
    plan = make_plan(workspace, wid, ['mechanical', 'electrical'], [view])
    crossed = copy.deepcopy(plan)
    crossed['views'][0]['diagram']['primitives'][2]['at'][0] = 370
    with pytest.raises(ValueError, match='第一锁定位置'):
        workspace.stage(wid, crossed)
    assert workspace.stage(wid, plan)['model_calls'] == 0


def test_disclosed_interlock_geometry_distinguishes_contact_from_a_hidden_edge(tmp_path, monkeypatch):
    source = (INTERLOCK + '阻挡边缘抵住第一控制杆，第二接触边缘与第二控制杆运动方向对齐；'
              '第二位置由第一接触边缘让行。中间位置由阻挡边缘两端阻止两个控制杆上升。')
    workspace = Workspace(OperationStore(tmp_path), 'owner')
    wid = workspace.ingest([{'name': 'interlock.txt', 'blob': source.encode()}])['workspace_id']
    names = ('第一控制杆', '第二控制杆', '阻挡边缘', '第一接触边缘', '第二接触边缘')
    parts = [{'id': f'p{i}', 'label': str(i), 'name': name} for i, name in enumerate(names, 1)]

    def view(ident, title, left, right, edge, guide=None):
        elements = [
            {'type': 'rect', 'id': 'left', 'part': 'p1', 'box': [200, 420, 240, 480]},
            {'type': 'rect', 'id': 'right', 'part': 'p2', 'box': [620, 420, 660, 480]},
            {'type': 'polyline', 'id': 'block', 'part': 'p3', 'points': edge},
        ]
        anchors = [('left', 'p1', [160, 460], [215, 450]), ('right', 'p2', [710, 460], [635, 450]),
                   ('block', 'p3', [405, 370], edge[1])]
        if guide:
            part, x = guide
            elements.append({'type': 'polyline', 'id': 'guide', 'part': part,
                             'points': [[x, 340], [x, 460]]})
            anchors.append(('guide', part, [x + 35, 355], [x, 360]))
        elements += [{'type': 'label', 'part': part, 'target_shape': name, 'at': at, 'target': point}
                     for name, part, at, point in anchors]
        return vector(ident, 'state', [title, left, right], [p['id'] for p in parts], elements)

    states = [
        view('first', '第一锁定位置', '第一控制杆阻挡', '第二控制杆让行',
             [[220, 420], [405, 360], [600, 420]], ('p5', 600)),
        view('second', '第二锁定位置', '第一控制杆让行', '第二控制杆阻挡',
             [[260, 420], [405, 360], [640, 420]], ('p4', 260)),
        view('middle', '中间位置', '两个控制杆均断开', '双断',
             [[220, 420], [405, 360], [640, 420]]),
    ]
    proposal = make_plan(workspace, wid, ['mechanical', 'electrical'], states, parts)
    hidden = copy.deepcopy(proposal)
    hidden_shapes = hidden['views'][2]['diagram']['primitives']
    hidden_shapes[2]['points'] = [[220, 420], [405, 460], [640, 420]]
    hidden_shapes[5]['target'] = [405, 460]
    with pytest.raises(ValueError, match='中间.*INTERLOCK-GEOMETRY'):
        workspace.stage(wid, hidden)

    crossed = copy.deepcopy(proposal)
    crossed_shapes = crossed['views'][0]['diagram']['primitives']
    crossed_shapes[3]['points'] = [[640, 340], [640, 460]]
    crossed_shapes[7]['target'] = [640, 360]
    with pytest.raises(ValueError, match='第一锁定位置.*INTERLOCK-GEOMETRY'):
        workspace.stage(wid, crossed)

    missing_leader = copy.deepcopy(proposal)
    del missing_leader['views'][2]['diagram']['primitives'][4]
    with pytest.raises(ValueError, match='中间.*INTERLOCK-GEOMETRY'):
        workspace.stage(wid, missing_leader)

    with monkeypatch.context() as old_version:
        old_version.setattr('patent_drawing.workspace.source_drawing_gaps', lambda *args: [])
        workspace.stage(wid, hidden)
        for state in states:
            workspace.render(wid, state['id'])
    with pytest.raises(ValueError, match='中间.*INTERLOCK-GEOMETRY'):
        workspace.render(wid, 'middle')
    with pytest.raises(ValueError, match='中间.*INTERLOCK-GEOMETRY'):
        workspace.package(wid)
    assert workspace.read(wid)['current'] is None

    assert workspace.stage(wid, proposal)['model_calls'] == 0

    ahead = copy.deepcopy(proposal)
    ahead_shapes = ahead['views'][0]['diagram']['primitives']
    ahead_shapes[3]['points'] = [[600, 340], [600, 400]]
    assert workspace.stage(wid, ahead)['model_calls'] == 0

    beside = copy.deepcopy(proposal)
    beside_shapes = beside['views'][0]['diagram']['primitives']
    beside_shapes[3]['points'] = [[620, 340], [620, 400]]
    beside_shapes[7]['target'] = [620, 360]
    assert workspace.stage(wid, beside)['model_calls'] == 0

    behind = copy.deepcopy(proposal)
    behind_shapes = behind['views'][0]['diagram']['primitives']
    behind_shapes[3]['points'] = [[600, 440], [600, 500]]
    behind_shapes[7]['target'] = [600, 460]
    with pytest.raises(ValueError, match='第一锁定位置.*INTERLOCK-GEOMETRY'):
        workspace.stage(wid, behind)


def package_fixture(workspace, wid):
    names = ('垂直贯穿电极', '再分布结构导电图案', '导电柱', '芯片连接端子')
    parts = [{'id': f'p{i}', 'label': str(i), 'name': name} for i, name in enumerate(names, 1)]
    shapes = [{'type': 'rect', 'id': f's{i}', 'part': f'p{i}', 'box': [130, 110*i, 340, 110*i+50]}
              for i in range(1, 5)]
    labels = [{'type': 'label', 'part': f'p{i}', 'at': [550, 110*i], 'target': [250, 110*i+25],
               'target_shape': f's{i}'} for i in range(1, 5)]
    view = vector('package', 'layer_section', parts=[part['id'] for part in parts], primitives=shapes)
    return make_plan(workspace, wid, ['semiconductor'], [view], parts), labels


def test_numbered_section_requires_anchored_visible_marks_and_renders_them(tmp_path):
    workspace = Workspace(OperationStore(tmp_path), 'owner')
    wid = workspace.ingest([{'name': 'package.txt', 'blob': PACKAGE.encode()}])['workspace_id']
    proposal, labels = package_fixture(workspace, wid)
    with pytest.raises(ValueError, match='SECTION-VISIBLE-MARK'):
        workspace.stage(wid, proposal)
    proposal['views'][0]['diagram']['primitives'].extend(labels)
    workspace.stage(wid, proposal)
    figure = workspace.render(wid, 'package')['record']
    assert [entry['label'] for entry in figure['annotations']] == ['1', '2', '3', '4']
    svg = workspace.store.get_artifact(workspace.owner, figure['svg'])
    assert all(f'>{i}</'.encode() in svg for i in range(1, 5))


def test_natural_incomplete_plan_remains_auditable_and_cannot_be_confirmed(tmp_path):
    class Planner:
        def plan(self, system, user, **kwargs):
            return {'text': json.dumps(proposal), 'request_id': 'saved-plan'}

    workspace = Workspace(OperationStore(tmp_path), 'owner', allow_models=True, text_client=Planner())
    wid = workspace.ingest([{'name': 'package.txt', 'blob': PACKAGE.encode()}])['workspace_id']
    proposal, _ = package_fixture(workspace, wid)
    raw = copy.deepcopy(proposal)
    first = workspace.plan(wid, '规划')
    assert first['blocking_state'] == 'planning_coverage_incomplete'
    assert first['model_calls'] == 1 and first.get('plan') is None
    state = workspace.read(wid)
    assert state['proposal'] is None and state['work'] is None
    assert state['planning_history'][-1]['domain_findings'][0]['rule'] == 'SECTION-VISIBLE-MARK'
    second = workspace.plan(wid, '规划')
    assert second['model_calls'] == 0 and second['domain_findings'] == first['domain_findings']
    assert proposal == raw


def test_planner_distinguishes_a_qualitative_image_from_spatial_expression_approval(tmp_path):
    class Planner:
        def plan(self, system, user, **kwargs):
            assert 'requested_type=relationship、delivered_type=relationship' in system
            assert '二维关系图省略expression_mode' in system
            assert '真实空间投影仍须私有表达审核' in system
            assert json.loads(user)['previous_plan'] is None
            return {'text': json.dumps(proposal)}

    workspace = Workspace(OperationStore(tmp_path), 'owner', allow_models=True, text_client=Planner())
    wid = workspace.ingest([{'name': 'material.txt', 'blob': '颗粒彼此接触，形成定性传热通路。'.encode()}],
                           technical_types=['chemical_material'])['workspace_id']
    view = {'id': 'overview', 'title': '颗粒关系', 'purpose': '定性示意相接与传热',
            'engine': 'image', 'requested_type': 'relationship', 'delivered_type': 'relationship',
            'parts': ['grain'], 'instruction': '用二维非比例符号和箭头示意已知关系。'}
    proposal = make_plan(workspace, wid, ['chemical_material'], [view],
                         [{'id': 'grain', 'label': '1', 'name': '颗粒'}])
    candidate = workspace.plan(wid, '规划一套专利附图。请用图片示意颗粒相接。')['plan']
    offline = Workspace(workspace.store, workspace.owner)
    assert offline.stage(wid, candidate, image_budget=1)['required_image_calls'] == 1
    assert offline.preflight(wid)['blocking_state'] == 'model_execution_disabled'
    spatial = copy.deepcopy(candidate)
    spatial['views'][0].update(requested_type='spatial', delivered_type='spatial',
                               expression_mode='relational_schematic')
    offline.stage(wid, spatial, image_budget=1)
    assert offline.preflight(wid)['blocking_state'] == 'needs_expression'


def test_supported_package_process_preserves_source_step_order(tmp_path):
    workspace = Workspace(OperationStore(tmp_path), 'owner')
    material = PACKAGE + '先形成再分布结构，再设置导电柱，最后热压接合。'
    wid = workspace.ingest([{'name': 'package.txt', 'blob': material.encode()}])['workspace_id']
    proposal, labels = package_fixture(workspace, wid)
    proposal['views'][0]['diagram']['primitives'].extend(labels)
    with pytest.raises(ValueError, match='PACKAGE-PROCESS'):
        workspace.stage(wid, proposal)

    flow = vector('assembly_flow', 'process_flow')
    flow['diagram']['nodes'] = [
        {'id': name, 'type': 'process', 'text': step, 'box': [100 + i*250, 150, 290 + i*250, 240]}
        for i, (name, step) in enumerate([
            ('redistribution', '形成再分布结构'), ('pillar', '设置导电柱和侧部绝缘'), ('bond', '热压接合')])
    ]
    flow['diagram']['edges'] = [{'source': 'redistribution', 'target': 'bond',
                                 'points': [[195, 150], [195, 90], [695, 90], [695, 150]]},
                                {'source': 'bond', 'target': 'pillar'}]
    proposal['views'].append(flow)
    with pytest.raises(ValueError, match='PACKAGE-PROCESS'):
        workspace.stage(wid, proposal)
    flow['diagram']['edges'] = [{'source': 'redistribution', 'target': 'pillar'},
                                {'source': 'pillar', 'target': 'bond'}]
    assert workspace.stage(wid, proposal)['model_calls'] == 0


@pytest.mark.parametrize(('boundary_shape', 'failure'), [
    ('straight', None), ('bowed', None), ('straight', 'split-origin'), ('bowed', 'split-origin'),
    ('bowed', 'false-midpoints'), ('straight', 'gap-near-origin'),
    ('straight', 'missing-arrowheads'), ('straight', 'missing-panel'), ('straight', 'fragmented-panel'),
    ('straight', 'misleading-leader'), ('straight', 'outside-region'), ('capped', None),
    ('straight', 'inside-panel'), ('straight', 'merged-strokes'),
])
def test_beam_range_generation_and_review_bind_four_rays_to_one_panel_point(tmp_path, boundary_shape, failure):
    import datetime
    import io
    import zipfile
    from PIL import Image, ImageDraw
    from patent_drawing.client import OpenAIImagesClient
    from patent_drawing.operation_store import digest
    from tests.test_images import b64
    import httpx

    raw = Image.new('RGB', (360, 240), 'white')
    draw = ImageDraw.Draw(raw)
    panel = [35, 70, 90, 170]
    origin = [90, 120]
    limits = [[305, 26], [305, 214]]
    beams = [{'tip': [280, 80], 'head': [[262, 77], [265, 90]]},
             {'tip': [270, 161], 'head': [[252, 149], [250, 165]]}]
    if failure == 'inside-panel':
        panel = [90, 25, 315, 220]
    if failure == 'merged-strokes':
        limits = [[305, 73], [305, 171]]
    if failure == 'fragmented-panel':
        corners = [(35, 70), (90, 70), (90, 170), (35, 170)]
        for a, b in zip(corners, corners[1:] + corners[:1]):
            for fraction in (.2, .5, .8):
                x, y = (round(start + fraction*(end-start)) for start, end in zip(a, b))
                draw.point((x, y), fill='black')
    elif failure != 'missing-panel':
        draw.rectangle(panel, outline='black', width=2)
    boundary_midpoints = [[198, 66], [198, 174]] if boundary_shape == 'bowed' else None
    for i, endpoint in enumerate(limits):
        start = [90, 90 + 60 * i] if failure == 'split-origin' else origin
        trace = [start, boundary_midpoints[i], endpoint] if boundary_midpoints else [start, endpoint]
        if failure == 'gap-near-origin' and i == 1:
            draw.line([tuple(start), (118, 132)], fill='black', width=2)
            draw.line([(130, 137), tuple(endpoint)], fill='black', width=2)
        else:
            draw.line(list(map(tuple, trace)), fill='black', width=2)
    for i, beam in enumerate(beams):
        start = [135, 110 + 25 * i] if failure == 'split-origin' else origin
        draw.line([tuple(start), tuple(beam['tip'])], fill='black', width=2)
        if failure != 'missing-arrowheads':
            draw.polygon([tuple(beam['tip']), *map(tuple, beam['head'])], fill='black')
    if boundary_shape == 'capped':
        points = [(round(305 + 48*t*(1-t)), round(26 + 188*t)) for t in (i/32 for i in range(33))]
        draw.line(points, fill='black', width=2)
    if failure == 'misleading-leader':
        draw.line([(280, 200), (248, 194), (246, 178)], fill='black', width=2)
        draw.polygon([(246, 178), (243, 187), (251, 185)], fill='black')
        draw.text((280, 193), 'Beam direction', fill='black')
    if failure == 'outside-region':
        draw.line([(342, 72), (342, 155)], fill='black', width=2)
    output = io.BytesIO()
    raw.save(output, format='PNG')
    requests = []

    def respond(request):
        requests.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={'data': [{'b64_json': b64(output.getvalue())}]})

    client = OpenAIImagesClient('offline-test', transport=httpx.MockTransport(respond))
    workspace = Workspace(OperationStore(tmp_path), 'reviewer', allow_models=True, image_client=client)
    quote = '发射设备定义相对于自身天线面板的角度范围，其中包含至少两个发射波束方向。'
    wid = workspace.ingest([{'name': 'beam.txt', 'blob': quote.encode()}],
                           technical_types=['communication'])['workspace_id']
    state = workspace.read(wid)
    view = {'id': 'range', 'engine': 'image', 'requested_type': 'relationship', 'delivered_type': 'relationship',
            'title': '角度范围', 'purpose': '表达天线面板及范围内的波束方向',
            'instruction': '绘制二维示意。图内仅保留{{panel.name}}、发射波束方向等必要名称。',
            'parts': ['panel'], 'required_facts': ['f1']}
    plan = make_plan(workspace, wid, ['communication'], [view],
                     [{'id': 'panel', 'label': '1', 'name': '天线面板'}])
    plan['facts'] = [{'id': 'f1', 'status': 'confirmed', 'text': quote,
                      'evidence': [{'page_id': state['bundle']['pages'][0]['id'], 'quote': quote}]}]
    workspace.stage(wid, plan, image_budget=1)
    contract = workspace.read(wid)['work']['plan']['visibility_contract']['range']
    assert [feature['feature_id'] for feature in contract['features']] == [
        'panel-beam-common-origin', 'source-fidelity']
    record = workspace.render(wid, 'range')['record']
    assert requests[0][0] == '/v1/images/edits'
    assert requests[0][1]['images'][0]['image_url'].startswith('data:image/png;base64,')
    assert record['reference_selection'] == 'source_bound_beam_layout'
    assert '两条边界和两条带箭尖的波束线均从同一点出发' in requests[0][1]['prompt']
    if failure is None and boundary_shape == 'straight':
        prompt = requests[0][1]['prompt']
        assert '上边和下边水平、左边和右边竖直' in prompt
        assert '二维关系示意图' in prompt and '结构示意图：' not in prompt
        assert '调整投影' not in prompt
        assert prompt.count('矩形外的右侧留白') == 1
        assert '下边界独立于下方向箭' in prompt
        assert '这是修改已有附图的任务' not in prompt
        assert prompt.index('矩形外的右侧留白') < prompt.index('"facts"')
        assert '连接处无白缝' in prompt
    region = [25, 15, 330, 225]
    geometry = {'panel': panel, 'origin': origin, 'boundaries': limits, 'beams': beams}
    if boundary_midpoints:
        geometry['boundary_midpoints'] = ([[198, 80], [198, 160]] if failure == 'false-midpoints'
                                          else boundary_midpoints)
    if boundary_shape == 'capped':
        geometry['range_cap_midpoint'] = [317, 120]
    review = {
        'view_id': 'range', 'base_sha256': record['base']['sha256'], 'spec_sha256': record['base_spec_sha256'],
        'contract_sha256': digest(contract), 'reviewer': 'synthetic engineering fixture',
        'reviewed_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'features': [
            {'feature_id': 'source-fidelity', 'verdict': 'PASS', 'note': '定性关系', 'region': region},
            {'feature_id': 'panel-beam-common-origin', 'verdict': 'PASS', 'note': '检查共同起点与箭尖',
             'region': region, 'beam_geometry': geometry},
        ],
    }
    if not failure:
        assert workspace.review_visibility(wid, 'range', review)['technical_review'] == 'PASS'
        assert not workspace.preflight(wid)['blocking_state']
    else:
        reason = {'split-origin': 'strokes do not meet', 'missing-arrowheads': 'arrowhead wings are not visible',
                  'missing-panel': 'panel edge is not visible', 'false-midpoints': 'strokes do not meet',
                  'gap-near-origin': 'strokes do not meet',
                  'fragmented-panel': 'panel edge is not visible',
                  'misleading-leader': 'unexpected strokes', 'outside-region': 'unexpected strokes',
                  'inside-panel': 'outside the same panel edge',
                  'merged-strokes': 'four independently visible strokes'}[failure]
        with pytest.raises(ValueError, match=reason):
            workspace.review_visibility(wid, 'range', review)
        assert workspace.preflight(wid)['blocking_state'] == 'awaiting_visual_review'
        review['features'][1].update(verdict='FAIL', note=reason)
        assert workspace.review_visibility(wid, 'range', review)['technical_review'] == 'BLOCKED'
        assert workspace.preflight(wid)['blocking_state'] == 'technical_failed'
    if not failure and boundary_shape == 'straight':
        assert '图内仅保留' not in prompt
        assert '不要在底图中添加文字、标号、引出线' in prompt
        zip_blob = workspace.package(wid)['zip']
        with zipfile.ZipFile(io.BytesIO(zip_blob)) as archive:
            description = archive.read('figure-descriptions.txt').decode()
        assert '无箭尖的两线表示相对角度范围，两支内箭分别表示示意波束方向' in description
    assert len(requests) == 1


@pytest.mark.parametrize('tail_gap', [0, 24])
def test_outward_strokes_share_actual_ink_at_the_panel_edge(tail_gap):
    from PIL import Image, ImageDraw
    from patent_drawing.visibility import panel_beam_issue

    image = Image.new('L', (1536, 1024), 255)
    draw = ImageDraw.Draw(image)
    panel = [60, 352, 450, 585]
    origin = [450, 467]
    boundaries = [[1458, 164], [1458, 782]]
    midpoints = [[950, 307], [950, 633]]
    beams = [{'tip': [1176, 336], 'head': [[1127, 331], [1132, 359]]},
             {'tip': [1177, 590], 'head': [[1132, 569], [1128, 597]]}]
    draw.rectangle(panel, outline=0, width=3)
    for midpoint, end in zip(midpoints, boundaries):
        draw.line([tuple(origin), tuple(midpoint), tuple(end)], fill=0, width=3)
    for beam in beams:
        x, y = beam['tip']
        start = (origin[0] + tail_gap, round(origin[1] + tail_gap * (y-origin[1]) / (x-origin[0])))
        draw.line([start, (x, y)], fill=0, width=3)
        draw.polygon([(x, y), *map(tuple, beam['head'])], fill=0)
    row = {'region': [53, 155, 1465, 790], 'beam_geometry': {
        'panel': panel, 'origin': origin, 'boundaries': boundaries,
        'boundary_midpoints': midpoints, 'beams': beams,
    }}
    policy = dict(allow_midpoints=True, require_full_panel=True, require_clean_base=True, require_outward=True)
    assert panel_beam_issue(row, image.size, image, **policy) is None
    expected = 'four outside strokes must connect to the actual panel root' if tail_gap else None
    assert panel_beam_issue(row, image.size, image, require_connected_root=True, **policy) == expected


def test_explicit_revision_after_incomplete_plan_reuses_successful_refinement(tmp_path):
    calls = []

    class Planner:
        def plan(self, system, user, **kwargs):
            calls.append(json.loads(user)['query'])
            return {'text': json.dumps(repaired if len(calls) > 1 else original)}

    workspace = Workspace(OperationStore(tmp_path), 'owner', allow_models=True, text_client=Planner())
    wid = workspace.ingest([{'name': 'package.txt', 'blob': PACKAGE.encode()}])['workspace_id']
    original, labels = package_fixture(workspace, wid)
    repaired = copy.deepcopy(original)
    repaired['views'][0]['diagram']['primitives'].extend(labels)
    assert workspace.plan(wid, '规划')['blocking_state'] == 'planning_coverage_incomplete'
    query = '规划：补齐四项实际引线标号'
    assert workspace.plan(wid, query)['model_calls'] == 1
    assert workspace.plan(wid, query)['model_calls'] == 0
    assert calls == ['规划', query]


def test_new_refinement_does_not_bypass_an_unknown_prior_response(tmp_path):
    calls = []

    class Planner:
        def plan(self, system, user, **kwargs):
            calls.append(json.loads(user)['query'])
            if len(calls) > 1:
                raise DrawingError('result_unknown', 'no terminal receipt', 'req-unknown')
            return {'text': json.dumps(proposal)}

    workspace = Workspace(OperationStore(tmp_path), 'owner', allow_models=True, text_client=Planner())
    wid = workspace.ingest([{'name': 'package.txt', 'blob': PACKAGE.encode()}])['workspace_id']
    proposal, _ = package_fixture(workspace, wid)
    workspace.plan(wid, '规划')
    with pytest.raises(PlannerResultUnknown) as original:
        workspace.plan(wid, '规划：补齐实际引线')
    with pytest.raises(PlannerResultUnknown) as subsequent:
        workspace.plan(wid, '规划：重新排布引线')
    assert original.value.operation_id == subsequent.value.operation_id
    assert subsequent.value.model_calls == 0 and len(calls) == 2


def test_planner_extends_trailing_canvas_without_moving_text_or_geometry(tmp_path):
    workspace = Workspace(OperationStore(tmp_path), 'owner')
    wid = workspace.ingest([{'name': 'source.txt', 'blob': '接触开关检测电路状态。'.encode()}])['workspace_id']
    view = vector('sensor', 'relationship', primitives=[
        {'type': 'rect', 'id': 'housing', 'box': [200, 150, 680, 500]},
        {'type': 'text', 'at': [450, 675], 'font_size': 30, 'anchor': 'middle', 'text': '检测电路状态'},
    ])
    original = make_plan(workspace, wid, ['electrical'], [view])
    normalized, receipt = normalize_proposal(original)
    assert normalized['views'][0]['diagram']['height'] == 712
    assert normalized['views'][0]['diagram']['primitives'] == view['diagram']['primitives']
    assert original['views'][0]['diagram']['height'] == 700
    assert receipt['semantic_rewrite'] is False
    assert receipt['changes'][0]['action'] == 'extend_canvas_for_text'
    assert workspace.stage(wid, normalized)['model_calls'] == 0
    assert workspace.render(wid, 'sensor')['model_calls'] == 0
    invalid_geometry = copy.deepcopy(original)
    invalid_geometry['views'][0]['diagram']['primitives'][0]['box'][3] = 705
    with pytest.raises(DrawingError, match='坐标'):
        normalize_proposal(invalid_geometry)


def test_mixed_range_sequence_marks_visible_and_can_be_linked():
    from patent_drawing.spatial_brief import bind_mixed_communication_marks, panel_range_diagram

    parts = [
        {'id': pid, 'label': str(i), 'name': name}
        for i, (pid, name) in enumerate(
            [('tx', '发射设备'), ('rx', '接收设备'), ('p', '天线面板'),
             ('r', '角度范围'), ('b', '发射波束方向')], 1)]
    beam = {'id': 'range', 'engine': 'vector', 'requested_type': 'relationship', 'parts': ['p', 'r', 'b'],
            'diagram': panel_range_diagram('p', 'r', 'b')}
    seq = {'id': 'sequence', 'engine': 'vector', 'requested_type': 'sequence', 'parts': ['tx', 'rx'],
           'diagram': {'nodes': [{'id': 'a', 'text': '发射设备'}, {'id': 'b', 'text': '接收设备'}]}}
    plan = {'parts': parts, 'views': [beam, seq]}
    assert bind_mixed_communication_marks(plan, [{'view_id': 'range', 'layout': 'panel-relative-beam-range'}])
    assert seq['diagram']['nodes'][0]['text'] == '{{tx.name}} {{tx.label}}'
    assert seq['diagram']['nodes'][1]['text'] == '{{rx.name}} {{rx.label}}'
    assert {item['part'] for item in beam['diagram']['primitives'] if item['type'] == 'label'} == {'p', 'r', 'b'}
def test_coating_section_draws_an_open_pore_inside_the_surface():
    import io
    from PIL import Image
    from patent_drawing.diagrams import render_diagram
    from patent_drawing.planner_contract import coating_pore_section

    roles = dict(substrate='base', coating='film', open_pore='open', covered_pore='covered')
    parts = [{'id': pid, 'label': str(i), 'name': pid} for i, pid in enumerate(roles.values(), 1)]
    section = coating_pore_section(roles)
    figure = {'id': 'section', 'parts': list(roles.values()), 'diagram': section}
    graphics = render_diagram({'parts': parts}, figure)
    with Image.open(io.BytesIO(graphics['png'])) as image:
        assert image.getpixel((380, 260)) == (255, 255, 255)
        assert image.getpixel((380, 250)) == (255, 255, 255)
        assert image.getpixel((380, 350)) == (0, 0, 0)
        assert image.getpixel((795, 260)) == (0, 0, 0)
    assert {anchor['part'] for anchor in graphics['anchors']} == set(roles.values())

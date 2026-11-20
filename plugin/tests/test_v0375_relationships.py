import copy
import io
import json
import zipfile
from xml.etree import ElementTree

import pypdfium2
import pytest
from PIL import Image

from patent_drawing.diagrams import reanchor_edge, validate_diagram
from patent_drawing.errors import SourceCoverageIncomplete
from patent_drawing.operation_store import OperationStore, digest
from patent_drawing.workspace import Workspace


class RecordedPlanner:
    def plan(self, system, user, **kwargs):
        self.context = json.loads(user)
        return {'text': json.dumps(self.proposal, ensure_ascii=False)}


def node(ident, text, box, kind='process', **fields):
    return {'id': ident, 'text': text, 'box': box, 'type': kind, **fields}


def view(ident, kind, parts, nodes, edges, *, width=1500, height=1000, primitives=()):
    return {'id': ident, 'title': ident, 'purpose': '表达来源关系', 'instruction': '按来源图示',
            'requested_type': kind, 'delivered_type': kind, 'engine': 'vector', 'parts': list(parts),
            'depends_on': [], 'required_facts': [],
            'diagram': {'width': width, 'height': height, 'nodes': nodes, 'edges': edges,
                        'primitives': list(primitives)}}


def setup(tmp_path, text, types, names, views):
    planner = RecordedPlanner()
    workspace = Workspace(OperationStore(tmp_path), 'owner', allow_models=True, text_client=planner)
    wid = workspace.ingest([{'name': 'disclosure.txt', 'blob': text.encode()}], technical_types=types)['workspace_id']
    planner.proposal = {'schema_version': 'drawing-set-2', 'patent_type': 'invention', 'title': '核对图组',
                        'part_mark_mode': 'numbered', 'technical_types': types,
                        'parts': [{'id': pid, 'name': name, 'label': str(i)}
                                  for i, (pid, name) in enumerate(names.items(), 1)],
                        'facts': [], 'views': views, 'constraints': [], 'unresolved': []}
    return workspace, wid, planner


def workspace_plan(workspace, wid, planner):
    return {**copy.deepcopy(planner.proposal), 'revision': 1, 'set_id': 'imported',
            'source_sha256': workspace.read(wid)['source_sha256']}


COMMUNICATION = ('通信模块通过计算机总线与监测控制单元交换数据。'
                 '运行数据上送工业计算机，工业计算机命令沿相应路径返回单元。'
                 '通信模块经网络交换机接入中央交换机，再与工业计算机相连。'
                 '主供电经保护单元送至负载。')


def cabinet(tmp_path, source=COMMUNICATION):
    names = {'control': '监测控制单元', 'bus': '计算机总线', 'module': '通信模块',
             'switch': '网络交换机', 'central': '中央交换机', 'computer': '工业计算机',
             'supply': '主供电', 'protect': '保护单元', 'load': '负载'}
    chain = list(names)[:6]
    topology = view('topology', 'topology', chain, [
        node(pid, '{{' + pid + '.name}}', [45 + i * 355, 300, 305 + i * 355, 410])
        for i, pid in enumerate(chain)], [
        {'source': a, 'target': b, 'label': '双向', 'label_at': [315 + i * 355, 310]}
        for i, (a, b) in enumerate(zip(chain, chain[1:]))], width=2200)
    power_parts = ['supply', 'protect', 'load', 'control']
    power = view('power', 'block_diagram', power_parts, [
        node(pid, '{{' + pid + '.name}}', [40 + i * 345, 300, 280 + i * 345, 410])
        for i, pid in enumerate(power_parts)], [
        {'source': 'supply', 'target': 'protect'}, {'source': 'protect', 'target': 'load'}])
    return setup(tmp_path, source, ['electrical'], names, [power, topology])


def test_communication_round_trip_on_actual_svg_png_pdf(tmp_path):
    workspace, wid, planner = cabinet(tmp_path)
    plan = workspace.plan(wid, '规划')['plan']
    power, topology = plan['views']
    assert all(edge.get('bidirectional') is True for edge in topology['diagram']['edges'])
    assert len(topology['diagram']['edges']) == 5
    assert all('bidirectional' not in edge for edge in power['diagram']['edges'])
    workspace.stage(wid, plan, require_complete_marks=True)
    for fid in ('power', 'topology'):
        workspace.render(wid, fid)
    packed = workspace.package(wid)
    with zipfile.ZipFile(io.BytesIO(packed['zip'])) as archive:
        svg = ElementTree.fromstring(archive.read('figures/topology.svg'))
        with Image.open(io.BytesIO(archive.read('figures/topology.png'))) as png:
            for index in range(5):
                left, right = 305 + index * 355, 400 + index * 355
                assert png.getpixel((left + 12, 358))[:3] == (0, 0, 0)
                assert png.getpixel((right - 12, 358))[:3] == (0, 0, 0)
        tips = [tuple(map(float, item.attrib['points'].split()[0].split(',')))
                for item in svg.iter() if item.tag.endswith('polygon') and item.get('fill') == 'black']
        assert tips == [(end, 355.0) for index in range(5)
                        for end in (400 + index * 355, 305 + index * 355)]
        descriptions = archive.read('figure-descriptions.txt').decode()
        assert descriptions.count('1—监测控制单元') == 2
    document = pypdfium2.PdfDocument(packed['pdf'])
    try:
        assert len(document) == 2
        page = document[1]
        try:
            raster = page.render(scale=2).to_pil().convert('L')
            assert raster.getextrema() == (0, 255)
        finally:
            page.close()
    finally:
        document.close()


def test_textual_arrow_claim_does_not_satisfy_import_gate(tmp_path):
    workspace, wid, planner = cabinet(tmp_path)
    with pytest.raises(SourceCoverageIncomplete, match='COMMUNICATION-DIRECTION'):
        workspace.stage(wid, workspace_plan(workspace, wid, planner))


def test_communication_packet_requires_both_heads_in_the_stored_png(tmp_path):
    workspace, wid, _ = cabinet(tmp_path)
    plan = workspace.plan(wid, '规划')['plan']
    workspace.stage(wid, plan)
    workspace.render(wid, 'power')
    record = workspace.render(wid, 'topology')['record']
    raw = workspace.store.get_artifact(workspace.owner, record['png'])
    output = io.BytesIO()
    with Image.open(io.BytesIO(raw)) as image:
        # Choose a wing from actual SVG geometry so this remains valid at the new print size.
        import xml.etree.ElementTree as ET
        svg = ET.fromstring(workspace.store.get_artifact(workspace.owner, record['svg']))
        poly = next(node for node in svg.iter() if node.tag.endswith('polygon') and node.get('fill') == 'black')
        tip, wing, opposite = [[float(v) for v in point.split(',')] for point in poly.get('points').split()]
        x, y = (round(.18 * t + .72 * w + .10 * o) for t, w, o in zip(tip, wing, opposite))
        image.putpixel((x, y), (255, 255, 255))
        image.save(output, 'PNG')
    record['png'] = workspace.store.put_artifact(workspace.owner, output.getvalue())
    workspace.store.save(workspace.owner, workspace.record_key(wid, 'topology', record['spec_sha256']), record)
    with pytest.raises(SourceCoverageIncomplete, match='RENDERED-RELATIONSHIP'):
        workspace.package(wid)
    assert workspace.read(wid)['current'] is None


def test_old_staged_communication_cannot_bypass_preflight(tmp_path):
    workspace, wid, planner = cabinet(tmp_path)
    plan = workspace.plan(wid, '规划')['plan']
    workspace.stage(wid, plan)
    state = workspace.read(wid)
    state['work']['plan'] = workspace_plan(workspace, wid, planner)
    workspace.save(wid, state)
    check = workspace.preflight(wid)
    assert check['blocking_state'] == 'planning_coverage_incomplete'
    assert check['blocked_view'] == 'topology' and check['model_calls'] == 0


def test_retry_of_saved_natural_operation_reuses_the_original_request_identity(tmp_path):
    workspace, wid, _ = cabinet(tmp_path)
    first = workspace.plan(wid, '规划')
    again = workspace.plan(wid, '规划')
    assert first['model_calls'] == 1 and again['model_calls'] == 0
    assert digest(first['plan']) == digest(again['plan'])


def test_supply_or_unknown_links_cannot_claim_bidirectional(tmp_path):
    workspace, wid, planner = cabinet(tmp_path, source='主供电经保护单元送至负载。通信模块的接口方向待确认。')
    planner.proposal['views'][0]['diagram']['edges'][0]['bidirectional'] = True
    with pytest.raises(SourceCoverageIncomplete, match='DIRECTION-SOURCE'):
        workspace.stage(wid, workspace_plan(workspace, wid, planner))


@pytest.mark.parametrize('kind', ['flowchart', 'block_diagram'])
def test_mode_decision_is_an_independent_pwm_input_even_if_part_is_already_listed(tmp_path, kind):
    source = ('评估当前调制及工况所对应的调制率变动；采用补偿增益上限。'
              '把计算增益限制在相应上限内，以受限增益修正电压指令，再由PWM输出控制电机。'
              '保留电流反馈和调制模式判定构成的运行闭环；同步/非同步PWM的具体判定参数需要标定。')
    names = {'pwm': 'PWM输出', 'mode': '调制模式判定', 'feedback': '电流反馈', 'cap': '补偿增益上限'}
    graph = view('control', kind, names, [
        node('variation', '评估调制率变动', [820, 110, 1190, 210]),
        node('cap', '{{cap.name}}', [820, 300, 1190, 400]),
        node('feedback', '{{feedback.name}}', [150, 110, 550, 210]),
        node('limit', '限制在相应上限内', [150, 460, 550, 560]),
        node('pwm', '{{pwm.name}}', [150, 680, 550, 780]),
    ], [{'source': 'variation', 'target': 'cap'}, {'source': 'cap', 'target': 'limit',
         'points': [[1005, 400], [1005, 510], [550, 510]]}, {'source': 'feedback', 'target': 'limit'},
         {'source': 'limit', 'target': 'pwm'}, {'source': 'pwm', 'target': 'feedback',
          'points': [[150, 730], [70, 730], [70, 160], [150, 160]]}])
    workspace, wid, planner = setup(tmp_path, source, ['electrical'], names, [graph])
    with pytest.raises(SourceCoverageIncomplete, match='PWM-MODE-INFLUENCE'):
        workspace.stage(wid, workspace_plan(workspace, wid, planner))
    planned = workspace.plan(wid, '规划')['plan']
    checked = planned['views'][0]['diagram']
    mode = next(row for row in checked['nodes'] if '{{mode.name}}' in row['text'])
    assert mode['type'] == 'process' and 'branches' not in mode
    assert {'source': mode['id'], 'target': 'pwm'} in checked['edges']
    assert {('variation', 'cap'), ('cap', 'limit'), ('pwm', 'feedback')} <= {
        (edge['source'], edge['target']) for edge in checked['edges']}
    workspace.stage(wid, planned)
    record = workspace.render(wid, 'control')['record']
    svg = workspace.store.get_artifact(workspace.owner, record['svg'])
    assert '调制模式判定'.encode() in svg and b'PWM' in svg
    assert workspace.package(wid)['pdf'].startswith(b'%PDF')


BEAM = ('发射设备定义相对于自身天线面板的角度范围，其中包含至少两个发射波束方向，并把该范围指示发送给接收设备。'
        '接收设备根据该范围选择接收波束候选。'
        '发射设备发送参考信号，接收设备使用选定波束进行接收和测量，再向发射设备回传测量反馈。')


def test_beam_repair_keeps_ambiguous_feedback_out_of_partial_delivery(tmp_path):
    sequence = view('sequence', 'sequence', ['tx', 'rx'], [
        node('tx', '{{tx.name}}', [65, 55, 520, 145], 'actor'),
        node('rx', '{{rx.name}}', [875, 55, 1330, 145], 'actor'),
        node('range', '定义角度范围', [70, 220, 520, 320]),
        node('select', '选择接收波束候选', [880, 410, 1340, 510]),
        node('send', '发送参考信号', [70, 550, 520, 650]),
        node('receive', '接收和测量', [880, 740, 1340, 840]),
    ], [
        {'source': 'range', 'target': 'select', 'label': '范围指示', 'points': [[290, 360], [1090, 360]]},
        {'source': 'send', 'target': 'receive', 'label': '参考信号', 'points': [[290, 690], [1090, 690]]},
        {'source': 'receive', 'target': 'range', 'label': '测量反馈', 'points': [[1090, 900], [290, 900]]},
    ])
    separate = view('other', 'block_diagram', ['tx'], [node('actor', '{{tx.name}}', [70, 55, 500, 155])], [])
    workspace, wid, planner = setup(tmp_path, BEAM, ['communication'], {'tx': '发射设备', 'rx': '接收设备'}, [separate, sequence])
    result = workspace.plan(wid, '规划')
    assert result['blocking_state'] == 'planning_coverage_incomplete'
    findings = result['domain_findings']
    assert any(row['rule'] == 'SEQUENCE-EDGE-BOUNDARY' and row['view_id'] == 'sequence' for row in findings)
    assert any(row['rule'] == 'BEAM-VIEW-COVERAGE' for row in findings)
    history = workspace.read(wid)['planning_history'][-1]
    candidate = json.loads(workspace.store.get_artifact(workspace.owner, history['unapproved_candidate']))
    edges = candidate['views'][1]['diagram']['edges']
    assert edges[0]['points'] == [[290, 320], [290, 360], [1090, 360], [1090, 410]]
    assert edges[1]['points'] == [[290, 650], [290, 690], [1090, 690], [1090, 740]]
    assert edges[2] == planner.proposal['views'][1]['diagram']['edges'][2]
    offline = Workspace(workspace.store, workspace.owner)
    assert offline.preflight(wid)['route'] == 'reply'
    packed = offline.package_partial(wid)
    assert packed['missing_ids'] == ['sequence']
    with zipfile.ZipFile(io.BytesIO(packed['zip'])) as archive:
        assert 'figures/other.png' in archive.namelist()
        assert 'figures/sequence.png' not in archive.namelist()
    assert offline.read(wid)['current'] is None


def test_boundary_repair_uses_a_diamond_outline_and_rejects_crossed_nodes():
    start = node('start', '起点', [50, 130, 220, 230])
    decision = node('decision', '是否重试', [520, 325, 760, 485], 'decision')
    points = [[130, 260], [640, 260]]
    corrected = reanchor_edge(points, start, decision, [start, decision])
    assert corrected == [[130, 230], *points, [640, 325]]
    obstacle = node('other', '另一步骤', [300, 240, 410, 280])
    assert reanchor_edge(points, start, decision, [start, decision, obstacle]) is None


def test_double_arrow_rejects_too_short_route_between_nodes(tmp_path):
    workspace, _, planner = cabinet(tmp_path)
    fig = planner.proposal['views'][1]
    fig['diagram']['nodes'][1]['box'][0] = 330
    fig['diagram']['edges'][0].update(bidirectional=True, label_at=[100, 230])
    with pytest.raises(ValueError, match='双向箭尖'):
        validate_diagram(planner.proposal, fig)

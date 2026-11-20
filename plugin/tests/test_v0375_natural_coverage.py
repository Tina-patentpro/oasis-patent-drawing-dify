import io
import zipfile

import pytest

from patent_drawing.errors import SourceCoverageIncomplete
from patent_drawing.operation_store import OperationStore
from patent_drawing.spatial_brief import panel_range_diagram
from patent_drawing.workspace import Workspace
from .test_v0375_relationships import BEAM, COMMUNICATION, node, setup, view, workspace_plan


def multi_column(tmp_path, *, disclosed=True, literal_duplicates=False, crossed=False):
    source = COMMUNICATION
    if disclosed:
        source = '每列的' + source.replace('通信模块经网络交换机', '多列通信模块经网络交换机')
    names = {'monitor': '监测控制单元', 'bus': '计算机总线', 'module': '通信模块',
             'switch': '网络交换机', 'central': '中央交换机', 'computer': '工业计算机'}
    local = ('monitor', 'bus', 'module')
    nodes = [
        node(f'{part}_{column}', names[part] if literal_duplicates else '{{' + part + '.name}}',
             [40 + index * 290, ordinate, 280 + index * 290, ordinate + 90])
        for column, ordinate in [(1, 150), (2, 510)]
        for index, part in enumerate(local)
    ]
    nodes += [
        node(part, '{{' + part + '.name}}', [40 + index * 290, 330, 280 + index * 290, 420])
        for index, part in enumerate(('switch', 'central', 'computer'), 3)
    ]
    links = [
        ('monitor_1', 'bus_1', [[280, 195], [330, 195]]),
        ('bus_1', 'module_1', [[570, 195], [620, 195]]),
        ('monitor_2', 'bus_1', [[280, 555], [305, 555], [305, 225], [330, 225]]) if crossed else
        ('monitor_2', 'bus_2', [[280, 555], [330, 555]]),
        ('bus_2', 'module_2', [[570, 555], [620, 555]]),
        ('module_1', 'switch', [[860, 195], [1030, 195], [1030, 330]]),
        ('module_2', 'switch', [[860, 555], [1030, 555], [1030, 420]]),
        ('switch', 'central', [[1150, 375], [1200, 375]]),
        ('central', 'computer', [[1440, 375], [1490, 375]]),
    ]
    edges = [{'source': start, 'target': end, 'points': points, 'bidirectional': True}
             for start, end, points in links]
    graph = view('network', 'topology', names, nodes, edges, width=1850)
    return setup(tmp_path, source, ['electrical'], names, [graph])


def test_multicolumn_communication_binds_each_drawn_endpoint(tmp_path):
    workspace, wid, _ = multi_column(tmp_path)
    result = workspace.plan(wid, '规划')
    assert 'blocking_state' not in result
    groups = [row['placements'] for row in result['adaptation']['source_relationships']]
    assert [len(group) for group in groups] == [2, 2, 2, 1, 1]
    assert [row['node_ids'] for row in groups[0]] == [['monitor_1', 'bus_1'], ['monitor_2', 'bus_2']]
    assert groups[0][0]['part_ids'] == groups[0][1]['part_ids'] == ['monitor', 'bus']


@pytest.mark.parametrize('options', [
    {'disclosed': False},
    {'literal_duplicates': True},
    {'crossed': True},
])
def test_unproven_multi_column_connections_stay_blocked(tmp_path, options):
    workspace, wid, planner = multi_column(tmp_path, **options)
    with pytest.raises(SourceCoverageIncomplete, match='COMMUNICATION-DIRECTION|DIRECTION-SOURCE'):
        workspace.stage(wid, workspace_plan(workspace, wid, planner))


def beam_workspace(tmp_path, *, wrong_feedback=False):
    names = {'tx': '发射设备', 'rx': '接收设备', 'panel': '天线面板', 'angular': '角度范围',
             'beam': '发射波束方向', 'message': '参考信号', 'feedback': '测量反馈'}
    angle = view('angle', 'relationship', ['panel', 'angular', 'beam'], [], [])
    angle['diagram'] = panel_range_diagram('panel', 'angular', 'beam')
    nodes = [
        node('select', '选择接收波束', [770, 80, 1220, 170]),
        node('send_reference', '发送{{message.name}}', [80, 290, 530, 385]),
        node('measure', '用选定波束接收{{message.name}}并测量', [770, 290, 1220, 385]),
        node('feedback', '回传{{feedback.name}}', [770, 505, 1220, 600]),
        node('receipt', '接收{{feedback.name}}', [80, 505, 530, 600]),
    ]
    edges = [
        {'source': 'select', 'target': 'measure'},
        {'source': 'send_reference', 'target': 'measure', 'label': '{{message.name}}'},
        {'source': 'measure', 'target': 'feedback'},
        {'source': 'feedback', 'target': 'send_reference' if wrong_feedback else 'receipt',
         'label': '{{feedback.name}}', 'label_at': [555, 505] if wrong_feedback else [555, 509]},
    ]
    flow = view('receive', 'flowchart', names, nodes, edges, width=1300)
    return setup(tmp_path, BEAM, ['communication'], names, [angle, flow])


def test_combined_beam_flow_is_preserved_and_independent_views_are_supplied(tmp_path):
    workspace, wid, _ = beam_workspace(tmp_path)
    result = workspace.plan(wid, '规划')
    assert 'blocking_state' not in result
    assert [v['id'] for v in result['plan']['views']] == [
        'angle', 'receive', 'beam_device_interaction', 'beam_message_dependencies']
    workspace.stage(wid, result['plan'])
    for view in result['plan']['views']:
        workspace.render(wid, view['id'])
    # The synthetic model response declares five unbound marks in its combined flow.
    assert workspace.execution_state(wid)['blocking_state'] == 'visible_marks_missing'
    with zipfile.ZipFile(io.BytesIO(workspace.package_partial(wid)['zip'])) as archive:
        assert {'figures/angle.png', 'figures/receive.svg',
                'figures/beam_device_interaction.svg', 'figures/beam_message_dependencies.svg'} <= set(archive.namelist())


def test_combined_flow_with_unproven_feedback_endpoint_is_excluded_from_partial(tmp_path):
    workspace, wid, _ = beam_workspace(tmp_path, wrong_feedback=True)
    result = workspace.plan(wid, '规划')
    assert any(row['rule'] == 'SEQUENCE-EDGE-INTENT' and row['edge'] == ['feedback', 'send_reference']
               for row in result['domain_findings'])
    assert result['completed_ids'] == [
        'angle', 'beam_device_interaction', 'beam_message_dependencies', 'beam_receive_measurement']
    assert result['missing_ids'] == ['receive']

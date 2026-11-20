import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import zipfile
from xml.etree import ElementTree

import httpx
import pypdfium2
import pytest

from patent_drawing.coverage import relationship_coverage, source_drawing_gaps
from patent_drawing.marking import visible_vector_marks
from patent_drawing.operation_store import OperationStore
from patent_drawing.text_client import OpenAITextClient
from patent_drawing.workspace import Workspace


ARCHIVE_ROOT = Path(os.getenv('PATENT_V0376_REPLAY_ROOT',
                              '.offline-replay/v0375'))
FOLLOWUP = 'followup-20261115/records/jtti-native'
BUILD_07 = 'implementation/native/build-071/v375-live-resume-final.tar.gz'
ARCHIVES = {
    'TC-003': (
        FOLLOWUP + '/v375-live-originals.tar.gz',
        'bcf0abe511cc5d770c6a9d5790309887cc4880f969497b03e0a308d5005d2a87',
        'c365983fd063330a226b227c8193b2bb9a255cc2f8ae9724193da6e9d15b219d',
        'ba872893e7c740510cb6f038a7ae7daab58549028b4a250c688db5580aed57a4',
    ),
    'TC-006': (
        BUILD_07, '408d30ad773574021484b658cc9224c5021a06d0858a8da77e178ce44a1ba6df',
        'fdf03ac430f174bd11cc32fa55c70ac0c582803028b5f02293b80139ce588ea5',
        '744354a5333399ff744dfab9c50c4146d77abc1444b17b187b5355b12c971bac',
    ),
    'TC-016': (
        BUILD_07, '776cc4fea66d09e7670ad356e2850c36daf0c68f8f7e5716aadc2d6686368812',
        '3a6160ddc02d6aeb837fba00054d8a3ffa23b4340d67737086d185d2b630fbdc',
        '948c131951a1ee4f5716e2375f24166778cb4b5a665079bbddadaa46e1c1d850',
    ),
}


def archived_workspace(tmp_path, case, *, supplement=None):
    archive, slot, response_sha, source_sha = ARCHIVES[case]
    sources = ARCHIVE_ROOT / FOLLOWUP / 'v375-native-final.tar.gz'
    if not sources.is_file() or not (ARCHIVE_ROOT / archive).is_file():
        pytest.skip('Frozen offline replay archives are unavailable')
    with tarfile.open(sources) as tar:
        with zipfile.ZipFile(io.BytesIO(tar.extractfile('v375-native-fixtures.zip').read())) as fixtures:
            source = fixtures.read(case + '.pdf')
    with tarfile.open(ARCHIVE_ROOT / archive) as tar:
        prefix = f'provider/{slot}/'
        body = tar.extractfile(prefix + 'response.bin').read()
        request = json.load(tar.extractfile(prefix + 'request.json'))
        receipt = json.load(tar.extractfile(prefix + 'headers.json'))
    assert hashlib.sha256(source).hexdigest() == source_sha
    assert hashlib.sha256(body).hexdigest() == response_sha
    assert receipt['status'] == 200
    content = next(row['content'] for row in request['input'] if row['role'] == 'user')
    context = json.loads(content[0]['text'] if isinstance(content, list) else content)
    assert context['source_bundle']['sources'][0]['sha256'] == source_sha
    replayed = []

    def replay(outbound):
        assert outbound.method == 'POST' and outbound.url.path == '/v1/responses'
        assert not replayed
        replayed.append(outbound.url.path)
        return httpx.Response(200, content=body, headers={'x-request-id': receipt['headers']['x-request-id']})

    store = OperationStore(tmp_path)
    client = OpenAITextClient('mock-archive-only', transport=httpx.MockTransport(replay))
    workspace = Workspace(store, 'archive-replay', text_client=client, allow_models=True)
    inputs = [{'name': case + '.pdf', 'blob': source}]
    if supplement:
        inputs.append({'name': 'supplement.txt', 'blob': supplement.encode()})
    wid = workspace.ingest(inputs, technical_types=context['technical_types'], patent_type=context['patent_type'],
                           application_stage=context['application_stage'])['workspace_id']
    result = workspace.plan(wid, context['query'])
    assert replayed == ['/v1/responses']
    state = workspace.read(wid)
    history = state['planning_history'][-1]
    plan = state['proposal'] or json.loads(store.get_artifact(workspace.owner, history['unapproved_candidate']))
    return workspace, wid, result, plan


def package(workspace, wid, plan, *, partial=False):
    if partial:
        return workspace.package_partial(wid, audit=True)
    workspace.stage(wid, plan)
    for view in plan['views']:
        workspace.render(wid, view['id'])
    return workspace.package(wid)


def arrowheads(svg):
    root = ElementTree.fromstring(svg)
    return sum(node.tag.endswith('polygon') and node.get('fill') == 'black' for node in root.iter())


def test_actual_account_plan_has_an_independent_visible_fourth_view(tmp_path):
    workspace, wid, result, plan = archived_workspace(tmp_path, 'TC-003')
    assert 'blocking_state' not in result
    assert [view['id'] for view in plan['views'][:3]] == ['V1', 'V2', 'V3']
    assert len(plan['views']) == 4
    assert not any('接收并应用' in node['text'] for node in plan['views'][0]['diagram']['nodes'])
    assert any('必须隔离' in node['text'] for node in plan['views'][0]['diagram']['nodes'])
    output = package(workspace, wid, plan)
    fourth = plan['views'][3]['id']
    with zipfile.ZipFile(io.BytesIO(output['zip'])) as z:
        svg = z.read(f'figures/{fourth}.svg')
        assert all(text.encode() in svg for text in ('账号甲', '账号乙', '本地待同步队列'))
        assert arrowheads(svg) == 2
    with pypdfium2.PdfDocument(output['pdf']) as pdf:
        assert len(pdf) == 4
    wrong = copy.deepcopy(plan)
    wrong['views'].pop()
    wrong['views'][0]['title'] = '账号与队列隔离关系图'
    state = workspace.read(wid)
    gaps = source_drawing_gaps(state['bundle'], wrong, state['technical_types'])
    assert [row['rule'] for row in gaps] == ['SYNC-ACCOUNT-ISOLATION']
    updated = copy.deepcopy(plan)
    next(part for part in updated['parts'] if part['id'] == 'queue')['name'] = '离线待同步队列'
    updated['revision'] = 2
    staged = workspace.stage(wid, updated, require_linked_confirmation=True)
    assert staged['queue'] == ['V1', 'V2', fourth]
    for fid in staged['queue']:
        workspace.render(wid, fid)
    revised = workspace.package(wid)
    assert workspace.package(wid, export_current=True, revision=1)['zip'] == output['zip']
    with (zipfile.ZipFile(io.BytesIO(output['zip'])) as before,
          zipfile.ZipFile(io.BytesIO(revised['zip'])) as after):
        assert before.read('figures/V3.png') == after.read('figures/V3.png')
        assert '离线待同步队列'.encode() in after.read(f'figures/{fourth}.svg')


def test_actual_cabinet_keeps_three_safe_views_and_reports_unbound_auxiliary_rail(tmp_path):
    workspace, wid, result, plan = archived_workspace(tmp_path, 'TC-006')
    assert result['blocking_state'] == 'planning_coverage_incomplete'
    assert any(row['rule'] == 'CABINET-AUXILIARY-RAIL' for row in result['domain_findings'])
    assert len(plan['views']) == 3
    assert 'auxiliary_rail' not in plan['views'][0]['parts']
    assert 'auxiliary_rail' in {part['id'] for part in plan['parts']}
    output = package(workspace, wid, plan, partial=True)
    with zipfile.ZipFile(io.BytesIO(output['zip'])) as z:
        assert arrowheads(z.read('figures/v1.svg')) == 4
        assert arrowheads(z.read('figures/v2.svg')) == 16
        assert '连接列'.encode() in z.read(f"figures/{plan['views'][2]['id']}.svg")
    with pypdfium2.PdfDocument(output['pdf']) as pdf:
        assert len(pdf) == 3
    imported_store = OperationStore(tmp_path / 'native-import')
    imported = Workspace(imported_store, 'archive-replay')
    original = workspace.store.get_artifact(workspace.owner, workspace.read(wid)['original_files'][0])
    second = imported.ingest([{'name': 'TC-006.pdf', 'blob': original}],
                             technical_types=['electrical'])['workspace_id']
    staged = imported.stage(second, plan, require_complete_marks=True)
    assert staged['blocking_state'] == 'planning_coverage_incomplete'
    assert staged['completed_ids'] == result['completed_ids']
    assert imported.read(second)['current'] is None
    assert imported.package_partial(second)['deliverable_status'] == 'incomplete_draft'
    with pytest.raises(ValueError, match='no complete/staged set'):
        imported.package(second)


@pytest.mark.parametrize('prefix, qualified', [('', True), ('待确认：', False), ('错误示例：', False)])
def test_auxiliary_rail_needs_an_affirmative_located_path(tmp_path, prefix, qualified):
    supplement = prefix + '供电板的输出端经辅助供电轨向监测控制单元的辅助电源输入端供电。'
    workspace, wid, result, plan = archived_workspace(tmp_path, 'TC-006', supplement=supplement)
    if not qualified:
        assert result['blocking_state'] == 'planning_coverage_incomplete'
        assert plan['views'][0]['id'] in result['completed_ids']
        assert all(node['id'] != 'rail' for node in plan['views'][0]['diagram']['nodes'])
        return
    assert 'blocking_state' not in result
    power = plan['views'][0]
    pairs = {(edge['source'], edge['target']) for edge in power['diagram']['edges']}
    assert {('board', 'rail'), ('rail', 'unit')} <= pairs
    assert ('board', 'unit') not in pairs
    source = next(fact for fact in plan['facts'] if fact['id'].endswith('_rail'))
    assert source['evidence'][0]['source_sha256'] == hashlib.sha256(supplement.encode()).hexdigest()
    output = package(workspace, wid, plan)
    with zipfile.ZipFile(io.BytesIO(output['zip'])) as archive:
        assert arrowheads(archive.read('figures/v1.svg')) == 5
        assert arrowheads(archive.read('figures/v2.svg')) == 16


def test_actual_beam_plan_draws_marks_and_two_independent_views(tmp_path):
    workspace, wid, result, plan = archived_workspace(tmp_path, 'TC-016')
    assert 'blocking_state' not in result
    assert len(plan['views']) == 4
    parts = {part['id']: part for part in plan['parts']}
    output = package(workspace, wid, plan)
    with zipfile.ZipFile(io.BytesIO(output['zip'])) as z:
        svg = z.read('figures/v1.svg')
        assert visible_vector_marks(plan['views'][0], parts, svg) == {'panel': '3', 'range': '4', 'tx_beam': '5'}
        assert arrowheads(z.read(f"figures/{plan['views'][2]['id']}.svg")) == 3
    with pypdfium2.PdfDocument(output['pdf']) as pdf:
        assert len(pdf) == 4
    state = workspace.read(wid)
    wrong = copy.deepcopy(plan)
    wrong['views'][0]['diagram']['primitives'][-1]['target_shape'] = 'panel_outline'
    assert 'BEAM-ANGLE-MARKS' in {row['rule'] for row in
                                  source_drawing_gaps(state['bundle'], wrong, state['technical_types'])}
    wrong = copy.deepcopy(plan)
    wrong['views'][2]['diagram']['edges'][-1].update(source='transmitter', target='receiver')
    wrong['views'][3]['diagram']['edges'].append({'source': 'select_beam', 'target': 'send_reference'})
    gaps, _ = relationship_coverage(state['bundle'], wrong, state['technical_types'])
    assert {(row['rule'], row.get('view_id')) for row in gaps} >= {
        ('SEQUENCE-EDGE-INTENT', plan['views'][2]['id']),
        ('SEQUENCE-EDGE-DEPENDENCY', plan['views'][3]['id']),
        ('BEAM-VIEW-COVERAGE', None),
    }
    updated = copy.deepcopy(plan)
    next(part for part in updated['parts'] if part['id'] == 'tx')['label'] = '101'
    updated['revision'] = 2
    workspace.stage(wid, updated, require_linked_confirmation=True)
    confirmation = workspace.linked_edit_preview(wid)
    assert confirmation['visible_edit_view_ids'] == [view['id'] for view in plan['views'][1:]]
    assert workspace.confirm_linked_edit(wid, confirmation['confirmation_code'])['confirmed']
    for fid in workspace.resume(wid)['queue']:
        workspace.render(wid, fid)
    revised = workspace.package(wid)
    assert workspace.package(wid, export_current=True, revision=1)['pdf'] == output['pdf']
    assert workspace.package(wid, export_current=True, revision=1)['zip'] == output['zip']
    with (zipfile.ZipFile(io.BytesIO(output['zip'])) as before,
          zipfile.ZipFile(io.BytesIO(revised['zip'])) as after):
        assert before.read('figures/v1.png') == after.read('figures/v1.png')
        assert before.read('figures/v1.svg') == after.read('figures/v1.svg')

"""Replay the exact build-02 continuations without live credentials or network."""
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import zipfile

import httpx
import pypdfium2
import pytest

from patent_drawing.marking import visible_vector_marks
from patent_drawing.operation_store import OperationStore
from patent_drawing.text_client import OpenAITextClient
from patent_drawing.workspace import Workspace
from tests.test_v0376_natural_completion import ARCHIVE_ROOT, FOLLOWUP, arrowheads, package


BASE = Path(os.getenv('PATENT_V0377_REPLAY_ROOT', '.offline-replay/v0376'))
ARCHIVES = {
    '003': '44d2f4392caa2b18720e7b0550b68ec051f215b05c095213a44d85e3c6555993',
    '006': 'de330195761e872267acaccebd11455c6d2d37bcec5b3660c7616cfbd823206a',
    '016': '82189edb0a92b37d7e357dde5a8c5c97ebedca94db2cbcc68b3fbd1abb6e6b4c',
}


def replay_build02(tmp_path, case, source_extra=None):
    archive = BASE / 'credit-restored-01/records/credit01' / f'live-t2-{case}-credit01/live-originals-1.tar.gz'
    inputs = ARCHIVE_ROOT / FOLLOWUP / 'v375-native-final.tar.gz'
    if not (archive.is_file() and inputs.is_file()) and not os.getenv('PATENT_V0377_REQUIRE_ORIGINALS'):
        pytest.skip('Private frozen build-02 inputs unavailable')
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == ARCHIVES[case]
    receipt = json.loads(archive.with_name(archive.name + '.verified.json').read_bytes())
    sol, = (row for row in receipt['requests'] if row['model'] == 'gpt-6.1-sol')
    with tarfile.open(archive) as original:
        prefix = f"provider/{sol['slot']}/"
        body = original.extractfile(prefix + 'response.bin').read()
        req = original.extractfile(prefix + 'request.json').read()
        assert hashlib.sha256(body).hexdigest() == sol['response_sha256']
        assert hashlib.sha256(req).hexdigest() == sol['request_sha256'] and sol['status'] == 200
    user = next(row['content'] for row in json.loads(req)['input'] if row['role'] == 'user')
    context = json.loads(user[0]['text'] if isinstance(user, list) else user)
    with tarfile.open(inputs) as original:
        with zipfile.ZipFile(io.BytesIO(original.extractfile('v375-native-fixtures.zip').read())) as z:
            pdf = z.read(f'TC-{case}.pdf')
    assert hashlib.sha256(pdf).hexdigest() == context['source_bundle']['sources'][0]['sha256']
    calls = []

    def respond(request):
        assert request.method == 'POST' and request.url.path == '/v1/responses'
        assert not calls
        calls.append('archived-response')
        return httpx.Response(200, content=body, headers={'x-request-id': sol['request_id']})

    client = OpenAITextClient('mock-archive-only', transport=httpx.MockTransport(respond))
    w = Workspace(OperationStore(tmp_path), 'archive-replay', text_client=client, allow_models=True)
    entries = [{'name': f'TC-{case}.pdf', 'blob': pdf}]
    if source_extra:
        entries.append({'name': 'supplement.txt', 'blob': source_extra.encode()})
    wid = w.ingest(entries, technical_types=context['technical_types'], patent_type=context['patent_type'],
                   application_stage=context['application_stage'])['workspace_id']
    result = w.plan(wid, context['query'])
    state = w.read(wid)
    plan = state['proposal']
    if plan is None:
        entry = state['planning_history'][-1]
        plan = json.loads(w.store.get_artifact(w.owner, entry['unapproved_candidate']))
    assert calls == ['archived-response']
    return w, wid, result, plan


def test_account_roles_from_build02_are_bound_by_source_semantics(tmp_path):
    w, wid, result, plan = replay_build02(tmp_path, '003')
    assert 'blocking_state' not in result, result.get('domain_findings')
    assert [v['id'] for v in plan['views']] == ['v1', 'v2', 'v3', 'v4', 'v5']
    with zipfile.ZipFile(io.BytesIO(package(w, wid, plan)['zip'])) as z:
        svg = z.read('figures/v4.svg')
        assert arrowheads(svg) == 2
        assert all(text.encode() in svg for text in ('归属', '账号甲', '账号乙'))


def test_cabinet_build02_recovers_supply_without_unverified_rail(tmp_path):
    w, wid, result, plan = replay_build02(tmp_path, '006')
    assert {finding['rule'] for finding in result.get('domain_findings', [])} >= {'CABINET-AUXILIARY-RAIL'}
    assert not any(f['rule'] == 'CABINET-SUPPLY-DIRECTION' for f in result['domain_findings'])
    rendered = package(w, wid, plan, partial=True)
    with pypdfium2.PdfDocument(rendered['pdf']) as doc:
        assert len(doc) == 3
    with zipfile.ZipFile(io.BytesIO(rendered['zip'])) as z:
        a, b = [z.read(f'figures/{view["id"]}.svg') for view in plan['views'][:2]]
        assert (arrowheads(a), arrowheads(b)) == (4, 16)
        assert '辅助供电轨'.encode() not in a


def test_cabinet_build02_accepts_located_auxiliary_path_and_renumbers(tmp_path):
    from copy import deepcopy

    supplement = '技术补证测试夹具。供电板的输出端经辅助供电轨向监测控制单元的辅助电源输入端供电。'
    w, wid, result, plan = replay_build02(tmp_path, '006', supplement)
    assert not result.get('blocking_state'), result.get('domain_findings')
    supply = plan['views'][0]['diagram']
    assert {(row['source'], row['target']) for row in supply['edges']} == {
        ('supply', 'protection'), ('protection', 'monitor'), ('protection', 'load'),
        ('board', 'rail'), ('rail', 'monitor'),
    }
    before = package(w, wid, plan)
    revised = deepcopy(plan)
    revised['revision'] = 2
    monitor = next(part for part in revised['parts'] if part['name'] == '监测控制单元')
    monitor['label'] = '104'
    w.stage(wid, revised, require_linked_confirmation=True)
    preview = w.linked_edit_preview(wid)
    assert preview['ready'] and w.confirm_linked_edit(wid, preview['confirmation_code'])['confirmed']
    for fid in w.resume(wid)['queue']:
        w.render(wid, fid)
    after = w.package(wid)
    assert before['pdf'] == w.package(wid, export_current=True, revision=1)['pdf']
    with (zipfile.ZipFile(io.BytesIO(before['zip'])) as left,
          zipfile.ZipFile(io.BytesIO(after['zip'])) as right):
        changed = {v['id'] for v in plan['views']
                   if left.read(f"figures/{v['id']}.png") != right.read(f"figures/{v['id']}.png")}
        assert changed == set(preview['visible_edit_view_ids'])
        assert {'V1', 'V2'} <= changed


def test_beam_build02_has_four_views_and_versioned_edit(tmp_path):
    w, wid, result, plan = replay_build02(tmp_path, '016')
    assert 'blocking_state' not in result, result.get('domain_findings')
    assert len(plan['views']) == 4 and len({v['id'] for v in plan['views']}) == 4
    before = package(w, wid, plan)
    tx = next(part for part in plan['parts'] if part['name'] == '发射设备')
    assert tx['label'] == '1'
    tx['label'] = '101'
    plan['revision'] = 2
    w.stage(wid, plan, require_linked_confirmation=True)
    edit = w.linked_edit_preview(wid)
    angle = 'v_angle'
    assert angle not in edit['visible_edit_view_ids'] and len(edit['visible_edit_view_ids']) >= 2
    assert w.confirm_linked_edit(wid, edit['confirmation_code'])['confirmed']
    for fid in w.resume(wid)['queue']:
        w.render(wid, fid)
    after = w.package(wid)
    history = w.package(wid, export_current=True, revision=1)
    assert (history['pdf'], history['zip']) == (before['pdf'], before['zip'])
    with (zipfile.ZipFile(io.BytesIO(before['zip'])) as left,
          zipfile.ZipFile(io.BytesIO(after['zip'])) as right):
        assert left.read(f'figures/{angle}.png') == right.read(f'figures/{angle}.png')
        marks = visible_vector_marks(plan['views'][0], {p['id']: p for p in plan['parts']},
                                     right.read(f'figures/{angle}.svg'))
        assert set(marks.values()) == {'3', '4', '5'}


def test_cabinet_build02_communication_typography_and_arrow_ink_at_small_scale(tmp_path):
    from xml.etree import ElementTree

    w, wid, _, plan = replay_build02(tmp_path, '006')
    output = package(w, wid, plan, partial=True)
    with zipfile.ZipFile(io.BytesIO(output['zip'])) as archive:
        root = ElementTree.fromstring(archive.read('figures/V2.svg'))
    ns = '{http://www.w3.org/2000/svg}'
    marks = [int(node.get('font-size')) for node in root.iter(ns + 'text') if node.get('data-mark-parts')]
    tips = [node.get('points') for node in root.iter(ns + 'polygon') if node.get('fill') == 'black']
    assert len(tips) == 16 and len(marks) >= 6
    with pypdfium2.PdfDocument(output['pdf']) as pdf:
        page = pdf[1]
        sample = page.render(scale=96 / 72 * 2 / 3)
        assert sample.width >= 500
        sample.close()
        images = [obj.get_bounds() for obj in page.get_objects() if obj.type == 3]
        left, _, right, _ = max(images, key=lambda box: box[2] - box[0])
        scale = (right - left) / int(root.get('width')) * 96 / 72 * 2 / 3
        assert min(marks) * scale >= 7
        assert all(abs(float(poly.split()[0].split(',')[0]) - float(poly.split()[1].split(',')[0])) * scale >= 3
                   or abs(float(poly.split()[0].split(',')[1]) - float(poly.split()[1].split(',')[1])) * scale >= 3
                   for poly in tips)


def test_beam_device_interaction_cannot_count_as_unrendered_angle(tmp_path):
    from patent_drawing.coverage import relationship_coverage
    from patent_drawing.errors import SourceCoverageIncomplete

    w, wid, _, plan = replay_build02(tmp_path, '016')
    angle = plan['views'].pop(0)
    interaction = next(view for view in plan['views'] if view['id'] == 'beam_device_interaction')
    interaction['parts'].extend(part for part in angle['parts'] if part not in interaction['parts'])
    interaction['required_facts'] = []
    state = w.read(wid)
    findings, _ = relationship_coverage(state['bundle'], plan, state['technical_types'])
    assert any(finding.get('missing_views') == ['相对角度'] for finding in findings)
    with pytest.raises(SourceCoverageIncomplete):
        w.stage(wid, plan)


def test_sync_protection_cannot_narrow_queue_to_one_record(tmp_path):
    from patent_drawing.coverage import source_drawing_gaps

    w, wid, _, plan = replay_build02(tmp_path, '003')
    view = next(view for view in plan['views'] if view['id'] == 'v2')
    view['diagram']['nodes'][-1]['text'] = '应用变更，不得覆盖未获确认的单条学习记录'
    state = w.read(wid)
    gaps = source_drawing_gaps(state['bundle'], plan, state['technical_types'])
    assert any(finding['rule'] == 'SYNC-INCREMENTAL' for finding in gaps)


def test_beam_angle_marks_required_when_planner_omits_fact_ids(tmp_path):
    from patent_drawing.coverage import relationship_coverage

    w, wid, _, plan = replay_build02(tmp_path, '016')
    angle = next(view for view in plan['views'] if view['id'] == 'v_angle')
    angle['required_facts'] = []
    angle['diagram']['primitives'] = [item for item in angle['diagram']['primitives']
                                      if item['type'] != 'label']
    state = w.read(wid)
    findings, _ = relationship_coverage(state['bundle'], plan, state['technical_types'])
    assert any(finding['rule'] == 'BEAM-ANGLE-MARKS' for finding in findings)


def test_sync_confirmation_rejects_swapped_branches(tmp_path):
    from patent_drawing.coverage import source_drawing_gaps

    w, wid, _, plan = replay_build02(tmp_path, '003')
    view = next(view for view in plan['views'] if view['id'] == 'v1')
    gate = next(node for node in view['diagram']['nodes'] if node['id'] == 'n8')
    gate['branches']['yes'], gate['branches']['no'] = gate['branches']['no'], gate['branches']['yes']
    for edge in view['diagram']['edges']:
        if edge['source'] == gate['id']:
            edge['branch'] = 'no' if edge['branch'] == 'yes' else 'yes'
    state = w.read(wid)
    gaps = source_drawing_gaps(state['bundle'], plan, state['technical_types'])
    assert any(finding['rule'] == 'SYNC-ACKNOWLEDGMENT' for finding in gaps)


def test_sync_cannot_deliver_cross_account_drawing_alongside_valid_view(tmp_path):
    from copy import deepcopy
    from patent_drawing.coverage import source_drawing_gaps

    w, wid, _, plan = replay_build02(tmp_path, '003')
    good = next(view for view in plan['views'] if view['id'] == 'v4')
    crossed = deepcopy(good)
    crossed['id'] = 'crossed_accounts'
    for edge in crossed['diagram']['edges']:
        edge['target'] = 'queue_b' if edge['source'] == 'account_a' else 'queue_a'
    plan['views'].append(crossed)
    context = w.read(wid)
    findings = source_drawing_gaps(context['bundle'], plan, context['technical_types'])
    assert any(finding['rule'] == 'SYNC-ACCOUNT-ISOLATION' and finding.get('view_id') == crossed['id']
               for finding in findings)


def test_previous_dense_topology_version_can_be_renumbered_without_relayout(tmp_path, monkeypatch):
    from copy import deepcopy
    from patent_drawing import workspace as workspace_module
    from tests.test_v0375_relationships import cabinet

    w, wid, _ = cabinet(tmp_path)
    plan = w.plan(wid, '规划')['plan']
    layers = workspace_module.vector_layers
    with monkeypatch.context() as old_renderer:
        old_renderer.setattr(workspace_module, 'PIPELINE_VERSION', 'drawing-pipeline-v039-6')

        def old_layers(parts, view, annotations, **options):
            return layers(parts, view, annotations, legacy_layout=True)

        old_renderer.setattr(workspace_module, 'vector_layers', old_layers)
        # Simulate the 0.3.76 verifier as well as its renderer for the old snapshot.
        drawn_edges = workspace_module.rendered_edge_findings
        old_renderer.setattr(workspace_module, 'rendered_edge_findings',
                             lambda *args, **kwargs: drawn_edges(*args, legacy_layout=True))
        before = package(w, wid, plan)
    revised = deepcopy(plan)
    revised['revision'] = 2
    target = next(part for part in revised['parts'] if part['name'] == '监测控制单元')
    target['label'] = '104'
    w.stage(wid, revised, require_linked_confirmation=True)
    edit = w.linked_edit_preview(wid)
    assert w.confirm_linked_edit(wid, edit['confirmation_code'])['confirmed']
    for fid in w.resume(wid)['queue']:
        w.render(wid, fid)
    w.package(wid)
    assert w.package(wid, export_current=True, revision=1)['zip'] == before['zip']

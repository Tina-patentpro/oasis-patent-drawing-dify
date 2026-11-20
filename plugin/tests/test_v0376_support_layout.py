"""Source-bound C callout regressions; restricted artwork stays outside the repo."""

import copy
import hashlib
import io
import json
import os
import zipfile
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageDraw

from patent_drawing.annotations import compose, outside_regions_unchanged, png, regular_font
from patent_drawing.engineering_layout import build_support_layout


def sha(blob):
    return hashlib.sha256(blob).hexdigest()


def rgb(blob):
    with Image.open(io.BytesIO(blob)) as image:
        return image.convert('RGB')


def bind_lower(source, spec, dot, edge, elbow, label_box):
    spec['detail']['lower'] = {
        'source_point': dot, 'crop_exit': edge, 'source_elbow': elbow,
        'source_label': '12', 'source_label_box': label_box,
        'source_label_pixels_sha256': sha(source.crop(tuple(label_box)).tobytes()),
    }


@pytest.fixture
def c_source():
    source = Image.new('RGB', (460, 350), 'white')
    draw = ImageDraw.Draw(source)
    for x in (45, 175, 310):
        draw.line((x, 38, x, 120), fill='black', width=2)
    draw.line((300, 70, 341, 43), fill='black')
    draw.ellipse((297, 67, 303, 73), fill='black')
    draw.line([(299, 90), (341, 105), (386, 121), (434, 121)], fill='black')
    draw.ellipse((296, 87, 302, 93), fill='black')
    draw.text((396, 95), '12', font=regular_font(26), anchor='lt', fill='black')
    draw.rectangle([224, 190, 338, 320], outline='black')
    spec = {
        'source_png_sha256': sha(png(source)),
        'windows': [{'marker': marker, 'source_box': [x - 30, 20, x + 32, 140],
                     'upper': [x, 38], 'lower': [x, 120]}
                    for marker, x in zip('ABC', (45, 175, 310))],
        'detail': {'source_box': [215, 177, 360, 337], 'vertical': [224, 206],
                   'spring': {'source_point': [300, 70], 'crop_exit': [341, 43]}},
    }
    bind_lower(source, spec, [299, 90], [341, 105], [386, 121], [394, 93, 430, 118])
    roles = {'upper': 'board', 'spring': 'coil', 'lower': 'holder', 'vertical': 'plate'}
    return source, spec, roles


def test_c_lower_mark_reuses_part_id_without_touching_source_windows(c_source):
    source, spec, roles = c_source
    old = copy.deepcopy(spec)
    old['detail'].pop('lower')
    prior = build_support_layout(png(source), old, roles)
    layout = build_support_layout(png(source), spec, roles)
    labels = {'board': '10', 'coil': '11', 'holder': '12', 'plate': '13'}
    layered = [{**row, 'label': labels[row['part_id']]} for row in layout['annotations']]
    rendered = rgb(compose(layout['base'], layered)['png'])

    schematic, closeup = [row for row in layered if row['part_id'] == 'holder']
    assert schematic['target'] == [202, 583]
    assert closeup['source_leader']['source_label'] == '12'
    assert closeup['source_leader']['source_point'] == [299, 90]
    assert closeup['source_leader']['crop_exit'] == [341, 105]
    assert [row for row in layout['annotations']
            if row.get('source_leader', {}).get('source_label') != '12'] == prior['annotations']
    assert layout['base'] == prior['base']
    assert layout['endpoint_review'] == prior['endpoint_review']
    assert [row['basis'] for row in layout['endpoint_review'][-2:]] == ['source_relation'] * 2

    for row in layout['crops'][:3]:
        expected = source.crop(tuple(row['source_box']))
        expected = expected.resize(tuple(n * row['scale'] for n in expected.size), Image.Resampling.NEAREST)
        assert rendered.crop(tuple(row['placed_box'])).tobytes() == expected.tobytes()
    right, y = closeup['target']
    assert right == layout['crops'][2]['placed_box'][2]
    assert any(rendered.getpixel((right, yy)) == (0, 0, 0)
               and any(rendered.getpixel((right - 1, yy + d))[0] < 128 for d in (-1, 0, 1))
               for yy in range(y - 1, y + 2))


@pytest.mark.parametrize('case', ['reversed', 'occluded', 'obscured-by-solid-ink', 'missing-label', 'wrong-glyph-digest'])
def test_c_lower_mark_is_withheld_without_supported_direction_and_source_trace(c_source, case):
    source, spec, roles = c_source
    lower = spec['detail']['lower']
    if case == 'reversed':
        lower['source_point'], lower['crop_exit'] = lower['crop_exit'], lower['source_point']
    elif case == 'occluded':
        ImageDraw.Draw(source).rectangle([364, 103, 374, 125], fill='white')
    elif case == 'obscured-by-solid-ink':
        ImageDraw.Draw(source).rectangle([357, 100, 373, 124], fill='black')
    elif case == 'missing-label':
        ImageDraw.Draw(source).rectangle([394, 93, 429, 117], fill='white')
        lower['source_label_pixels_sha256'] = sha(source.crop((394, 93, 430, 118)).tobytes())
    else:
        lower['source_label_pixels_sha256'] = '0' * 64
    spec['source_png_sha256'] = sha(png(source))
    with pytest.raises(ValueError, match='C lower leader'):
        build_support_layout(png(source), spec, roles)


@pytest.fixture
def frozen_tc001():
    if not (root := os.environ.get('PATENT_V0376_TC001_FIXTURE_ROOT')):
        pytest.skip('set PATENT_V0376_TC001_FIXTURE_ROOT to the frozen 0.3.75 archive')
    root = Path(root)
    from patent_drawing.materials import extract_pdf_image

    pdf = (root / 'frozen-focus-originals/TC-001.pdf').read_bytes()
    assert sha(pdf) == '4b389c59f8bdd5515e313e0a083976c4b91a7cda3708915e201ea4994cc4df05'
    source_png = extract_pdf_image(pdf, 1, 10)
    assert sha(source_png) == '8c6af647a50da3b09444d67708bbd648c76fd0626a3d52c01232c4ec32fe0c43'
    independent = (root / 'followup-20261115/records/jtti-native/TC001-independently-pixel-checked-source-zero.png').read_bytes()
    assert sha(independent) == 'f0da14ede69a21ce63140bf924c13a94d5bedbe1479ac9aecef38ae60d339722'
    source = rgb(source_png)
    assert source.tobytes() == rgb(independent).tobytes()
    legacy_name = 'followup-layout-20261115.ergmiAOY/verified-02/TC-001-support-layout.json'
    legacy = (root / legacy_name).read_bytes()
    assert sha(legacy) == '5bddb87d3571563ddeb6961a0c6b0dc85ebb10a2f95b254f84c31c46ae4e005c'
    spec = json.loads(legacy)
    bind_lower(source, spec, [470, 430], [543, 466], [714, 551], [727, 520, 758, 548])
    assert spec['detail']['lower']['source_label_pixels_sha256'] == 'c4c05a2da11748630d85b13db8337b283145adb7d7b9fabd2d1161da3a1e6092'
    return root, pdf, source_png, spec


def test_frozen_c_lower_leader_continues_from_object_10_and_renumbers_only_its_glyphs(frozen_tc001):
    _, _, source_png, spec = frozen_tc001
    roles = {'upper': 'board', 'spring': 'coil', 'lower': 'holder', 'vertical': 'plate'}
    layout = build_support_layout(source_png, spec, roles)
    base = layout['base']
    labels = {'board': '10', 'coil': '11', 'holder': '12', 'plate': '13'}

    def rendered():
        return compose(base, [{**row, 'label': labels[row['part_id']]} for row in layout['annotations']])

    prior = rendered()
    labels['holder'] = '112'
    revised = rendered()
    assert outside_regions_unchanged(prior['png'], revised['png'], prior['regions'] + revised['regions'])
    difference = ImageChops.difference(rgb(prior['png']), rgb(revised['png']))
    assert difference.crop((60, 610, 180, 670)).getbbox()
    assert difference.crop((1290, 1060, 1400, 1120)).getbbox()
    assert not difference.crop((0, 700, 1288, 1135)).getbbox()
    assert not difference.crop((1290, 800, 1400, 880)).getbbox()


@pytest.fixture
def pending_frozen_layout(frozen_tc001, tmp_path):
    from patent_drawing.operation_store import OperationStore
    from patent_drawing.text_client import decode_plan_response
    from patent_drawing.workspace import Workspace

    root, pdf, source_png, spec = frozen_tc001
    folder = root / 'frozen-focus-originals/TC-001-natural'
    saved_request = (folder / 'request.json').read_bytes()
    saved_response = (folder / 'response.bin').read_bytes()
    assert sha(saved_request) == '944943c635e37accb8d7e75000847813c4ad6a8e7d11487d55af7bdf17109792'
    assert sha(saved_response) == '7a1e8de94bc36d896d4847148b0f7f04df286e583207bc54533d0570118eb713'

    class ArchivedPlanner:
        def plan(self, system, user, *, images, evidence_sink):
            evidence_sink('request', saved_request)
            evidence_sink('response', saved_response)
            return decode_plan_response(saved_response, 'req_ef5abec151e9453f8e811b2cd35c5bce')

    store = OperationStore(tmp_path)
    workspace = Workspace(store, 'offline-tc001')
    wid = workspace.ingest([{'name': 'TC-001.pdf', 'blob': pdf}], technical_types=['mechanical'])['workspace_id']
    planner = Workspace(store, workspace.owner, allow_models=True, text_client=ArchivedPlanner())
    plan = planner.plan(wid, '规划：核对四支承和原图。')['plan']
    view_ids = [view['id'] for view in plan['views']]
    assert view_ids == ['v1', 'v2', 'support_relation', 'v3']
    assert workspace.stage(wid, plan, require_complete_marks=True)['required_image_calls'] == 0
    assert workspace.source_reference_blob(workspace.read(wid), plan['views'][2]['source_region_id'],
                                           intrinsic=True) == source_png
    return workspace, wid, spec


def test_frozen_support_confirmation_records_source_and_invalidates_a_failed_revision(pending_frozen_layout):
    workspace, wid, spec = pending_frozen_layout
    preview = workspace.preview_support_layout(wid, spec)
    prior = workspace.confirm_support_layout(wid, preview['preview_id'], 'source evidence reviewer')
    assert prior['affected_view_ids'] == ['support_relation']
    assert prior['source']['page'] == 1 and prior['source']['object_number'] == 10
    assert prior['source']['png_sha256'] == spec['source_png_sha256']
    assert prior['source']['pdf_sha256'] == '4b389c59f8bdd5515e313e0a083976c4b91a7cda3708915e201ea4994cc4df05'
    lower, = [row for row in prior['source_leaders'] if row.get('source_label') == '12']
    assert lower['source_point'] == [470, 430] and lower['crop_exit'] == [543, 466]
    assert lower['extension_region'][0] == 1274 and lower['label_at'][0] == 1297
    assert prior['endpoint_count'] == 8

    # Missing evidence must not silently fall back to the previously approved preview.
    bad = copy.deepcopy(spec)
    bad['detail'].pop('lower')
    with pytest.raises(ValueError, match='C lower leader'):
        workspace.preview_support_layout(wid, bad)
    blocked = workspace.execution_state(wid)
    assert blocked['blocked_view'] == 'support_relation'
    assert blocked['blocking_state'] == 'awaiting_support_layout'
    assert 'C 局部' in blocked['message'] and '托板' in blocked['message']
    with pytest.raises(ValueError, match='expired'):
        workspace.confirm_support_layout(wid, preview['preview_id'], 'source evidence reviewer')
    assert prior['preview_id'] in {row['preview_id'] for row in workspace.read(wid)['support_layout_history']}
    refreshed = workspace.preview_support_layout(wid, spec)
    assert refreshed['preview_id'] != preview['preview_id']
    assert workspace.read(wid)['support_layout_issue_history'][0]['message'] == blocked['message']


@pytest.mark.parametrize(('original', 'updated', 'visible'), [
    ('10', '110', {'v1', 'support_relation', 'v3'}),
    ('12', '112', {'v1', 'support_relation'}),
])
def test_frozen_four_figure_set_linked_edits_preserve_prior_bytes(pending_frozen_layout, original, updated, visible):
    workspace, wid, spec = pending_frozen_layout
    preview = workspace.preview_support_layout(wid, spec)
    workspace.confirm_support_layout(wid, preview['preview_id'], 'source evidence reviewer')

    def render_set():
        for fid in workspace.resume(wid)['queue']:
            assert workspace.render(wid, fid)['model_calls'] == 0
        return workspace.package(wid)

    before = render_set()
    prior = workspace.read(wid)['current']
    updated_plan = copy.deepcopy(prior['plan'])
    part = next(part for part in updated_plan['parts'] if part['label'] == original)
    part['label'] = updated
    updated_plan['revision'] += 1
    workspace.stage(wid, updated_plan, require_linked_confirmation=True, require_complete_marks=True)
    assert not workspace.linked_edit_preview(wid)['ready']
    roi, at = ([696, 24, 777, 69], [706, 40]) if original == '10' else ([723, 514, 775, 549], [724, 517])
    workspace.approve_source_label(wid, 'v1', part['id'], updated, roi=roi, at=at, font_size=27,
                                   reviewer='source evidence reviewer')
    edit = workspace.linked_edit_preview(wid)
    assert edit['ready'] and set(edit['visible_edit_view_ids']) == visible
    detail = next(row for row in edit['views'] if row['view_id'] == 'support_relation')
    assert len(detail['positions']) == (2 if original == '12' else 1)
    assert workspace.confirm_linked_edit(wid, edit['confirmation_code'])['confirmed']
    after = render_set()
    later = workspace.read(wid)['current']
    assert set(later['records']) == {'v1', 'v2', 'support_relation', 'v3'}
    assert {fid for fid, record in prior['records'].items()
            if record['png'] != later['records'][fid]['png']} == visible
    for fid, record in prior['records'].items():
        new = later['records'][fid]
        assert new['base'] == record['base']
        if fid in visible:
            assert new['annotation_outside_unchanged']
    assert [a['label'] for a in later['records']['support_relation']['annotations']
            if a['part_id'] == part['id']] == [updated] * (2 if original == '12' else 1)
    for entry, bundle in ((prior, before), (later, after)):
        restored = workspace.package(wid, export_current=True, revision=entry['plan']['revision'])
        assert restored['pdf'] == bundle['pdf'] and restored['zip'] == bundle['zip']
    with zipfile.ZipFile(io.BytesIO(after['zip'])) as z:
        descriptions = z.read('figure-descriptions.txt').decode()
        assert part['name'] in descriptions and updated in descriptions
    assert workspace.image_dispatch_count(wid) == 0

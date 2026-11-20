import base64
import copy
import hashlib
import io
import json
import zipfile

import pytest
from PIL import Image, ImageChops, ImageDraw

from patent_drawing.annotations import outside_regions_unchanged, png
from patent_drawing.operation_store import OperationStore, digest
from patent_drawing.workspace import Workspace


SUPPORT_QUOTE = '四个弓头弹簧组件分布在滑板安装板的前、后、左、右角部，并将滑板安装板弹性支承在托板上。'


def sha(blob):
    return hashlib.sha256(blob).hexdigest()


def fixture_workspace(tmp_path, source, types):
    workspace = Workspace(OperationStore(tmp_path), 'reviewer')
    wid = workspace.ingest([{'name': 'source.txt', 'blob': source.encode()}],
                           technical_types=types)['workspace_id']
    state = workspace.read(wid)
    plan = {
        'schema_version': 'drawing-set-2', 'set_id': 'fixture', 'revision': 1,
        'source_sha256': state['source_sha256'], 'title': '来源关系图',
        'patent_type': 'invention', 'technical_types': types, 'parts': [],
        'facts': [{'id': 'f', 'status': 'confirmed', 'text': source,
                   'evidence': [{'page_id': state['bundle']['pages'][0]['id'], 'quote': source}]}],
        'views': [], 'constraints': [], 'unresolved': [],
    }
    return workspace, wid, state, plan


def vector_view(fid, kind, parts):
    return {
        'id': fid, 'engine': 'vector', 'requested_type': kind, 'delivered_type': kind,
        'title': fid, 'purpose': '表达已披露动作。', 'instruction': '保持已披露顺序。',
        'parts': parts, 'diagram': {'width': 900, 'height': 700, 'nodes': [], 'edges': [],
            'primitives': [{'type': 'text', 'at': [95, 125], 'text': '原有动作流程'}]},
    }


def test_natural_support_plan_keeps_original_views_and_independent_flow(tmp_path):
    workspace, wid, state, proposal = fixture_workspace(tmp_path, SUPPORT_QUOTE, ['mechanical'])
    source = Image.new('RGB', (410, 340), 'white')
    candidate_pages = []
    ImageDraw.Draw(source).line([(50, 50), (300, 200)], fill='black')
    for i, kind in enumerate(('assembly', 'section'), 1):
        page = {**state['bundle']['pages'][0], 'id': 'page_' + str(i), 'page': i,
                'image': workspace.store.put_artifact(workspace.owner, png(source))}
        state['bundle']['pages'].append(page)
        candidate = 'original_' + str(i)
        candidate_pages.append({'id': candidate, 'page_id': page['id'], 'bbox': [0, 0, 410, 340],
                                'kind': 'image', 'object_number': i})
        view = {'id': 'source' + str(i), 'engine': 'source', 'requested_type': kind,
                'delivered_type': kind, 'title': f'图{i} 原图', 'purpose': '保留原图。',
                'instruction': '保留原图。', 'source_region_id': candidate, 'parts': []}
        if kind == 'section':
            view['source_section'] = True
        proposal['views'].append(view)
    state['bundle'].update(figure_presence='present', figure_candidates=candidate_pages)
    workspace.save(wid, state)
    proposal['parts'] = [
        {'id': 'upper', 'name': '滑板安装板', 'label': '10'},
        {'id': 'spring', 'name': '弓头弹簧组件', 'label': '11'},
        {'id': 'lower', 'name': '托板', 'label': '12'},
        {'id': 'vertical', 'name': '竖板', 'label': '13'},
    ]
    proposal['views'][0]['parts'] = [p['id'] for p in proposal['parts']]
    flow = vector_view('flow', 'flowchart', [])
    proposal['views'].append({**flow, 'title': '图3 独立动作图'})

    class SavedPlanner:
        def plan(self, system, user, **kwargs):
            return {'text': json.dumps(proposal)}

    workspace.allow_models = True
    workspace.text_client = SavedPlanner()
    normalized = workspace.plan(wid, '规划')['plan']
    assert [v['id'] for v in normalized['views']] == ['source1', 'source2', 'support_relation', 'flow']
    view = normalized['views'][2]
    assert (view['engine'], view['source_region_id'], view['required_facts']) == (
        'support_relation', 'original_1', ['f'])
    assert normalized['views'][3]['title'] == '图4 独立动作图'
    assert [v['source_extraction'] for v in normalized['views'][:2]] == [
        'intrinsic_bitmap', 'intrinsic_bitmap']
    stripped = copy.deepcopy(normalized)
    stripped['views'].pop(2)
    with pytest.raises(ValueError, match='SUPPORT-RELATION-VIEW'):
        workspace.stage(wid, stripped)
    assert workspace.stage(wid, normalized)['required_image_calls'] == 0


def test_support_layout_keeps_exact_source_pixels_and_eight_distinct_endpoints():
    from patent_drawing import engineering_layout

    raw = Image.new('RGB', (390, 350), 'white')
    draw = ImageDraw.Draw(raw)
    for x in (45, 175, 310):
        draw.line([(x, 38), (x, 120)], fill='black', width=2)
    draw.rectangle([224, 190, 338, 320], outline='black')
    draw.ellipse([257, 220, 303, 275], outline='black')
    source = png(raw)
    spec = {
        'source_png_sha256': sha(source),
        'windows': [{'marker': letter, 'source_box': [x - 30, 20, x + 32, 140],
                     'upper': [x, 38], 'lower': [x, 120]}
                    for letter, x in zip('ABC', (45, 175, 310))],
        'detail': {'source_box': [215, 177, 360, 337], 'spring': [224, 206], 'vertical': [257, 247]},
    }
    roles = {'upper': 'board', 'spring': 'coil', 'lower': 'holder', 'vertical': 'plate'}
    result = getattr(engineering_layout, 'build_support_layout', lambda *_: {})(source, spec, roles)
    assert result.get('endpoint_review') is not None, 'render must identify the eight visible endpoint marks'
    endpoint_rows = result['endpoint_review']
    assert [(r['marker'], r['side'], r['part_id']) for r in endpoint_rows] == [
        (letter, side, part) for letter in 'ABCD' for side, part in (('upper', 'board'), ('lower', 'holder'))]
    assert len({tuple(r['at']) for r in endpoint_rows}) == 8
    assert [r['source_pixel'] for r in endpoint_rows[-2:]] == [None, None]
    rendered = Image.open(io.BytesIO(result['base']))
    for row in result['crops']:
        region = raw.crop(tuple(row['source_box']))
        enlargement = max(1, row['scale'])
        assert row['scale'] >= 2
        expected = region.resize(tuple(size * enlargement for size in region.size), Image.Resampling.NEAREST)
        assert rendered.crop(tuple(row['placed_box'])).tobytes() == expected.tobytes()
    bad = copy.deepcopy(spec)
    bad['windows'][0]['upper'] = [20, 40]
    with pytest.raises(ValueError, match='source ink'):
        engineering_layout.build_support_layout(source, bad, roles)


def test_support_layout_continues_the_reviewed_c_spring_leader_outside_the_source_crop():
    from patent_drawing import engineering_layout
    from patent_drawing.annotations import compose

    raw = Image.new('RGB', (390, 350), 'white')
    draw = ImageDraw.Draw(raw)
    for x in (45, 175, 310):
        draw.line([(x, 38), (x, 120)], fill='black', width=2)
    draw.line([(300, 70), (341, 43)], fill='black')
    draw.ellipse((297, 67, 303, 73), fill='black')
    draw.rectangle([224, 190, 338, 320], outline='black')
    source = png(raw)
    spec = {
        'source_png_sha256': sha(source),
        'windows': [{'marker': letter, 'source_box': [x - 30, 20, x + 32, 140],
                     'upper': [x, 38], 'lower': [x, 120]}
                    for letter, x in zip('ABC', (45, 175, 310))],
        'detail': {'source_box': [215, 177, 360, 337], 'vertical': [224, 206],
                   'spring': {'source_point': [300, 70], 'crop_exit': [341, 43]}},
    }
    roles = {'upper': 'board', 'spring': 'coil', 'lower': 'holder', 'vertical': 'plate'}
    result = engineering_layout.build_support_layout(source, spec, roles)
    spring = next(row for row in result['annotations'] if row['part_id'] == 'coil')
    c = next(row for row in result['crops'] if row['marker'] == 'C')
    x, y, right, _ = c['placed_box']
    assert spring['target'][0] == right
    assert spring['source_leader']['source_point'] == [300, 70]
    assert spring['source_leader']['placed_point'] == [
        x + (300 - 280) * c['scale'] + c['scale'] // 2,
        y + (70 - 20) * c['scale'] + c['scale'] // 2,
    ]
    labels = {'board': '10', 'coil': '11', 'holder': '12', 'plate': '13'}
    annotations = [{**row, 'label': labels[row['part_id']]} for row in result['annotations']]
    rendered = Image.open(io.BytesIO(compose(result['base'], annotations)['png']))
    base = Image.open(io.BytesIO(result['base']))
    for row in result['crops'][:3]:
        box = tuple(row['placed_box'])
        assert rendered.crop(box).tobytes() == base.crop(box).tobytes()
    _, join_y = spring['target']
    assert any(rendered.getpixel((right, y)) == (0, 0, 0)
               and any(base.getpixel((right - 1, y + dy)) == (0, 0, 0) for dy in (-1, 0, 1))
               for y in range(join_y - 1, join_y + 2))
    bad = copy.deepcopy(spec)
    bad['detail']['spring']['source_point'] = [310, 120]
    with pytest.raises(ValueError, match='continuous source leader'):
        engineering_layout.build_support_layout(source, bad, roles)


def archived_fixture(tmp_path):
    quote = '发射设备定义相对于自身天线面板的角度范围，其中包含至少两个发射波束方向。'
    workspace, wid, state, plan = fixture_workspace(tmp_path, quote, ['communication'])
    geometry = {'panel': [60, 220, 310, 630], 'origin': [310, 420],
                'boundaries': [[1020, 110], [1020, 730]], 'beams': [
                    {'tip': [930, 320], 'head': [[901, 315], [903, 334]]},
                    {'tip': [933, 519], 'head': [[905, 505], [902, 525]]}]}
    raw = Image.new('RGB', (1200, 840), 'white')
    draw = ImageDraw.Draw(raw)
    draw.rectangle(geometry['panel'], outline='black', width=4)
    for end in geometry['boundaries']:
        draw.line([tuple(geometry['origin']), tuple(end)], fill='black', width=4)
    for beam in geometry['beams']:
        draw.line([tuple(geometry['origin']), tuple(beam['tip'])], fill='black', width=4)
        draw.polygon([tuple(beam['tip']), *map(tuple, beam['head'])], fill='black')
    image = png(raw)
    plan['parts'] = [
        {'id': 'panel', 'name': '天线面板', 'label': '2'},
        {'id': 'range', 'name': '角度范围', 'label': '4'},
        {'id': 'beam', 'name': '发射波束方向', 'label': '5'},
    ]
    plan['views'] = [{
        'id': 'v1', 'engine': 'image', 'requested_type': 'relationship', 'delivered_type': 'relationship',
        'title': '角度范围', 'purpose': '定性表达天线面板、角度范围及两个发射波束方向。',
        'instruction': '二维示意面板和波束方向。', 'parts': ['panel', 'range', 'beam'], 'required_facts': ['f'],
        'archived_image_sha256': sha(image),
    }]
    request = b'{"model":"gpt-image-2.6-flare","output_format":"png"}'
    response = json.dumps({'data': [{'b64_json': base64.b64encode(image).decode()}]}).encode()
    receipt = {'http_status': 200, 'request_id': 'req_fixture', 'source_sha256': state['source_sha256'],
               'request_sha256': sha(request), 'response_sha256': sha(response)}
    return workspace, wid, plan, image, geometry, request, response, receipt


def test_archived_png_keeps_exact_bytes_without_opening_a_dispatch_slot(tmp_path):
    workspace, wid, plan, image, geometry, request, response, receipt = archived_fixture(tmp_path)
    assert workspace.stage(wid, plan)['required_image_calls'] == 0
    pending = workspace.read(wid)['work']['plan_sha256']
    assert workspace.preflight(wid)['blocking_state'] == 'awaiting_archived_image'
    with pytest.raises(ValueError, match='plan'):
        workspace.import_archived_image(wid, 'v1', image, plan_sha256=sha(b'different plan'),
                                        request=request, response=response, receipt=receipt)
    result = workspace.import_archived_image(wid, 'v1', image, plan_sha256=pending, request=request,
                                             response=response, receipt=receipt)
    assert result['record']['base']['sha256'] == sha(image)
    assert workspace.read_archived_image(wid, 'v1', sha(image)) == image
    assert workspace.image_dispatch_count(wid) == 0
    assert workspace.preflight(wid)['blocking_state'] == 'awaiting_visual_review'


def test_archived_beam_rejects_old_approval_and_requires_both_range_leaders(tmp_path):
    workspace, wid, plan, image, geometry, request, response, receipt = archived_fixture(tmp_path)
    workspace.stage(wid, plan)
    work = workspace.read(wid)['work']
    import_image = getattr(workspace, 'import_archived_image', None)
    assert import_image is not None, 'preserve the archived image in the independent staged revision'
    record = import_image(wid, 'v1', image, plan_sha256=work['plan_sha256'], request=request,
                          response=response, receipt=receipt)['record']
    contract = work['plan']['visibility_contract']['v1']
    region = [50, 100, 1040, 750]
    observation = {'view_id': 'v1', 'base_sha256': sha(image), 'spec_sha256': record['base_spec_sha256'],
                   'contract_sha256': digest(contract), 'reviewer': 'synthetic engineering replay',
                   'reviewed_at': '2026-11-14T11:00:00+08:00', 'features': [
                       {'feature_id': 'panel-beam-common-origin', 'verdict': 'PASS',
                        'region': region, 'note': '四条线贴同根，外边界无箭。', 'beam_geometry': geometry},
                       {'feature_id': 'source-fidelity', 'verdict': 'PASS', 'region': region, 'note': '按交底定性表达。'},
                   ]}
    workspace.review_visibility(wid, 'v1', observation)
    anchors = [
        {'part_id': 'panel', 'at': [11, 385], 'target': [60, 409],
         'target_region': [54, 401, 66, 417], 'font_size': 48},
        {'part_id': 'range', 'at': [1095, 360], 'target': [1020, 110],
         'target_region': [1013, 103, 1028, 118], 'font_size': 48},
        {'part_id': 'beam', 'at': [791, 425], 'target': [850, 505],
         'target_region': [844, 500, 856, 511], 'font_size': 48},
    ]
    with pytest.raises(ValueError, match='base'):
        workspace.approve_image_annotations(wid, 'v1', anchors, 'operator', base_sha256=sha(b'old'),
                                            plan_sha256=work['plan_sha256'])
    with pytest.raises(ValueError, match='both.*boundar'):
        workspace.approve_image_annotations(wid, 'v1', anchors, 'operator', base_sha256=sha(image),
                                            plan_sha256=work['plan_sha256'])
    anchors[1]['additional_targets'] = [{'target': [1020, 730], 'target_region': [1013, 722, 1028, 737]}]
    misplaced = copy.deepcopy(anchors)
    misplaced[2].update(target=[1015, 728], target_region=[1010, 722, 1024, 736])
    with pytest.raises(ValueError, match='beam.*anchor'):
        workspace.approve_image_annotations(wid, 'v1', misplaced, 'operator', base_sha256=sha(image),
                                            plan_sha256=work['plan_sha256'])
    approval = workspace.approve_image_annotations(wid, 'v1', anchors, 'operator', base_sha256=sha(image),
                                                   plan_sha256=work['plan_sha256'])
    assert approval['base_sha256'] == sha(image)
    completed = workspace.package(wid)
    archive = zipfile.ZipFile(io.BytesIO(completed['zip']))
    stored = workspace.read(wid)['current']['records']['v1']
    display = archive.read('figures/v1.png')
    assert outside_regions_unchanged(image, display, stored['regions'])
    with Image.open(io.BytesIO(image)) as base, Image.open(io.BytesIO(display)) as marked:
        difference = ImageChops.difference(base.convert('L'), marked.convert('L'))
        left, top, _, bottom = stored['regions'][1]['text_box']
        for target in (anchors[1]['target'], anchors[1]['additional_targets'][0]['target']):
            x, y = target
            runs = []
            for ratio in [i / 100 for i in range(10, 91)]:
                sample_x = round(left - 5 + (x - left + 5) * ratio)
                sample_y = round((top + bottom)/2 + (y - (top + bottom)/2) * ratio)
                visible = difference.getpixel((sample_x, sample_y)) > 100
                if not runs or runs[-1] != visible:
                    runs.append(visible)
            assert runs.count(False) >= 5 and runs.count(True) >= 5
    assert workspace.read_archived_image(wid, 'v1', sha(image)) == image

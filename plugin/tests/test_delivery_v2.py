import importlib
import io
import json
import zipfile
import pytest
import pypdfium2 as pdfium
from tests.test_images import picture


def delivery():
    assert importlib.util.find_spec('patent_drawing.delivery_v2'), 'CN delivery module missing'
    return importlib.import_module('patent_drawing.delivery_v2')


def test_cn_draft_pdf_content_respects_a4_margins_and_caption_below_figure():
    result = delivery().package_drawings({'title': '测试', 'views': [{'id': 'fig1', 'title': '图1 整体图'}]},
        [{'png': picture(size=(1536, 1024)), 'id': 'fig1', 'record': {'quality': 'UNKNOWN'}}])
    pdf = pdfium.PdfDocument(result['pdf']);page = pdf[0]
    images = sorted((o.get_bounds() for o in page.get_objects() if o.type == 3), key=lambda b:(b[2]-b[0])*(b[3]-b[1]), reverse=True)
    image, caption = images[:2]
    assert abs(page.get_size()[0] - 595.276) < .1
    for left, bottom, right, top in images:
        assert left >= 70.86 - .1 and right <= 552.76 + .1
        assert bottom >= 42.52 - .1 and top <= 771.03 + .1
    assert caption[3] < image[1], 'figure number must be below the figure'
    assert image[1] - caption[3] < 15, 'figure number should remain adjacent to its own drawing'
    page.close();pdf.close()
    with zipfile.ZipFile(io.BytesIO(result['zip'])) as z:
        manifest = json.loads(z.read('manifest.json'))
        assert manifest['professional_acceptance'] == 'not_reviewed'
        assert z.read('figure-descriptions.txt').decode().count('图1') == 1
        assert 'quality.json' in z.namelist() and 'SHA256SUMS.json' in z.namelist()


def test_missing_or_duplicated_figures_cannot_be_packaged():
    with pytest.raises(ValueError):
        delivery().package_drawings({'views': [{'id': 'f1'}, {'id': 'f2'}]}, [{'id': 'f1', 'png': picture()}])


def test_unrendered_designation_is_not_presented_as_visible_in_captions():
    plan = {'parts': [{'id': 'p', 'name': '测试部件', 'label': '103'}],
            'views': [{'id': 'f', 'title': '框图', 'engine': 'vector', 'parts': ['p']}]}
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><text>1</text><text>03</text></svg>'
    result = delivery().package_drawings(plan, [{'id': 'f', 'png': picture(), 'svg': svg,
                                                'record': {'engine': 'vector'}}])
    with zipfile.ZipFile(io.BytesIO(result['zip'])) as archive:
        captions = archive.read('figure-descriptions.txt').decode()
        assert '图面编号待核' in captions
        assert '涉及部件：103' not in captions
        assert json.loads(archive.read('manifest.json'))['figures'][0]['part_mark_status'] == 'pending'


def test_contiguous_three_digit_mark_appears_in_svg_node():
    from patent_drawing.diagrams import _lines

    class FixedWidth:
        def getlength(self, text):
            return len(text)

    assert _lines('装置103', FixedWidth(), 3) == ['装置', '103']


def test_black_text_hidden_under_black_ink_does_not_count_as_visible():
    from patent_drawing.diagrams import render_diagram
    from patent_drawing.marking import visible_vector_marks

    part = {'id': 'p', 'label': '103', 'name': '测试部件'}
    figure = {'parts': ['p'], 'diagram': {'width': 650, 'height': 650, 'primitives': [
        {'type': 'rect', 'box': [50, 50, 500, 500], 'fill': 'black'},
        {'type': 'text', 'at': [100, 100], 'text': '{{p.label}}'},
    ]}}
    output = render_diagram({'parts': [part]}, figure, layered=True)
    assert visible_vector_marks(figure, {'p': part}, output['svg']) == {}


def test_numbered_vector_plan_binds_an_unambiguous_existing_node_without_changing_geometry():
    from patent_drawing.marking import bind_vector_numbers

    parts = [{'id': 'supply', 'name': '电源', 'label': '103'}]
    view = {'id': 'f', 'engine': 'vector', 'parts': ['supply'],
            'diagram': {'nodes': [{'id': 'source', 'text': '电源', 'box': [1, 2, 3, 4]}]}}
    plan = {'part_mark_mode': 'numbered', 'parts': parts, 'views': [view]}
    assert bind_vector_numbers(plan) == [{'view_id': 'f', 'part_id': 'supply', 'target': 'source'}]
    assert view['diagram']['nodes'][0]['text'] == '电源 {{supply.label}}'
    assert view['diagram']['nodes'][0]['box'] == [1, 2, 3, 4]
    assert bind_vector_numbers(plan) == []


def test_pdf_does_not_include_drafting_comments_as_drawing_caption():
    r = delivery().package_drawings({'title': '甲', 'views': [{'id': 'f', 'title': '整体图', 'purpose': '待确认备注应在报告'}]}, [{'id': 'f', 'png': picture(), 'record': {}}])
    assert r['page_captions'] == ['图1']


def test_review_status_is_visible_on_the_pdf_and_on_each_extracted_page():
    plan = {'views': [{'id': 'main'}, {'id': 'cut'}]}
    figures = [{'id': fid, 'png': picture()} for fid in ('main', 'cut')]
    scope = {'status': 'incomplete_draft', 'required_ids': ['main', 'cut', 'detail'],
             'figure_numbers': {'main': 1, 'cut': 2, 'detail': 3}, 'missing_ids': ['detail']}
    initial = delivery().package_drawings(plan, figures, {'technical_status': 'UNKNOWN'}, delivery_scope=scope)
    reviewed = delivery().package_drawings(plan, figures, {
        'technical_status': 'FAIL', 'failed_feature_ids': ['cut/centerline'],
    }, delivery_scope=scope)

    assert reviewed['pdf'] != initial['pdf']
    pdf = pdfium.PdfDocument(reviewed['pdf'])
    for index, page in enumerate(pdf):
        bitmap = page.render(scale=2)
        pixels = bitmap.to_pil().convert('L')
        if index == 0:
            assert pixels.crop((140, 140, 1104, 215)).getextrema()[0] < 128
        assert pixels.crop((140, 1518, 1104, 1564)).getextrema()[0] < 128
    with zipfile.ZipFile(io.BytesIO(reviewed['zip'])) as z:
        quality = json.loads(z.read('quality.json'))
        assert quality['failed_ids'] == ['cut']
        assert quality['missing_ids'] == ['detail']
        assert '图2' in z.read('delivery-notice.txt').decode()
        assert '图3' in z.read('delivery-notice.txt').decode()


def test_default_drawing_zip_excludes_private_evidence_and_metadata():
    plan = {
        'revision': 3, 'title': '总图', 'facts': [{'quote': 'private-source-marker'}],
        'parts': [{'id': 'bracket', 'label': '10', 'name': '支架', 'source': 'private-source-marker'}],
        'views': [{'id': 'main', 'title': '总图', 'parts': ['bracket'], 'source_ref': 'private-source-marker'}],
    }
    png = picture()
    figures = [{'id': 'main', 'png': png, 'base_png': png,
                'record': {'engine': 'source', 'base': {'name': 'private-source-marker'}}}]
    raw = {'evidence/original-source-1.pdf': b'private-original',
           'evidence/planner-1-request.bin': b'private-request'}
    result = delivery().package_drawings(plan, figures, {
        'technical_status': 'UNKNOWN', 'findings': [{'message': 'private-source-marker'}],
    }, raw)

    with zipfile.ZipFile(io.BytesIO(result['zip'])) as z:
        assert set(z.namelist()) == {
            'drawings.pdf', 'figure-descriptions.txt', 'figures/main.png', 'manifest.json',
            'plan.json', 'quality.json', 'SHA256SUMS.json',
        }
        assert all(b'private-source-marker' not in z.read(n) for n in z.namelist() if n.endswith('.json'))
        assert json.loads(z.read('plan.json'))['parts'] == [{'id': 'bracket', 'label': '10', 'name': '支架'}]
        assert z.read('figures/main.png') == png


def test_engineering_reference_export_uses_its_workspace_source_bundle(tmp_path, monkeypatch):
    from patent_drawing.operation_store import OperationStore
    from patent_drawing.workspace import Workspace

    workspace = Workspace(OperationStore(tmp_path / 'state'), 'test-owner')
    source = '四处支承共同支承同一上板。'.encode()
    wid = workspace.ingest([{'name': 'source.txt', 'blob': source}])['workspace_id']
    state = workspace.read(wid)
    png = picture()
    plan = {
        'title': '支承关系', 'revision': 1, 'parts': [],
        'views': [{'id': 'guide', 'requested_type': 'relationship', 'parts': []}],
    }
    receipt = {
        'view_id': 'guide', 'guide_id': 'reviewed-guide',
        'source_sha256': state['source_sha256'], 'spatial_geometry_status': 'UNKNOWN',
        'artifacts': {'png': workspace.store.put_artifact(workspace.owner, png)},
    }
    monkeypatch.setattr(workspace, '_engineering_reference_context',
                        lambda *args, **kwargs: (plan, receipt, {'technical_status': 'UNKNOWN'}, []))

    result = workspace.package_engineering_reference(wid)

    assert result['model_calls'] == 0 and result['reference_revision'] == 1
    with zipfile.ZipFile(io.BytesIO(result['zip'])) as pack:
        manifest = json.loads(pack.read('manifest.json'))
        assert manifest['reference_only_ids'] == ['guide'] and not manifest['filing_ready']
        assert pack.read('figures/guide.png') == png
        assert not any(name.startswith('evidence/') for name in pack.namelist())


def test_restricted_audit_export_retains_clean_figure_and_exact_source_bytes(tmp_path):
    from patent_drawing.operation_store import OperationStore
    from patent_drawing.workspace import Workspace

    workspace = Workspace(OperationStore(tmp_path / 'state'), 'test-owner')
    raw = '电路包含电源。'.encode()
    wid = workspace.ingest([{'name': 'source.txt', 'blob': raw}])['workspace_id']
    plan = {
        'schema_version': 'drawing-set-2', 'set_id': 'test', 'revision': 1,
        'source_sha256': workspace.read(wid)['source_sha256'], 'title': '供电原理', 'patent_type': 'invention',
        'technical_types': ['electrical'], 'parts': [], 'facts': [],
        'views': [{'id': 'overview', 'title': '总图', 'purpose': '表达电源',
                   'engine': 'vector', 'requested_type': 'block', 'delivered_type': 'block', 'parts': [],
                   'diagram': {'width': 1000, 'height': 800, 'nodes': [
                       {'id': 'supply', 'type': 'process', 'box': [200, 200, 500, 400], 'text': '电源'}], 'edges': []}}],
    }
    workspace.stage(wid, plan)
    workspace.render(wid, 'overview')
    public = workspace.package(wid)
    before = workspace.read(wid)
    restricted = workspace.package(wid, export_current=True, audit=True)
    assert workspace.read(wid) == before

    with zipfile.ZipFile(io.BytesIO(public['zip'])) as drawings, zipfile.ZipFile(io.BytesIO(restricted['zip'])) as audit:
        assert 'evidence/original-source-1.txt' not in drawings.namelist()
        assert audit.read('evidence/original-source-1.txt') == raw
        assert audit.read('figures/overview.png') == drawings.read('figures/overview.png')
        assert audit.read('bases/overview.png').startswith(b'\x89PNG')
        review = json.loads(audit.read('evidence/actual-review-bundle.json'))
        assert review['missing_actual_files'] == []


def test_manifest_uses_only_review_bound_to_exported_image(tmp_path):
    import base64
    from datetime import datetime, timezone

    import httpx

    from patent_drawing.client import OpenAIImagesClient
    from patent_drawing.operation_store import OperationStore, digest
    from patent_drawing.workspace import Workspace

    def respond(request):
        color = 'black' if '左侧' in request.content.decode() else 'white'
        data = base64.b64encode(picture(color=color)).decode()
        return httpx.Response(200, json={'data': [{'b64_json': data}]})

    workspace = Workspace(OperationStore(tmp_path / 'state'), 'test-owner', allow_models=True,
                          image_client=OpenAIImagesClient('fixture-key', transport=httpx.MockTransport(respond)))
    wid = workspace.ingest([{'name': 'source.txt', 'blob': '电源安装在支架上。'.encode()}],
                           technical_types=['mechanical'])['workspace_id']
    plan = {'schema_version': 'drawing-set-2', 'set_id': 'review', 'revision': 1,
            'source_sha256': workspace.read(wid)['source_sha256'], 'title': '支架',
            'patent_type': 'invention', 'technical_types': ['mechanical'], 'parts': [], 'facts': [],
            'views': [{'id': 'main', 'title': '支架示意', 'purpose': '表达电源支承',
                       'engine': 'image', 'requested_type': 'relationship', 'delivered_type': 'relationship',
                       'parts': [], 'instruction': '绘制黑白支架示意。'}]}

    def manifest(package):
        with zipfile.ZipFile(io.BytesIO(package['zip'])) as archive:
            return json.loads(archive.read('manifest.json'))

    def read_status(package):
        return manifest(package)['figures'][0]['technical_review']

    workspace.stage(wid, plan, image_budget=1)
    record = workspace.render(wid, 'main')['record']
    assert read_status(workspace.package_partial(wid)) == 'not_reviewed'
    contract = workspace.read(wid)['work']['plan']['visibility_contract']['main']
    review = {'view_id': 'main', 'base_sha256': record['base']['sha256'],
              'spec_sha256': record['base_spec_sha256'], 'contract_sha256': digest(contract),
              'reviewed_at': datetime.now(timezone.utc).isoformat(), 'reviewer': 'fixture-reviewer',
              'features': [{'feature_id': f['feature_id'], 'verdict': 'FAIL',
                            'note': 'fixture observation'} for f in contract['features']]}
    workspace.review_visibility(wid, 'main', review)
    assert read_status(workspace.package_partial(wid)) == 'BLOCKED'
    for feature in review['features']:
        feature['verdict'] = 'UNKNOWN'
    workspace.review_visibility(wid, 'main', review)
    assert read_status(workspace.package_partial(wid)) == 'BLOCKED'
    for feature in review['features']:
        feature.update(verdict='PASS', region=[1, 1, 20, 30])
    workspace.review_visibility(wid, 'main', review)
    assert read_status(workspace.package_partial(wid)) == 'PASS'
    completed = workspace.package(wid)
    assert read_status(completed) == 'PASS'
    assert manifest(completed)['technical_status'] == 'UNKNOWN'
    assert manifest(completed)['professional_acceptance'] == 'not_reviewed'
    with zipfile.ZipFile(io.BytesIO(completed['zip'])) as archive:
        assert b'fixture-reviewer' not in archive.read('manifest.json')
    assert workspace.read(wid)['current']['records']['main']['technical_review'] == 'not_reviewed'

    plan['revision'] = 2
    plan['views'][0]['instruction'] = '从左侧绘制黑白支架示意。'
    workspace.stage(wid, plan, image_budget=1)
    changed = workspace.render(wid, 'main')['record']
    assert changed['base']['sha256'] != record['base']['sha256']
    assert read_status(workspace.package_partial(wid)) == 'not_reviewed'
    assert workspace.package(wid, export_current=True, revision=1)['zip'] == completed['zip']


def test_two_visible_vector_labels_change_in_png_and_svg_while_other_views_stay_unchanged(tmp_path):
    import copy
    import xml.etree.ElementTree as ET
    from PIL import Image, ImageChops

    from patent_drawing.operation_store import OperationStore
    from patent_drawing.workspace import Workspace

    workspace = Workspace(OperationStore(tmp_path / 'state'), 'test-owner')
    wid = workspace.ingest([{'name': 'source.txt', 'blob': '供电模块及接线。'.encode()}])['workspace_id']
    part = {'id': 'supply', 'label': '10', 'name': '电源'}
    view = {'id': 'main', 'title': '电源总图', 'purpose': '表示电源10', 'engine': 'vector',
            'requested_type': 'structure', 'delivered_type': 'structure', 'parts': ['supply'],
            'diagram': {'width': 1000, 'height': 800, 'nodes': [], 'edges': [], 'primitives': [
                {'type': 'rect', 'id': 'shape', 'part': 'supply', 'box': [100, 250, 600, 550]},
                {'type': 'label', 'part': 'supply', 'at': [750, 100], 'target': [500, 250], 'target_shape': 'shape'}]}}
    other = copy.deepcopy(view)
    other['id'] = 'detail'
    register_only = copy.deepcopy(view)
    register_only['id'] = 'register'
    register_only['diagram']['primitives'].pop()
    plan = {'schema_version': 'drawing-set-2', 'set_id': 'test', 'revision': 1,
            'source_sha256': workspace.read(wid)['source_sha256'], 'title': '供电图',
            'patent_type': 'invention', 'technical_types': ['electrical'], 'parts': [part], 'facts': [],
            'views': [view, other, register_only]}
    for fid in workspace.stage(wid, plan)['queue']:
        workspace.render(wid, fid)
    old = workspace.package(wid)
    plan['revision'] = 2
    part['label'] = '110'
    preview = workspace.stage(wid, plan, require_linked_confirmation=True)
    assert preview['linked_edit']['visible_edit_view_ids'] == ['main', 'detail']
    assert preview['linked_edit']['metadata_only_view_ids'] == ['register']
    assert workspace.preflight(wid)['blocking_state'] == 'awaiting_linked_confirmation'
    with pytest.raises(ValueError, match='confirm'):
        workspace.render(wid, 'main')
    review = workspace.linked_edit_preview(wid)
    assert review['ready'] and review['target_revision'] == 2
    assert [entry['figure_number'] for entry in review['views']] == [1, 2, 3]
    for entry in review['views']:
        assert entry['text_updates'] == [{'field': 'purpose', 'before': '表示电源10', 'after': '表示电源110'}]
        assert entry['registration_update'] == {'before': '10—电源', 'after': '110—电源'}
    assert review['views'][2]['status'] == 'registration_only'
    assert review['views'][2]['allowed_boxes'] == []
    assert not workspace.confirm_linked_edit(wid)['confirmed']
    assert workspace.confirm_linked_edit(wid, review['confirmation_code'])['confirmed']
    for fid in workspace.resume(wid)['queue']:
        workspace.render(wid, fid)
    new = workspace.package(wid)
    with zipfile.ZipFile(io.BytesIO(old['zip'])) as before, zipfile.ZipFile(io.BytesIO(new['zip'])) as after:
        for entry in review['views'][:2]:
            fid = entry['view_id']
            old_svg, svg = [ET.fromstring(archive.read(f'figures/{fid}.svg')) for archive in (before, after)]
            assert [e.text for e in old_svg.iter() if e.tag.endswith('}text')] == ['10']
            assert [e.text for e in svg.iter() if e.tag.endswith('}text')] == ['110']
            assert before.read(f'figures/{fid}.png') != after.read(f'figures/{fid}.png')
            old_image, new_image = [Image.open(io.BytesIO(archive.read(f'figures/{fid}.png'))).convert('RGB') for archive in (before, after)]
            assert entry['allowed_boxes'] == [list(ImageChops.difference(old_image, new_image).getbbox())]
            assert entry['positions'] == [{'at': [750, 100], 'target': [500, 250]}]
        for suffix in ('png', 'svg'):
            assert before.read(f'figures/register.{suffix}') == after.read(f'figures/register.{suffix}')
    status = workspace.read(wid)['current']['transaction']
    assert status['visible_edit_view_ids'] == ['main', 'detail']
    assert status['metadata_only_view_ids'] == ['register']

    old_again = workspace.package(wid, export_current=True, revision=1)
    assert old_again['pdf'] == old['pdf']
    assert old_again['zip'] == old['zip']
    assert workspace.read(wid)['current']['plan']['revision'] == 2


def test_source_roi_reapproval_requires_new_confirmation_and_replaces_stale_output(tmp_path):
    from PIL import Image, ImageDraw

    from patent_drawing.operation_store import OperationStore
    from patent_drawing.workspace import Workspace

    source = Image.new('RGB', (700, 800), 'white')
    draw = ImageDraw.Draw(source)
    draw.text((550, 20), '10', fill='black')
    draw.line((500, 50, 650, 50), fill='black', width=2)
    draw.rectangle((100, 150, 600, 650), outline='black')
    original = io.BytesIO()
    source.save(original, format='PNG')
    w = Workspace(OperationStore(tmp_path / 'state'), 'test-owner')
    wid = w.ingest([{'name': 'source.png', 'blob': original.getvalue()}])['workspace_id']
    state = w.read(wid)
    part = {'id': 'plate', 'name': '托板', 'label': '10'}
    view = {'id': 'original', 'title': '托板10', 'engine': 'source', 'parts': ['plate'],
            'requested_type': 'structure', 'delivered_type': 'structure',
            'source_region_id': state['bundle']['figure_candidates'][0]['id']}
    plan = {'schema_version': 'drawing-set-2', 'set_id': 'test', 'revision': 1,
            'source_sha256': state['source_sha256'], 'title': '托板图', 'patent_type': 'invention',
            'technical_types': ['mechanical'], 'parts': [part], 'facts': [], 'views': [view]}
    w.stage(wid, plan)
    w.render(wid, 'original')
    old = w.package(wid)
    plan['revision'] = 2
    part['label'] = '110'
    w.stage(wid, plan, require_linked_confirmation=True)
    assert not w.linked_edit_preview(wid)['ready']
    assert not w.confirm_linked_edit(wid)['confirmed']

    first_roi = [540, 10, 590, 45]
    w.approve_source_label(wid, 'original', 'plate', '110', first_roi, [545, 18], 18, 'first review')
    confirmation = w.confirm_linked_edit(wid)
    assert not confirmation['confirmed']
    assert confirmation['linked_edit']['views'][0]['allowed_boxes'] == [first_roi]
    old_code = confirmation['linked_edit']['confirmation_code']
    assert not w.confirm_linked_edit(wid)['confirmed']
    assert w.confirm_linked_edit(wid, old_code)['confirmed']
    initial = w.render(wid, 'original')['record']

    second_roi = [539, 10, 593, 45]
    w.approve_source_label(wid, 'original', 'plate', '110', second_roi, [546, 18], 18, 'revised review')
    assert w.preflight(wid)['blocking_state'] == 'awaiting_linked_confirmation'
    with pytest.raises(ValueError, match='confirm'):
        w.package(wid)
    assert w.package(wid, export_current=True)['zip'] == old['zip']
    updated_scope = w.linked_edit_preview(wid)
    assert updated_scope['confirmation_code'] != old_code
    assert not w.confirm_linked_edit(wid, old_code)['confirmed']
    assert not w.confirm_linked_edit(wid)['confirmed']
    assert w.confirm_linked_edit(wid, updated_scope['confirmation_code'])['confirmed']
    assert w.resume(wid)['queue'] == ['original']
    updated = w.render(wid, 'original')['record']
    assert updated['source_label_migration']['roi'] == second_roi
    assert updated['base'] == initial['base'] and updated['png'] != initial['png']
    w.package(wid)
    assert w.read(wid)['current']['plan']['revision'] == 2

def test_inline_vector_references_render_and_follow_atomic_renumbering(tmp_path):
    import copy
    import xml.etree.ElementTree as ET

    from patent_drawing.operation_store import OperationStore
    from patent_drawing.workspace import Workspace

    workspace = Workspace(OperationStore(tmp_path / 'state'), 'test-owner')
    wid = workspace.ingest([{'name': 'source.txt', 'blob': '电路的电源和控制器相连。'.encode()}])['workspace_id']
    supply = {'id': 'supply', 'label': '10', 'name': '电源'}
    controller = {'id': 'controller', 'label': '20', 'name': '控制器'}
    primitives = [
        {'type': 'rect', 'id': 'power', 'part': 'supply', 'box': [100, 250, 380, 450]},
        {'type': 'rect', 'id': 'control', 'part': 'controller', 'box': [500, 250, 780, 450]},
        {'type': 'label', 'part': 'supply', 'at': [450, 170], 'target': [220, 250], 'target_shape': 'power'},
        {'type': 'label', 'part': 'controller', 'at': [830, 170], 'target': [650, 250], 'target_shape': 'control'},
        {'type': 'text', 'text': '连接关系 {{supply.label}}、{{controller.label}}', 'at': [160, 535]},
    ]
    view = {'id': 'main', 'title': '电路总览图', 'purpose': '表示电源和控制器连接',
            'engine': 'vector', 'requested_type': 'structure', 'delivered_type': 'structure',
            'parts': ['supply', 'controller'], 'diagram': {'width': 1000, 'height': 800,
            'nodes': [], 'edges': [], 'primitives': primitives}}
    view['annotations'] = [{'part_id': 'supply', 'at': [450, 630], 'target': [210, 450],
                            'target_region': [100, 250, 390, 450], 'anchor_status': 'reviewed'}]
    detail = {**copy.deepcopy(view), 'id': 'detail', 'title': '电路局部图'}
    plan = {'schema_version': 'drawing-set-2', 'set_id': 'inline', 'revision': 1,
            'source_sha256': workspace.read(wid)['source_sha256'], 'title': '电路附图',
            'patent_type': 'invention', 'technical_types': ['electrical'],
            'parts': [supply, controller], 'facts': [], 'views': [view, detail]}

    for fid in workspace.stage(wid, plan)['queue']:
        workspace.render(wid, fid)
    initial = workspace.package(wid)
    plan['revision'] = 2
    supply['label'] = '110'
    renamed = copy.deepcopy(plan)
    renamed['parts'][1]['name'] = '其他控制模块'
    with pytest.raises(ValueError, match='separate part registration changes'):
        workspace.stage(wid, renamed, require_linked_confirmation=True)
    preview = workspace.stage(wid, plan, require_linked_confirmation=True)
    assert preview['linked_edit']['visible_edit_view_ids'] == ['main', 'detail']
    review = workspace.linked_edit_preview(wid)
    assert review['ready']
    assert workspace.confirm_linked_edit(wid, review['confirmation_code'])['confirmed']
    for fid in workspace.resume(wid)['queue']:
        record = workspace.render(wid, fid)['record']
        assert record['annotation_outside_unchanged'] is True
    revised = workspace.package(wid)
    with zipfile.ZipFile(io.BytesIO(initial['zip'])) as old, zipfile.ZipFile(io.BytesIO(revised['zip'])) as new:
        for fid in ('main', 'detail'):
            path = f'figures/{fid}.svg'
            before, after = (ET.fromstring(archive.read(path)) for archive in (old, new))
            assert [e.text for e in before.iter() if e.tag.endswith('}text')] == ['连接关系 10、20', '10', '10', '20']
            assert [e.text for e in after.iter() if e.tag.endswith('}text')] == ['连接关系 110、20', '110', '110', '20']
    assert workspace.package(wid, export_current=True, revision=1)['zip'] == initial['zip']


def test_closed_outline_keeps_the_leader_endpoint_inside_its_concave_component():
    from patent_drawing.diagrams import render_diagram

    points = [[150, 350], [450, 350], [450, 200], [550, 200],
              [550, 450], [150, 450], [150, 350]]
    shape = {'type': 'polyline', 'id': 'outline', 'part': 'component', 'points': points}
    label = {'type': 'label', 'part': 'component', 'at': [700, 390],
             'target': [500, 400], 'target_shape': 'outline'}
    part = {'id': 'component', 'label': '4', 'name': '部件'}
    view = {'parts': [part['id']], 'diagram': {'width': 900, 'height': 700,
            'primitives': [shape, label]}}
    output = render_diagram({'parts': [part]}, view)
    assert output['anchors'][0]['target'] == [500, 400]
    assert output['anchors'][0]['target_shape'] == 'outline'
    assert b'150,350 450,350 450,200 550,200 550,450 150,450 150,350' in output['svg']


@pytest.mark.parametrize('variant', ['open', 'arrow', 'outside', 'crossed', 'other_part'])
def test_closed_outline_does_not_relax_ambiguous_or_unrelated_leader_targets(variant):
    from patent_drawing.diagrams import validate_diagram
    from patent_drawing.errors import DrawingError

    points = [[150, 350], [450, 350], [450, 200], [550, 200],
              [550, 450], [150, 450], [150, 350]]
    shape = {'type': 'polyline', 'id': 'outline', 'part': 'component', 'points': points}
    label = {'type': 'label', 'part': 'component', 'at': [700, 390],
             'target': [500, 400], 'target_shape': 'outline'}
    if variant == 'open':
        points.pop()
    elif variant == 'arrow':
        shape['arrow'] = True
    elif variant == 'outside':
        label['target'] = [390, 280]
    elif variant == 'crossed':
        shape['points'] = [[100, 100], [400, 400], [100, 400], [400, 100], [100, 100]]
        label['target'] = [250, 150]
    else:
        label['part'] = 'other'
    parts = [{'id': 'component', 'label': '4', 'name': '部件'},
             {'id': 'other', 'label': '5', 'name': '另一部件'}]
    view = {'parts': [p['id'] for p in parts], 'diagram': {'width': 900, 'height': 700,
            'primitives': [shape, label]}}
    with pytest.raises(DrawingError, match='引出线未指向指定部件图形'):
        validate_diagram({'parts': parts}, view)


def test_derived_image_binds_parent_review_and_stops_when_that_receipt_changes(tmp_path):
    import base64
    from datetime import datetime, timezone

    import httpx

    from patent_drawing.client import OpenAIImagesClient
    from patent_drawing.operation_store import OperationStore, digest
    from patent_drawing.workspace import Workspace

    paths = []
    def respond(request):
        paths.append(request.url.path)
        png = picture(color='white' if len(paths) == 1 else 'black')
        return httpx.Response(200, json={'data': [{'b64_json': base64.b64encode(png).decode()}]})

    store = OperationStore(tmp_path / 'state')
    workspace = Workspace(store, 'alice', allow_models=True,
                          image_client=OpenAIImagesClient('test-key', transport=httpx.MockTransport(respond)))
    wid = workspace.ingest([{'name': 'source.txt', 'blob': '电源安装在支架上。'.encode()}],
                           technical_types=['mechanical'])['workspace_id']
    parent = {'id': 'main', 'title': '总图', 'purpose': '表达支架和电源关系',
              'engine': 'image', 'requested_type': 'spatial', 'delivered_type': 'spatial',
              'parts': ['bracket'], 'instruction': '黑白示意电源支架，不增加孔位。'}
    child = {**parent, 'id': 'rear', 'title': '另一观察方向', 'depends_on': ['main']}
    plan = {'schema_version': 'drawing-set-2', 'set_id': 'test', 'revision': 1,
            'source_sha256': workspace.read(wid)['source_sha256'], 'title': '支架',
            'patent_type': 'invention', 'technical_types': ['mechanical'],
            'parts': [{'id': 'bracket', 'label': '10', 'name': '支架'}], 'facts': [],
            'views': [parent, child]}
    workspace.stage(wid, plan, image_budget=2)
    main = workspace.render(wid, 'main')['record']
    contract = workspace.read(wid)['work']['plan']['visibility_contract']['main']
    review = {'view_id': 'main', 'base_sha256': main['base']['sha256'],
              'spec_sha256': main['base_spec_sha256'], 'contract_sha256': digest(contract),
              'reviewed_at': datetime.now(timezone.utc).isoformat(), 'reviewer': 'synthetic-tester',
              'features': [{'feature_id': f['feature_id'], 'verdict': 'PASS', 'region': [1, 1, 20, 30],
                            'note': 'synthetic fixture for dependency state'} for f in contract['features']]}
    workspace.review_visibility(wid, 'main', review)
    rear = workspace.render(wid, 'rear')['record']

    binding = rear['reference_parent_binding']
    assert binding['parent_view_id'] == 'main' and binding['parent_revision'] == 1
    assert binding['parent_base_sha256'] == main['base']['sha256']
    assert binding['parent_review_sha256'] == digest(review)
    assert paths == ['/v1/images/generations', '/v1/images/edits']
    workspace.review_visibility(wid, 'main', {**review, 'reviewer': 'second-synthetic-reviewer'})
    state = workspace.preflight(wid)
    assert state['blocking_state'] == 'awaiting_visual_review' and state['blocked_view'] == 'rear'
    assert state['stale_parent_review'] is True
    assert len(paths) == 2


def test_historical_source_mismatch_stops_before_image_dispatch(tmp_path):
    import sqlite3

    import httpx

    from patent_drawing.client import OpenAIImagesClient
    from patent_drawing.errors import ImageDispatchNotStarted
    from patent_drawing.operation_store import OperationStore
    from patent_drawing.operator_policy import AuditedTransport, OperatorPolicy
    from patent_drawing.workspace import Workspace

    def unexpected_request(_):
        pytest.fail('a known source in a different lineage must not reach the provider')

    store = OperationStore(tmp_path / 'workspace')
    workspace = Workspace(store, 'other-tenant:account:conversation', allow_models=True)
    wid = workspace.ingest([{'name': 'source.txt', 'blob': '电源安装在支架上。'.encode()}],
                           technical_types=['mechanical'])['workspace_id']
    source = workspace.read(wid)['source_sha256']
    transport = AuditedTransport(OperatorPolicy(
        'test-isolated', True, tmp_path / 'ledger', {'image': None, 'text': None}, 'a' * 64,
        blocked_lineages=({'source_sha256': source, 'lineage_sha256': 'b' * 64},),
        continuity={'ledger_id': 'c' * 64, 'mode': 'initialize',
                    'registry_path': str(tmp_path / 'registry.sqlite3')},
    ), inner=httpx.MockTransport(unexpected_request))
    workspace.image_client = OpenAIImagesClient('test-key', transport=transport)
    workspace.stage(wid, {
        'schema_version': 'drawing-set-2', 'set_id': 'test', 'revision': 1,
        'source_sha256': source, 'title': '支架', 'patent_type': 'invention',
        'technical_types': ['mechanical'], 'parts': [], 'facts': [],
        'views': [{'id': 'main', 'title': '总图', 'purpose': '表达支架和电源关系',
                   'engine': 'image', 'requested_type': 'spatial', 'delivered_type': 'spatial',
                   'parts': [], 'instruction': '黑白示意电源支架，不增加孔位。'}],
    }, image_budget=1)
    gate = workspace.preflight(wid)
    assert gate['blocking_state'] == 'source_lineage_mismatch'
    assert gate['supplier_state'] == 'not_called'
    assert gate['resume_action'] == 'reconcile_source_lineage'
    assert gate['route'] == 'reply'

    with pytest.raises(ImageDispatchNotStarted):
        workspace.render(wid, 'main')
    assert workspace.image_dispatch_count(wid) == 0
    assert workspace.read(wid)['current'] is None
    assert workspace.resume(wid)['queue'] == ['main']
    with sqlite3.connect(transport.db) as ledger:
        assert ledger.execute('SELECT count(*) FROM calls').fetchone() == (0,)


def test_name_only_flow_can_export_visible_formula_and_following_chinese_node(tmp_path):
    from patent_drawing.linked_edits import audit_active_references
    from patent_drawing.operation_store import OperationStore
    from patent_drawing.workspace import Workspace

    workspace = Workspace(OperationStore(tmp_path / 'state'), 'flow-owner')
    wid = workspace.ingest(
        [{'name': 'source.txt', 'blob': '按显式组大小确定点数，再恢复组边界。'.encode()}],
        technical_types=['computer_method'],
    )['workspace_id']
    formula = '组内点数 N = ptn_group_size_minus1 + 1'
    boundary = '据组大小恢复组边界'
    view = {'id': 'decode', 'title': '分组译码流程', 'purpose': '恢复当前组边界',
            'engine': 'vector', 'requested_type': 'flowchart', 'delivered_type': 'flowchart',
            'parts': ['size', 'boundary'], 'diagram': {'width': 1000, 'height': 800,
                'nodes': [
                    {'id': 'count', 'type': 'process', 'box': [110, 100, 890, 220], 'text': '{{size.name}}'},
                    {'id': 'split', 'type': 'process', 'box': [110, 390, 890, 510], 'text': '{{boundary.name}}'},
                ], 'edges': [{'source': 'count', 'target': 'split'}]}}
    plan = {'schema_version': 'drawing-set-2', 'set_id': 'group-decode', 'revision': 1,
            'source_sha256': workspace.read(wid)['source_sha256'], 'title': '组内译码',
            'patent_type': 'invention', 'technical_types': ['computer_method'],
            'part_mark_mode': 'name_only', 'facts': [], 'views': [view],
            'parts': [{'id': 'size', 'name': formula, 'label': ''},
                      {'id': 'boundary', 'name': boundary, 'label': ''}]}
    workspace.stage(wid, plan)
    workspace.render(wid, 'decode')
    packed = workspace.package(wid)
    assert packed['quality']['active_reference_audit']['status'] == 'PASS'
    assert workspace.read(wid)['current']['plan']['revision'] == 1

    audit = audit_active_references(plan, {'decode': {}}, {'decode': formula + '0 ' + boundary})
    assert audit['status'] == 'FAIL'
    assert audit['findings'][0]['part_id'] == 'size'


def test_public_export_retains_review_limit_for_the_relevant_communication_figure():
    note = '仅示意逻辑先后和消息方向。参考信号与测量反馈的具体空口时序待核。'
    private = 'private-source-marker https://internal.example/receipt'
    plan = {
        'technical_types': ['communication'],
        'facts': [
            {'status': 'unknown', 'text': '参考信号与测量反馈的具体时序尚未确定。', 'evidence': [private]},
        ],
        'unresolved': ['需补充参考信号与测量反馈的具体时序。' + private],
        'views': [
            {'id': 'angle', 'title': '面板角度范围', 'purpose': '表达范围内的波束方向。'},
            {'id': 'exchange', 'title': '设备交互', 'purpose': '表达参考信号和测量反馈的传递方向。'},
        ],
    }
    figures = [{'id': view['id'], 'png': picture()} for view in plan['views']]
    result = delivery().package_drawings(plan, figures)

    with zipfile.ZipFile(io.BytesIO(result['zip'])) as z:
        public = json.loads(z.read('plan.json'))
        assert 'limitations' not in public['views'][0]
        assert public['views'][1]['limitations'] == [note]
        descriptions = z.read('figure-descriptions.txt').decode().splitlines()
        assert len(descriptions) == 2
        assert note not in descriptions[0] and note in descriptions[1]
        assert all(private.encode() not in z.read(name) for name in z.namelist())
        assert set(z.namelist()) == {
            'drawings.pdf', 'figure-descriptions.txt', 'figures/angle.png', 'figures/exchange.png',
            'manifest.json', 'plan.json', 'quality.json', 'SHA256SUMS.json',
        }
    assert result['page_captions'] == ['图1', '图2']
    assert result['manifest']['technical_status'] == 'UNKNOWN'


def test_partial_export_does_not_describe_an_undelivered_communication_figure():
    plan = {
        'technical_types': ['communication'],
        'unresolved': ['参考信号与测量反馈时序待核。'],
        'views': [{'id': 'angle', 'title': '面板角度范围', 'purpose': '表达范围内的波束方向。'}],
    }
    scope = {'status': 'incomplete_draft', 'required_ids': ['angle', 'exchange'],
             'figure_numbers': {'angle': 1, 'exchange': 2}, 'missing_ids': ['exchange']}
    result = delivery().package_drawings(plan, [{'id': 'angle', 'png': picture()}], delivery_scope=scope)
    with zipfile.ZipFile(io.BytesIO(result['zip'])) as z:
        assert 'limitations' not in json.loads(z.read('plan.json'))['views'][0]
        assert '空口时序待核' not in z.read('figure-descriptions.txt').decode()


@pytest.mark.parametrize(('status', 'quote', 'binding', 'cited', 'expected'), [
    ('confirmed', '需要保持双方对参考坐标及面板方向的一致理解', True, True, True),
    ('confirmed', '双方应保持对参考坐标系和面板方向的一致理解', True, True, True),
    ('unknown', '需要保持双方对参考坐标及面板方向的一致理解', True, True, False),
    ('confirmed', '需要保持双方对参考坐标及面板方向的一致理解', False, True, False),
    ('confirmed', '需要保持双方对参考坐标及面板方向的一致理解', True, False, False),
    ('confirmed', '不需要保持双方对参考坐标及面板方向的一致理解', True, True, False),
    ('confirmed', '若需要保持双方对参考坐标及面板方向的一致理解，应先确认', True, True, False),
])
@pytest.mark.parametrize('view_type', ['relationship', 'sequence', 'flowchart'])
def test_public_export_preserves_only_confirmed_source_bound_coordinate_alignment(
    status, quote, binding, cited, expected, view_type,
):
    public_note = '双方需对参考坐标及面板方向保持一致理解。'
    private = '私有设计说明 https://internal.example/source'
    quote = '接收端根据所指示范围选束；' + quote + '。' + private
    plan = {
        'technical_types': ['communication'],
        'facts': [{'id': 'selection', 'status': status, 'text': private,
                   'evidence': [{'page_id': 'p1', 'quote': quote}]}],
        'views': [
            {'id': 'angle', 'title': '面板角度范围', 'required_facts': ['selection']},
            {'id': 'exchange', 'title': '设备交互', 'purpose': '表达参考信号及测量反馈。',
             'delivered_type': view_type, 'required_facts': ['selection'] if cited else []},
        ],
    }
    source_bundle = {'pages': [{'id': 'p1', 'text': quote if binding else private}]}
    figures = [{'id': view['id'], 'png': picture()} for view in plan['views']]
    result = delivery().package_drawings(plan, figures, source_bundle=source_bundle)

    with zipfile.ZipFile(io.BytesIO(result['zip'])) as z:
        public = json.loads(z.read('plan.json'))
        descriptions = z.read('figure-descriptions.txt').decode().splitlines()
        assert 'limitations' not in public['views'][0] and public_note not in descriptions[0]
        assert public['views'][1].get('limitations', []) == ([public_note] if expected else [])
        assert (public_note in descriptions[1]) is expected
        assert all(private.encode() not in z.read(name) for name in z.namelist())


def test_engineering_caption_uses_current_mark_and_evidence_scope(tmp_path, monkeypatch):
    import copy

    from patent_drawing.operation_store import OperationStore
    from patent_drawing.workspace import Workspace

    w = Workspace(OperationStore(tmp_path / 'state'), 'test-owner')
    wid = w.ingest([{'name': 'source.txt', 'blob': '支承关系示意'.encode()}])['workspace_id']
    state = w.read(wid)
    png = picture()
    ref = w.store.put_artifact(w.owner, png)
    plan = {'title': '支承关系', 'revision': 1,
            'parts': [{'id': 'top', 'label': '10', 'name': '上板'}],
            'views': [
                {'id': 'main', 'title': '总图', 'engine': 'source', 'parts': ['top'],
                 'purpose': '保留原轮廓及原有标号，作为参照。'},
                {'id': 'section', 'title': '剖面', 'engine': 'source', 'parts': [],
                 'purpose': '剖视标号不变。'},
                {'id': 'guide', 'requested_type': 'relationship', 'parts': ['top']}]}
    receipt = {'view_id': 'guide', 'guide_id': 'reviewed-guide',
               'source_sha256': state['source_sha256'], 'spatial_geometry_status': 'UNKNOWN',
               'artifacts': {'png': ref}}
    figures = [{'id': fid, 'png': png, 'record': {'engine': 'source'}} for fid in ('main', 'section')]
    revised = copy.deepcopy(plan)
    revised['parts'][0]['label'] = '110'
    revised['revision'] = 2
    state['engineering_label_revision'] = {'plan': revised,
        'transaction': {'guide_id': receipt['guide_id'], 'part_id': 'top', 'old_label': '10',
                        'new_label': '110', 'visible_edit_view_ids': ['main', 'guide']},
        'approval': {'id': 'receipt', 'updated_pngs': {'guide': ref, 'main': ref}}}
    w.save(wid, state)
    monkeypatch.setattr(w, '_engineering_reference_context',
        lambda *args, **kwargs: (plan, receipt, {'technical_status': 'UNKNOWN'}, figures[:]))
    before = w.package_engineering_reference(wid, revision=1)
    after = w.package_engineering_reference(wid)
    explicit = w.package_engineering_reference(wid, revision=2)
    assert explicit['zip'] == after['zip']
    restarted = Workspace(OperationStore(tmp_path / 'state'), 'test-owner')
    monkeypatch.setattr(restarted, '_engineering_reference_context',
        lambda *args, **kwargs: (plan, receipt, {'technical_status': 'UNKNOWN'}, figures[:]))
    assert restarted.package_engineering_reference(wid, revision=2)['pdf'] == after['pdf']
    with zipfile.ZipFile(io.BytesIO(before['zip'])) as old, zipfile.ZipFile(io.BytesIO(after['zip'])) as new:
        lines = new.read('figure-descriptions.txt').decode().splitlines()
        assert '110' in lines[0] and '原有标号' not in lines[0]
        assert lines[1] == old.read('figure-descriptions.txt').decode().splitlines()[1]
        assert '110' in lines[2] and '交底已确认' not in lines[2]
        assert new.read('figures/section.png') == old.read('figures/section.png')


def test_local_detail_captions_follow_bound_figures_and_report_missing_opening():
    from patent_drawing.source_contract import detail_caption_updates

    views = [{'id': 'total'}, {'id': 'channels'}, {'id': 'fit'}, {'id': 'opening'}]
    review = {'status': 'PASS', 'bindings': [
        {'parent_view_id': 'total', 'child_view_id': 'fit', 'locator': 'B'},
        {'parent_view_id': 'total', 'child_view_id': 'channels', 'locator': 'A'},
    ]}
    updates = detail_caption_updates(views, review, {'opening'})
    assert 'A 对应图2' in updates['total'] and 'B 对应图3' in updates['total']
    assert '图1 的 A' in updates['channels'] and '图1 的 B' in updates['fit']
    assert '总图位置待核' in updates['opening']
    bound = detail_caption_updates(views, {'status': 'PASS', 'bindings': [
        {'parent_view_id': 'total', 'child_view_id': 'opening', 'locator': 'C'}]}, {'opening'})
    assert '图1 的 C' in bound['opening'] and '待核' not in bound['opening']


def test_linked_vector_renumber_needs_visible_source_and_target_marks():
    from patent_drawing.linked_edits import plan_label_change
    from patent_drawing.operation_store import digest

    plan = {'revision': 1, 'parts': [{'id': 'sender', 'name': '发射设备', 'label': '1'}],
            'views': [{'id': 'sequence', 'engine': 'vector', 'parts': ['sender'],
                       'diagram': {'nodes': [{'text': '发射设备'}]}}]}
    ref = {'sha256': digest('original')}
    records = {'sequence': {'base': ref, 'png': ref}}
    missing = plan_label_change(plan, records, 'sender', '101')
    assert missing['missing_visible_mark_view_ids'] == ['sequence']
    plan['views'][0]['diagram']['nodes'][0]['text'] = '发射设备 {{sender.label}}'
    ready = plan_label_change(plan, records, 'sender', '101')
    assert ready['missing_visible_mark_view_ids'] == []
    assert ready['visible_edit_view_ids'] == ['sequence']

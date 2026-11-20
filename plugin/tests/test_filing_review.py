from copy import deepcopy

import pytest

from patent_drawing import delivery_v2


@pytest.mark.parametrize('change,expected', [
    (None, 'UNKNOWN'), ('missing_scale', 'UNKNOWN'), ('wrong_scale', 'FAIL'),
    ('different_document', 'FAIL'), ('source_conflict', 'FAIL'),
])
def test_filing_requires_four_gates_bound_to_actual_bundle(change, expected):
    from patent_drawing.operation_store import digest

    def sha(label):
        return digest(label)

    plan = {'revision': 2, 'views': [{'id': 'main', 'parts': ['part']}],
            'parts': [{'id': 'part', 'label': '101'}]}
    figures = {'pdf_sha256': sha('pdf'), 'captions_sha256': sha('captions'),
               'views': {'main': {'sha256': sha('png'), 'marks': {'part': '101'}}}}
    specification = {'description_sha256': sha('description'), 'claims_sha256': sha('claims'),
                     'marks': {'main': {'part': '101'}}}
    sources = {'source_sha256': sha('source'), 'features': [
        {'feature_id': 'part', 'artifact_sha256': sha('original'), 'locator': 'p.1',
         'support_scope': '结构', 'reviewer': 'source reviewer', 'verdict': 'PASS'}]}
    receipts = {
        key: {'bundle_sha256': digest([plan, figures, specification, sources]),
              'reviewer': 'reviewer', 'verdict': 'PASS'} for key in ('F1', 'F2', 'F3', 'F4')}
    receipts['F1']['views'] = ['main']
    receipts['F2']['pages'] = [{'view_id': 'main', 'normal_sha256': sha('normal'),
                              'scaled_sha256': sha('scaled'), 'scale': [2, 3], 'dpi': 300,
                              'renderer': 'renderer v1', 'verdict': 'PASS'}]
    if change == 'missing_scale':
        del receipts['F2']
    elif change == 'wrong_scale':
        receipts['F2']['pages'][0]['scale'] = [1, 2]
    elif change == 'different_document':
        specification['description_sha256'] = sha('changed')
    elif change == 'source_conflict':
        sources = deepcopy(sources)
        sources['features'][0]['verdict'] = 'FAIL'
        for receipt in receipts.values():
            receipt['bundle_sha256'] = digest([plan, figures, specification, sources])
    evaluator = getattr(delivery_v2, 'evaluate_filing', lambda *_: {'status': 'missing'})
    assert evaluator(plan, figures, specification, sources, receipts)['status'] == expected


def test_clean_candidate_requires_operator_receipts_for_filing_export():
    import io
    import zipfile

    from PIL import Image

    image = io.BytesIO()
    Image.new('RGB', (600, 600), 'white').save(image, format='PNG')
    plan = {'title': 'Test', 'revision': 1, 'parts': [],
            'views': [{'id': 'main', 'title': '结构图', 'parts': []}]}
    figures = [{'id': 'main', 'png': image.getvalue(), 'record': {'engine': 'vector'}}]
    candidate = delivery_v2.package_drawings(plan, figures, {'technical_status': 'PASS'},
                                              filing_candidate=True)
    assert candidate['manifest']['filing_status'] == 'UNKNOWN'
    assert not candidate['manifest']['filing_ready']
    receipt = candidate['filing_review_rendered']['scale_samples']['main']
    assert receipt['dpi'] == 300 and receipt['scale'] == [2, 3]
    assert receipt['normal_sha256'] != receipt['scaled_sha256']
    assert all(abs(b - a * 2 / 3) <= 1 for a, b in zip(receipt['normal_pixels'], receipt['scaled_pixels']))
    assert b'/ReviewFooter' not in candidate['pdf']
    with zipfile.ZipFile(io.BytesIO(candidate['zip'])) as archive:
        from hashlib import sha256
        import json

        assert 'filing-review.json' in archive.namelist()
        index = json.loads(archive.read('filing-samples/index.json'))
        assert index['pdf_sha256'] == sha256(candidate['pdf']).hexdigest()
        assert index['pages']['main']['page'] == 1
        for key, filename in (('normal', 'normal'), ('scaled', 'two-thirds')):
            sample = archive.read(f'filing-samples/01-{filename}.png')
            with Image.open(io.BytesIO(sample)) as rendered:
                assert abs(rendered.info['dpi'][0] - 300) < 0.1
                assert list(rendered.size) == receipt[f'{key}_pixels']
                assert sha256(rendered.convert('RGB').tobytes()).hexdigest() == receipt[f'{key}_sha256']
            assert sha256(sample).hexdigest() == index['pages']['main'][f'{key}_png_sha256']


def test_candidate_can_release_with_matching_actual_scale_and_professional_receipts():
    import io
    import zipfile
    from hashlib import sha256

    from PIL import Image, ImageDraw

    from patent_drawing.operation_store import digest

    plan = {'title': '核查示例', 'revision': 1, 'parts': [],
            'views': [{'id': 'main', 'title': '结构图', 'parts': []}]}
    drawing = Image.new('RGB', (320, 240), 'white')
    draw = ImageDraw.Draw(drawing)
    draw.rectangle((30, 30, 290, 210), outline='black', width=6)
    draw.line((30, 30, 290, 210), fill='black', width=4)
    figure = io.BytesIO()
    drawing.save(figure, format='PNG')
    figures = [{'id': 'main', 'png': figure.getvalue(), 'record': {'engine': 'vector'}}]
    quality = {'technical_status': 'UNKNOWN'}
    preview = delivery_v2.package_drawings(plan, figures, quality, filing_candidate=True)
    actual = preview['filing_review_rendered']
    specification = {'description_sha256': digest('complete description'),
                     'claims_sha256': digest('claims'), 'marks': {'main': {}}}
    source = {'source_sha256': digest('approved source'), 'required_feature_ids': ['relation'],
              'features': [{'feature_id': 'relation', 'artifact_sha256': digest('exact original'),
                            'locator': 'page 1 / region A', 'support_scope': '定性关系',
                            'reviewer': 'fixture reviewer', 'verdict': 'PASS'}]}
    receipts = {gate: {'bundle_sha256': digest([plan, actual, specification, source]),
                       'reviewer': 'fixture reviewer', 'verdict': 'PASS'} for gate in ('F1','F2','F3','F4')}
    receipts['F1']['views'] = ['main']
    receipts['F2']['pages'] = [{**actual['scale_samples']['main'], 'view_id': 'main', 'verdict': 'PASS'}]
    verified = {'specification': specification, 'source_evidence': source, 'review_receipts': receipts}
    sample = actual['scale_samples']['main']
    with zipfile.ZipFile(io.BytesIO(preview['zip'])) as archive:
        with Image.open(io.BytesIO(archive.read(sample['scaled_png']))) as page_image:
            printable = page_image.convert('L')
    registration = [48, 64, 48 + printable.width, 64 + printable.height]

    def register(image, dpi=300):
        full_page = Image.new('L', sample['normal_pixels'], 255)
        if image is not None:
            full_page.paste(image, tuple(registration[:2]))
        buffer = io.BytesIO()
        full_page.save(buffer, format='PNG', dpi=(dpi, dpi))
        return buffer.getvalue()

    scan = register(printable.point(lambda value: min(251, value + 4)))
    print_scan = {'capture': 'printed_and_scanned', 'pdf_sha256': actual['pdf_sha256'],
                  'page': 1, 'page_box': registration, 'dpi': 300,
                  'figure_size_mm': [2 * side / 3 for side in sample['figure_size_mm']]}

    def bind_scan(raw):
        receipts['F2']['pages'][0]['print_scan'] = {**print_scan, 'sha256': sha256(raw).hexdigest()}
        verified['physical_scale_scans'] = {'main': raw}

    without_scan = delivery_v2.package_drawings(plan, figures, quality, filing_candidate=True, filing_inputs=verified)
    assert without_scan['manifest']['filing_status'] == 'UNKNOWN'
    assert not without_scan['manifest']['filing_ready']

    bind_scan(scan)
    no_specialist = delivery_v2.package_drawings(plan, figures, quality, filing_candidate=True, filing_inputs=verified)
    assert no_specialist['manifest']['filing_status'] == 'UNKNOWN'
    assert next(r for r in no_specialist['filing_review']['findings'] if r['rule_id']=='F4')['verdict']=='UNKNOWN'
    originals = {'technical_source': b'test technical source confirmation for the reviewed bundle',
                 'patent_practice': b'test patent practice confirmation for the reviewed bundle'}
    receipts['F4']['thematic_reviews'] = [
        {'role': role, 'name': 'test ' + role + ' reviewer', 'verdict': 'PASS',
         'artifact_sha256': sha256(raw).hexdigest(), 'bundle_sha256': receipts['F4']['bundle_sha256']}
        for role, raw in originals.items()
    ]
    missing_signoff_bytes = delivery_v2.package_drawings(plan, figures, quality, filing_candidate=True,
                                                         filing_inputs=verified)
    assert missing_signoff_bytes['manifest']['filing_status'] == 'UNKNOWN'
    assert not missing_signoff_bytes['manifest']['filing_ready']

    verified['signoff_artifacts'] = originals
    package = delivery_v2.package_drawings(plan, figures, quality, filing_candidate=True, filing_inputs=verified)
    assert package['manifest']['filing_ready'] is True
    bad_scans = [register(None), register(printable, dpi=72), register(printable), b'not the checked scan']
    for bad_scan in bad_scans:
        bind_scan(bad_scan)
        wrong_scan = delivery_v2.package_drawings(plan, figures, quality, filing_candidate=True, filing_inputs=verified)
        assert wrong_scan['manifest']['filing_status'] == 'FAIL'
        assert not wrong_scan['manifest']['filing_ready']

    bind_scan(scan)
    receipts['F2']['pages'][0]['print_scan']['page'] = 2
    wrong_page = delivery_v2.package_drawings(plan, figures, quality, filing_candidate=True, filing_inputs=verified)
    assert wrong_page['manifest']['filing_status'] == 'FAIL'
    bind_scan(scan)
    receipts['F4']['thematic_reviews'][0]['bundle_sha256'] = digest('different drawing revision')
    stale_review = delivery_v2.package_drawings(plan, figures, quality, filing_candidate=True, filing_inputs=verified)
    assert stale_review['manifest']['filing_status'] == 'FAIL'
    receipts['F4']['thematic_reviews'][0]['bundle_sha256'] = receipts['F4']['bundle_sha256']
    verified['signoff_artifacts'] = {**originals, 'patent_practice': originals['technical_source']}
    changed_signoff = delivery_v2.package_drawings(plan, figures, quality, filing_candidate=True, filing_inputs=verified)
    assert changed_signoff['manifest']['filing_status'] == 'FAIL'
    verified['signoff_artifacts'] = originals
    receipts['F2']['pages'][0]['scaled_sha256'] = digest('another document')
    blocked = delivery_v2.package_drawings(plan, figures, quality, filing_candidate=True, filing_inputs=verified)
    assert blocked['manifest']['filing_status'] == 'FAIL' and not blocked['manifest']['filing_ready']


def test_workspace_retains_private_same_revision_signoff_artifacts(tmp_path):
    from hashlib import sha256

    from patent_drawing.operation_store import OperationStore
    from patent_drawing.workspace import Workspace
    from tests.test_v0375_artifacts import fixture_workspace, vector_view

    workspace, wid, state, plan = fixture_workspace(tmp_path, '控制器向显示器发送状态。', ['electrical'])
    plan['views'] = [vector_view('main', 'relationship', [])]
    workspace.stage(wid, plan)
    workspace.render(wid, 'main')
    workspace.package(wid)

    artifact_bytes = {'technical_source': b'technical review fixture', 'patent_practice': b'practice review fixture'}
    result = workspace.record_filing_review(wid, 1, specification={}, source_evidence={
        'source_sha256': state['source_sha256'],
    }, visible_mark_review={}, review_receipts={}, signoff_artifacts=artifact_bytes)
    reopened = Workspace(OperationStore(tmp_path), workspace.owner)
    history = reopened.read(wid)['filing_review_history']
    refs = history[-1]['inputs']['signoff_artifact_refs']
    assert {role: reopened.store.get_artifact(reopened.owner, ref) for role, ref in refs.items()} == artifact_bytes
    assert {role: ref['sha256'] for role, ref in refs.items()} == {
        role: sha256(raw).hexdigest() for role, raw in artifact_bytes.items()}
    release = reopened.filing_candidate(wid, release=True)
    assert release['filing_review'] == result
    assert release['manifest']['filing_status'] == 'UNKNOWN'
    assert 'zip' not in release

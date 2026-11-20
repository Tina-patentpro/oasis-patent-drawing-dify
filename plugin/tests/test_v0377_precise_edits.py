import datetime
from io import BytesIO
import json
import zipfile

from PIL import Image, ImageDraw
import pytest

from patent_drawing import reference_preparation
from patent_drawing.operation_store import OperationStore
from patent_drawing.workspace import Workspace
from tests.test_v0377_natural_completion import BASE

APPROVED_LINE = {'mode': 'local', 'endpoints': [[476, 470], [568, 470]], 'width': 6}
FIXTURE = BASE / 'ab-fixture-attempt-04'
OUTPUTS = BASE / 'credit-restored-01/ab2-verification'


@pytest.mark.parametrize('branch', ['direct', 'agent'])
def test_archived_black_band_rejected_after_composition(branch):
    base, mask = ((FIXTURE / name).read_bytes() for name in ('section.png', 'mask.png'))
    raw = (OUTPUTS / f'{branch}-provider.png').read_bytes()
    delivery, changes = reference_preparation.compose_reviewed_mask(base, mask, raw)
    assert changes['delivery_changed_outside'] == 0
    for image in (raw, delivery, (OUTPUTS / f'{branch}-delivery.png').read_bytes()):
        proof = reference_preparation.inspect_precise_line(base, mask, image, APPROVED_LINE)
        assert proof['verdict'] == 'FAIL'
        assert {'line_width', 'outside_line'} <= set(proof['issues'])


def test_approved_line_exports_using_existing_source_engine_with_zero_model(tmp_path):
    inputs = [{'name': name, 'blob': (FIXTURE / name).read_bytes()}
              for name in ('overview.png', 'section.png', 'materials.txt')]
    w = Workspace(OperationStore(tmp_path), 'local')
    wid = w.ingest(inputs, technical_types=['mechanical'], patent_type='invention',
                   application_stage='draft')['workspace_id']
    plan = json.loads((FIXTURE / 'plan.json').read_bytes())
    mask = (FIXTURE / 'mask.png').read_bytes()
    prep = json.loads((FIXTURE / 'reference.json').read_bytes())['approval_input']
    prep.update(line_edit=APPROVED_LINE, reviewed_at=datetime.datetime.now(datetime.UTC).isoformat())
    state = w.read(wid)
    assert state['source_sha256'] == plan['source_sha256']
    state['proposal'] = plan
    w.save(wid, state)
    w.approve_reference_preparation(wid, 'section', prep, mask, candidate=True)
    plan = w.read(wid)['proposal']
    assert w.stage(wid, plan, require_image_route=True)['required_image_calls'] == 0
    rendered = w.render(wid, 'section')
    assert rendered['model_calls'] == 0
    proof = rendered['record']['masked_edit']['precision']
    assert proof['verdict'] == 'PASS' and proof['outside_mask'] == 0
    assert proof['median_width'] == 6
    with Image.open(BytesIO(rendered['png'])) as image:
        assert image.getpixel((522, 470)) == (0, 0, 0)
        assert image.getpixel((575, 470)) == (255, 255, 255)
    w.render(wid, 'overview')
    export = w.package(wid)
    assert export['zip'] == w.package(wid, export_current=True, revision=1)['zip']


def test_precise_line_rejects_stray_ink_and_erasure():
    base, mask = ((FIXTURE / name).read_bytes() for name in ('section.png', 'mask.png'))
    png, _ = reference_preparation.render_precise_line(base, mask, APPROVED_LINE)
    with Image.open(BytesIO(png)) as image:
        draw = ImageDraw.Draw(image)
        draw.point((477, 460), fill='black')
        draw.point((475, 470), fill='white')
        buf = BytesIO()
        image.save(buf, format='PNG')
    proof = reference_preparation.inspect_precise_line(base, mask, buf.getvalue(), APPROVED_LINE)
    assert {'outside_line', 'erased_source_ink'} <= set(proof['issues'])


def test_saved_supplier_black_band_blocks_delivery_and_is_not_dispatched_again(tmp_path):
    import base64

    import httpx

    from patent_drawing.client import OpenAIImagesClient
    from patent_drawing.errors import DrawingError

    band = (OUTPUTS / 'direct-provider.png').read_bytes()
    calls = []

    def provider(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={'data': [{'b64_json': base64.b64encode(band).decode()}]},
                              headers={'x-request-id': 'req_fixture_band'})

    w = Workspace(OperationStore(tmp_path), 'supplier', allow_models=True,
                  image_client=OpenAIImagesClient('fixture', transport=httpx.MockTransport(provider)))
    inputs = [{'name': name, 'blob': (FIXTURE / name).read_bytes()}
              for name in ('overview.png', 'section.png', 'materials.txt')]
    wid = w.ingest(inputs, technical_types=['mechanical'], patent_type='invention',
                   application_stage='draft')['workspace_id']
    plan = json.loads((FIXTURE / 'plan.json').read_bytes())
    mask = (FIXTURE / 'mask.png').read_bytes()
    prep = json.loads((FIXTURE / 'reference.json').read_bytes())['approval_input']
    prep.update(line_edit={**APPROVED_LINE, 'mode': 'supplier'},
                reviewed_at=datetime.datetime.now(datetime.UTC).isoformat())
    state = w.read(wid)
    state['proposal'] = plan
    w.save(wid, state)
    w.approve_reference_preparation(wid, 'section', prep, mask, candidate=True)
    w.stage(wid, w.read(wid)['proposal'], 1)
    w.render(wid, 'overview')
    for _ in range(2):
        with pytest.raises(DrawingError, match='precise_edit_rejected'):
            w.render(wid, 'section')
    assert calls == ['/v1/images/edits']
    execution = w.execution_state(wid)
    assert execution['blocking_state'] == 'precise_edit_rejected'
    assert execution['missing_ids'] == ['section']
    assert set(w.partial(wid)['figures']) == {'overview'}
    with zipfile.ZipFile(BytesIO(w.package_partial(wid)['zip'])) as archive:
        assert not any(name.startswith('figures/section.') for name in archive.namelist())


def test_transparent_black_does_not_count_as_visible_ink():
    base = Image.new('RGBA', (90, 50), 'white')
    draw = ImageDraw.Draw(base)
    draw.rectangle((10, 10, 20, 40), fill='black')
    draw.rectangle((70, 10, 80, 40), fill='black')
    mask = Image.new('RGBA', base.size, 'black')
    ImageDraw.Draw(mask).rectangle((20, 10, 70, 40), fill=(0, 0, 0, 0))
    premature = base.copy()
    ImageDraw.Draw(premature).rectangle((20, 22, 70, 27), fill=(0, 0, 0, 0))
    line = {'mode': 'supplier', 'endpoints': [[20, 25], [70, 25]], 'width': 6}
    proof = reference_preparation.inspect_precise_line(
        encode_png(base), encode_png(mask), encode_png(premature), line)
    assert proof['verdict'] == 'FAIL'
    assert {'line_gap', 'erased_source_ink'} <= set(proof['issues'])


def encode_png(image):
    output = BytesIO()
    image.save(output, 'PNG')
    return output.getvalue()


def test_detached_pixel_within_tolerated_corridor_is_rejected():
    base = Image.new('RGB', (90, 50), 'white')
    draw = ImageDraw.Draw(base)
    draw.rectangle((10, 10, 20, 40), fill='black')
    draw.rectangle((70, 10, 80, 40), fill='black')
    mask = Image.new('RGBA', base.size, 'black')
    ImageDraw.Draw(mask).rectangle((20, 10, 70, 40), fill=(0, 0, 0, 0))
    supplier = base.copy()
    draw = ImageDraw.Draw(supplier)
    draw.rectangle((20, 23, 70, 27), fill='black')
    draw.point((45, 22), fill='white')
    # A very thin but separate stroke may coexist with a correct median width.
    draw.rectangle((45, 25, 45, 27), fill='white')
    draw.point((45, 27), fill='black')
    line = {'mode': 'supplier', 'endpoints': [[20, 25], [70, 25]], 'width': 6}
    proof = reference_preparation.inspect_precise_line(
        encode_png(base), encode_png(mask), encode_png(supplier), line)
    assert proof['verdict'] == 'FAIL'
    assert 'line_gap' in proof['issues']
    # Build a continuous in-tolerance 5 px stroke with one isolated dark point.
    supplier = base.copy()
    draw = ImageDraw.Draw(supplier)
    draw.rectangle((20, 22, 70, 26), fill='black')
    draw.point((45, 28), fill='black')
    proof = reference_preparation.inspect_precise_line(
        encode_png(base), encode_png(mask), encode_png(supplier), line)
    assert proof['verdict'] == 'FAIL'
    assert 'detached_ink' in proof['issues']


def test_approved_one_pixel_diagonal_is_contiguous():
    base = Image.new('RGB', (64, 64), 'white')
    draw = ImageDraw.Draw(base)
    draw.line((0, 0, 10, 10), fill='black', width=1)
    draw.line((50, 50, 63, 63), fill='black', width=1)
    mask = Image.new('RGBA', base.size, 'black')
    ImageDraw.Draw(mask).rectangle((9, 9, 51, 51), fill=(0, 0, 0, 0))
    line = {'mode': 'local', 'endpoints': [[10, 10], [50, 50]], 'width': 1}
    _, proof = reference_preparation.render_precise_line(encode_png(base), encode_png(mask), line)
    assert proof['verdict'] == 'PASS'

import base64
import datetime
import io
import json

import httpx
import pytest
from PIL import Image, ImageChops, ImageDraw, ImageOps

from patent_drawing.client import OpenAIImagesClient
from patent_drawing.operation_store import OperationStore, digest
from patent_drawing.workspace import Workspace


def make_workspace(tmp_path, bbox, engine='image'):
    image = Image.new('RGB', (128, 96), 'white')
    ImageDraw.Draw(image).line((3, 4, 120, 76), fill='black', width=3)
    output = io.BytesIO()
    image.save(output, format='PNG')
    requests = []

    def respond(request):
        requests.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={
            'data': [{'b64_json': base64.b64encode(output.getvalue()).decode()}],
        })

    client = OpenAIImagesClient('offline-test', transport=httpx.MockTransport(respond))
    workspace = Workspace(OperationStore(tmp_path), 'reviewer', allow_models=True, image_client=client)
    quote = '连接板的边缘有一处缺口。'
    wid = workspace.ingest([{'name': 'disclosure.txt', 'blob': quote.encode()}])['workspace_id']
    state = workspace.read(wid)
    parent = {'id': 'main', 'engine': 'image', 'requested_type': 'relationship',
              'delivered_type': 'relationship', 'title': '关系图', 'purpose': '连接板边缘关系',
              'instruction': '示意连接板边缘的一处缺口', 'parts': ['plate'], 'required_facts': ['edge']}
    child = {**parent, 'id': 'detail', 'engine': engine, 'requested_type': 'detail', 'delivered_type': 'detail',
             'title': '缺口局部图', 'parent_view': 'main', 'depends_on': ['main'],
             'region': {'space': 'parent_base_pixels', 'bbox': bbox}}
    plan = {'schema_version': 'drawing-set-2', 'set_id': 'edge_detail', 'revision': 1,
            'source_sha256': state['source_sha256'], 'title': '连接板边缘', 'patent_type': 'invention',
            'technical_types': ['mechanical'], 'parts': [{'id': 'plate', 'name': '连接板', 'label': '1'}],
            'facts': [{'id': 'edge', 'status': 'confirmed', 'text': quote,
                       'evidence': [{'page_id': state['bundle']['pages'][0]['id'], 'quote': quote}]}],
            'views': [parent, child]}
    workspace.stage(wid, plan, image_budget=2 if engine == 'image' else 1)
    record = workspace.render(wid, 'main')['record']
    contract = workspace.read(wid)['work']['plan']['visibility_contract']['main']
    workspace.review_visibility(wid, 'main', {
        'view_id': 'main', 'base_sha256': record['base']['sha256'],
        'spec_sha256': record['base_spec_sha256'], 'contract_sha256': digest(contract),
        'reviewer': 'offline test', 'reviewed_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'features': [{'feature_id': item['feature_id'], 'verdict': 'PASS',
                      'region': [2, 3, 100, 80], 'note': 'synthetic regression fixture'}
                     for item in contract['features']],
    })
    return workspace, wid, image, requests, record


def test_detail_sends_confirmed_parent_pixel_crop_and_binds_it_to_the_review(tmp_path):
    bbox = [23, 17, 99, 71]
    workspace, wid, parent_image, requests, parent = make_workspace(tmp_path, bbox)

    child = workspace.render(wid, 'detail')['record']
    path, payload = requests[1]
    data_url = payload['images'][0]['image_url']
    with Image.open(io.BytesIO(base64.b64decode(data_url.split(',', 1)[1]))) as reference:
        assert reference.size == (76, 54)
        assert ImageChops.difference(reference.convert('RGB'), parent_image.crop(bbox)).getbbox() is None
    assert path == '/v1/images/edits'
    assert child['reference_parent_binding']['parent_crop_bbox'] == bbox
    assert child['reference_parent_binding']['parent_base_sha256'] == parent['base']['sha256']
    assert child['reference_parent_binding']['parent_reference_sha256'] == child['reference_sha256']
    assert workspace.render(wid, 'detail')['model_calls'] == 0
    assert len(requests) == 2


def test_detail_rejects_crop_outside_actual_parent_before_request(tmp_path):
    workspace, wid, _, requests, _ = make_workspace(tmp_path, [0, 0, 130, 76])

    with pytest.raises(ValueError, match='outside actual parent image'):
        workspace.render(wid, 'detail')
    assert len(requests) == 1


def review_detail(workspace, wid, record):
    contract = workspace.read(wid)['work']['plan']['visibility_contract']['detail']
    return workspace.review_visibility(wid, 'detail', {
        'view_id': 'detail', 'base_sha256': record['base']['sha256'],
        'spec_sha256': record['base_spec_sha256'], 'contract_sha256': digest(contract),
        'reviewer': 'offline test', 'reviewed_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'features': [{'feature_id': item['feature_id'], 'verdict': 'PASS',
                      'region': [0, 0, 1536, 1024], 'note': 'synthetic regression fixture'}
                     for item in contract['features']],
    })


def test_local_detail_preserves_the_whole_crop_and_requires_its_own_review(tmp_path):
    bbox = [23, 17, 99, 71]
    workspace, wid, parent_image, requests, parent = make_workspace(tmp_path, bbox, 'parent_crop')
    workspace.allow_models = False
    assert workspace.preview(wid)['budget']['image'] == 1
    assert workspace.preflight(wid)['route'] == 'execute'

    child = workspace.render(wid, 'detail')
    assert child['model_calls'] == 0 and len(requests) == 1
    record = child['record']
    assert record['request_id'] is None
    assert record['reference_parent_binding']['parent_crop_bbox'] == bbox
    assert record['reference_parent_binding']['parent_base_sha256'] == parent['base']['sha256']
    with Image.open(io.BytesIO(child['png'])) as actual:
        assert actual.size == (1536, 1024)
        expected = ImageOps.contain(parent_image.crop(bbox), (1440, 928), Image.Resampling.NEAREST)
        canvas = Image.new('RGB', actual.size, 'white')
        canvas.paste(expected, ((actual.width - expected.width) // 2,
                                (actual.height - expected.height) // 2))
        assert ImageChops.difference(actual.convert('RGB'), canvas).getbbox() is None
    assert workspace.preflight(wid)['blocking_state'] == 'awaiting_visual_review'
    assert workspace.partial(wid)['unreviewed_view_ids'] == ['detail']
    with pytest.raises(ValueError, match='visual review required'):
        workspace.package(wid)
    assert review_detail(workspace, wid, record)['technical_review'] == 'PASS'
    assert workspace.package(wid)['quality']['filing_ready'] is False


def test_local_detail_blocks_out_of_bounds_coordinates_before_render(tmp_path):
    workspace, wid, _, requests, _ = make_workspace(tmp_path, [23, 17, 129, 71], 'parent_crop')
    workspace.allow_models = False
    result = workspace.preflight(wid)
    assert result['blocking_state'] == 'invalid_parent_crop'
    assert result['blocked_view'] == 'detail' and result['model_calls'] == 0
    with pytest.raises(ValueError, match='outside actual parent image'):
        workspace.render(wid, 'detail')
    assert len(requests) == 1 and 'detail' not in workspace.partial(wid)['figures']


def test_local_detail_cannot_reuse_review_after_parent_review_changes(tmp_path):
    workspace, wid, _, requests, _ = make_workspace(tmp_path, [23, 17, 99, 71], 'parent_crop')
    workspace.allow_models = False
    record = workspace.render(wid, 'detail')['record']
    review_detail(workspace, wid, record)
    parent_review = workspace.read(wid)['visual_reviews']['main']
    parent_review['reviewer'] = 'new reviewer'
    workspace.review_visibility(wid, 'main', parent_review)

    result = workspace.preflight(wid)
    assert result['stale_parent_review'] and result['blocked_view'] == 'detail'
    with pytest.raises(ValueError, match='parent binding changed'):
        workspace.package(wid)
    with pytest.raises(ValueError, match='parent binding changed'):
        review_detail(workspace, wid, record)
    assert len(requests) == 1

import hashlib

import pytest

from patent_drawing.material_sections import catalog_material, classify_catalog, only_claims, read_spans


def page(text, number, *, source='a', **options):
    return {'id': f'{source}-p{number}', 'source_id': source, 'page': number, 'source_sha256': source*64,
            'text_sha256': hashlib.sha256(text.encode()).hexdigest(), 'text': text, **options}


def test_mixed_in_one_pdf_and_claim_continued_on_next_page():
    pages = [
        page('权利要求书\n1. 一种方法，发送参考，', 1),
        page('由接收侧测量。\n2. 根据权利要求1所述的方法，返回反馈。\n说明书\n依据权利要求1解释实施例。\n附图说明\n图1是流程图。', 2),
    ]
    catalog = catalog_material({'pages': pages})
    claims, spec, captions = catalog['sections']
    assert [s['hint'] for s in catalog['sections']] == ['claims', 'specification', 'specification']
    assert claims['type'] == 'claims'
    assert [p['page'] for p in claims['spans']] == [1, 2]
    assert '由接收侧测量。' in read_spans(pages, claims['spans'])
    assert '依据权利要求1解释实施例。' == read_spans(pages, spec['spans']).strip()
    assert captions['heading'] == '附图说明'
    assert only_claims({'pages': pages}, catalog) is None


def test_unheaded_claims_only_and_readback_hash_binding():
    text = '1. 一种校核方法，获取样本。\n2. 根据权利要求1所述的方法，输出结果。'
    pages = [page(text, 1)]
    catalog = catalog_material({'pages': pages})
    assert only_claims({'pages': pages}, catalog) == text
    with pytest.raises(ValueError):
        read_spans([page('被篡改', 1)], catalog['sections'][0]['spans'])


def test_report_ambiguous_scan_and_multiple_source_versions():
    pages = [page('权利要求书\n1. 一种设备，端点接通。', 1),
             page('权利要求书\n1. 一种设备，端点断开。', 1, source='b'),
             page('', 1, source='c', png_sha256='d'*64, width=500, height=1000)]
    catalog = catalog_material({'pages': pages})
    assert catalog['version_choices']['claims'] == [s['id'] for s in catalog['sections']]
    assert catalog['review_pages'][0]['page_id'] == 'c-p1'
    assert only_claims({'pages': pages}, catalog) is None


def test_model_cannot_replace_verified_heading_or_promote_claim_quotation():
    pages = [page('说明书\n1. 根据权利要求1举例说明。', 1)]
    catalog = catalog_material({'pages': pages})
    item, = catalog['sections']
    classified = classify_catalog(catalog, {item['id']: 'claims'})
    assert classified['sections'][0]['type'] == 'specification'
    assert classified['sections'][0]['decision_mismatch']

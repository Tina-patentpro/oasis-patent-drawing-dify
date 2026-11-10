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


def test_pdf_does_not_include_drafting_comments_as_drawing_caption():
    r = delivery().package_drawings({'title': '甲', 'views': [{'id': 'f', 'title': '整体图', 'purpose': '待确认备注应在报告'}]}, [{'id': 'f', 'png': picture(), 'record': {}}])
    assert r['page_captions'] == ['图1']

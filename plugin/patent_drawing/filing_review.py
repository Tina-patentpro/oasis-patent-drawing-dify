"""Evaluate reviewed filing evidence for a single actual export."""
import hashlib
import io
import math
import re

from .operation_store import digest

GATES = ('F1', 'F2', 'F3', 'F4')
SHA = re.compile(r'[0-9a-f]{64}')
SIGNOFF_ROLES = {'technical_source', 'patent_practice'}


def is_sha(value):
    return isinstance(value, str) and SHA.fullmatch(value) is not None


def filing_scale_samples(pdf, view_ids, *, png_files=None):
    """Recompute native PDF page pixels for the exact filing placement."""
    import pypdfium2

    samples = {}
    with pypdfium2.PdfDocument(pdf) as document:
        if len(document) != len(view_ids):
            raise ValueError('filing page count does not match the view order')
        for index, fid in enumerate(view_ids):
            page = document[index]
            try:
                objects = [obj.get_bounds() for obj in page.get_objects() if obj.type == 3]
                main, caption = objects
                if (not 70.8 <= main[0] <= main[2] <= 552.8
                        or not 42.4 <= main[1] <= main[3] <= 771.2
                        or caption[3] >= main[1] or str(index + 1) not in _page_text(page)):
                    raise ValueError('candidate PDF page, figure or margins do not match')
                sizes, hashes, images = [], [], {}
                for key, name, scale in (('normal', 'normal', 300 / 72),
                                         ('scaled', 'two-thirds', 300 / 72 * 2 / 3)):
                    bitmap = page.render(scale=scale)
                    try:
                        pixels = bitmap.to_pil().convert('RGB')
                        sizes.append(list(pixels.size))
                        hashes.append(hashlib.sha256(pixels.tobytes()).hexdigest())
                        if png_files is not None:
                            path = f'filing-samples/{index + 1:02}-{name}.png'
                            encoded = io.BytesIO()
                            pixels.save(encoded, format='PNG', dpi=(300, 300))
                            png_files[path] = encoded.getvalue()
                            images.update({f'{key}_png': path,
                                           f'{key}_png_sha256': hashlib.sha256(png_files[path]).hexdigest()})
                    finally:
                        bitmap.close()
                samples[fid] = {'scale': [2, 3], 'dpi': 300, 'renderer': 'PDFium RGB',
                    'page': index + 1, 'figure_bounds_pt': list(main),
                    'figure_size_mm': [(main[far] - main[near]) * 25.4 / 72 for near, far in ((0, 2), (1, 3))],
                    'normal_pixels': sizes[0], 'scaled_pixels': sizes[1],
                    'normal_sha256': hashes[0], 'scaled_sha256': hashes[1], **images}
            finally:
                page.close()
    return samples


def _page_text(page):
    text = page.get_textpage()
    try:
        return text.get_text_range().strip()
    finally:
        text.close()


def _scan_matches_page(raw, box, printable, sample):
    from PIL import Image, ImageChops

    if (not isinstance(box, (list, tuple)) or len(box) != 4
            or any(type(coordinate) is not int for coordinate in box)):
        return False
    left, top, right, bottom = box
    pixels_per_mm = 300 / 25.4
    try:
        with Image.open(io.BytesIO(raw), formats=['PNG', 'JPEG']) as scan:
            dpi = scan.info.get('dpi', ())
            if (len(dpi) != 2 or any(not math.isfinite(value) or abs(value - 300) > 1 for value in dpi)
                    or any(abs(actual - expected) > 5 * pixels_per_mm
                           for actual, expected in zip(scan.size, sample['normal_pixels']))
                    or not (0 <= left < right <= scan.width and 0 <= top < bottom <= scan.height)
                    or any(abs(actual - expected) > pixels_per_mm
                           for actual, expected in zip((right - left, bottom - top), sample['scaled_pixels']))):
                return False
            registered = scan.crop(box).convert('RGB')
        with Image.open(io.BytesIO(printable), formats=['PNG']) as page:
            expected = page.convert('L')
    except (OSError, SyntaxError, ValueError, TypeError, Image.DecompressionBombError):
        return False
    if (registered.size == expected.size
            and hashlib.sha256(registered.tobytes()).hexdigest() == sample['scaled_sha256']):
        return False

    # A registered ink comparison catches blank or unrelated pages. The named
    # operator attests print/scan provenance and checks actual legibility.
    actual = registered.convert('L').resize(expected.size, Image.Resampling.BILINEAR)
    ink = [image.point(lambda value: 255 if value < 128 else 0) for image in (actual, expected)]
    overlap = ImageChops.darker(*ink).histogram()[255]
    union = ImageChops.lighter(*ink).histogram()[255]
    return bool(union) and overlap / union >= 0.35


def _review_physical_scale(pages, rendered, scans, printable_pages):
    seen = set()
    status = 'PASS'
    for page in pages:
        fid = page['view_id']
        receipt = page.get('print_scan')
        raw = scans.get(fid)
        sample = rendered['scale_samples'][fid]
        printable = printable_pages.get(sample.get('scaled_png'))
        if receipt is None or raw is None or printable is None:
            status = 'UNKNOWN'
            continue
        if (not isinstance(receipt, dict) or not isinstance(raw, bytes)
                or len(raw) > 15 * 1024 * 1024 or receipt.get('dpi') != 300
                or hashlib.sha256(raw).hexdigest() != receipt.get('sha256')):
            return 'FAIL'
        if not {'capture', 'pdf_sha256', 'page', 'page_box'} <= receipt.keys():
            status = 'UNKNOWN'
            continue
        if (receipt['capture'] != 'printed_and_scanned' or receipt['pdf_sha256'] != rendered['pdf_sha256']
                or type(receipt['page']) is not int or receipt['page'] != sample['page']
                or receipt['sha256'] in seen or not _scan_matches_page(raw, receipt['page_box'], printable, sample)):
            return 'FAIL'
        seen.add(receipt['sha256'])
        measured = receipt.get('figure_size_mm')
        original = sample['figure_size_mm']
        if (not isinstance(measured, (list, tuple)) or len(measured) != 2
                or any(type(value) not in (int, float) or not math.isfinite(value)
                       or value <= 0 or abs(value - expected * 2 / 3) > 1
                       for value, expected in zip(measured, original))):
            return 'FAIL'
    return status


def _review_signoffs(receipt, bundle_sha, originals):
    rows = receipt.get('thematic_reviews', [])
    if not isinstance(rows, list):
        return 'FAIL'
    seen = set()
    covered = {}
    for row in rows:
        if not isinstance(row, dict):
            return 'FAIL'
        role = row.get('role')
        name = row.get('name')
        if (not isinstance(role, str) or role not in SIGNOFF_ROLES or role in seen
                or row.get('bundle_sha256') != bundle_sha or not isinstance(name, str) or not name.strip()
                or not is_sha(row.get('artifact_sha256')) or row.get('verdict') not in ('PASS', 'UNKNOWN')):
            return 'FAIL'
        seen.add(role)
        raw = originals.get(role)
        if raw is not None:
            if not isinstance(raw, bytes) or not raw or hashlib.sha256(raw).hexdigest() != row['artifact_sha256']:
                return 'FAIL'
            if row['verdict'] == 'PASS':
                covered[role] = row['artifact_sha256']
    if len(set(covered.values())) != len(covered):
        return 'FAIL'
    return 'PASS' if covered.keys() == SIGNOFF_ROLES else 'UNKNOWN'


def evaluate_filing(plan, rendered_figures, specification, source_evidence, review_receipts,
                    *, physical_scale_scans=None, printable_pages=None, signoff_artifacts=None):
    bundle_sha = digest([plan, rendered_figures, specification, source_evidence])
    views = {v['id'] for v in plan['views']}
    rendered = rendered_figures.get('views', {})
    spec_marks = specification.get('marks')
    features = source_evidence.get('features', [])
    findings = []

    def check(gate, verdict, action, *, view_id=None, feature_id=None, evidence_ids=()):
        findings.append({'rule_id': gate, 'verdict': verdict, 'view_id': view_id,
                         'feature_id': feature_id, 'evidence_ids': list(evidence_ids),
                         'next_action': action})

    messages = {
        'F1': '逐页检查正式图号、线条和必要文字。',
        'F2': '核对每页原尺寸及 2/3 样张，提交带页面定位和置图实测尺寸的同版 300 dpi 打印扫描件。',
        'F3': '核对实际图面、图说、完整说明书和权利要求。',
        'F4': '核对关键技术关系的原件位置，提交同版技术来源与专利专业的具名签核原件。',
    }
    for gate in GATES:
        receipt = review_receipts.get(gate)
        if not receipt:
            check(gate, 'UNKNOWN', messages[gate])
            continue
        if (not isinstance(receipt, dict) or receipt.get('bundle_sha256') != bundle_sha
                or receipt.get('verdict') not in ('PASS', 'FAIL', 'UNKNOWN')
                or not isinstance(receipt.get('reviewer'), str) or not receipt['reviewer'].strip()):
            check(gate, 'FAIL', '审阅回执和当前图件、申请文件或来源版本不匹配。')
            continue
        verdict = receipt['verdict']
        if verdict != 'PASS':
            check(gate, verdict, messages[gate])
            continue
        if gate == 'F1':
            if (set(receipt.get('views', [])) != views or set(rendered) != views
                    or not all(is_sha(rendered_figures.get(key)) for key in ('pdf_sha256', 'captions_sha256'))
                    or not all(is_sha(figure.get('sha256')) for figure in rendered.values())):
                verdict = 'FAIL'
        elif gate == 'F2':
            pages = receipt.get('pages', [])
            samples = rendered_figures.get('scale_samples')
            if (not isinstance(pages, list) or len(pages) != len(views)
                    or any(not isinstance(page, dict) or not isinstance(page.get('view_id'), str) for page in pages)
                    or {page['view_id'] for page in pages} != views
                    or any(page.get('scale') != [2, 3] or page.get('dpi') != 300
                           or not page.get('renderer') or page.get('verdict') != 'PASS'
                           or not is_sha(page.get('normal_sha256')) or not is_sha(page.get('scaled_sha256'))
                           or page.get('normal_sha256') == page.get('scaled_sha256') for page in pages)):
                verdict = 'FAIL'
            elif samples is not None and (set(samples) != views or any(
                    any(page.get(field) != value for field, value in samples[page['view_id']].items()
                        if field in {'scale', 'dpi', 'renderer', 'normal_sha256', 'scaled_sha256'})
                    for page in pages)):
                verdict = 'FAIL'
            elif not samples:
                verdict = 'UNKNOWN'
            else:
                verdict = _review_physical_scale(pages, rendered_figures, physical_scale_scans or {},
                                                 printable_pages or {})
        elif gate == 'F3':
            if (not isinstance(spec_marks, dict)
                    or not all(is_sha(specification.get(key)) for key in ('description_sha256', 'claims_sha256'))
                    or set(rendered) != views):
                verdict = 'UNKNOWN'
            elif spec_marks != {fid: row.get('marks', {}) for fid, row in rendered.items()}:
                verdict = 'FAIL'
        elif gate == 'F4':
            required = set(source_evidence.get('required_feature_ids', []))
            covered = {row.get('feature_id') for row in features}
            if (not features or not is_sha(source_evidence.get('source_sha256'))
                    or required - covered):
                verdict = 'UNKNOWN'
            for row in features:
                if row.get('verdict') == 'FAIL':
                    verdict = 'FAIL'
                    break
                if (row.get('verdict') != 'PASS' or not is_sha(row.get('artifact_sha256'))
                        or any(not row.get(field) for field in ('feature_id', 'locator', 'support_scope', 'reviewer'))):
                    verdict = 'UNKNOWN'
            signoffs = _review_signoffs(receipt, bundle_sha, signoff_artifacts or {})
            if signoffs == 'FAIL' or verdict == 'PASS':
                verdict = signoffs
        check(gate, verdict, '' if verdict == 'PASS' else messages[gate])
    status = 'FAIL' if any(row['verdict'] == 'FAIL' for row in findings) else (
        'UNKNOWN' if any(row['verdict'] == 'UNKNOWN' for row in findings) else 'PASS')
    return {'status': status, 'revision': plan['revision'], 'bundle_sha256': bundle_sha, 'findings': findings}

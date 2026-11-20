"""Split material by source and retain exact intervals for every candidate."""
import hashlib
import re
from collections import defaultdict

from .claims_input import read_claims_text
from .operation_store import digest


TYPES = ('claims', 'specification', 'disclosure', 'other')
UNKNOWN = 'uncertain'
HEADINGS = {
    '权利要求书': 'claims', '权利要求': 'claims',
    '说明书': 'specification', '技术领域': 'specification', '背景技术': 'specification',
    '发明内容': 'specification', '实用新型内容': 'specification',
    '具体实施方式': 'specification', '具体实施例': 'specification', '附图说明': 'specification',
    '技术交底书': 'disclosure', '技术交底材料': 'disclosure', '技术交底': 'disclosure',
    '补充材料': 'other', '其他材料': 'other', '参考材料': 'other',
}
_HEADING = re.compile(
    r'(?m)^[ \t]*(?:#{1,6}[ \t]*)?(?P<name>' +
    '|'.join(sorted(HEADINGS, key=len, reverse=True)) +
    r')(?P<suffix>[ \t]*(?:[:：][ \t]*|(?=\r?$)))'
)


def read_spans(pages, spans):
    by_id = {page['id']: page for page in pages}
    text = []
    for span in spans:
        page = by_id.get(span['page_id'])
        if not page or span['source_sha256'] != page['source_sha256'] or span['page'] != page['page']:
            raise ValueError('材料页来源与原记录不一致。')
        raw = page['text']
        start, end = span['start'], span['end']
        quote = raw[start:end]
        if (not 0 <= start < end <= len(raw)
                or span['text_sha256'] != hashlib.sha256(raw.encode()).hexdigest()
                or span['quote_sha256'] != hashlib.sha256(quote.encode()).hexdigest()):
            raise ValueError('材料段原文或偏移与原记录不一致。')
        text.append(quote)
    return '\n'.join(text)


def _append_span(spans, page, start, end):
    quote = page['text'][start:end]
    if quote.strip():
        spans.append({
            'page_id': page['id'], 'source_sha256': page['source_sha256'],
            'text_sha256': page['text_sha256'], 'page': page['page'], 'start': start, 'end': end,
            'quote_sha256': hashlib.sha256(quote.encode()).hexdigest(),
        })


def _claim_body(text):
    try:
        return read_claims_text(text)[0]
    except ValueError:
        return None


def _version_choices(sections):
    choices = defaultdict(list)
    for section in sections:
        if section['type'] == 'claims':
            choices[section['type']].append(section['id'])
        elif section['type'] in {'specification', 'disclosure'}:
            choices[section['type']].append(section['source_sha256'])
    versions = {}
    for kind, identities in choices.items():
        if len(set(identities)) > 1:
            versions[kind] = [s['id'] for s in sections if s['type'] == kind]
    return versions


def catalog_material(bundle):
    pages = bundle['pages']
    sections, review, spans = [], [], []
    heading = ''
    previous_source = None
    current_kind = UNKNOWN

    def finish():
        if not spans:
            return
        text = read_spans(pages, spans)
        hint = current_kind
        if not heading and _claim_body(text):
            hint = 'claims'
        known = hint if hint != 'claims' or _claim_body(text) else UNKNOWN
        sections.append({'id': 's' + digest([heading, spans])[:16], 'heading': heading, 'hint': hint,
                         'type': known, 'source_sha256': spans[0]['source_sha256'], 'spans': list(spans)})
        spans.clear()

    for page in pages:
        if page['source_id'] != previous_source:
            finish()
            heading, current_kind = '', UNKNOWN
            previous_source = page['source_id']
        text = page['text']
        if (page.get('reading_order') == 'uncertain' or page.get('ocr_unverified')
                or not text.strip() and page.get('height')):
            finish()
            review.append({'page_id': page['id'], 'page': page['page'], 'source_sha256': page['source_sha256'],
                           'page_image_sha256': page.get('png_sha256'),
                           'reason': 'reading_order' if text.strip() and not page.get('ocr_unverified') else 'no_verified_text'})
            continue
        last = 0
        for match in _HEADING.finditer(text):
            _append_span(spans, page, last, match.start())
            finish()
            heading = match['name']
            indicated = HEADINGS[heading]
            if indicated == 'specification' and heading != '说明书' and current_kind == 'disclosure':
                indicated = 'disclosure'
            current_kind = indicated
            last = match.end()
        _append_span(spans, page, last, len(text))
    finish()
    assets = []
    by_id = {page['id']: page for page in pages}
    for candidate in bundle.get('figure_candidates', []):
        page = by_id[candidate['page_id']]
        assets.append({key: candidate[key] for key in ('id', 'page_id', 'kind', 'bbox') if key in candidate}
                      | {'source_sha256': page['source_sha256'], 'page': page['page'],
                         'page_image_sha256': page['png_sha256'], 'status': 'candidate'})
    sources = [{k: row[k] for k in ('id','sha256','name') if k in row} for row in bundle.get('sources',[])]
    return {'sections': sections, 'review_pages': review, 'figures': assets, 'sources':sources,
            'version_choices': _version_choices(sections), 'figure_presence': bundle.get('figure_presence', 'absent')}


def only_claims(bundle, catalog):
    if (len(catalog['sections']) != 1 or catalog['review_pages'] or catalog['figures']
            or catalog['sections'][0]['type'] != 'claims'):
        return None
    text = read_spans(bundle['pages'], catalog['sections'][0]['spans'])
    return _claim_body(text)


def classify_catalog(catalog, answers):
    sections = []
    for candidate in catalog['sections']:
        choice = answers[candidate['id']]
        if choice not in (*TYPES, UNKNOWN):
            raise ValueError('Luna 材料类型超出已登记选项。')
        # Numbered text without a verified claim structure cannot become a claim source.
        supported = candidate['type'] if candidate['type'] != UNKNOWN or choice == 'claims' else choice
        sections.append({**candidate, 'type': supported, 'decision': choice,
                         'decision_mismatch': supported != choice})
    return {**catalog, 'sections': sections, 'version_choices': _version_choices(sections)}


def scope_catalog(catalog, selected):
    """Resolve explicitly selected versions without changing the original directory."""
    sections, deferred = [], []
    by_id = {entry['id']: entry for entry in catalog['sections']}
    for section in catalog['sections']:
        kind = section['type']
        if kind not in catalog['version_choices']:
            sections.append(section)
            continue
        chosen = by_id.get(selected.get(kind))
        if chosen and chosen['type'] == kind and (
                section['id'] == chosen['id'] if kind == 'claims'
                else section['source_sha256'] == chosen['source_sha256']):
            sections.append(section)
        else:
            deferred.append(section['id'])
    return {**catalog, 'sections': sections, 'excluded_versions': deferred,
            'version_choices': {k:v for k,v in catalog['version_choices'].items()
                                if selected.get(k) not in v}}

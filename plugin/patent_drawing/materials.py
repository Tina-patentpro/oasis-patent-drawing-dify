"""Bind full source pages, text and graphic regions before planning any views."""
import base64
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys

from PIL import Image


def sha(blob):
    return hashlib.sha256(blob).hexdigest()


def ingest_material(files, max_bytes=16 * 1024 * 1024):
    if not isinstance(files, list) or not 1 <= len(files) <= 6:
        raise ValueError('source file capacity exceeded (1..6)')
    if any(not isinstance(f, dict) or not isinstance(f.get('blob'), bytes) for f in files):
        raise ValueError('source bytes required')
    if sum(len(f['blob']) for f in files) > max_bytes:
        raise ValueError('source capacity exceeded; original not truncated')
    sources, pages, candidates = [], [], []
    total_text, total_pixels = 0, 0
    for item in files:
        if item.get('visibility', 'generation_visible') != 'generation_visible':
            raise ValueError('evaluation-only sources are not generation inputs')
        blob = item['blob']; name = Path(item.get('name', 'material')).name[:180]
        suffix = Path(name).suffix.lower(); h = sha(blob); sid = 'src_' + h[:24]
        if any(x['id'] == sid for x in sources):
            continue
        sources.append({'id': sid, 'name': name, 'sha256': h, 'bytes': len(blob), 'visibility': 'generation_visible'})
        if suffix == '.pdf' and blob.startswith(b'%PDF'):
            result = subprocess.run([sys.executable, '-m', 'patent_drawing.pdf_worker'], input=blob,
                capture_output=True, timeout=25, cwd=Path(__file__).resolve().parents[1])
            if len(result.stdout) > 40 * 1024 * 1024:
                raise ValueError('PDF result capacity exceeded')
            if result.returncode:
                raise ValueError('PDF解析失败、加密或资源capacity超限；未调用模型')
            extracted = json.loads(result.stdout)['pages']
            for pg in extracted:
                pg['png'] = base64.b64decode(pg.pop('png_base64'), validate=True)
        elif suffix in {'.png', '.jpg', '.jpeg'}:
            with Image.open(io.BytesIO(blob)) as im:
                if im.width * im.height > 8_000_000:
                    raise ValueError('source image pixel capacity exceeded')
                im.load(); im = im.convert('RGB'); out = io.BytesIO(); im.save(out, format='PNG')
                extracted = [{'page': 1, 'text': '', 'png': out.getvalue(), 'width': im.width, 'height': im.height,
                    'graphics': [{'kind': 'supplied_image', 'bbox': [0, 0, im.width, im.height]}], 'figure_presence': 'present'}]
        elif suffix in {'.txt', '.md'}:
            extracted = [{'page': 1, 'text': blob.decode('utf-8-sig'), 'png': b'', 'width': 0, 'height': 0,
                          'graphics': [], 'figure_presence': 'absent'}]
        else:
            raise ValueError('unsupported source format; use PDF, PNG/JPEG or UTF-8 text')
        for pg in extracted:
            total_text += len(pg['text']); total_pixels += pg['width'] * pg['height']
            if total_text > 64000 or total_pixels > 20_000_000 or len(pages) >= 12:
                raise ValueError('aggregate source capacity exceeded; no truncation')
            pg.update(id=sid + '_p' + str(pg['page']), source_id=sid, source_sha256=h,
                      text_sha256=sha(pg['text'].encode()), png_sha256=sha(pg['png']))
            for i, graphic in enumerate(pg['graphics']):
                candidates.append({'id': pg['id'] + '_g' + str(i + 1), 'page_id': pg['id'], **graphic,
                    'semantic_review': 'not_reviewed'})
            pages.append(pg)
    presence = 'present' if any(p['figure_presence'] == 'present' for p in pages) else 'unknown' if any(p['figure_presence'] == 'unknown' for p in pages) else 'absent'
    return {'schema_version': 'source-bundle-1', 'sources': sources, 'pages': pages, 'figure_candidates': candidates,
        'figure_presence': presence, 'strategy': {'present': 'modify_supplement', 'absent': 'create_new', 'unknown': 'review_source_figures'}[presence],
        'text': '\n\n'.join(f'[{p["id"]}]\n{p["text"]}' for p in pages),
        'extractor': 'pypdfium2-5.14.0/process; full pages retained; graphic detection is not semantic acceptance'}

"""Deterministic annotation plane and exact changed-pixel masks."""
import base64
import hashlib
import io
import math
from pathlib import Path
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont, ImageChops

FONT = Path(__file__).resolve().parents[1] / '_assets/NotoSansSC.ttf'


def regular_font(size):
    face = ImageFont.truetype(str(FONT), size)
    if hasattr(face, 'get_variation_names'):
        try:
            names = face.get_variation_names()
            if b'Regular' in names: face.set_variation_by_name(b'Regular')
        except OSError:
            pass
    return face


@lru_cache(maxsize=1)
def font_hash():
    return hashlib.sha256(FONT.read_bytes()).hexdigest()


def fingerprint():
    return {'renderer': 'layered-pillow-v2-text-plane', 'font_sha256': font_hash(), 'weight': 'Regular'}


def png(image):
    stream = io.BytesIO(); image.save(stream, format='PNG', compress_level=6); return stream.getvalue()


def compose(base, annotations, embedded_labels=False):
    if embedded_labels:
        raise ValueError('embedded labels require reviewed migration; no white-box erasure')
    if not isinstance(annotations, list) or len(annotations) > 120:
        raise ValueError('annotation capacity exceeded')
    with Image.open(io.BytesIO(base)) as opened:
        if opened.width * opened.height > 8_000_000: raise ValueError('annotation image capacity')
        image = opened.convert('RGB')
    layer = Image.new('RGBA', image.size, (0, 0, 0, 0)); draw = ImageDraw.Draw(layer); regions = []
    for a in annotations:
        at, target, region = a.get('at'), a.get('target'), a.get('target_region')
        values = (at or []) + (target or []) + (region or [])
        if len(values) != 8 or any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
            raise ValueError('invalid annotation coordinates')
        x, y = at; tx, ty = target; left, top, right, bottom = region
        if a.get('anchor_status') != 'reviewed' or not a.get('part_id') or not (0 <= left <= tx <= right <= image.width and 0 <= top <= ty <= bottom <= image.height):
            raise ValueError('annotation anchor not reviewed/in correct part region')
        label = str(a.get('label', '')); size = a.get('font_size', 30)
        if not 1 <= len(label) <= 32 or type(size) is not int or not 14 <= size <= 80:
            raise ValueError('invalid annotation label/size')
        face = regular_font(size); box = draw.textbbox((x, y), label, font=face, anchor='lt')
        if not (0 <= box[0] < box[2] <= image.width and 0 <= box[1] < box[3] <= image.height):
            raise ValueError('annotation clips canvas')
        for prior in regions:
            b = prior['text_box']
            if min(box[2], b[2]) > max(box[0], b[0]) and min(box[3], b[3]) > max(box[1], b[1]):
                raise ValueError('annotation labels overlap')
        start = (box[0] - 5 if tx < x else box[2] + 5, (box[1] + box[3]) / 2)
        draw.line([start, (tx, ty)], fill='black', width=2)
        draw.text((x, y), label, font=face, anchor='lt', fill='black')
        regions.append({'text_box': list(box)})
    if regions:
        regions[0]['mask'] = base64.b64encode(png(layer.getchannel('A').point(lambda v: 255 if v else 0))).decode()
    image.paste(layer, mask=layer.getchannel('A'))
    return {'png': png(image), 'base_sha256': hashlib.sha256(base).hexdigest(), 'regions': regions,
            'renderer_fingerprint': fingerprint(), 'anchor_semantic_review': 'caller supplied reviewed region; not inferred from part_id'}


def outside_regions_unchanged(before, after, regions):
    with Image.open(io.BytesIO(before)) as a, Image.open(io.BytesIO(after)) as b:
        if a.size != b.size: return False
        mask = Image.new('L', a.size, 0)
        for r in regions:
            if isinstance(r, list):
                ImageDraw.Draw(mask).rectangle(r, fill=255)
            elif r.get('mask'):
                with Image.open(io.BytesIO(base64.b64decode(r['mask'], validate=True))) as m:
                    if m.size != a.size: raise ValueError('annotation mask size mismatch')
                    mask = ImageChops.lighter(mask, m.convert('L'))
        diff = ImageChops.difference(a.convert('RGB'), b.convert('RGB'))
        diff.paste((0, 0, 0), mask=mask)
        return diff.getbbox() is None

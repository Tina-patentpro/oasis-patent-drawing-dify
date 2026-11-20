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


def annotation_targets(annotation):
    extras = annotation.get('additional_targets', [])
    if not isinstance(extras, list) or len(extras) > 1 or any(
            not isinstance(row, dict) or set(row) != {'target', 'target_region'} for row in extras):
        raise ValueError('invalid additional annotation leader')
    return [(row.get('target'), row.get('target_region')) for row in [annotation, *extras]]


def dashed_leader_segments(start, end, width):
    length = math.dist(start, end)
    dash = max(10, width * 3)
    if length <= dash:
        return [(start, end)]
    segments = []
    # Start at the actual endpoint so every dashed leader reaches its reviewed stroke.
    for offset in range(0, math.ceil(length), dash * 2):
        t0, t1 = offset / length, min(offset + dash, length) / length
        segments.append(tuple(tuple(end[i] + (start[i] - end[i]) * t for i in (0, 1)) for t in (t0, t1)))
    return segments


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
        at = a.get('at')
        endpoints = annotation_targets(a)
        if not isinstance(at, list) or len(at) != 2 or any(type(v) not in (int, float)
                                                          or not math.isfinite(v) for v in at):
            raise ValueError('invalid annotation coordinates')
        x, y = at
        for target, region in endpoints:
            if (not isinstance(target, list) or not isinstance(region, list) or len(target) != 2 or len(region) != 4
                    or any(type(v) not in (int, float) or not math.isfinite(v) for v in target + region)):
                raise ValueError('invalid annotation coordinates')
            tx, ty = target
            left, top, right, bottom = region
            if a.get('anchor_status') != 'reviewed' or not a.get('part_id') or not (
                    0 <= left <= tx <= right <= image.width and 0 <= top <= ty <= bottom <= image.height):
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
        leader_width = a.get('leader_width', 2)
        if type(leader_width) is not int or not 2 <= leader_width <= 8:
            raise ValueError('invalid annotation leader width')
        for (tx, ty), _ in endpoints:
            start = (box[0] - 5 if tx < x else box[2] + 5, (box[1] + box[3]) / 2)
            segments = (dashed_leader_segments(start, (tx, ty), leader_width) if len(endpoints) > 1
                        else [(start, (tx, ty))])
            for segment in segments:
                draw.line(list(segment), fill='black', width=leader_width)
        draw.text((x, y), label, font=face, anchor='lt', fill='black')
        regions.append({'text_box': list(box)})
    if regions:
        regions[0]['mask'] = base64.b64encode(png(layer.getchannel('A').point(lambda v: 255 if v else 0))).decode()
    image.paste(layer, mask=layer.getchannel('A'))
    return {'png': png(image), 'base_sha256': hashlib.sha256(base).hexdigest(), 'regions': regions,
            'renderer_fingerprint': fingerprint(), 'anchor_semantic_review': 'caller supplied reviewed region; not inferred from part_id'}


def append_svg_annotations(svg, annotations, regions, before=None, after=None):
    """Apply the same reviewed label and leader geometry used for the PNG."""
    if not annotations:
        return svg
    import xml.etree.ElementTree as ET

    ns = '{http://www.w3.org/2000/svg}'
    root = ET.fromstring(svg)
    ink = None
    if before is not None and after is not None:
        with Image.open(io.BytesIO(before)) as base, Image.open(io.BytesIO(after)) as rendered:
            ink = ImageChops.difference(base.convert('L'), rendered.convert('L'))
    for annotation, region in zip(annotations, regions, strict=True):
        x, y = annotation['at']
        left, top, right, bottom = region['text_box']
        middle = (top + bottom) / 2
        endpoints = annotation_targets(annotation)
        width = annotation.get('leader_width', 2)
        for (tx, ty), _ in endpoints:
            start = left - 5 if tx < x else right + 5
            segments = (dashed_leader_segments((start, middle), (tx, ty), width) if len(endpoints) > 1
                        else [((start, middle), (tx, ty))])
            for first, second in segments:
                ET.SubElement(root, ns + 'polyline', points=f'{first[0]},{first[1]} {second[0]},{second[1]}',
                              fill='none', stroke='black', **{'stroke-width': str(width)})
        label = ET.SubElement(root, ns + 'text', x=str(x), y=str(y), fill='black',
                              **{'dominant-baseline': 'text-before-edge',
                                 'font-family': 'Noto Sans SC, sans-serif',
                                 'font-size': str(annotation.get('font_size', 30))})
        label.text = annotation['label']
        if ink is not None and _visible_annotation(ink, annotation, region):
            label.set('data-mark-parts', annotation['part_id'])
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def _visible_annotation(ink, annotation, region):
    text = annotation['label']
    left, top, right, bottom = region['text_box']
    x, _ = annotation['at']
    face = regular_font(annotation.get('font_size', 30))
    bounds = [max(left, round(x + face.getlength(text[:i]))) for i in range(len(text))]
    for start, stop in zip(bounds, bounds[1:] + [right], strict=True):
        if stop <= start or not any(ink.crop((start, top, stop, bottom + 1)).histogram()[120:]):
            return False
    tx, ty = annotation['target']
    sx = left - 5 if tx < x else right + 5
    sy = (top + bottom) / 2
    checks = [(round(sx + (tx - sx) * k / 12), round(sy + (ty - sy) * k / 12)) for k in range(1, 12)]
    return sum(ink.crop((a - 2, b - 2, a + 3, b + 3)).getextrema()[1] >= 120
               for a, b in checks) >= 7


def compose_projection_labels(base, svg, visible_regions, parts, anchors):
    """Apply privately located numeric labels to visible projection ink."""
    if not isinstance(anchors, list) or not anchors or len(anchors) > 24:
        raise ValueError('reviewed projection labels required')
    visible = {r['instance_id']: r for r in visible_regions if r.get('ink_pixels', 0) > 0}
    annotations = []
    selected = set()
    with Image.open(io.BytesIO(base), formats=['PNG']) as opened:
        image = opened.convert('L')
    for anchor in anchors:
        if (not isinstance(anchor, dict) or set(anchor) not in (
                {'instance_id', 'at', 'target'}, {'instance_id', 'at', 'target', 'opening_gap'})):
            raise ValueError('invalid reviewed projection label')
        region = visible.get(anchor['instance_id'])
        part_id = region.get('part_id') if region else None
        label = parts.get(part_id)
        if not label or part_id in selected:
            raise ValueError('projection label must name one distinct visible part')
        at, target = anchor['at'], anchor['target']
        if any(not isinstance(pair, list) or len(pair) != 2
               or any(type(c) is not int for c in pair) for pair in (at, target)):
            raise ValueError('invalid projection anchor coordinates')
        x, y = target
        target_region = region['ink_bbox']
        if 'opening_gap' in anchor:
            gap = anchor['opening_gap']
            from .spatial_guide import box_valid, point_in_box

            if (region.get('role') != 'weld_opening' or not box_valid(gap, *image.size)
                    or not point_in_box(target, gap) or not point_in_box(target, target_region)
                    or not all(gap[i] + 3 <= target[i] < gap[i + 2] - 3 for i in (0, 1))
                    or image.crop(tuple(gap)).getextrema()[0] < 245):
                raise ValueError('opening label must end inside the reviewed clear gap')
            target_region = gap
        elif not (0 <= x < image.width and 0 <= y < image.height) or image.getpixel((x, y)) >= 128:
            raise ValueError('projection anchor must end on visible ink')
        annotations.append({'at': at, 'target': target, 'target_region': target_region,
                            'part_id': part_id, 'label': label, 'anchor_status': 'reviewed', 'font_size': 28})
        selected.add(part_id)
    result = compose(base, annotations)
    if any(image.crop(region['text_box']).getextrema()[0] < 245 for region in result['regions']):
        raise ValueError('projection label text overlaps existing ink')
    if not outside_regions_unchanged(base, result['png'], result['regions']):
        raise ValueError('projection geometry changed outside the label layer')
    return {'png': result['png'], 'svg': append_svg_annotations(svg, annotations, result['regions']),
            'part_ids': sorted(selected)}


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


def validate_beam_anchors(base, anchors, parts, geometry):
    """Tie new leaders to the reviewed pixel traces of this base."""
    roles = {'天线面板': 'panel', '角度范围': 'range', '发射波束方向': 'beam'}
    identified = {roles.get(parts[a['part_id']]['name']): a for a in anchors}
    if len(anchors) != 3 or set(identified) != {'panel', 'range', 'beam'}:
        raise ValueError('separate panel, range and beam anchors required')
    if len(annotation_targets(identified['range'])) != 2:
        raise ValueError('range needs separate leaders on both boundaries')
    if any(len(annotation_targets(identified[key])) != 1 for key in ('panel', 'beam')):
        raise ValueError('panel and beam each need one unambiguous anchor')

    with Image.open(io.BytesIO(base), formats=['PNG']) as raw:
        gray = raw.convert('L')
    panel, origin = geometry['panel'], geometry['origin']
    panel_corners = [[panel[0], panel[1]], [panel[2], panel[1]],
                     [panel[2], panel[3]], [panel[0], panel[3]], [panel[0], panel[1]]]
    paths = {'panel': [panel_corners],
             'range': [[origin, *([middle] if middle else []), end] for middle, end in zip(
                 geometry.get('boundary_midpoints', [None, None]), geometry['boundaries'])],
             'beam': [[origin, beam['tip']] for beam in geometry['beams']]}
    tolerance = 3
    geometry_paths = [(role, i, path) for role, segments in paths.items() for i, path in enumerate(segments)]

    def distance(point, a, b):
        dx, dy = b[0] - a[0], b[1] - a[1]
        fraction = max(0, min(1, ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / (dx*dx + dy*dy)))
        return math.hypot(point[0] - a[0] - fraction*dx, point[1] - a[1] - fraction*dy)

    for role, annotation in identified.items():
        matched = set()
        for target, region in annotation_targets(annotation):
            if (not isinstance(target, list) or len(target) != 2 or any(type(v) is not int for v in target)
                    or not isinstance(region, list) or len(region) != 4 or any(type(v) is not int for v in region)
                    or not all(0 <= region[i] <= target[i] < region[i + 2] <= gray.size[i] for i in (0, 1))):
                raise ValueError(role + ' anchor is outside the actual image or reviewed region')
            x, y = target
            ink = gray.crop((max(0, x-1), max(0, y-1), min(gray.width, x+2), min(gray.height, y+2)))
            candidates = [(kind, index) for kind, index, path in geometry_paths
                          if min(distance(target, a, b) for a, b in zip(path, path[1:])) <= tolerance]
            near_other = any(kind != role and min(distance(target, a, b) for a, b in zip(path, path[1:])) <= 8
                             for kind, _, path in geometry_paths)
            if ink.getextrema()[0] >= 128 or len(candidates) != 1 or candidates[0][0] != role or near_other:
                raise ValueError(role + ' anchor must terminate on its distinct reviewed stroke')
            matched.add(candidates[0][1])
        if role == 'range' and matched != {0, 1}:
            raise ValueError('range needs separate leaders on both boundaries')

    composed = compose(base, [{**a, 'label': parts[a['part_id']]['label']} for a in anchors])
    for annotation, region in zip(anchors, composed['regions'], strict=True):
        box = region['text_box']
        if gray.crop(tuple(box)).getextrema()[0] < 128:
            raise ValueError('reviewed beam annotation label overlaps the source geometry')
        for (tx, ty), _ in annotation_targets(annotation):
            x = box[0] - 5 if tx < annotation['at'][0] else box[2] + 5
            y = (box[1] + box[3]) / 2
            length = math.hypot(tx - x, ty - y)
            for step in range(0, max(0, round(length) - 10), 2):
                u = round(x + (tx - x) * step / length)
                v = round(y + (ty - y) * step / length)
                if 0 <= u < gray.width and 0 <= v < gray.height and gray.getpixel((u, v)) < 128:
                    raise ValueError('annotation leader crosses an unrelated source stroke')

"""Validated operator reference preparation, not reconstruction of hidden facts."""
import datetime
import hashlib
import io
from PIL import Image, ImageChops, ImageDraw
from .errors import DrawingError
from .images import prepare_reference, prepare_mask


def validate_preparation(prep: dict, source_record: dict, image: bytes, mask: bytes | None) -> dict:
    ref = prepare_reference(image)
    for key in ['source_sha256', 'source_region_id', 'base_sha256']:
        if prep.get(key) != source_record.get(key): raise ValueError('reference preparation source/base identity differs')
    if prep.get('base_sha256') != hashlib.sha256(image).hexdigest(): raise ValueError('reference base hash differs')
    if prep.get('method') not in {'crop', 'mask'} or prep.get('orientation') != 'normalized':
        raise ValueError('reference preparation method/orientation invalid')
    if prep.get('size') != [ref.width, ref.height]: raise ValueError('reference normalized dimensions differ')
    if not all(isinstance(prep.get(k), str) and prep[k].strip() for k in ['reviewer', 'source_note']):
        raise ValueError('reference preparation requires reviewer and source note')
    try:
        if datetime.datetime.fromisoformat(prep['reviewed_at']).tzinfo is None: raise ValueError('timezone required')
    except (KeyError, TypeError, ValueError): raise ValueError('reference preparation timestamp invalid') from None
    regions = prep.get('allowed_regions', [])
    if not isinstance(regions, list) or not 1 <= len(regions) <= 24: raise ValueError('allowed reference regions required')
    for r in regions:
        if (not isinstance(r, list) or len(r) != 4 or any(type(x) is not int for x in r)
                or not 0 <= r[0] < r[2] <= ref.width or not 0 <= r[1] < r[3] <= ref.height):
            raise ValueError('reference region outside normalized image')
    if prep['method'] == 'mask':
        if mask is None or prep.get('mask_sha256') != hashlib.sha256(mask).hexdigest():
            raise ValueError('reference mask identity differs')
        try: dispatched_mask = prepare_mask(mask, ref)
        except Exception as error: raise ValueError('invalid reference mask') from error
        with Image.open(io.BytesIO(mask)) as original:
            if original.getexif().get(274, 1) != 1:
                raise ValueError('reference mask orientation is not normalized')
        with Image.open(io.BytesIO(dispatched_mask.blob)) as im:
            alpha = im.convert('RGBA').getchannel('A')
            hist = alpha.histogram()
            if any(hist[1:-1]):
                raise ValueError('reviewed reference mask requires binary transparency')
            allowed = Image.new('1', alpha.size, 0)
            draw = ImageDraw.Draw(allowed)
            for x1, y1, x2, y2 in regions: draw.rectangle([x1, y1, x2-1, y2-1], fill=1)
            if any(a == 0 and not ok for a, ok in zip(alpha.tobytes(), allowed.convert('L').tobytes())):
                raise ValueError('transparent mask pixels outside reviewed regions')
    elif mask is not None: raise ValueError('crop preparation cannot carry a mask')
    if 'line_edit' in prep:
        if prep['method'] != 'mask':
            raise ValueError('精确补线需要已核蒙版。')
        base, editable = _edit_base(ref.blob, dispatched_mask.blob)
        _line_scope(base, editable, prep['line_edit'])
    result = dict(prep)
    if mask is not None: result['dispatch_mask_sha256'] = dispatched_mask.sha256
    return result


def compose_reviewed_mask(reference: bytes, mask: bytes, output: bytes) -> tuple[bytes, dict]:
    with (Image.open(io.BytesIO(reference)) as source,
          Image.open(io.BytesIO(mask)) as region,
          Image.open(io.BytesIO(output)) as generated):
        if source.size != generated.size or source.size != region.size:
            raise DrawingError('mask_alignment_mismatch', '模型图尺寸与已核蒙版不一致，原响应已保留，待核对图面。')
        mode = 'RGBA' if 'A' in source.getbands() else 'RGB'
        base = source.convert(mode)
        edited = generated.convert(mode)
        protected = region.convert('RGBA').getchannel('A')
        editable = ImageChops.invert(protected)
        merged = Image.composite(base, edited, protected)

    def changed(a, b, area):
        channels = ImageChops.difference(a, b).split()
        difference = channels[0]
        for channel in channels[1:]:
            difference = ImageChops.lighter(difference, channel)
        return sum(ImageChops.multiply(difference, area).histogram()[1:])

    report = {'raw_changed_outside': changed(base, edited, protected),
              'delivery_changed_outside': changed(base, merged, protected),
              'changed_inside': changed(base, merged, editable)}
    data = io.BytesIO()
    merged.save(data, format='PNG')
    return data.getvalue(), report


def _line_geometry(size, line):
    import math

    if not isinstance(line, dict) or set(line) != {'mode', 'endpoints', 'width'}:
        raise ValueError('精确补线需要模式、两个端点及线宽。')
    points = line['endpoints']
    width = line['width']
    if (line['mode'] not in {'local', 'supplier'} or type(width) is not int or not 1 <= width <= 32
            or not isinstance(points, list) or len(points) != 2
            or any(not isinstance(p, list) or len(p) != 2 or
                   any(type(v) is not int or not 0 <= v < n for v, n in zip(p, size)) for p in points)):
        raise ValueError('精确补线坐标或线宽未通过校验。')
    (x, y), (end_x, end_y) = points
    length = math.hypot(end_x - x, end_y - y)
    if length <= width * 2:
        raise ValueError('精确补线的端点间距不足以辨认两处接合。')
    return x, y, (end_x - x) / length, (end_y - y) / length, length, width


def _stroke_mask(size, geometry, width):
    import math

    x, y, dx, dy, length, _ = geometry
    mask = Image.new('L', size, 0)
    pixels = mask.load()
    end_x, end_y = x + dx * length, y + dy * length
    radius = width / 2
    bounds = [max(0, math.floor(min(x, end_x) - radius)),
              max(0, math.floor(min(y, end_y) - radius)),
              min(size[0], math.ceil(max(x, end_x) + radius) + 1),
              min(size[1], math.ceil(max(y, end_y) + radius) + 1)]
    for row in range(bounds[1], bounds[3]):
        for column in range(bounds[0], bounds[2]):
            along = (column - x) * dx + (row - y) * dy
            across = -(column - x) * dy + (row - y) * dx
            if -1e-8 <= along <= length + 1e-8 and -radius <= across < radius:
                pixels[column, row] = 255
    return mask


def _edit_base(reference, mask):
    with Image.open(io.BytesIO(reference)) as image:
        base = image.convert('RGBA' if 'A' in image.getbands() else 'RGB')
    with Image.open(io.BytesIO(mask)) as image:
        editable = ImageChops.invert(image.convert('RGBA').getchannel('A'))
    if base.size != editable.size:
        raise ValueError('精确补线的底图与蒙版尺寸不一致。')
    return base, editable


def _line_scope(base, editable, line):
    geometry = _line_geometry(base.size, line)
    nominal = _stroke_mask(base.size, geometry, line['width'])
    if ImageChops.subtract(nominal, editable).getbbox():
        raise ValueError('已核线段越出允许编辑范围。')
    luminance = _visible_luminance(base)
    if any(luminance.getpixel(tuple(point)) >= 128 for point in line['endpoints']):
        raise ValueError('底图上须能核对两个现有轮廓接合点。')
    return geometry, nominal


def _visible_luminance(image):
    if 'A' in image.getbands():
        white = Image.new('RGB', image.size, 'white')
        white.paste(image, mask=image.getchannel('A'))
        image = white
    return image.convert('L')


def _connected_stroke(ink, start):
    """Return the dark component connected by adjacent pixels, including diagonals."""
    connected = Image.new('L', ink.size)
    visited = connected.load()
    pixels = ink.load()
    pending = []
    if pixels[start]:
        pending.append(start)
        visited[start] = 255
    while pending:
        x, y = pending.pop()
        for row in range(max(0, y - 1), min(ink.height, y + 2)):
            for col in range(max(0, x - 1), min(ink.width, x + 2)):
                if pixels[col, row] and not visited[col, row]:
                    visited[col, row] = 255
                    pending.append((col, row))
    return connected


def inspect_precise_line(reference, mask, output, line):
    """Check the saved candidate against the source pixels and reviewed contacts."""
    from statistics import median

    base, editable = _edit_base(reference, mask)
    geometry, _ = _line_scope(base, editable, line)
    with Image.open(io.BytesIO(output)) as image:
        candidate = image.convert(base.mode)
    if candidate.size != base.size:
        return {'verdict': 'FAIL', 'issues': ['alignment']}
    x, y, dx, dy, length, width = geometry
    tolerance = max(1, width * 0.25)
    maximum = int(width + tolerance)
    corridor = _stroke_mask(base.size, geometry, maximum)
    difference_channels = ImageChops.difference(base, candidate).split()
    difference = difference_channels[0]
    for channel in difference_channels[1:]:
        difference = ImageChops.lighter(difference, channel)
    outside = ImageChops.multiply(difference, ImageChops.invert(editable))
    unexpected = ImageChops.multiply(difference, ImageChops.invert(corridor))
    prior_dark = _visible_luminance(base).point(lambda n: 255 if n < 128 else 0)
    dark = _visible_luminance(candidate).point(lambda n: 255 if n < 128 else 0)
    erased = ImageChops.subtract(prior_dark, dark)
    ink = ImageChops.multiply(dark, corridor)
    connected = _connected_stroke(ink, tuple(line['endpoints'][0]))
    detached = ImageChops.multiply(ImageChops.subtract(ink, connected), ImageChops.subtract(dark, prior_dark))

    # Follow the approved segment at one-pixel intervals in its own coordinates.
    gaps = 0
    spans = []
    for step in range(round(length) + 1):
        along = min(step, length)
        cx, cy = x + dx * along, y + dy * along
        if not dark.getpixel((round(cx), round(cy))):
            gaps += 1
        if width < along < length - width:
            samples = {(round(cx - dy * k), round(cy + dx * k))
                       for k in range(-maximum - 1, maximum + 1)}
            spans.append(sum(bool(dark.getpixel(point)) for point in samples
                             if 0 <= point[0] < dark.width and 0 <= point[1] < dark.height))
    typical = median(spans) if spans else 0
    issues = []
    if outside.getbbox():
        issues.append('outside_mask')
    if unexpected.getbbox():
        issues.append('outside_line')
    if erased.getbbox():
        issues.append('erased_source_ink')
    if detached.getbbox():
        issues.append('detached_ink')
    if gaps or not connected.getpixel(tuple(line['endpoints'][1])):
        issues.append('line_gap')
    if not width - tolerance <= typical <= width + tolerance:
        issues.append('line_width')
    return {'verdict': 'FAIL' if issues else 'PASS', 'issues': issues, 'median_width': typical,
            'missing_center_samples': gaps, 'outside_mask': sum(outside.histogram()[1:]),
            'outside_line': sum(unexpected.histogram()[1:])}


def render_precise_line(reference, mask, line):
    base, editable = _edit_base(reference, mask)
    _, stroke = _line_scope(base, editable, line)
    base.paste((0, 0, 0, 255) if base.mode == 'RGBA' else (0, 0, 0), mask=stroke)
    data = io.BytesIO()
    base.save(data, format='PNG')
    result = data.getvalue()
    return result, inspect_precise_line(reference, mask, result, line)

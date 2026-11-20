"""Validate annotated traces on a material relationship drawing's actual base."""

from itertools import product

from PIL import ImageDraw

from .annotations import png, regular_font
from .diagrams import _orientation, _overlap
from .spatial_brief import MATERIAL_CONTACT, MATERIAL_HEAT_PATH, MATERIAL_THICKNESS


def _in_region(point, box):
    return (isinstance(point, list) and len(point) == 2
            and all(type(value) is int and box[i] <= value < box[i + 2]
                    for i, value in enumerate(point)))


def _crosses_interior(a, b, c, d):
    return _orientation(a, b, c) * _orientation(a, b, d) < 0 and _orientation(c, d, a) * _orientation(c, d, b) < 0


def _connected_ink(gray, start, end, tolerance, *, endpoint_tolerance=None):
    """Follow pixels in a narrow corridor so nearby disconnected marks cannot pass."""
    x0, y0 = start
    x1, y1 = end
    dx, dy = x1-x0, y1-y0
    radius_sq = tolerance**2 * (dx*dx + dy*dy)
    pixels = gray.load()
    left = max(0, min(x0, x1) - tolerance)
    right = min(gray.width-1, max(x0, x1) + tolerance)
    top = max(0, min(y0, y1) - tolerance)
    bottom = min(gray.height-1, max(y0, y1) + tolerance)

    def ink(x, y):
        return (left <= x <= right and top <= y <= bottom
                and pixels[x, y] < 128 and (dx*(y-y0)-dy*(x-x0))**2 <= radius_sq)

    endpoint_tolerance = tolerance if endpoint_tolerance is None else endpoint_tolerance
    offsets = range(-endpoint_tolerance, endpoint_tolerance+1)
    pending = [(x0+x, y0+y) for x, y in product(offsets, repeat=2) if ink(x0+x, y0+y)]
    seen = set(pending)
    while pending:
        x, y = pending.pop()
        if abs(x-x1) <= endpoint_tolerance and abs(y-y1) <= endpoint_tolerance:
            return True
        for nx, ny in product(range(x-1, x+2), range(y-1, y+2)):
            if (nx, ny) not in seen and ink(nx, ny):
                seen.add((nx, ny))
                pending.append((nx, ny))
    return False


def _contact_segments(contact, box, tolerance):
    if isinstance(contact, dict) and contact.keys() == {'upper', 'lower'}:
        segments = [contact['upper'], contact['lower']]
    else:
        segments = [contact]
    if any(not isinstance(segment, list) or len(segment) != 2
           or not all(_in_region(point, box) for point in segment)
           or abs(segment[0][1] - segment[1][1]) < 6*tolerance
           for segment in segments):
        return None
    if len(segments) == 2:
        upper, lower = segments
        if not upper[0][1] < upper[1][1] < lower[0][1] < lower[1][1]:
            return None
    return segments


def _crosses_contact(start, tip, segments, gray, tolerance):
    if len(segments) == 1:
        return _crosses_interior(start, tip, *segments[0])
    upper, lower = segments
    top, bottom = upper[1], lower[0]
    if (bottom[1] - top[1] > 3*tolerance or abs(bottom[0] - top[0]) > 4*tolerance
            or not _crosses_interior(start, tip, top, bottom)):
        return False
    if gray is None:
        return True
    # This point is on the observed shaft between the visible branch ends.
    # It tests local ink connectivity without tracing the hidden seam.
    above = _orientation(start, tip, top)
    below = _orientation(start, tip, bottom)
    fraction = above / (above - below)
    shaft = [round(first + fraction*(last-first)) for first, last in zip(top, bottom)]
    return all(_connected_ink(gray, point, shaft, tolerance, endpoint_tolerance=0)
               for point in (top, bottom))


def material_heat_issue(rows, size, image):
    """Check the submitted drawing coordinates and, on receipt, the underlying ink."""
    indexed = {row['feature_id']: row for row in rows}
    network, heat, directions = (indexed[feature] for feature in
                                 (MATERIAL_CONTACT, MATERIAL_HEAT_PATH, MATERIAL_THICKNESS))
    observations = [row for row in (network, heat, directions) if row['verdict'] == 'PASS']
    if not observations:
        return None
    if not size or any(row['region'][2] > size[0] or row['region'][3] > size[1] for row in observations):
        return observations[0]['feature_id'], '需要实际底图尺寸及图内观察区'
    tolerance = max(2, min(6, round(min(size) / 240)))
    gray = image.convert('L') if image is not None else None

    def has_ink(start, end):
        return gray is None or _connected_ink(gray, start, end, tolerance)

    contacts = []
    if network['verdict'] == 'PASS':
        box = network['region']
        traces = network.get('contacts')
        if not isinstance(traces, list) or len(traces) != 2:
            return MATERIAL_CONTACT, '需在鳞片网络区内记录左右两处彼此分开的实际接合线段'
        contacts = [_contact_segments(trace, box, tolerance) for trace in traces]
        if (not all(contacts)
                or max(p[0] for s in contacts[0] for p in s) + 2*tolerance >= min(p[0] for s in contacts[1] for p in s)):
            return MATERIAL_CONTACT, '需在鳞片网络区内记录左右两处彼此分开的实际接合线段'
        if not all(has_ink(*segment) for trace in contacts for segment in trace):
            return MATERIAL_CONTACT, '记录的接合线段在实际底图上不连续'

    if heat['verdict'] == 'PASS':
        trace = heat.get('heat_path')
        if network['verdict'] != 'PASS' or not isinstance(trace, dict) or set(trace) != {'origin', 'arrows'}:
            return MATERIAL_HEAT_PATH, '需先核对接合线，再记录同一起点及左右箭线'
        origin, arrows = trace['origin'], trace['arrows']
        boxes = (network['region'], heat['region'])

        def in_both(point):
            return all(_in_region(point, box) for box in boxes)

        if (not in_both(origin) or not isinstance(arrows, list) or len(arrows) != 2
                or any(not isinstance(arrow, dict) or set(arrow) != {'tip', 'head'} or not in_both(arrow['tip'])
                       or not isinstance(arrow['head'], list) or len(arrow['head']) != 2
                       or not all(in_both(point) for point in arrow['head']) for arrow in arrows)):
            return MATERIAL_HEAT_PATH, '箭线、起点及箭尖须在同一实际鳞片网络区内'
        for sign, arrow, seam in zip((-1, 1), arrows, contacts):
            tip, wings = arrow['tip'], arrow['head']
            run = sign * (tip[0] - origin[0])
            if (run < 8*tolerance or abs(tip[1] - origin[1]) > 2*tolerance + run/16
                    or not _crosses_contact(origin, tip, seam, gray, tolerance)):
                return MATERIAL_HEAT_PATH, '左右箭线必须由同一点发出并各自穿过一处接合线'
            if (not all(2*tolerance <= sign*(tip[0] - wing[0]) < run for wing in wings)
                    or _orientation(origin, tip, wings[0]) * _orientation(origin, tip, wings[1]) >= 0
                    or not all(has_ink(tip, wing) for wing in wings)
                    or not has_ink(origin, tip)):
                return MATERIAL_HEAT_PATH, '底图未显示贯穿接合位置的连续箭线与真实箭尖'

    if directions['verdict'] == 'PASS':
        box = directions['region']
        axes = directions.get('direction_axes')
        fields = {'origin', 'plane_tip', 'thickness_tip'}
        if (not isinstance(axes, dict) or axes.keys() != fields
                or not all(_in_region(axes[key], box) for key in fields)
                or (network['verdict'] == 'PASS' and _overlap(box, network['region']))):
            return MATERIAL_THICKNESS, '膜面与厚度方向须标在鳞片网络外的独立区域'
        x, y = axes['origin']
        px, py = axes['plane_tip']
        tx, ty = axes['thickness_tip']
        if (abs(px-x) < 6*tolerance or abs(ty-y) < 6*tolerance
                or abs(py-y) > tolerance or abs(tx-x) > tolerance
                or not all(has_ink(axes['origin'], axes[key]) for key in ('plane_tip', 'thickness_tip'))):
            return MATERIAL_THICKNESS, '实际底图须有横向膜面轴和与其正交的厚度轴'
    return None


def label_material_axes(image, directions, network_region):
    """Place fixed axis names beside the independently reviewed traces."""
    positions = directions.get('axis_label_positions')
    texts = {'plane': '膜面方向', 'thickness': '厚度方向'}
    if (not isinstance(positions, dict) or positions.keys() != texts.keys()
            or not all(_in_region(point, [0, 0, *image.size]) for point in positions.values())):
        raise ValueError('two reviewed axis label positions required')
    axes = directions['direction_axes']
    x, y = axes['origin']
    px, py = axes['plane_tip']
    tx, ty = axes['thickness_tip']
    size = max(20, min(42, round(image.width / 40)))
    font = regular_font(size)
    drawn = image.convert('RGB')
    gray = drawn.convert('L')
    draw = ImageDraw.Draw(drawn)
    labels = {}
    for role, label in texts.items():
        at = positions[role]
        box = draw.textbbox(at, label, font=font, anchor='lt')
        left, top, right, bottom = box
        if role == 'plane':
            gap = max(top - max(y, py), min(y, py) - bottom)
            along = min(x, px) <= left < right <= max(x, px)
        else:
            gap = max(left - max(x, tx), min(x, tx) - right)
            along = min(y, ty) <= top < bottom <= max(y, ty)
        if (not along or not 2 <= gap <= 2*size or right > image.width or bottom > image.height
                or left < 0 or top < 0 or (network_region and _overlap(box, network_region))
                or any(_overlap(box, prior['box']) for prior in labels.values())
                or gray.crop(box).getextrema()[0] < 245):
            raise ValueError('axis label must fit in unmarked space beside its own reviewed axis')
        draw.text(at, label, font=font, anchor='lt', fill='black')
        labels[role] = {'text': label, 'box': list(box)}
    return png(drawn), labels

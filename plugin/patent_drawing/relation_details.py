"""Render optional, source-bound relationship schematics for engineering export."""
from io import BytesIO
from xml.etree import ElementTree as ET

from PIL import Image

from .annotations import png, regular_font
from .diagrams import render_diagram


_NS = '{http://www.w3.org/2000/svg}'
_TITLES = {
    'rib_in_plated_hole': '销孔接触关系示意',
    'cable_weld_access': '焊接开口关系示意',
}
_NOTES = {
    'rib_in_plated_hole': '加粗线表示肋与孔壁的共界。肋的外形、孔壁带宽、过盈及变形待核。',
    'cable_weld_access': '箭头表示开口通向屏蔽层的关系。槽壁、开口外形和实际焊接位置待核。',
}


def _one(rows, kind):
    matched = [row for row in rows if row['kind'] == kind]
    if len(matched) != 1:
        raise ValueError('one source-bound relation required for ' + kind)
    return matched[0]


def _fit():
    lines = [
        (3, [(405, 68), (405, 446), (555, 446), (555, 318),
             (665, 295), (665, 225), (555, 202), (555, 68)]),
        (3, [(285, 165), (325, 165), (325, 355), (285, 355)]),
        (3, [(705, 165), (665, 165), (665, 225)]),
        (3, [(665, 295), (665, 355), (705, 355)]),
        (5, [(665, 225), (665, 295)]),
    ]
    for left in (135, 705):
        right = left + 150
        lines.append((3, [(left, 165), (right, 165), (right, 355), (left, 355), (left, 165)]))
        for offset in range(-50, 190, 20):
            x = left + offset
            lo, hi = max(left, x), min(right, x + 105)
            if lo < hi:
                lines.append((2, [(lo, 165 + x + 105 - lo), (hi, 165 + x + 105 - hi)]))
    labels = [
        ((448, 27), [(449, 63), (405, 112)]),
        ((588, 369), [(605, 366), (619, 305)]),
        ((753, 93), [(748, 118), (688, 165)]),
        ((100, 94), [(137, 115), (188, 165)]),
    ]
    return lines, labels


def _opening():
    lines = [
        (3, [(105, 211), (333, 211)]),
        (3, [(105, 319), (333, 319)]),
        (3, [(333, 211), (333, 319), (677, 319), (677, 211), (333, 211)]),
        (3, [(333, 319), (735, 319), (735, 347), (333, 347), (333, 319)]),
        (3, [(333, 211), (333, 183), (480, 183), (480, 211)]),
        (3, [(605, 211), (605, 183), (735, 183), (735, 211), (605, 211)]),
        (2, [(535, 185), (545, 199), (555, 185)]),
    ]
    lines.extend((2, [(545, y), (545, y+12)]) for y in (112, 135, 158, 181))
    labels = [
        ((173, 133), [(190, 170), (238, 211)]),
        ((752, 247), [(748, 268), (677, 268)]),
        ((758, 370), [(752, 375), (692, 347)]),
        ((428, 78), [(463, 103), (511, 195)]),
    ]
    return lines, labels


def _bindings(scene, kind):
    rel = _one(scene['relations'], kind)
    instances = {row['id']: row for row in scene['instances']}
    if kind == 'rib_in_plated_hole':
        if len(rel['ribs']) != 1:
            raise ValueError('fit schematic requires one represented rib')
        ids = [rel['pin'], rel['ribs'][0], rel['hole'], rel['board']]
        facts = rel['fact_ids']
    else:
        shield = _one(scene['relations'], 'trough_shield_contact')
        if rel['target'] != shield['shield'] or set(rel['troughs']) != set(shield['troughs']):
            raise ValueError('opening schematic requires the source-bound shield and its own trough')
        trough = {instances[i]['part_id'] for i in rel['troughs']}
        if len(trough) != 1:
            raise ValueError('opening schematic requires one represented trough')
        ids = [shield['cable'], shield['shield'], rel['troughs'][0], rel['opening']]
        facts = rel['fact_ids'] + shield['fact_ids']
    return [instances[i]['part_id'] for i in ids], sorted(set(facts))


def _attach(projection, panel):
    with Image.open(BytesIO(projection['png'])) as base, Image.open(BytesIO(panel['png'])) as detail:
        width, height = base.size
        dw = min(width - 40, 1200)
        dh = round(dw * detail.height / detail.width)
        x, y = (width - dw) // 2, height + 18
        canvas = Image.new('RGB', (width, y + dh + 14), 'white')
        canvas.paste(base.convert('RGB'))
        canvas.paste(detail.resize((dw, dh), Image.Resampling.LANCZOS), (x, y))
    root = ET.Element(_NS+'svg', {'width': str(canvas.width), 'height': str(canvas.height),
                                 'viewBox': f'0 0 {canvas.width} {canvas.height}'})
    ET.SubElement(root, _NS+'rect', {'width': '100%', 'height': '100%', 'fill': 'white'})
    root.append(ET.fromstring(projection['svg']))
    svg = ET.fromstring(panel['svg'])
    svg.attrib.update(x=str(x), y=str(y), width=str(dw), height=str(dh))
    root.append(svg)
    return png(canvas), ET.tostring(root, encoding='utf-8', xml_declaration=True)


def add_relation_detail(projection, scene, parts, kind):
    """Use a validated scene and its stable part numbers, preserving the full projection."""
    if not isinstance(kind, str) or kind not in _TITLES:
        raise ValueError('unsupported structural detail kind')
    part_ids, fact_ids = _bindings(scene, kind)
    if len(set(part_ids)) != len(part_ids) or not set(part_ids) <= parts.keys():
        raise ValueError('detail must bind distinct parts of the approved view')
    lines, labels = _fit() if kind == 'rib_in_plated_hole' else _opening()
    primitives = []
    for part_id, (at, leader) in zip(part_ids, labels, strict=True):
        value = parts[part_id]
        if regular_font(30).getlength(value) > 58:
            raise ValueError('part label too wide for the relation schematic')
        primitives.append({'type': 'text', 'at': list(at), 'text': value, 'font_size': 30})
        lines.append((2, leader))
    primitives.extend({'type': 'polyline', 'points': [list(p) for p in points], 'stroke_width': width}
                      for width, points in lines)
    primitives.extend([
        {'type': 'text', 'at': [500, 478], 'text': _TITLES[kind] + '（无比例）', 'anchor': 'middle', 'font_size': 31},
        {'type': 'text', 'at': [500, 529], 'text': _NOTES[kind], 'anchor': 'middle', 'font_size': 23},
    ])
    panel = render_diagram({'parts': []}, {'parts': [],
        'diagram': {'width': 1000, 'height': 600, 'primitives': primitives}}, font_factory=regular_font)
    image, svg = _attach(projection, panel)
    return {'png': image, 'svg': svg, 'part_ids': sorted(set(projection['part_ids']) | set(part_ids)),
            'relation_detail': {'kind': kind, 'fact_ids': fact_ids, 'part_ids': part_ids, 'scale': 'symbolic'}}

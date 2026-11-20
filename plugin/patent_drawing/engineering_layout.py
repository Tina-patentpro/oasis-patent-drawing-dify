"""Reflow a reviewed support relation and a source crop for an engineering page."""
from io import BytesIO

from PIL import Image, ImageDraw

from .annotations import png, regular_font


def reflow_support_reference(source, spec, labels, *, support_label=None):
    supports = spec['supports']
    if [item['marker'] for item in supports] != list('ABCD'):
        raise ValueError('reviewed four-support relation is required')
    upper = supports[0]['upper']['owner_part_id']
    lower = supports[0]['lower']['owner_part_id']
    for item in supports:
        if item['upper']['owner_part_id'] != upper or item['lower']['owner_part_id'] != lower:
            raise ValueError('reviewed supports must share the same plates')
    if any(supports[-1][side]['basis'] != 'source_relation' for side in ('upper', 'lower')):
        raise ValueError('fourth support needs the reviewed relation-only basis')

    canvas = Image.new('RGB', (1400, 1860 if support_label else 1550), 'white')
    draw = ImageDraw.Draw(canvas)
    face = regular_font(58)
    if support_label:
        draw.text((80, 870), f'{support_label} 弓头弹簧组件 A–D', font=face, fill='black')
    draw.text((80, 66), f'公共上板 {labels[upper]}', font=face, fill='black')
    draw.text((80, 755), f'托板 {labels[lower]}', font=face, fill='black')
    for y in (194, 704):
        draw.line((85, y, 1315, y), fill='black', width=7)
    for index, item in enumerate(supports):
        x = 235 + 310 * index
        for start, end in ((194, 390), (508, 704)):
            if item['marker'] == 'D':
                for y in range(start, end, 37):
                    draw.line((x, y, x, min(end, y + 21)), fill='black', width=7)
            else:
                draw.line((x, start, x, end), fill='black', width=7)
        for y in (194, 704):
            draw.ellipse((x - 13, y - 13, x + 13, y + 13), fill='white', outline='black', width=6)
        draw.text((x, 452), item['marker'], anchor='mm', font=face, fill='black')

    plate = spec['vertical_plate']
    box = plate['source_box']
    with Image.open(BytesIO(source)) as image:
        if not (0 <= box[0] < box[2] <= image.width and 0 <= box[1] < box[3] <= image.height):
            raise ValueError('reviewed plate crop falls outside the source')
        crop = image.convert('RGB').crop(box)
        crop.thumbnail((1100, 500), Image.Resampling.LANCZOS)
        scale = min(1100 / crop.width, 500 / crop.height)
        crop = crop.resize((round(crop.width * scale), round(crop.height * scale)), Image.Resampling.NEAREST)
    lower_shift = 130 if support_label else 0
    draw.text((80, 925 + lower_shift), f'竖板 {labels[plate["part_id"]]}（原图局部）', font=face, fill='black')
    canvas.paste(crop, ((canvas.width - crop.width) // 2, 1040 + lower_shift))
    if support_label:
        with Image.open(BytesIO(source)) as original:
            overview = original.convert('RGB')
            original_width, original_height = overview.size
            overview.thumbnail((390, 150), Image.Resampling.LANCZOS)
            canvas.paste(overview, (90, 1690))
            for row in supports[:3]:
                left, top, right, bottom = row['upper']['source_box']
                sx = 90 + ((left + right) / 2) * overview.width / original_width
                sy = 1690 + ((top + bottom) / 2) * overview.height / original_height
                draw.ellipse((sx - 5, sy - 5, sx + 5, sy + 5), outline='black', width=2)
                draw.text((sx + 7, sy - 25), row['marker'], font=regular_font(26),
                          fill='black', stroke_width=2, stroke_fill='white')
            draw.text((520, 1720), 'A、B、C 对应原图所标三处支承', font=regular_font(40), fill='black')
            draw.text((520, 1780), 'D 仅示意共同支承关系', font=regular_font(40), fill='black')
    return png(canvas)


SUPPORT_ROLES = {
    'upper': '滑板安装板', 'spring': '弓头弹簧组件', 'lower': '托板', 'vertical': '竖板',
}
SUPPORT_VIEW = 'support_relation'
_SUPPORT_TITLE = '四处支承关系示意图'
_SUPPORT_PURPOSE = ('示意四处支承上下端与滑板安装板、托板的共同关系。'
                    'A、B、C 对照原图可见局部，D 为正文支持的定性关系。'
                    '第四处实体接口、空间位置及弹簧连接待核。')


def support_fact(bundle, plan):
    """Bind the disclosed common support to one positive, located statement."""
    import re

    from .source_contract import compact, validate_facts

    if bundle.get('figure_presence') != 'present' or 'mechanical' not in plan.get('technical_types', []):
        return None
    pattern = re.compile(r'四(?:个|处)弓头弹簧组件[^。；]{0,80}滑板安装板[^。；]{0,75}(?:支承|支撑)在托板上')
    facts = [fact for fact in plan.get('facts', [])
             if fact.get('status') == 'confirmed' and any(
                 pattern.search(compact(ref.get('quote', ''))) for ref in fact.get('evidence', []))
             and not validate_facts(bundle, [fact])]
    return facts[0] if facts else None


def support_roles(plan):
    return {role: next((part['id'] for part in plan['parts'] if part['name'] == name), None)
            for role, name in SUPPORT_ROLES.items()}


def _originals(bundle, plan):
    regions = {row['id']: row for row in bundle.get('figure_candidates', []) if row['kind'] == 'image'}
    views = [v for v in plan['views'] if v['engine'] == 'source' and v.get('source_region_id') in regions]
    overall = [v for v in views if v['delivered_type'] in {'assembly', 'spatial', 'structure'}]
    section = [v for v in views if v['delivered_type'] == 'section' and v.get('source_section')]
    if len(overall) == len(section) == 1 and overall[0]['source_region_id'] != section[0]['source_region_id']:
        return overall[0], section[0]
    return None


def bind_support_relation(bundle, plan):
    """Require a local relationship page alongside the two located original images."""
    import re

    fact = support_fact(bundle, plan)
    originals = _originals(bundle, plan) if fact else None
    roles = support_roles(plan) if originals else {}
    if not originals or not all(roles.values()) or any(v['id'] == SUPPORT_VIEW for v in plan['views']):
        return None
    overall, section = originals
    for view in originals:
        view['source_extraction'] = 'intrinsic_bitmap'
    relation = {
        'id': SUPPORT_VIEW, 'engine': SUPPORT_VIEW, 'requested_type': 'relationship',
        'delivered_type': 'relationship', 'title': '图3 ' + _SUPPORT_TITLE,
        'purpose': _SUPPORT_PURPOSE, 'instruction': '用经核的原图局部表达共同支承关系。',
        'parts': list(roles.values()), 'depends_on': [], 'required_facts': [fact['id']],
        'source_region_id': overall['source_region_id'],
    }
    extras = [v for v in plan['views'] if v not in originals]
    plan['views'] = [*originals, relation, *extras]
    for i, view in enumerate(plan['views'], 1):
        if re.match(r'^图\s*\d+', view['title']):
            view['title'] = re.sub(r'^图\s*\d+', f'图{i}', view['title'])
    return {'view_id': SUPPORT_VIEW, 'figure_number': 3, 'source_region_id': relation['source_region_id'],
            'fact_id': fact['id']}


def support_relation_gaps(bundle, plan):
    from .source_contract import compact, issue

    fact = support_fact(bundle, plan)
    originals = _originals(bundle, plan) if fact else None
    views = plan['views']
    # Read historic staged engineering guides under their original pipeline.
    legacy = (len(views) >= 3 and views[2].get('engine') == 'image'
              and views[2].get('expression_mode') == 'relational_schematic')
    if legacy and originals and views[2].get('source_region_id') == originals[0]['source_region_id']:
        return []
    if not fact and not any(v['engine'] == SUPPORT_VIEW for v in views):
        # Keep a missing planner citation visible when the words are clearly
        # present on an original page. No physical interfaces are inferred.
        if (bundle.get('figure_presence') != 'present' or 'mechanical' not in plan.get('technical_types', [])
                or not any('四个弓头弹簧组件' in compact(p['text'])
                           and '支承在托板上' in compact(p['text']) for p in bundle['pages'])):
            return []
    if originals and len(views) >= 3 and list(originals) == views[:2]:
        view = views[2]
        if (view['engine'] == SUPPORT_VIEW and view['id'] == SUPPORT_VIEW
                and view.get('source_region_id') == originals[0]['source_region_id']
                and all(v.get('source_extraction') == 'intrinsic_bitmap' for v in originals)
                and view.get('parts') == list(support_roles(plan).values())
                and view.get('required_facts') == [fact['id']]
                and sum(v['engine'] == SUPPORT_VIEW for v in views) == 1):
            return []
    return [issue('SUPPORT-RELATION-VIEW', 'FAIL', '图集需保留原总图和剖视，并将有出处的八端点支承关系安排为图3。')]


def build_support_layout(source, spec, roles):
    """Use integer-scale source crops alongside a qualitative eight-point relation."""
    import hashlib

    from .spatial_guide import box_valid, point_in_box

    if (not isinstance(spec, dict) or set(spec) != {'source_png_sha256', 'windows', 'detail'}
            or hashlib.sha256(source).hexdigest() != spec['source_png_sha256']
            or not isinstance(spec['windows'], list) or len(spec['windows']) != 3
            or not all(roles.values()) or len(set(roles.values())) != 4):
        raise ValueError('exact reviewed intrinsic source and three support windows required')
    with Image.open(BytesIO(source), formats=['PNG']) as opened:
        original = opened.convert('RGB')
    image = Image.new('RGB', (1400, 1580), 'white')
    draw = ImageDraw.Draw(image)
    face = regular_font(67)
    annotation = []
    endpoints = []
    crops = []

    def mark(role, at, target, *, leader_width=4):
        x, y = target
        annotation.append({'part_id': roles[role], 'at': list(at), 'target': [x, y],
                           'target_region': [x - 3, y - 3, x + 3, y + 3],
                           'font_size': 61, 'leader_width': leader_width, 'anchor_status': 'reviewed'})

    gray = original.convert('L')

    def source_ink(point, box):
        if (not isinstance(point, list) or len(point) != 2 or any(type(x) is not int for x in point)
                or not point_in_box(point, box) or gray.getpixel(tuple(point)) >= 128):
            raise ValueError('reviewed support/detail endpoint must meet actual source ink')

    def continue_spring_leader(spring, placement, mapped):
        # The original dot and leader stay in C. Only extend the exiting stroke
        # in the margin, keeping the numeric label on its normal edit layer.
        if set(spring) != {'source_point', 'crop_exit'}:
            raise ValueError('reviewed spring dot and crop exit required')
        dot, exit_pixel = spring['source_point'], spring['crop_exit']
        placed_point, placed_exit = mapped(dot), mapped(exit_pixel)
        _, top, right, bottom = placement['source_box']
        if exit_pixel[0] != right - 1 or dot[0] >= exit_pixel[0] or exit_pixel[1] >= dot[1]:
            raise ValueError('reviewed spring leader must rise to the right edge of C')
        slope = (exit_pixel[1] - dot[1]) / (exit_pixel[0] - dot[0])
        for x in range(dot[0], right):
            y = round(dot[1] + (x - dot[0]) * slope)
            if gray.crop((x, max(top, y - 1), x + 1, min(bottom, y + 2))).getextrema()[0] >= 128:
                raise ValueError('reviewed spring needs a continuous source leader')
        border = placement['placed_box'][2]
        join_y = round(placed_exit[1] + (border - placed_exit[0]) * slope)
        mark('spring', (border + 23, join_y + round(18 * slope) - 23), (border, join_y), leader_width=2)
        annotation[-1]['source_leader'] = {'marker': 'C', 'source_point': dot, 'crop_exit': exit_pixel,
                                            'placed_point': placed_point, 'placed_exit': placed_exit}

    def continue_lower_leader(lower, placement, mapped):
        error = 'C lower leader'
        keys = {'source_point', 'crop_exit', 'source_elbow', 'source_label', 'source_label_box',
                'source_label_pixels_sha256'}
        if not isinstance(lower, dict) or set(lower) != keys:
            raise ValueError(f'{error} needs the reviewed source path and mark')
        dot, exit_pixel, elbow = (lower[key] for key in ('source_point', 'crop_exit', 'source_elbow'))
        label_box, source_label = lower['source_label_box'], lower['source_label']
        source_box = placement['source_box']

        def source_point(point):
            return (isinstance(point, list) and len(point) == 2 and all(type(v) is int for v in point)
                    and 0 <= point[0] < gray.width and 0 <= point[1] < gray.height)

        if (not all(map(source_point, (dot, exit_pixel, elbow))) or not box_valid(label_box, *original.size)
                or not isinstance(source_label, str) or not 1 <= len(source_label) <= 3
                or not source_label.isascii() or not source_label.isdecimal()
                or not point_in_box(dot, source_box) or not point_in_box(exit_pixel, source_box)
                or exit_pixel[0] != source_box[2] - 1
                or not (dot[0] < exit_pixel[0] < elbow[0] < label_box[0]
                        and dot[1] < exit_pixel[1] < elbow[1])
                or not 0 <= elbow[1] - label_box[3] <= label_box[3] - label_box[1]):
            raise ValueError(f'{error} must extend down and right from the source dot to the marked rule')

        label_pixels = original.crop(label_box)
        if (hashlib.sha256(label_pixels.tobytes()).hexdigest() != lower['source_label_pixels_sha256']
                or label_pixels.convert('L').getextrema()[0] >= 128):
            raise ValueError(f'{error} cannot locate the reviewed numeric mark in the source')
        x, y = dot
        if sum(n < 128 for n in gray.crop((x - 1, y - 1, x + 2, y + 2)).getdata()) < 5:
            raise ValueError(f'{error} lacks the reviewed source dot')

        def continuous(first, last, *, clear_background=False):
            x0, y0 = first
            x1, y1 = last
            obscured_run = 0
            for x in range(x0, x1 + 1):
                y = round(y0 + (x - x0) * (y1 - y0) / (x1 - x0))
                if gray.crop((x, max(0, y - 1), x + 1, min(gray.height, y + 2))).getextrema()[0] >= 128:
                    return False
                # Beyond C, a line covered by solid ink cannot substantiate its direction.
                if clear_background:
                    band = gray.crop((x, max(0, y - 3), x + 1, min(gray.height, y + 4)))
                    obscured_run = obscured_run + 1 if band.getextrema()[1] < 128 else 0
                    if obscured_run >= 3:
                        return False
            return True

        if not (continuous(dot, exit_pixel) and continuous(exit_pixel, elbow, clear_background=True)
                and continuous(elbow, ((label_box[0] + label_box[2]) // 2, elbow[1]), clear_background=True)):
            raise ValueError(f'{error} lacks a clear, continuous path in the source')
        try:
            placed_point, placed_exit = mapped(dot), mapped(exit_pixel)
        except ValueError as exc:
            raise ValueError(f'{error} dot and crop exit must meet source ink') from exc

        slope = (elbow[1] - dot[1]) / (elbow[0] - dot[0])
        border = placement['placed_box'][2]
        join_y = round(placed_exit[1] + (border - placed_exit[0]) * slope)
        mid_y = join_y + round(18 * slope)
        at = (border + 23, mid_y - 23)
        text_box = draw.textbbox(at, '000', font=regular_font(61), anchor='lt')
        if text_box[2] > image.width or text_box[3] >= 1120:
            raise ValueError(f'{error} needs a clear margin outside the source windows')
        mark('lower', at, (border, join_y), leader_width=2)
        annotation[-1]['source_leader'] = {
            **lower, 'marker': 'C', 'placed_point': placed_point, 'placed_exit': placed_exit,
            'extension_region': [border, join_y - 2, at[0], mid_y + 3], 'label_at': list(at),
        }

    def crop(row, area_left, area_top, available_width, available_height, marker):
        box = row['source_box']
        if not box_valid(box, *original.size):
            raise ValueError('reviewed crop falls outside the source')
        local = original.crop(tuple(box))
        scale = min(available_width // local.width, available_height // local.height)
        if scale < 2:
            raise ValueError('reviewed crop is too wide to retain enlarged source pixels')
        size = (local.width * scale, local.height * scale)
        x = area_left + (available_width - size[0]) // 2
        y = area_top + (available_height - size[1]) // 2
        image.paste(local.resize(size, Image.Resampling.NEAREST), (x, y))
        placement = [x, y, x + size[0], y + size[1]]
        crops.append({'marker': marker, 'source_box': list(box), 'placed_box': placement, 'scale': scale})

        def mapped(point):
            source_ink(point, box)
            return [x + (point[0] - box[0]) * scale + scale // 2,
                    y + (point[1] - box[1]) * scale + scale // 2]
        return mapped

    detail = spec['detail']
    if (not isinstance(detail, dict)
            or set(detail) - {'lower'} != {'source_box', 'spring', 'vertical'}):
        raise ValueError('one independently reviewed spring and vertical plate detail required')
    spring = detail['spring']
    draw.line((87, 165, 1285, 165), fill='black', width=8)
    draw.line((87, 583, 1285, 583), fill='black', width=8)
    upper, lower = 165, 583
    mark('upper', (72, 41), (202, upper))
    mark('lower', (72, 616), (202, lower))
    for column, marker in enumerate('ABCD'):
        x = 320 + 280 * column
        for start, end in ((upper, 294), (454, lower)):
            if marker == 'D':
                for y in range(start, end, 36):
                    draw.line((x, y, x, min(y + 20, end)), fill='black', width=8)
            else:
                draw.line((x, start, x, end), fill='black', width=8)
        draw.text((x, 372), marker, anchor='mm', font=face, fill='black')
        for y in (upper, lower):
            draw.ellipse((x - 17, y - 17, x + 17, y + 17), fill='white', outline='black', width=7)
        selected = None
        if marker != 'D':
            row = spec['windows'][column]
            if not isinstance(row, dict) or set(row) != {'marker', 'source_box', 'upper', 'lower'} or row['marker'] != marker:
                raise ValueError('reviewed A/B/C windows must be distinct and ordered')
            left = 65 + 440 * column
            draw.text((left + 181, 755), marker, anchor='mm', font=face, fill='black')
            selected = crop(row, left, 810, 362, 292, marker)
            if marker == 'C' and isinstance(spring, dict):
                continue_spring_leader(spring, crops[-1], selected)
            if marker == 'C' and 'lower' in detail:
                continue_lower_leader(detail['lower'], crops[-1], selected)
            if row['upper'] == row['lower']:
                raise ValueError('visible support endpoints must be distinct')
        for side, y in (('upper', upper), ('lower', lower)):
            endpoints.append({
                'marker': marker, 'side': side, 'part_id': roles[side], 'at': [x, y],
                'basis': 'source_pixels' if selected else 'source_relation',
                'source_pixel': row[side] if selected else None,
                'window_pixel': selected(row[side]) if selected else None,
            })

    if len({tuple(row['source_pixel']) for row in endpoints if row['source_pixel']}) != 6:
        raise ValueError('each visible support needs a distinct source endpoint')
    mapped = crop(detail, 266, 1120, 838, 400, 'detail')
    if not isinstance(spring, dict):
        mark('spring', (85, 1330), mapped(spring))
    vertical_x, vertical_y = mapped(detail['vertical'])
    mark('vertical', (1170, vertical_y + 118), (vertical_x, vertical_y))
    return {'base': png(image), 'annotations': annotation, 'endpoint_review': endpoints, 'crops': crops}

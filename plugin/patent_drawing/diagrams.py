"""Restricted geometry produces SVG and PNG from the same checked scene."""
import copy
import base64
import math
import re
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET

from PIL import Image, ImageDraw, ImageFont

from .errors import DrawingError
from .sets import fail, ident, text

FONT = Path(__file__).resolve().parents[1] / '_assets/NotoSansSC.ttf'
TOKEN = re.compile(r'\{\{([A-Za-z0-9_-]+)\.(name|label)\}\}')
ARROW_LENGTH = 15
VERTICAL_ELLIPSIS = '⋮'


@lru_cache(maxsize=8)
def font(size):
    return ImageFont.truetype(str(FONT), size)


@lru_cache(maxsize=2048)
def _has_glyph(character):
    face = font(28)
    glyph = face.getmask(character)
    missing = face.getmask(chr(0x10FFFF))
    return (glyph.size, bytes(glyph)) != (missing.size, bytes(missing))


def _check_glyphs(value):
    for line in value.split('\n'):
        if line.strip() == VERTICAL_ELLIPSIS:
            continue
        for character in line:
            if not character.isspace() and not _has_glyph(character):
                fail(f'绘图字体缺少字形 U+{ord(character):04X}，请核对图中文字')


def resolve(value, parts):
    text(value, 1600)
    def replace(match):
        pid, field = match.groups()
        if pid not in parts: fail('文本引用了图外或未知部件')
        return parts[pid][field]
    value = TOKEN.sub(replace, value)
    if '{{' in value or '}}' in value: fail('文本占位符无效')
    _check_glyphs(value)
    return value


def _number(value, upper):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= upper:
        fail('绘图坐标超限或非有限')
    return value


def _point(value, width, height):
    if not isinstance(value, list) or len(value) != 2: fail('坐标必须为二维点')
    return [_number(value[0], width), _number(value[1], height)]


def _box(value, width, height):
    if not isinstance(value, list) or len(value) != 4: fail('图形缺少边界框')
    x, y = _point(value[:2], width, height); x2, y2 = _point(value[2:], width, height)
    if x2 <= x or y2 <= y: fail('图形边界框为空')
    return [x, y, x2, y2]


def _distance(point, a, b):
    dx, dy = b[0]-a[0], b[1]-a[1]
    t = max(0, min(1, ((point[0]-a[0])*dx+(point[1]-a[1])*dy)/(dx*dx+dy*dy))) if dx or dy else 0
    return math.hypot(point[0]-a[0]-t*dx, point[1]-a[1]-t*dy)


def _orientation(a, b, point):
    return (b[0] - a[0]) * (point[1] - a[1]) - (b[1] - a[1]) * (point[0] - a[0])


def _segments_meet(a, b, c, d):
    for axis in (0, 1):
        if max(a[axis], b[axis]) < min(c[axis], d[axis]) or max(c[axis], d[axis]) < min(a[axis], b[axis]):
            return False
    return (_orientation(a, b, c) * _orientation(a, b, d) <= 0
            and _orientation(c, d, a) * _orientation(c, d, b) <= 0)


@lru_cache(maxsize=32)
def _simple_ring(points):
    if len(set(points[:-1])) != len(points) - 1:
        return False
    edges = list(zip(points, points[1:]))
    return not any(_segments_meet(a, b, c, d)
                   for index, (a, b) in enumerate(edges)
                   for c, d in edges[index + 2:]
                   if index != 0 or d != points[0])


def _inside_closed_outline(point, points):
    if len(points) < 4 or points[0] != points[-1]:
        return False
    x, y = point
    crossings = sum(x < ax + (y - ay) * (bx - ax) / (by - ay)
                    for (ax, ay), (bx, by) in zip(points, points[1:])
                    if (ay > y) != (by > y))
    return crossings % 2 == 1 and _simple_ring(tuple(map(tuple, points)))


def _inside_shape(point, shape):
    if shape['type'] == 'polyline':
        points = shape['points']
        if any(_distance(point, a, b) <= 8 for a, b in zip(points, points[1:])):
            return True
        # A closed part outline encloses a targetable area, like a rectangle.
        return not shape.get('arrow', False) and _inside_closed_outline(point, points)
    x, y, x2, y2 = shape['box']; px, py = point
    if shape['type'] == 'ellipse':
        return ((px-(x+x2)/2)/((x2-x)/2))**2 + ((py-(y+y2)/2)/((y2-y)/2))**2 <= 1.08
    return x-2 <= px <= x2+2 and y-2 <= py <= y2+2


def _touches(point, box):
    x, y, x2, y2 = box
    return ((x-2 <= point[0] <= x2+2 and min(abs(point[1]-y), abs(point[1]-y2)) <= 2)
            or (y-2 <= point[1] <= y2+2 and min(abs(point[0]-x), abs(point[0]-x2)) <= 2))


def _touches_node(point,node):
    if node['type']!='decision':return _touches(point,node['box'])
    x,y,x2,y2=node['box'];rx,ry=(x2-x)/2,(y2-y)/2
    value=abs(point[0]-(x+x2)/2)/rx+abs(point[1]-(y+y2)/2)/ry
    return abs(value-1)<=max(2/rx,2/ry)


def _overlap(a,b):
    return max(a[0],b[0])<min(a[2],b[2]) and max(a[1],b[1])<min(a[3],b[3])


def _crosses(a,b,box):
    # Liang-Barsky clipping against the open node rectangle.
    x,y,x2,y2=box;x+=0.2;y+=0.2;x2-=0.2;y2-=0.2
    dx,dy=b[0]-a[0],b[1]-a[1];lo,hi=0.0,1.0
    for p,q in [(-dx,a[0]-x),(dx,x2-a[0]),(-dy,a[1]-y),(dy,y2-a[1])]:
        if abs(p)<1e-12:
            if q<0:return False
        elif p<0:lo=max(lo,q/p)
        else:hi=min(hi,q/p)
        if lo>hi:return False
    return lo<=hi


def _text_box(value,at,size,anchor='start'):
    lines=value.split('\n');width=max(font(size).getlength(v)for v in lines)
    left=at[0]-(width/2 if anchor=='middle'else width if anchor=='end'else 0)
    return [left,at[1],left+width,at[1]+len(lines)*(size+7)]


def _node_layout(node, face_factory=font, *, preferred=28):
    x, y, right, bottom = node['box']
    width, height = right - x, bottom - y
    decision = node['type'] == 'decision'
    usable_width = width * 0.60 if decision else width - 28
    usable_height = height * 0.56 if decision else height - (6 if preferred > 28 else 22)
    compact_layout = None
    for size in range(preferred, 17, -2):
        face = face_factory(size)
        try:
            lines = _lines(node['text'], face, usable_width)
        except DrawingError:
            continue
        block_height = len(lines) * (size + 7)
        if block_height <= usable_height:
            return size, lines, block_height
        if compact_layout is None and not decision and len(lines) > 1:
            # Keep the line spacing and omit the gap after the final line.
            last_line_height = max(size, face.getbbox(lines[-1], anchor='lt')[3])
            compact_height = (len(lines) - 1) * (size + 7) + last_line_height
            if compact_height <= usable_height:
                if preferred > 28:
                    return size, lines, compact_height
                compact_layout = size, lines, compact_height
    if compact_layout is not None:
        return compact_layout
    fail('节点文字过多，不能在节点中完整排版')


def _route(source, target):
    a, b = source['box'], target['box']
    ax, ay, bx, by = (a[0]+a[2])/2, (a[1]+a[3])/2, (b[0]+b[2])/2, (b[1]+b[3])/2
    if abs(by-ay) >= abs(bx-ax):
        return [[ax, a[3] if by >= ay else a[1]], [bx, b[1] if by >= ay else b[3]]]
    return [[a[2] if bx >= ax else a[0], ay], [b[0] if bx >= ax else b[2], by]]


def _node_outline(node):
    x, y, right, bottom = node['box']
    if node['type'] == 'decision':
        cx, cy = (x + right) / 2, (y + bottom) / 2
        return [(cx, y), (right, cy), (cx, bottom), (x, cy)]
    return [(x, y), (right, y), (right, bottom), (x, bottom)]


def _through_node(a, b, node):
    """Clip to the node's open interior, allowing contact at its boundary."""
    lo, hi = 0.0, 1.0
    outline = _node_outline(node)
    for c, d in zip(outline, outline[1:] + outline[:1]):
        margin = 0.2 * math.dist(c, d)
        start, end = (_orientation(c, d, point) - margin for point in (a, b))
        if start < 0 and end < 0:
            return False
        if start < 0:
            lo = max(lo, start / (start - end))
        elif end < 0:
            hi = min(hi, start / (start - end))
        if lo > hi:
            return False
    return True


def arrow_polygon(points, length=ARROW_LENGTH):
    tip = points[-1]
    previous = next((p for p in reversed(points[:-1]) if p != tip), None)
    if previous is None:
        fail('箭头路径为空')
    angle = math.atan2(tip[1] - previous[1], tip[0] - previous[0])
    return [tip, *[[tip[0] - length * math.cos(a), tip[1] - length * math.sin(a)]
                   for a in (angle - 0.45, angle + 0.45)]]


def _route_arrowheads(points, both, length=ARROW_LENGTH):
    return [arrow_polygon(points, length)] + ([arrow_polygon(points[::-1], length)] if both else [])


def _dense_topology(figure, scene):
    return figure.get('delivered_type', figure.get('view_type')) == 'topology' and sum(
        edge.get('bidirectional', False) for edge in scene['edges']) >= 4


def _scaled_arrow_length(scene, edge, dense):
    if dense and edge.get('bidirectional'):
        nodes = {node['id']: node for node in scene['nodes']}
        if edge_geometry_fits(edge['points'], nodes[edge['source']], nodes[edge['target']],
                              list(nodes.values()), both=True, length=22):
            return 22
    return ARROW_LENGTH


def _svg_points(points):
    return ' '.join(f'{x},{y}' for x, y in points)


def rendered_edge_findings(plan, figure, svg, png, bindings, *, legacy_layout=False):
    """Verify each bound SVG shaft and arrowhead, including its actual PNG ink."""
    placements = [(row['kind'], placement['node_ids']) for row in bindings
                  if row['kind'] in {'communication', 'pwm'} for placement in row['placements']
                  if placement['view_id'] == figure['id']]
    if not placements:
        return []
    paths, heads = set(), []
    scene = validate_diagram(plan, figure)
    for kind, (source, target) in placements:
        for edge in scene['edges']:
            pair = (edge['source'], edge['target'])
            if pair == (source, target) or kind == 'communication' and pair == (target, source):
                paths.add(_svg_points(edge['points']))
                heads.extend(_route_arrowheads(edge['points'], edge.get('bidirectional', False),
                                               _scaled_arrow_length(scene, edge, not legacy_layout and _dense_topology(figure, scene))))
    try:
        root = ET.fromstring(svg)
        namespace = '{http://www.w3.org/2000/svg}'
        shafts = {line.get('points') for line in root.iter(namespace + 'polyline') if line.get('stroke') == 'black'}
        tips = {shape.get('points') for shape in root.iter(namespace + 'polygon') if shape.get('fill') == 'black'}
        with Image.open(BytesIO(png)) as image:
            ink = image.convert('L')
        if (paths and paths <= shafts and {_svg_points(head) for head in heads} <= tips
                and ink.size == (scene['width'], scene['height'])
                and all(_arrows_visible(ink, head) for head in heads)):
            return []
    except (ValueError, OSError, ET.ParseError, TypeError):
        pass
    return [{'rule': 'RENDERED-RELATIONSHIP', 'verdict': 'FAIL', 'view_id': figure['id'],
             'message': '实际 SVG 或 PNG 缺可辨的来源关系箭尖，请核对该图的遮挡、边界与方向。'}]


def _arrows_visible(image, head):
    tip, a, b = head
    # Two samples are inside the arrow wings and outside its 3px centerline.
    for near, far in ((a, b), (b, a)):
        x, y = [round(0.18 * t + 0.72 * n + 0.10 * f) for t, n, f in zip(tip, near, far, strict=True)]
        if not (0 <= x < image.width and 0 <= y < image.height and image.getpixel((x, y)) <= 64):
            return False
    return True


def edge_geometry_fits(points, source, target, nodes, *, both=False, length=ARROW_LENGTH):
    """Check physical contact, ingress/egress, and arrowhead clearance."""
    if (not _touches_node(points[0], source) or not _touches_node(points[-1], target)
            or math.dist(*points[-2:]) < length
            or (both and (math.dist(*points[:2]) < length
                         or math.dist(points[0], points[-1]) <= 2 * length))):
        return False
    segments = list(zip(points, points[1:]))
    heads = _route_arrowheads(points, both, length)
    segments += [(a, b) for head in heads for a, b in zip(head, head[1:] + head[:1])]
    return not any(_through_node(a, b, node) for a, b in segments for node in nodes)


def reanchor_edge(points, source, target, nodes):
    """Keep the original run of a detached path and extend it to unique node boundaries."""
    if edge_geometry_fits(points, source, target, nodes):
        return points

    def contacts(point, node):
        if _touches_node(point, node):
            return [point]
        x, y = point
        outline = _node_outline(node)
        candidates = []
        for a, b in zip(outline, outline[1:] + outline[:1]):
            for axis in (0, 1):
                if a[axis] == b[axis] or not min(a[axis], b[axis]) <= point[axis] <= max(a[axis], b[axis]):
                    continue
                ratio = (point[axis] - a[axis]) / (b[axis] - a[axis])
                hit = [a[i] + ratio * (b[i] - a[i]) for i in (0, 1)]
                hit[axis] = x if axis == 0 else y
                if hit not in candidates and not _through_node(point, hit, node):
                    candidates.append(hit)
        return candidates

    candidates = []
    for first in contacts(points[0], source):
        for last in contacts(points[-1], target):
            route = ([] if first == points[0] else [first]) + points + ([] if last == points[-1] else [last])
            if len(route) <= 30 and edge_geometry_fits(route, source, target, nodes):
                candidates.append(route)
    return candidates[0] if len(candidates) == 1 else None


def validate_diagram(plan, figure):
    scene = copy.deepcopy(figure.get('diagram'))
    if not isinstance(scene, dict) or set(scene)-{'width','height','primitives','nodes','edges'}:
        fail('绘图数据字段无效')
    for k in ['width','height']:
        if type(scene.get(k)) is not int or not 600 <= scene[k] <= 2400: fail('画布支持600至2400像素')
    width, height = scene['width'], scene['height']
    parts = {p['id']: p for p in plan['parts'] if p['id'] in figure['parts']}
    shapes = {}
    for key, limit in [('primitives',600),('nodes',48),('edges',96)]:
        scene.setdefault(key, [])
        if not isinstance(scene[key],list) or len(scene[key]) > limit: fail('绘图元素超限')
    if not scene['primitives'] and not scene['nodes']: fail('绘图场景为空')
    for item in scene['primitives']:
        if not isinstance(item,dict): fail('图形元素无效')
        typ = item.get('type')
        fields = {'rect': {'box','fill'}, 'ellipse': {'box','fill'},
                  'polyline': {'points','arrow'}, 'text': {'at','text','font_size','anchor'},
                  'label': {'part','at','target','target_shape','font_size'}}
        if typ not in fields or set(item)-fields[typ]-{'type','id','part','stroke_width'}:
            fail('不支持的图形元素或字段')
        if 'part' in item and item['part'] not in parts: fail('图形引用图外部件')
        if 'id' in item:
            ident(item['id'])
            if item['id'] in shapes: fail('图形ID重复')
            shapes[item['id']] = item
        if type(item.get('stroke_width',3)) not in (int,float) or not 1 <= item.get('stroke_width',3) <= 10:
            fail('线宽无效')
        if typ in {'rect','ellipse'}:
            item['box'] = _box(item.get('box'),width,height)
            if item.get('fill','white') not in {'white','black','none'}: fail('仅支持黑白图形')
        if typ == 'polyline':
            points=item.get('points')
            if not isinstance(points,list) or not 2 <= len(points) <= 300: fail('折线点无效')
            item['points']=[_point(p,width,height) for p in points]
            if type(item.get('arrow',False)) is not bool: fail('箭头参数无效')
        if typ in {'text','label'}:
            item['at']=_point(item.get('at'),width,height)
            if type(item.get('font_size',26)) is not int or not 14 <= item.get('font_size',26) <= 52:
                fail('字号无效')
        if typ == 'text':
            item['text']=resolve(item.get('text'),parts)
            if item.get('anchor','start') not in {'start','middle','end'}: fail('文本锚点无效')
        if typ == 'label':
            item['target']=_point(item.get('target'),width,height)
            if item.get('part') not in parts: fail('标号缺少部件')
    for item in scene['primitives']:
        if item['type']=='label':
            shape=shapes.get(item.get('target_shape'))
            if (not shape or shape['type'] not in {'rect','ellipse','polyline'}
                    or shape.get('part')!=item['part'] or not _inside_shape(item['target'],shape)):
                fail('引出线未指向指定部件图形')
    nodes={}
    for node in scene['nodes']:
        if not isinstance(node,dict) or set(node)-{'id','type','box','text','branches'}: fail('节点字段无效')
        ident(node.get('id'))
        if node['id'] in nodes: fail('节点ID重复')
        if node.get('type') not in {'process','decision','terminal','note','actor'}: fail('节点类型无效')
        node['box']=_box(node.get('box'),width,height); node['text']=resolve(node.get('text'),parts)
        if node['type']=='decision':
            if not isinstance(node.get('branches'),dict) or set(node['branches'])!={'yes','no'}:
                fail('判断节点必须声明是/否去向')
        elif 'branches' in node: fail('非判断节点不可声明条件分支')
        nodes[node['id']]=node
    branch_counts={nid:[] for nid,n in nodes.items()if n['type']=='decision'}
    for edge in scene['edges']:
        if not isinstance(edge,dict) or set(edge)-{'source','target','branch','label','points','label_at','bidirectional'}:
            fail('连线字段无效')
        if edge.get('source') not in nodes or edge.get('target') not in nodes: fail('连线节点缺失')
        source,target=nodes[edge['source']],nodes[edge['target']]
        if source['type']=='decision':
            branch=edge.get('branch')
            if branch not in {'yes','no'} or source['branches'][branch]!=edge['target'] or 'label' in edge:
                fail('条件分支与目标不一致')
            branch_counts[edge['source']].append(branch)
            edge['label']='是' if branch=='yes' else '否'
        else:
            if 'branch' in edge: fail('非判断节点不能有是/否分支')
            edge['label']=resolve(edge['label'],parts)if edge.get('label')else''
        if type(edge.get('bidirectional', False)) is not bool or edge.get('bidirectional') and (
                source['type'] == 'decision' or target['type'] == 'decision' or source is target):
            fail('双向箭线必须连接两个不同的非判断节点')
        points=edge.get('points',_route(source,target))
        if not isinstance(points,list) or not 2 <= len(points) <= 30: fail('连线路径无效')
        edge['points']=[_point(p,width,height) for p in points]
        if not _touches_node(edge['points'][0],source) or not _touches_node(edge['points'][-1],target):
            fail('连线端点未落在正确节点边界')
        if 'label_at' in edge: edge['label_at']=_point(edge['label_at'],width,height)
        else:
            a,b=edge['points'][:2];edge['label_at']=[(a[0]+b[0])/2,(a[1]+b[1])/2-22]
    if any(sorted(branches)!=['no','yes'] for branches in branch_counts.values()):
        fail('判断节点缺少或重复分支')
    node_list=list(nodes.values())
    for index,node in enumerate(node_list):
        _node_layout(node)
        if any(_overlap(node['box'],other['box'])for other in node_list[index+1:]):fail('节点重叠，图示含义不清')
    for edge in scene['edges']:
        if edge.get('bidirectional') and not edge_geometry_fits(
                edge['points'], nodes[edge['source']], nodes[edge['target']], node_list, both=True):
            fail('双向箭尖须分别落在对应节点边界，且线路与箭尖不得穿入节点或互相覆盖')
        if edge['label']:
            box=_text_box(edge['label'],edge['label_at'],24)
            if box[0]<0 or box[1]<0 or box[2]>width or box[3]>height:fail('连线标签超出画布')
            if any(_overlap(box,node['box'])for node in node_list):fail('连线标签被节点遮挡或与节点重叠')
        for node in node_list:
            if node['id']in {edge['source'],edge['target']}:continue
            if any(_crosses(a,b,node['box'])for a,b in zip(edge['points'],edge['points'][1:])):fail('连线路径穿过无关节点')
    for item in scene['primitives']:
        if item['type']in {'text','label'}:
            value=item['text']if item['type']=='text'else parts[item['part']]['label']
            _check_glyphs(value)
            box=_text_box(value,item['at'],item.get('font_size',26),item.get('anchor','start'))
            if box[0]<0 or box[1]<0 or box[2]>width or box[3]>height:fail('文字超出画布')
    return scene


def _lines(value, face, width):
    lines=[]
    forbidden='，。！？；：、,.!?;:)]）】》'
    for paragraph in value.split('\n'):
        local=[];line=''
        tokens = re.findall(r'[0-9]+[A-Za-z]?|.', paragraph)
        for token in tokens:
            if line and face.getlength(line + token) > width:
                if token in forbidden and len(line) > 1 and not line[-1].isascii():
                    local.append(line[:-1])
                    line = line[-1] + token
                else:
                    local.append(line)
                    line = token
            else:
                line += token
        if len(line)==1 and local and len(local[-1])>1 and not local[-1][-1].isascii():
            line=local[-1][-1]+line;local[-1]=local[-1][:-1]
        if any(face.getlength(x)>width for x in local+[line]):fail('节点文字不能在给定宽度中完整排版')
        lines.extend(local+[line])
    return lines


def render_diagram(plan, figure, font_factory=None, layered=False, *, legacy_layout=False):
    font = font_factory or globals()['font']
    scene=validate_diagram(plan,figure);width,height=scene['width'],scene['height']
    image=Image.new('RGB',(width,height),'white');draw=ImageDraw.Draw(image)
    text_layer=Image.new('RGBA',(width,height),(0,0,0,0));text_draw=ImageDraw.Draw(text_layer) if layered else draw
    root=ET.Element('svg',xmlns='http://www.w3.org/2000/svg',width=str(width),height=str(height),viewBox=f'0 0 {width} {height}')
    ET.SubElement(root,'rect',x='0',y='0',width=str(width),height=str(height),fill='white')
    parts={p['id']:p for p in plan['parts']};anchors=[]

    dense = not legacy_layout and _dense_topology(figure, scene)

    def line(points,stroke=3,arrow=False,both=False, *, arrow_length=ARROW_LENGTH):
        draw.line([tuple(p)for p in points],fill='black',width=round(stroke),joint='curve')
        ET.SubElement(root,'polyline',points=_svg_points(points),fill='none',stroke='black',**{'stroke-width':str(stroke)})
        if arrow:
            for polygon in _route_arrowheads(points, both, arrow_length):
                draw.polygon([tuple(p)for p in polygon],fill='black')
                ET.SubElement(root,'polygon',points=_svg_points(polygon),fill='black')

    def put(value,at,size=26,anchor='start',bindings=()):
        face=font(size);x,y=at
        for i,value_line in enumerate(value.split('\n')):
            length=face.getlength(value_line);left=x-(length/2 if anchor=='middle'else length if anchor=='end'else 0)
            top=y+i*(size+7)
            if left<0 or left+length>width or top<0 or top+size+7>height: fail('文字超出画布')
            if value_line.strip() == VERTICAL_ELLIPSIS:
                center = left + length / 2
                radius = max(1, size * 0.07)
                symbol = ET.SubElement(root, 'g', {'aria-label': VERTICAL_ELLIPSIS})
                for fraction in (0.2, 0.5, 0.8):
                    dot_y = top + size * fraction
                    text_draw.ellipse((center - radius, dot_y - radius, center + radius, dot_y + radius), fill='black')
                    ET.SubElement(symbol, 'circle', cx=str(center), cy=str(dot_y), r=str(radius), fill='black')
                continue
            text_draw.text((left,top),value_line,font=face,fill='black',anchor='lt')
            element=ET.SubElement(root,'text',x=str(left),y=str(top+size),fill='black',
                **{'font-size':str(size),'font-family':'Noto Sans SC, sans-serif'})
            element.text=value_line
            from .marking import mark_pattern
            marks = [pid for pid in bindings if mark_pattern(parts[pid]['label']).search(value_line)]
            if marks:
                element.set('data-mark-parts', ' '.join(sorted(set(marks))))
                bounds = face.getbbox(value_line, anchor='lt')
                element.set('data-ink-box', ' '.join(map(str, (
                    math.floor(left + bounds[0]), math.floor(top + bounds[1]),
                    math.ceil(left + bounds[2]), math.ceil(top + bounds[3]),
                ))))

    def bindings(value):
        return [pid for pid, field in TOKEN.findall(value) if field == 'label' and parts[pid]['label']]

    def scope_outline(item, original):
        if item['type'] != 'rect' or 'fill' in original:
            return False
        left, top, right, bottom = item['box']
        contained = [node for node in scene['nodes'] if
                     left < node['box'][0] < node['box'][2] < right
                     and top < node['box'][1] < node['box'][3] < bottom]
        return bool(contained)

    for edge in scene['edges']:
        line(edge['points'], arrow=True, both=edge.get('bidirectional', False),
             arrow_length=_scaled_arrow_length(scene, edge, dense))
        if edge['label']:put(edge['label'],edge['label_at'],24)
    for node, original in zip(scene['nodes'], figure['diagram'].get('nodes', ()), strict=True):
        x,y,x2,y2=node['box'];boxwidth,boxheight=x2-x,y2-y
        if node['type']=='decision':
            pts=[[(x+x2)/2,y],[x2,(y+y2)/2],[(x+x2)/2,y2],[x,(y+y2)/2]]
            draw.polygon([tuple(p)for p in pts],fill='white',outline='black',width=3)
            ET.SubElement(root,'polygon',points=' '.join(f'{a},{b}'for a,b in pts),fill='white',stroke='black',**{'stroke-width':'3'})
        else:
            radius=28 if node['type']=='terminal'else 0
            draw.rounded_rectangle((x,y,x2,y2),radius=radius,fill='white',outline='black',width=3)
            ET.SubElement(root,'rect',x=str(x),y=str(y),width=str(boxwidth),height=str(boxheight),rx=str(radius),fill='white',stroke='black',**{'stroke-width':'3'})
        size, lines, block_height = _node_layout(node, font, preferred=38 if dense else 28)
        top=(y+y2)/2-block_height/2
        for index,value in enumerate(lines):
            put(value, [(x+x2)/2, top+index*(size+7)], size, 'middle', bindings(original['text']))
    for item, original in zip(scene['primitives'], figure['diagram'].get('primitives', ()), strict=True):
        typ=item['type'];stroke=item.get('stroke_width',3)
        if typ in {'rect','ellipse'}:
            x,y,x2,y2=item['box']
            fill = 'none' if scope_outline(item, original) else item.get('fill', 'white')
            pillow_fill=None if fill=='none'else fill
            if typ=='rect':
                draw.rectangle((x,y,x2,y2),fill=pillow_fill,outline='black',width=round(stroke))
                ET.SubElement(root,'rect',x=str(x),y=str(y),width=str(x2-x),height=str(y2-y),fill=fill,stroke='black',**{'stroke-width':str(stroke)})
            else:
                draw.ellipse((x,y,x2,y2),fill=pillow_fill,outline='black',width=round(stroke))
                ET.SubElement(root,'ellipse',cx=str((x+x2)/2),cy=str((y+y2)/2),rx=str((x2-x)/2),ry=str((y2-y)/2),fill=fill,stroke='black',**{'stroke-width':str(stroke)})
        elif typ=='polyline':line(item['points'],stroke,item.get('arrow',False))
        elif typ=='text':put(item['text'],item['at'],item.get('font_size',26),item.get('anchor','start'),bindings(original['text']))
        elif typ=='label':
            value=parts[item['part']]['label'];size=item.get('font_size',30);x,y=item['at'];target=item['target']
            start=[x-9 if target[0]<x else x+font(size).getlength(value)+9,y+size/2]
            line([start,target],2);put(value,item['at'],size,bindings=(item['part'],))
            anchors.append({'part':item['part'],'text':value,'at':item['at'],
                            'target':target,'target_shape':item['target_shape']})
    geometry=BytesIO();image.save(geometry,format='PNG',compress_level=6)
    regions=[]
    if layered:
        mask=text_layer.getchannel('A').point(lambda v:255 if v else 0)
        if mask.getbbox():
            encoded=BytesIO();mask.save(encoded,format='PNG',compress_level=6);regions=[{'text_box':list(mask.getbbox()),'mask':base64.b64encode(encoded.getvalue()).decode()}]
        from PIL import ImageChops
        contrasted = ImageChops.multiply(mask, image.convert('L').point(lambda v: 255 if v >= 192 else 0))
        for node in root.iter('text'):
            if box := node.get('data-ink-box'):
                bounds = tuple(int(n) for n in box.split())
                active, visible = mask.crop(bounds).histogram()[255], contrasted.crop(bounds).histogram()[255]
                if not active or visible * 10 < active * 9:
                    node.attrib.pop('data-mark-parts', None)
        image.paste(text_layer,mask=text_layer.getchannel('A'))
    out=BytesIO();image.save(out,format='PNG',compress_level=6)
    return {'base_png':geometry.getvalue(),'text_regions':regions,'png':out.getvalue(),'svg':ET.tostring(root,encoding='utf-8',xml_declaration=True),
            'width':width,'height':height,'anchors':anchors,'edges':scene['edges']}


def split_drafting_notes(diagram):
    """Move only isolated editorial scope notes; keep operative graph untouched."""
    result=copy.deepcopy(diagram);connected={e[k]for e in result.get('edges',[])for k in ['source','target']}
    connected.update(target for n in result.get('nodes',[])for target in n.get('branches',{}).values())
    notes=[];kept=[]
    for node in result.get('nodes',[]):
        if node.get('type')=='note'and node['id']not in connected and re.match(r'本图不(?:设|指定|表示)|制图(?:说明|备注)',node.get('text','')):
            notes.append({'node':node,'reason':'isolated editorial note retained outside drawing; source plan unchanged'})
        else:kept.append(node)
    # A note-only scene cannot be converted into a blank drawing.
    if kept or result.get('primitives'):
        result['nodes']=kept
        return result,notes
    return copy.deepcopy(diagram),[]

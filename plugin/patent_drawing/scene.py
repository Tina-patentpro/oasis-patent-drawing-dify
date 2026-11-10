"""Bounded geometric relations over explicit instances, not invented CAD."""
import math


def validate_scene(scene):
    rows = scene.get('instances', [])
    if not isinstance(rows, list) or len(rows) > 240:
        raise ValueError('instance capacity exceeded')
    instances = {}
    for row in rows:
        ident = row.get('id'); box = row.get('bounds')
        if ident in instances or not isinstance(ident, str) or not isinstance(box, list) or len(box) != 6:
            raise ValueError('instance identity/bounds invalid')
        if any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 1e6 for v in box) or any(box[i] >= box[i + 3] for i in range(3)):
            raise ValueError('instance bounds must have finite positive extent')
        instances[ident] = row
    out = []
    def fail(rule, message):
        out.append({'rule': rule, 'verdict': 'FAIL', 'message': message})
    relations = scene.get('relations', [])
    if not isinstance(relations, list) or len(relations) > 160:
        raise ValueError('relation capacity exceeded')
    for r in relations:
        subjects = r.get('subjects', []); obj = instances.get(r.get('object'))
        if not subjects or set(subjects) - instances.keys() or obj is None:
            fail('SCENE-REFERENCE', '关系引用缺失的实例'); continue
        if len(subjects) != len(set(subjects)) or ('count' in r and len(subjects) != r['count']):
            fail('SCENE-COUNT', '部件实例数量不符或重复计数')
        if r['type'] == 'supports':
            base = instances.get(r.get('base'))
            if base is None:
                fail('SCENE-SUPPORT', '缺支承基部'); continue
            upper, lower = obj['bounds'], base['bounds']; corners = set()
            for sid in subjects:
                box = instances[sid]['bounds']; x = (box[0] + box[3]) / 2; y = (box[1] + box[4]) / 2
                if abs(box[2] - lower[5]) > 1e-6 or abs(box[5] - upper[2]) > 1e-6 or any(min(box[i+3], surface[i+3]) <= max(box[i], surface[i]) for surface in [lower, upper] for i in [0, 1]):
                    fail('SCENE-SUPPORT', f'{sid}没有位于指定支承板和托板之间并连接二者')
                corners.add((x > (upper[0] + upper[3]) / 2, y > (upper[1] + upper[4]) / 2))
            if r.get('four_corners') and len(corners) != 4:
                fail('SCENE-CORNERS', '四角支承没有占据四个独立角区')
        elif r['type'] == 'coaxial':
            axis = {'x': 0, 'y': 1, 'z': 2}.get(r.get('axis'))
            if axis is None: raise ValueError('coaxial axis invalid')
            for sid in subjects:
                a, b = instances[sid]['bounds'], obj['bounds']
                if any(abs((a[i] + a[i + 3]) / 2 - (b[i] + b[i + 3]) / 2) > 1e-6 for i in range(3) if i != axis):
                    fail('SCENE-COAXIAL', '销和孔轴线不重合')
        else:
            out.append({'rule': 'SCENE-RELATION', 'verdict': 'UNKNOWN', 'message': f'关系{r["type"]}尚无可计算几何检查'})
    return out

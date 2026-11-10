"""Credential-free, bounded drawing-set contract and dependency invalidation."""
import copy
import hashlib
import json
import re

from .errors import DrawingError
from .prompts import build_prompt

SCHEMA = 'drawing-set-1'
MAX_PLAN_BYTES = 240_000
MAX_FIGURES = 24
ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$')
LABEL = re.compile(r'^[A-Za-z0-9][A-Za-z0-9()._-]{0,23}$')


def fail(message):
    raise DrawingError('set_contract_error', message)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result: fail('JSON包含重复字段')
        result[key] = value
    return result


def parse_object(value):
    try:
        if isinstance(value, str):
            if len(value.encode()) > MAX_PLAN_BYTES: fail('图组JSON超限')
            value = json.loads(value, object_pairs_hook=_pairs,
                               parse_constant=lambda _: fail('不支持非有限数值'))
        if not isinstance(value, dict): fail('图组必须为JSON对象')
        if len(canonical(value).encode()) > MAX_PLAN_BYTES: fail('图组JSON超限')
        return copy.deepcopy(value)
    except DrawingError:
        raise
    except (ValueError, TypeError, RecursionError, OverflowError):
        fail('图组JSON不可解析')


def text(value, limit=500, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        fail('文本缺失或超限')
    if any(ord(c) < 32 and c not in '\n\t\r' for c in value): fail('文本包含控制字符')
    return value


def ident(value):
    if not isinstance(value, str) or not ID.fullmatch(value): fail('图组/图件/部件ID无效')
    return value


def _strings(value, limit=80, item_limit=1000):
    if not isinstance(value, list) or len(value) > limit: fail('列表缺失或超限')
    return [text(x, item_limit) for x in value]


def validate_plan(data):
    plan = parse_object(data)
    allowed = {'schema_version', 'set_id', 'revision', 'title', 'source_text',
               'source_sha256', 'parts', 'relations', 'constraints', 'unresolved', 'figures'}
    if set(plan) - allowed: fail('图组包含未支持字段')
    if plan.get('schema_version') != SCHEMA: fail('图组合同版本不支持')
    ident(plan.get('set_id'))
    if type(plan.get('revision')) is not int or not 1 <= plan['revision'] <= 10000:
        fail('图组修订号无效')
    text(plan.get('title'), 120); text(plan.get('source_text'), 24000)
    if not isinstance(plan.get('source_sha256'), str) or not re.fullmatch('[0-9a-f]{64}', plan['source_sha256']):
        fail('来源SHA256无效')
    for key in ['constraints', 'unresolved']:
        plan[key] = _strings(plan.get(key, []))
    parts = plan.get('parts')
    if not isinstance(parts, list) or not 1 <= len(parts) <= 120: fail('部件表数量无效')
    ids, labels = set(), set()
    for part in parts:
        if not isinstance(part, dict) or set(part) != {'id', 'label', 'name'}: fail('部件字段无效')
        ident(part['id']); text(part['name'], 80)
        if not isinstance(part['label'], str) or not LABEL.fullmatch(part['label']): fail('部件标号无效')
        if part['id'] in ids or part['label'] in labels: fail('部件身份或标号冲突')
        ids.add(part['id']); labels.add(part['label'])
    relations = plan.setdefault('relations', [])
    if not isinstance(relations, list) or len(relations) > 160: fail('关系表数量无效')
    rids = set()
    for relation in relations:
        if not isinstance(relation, dict) or set(relation) != {'id', 'subject', 'predicate', 'object'}:
            fail('关系字段无效')
        ident(relation['id']); text(relation['predicate'], 120)
        if relation['id'] in rids or relation['subject'] not in ids or relation['object'] not in ids:
            fail('关系身份或部件引用无效')
        rids.add(relation['id'])
    figures = plan.get('figures')
    if not isinstance(figures, list) or not 1 <= len(figures) <= MAX_FIGURES: fail('每组支持1至24图')
    fids = set()
    fields = {'id', 'title', 'purpose', 'engine', 'kind', 'parts', 'depends_on', 'covers', 'instruction', 'diagram'}
    for figure in figures:
        if not isinstance(figure, dict) or set(figure) - fields: fail('图件字段无效')
        ident(figure.get('id'))
        if figure['id'] in fids: fail('图件ID重复')
        fids.add(figure['id'])
        text(figure.get('title'), 120); text(figure.get('purpose'), 500)
        text(figure.get('instruction'), 8000)
        if figure.get('engine') not in {'image', 'vector'}: fail('绘图引擎无效')
        if figure.get('kind') not in {'structure', 'flowchart', 'block_diagram', 'circuit'}:
            fail('附图类型无效')
        for key in ['parts', 'depends_on', 'covers']:
            figure[key] = _strings(figure.get(key, []), 160, 48)
            if len(figure[key]) != len(set(figure[key])): fail('图件引用重复')
        if not figure['parts'] or set(figure['parts']) - ids or set(figure['covers']) - rids:
            fail('图件引用未知部件/关系')
        if figure['engine'] == 'vector' and not isinstance(figure.get('diagram'), dict):
            fail('矢量图缺少确定性绘图数据')
    if any(set(f['depends_on']) - fids for f in figures): fail('图件依赖缺失')
    topological_order(plan)
    from .diagrams import validate_diagram
    for figure in figures:
        if figure['engine'] == 'vector': validate_diagram(plan, figure)
        else: _build_figure_request(plan,figure['id'],'generate','')
    return plan


def topological_order(plan):
    figures = {f['id']: f for f in plan['figures']}
    ordered, visiting, done = [], set(), set()

    def visit(fid):
        if fid in visiting: fail('图件依赖存在循环')
        if fid in done: return
        if fid not in figures: fail('图件依赖缺失')
        visiting.add(fid)
        for parent in figures[fid]['depends_on']: visit(parent)
        visiting.remove(fid); done.add(fid); ordered.append(fid)

    for fid in figures: visit(fid)
    return ordered


def plan_hash(plan):
    return digest(validate_plan(plan))


def figure_spec_hash(plan, figure_id):
    plan = validate_plan(plan)
    figures = {f['id']: f for f in plan['figures']}
    if figure_id not in figures: fail('图件不存在')
    hashes = {}
    for fid in topological_order(plan):
        f = figures[fid]
        hashes[fid] = digest({'figure': f, 'source': plan['source_sha256'],
            'source_text': plan['source_text'], 'constraints': plan['constraints'],
            'unresolved': plan['unresolved'],
            'parts': [p for p in plan['parts'] if p['id'] in f['parts']],
            'relations': [r for r in plan['relations'] if r['id'] in f['covers']],
            'parents': {x: hashes[x] for x in f['depends_on']}})
    return hashes[figure_id]


def affected_figures(old, new):
    old, new = validate_plan(old), validate_plan(new)
    if (old['set_id'] != new['set_id'] or new['revision'] != old['revision'] + 1
            or old['source_sha256'] != new['source_sha256'] or old['source_text'] != new['source_text']):
        fail('修改必须基于同一材料及紧邻图组版本')
    for field in ['constraints','unresolved']:
        if set(old[field])-set(new[field]):fail('修改不得删除原材料的约束或待确认项；新事实须另建材料版本')
    old_ids = {f['id'] for f in old['figures']}
    return [fid for fid in topological_order(new)
            if fid not in old_ids or figure_spec_hash(old, fid) != figure_spec_hash(new, fid)]


def build_figure_request(plan, figure_id, mode='generate', edit_instruction=''):
    return _build_figure_request(validate_plan(plan),figure_id,mode,edit_instruction)


def _build_figure_request(plan,figure_id,mode,edit_instruction):
    figure = next((f for f in plan['figures'] if f['id'] == figure_id), None)
    if figure is None: fail('图件不存在')
    labels = '\n'.join(p['label'] + '：' + p['name'] for p in plan['parts'] if p['id'] in figure['parts'])
    instruction = ('本图：' + figure['title'] + '\n目的：' + figure['purpose'] + '\n'
                   + figure['instruction'] + '\n原始技术材料：\n' + plan['source_text'])
    if edit_instruction: instruction += '\n本次修改：' + text(edit_instruction, 2000)
    return build_prompt({'prompt': instruction, 'reference_labels': labels,
        'constraints': '\n'.join(plan['constraints']) + '\n待确认，不补造：\n' + '\n'.join(plan['unresolved']),
        'drawing_type': figure['kind'], 'size': '1536x1024', 'quality': 'high',
        'complexity': 'high' if mode == 'generate' else 'low', 'output_format': 'png'}, mode)

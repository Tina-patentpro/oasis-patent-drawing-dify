"""Stable part identity is distinct from an optional workflow display mark."""
import copy
import re
from xml.etree import ElementTree
from .operation_store import canonical, digest

CONTRACT = 'drawing-contract-035-r1'
FLOW_TYPES = {'flowchart', 'block_diagram', 'sequence'}


def mark_pattern(mark):
    return re.compile(r'(?<![A-Za-z0-9])' + re.escape(mark) + r'(?![A-Za-z0-9])')


def visible_vector_marks(view, parts, svg):
    """A designation must be continuous and tied to its figure object."""
    root = ElementTree.fromstring(svg)
    rows = [node for node in root.iter() if node.tag.rsplit('}', 1)[-1] == 'text']
    visible = {}
    for part_id in view.get('parts', []):
        if part_id not in parts or not (mark := parts[part_id].get('label')):
            continue
        boundary = mark_pattern(mark)
        if any(part_id in row.get('data-mark-parts', '').split() and boundary.search(row.text or '') for row in rows):
            visible[part_id] = mark
    return visible


def bind_vector_numbers(plan):
    """Bind marks to uniquely named existing objects, leaving geometry intact."""
    changes = []
    if plan.get('part_mark_mode') != 'numbered':
        return changes
    parts = {part['id']: part for part in plan.get('parts', [])}
    for view in plan.get('views', []):
        if view.get('engine') != 'vector':
            continue
        scene = view.get('diagram', {})
        primitives = scene.get('primitives', [])
        entries = [*scene.get('nodes', []), *[p for p in primitives if p.get('type') == 'text']]
        for part_id in view.get('parts', []):
            part = parts.get(part_id)
            if not part or not part.get('label') or not part.get('name'):
                continue
            token = '{{' + part_id + '.label}}'
            if (any(p.get('type') == 'label' and p.get('part') == part_id for p in primitives)
                    or any(token in e.get('text', '') for e in entries)):
                continue
            name_token = '{{' + part_id + '.name}}'
            anchors = [e for e in entries if name_token in e.get('text', '')
                       or e.get('text', '').split('\n')[0].strip() in {
                           part['name'], part['name'] + ' ' + part['label']
                       }]
            # An explicit named object can also appear in a second procedural
            # step. In that case the earliest explicitly named node gets the
            # mark, and references elsewhere retain their existing wording.
            explicit = [entry for entry in anchors if name_token in entry.get('text', '')]
            if len(explicit) > 1:
                anchors = explicit[:1]
            if len(anchors) != 1:
                continue
            target = anchors[0]
            name = name_token if name_token in target['text'] else part['name']
            printed = name + ' ' + part['label']
            before = printed if printed in target['text'] else name
            target['text'] = target['text'].replace(before, name + ' ' + token, 1)
            changes.append({'view_id': view['id'], 'part_id': part_id,
                            'target': target.get('id') or target.get('at')})
    return changes


def _name_only_eligible(plan, bundle):
    views = plan.get('views', [])
    if (set(plan.get('technical_types', [])) != {'computer_method'} or not views
            or bundle.get('figure_presence') != 'absent' or plan.get('scene')
            or plan.get('specification_labels') or plan.get('specification_labels_complete')):
        return False
    for view in views:
        if (view.get('engine') != 'vector' or view.get('requested_type') not in FLOW_TYPES
                or view.get('delivered_type') not in FLOW_TYPES or view.get('annotations')
                or view.get('labels') or view.get('source_region_id')):
            return False
        diagram = view.get('diagram', {})
        if any(p.get('type') == 'label' for p in diagram.get('primitives', [])):
            return False
        if re.search(r'\{\{[^{}]+\.label\}\}', canonical(diagram)):
            return False
    return True


def adapt_marking(plan: dict, bundle: dict) -> tuple[dict, dict]:
    p = copy.deepcopy(plan); changes = []
    parts = p.get('parts', [])
    if ('part_mark_mode' not in p and parts and all(x.get('label') == '' for x in parts)
            and _name_only_eligible(p, bundle)):
        p['part_mark_mode'] = 'name_only'
        changes.append({'path': 'part_mark_mode', 'before': None, 'after': 'name_only',
                        'rule': 'PURE-COMPUTER-FLOW-NAME-ONLY'})
    return p, {'contract': CONTRACT, 'input_sha256': digest(plan), 'output_sha256': digest(p),
               'changes': changes, 'part_values_changed': False}


def validate_marking(plan: dict, bundle: dict) -> list[dict]:
    def fail(message):
        return [{'rule': 'PART-MARK-CONTRACT', 'verdict': 'FAIL', 'message': message}]
    mode = plan.get('part_mark_mode', 'numbered')
    if mode not in {'numbered', 'name_only'}: return fail('unsupported part mark mode')
    if mode == 'name_only' and not _name_only_eligible(plan, bundle):
        return fail('name_only requires pure name-based computer flow with no known numeric obligations')
    ids, marks = set(), set()
    for part in plan.get('parts', []):
        pid, mark, name = part.get('id'), part.get('label'), part.get('name')
        if not isinstance(pid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', pid) or pid in ids:
            return fail('duplicate or invalid stable part identity')
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            return fail('invalid part name')
        if not isinstance(mark, str): return fail('part label must remain a string; no coercion')
        if mode == 'name_only':
            if mark != '': return fail('name_only requires exact empty strings, not missing or numbered marks')
        elif not mark.strip() or mark != mark.strip() or mark in marks:
            return fail('part/label conflict: numbered marks must be nonempty and unique')
        ids.add(pid); marks.add(mark)
    return []

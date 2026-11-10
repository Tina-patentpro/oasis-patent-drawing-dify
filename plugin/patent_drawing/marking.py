"""Stable part identity is distinct from an optional workflow display mark."""
import copy
import re
from .operation_store import canonical, digest

CONTRACT = 'drawing-contract-035-r1'
FLOW_TYPES = {'flowchart', 'block_diagram', 'sequence'}


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

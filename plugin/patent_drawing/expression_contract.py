"""Source-bound expression choices. Validation never grants operator authority."""
import copy
import math
import re

from .operation_store import canonical, digest
from .source_contract import compact


def unknown_disposition_issues(unknowns: list, plan: dict, *, require_all=True) -> list:
    source = {f['id']: f for f in plan.get('facts', []) if f.get('status') != 'confirmed'}
    allowed_features = {'source-fidelity'} | {
        f['feature_id'] for c in plan.get('visibility_contract', {}).values() for f in c['features']}
    issues, seen = [], set()
    def issue(verdict, text):
        issues.append({'rule': 'EXPRESSION-CONTRACT', 'verdict': verdict, 'message': text})
    for u in unknowns:
        if not isinstance(u, dict) or not isinstance(u.get('id'), str):
            issue('FAIL', 'unknown disposition object invalid'); continue
        uid = u['id']; seen.add(uid); original = source.get(uid)
        if original:
            if not isinstance(u.get('text'), str) or compact(u['text']) != compact(original.get('text', '')):
                issue('FAIL', 'source unknown text changed: ' + uid)
            if u.get('critical') is False:
                reason = u.get('reason'); affected = u.get('affected_features')
                if (not isinstance(reason, str) or not 12 <= len(reason.strip()) <= 2000
                        or not isinstance(affected, list)
                        or any(not isinstance(x, str) or x not in allowed_features for x in affected)
                        or (u.get('view_ids') and not affected) or original.get('status') == 'conflict'):
                    issue('UNKNOWN', 'per-unknown source/feature disposition required: ' + uid)
        if u.get('critical') is True:
            issue('UNKNOWN', 'critical unknown: ' + uid)
    if require_all:
        for uid in sorted(source.keys() - seen):
            issue('UNKNOWN', 'source unknown omitted from expression: ' + uid)
    return issues


def expression_context(plan: dict) -> str:
    """Bind all technical content, retaining an immutable full original separately.

    Operator receipts and display-only edits are not new source geometry. The
    original full required-plan hash is still checked against its saved bytes.
    """
    from .linked_edits import stable_display_context
    p = copy.deepcopy(plan)
    for field in ('revision', 'title', 'specification_labels', 'specification_labels_complete'):
        p.pop(field, None)
    for view in p.get('views', []):
        for field in ('annotations', 'reference_bundle_approval', 'source_geometry_approval',
                      'expression_approval', 'reference_preparation'):
            view.pop(field, None)
        for field in ('title', 'purpose', 'instruction'):
            if isinstance(view.get(field), str):
                view[field] = stable_display_context(view[field], plan.get('parts', []))
    for part in p.get('parts', []):
        part['name'] = ''; part['label'] = ''
    return digest(p)


def validate_expression_contract(contract: dict, source_manifest: dict, required_plan: dict) -> dict:
    issues = []
    def issue(verdict, message):
        issues.append({'rule': 'EXPRESSION-CONTRACT', 'verdict': verdict, 'message': message})
    allowed = {'schema_version', 'mode', 'source_sha256', 'required_plan_sha256', 'facts',
               'unknowns', 'conventions', 'view_requirements'}
    if not isinstance(contract, dict) or set(contract) != allowed:
        issue('FAIL', 'expression fields/schema invalid')
        return {'status': 'FAIL', 'contract': None, 'issues': issues, 'contract_sha256': None}
    if len(canonical(contract).encode()) > 200_000:
        issue('FAIL', 'expression capacity exceeded')
    if contract['schema_version'] != 1 or contract['mode'] not in {'source_contour', 'relational_schematic'}:
        issue('FAIL', 'unsupported expression mode')
    sources = source_manifest.get('sources', [])
    if (not sources or any(s.get('visibility', 'generation_visible') != 'generation_visible' for s in sources)
            or contract['source_sha256'] != digest([s.get('sha256') for s in sources])
            or contract['source_sha256'] != required_plan.get('source_sha256')):
        issue('FAIL', 'source identity or visibility differs')
    if contract['required_plan_sha256'] != digest(required_plan):
        issue('FAIL', 'complete original required plan identity differs')
    pages = {p['id']: p for p in source_manifest.get('pages', [])}
    source_hashes = {s.get('sha256') for s in sources}
    for key, capacity in [('facts', 160), ('unknowns', 80), ('conventions', 80), ('view_requirements', 24)]:
        if not isinstance(contract[key], list) or len(contract[key]) > capacity:
            issue('FAIL', key + ' capacity/type invalid')
    if issues and any(not isinstance(contract[k], list) for k in ('facts', 'unknowns', 'conventions', 'view_requirements')):
        return {'status': 'FAIL', 'contract': None, 'issues': issues, 'contract_sha256': None}
    facts = set()
    part_ids={p['id']for p in required_plan.get('parts',[])}
    def ident(value):
        return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value)
    for f in contract['facts']:
        if not isinstance(f, dict) or not ident(f.get('id')) or f.get('id') in facts:
            issue('FAIL', 'fact identity invalid'); continue
        facts.add(f['id'])
        if set(f) != {'id', 'subject', 'predicate', 'object', 'certainty', 'evidence'}:
            issue('FAIL', 'fact fields invalid')
        if any(not isinstance(f.get(k), str) or not 1 <= len(f[k]) <= 160 for k in ('subject', 'predicate', 'object')):
            issue('FAIL', 'fact relation missing')
        if f.get('subject')not in part_ids or f.get('object')not in part_ids:
            issue('FAIL','typed source subject/object must identify actual stable plan parts')
        if f.get('certainty') != 'confirmed': issue('UNKNOWN', 'unconfirmed expression fact')
        evidence = f.get('evidence', [])
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= 12:
            issue('FAIL', 'source evidence required'); continue
        for e in evidence:
            if not isinstance(e, dict): issue('FAIL', 'invalid evidence'); continue
            pg = pages.get(e.get('page_id')); quote = e.get('quote')
            if (not pg or pg.get('source_sha256') not in source_hashes or not isinstance(quote, str)
                    or not quote.strip() or compact(quote) not in compact(pg.get('text', ''))):
                issue('FAIL', 'source quotation/page mismatch')
    view_ids = {v['id'] for v in required_plan.get('views', [])}
    for u in contract['unknowns']:
        if (not isinstance(u, dict) or not ident(u.get('id')) or type(u.get('critical')) is not bool
                or not isinstance(u.get('view_ids'), list) or set(u.get('view_ids', [])) - view_ids
                or not isinstance(u.get('text'), str) or not u['text'].strip()):
            issue('FAIL', 'unknown scope invalid'); continue
    issues.extend(unknown_disposition_issues(contract['unknowns'], required_plan,
                                           require_all=bool(contract['facts'])))
    parts = {p['id'] for p in required_plan.get('parts', [])}
    for c in contract['conventions']:
        if not isinstance(c, dict): issue('FAIL', 'convention invalid'); continue
        r = c.get('region'); ids = c.get('part_ids')
        if (set(c) != {'id', 'kind', 'part_ids', 'region', 'not_physical_feature'}
                or not ident(c.get('id')) or c.get('kind') not in {'local_omission', 'local_section', 'local_relation_section'}
                or c.get('not_physical_feature') is not True or not isinstance(ids, list)
                or not ids or set(ids) - parts or not isinstance(r, list) or len(r) != 4
                or any(type(x) not in (int, float) or not math.isfinite(x) or abs(x) > 10000 for x in r)
                or r[0] >= r[2] or r[1] >= r[3]):
            issue('FAIL', 'convention cannot invent physical geometry')
        elif c['kind']=='local_relation_section' and (len(ids)!=1 or r[2]-r[0]>80 or r[3]-r[1]>36):
            issue('FAIL', 'relation section must be limited to one plate and a bounded display window')
    seen = set()
    for v in contract['view_requirements']:
        if (not isinstance(v, dict) or set(v) != {'view_id', 'fact_ids'}
                or v.get('view_id') not in view_ids or v.get('view_id') in seen
                or not isinstance(v.get('fact_ids'), list) or set(v.get('fact_ids', [])) - facts):
            issue('FAIL', 'view facts/scope invalid'); continue
        seen.add(v['view_id'])
    if not seen: issue('FAIL', 'view obligation required')
    status = 'FAIL' if any(x['verdict'] == 'FAIL' for x in issues) else 'UNKNOWN' if issues else 'PASS'
    return {'status': status, 'contract': copy.deepcopy(contract), 'issues': issues,
            'contract_sha256': digest(contract)}

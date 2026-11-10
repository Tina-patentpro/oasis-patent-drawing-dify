"""Versioned source/plan/artifacts; Chatflow schedules one figure per call.

The disabled-by-default model gate is independent of user-authored JSON. Raw
provider results are saved before file message wrapping or set packaging.
"""
import base64
import copy
import hashlib
import io
import json
import re
import sqlite3
import uuid
import datetime
from functools import wraps
from pathlib import Path
from PIL import Image

from .annotations import compose, fingerprint, outside_regions_unchanged, regular_font
from .cn_policy import check_policy, recommend_views, suggest_types
from .delivery_v2 import package_drawings
from .diagrams import render_diagram, validate_diagram
from .errors import DrawingError, ImageDispatchNotStarted
from .materials import ingest_material
from .operation_store import canonical, digest
from .quality import check_set
from .scene import validate_scene
from .source_contract import validate_facts, validate_view_plan, validate_source_update
from .views import render_view


PIPELINE_VERSION='drawing-pipeline-v039-6'

def identity(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value):
        raise ValueError('unsafe identity')
    return value


def source_digest(bundle):
    return digest([x['sha256'] for x in bundle['sources']])


def from_legacy(plan, source_sha256):
    return {'schema_version': 'drawing-set-2', 'set_id': plan['set_id'], 'revision': plan['revision'],
        'source_sha256': source_sha256, 'title': plan['title'], 'patent_type': 'invention',
        'technical_types': suggest_types(plan['source_text']), 'parts': plan['parts'],
        'facts': [{'id': 'legacy_relation_' + str(i), 'status': 'unknown', 'text': canonical(r),
                   'origin': 'legacy relation claim; requires source confirmation'} for i, r in enumerate(plan.get('relations', []), 1)],
        'constraints': plan.get('constraints', []), 'unresolved': plan.get('unresolved', []),
        'views': [{'id': f['id'], 'title': f['title'], 'purpose': f['purpose'], 'engine': f['engine'],
            'requested_type': f['kind'], 'delivered_type': f['kind'], 'parts': f['parts'],
            'depends_on': f.get('depends_on', []), 'instruction': f.get('instruction', ''),
            **({'diagram': f['diagram']} if 'diagram' in f else {})} for f in plan['figures']],
        'legacy_origin_sha256': digest(plan)}


def serialized(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self.store.owner_lock(self.owner):
            return method(self, *args, **kwargs)
    return locked


class Workspace:
    def __init__(self, store, owner, allow_models=False, image_client=None, text_client=None):
        if not owner: raise ValueError('runtime owner required')
        self.store = store; self.owner = owner; self.allow_models = allow_models
        self.image_client = image_client; self.text_client = text_client

    @serialized
    def read(self, wid):
        return self.store.load(self.owner, 'workspace:' + identity(wid))

    @serialized
    def save(self, wid, state):
        self.store.save(self.owner, 'workspace:' + identity(wid), state)

    @serialized
    def ingest(self, files, previous_workspace=None, patent_type='invention', technical_types=None, application_stage='draft'):
        if previous_workspace: self.read(previous_workspace)
        bundle = ingest_material(files)
        originals = [{**self.store.put_artifact(self.owner, f['blob']), 'name': Path(f['name']).name} for f in files]
        for page in bundle['pages']:
            png = page.pop('png')
            page['image'] = self.store.put_artifact(self.owner, png) if png else None
        wid = uuid.uuid4().hex
        if patent_type not in {'invention', 'utility_model', 'design'} or application_stage not in {'draft', 'filed'}:
            raise ValueError('patent type/stage invalid')
        if technical_types: recommend_views(technical_types)
        root_source=self.resolve_source_family(bundle,previous_workspace)
        state = {'workspace_id': wid, 'bundle': bundle, 'source_sha256': source_digest(bundle),
            'original_files': originals, 'previous_workspace': previous_workspace, 'lineage_root_source_sha256': root_source, 'current': None, 'work': None,
            'history': [], 'technical_types': technical_types or suggest_types(bundle['text']),
            'explicit_technical_type': bool(technical_types), 'patent_type': patent_type, 'application_stage': application_stage}
        self.save(wid, state)
        return self.preview(wid)

    @serialized
    def preview(self, wid, current_only=False):
        s = self.read(wid); b = s['bundle']
        p = (s.get('current') or {}).get('plan') if current_only else (s.get('work') or {}).get('plan') or s.get('proposal') or (s.get('current') or {}).get('plan')
        result = {'workspace_id': wid, 'source_sha256': s['source_sha256'], 'figure_presence': b['figure_presence'],
            'strategy': b['strategy'], 'source_pages': [{'id': x['id'], 'image': x['image']} for x in b['pages']],
            'technical_types': s['technical_types'], 'recommendations': recommend_views(s['technical_types']),
            'patent_type': s.get('patent_type', 'invention'), 'application_stage': s.get('application_stage', 'draft'),
            'pending_revision': s['work']['plan']['revision'] if s.get('work') else None,
            'model_execution_enabled': self.allow_models, 'model_calls': 0,
            'message': {'modify_supplement': '发现原始附图：优先保留、根据交底修改并补充图集。',
                        'create_new': '未发现原始附图：按技术类型规划新的附图集。',
                        'review_source_figures': '存在扫描页/候选图形：先核对页图，不推断无附图。'}[b['strategy']]}
        if p:
            result.update(plan=p, parts=p['parts'], unresolved=p.get('unresolved', []), queue=[] if current_only else self.resume(wid)['queue'],
                budget={'image': 0 if current_only else s['work']['required_image_calls'] if s.get('work') else sum(v['engine'] == 'image' for v in p['views']), 'text': 0},
                views=[{'id': v['id'], 'requested_type': v['requested_type'], 'delivered_type': v['delivered_type'],
                        'engine': v['engine'], 'purpose': v.get('purpose', '')} for v in p['views']])
        return result

    @serialized
    def plan(self, wid, query):
        s = self.read(wid)
        if not self.allow_models or not self.text_client: raise ValueError('zero-model gate: planner execution disabled')
        from .images import prepare_reference
        images = [prepare_reference(self.store.get_artifact(self.owner, pg['image'])) for pg in s['bundle']['pages'] if pg.get('image')]
        current = s.get('current'); editing = query.startswith(('修改', '改号', '改名')) and current
        if current and query.startswith('改号') and current['plan'].get('part_mark_mode') == 'name_only':
            raise ValueError('name_only has no reference numbers; review a numbering proposal first; no model called')
        system = (Path(__file__).resolve().parents[1] / '_assets/drawing_set_v2_prompt.txt').read_text()
        user = canonical({'query': query, 'patent_type': s.get('patent_type', 'invention'),
            'technical_types': s['technical_types'], 'application_stage': s.get('application_stage', 'draft'),
            'source_bundle': s['bundle'], 'previous_plan': current['plan'] if editing else None,
            'recommendations': recommend_views(s['technical_types'])})
        revision = current['plan']['revision'] + 1 if editing else 1
        operation = 'plan_' + digest([wid, query, current['plan'] if editing else None])
        from .text_client import prepare_plan_payload
        prepare_plan_payload(system,user,images)
        called = False
        def produce():
            nonlocal called
            evidence = {}
            def capture(kind, raw):
                evidence[kind] = self.store.put_artifact(self.owner, raw)
                self.store.save(self.owner, operation + ':evidence', evidence)
            called = True
            result = self.text_client.plan(system, user, images=images, evidence_sink=capture)
            result['evidence'] = evidence
            return result
        result = self.store.run_once(self.owner, operation, {'input': digest(user), 'system': digest(system)}, produce,
            budget={'scope': wid + ':plan:r' + str(revision), 'kind': 'text', 'limit': 1})
        def pairs(items):
            d = {}
            for k, v in items:
                if k in d: raise ValueError('duplicate planner JSON key')
                d[k] = v
            return d
        proposal = json.loads(result['text'], object_pairs_hook=pairs)
        if not isinstance(proposal, dict): raise ValueError('planner must return object')
        if set(proposal) == {'changes'}:
            if not editing or not isinstance(proposal['changes'], list) or len(proposal['changes']) > 120:
                raise ValueError('invalid display-name/label patch')
            new = copy.deepcopy(current['plan']); parts = {p['id']: p for p in new['parts']}
            for c in proposal['changes']:
                if set(c) != {'part_id', 'field', 'value'} or c['part_id'] not in parts or c['field'] not in {'label', 'name'}:
                    raise ValueError('patch cannot modify source facts/unknowns')
                parts[c['part_id']][c['field']] = c['value']
            proposal = new
        from .planner_contract import normalize_proposal
        proposal, adaptation = normalize_proposal(proposal)
        from .marking import adapt_marking
        proposal, marking_receipt = adapt_marking(proposal, s['bundle'])
        adaptation['marking'] = marking_receipt
        self.store.save(self.owner,operation+':adaptation',adaptation)
        proposal['source_sha256'] = s['source_sha256']; proposal['revision'] = revision
        proposal['set_id'] = current['plan']['set_id'] if editing else 'drawing_' + wid[:12]
        self.validate(s, proposal)
        if editing:
            validate_source_update(current['plan'], proposal)
            for field in ['unresolved', 'constraints']:
                if any(x not in proposal.get(field, []) for x in current['plan'].get(field, [])): raise ValueError('unknown/constraint removed')
        entry={'revision':revision,'candidate_plan_sha256':digest(proposal),'adaptation':adaptation,'provider_evidence':result.get('evidence',{})}
        history=s.setdefault('planning_history',[])
        if entry not in history:history.append(entry)
        s['proposal'] = proposal;self.save(wid, s)
        return {'workspace_id': wid, 'plan': proposal, 'model_calls': int(called), 'request_id': result.get('request_id'), 'usage':result.get('usage'), 'usage_projection':result.get('usage_projection'), 'adaptation': adaptation,
            'message': '已根据原文和页图提出候选图单；格式或标签位置适配已留档，原响应保留；技术关系、缺件及图文一致性仍需核查。'}

    def validate(self, state, p):
        if len(canonical(p).encode()) > 400_000: raise ValueError('plan capacity exceeded')
        allowed = {'schema_version', 'set_id', 'revision', 'source_sha256', 'title', 'patent_type', 'technical_types',
            'parts', 'facts', 'views', 'constraints', 'unresolved', 'scene', 'specification_labels', 'application_stage', 'legacy_origin_sha256', 'planning_notes', 'specification_labels_complete', 'part_mark_mode', 'visibility_contract'}
        if set(p) - allowed or p.get('schema_version') != 'drawing-set-2': raise ValueError('unsupported plan fields/schema')
        if p.get('patent_type') != state.get('patent_type', 'invention'): raise ValueError('patent type differs from explicit workspace selection')
        if state.get('explicit_technical_type') and set(p.get('technical_types', [])) != set(state['technical_types']):
            raise ValueError('technical type differs from explicit workspace selection')
        if p.get('application_stage', 'draft') != state.get('application_stage', 'draft'): raise ValueError('application stage differs from explicit workspace selection')
        identity(p.get('set_id')); parts = p.get('parts', [])
        if p.get('source_sha256') != state['source_sha256']: raise ValueError('source identity changed')
        if not isinstance(p.get('title'), str) or not 1 <= len(p['title']) <= 160: raise ValueError('title invalid')
        if type(p.get('revision')) is not int or p['revision'] < 1: raise ValueError('revision invalid')
        if not isinstance(parts, list) or len(parts) > 120: raise ValueError('parts capacity')
        from .marking import validate_marking
        mark_errors = validate_marking(p, state['bundle'])
        if mark_errors: raise ValueError(canonical(mark_errors))
        ids = set()
        for part in parts:
            identity(part.get('id'))
            ids.add(part['id'])
        for field in ['unresolved', 'constraints']:
            if not isinstance(p.get(field, []), list) or len(p.get(field, [])) > 80 or any(not isinstance(x, str) or len(x) > 1000 for x in p.get(field, [])):
                raise ValueError('constraint/unknown capacity')
        if type(p.get('specification_labels_complete',False))is not bool:raise ValueError('specification completeness must be boolean')
        problems = validate_facts(state['bundle'], p.get('facts', []))
        problems += validate_view_plan(p.get('facts', []), p.get('scene', {}).get('instances', []), p.get('views', []))
        from .flow_fidelity import check_logic
        problems += check_logic(state['bundle'],p)
        problems += check_policy(p)
        if p.get('scene'): problems += validate_scene(p['scene'])
        views = {v['id']: v for v in p['views']}
        total_pixels = 0
        for v in views.values():
            identity(v['id'])
            if v.get('engine') not in {'source', 'vector', 'scene', 'image'} or set(v.get('parts', [])) - ids:
                raise ValueError('unsupported engine/unknown part')
            if v['engine'] == 'vector': validate_diagram({'parts': parts}, self.legacy_figure(v))
            if v['engine']=='image' and state['bundle']['figure_presence']=='present' and not v.get('source_region_id'):
                raise ValueError('source image redraw requires actual source reference region')
            if v['engine'] == 'source' or v.get('source_region_id'): self.source_region(state, v)
            if v['engine'] == 'scene' and not p.get('scene'): raise ValueError('explicit scene missing')
            if v['engine'] == 'vector':
                width, height = v['diagram']['width'], v['diagram']['height']
            elif v['engine'] == 'source':
                region, _ = self.source_region(state, v); b = region['bbox'];width, height = b[2] - b[0], b[3] - b[1]
            else:
                width, height = (1536, 1024) if v['engine'] == 'image' else (v.get('width', 1200), v.get('height', 900))
            total_pixels += width * height
            if total_pixels > 80_000_000: raise ValueError('aggregate decoded image capacity exceeded before execution')
            if v.get('annotations'):
                annotations = copy.deepcopy(v['annotations'])
                by_id = {part['id']: part for part in parts}
                for a in annotations:
                    if a.get('part_id') not in by_id: raise ValueError('unknown annotation part')
                    a['label'] = by_id[a['part_id']]['label']
                canvas = io.BytesIO();Image.new('RGB', (width, height), 'white').save(canvas, format='PNG')
                compose(canvas.getvalue(), annotations)
            if any(x not in views or x == v['id'] for x in v.get('depends_on', [])): raise ValueError('missing/cyclic view dependency')
        def visit(fid, path):
            if fid in path: raise ValueError('view dependency cycle')
            for other in views[fid].get('depends_on', []): visit(other, path | {fid})
        for fid in views: visit(fid, set())
        failures = [x for x in problems if x['verdict'] == 'FAIL']
        if failures: raise ValueError(canonical(failures))
        return problems

    @staticmethod
    def legacy_figure(view):
        return {'id': view['id'], 'parts': view['parts'], 'diagram': view['diagram']}

    def specs(self, p, *, pipeline=None):
        # Explicit combined-reference children consume an unnumbered parent
        # base. A parent's display-only edit must not redraw unrelated children.
        parent_bases = self.base_specs(p, pipeline=pipeline) if any(v.get('reference_selection') == 'guide_and_parent'
                                                for v in p['views']) else None
        return self._specs(p, parent_bases, pipeline=pipeline)

    def _specs(self, p, parent_bases=None, *, pipeline=None):
        parts = {x['id']: x for x in p['parts']}; views = {v['id']: v for v in p['views']}; result = {}
        def one(fid):
            if fid not in result:
                v = views[fid]
                result[fid] = digest({'pipeline':pipeline or PIPELINE_VERSION,'view': v, 'parts': [parts[x] for x in v['parts']], 'source': p['source_sha256'],
                    'facts': p.get('facts', []), 'scene': p.get('scene'), 'constraints': p.get('constraints', []),
                    'unresolved': p.get('unresolved', []), 'renderer': fingerprint(),
                    'part_mark_mode': p.get('part_mark_mode', 'numbered'),
                    'visibility_contract': p.get('visibility_contract', {}).get(fid),
                    'dependencies': {x: (parent_bases[x] if parent_bases and v['engine']=='image'
                        and v.get('reference_selection')=='guide_and_parent' else one(x))
                        for x in v.get('depends_on', [])}})
            return result[fid]
        for fid in views: one(fid)
        return result

    def base_specs(self, p, *, pipeline=None):
        base = copy.deepcopy(p)
        from .linked_edits import stable_display_context
        for view in base['views']:
            for field in ['title','purpose','instruction']:
                if isinstance(view.get(field),str):view[field]=stable_display_context(view[field],p['parts'])
        for part in base['parts']: part['label'] = ''; part['name'] = ''
        for v in base['views']:
            v.pop('annotations', None)
            if v['engine'] == 'vector':
                v['diagram']['primitives'] = [x for x in v['diagram'].get('primitives', []) if x.get('type') != 'label']
            elif v['engine'] == 'image':
                v.pop('diagram', None)
        return self._specs(base, pipeline=pipeline)

    def verified_previous_pipeline(self, current):
        if not current:
            return None
        plan, records = current['plan'], current['records']
        for previous in ('drawing-pipeline-v039-5', 'drawing-pipeline-v039-4'):
            if previous == PIPELINE_VERSION:
                continue
            specs = self.specs(plan, pipeline=previous)
            if set(records) != set(specs) or any(records[fid]['spec_sha256'] != key for fid, key in specs.items()):
                continue
            bases = self.base_specs(plan, pipeline=previous)
            if all(records[fid]['base_spec_sha256'] == key for fid, key in bases.items()):
                return previous
        return None

    @serialized
    def stage(self, wid, plan, image_budget=0):
        s = self.read(wid); p = copy.deepcopy(plan)
        previous_work = s.get('work')
        if previous_work:
            for v in previous_work['plan']['views']:
                if v['engine'] != 'image': continue
                op = 'render_' + digest([wid, v['id'], previous_work['specs'][v['id']]])
                attempts = s.setdefault('image_attempts', {}).setdefault(v['id'], [])
                if self.store.operation_status(self.owner, op)['state'] != 'not_started' and not any(x['operation_id'] == op for x in attempts):
                    attempts.append({'operation_id': op, 'spec_sha256': previous_work['specs'][v['id']], 'origin': 'legacy operation identity'})
            if previous_work['plan'] != p:
                s.setdefault('work_history', []).append(copy.deepcopy(previous_work))
        if p.get('schema_version') == 'drawing-set-1':
            p = from_legacy(p, s['source_sha256']);p['patent_type'] = s.get('patent_type', 'invention');p['application_stage'] = s.get('application_stage', 'draft')
            if s.get('explicit_technical_type'): p['technical_types'] = s['technical_types']
        from .linked_edits import sync_display_references
        current = s.get('current')
        p,display_updates=sync_display_references(current['plan'],p) if current else (p,[])
        pipeline = self.verified_previous_pipeline(current)
        if pipeline:
            previous = current['plan']
            legacy_contracts = previous.get('visibility_contract')
            legacy = copy.deepcopy(p)
            if legacy_contracts is not None:
                legacy['visibility_contract'] = legacy_contracts
            if (p.get('visibility_contract') in (None, legacy_contracts)
                    and self.base_specs(legacy, pipeline=pipeline) == self.base_specs(previous, pipeline=pipeline)):
                p = legacy
            else:
                pipeline = None
        from .visibility import build_visibility_contract
        contracts = p.get('visibility_contract', {}) if pipeline else {
            v['id']: build_visibility_contract(s['bundle'], p, v) for v in p['views'] if v['engine'] == 'image'}
        if p.get('visibility_contract') is not None and p['visibility_contract'] != contracts:
            raise ValueError('visibility contract differs from actual source obligations; re-plan explicitly')
        if contracts: p['visibility_contract'] = contracts
        self.validate(s, p)
        expected_revision = current['plan']['revision'] + 1 if current else 1
        if p['revision'] != expected_revision: raise ValueError('revision must be adjacent to current complete set')
        if current:
            validate_source_update(current['plan'], p)
            for field in ['unresolved', 'constraints']:
                if any(x not in p.get(field, []) for x in current['plan'].get(field, [])): raise ValueError('unknown/constraint removed')
        if current:
            prior = {part['id']: part for part in current['plan']['parts']}
            label_changed = any(part['id'] in prior and part['label'] != prior[part['id']]['label'] for part in p['parts'])
            if label_changed:
                for v in p['views']:
                    if v['engine'] != 'image' or v.get('annotations'): continue
                    old_record = current['records'].get(v['id'])
                    if not old_record: continue
                    try: approved = self.store.load(self.owner, 'image-anchors:' + wid + ':' + v['id'] + ':' + old_record['base']['sha256'])
                    except ValueError: continue
                    if approved['source_sha256'] != s['source_sha256'] or approved['base_spec_sha256'] != old_record['base_spec_sha256']:
                        raise ValueError('image anchor source/base identity changed')
                    v['annotations'] = copy.deepcopy(approved['annotations'])
        pending = s.get('work')
        if pending and pending.get('transaction') and pending['plan'] != p:
            def without_annotations(plan):
                stripped = copy.deepcopy(plan)
                for view in stripped['views']: view.pop('annotations', None)
                return stripped
            if without_annotations(pending['plan']) != without_annotations(p):
                raise ValueError('pending linked transaction must be completed before staging a different edit')
        specs = self.specs(p, pipeline=pipeline); old = current['records'] if current else {}
        queue = [fid for fid in specs if old.get(fid, {}).get('spec_sha256') != specs[fid]]
        bases = self.base_specs(p, pipeline=pipeline)
        required_images = sum(v['engine'] == 'image' and v['id'] in queue and old.get(v['id'], {}).get('base_spec_sha256') != bases[v['id']] for v in p['views'])
        if type(image_budget) is not int or image_budget < 0 or image_budget > 24: raise ValueError('invalid image budget')
        transaction = None
        if current:
            prior = {part['id']: part for part in current['plan']['parts']}
            changed_labels = [part for part in p['parts'] if part['id'] in prior and part['label'] != prior[part['id']]['label']]
            if changed_labels:
                if len(changed_labels) != 1: raise ValueError('one reviewed part label transaction at a time')
                if bases != self.base_specs(current['plan'], pipeline=pipeline):
                    raise ValueError('separate geometry changes from a label-only transaction; no hidden redraw')
                from .linked_edits import plan_label_change
                part = changed_labels[0]
                transaction = plan_label_change(current['plan'], current['records'], part['id'], part['label'])
        s['work'] = {'plan': p, 'specs': specs, 'base_specs': bases, 'queue': queue, 'image_budget': image_budget,
                     'required_image_calls': required_images, 'plan_sha256': digest(p)}
        if pipeline:
            if required_images:
                raise ValueError('previous pipeline permits only reuse of accepted bases')
            s['work']['spec_pipeline'] = pipeline
        if display_updates:s['work']['display_reference_updates']=display_updates
        if transaction: s['work']['transaction'] = transaction
        self.save(wid, s)
        return {'workspace_id': wid, 'queue': queue, 'plan_sha256': digest(p), 'required_image_calls': required_images,
                'source_strategy': s['bundle']['strategy'], 'unresolved': p.get('unresolved', []), 'model_calls': 0, 'dispatch_estimate':__import__('patent_drawing.budget_policy',fromlist=['estimate_dispatches']).estimate_dispatches(p,old,bases)}

    def source_region(self, s, view):
        region = next((x for x in s['bundle']['figure_candidates'] if x['id'] == view.get('source_region_id')), None)
        if region is None: raise ValueError('original source figure region missing')
        page = next(x for x in s['bundle']['pages'] if x['id'] == region['page_id'])
        return region, page

    def record_key(self, wid, fid, spec):
        return 'figure:' + digest([wid, fid, spec])

    def lineage_account(self):
        return (self.owner.rsplit(':',1)[0] if self.owner.count(':')>=2 else self.owner) + ':private-lineage'

    def resolve_source_family(self,bundle,previous_workspace=None):
        # Bind original document components, so adding an irrelevant text note
        # cannot reset a rejected task. Generic attachments are not new anchors.
        account=self.lineage_account()
        documents=[x for x in bundle['sources'] if Path(x.get('name','')).suffix.lower() not in {'.txt','.md'}]
        anchors=documents or bundle['sources'];roots=set()
        with self.store.owner_lock(account):
            for source in anchors:
                try:roots.add(self.store.load(account,'source-family:'+source['sha256'])['root'])
                except ValueError:pass
            if previous_workspace:
                old=self.read(previous_workspace);roots.add(old.get('lineage_root_source_sha256',old['source_sha256']))
            if len(roots)>1:raise ValueError('source spans existing task families; private lineage resolution required')
            root=next(iter(roots)) if roots else source_digest(bundle)
            for source in anchors:self.store.save(account,'source-family:'+source['sha256'],{'root':root})
        return root

    def lineage_context(self, state):
        account = self.owner.rsplit(':',1)[0] if self.owner.count(':')>=2 else self.owner
        root = state.get('lineage_root_source_sha256', state['source_sha256'])
        return account + ':private-lineage', digest(['drawing-family-036-r1', account, root])

    def _lineage_attempts(self, state):
        account, family = self.lineage_context(state)
        try: return self.store.load(account, 'family:' + family)['attempts']
        except ValueError: return []

    def _remember_lineage(self, state, attempt):
        self.resolve_source_family(state['bundle'],state.get('previous_workspace'))
        account, family = self.lineage_context(state)
        with self.store.owner_lock(account):
            rows = self._lineage_attempts(state)
            if not any(x['operation_id']==attempt['operation_id'] for x in rows):
                rows.append({**attempt,'operation_owner':self.owner,'source_sha256':state['source_sha256'],'lineage_sha256':family})
                self.store.save(account,'family:' + family,{'attempts':rows})

    @serialized
    def import_rejection_parent(self, wid, parent, raw_response):
        # Private operator import: exact raw refusal is readable, not a public plan field.
        s=self.read(wid);account,family=self.lineage_context(s)
        from .moderation_recovery import diagnose_rejection
        if parent.get('source_sha256')!=s['source_sha256'] or parent.get('response_sha256')!=hashlib.sha256(raw_response).hexdigest():raise ValueError('historical rejection source/raw mismatch')
        diagnosis=diagnose_rejection(parent['http_status'],raw_response,parent.get('request_id'))
        if diagnosis['state']!='provider_rejected':raise ValueError('historical rejection not resolved')
        evidence=self.store.put_artifact(self.owner,raw_response)
        self._remember_lineage(s,{**copy.deepcopy(parent),'external_state':'provider_rejected','response_ref':evidence,'imported':True})
        s.setdefault('rejection_parent_history',[]).append({'parent':copy.deepcopy(parent),'response_ref':evidence})
        self.save(wid,s)
        return {'lineage_sha256':family,'model_calls':0,'blocking_state':'provider_rejected'}

    def _blocked_attempt(self, state, fid):
        rows=state.get('image_attempts', {}).get(fid, []) + self._lineage_attempts(state)
        for attempt in reversed(rows):
            status = attempt.get('external_state') or self.store.operation_status(attempt.get('operation_owner',self.owner), attempt['operation_id'])['state']
            if status in {'not_started', 'local_preflight_rejected'}: continue
            if status == 'result_unknown' and not attempt.get('external_state'):
                account, family = self.lineage_context(state)
                owner = attempt.get('operation_owner', self.owner)
                key = 'local-no-send:' + digest([owner, attempt['operation_id']])
                try: proof = self.store.load(account, key)
                except ValueError: proof = None
                if (proof and proof['state'] == 'confirmed_not_dispatched'
                        and proof['lineage_sha256'] == family and proof['operation_id'] == attempt['operation_id']):
                    continue
            if status == 'response_saved': return None
            if status in {'provider_rejected', 'result_unknown', 'dispatching', 'response_saved_unusable'}:
                return {**attempt, 'state': status}
        return None

    @serialized
    def record_local_budget_rejection(self, wid, fid, evidence, native_terminal_raw, enabled_policy_raw, reviewer):
        """Append verified local non-dispatch evidence. Preserve the original operation and workspace."""
        from .operator_policy import verify_budget_non_dispatch
        fields = {'operation_id', 'run_id', 'policy_sha256', 'request_sha256', 'source_sha256',
                  'lineage_sha256', 'plan_sha256', 'native_terminal_sha256'}
        if not isinstance(evidence, dict) or set(evidence) != fields or not isinstance(reviewer, str) or not reviewer.strip():
            raise ValueError('private original-operation evidence and reviewer required')
        state = self.read(wid)
        work = state.get('work')
        if not work or fid not in work['specs']:
            raise ValueError('original staged figure required for reconciliation')
        account, family = self.lineage_context(state)
        operation_id = 'render_' + digest([wid, fid, work['specs'][fid]])
        with self.store.owner_lock(account):
            blocked = self._blocked_attempt(state, fid)
            if (not blocked or blocked['state'] != 'result_unknown' or blocked.get('external_state')
                    or blocked.get('operation_owner') != self.owner or blocked['operation_id'] != operation_id
                    or evidence['operation_id'] != operation_id or evidence['source_sha256'] != state['source_sha256']
                    or evidence['lineage_sha256'] != family or evidence['plan_sha256'] != digest(work['plan'])):
                raise ValueError('original pending operation/source/plan identity differs')
            request = self.store.load(self.owner, 'candidate:' + wid + ':' + fid)
            saved = self.store.load(self.owner, operation_id + ':evidence')
            raw = self.store.get_artifact(self.owner, request['request'])
            status = self.store.operation_status(self.owner, operation_id)
            if (set(saved) != {'request'} or saved['request'] != request['request']
                    or request['source_sha256'] != state['source_sha256'] or request['lineage_sha256'] != family
                    or status['error'] != {'category': 'result_unknown'}
                    or evidence['request_sha256'] != hashlib.sha256(raw).hexdigest()
                    or evidence['request_sha256'] != blocked['candidate_sha256']):
                raise ValueError('exact original request-only work evidence required')
            proof = verify_budget_non_dispatch(wid, fid, evidence, native_terminal_raw, enabled_policy_raw, raw)
            receipt = {**evidence, **proof, 'workspace_id': wid, 'view_id': fid, 'reviewer': reviewer,
                       'reviewed_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                       'state': 'confirmed_not_dispatched', 'original_state': 'result_unknown',
                       'native_terminal': self.store.put_artifact(account, native_terminal_raw),
                       'enabled_policy': self.store.put_artifact(account, enabled_policy_raw)}
            key = 'local-no-send:' + digest([self.owner, operation_id])
            self.store.save(account, key, receipt)
        return {'state': receipt['state'], 'proof_sha256': digest(receipt), 'model_calls': 0}

    @serialized
    def import_external_attempt(self, wid, attempt, blobs, reviewer):
        """Append a privately checked historical success without changing family gates."""
        from .external_attempts import validate_external_attempt
        s=self.read(wid)
        if not isinstance(reviewer,str) or not reviewer.strip():raise ValueError('private historical reviewer required')
        blocked=next((self._blocked_attempt(s,v['id'])for v in (s.get('work')or{}).get('plan',{}).get('views',[])
                      if v['engine']=='image' and self._blocked_attempt(s,v['id'])),None)
        if not blocked:raise ValueError('bound historical rejection required')
        lineage={**blocked,'source_sha256':s['source_sha256'],'lineage_sha256':self.lineage_context(s)[1]}
        checked=validate_external_attempt(attempt,blobs,lineage)
        if checked['status']!='PASS':raise ValueError(canonical(checked['issues']))
        verified=checked['verified_attempt'];key=verified['attempt_sha256']
        if any(r['attempt_sha256']==key for r in s.get('external_attempt_history',[])):
            return {'attempt_sha256':key,'model_calls':0,'scope':'historical_external_attempt_only'}
        refs={k:self.store.put_artifact(self.owner,raw)for k,raw in blobs.items()}
        receipt={**verified,'evidence_refs':refs,'reviewer':reviewer,
                 'reviewed_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
        self.store.save(self.owner,'external-attempt:'+wid+':'+key,receipt)
        s.setdefault('external_attempt_history',[]).append(receipt)
        self.save(wid,s)
        return {'attempt_sha256':key,'supplier_state':verified['supplier_state'],
                'semantic_state':verified['semantic_state'],'model_calls':0,'current_promoted':False,
                'ordinary_recovery_gate_changed':False}

    def _recovery_for(self, state, fid, blocked):
        receipt = state.get('recovery_approvals', {}).get(fid)
        if (blocked and blocked['state'] == 'provider_rejected' and receipt
                and receipt['previous_operation_id'] == blocked['operation_id']
                and receipt['source_sha256'] == state['source_sha256']
                and receipt.get('lineage_sha256') == self.lineage_context(state)[1]
                and receipt['authorization_id'] not in state.get('recovery_uses', {})):
            if receipt.get('kind') == 'approved_repair_run':
                from .build_identity import runtime_build_sha256
                q=receipt['qualification'];work=state.get('work')
                if not work or q.get('build_sha256')!=runtime_build_sha256():return None
                view=next(v for v in work['plan']['views']if v['id']==fid)
                try:
                    expression=self.expression_for(state,work.get('workspace_id') or receipt['qualification']['workspace_id'],view,work['plan'])
                except ValueError:return None
                if not expression or digest(expression)!=q.get('contract_sha256'):return None
            return receipt
        return None

    @serialized
    def authorize_repair_run(self, wid, authorization, reviewer):
        """One exact structural repair, after verified history and actual guide review."""
        from .build_identity import runtime_build_sha256
        from .external_attempts import validate_external_attempt, MEMBERS
        from .operator_policy import authorize_dispatch
        required={'kind','view_id','authorization_id','previous_operation_id','source_sha256','lineage_sha256',
                  'request_sha256','contract_sha256','guide_sha256','model','build_sha256','approval_record_sha256','approval_record'}
        if (not isinstance(authorization,dict) or set(authorization)!=required
                or authorization['kind']!='approved_repair_run' or not isinstance(reviewer,str) or not reviewer.strip()):
            raise ValueError('private exact repair authorization required')
        s=self.read(wid);fid=identity(authorization['view_id']);aid=identity(authorization['authorization_id'])
        if aid in s.get('recovery_uses',{}):raise ValueError('repair authorization consumed')
        blocked=self._blocked_attempt(s,fid)
        if not blocked or blocked['state']!='provider_rejected':raise ValueError('UNKNOWN or missing original refusal; inspect original')
        source=s['source_sha256'];family=self.lineage_context(s)[1]
        if (authorization['previous_operation_id']!=blocked['operation_id'] or authorization['source_sha256']!=source
                or authorization['lineage_sha256']!=family or authorization['model']!='gpt-image-2.6-flare'
                or authorization['build_sha256']!=runtime_build_sha256()):
            raise ValueError('repair source/family/model/build mismatch')
        history=[e for e in s.get('external_attempt_history',[])if e['previous_operation_id']==blocked['operation_id']]
        if not history:raise ValueError('verified historical external success required')
        external=history[-1]
        fields={'kind','operation_id','previous_operation_id','source_sha256','lineage_sha256','model','request_id'}|{k+'_sha256'for k in MEMBERS}
        original={k:external[k]for k in fields}
        blobs={k:self.store.get_artifact(self.owner,external['evidence_refs'][k])for k in MEMBERS}
        result=validate_external_attempt(original,blobs,{**blocked,'source_sha256':source,'lineage_sha256':family})
        if result['status']!='PASS' or digest(original)!=external['attempt_sha256']:
            raise ValueError('historical external evidence changed')
        work=s.get('work');view=next(v for v in work['plan']['views']if v['id']==fid) if work else None
        expression=self.expression_for(s,wid,view,work['plan']) if view else None
        references=self.reference_bundle(s,wid,view,work['plan']) if expression else None
        if not references:raise ValueError('private expression and actual guide approval required')
        guide=self.store.load(self.owner,'prepared-guide:'+wid+':'+fid+':'+references['receipt']['guide_id'])
        if (authorization['contract_sha256']!=digest(expression)
                or authorization['guide_sha256']!=guide['metadata']['guide_sha256']):
            raise ValueError('repair contract/guide mismatch')
        candidate=self.prepare_candidate(wid,fid)
        if candidate['request_sha256']!=authorization['request_sha256'] or candidate['request_sha256']==original['request_sha256']:
            raise ValueError('repair candidate exact bytes differ or repeat unchanged diagnostic')
        approval_raw=authorization['approval_record']
        if (not isinstance(approval_raw,bytes) or len(approval_raw)>1024*1024
                or hashlib.sha256(approval_raw).hexdigest()!=authorization['approval_record_sha256']):
            raise ValueError('private repair approval bytes/hash mismatch')
        approved=json.loads(approval_raw)
        if (approved.get('kind')!='approved_repair_run' or approved.get('approved')is not True
                or approved.get('request_sha256')!=candidate['request_sha256'] or not approved.get('scope')):
            raise ValueError('exact approved repair scope required')
        proof={'external_attempt_sha256':external['attempt_sha256'],
               'operator_approval_sha256':authorization['approval_record_sha256'],
               'contract_sha256':digest(expression),'guide_sha256':guide['metadata']['guide_sha256'],
               'build_sha256':authorization['build_sha256'],'model':authorization['model'],'workspace_id':wid,
               'official_feedback':False,'kind':'approved_repair_run'}
        evidence_sha=digest(proof)
        receipt={'kind':'approved_repair_run','authorization_id':aid,'previous_operation_id':blocked['operation_id'],
                 'source_sha256':source,'lineage_sha256':family,'candidate_sha256':candidate['request_sha256'],
                 'reason':'Source-bound structural repair under an exact private operator approval',
                 'evidence_sha256':evidence_sha,'allowed_action':'image_root',
                 'issued_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
        receipt['qualification']={**proof,**{k:receipt[k]for k in ('previous_operation_id','source_sha256','lineage_sha256','candidate_sha256','evidence_sha256')},'eligible':True}
        authorize_dispatch({**blocked,'source_sha256':source,'lineage_sha256':family},candidate['request_sha256'],receipt)
        artifact=self.store.put_artifact(self.owner,canonical(receipt).encode())
        self.store.put_artifact(self.owner,approval_raw)
        s.setdefault('recovery_history',[]).append({'receipt':receipt,'artifact':artifact,'reviewer':reviewer})
        s.setdefault('recovery_approvals',{})[fid]=receipt;self.save(wid,s)
        return receipt

    @serialized
    def approve_recovery(self, wid, fid, authorization):
        # Private operator action; never an SDK tool parameter or model action.
        s = self.read(wid); blocked = self._blocked_attempt(s, fid)
        keys = {'authorization_id', 'previous_operation_id', 'reason', 'evidence_sha256', 'allowed_action', 'issued_at','candidate_sha256','lineage_sha256','source_sha256','evidence'}
        if not isinstance(authorization, dict) or set(authorization) != keys: raise ValueError('invalid recovery receipt')
        identity(authorization['authorization_id'])
        if not blocked or blocked['state'] != 'provider_rejected': raise ValueError('UNKNOWN or unfailed operation cannot receive rejection recovery')
        if authorization['previous_operation_id'] != blocked['operation_id']: raise ValueError('recovery original operation differs')
        if authorization['allowed_action'] != 'image_root' or not re.fullmatch(r'[a-f0-9]{64}', authorization['evidence_sha256']): raise ValueError('recovery evidence/scope invalid')
        if not isinstance(authorization['reason'], str) or not 10 <= len(authorization['reason']) <= 1000: raise ValueError('recovery reason required')
        if datetime.datetime.fromisoformat(authorization['issued_at']).tzinfo is None: raise ValueError('recovery time needs timezone')
        if authorization['authorization_id'] in s.get('recovery_uses', {}): raise ValueError('recovery authorization consumed')
        p = s['work']['plan']; view = next(v for v in p['views'] if v['id'] == fid)
        if any(v['id'] in view.get('depends_on', []) and v['engine'] == 'image' for v in p['views']): raise ValueError('root image recovery required')
        from .moderation_recovery import recovery_eligibility
        candidate=self.store.load(self.owner,'candidate:' + wid + ':' + fid)
        if any(authorization.get(k)!=candidate[k] for k in ['candidate_sha256'] if k in candidate):raise ValueError('candidate mismatch')
        candidate['facts_sha256']=digest(p.get('facts',[]))
        original={**blocked,'operation_id':blocked['operation_id'],'source_sha256':blocked.get('source_sha256',s['source_sha256']),'lineage_sha256':self.lineage_context(s)[1]}
        qualification=recovery_eligibility(original,candidate,authorization['evidence'])
        from .operator_policy import authorize_dispatch
        clean={k:copy.deepcopy(v) for k,v in authorization.items() if k!='evidence'}
        clean['qualification']=qualification
        authorize_dispatch({**original,'source_sha256':s['source_sha256']},candidate['request_sha256'],clean)
        for e in authorization['evidence']:self.store.put_artifact(self.owner,e['blob'])
        receipt = {**clean, 'source_sha256': s['source_sha256'], 'workspace_id': wid, 'view_id': fid}
        artifact = self.store.put_artifact(self.owner, canonical(receipt).encode())
        s.setdefault('recovery_history', []).append({'receipt': receipt, 'artifact': artifact})
        s.setdefault('recovery_approvals', {})[fid] = receipt; self.save(wid, s)
        return {'authorization_id': receipt['authorization_id'], 'new_attempt_permitted': True, 'old_state': 'provider_rejected', 'model_calls': 0}

    @serialized
    def image_dispatch_count(self, wid):
        state = self.read(wid)
        return sum(self.store.dispatch_count(self.owner, a['operation_id']) for rows in state.get('image_attempts', {}).values() for a in rows)

    @serialized
    def current_review_block(self, wid):
        from .visibility import ACTUAL_CONTRACT, matching_review, review_passed
        state = self.read(wid)
        current = state.get('current')
        if not current:
            return None
        records = current['records']
        plan = current['plan']
        for view in plan['views']:
            fid = view['id']
            contract = plan.get('visibility_contract', {}).get(fid, {})
            if contract.get('contract') != ACTUAL_CONTRACT:
                continue
            record = records[fid]
            review = matching_review(state, fid, record, contract)
            failure = any(x['verdict'] == 'FAIL' for x in (review or {}).get('features', []))
            if not review_passed(review, record, contract):
                return {'blocking_state': 'technical_failed' if failure else 'awaiting_visual_review',
                        'blocked_view': fid, 'message': f'{fid} 的当前实际图审阅未通过，整套导出暂停。'}
            if contract['requires_parent']:
                try:
                    parent, parent_record = self.reviewed_image_parent(state, wid, view, current)
                    self.validate_parent_binding(state, wid, view, current, record, parent, parent_record)
                except ValueError:
                    return {'blocking_state': 'awaiting_visual_review', 'blocked_view': fid,
                            'stale_parent_review': True, 'message': f'{fid} 的实际母图审阅身份已变化，整套导出暂停。'}
        return None

    @serialized
    def execution_state(self, wid):
        s = self.read(wid); w = s.get('work')
        result = {'blocking_state': None, 'blocked_view': None, 'route': 'execute', 'model_calls': 0}
        if not w:
            current_block = self.current_review_block(wid)
            return {**result, 'route': 'reply', 'message': '没有待执行修订。', **(current_block or {})}
        p = w['plan']; records = self.partial(wid)['figures']
        for v in p['views']:
            if v['engine'] != 'image': continue
            fid = v['id']; contract = p.get('visibility_contract', {}).get(fid)
            blocked = self._blocked_attempt(s, fid)
            recovery = self._recovery_for(s, fid, blocked) if blocked else None
            if blocked and not recovery:
                return {**result, 'route': 'reply', 'blocking_state': blocked['state'], 'blocked_view': fid,
                        'message': '本图请求被供应商拒绝或结果尚待核对；依赖图停止，原始记录保留，不自动重试。'}
            if v.get('expression_mode') == 'relational_schematic':
                try: expression = self.expression_for(s, wid, v, p)
                except ValueError: expression = None
                if not expression:
                    return {**result, 'route':'reply', 'blocking_state':'needs_expression', 'blocked_view':fid,
                            'message':'空间表达合同待私有核对；未知轮廓不会自动补全。'}
                reused=(s.get('current')or{}).get('records',{}).get(fid,{}).get('base_spec_sha256')==w['base_specs'][fid]
                try: reference = True if reused else self.reference_bundle(s, wid, v, p)
                except ValueError: reference = None
                if not reference:
                    return {**result, 'route':'reply', 'blocking_state':'awaiting_guide_review', 'blocked_view':fid,
                            'message':'先准备并核对空间引导及引用，尚未批准模型执行。'}
            if contract and contract['drawability'] == 'needs_source':
                return {**result, 'route': 'reply', 'blocking_state': 'needs_source', 'blocked_view': fid,
                        'message': '来源不足以确认本图空间关系；请补充来源草图或接口说明，已有图件保留。'}
            if recovery and fid in self.resume(wid)['queue']:return result
            if v.get('expression_mode') != 'relational_schematic' and fid not in records and not ((s.get('current') or {}).get('records',{}).get(fid,{}).get('base_spec_sha256')==w['base_specs'][fid]) and s['bundle']['figure_presence']=='present' and (v.get('delivered_type')=='spatial' or any(f['feature_id'].startswith('spring-support-') for f in (contract or {}).get('features',[]))):
                try:geometry=self.source_geometry(s,wid,v,p)
                except ValueError:geometry=None
                if not geometry:return {**result,'route':'reply','blocking_state':'needs_source','blocked_view':fid,'message':'空间补图需要来源可核的外包络、内外属性和未知轮廓；模型不能自行批准。'}
                try:bundle=self.reference_bundle(s,wid,v,p)
                except ValueError:bundle=None
                if not bundle:return {**result,'route':'reply','blocking_state':'awaiting_guide_review','blocked_view':fid,'message':'来源几何已保存；需预览并私有审阅引导、参考顺序和markup后继续。'}
            image_parents=[x['id']for x in p['views']if x['id']in v.get('depends_on',[])and x['engine']=='image']
            if (v.get('reference_selection')=='guide_and_parent' and not w.get('transaction')
                    and image_parents and all(x in records for x in image_parents)):
                try:
                    parent,parent_record=self.reviewed_image_parent(s,wid,v,w)
                    if fid in records:self.validate_parent_binding(s,wid,v,w,records[fid]['record'],parent,parent_record)
                except ValueError as error:
                    from .visibility import ACTUAL_CONTRACT
                    actual = (contract or {}).get('contract') == ACTUAL_CONTRACT and fid in records
                    return {**result,'route':'reply','blocking_state':'awaiting_visual_review' if actual else 'needs_source',
                            'stale_parent_review':actual,'technical_status':'UNKNOWN',
                            'blocked_view':fid,'message':fid+' 的母图或祖先引用已失效，停止依赖派发：'+str(error)}
            if fid in records:
                from .visibility import matching_review
                actual=matching_review(s,fid,records[fid]['record'],contract) if contract else None
                if actual and any(f['verdict']=='FAIL' for f in actual['features']):
                    return {**result,'route':'reply','blocking_state':'technical_failed','technical_status':'FAIL','blocked_view':fid,'message':'实际图件技术审阅失败；保留失败母图并停止依赖图及整套提升。'}
                from .visibility import review_passed
                if not contract or not review_passed(s.get('visual_reviews', {}).get(fid), records[fid]['record'], contract):
                    return {**result, 'route': 'reply', 'blocking_state': 'awaiting_visual_review', 'blocked_view': fid,
                            'message': '本图已保存，需核对实际图件可见性并取得技术审阅后继续；未宣称整套通过。'}
                if v.get('annotations'):
                    try: self._anchor_receipt(s, wid, fid, records[fid]['record'], v['annotations'], require_output=True)
                    except ValueError:
                        return {**result, 'route': 'reply', 'blocking_state': 'pending_edit', 'blocked_view': fid,
                                'message': '无编号底图已保存；标注需操作员核对实际底图锚点，图单中的reviewed字样不是批准。'}
        if w.get('transaction'):
            for requirement in w['transaction']['required_approvals']:
                fid = requirement['view_id']
                if fid not in self.resume(wid)['queue']: continue
                try:
                    if requirement['kind'] == 'image_anchor':
                        self.store.load(self.owner, 'image-anchors:' + wid + ':' + fid + ':' + requirement['base_sha256'])
                    else:
                        self.store.load(self.owner, 'source-label:' + wid + ':' + fid + ':' + str(p['revision']) + ':' + w['transaction']['part_id'])
                except ValueError:
                    return {**result, 'route': 'reply', 'blocking_state': 'pending_edit', 'blocked_view': fid,
                            'message': '整套关联修改待实际底图锚点或原嵌字区域批准；已完成局部保留，当前版本不提升。'}
        return result

    @serialized
    def preflight(self, wid):
        result=self.execution_state(wid)
        s = self.read(wid)
        work = s.get('work')
        pending = self.resume(wid)['queue']
        saved_response = False
        if result.get('blocking_state') in {'needs_source','needs_expression','awaiting_guide_review'}:
            if work and pending:
                v=next(v for v in work['plan']['views'] if v['id']==pending[0])
                old_parts={p['id']:p for p in (s.get('current') or {}).get('plan',{}).get('parts',[])}
                parts={p['id']:p for p in work['plan']['parts']}
                unchanged=not old_parts or all(old_parts.get(pid)==parts[pid] for pid in v['parts'])
                views={v['id']:v for v in work['plan']['views']};available=self.partial(wid)['figures']
                def approved_dependencies(fid,visiting):
                    if fid in visiting:return False
                    for parent in views[fid].get('depends_on',[]):
                        if parent in pending or parent not in available:return False
                        row=available[parent]['record'];pv=views[parent]
                        if row['spec_sha256']!=work['specs'][parent] or row['base_spec_sha256']!=work['base_specs'][parent]:return False
                        if pv['engine']=='image':
                            from .visibility import matching_review,review_passed
                            contract=work['plan'].get('visibility_contract',{}).get(parent)
                            if not contract or not review_passed(matching_review(s,parent,row,contract),row,contract):return False
                            if pv.get('annotations'):
                                try:self._anchor_receipt(s,wid,parent,row,pv['annotations'],require_output=True)
                                except ValueError:return False
                        if not approved_dependencies(parent,visiting|{fid}):return False
                    return True
                if v['engine']=='source' and approved_dependencies(v['id'],set()) and unchanged:
                    result.update(deferred_blocking_state=result['blocking_state'],deferred_blocked_view=result['blocked_view'],
                        blocking_state=None,blocked_view=None,route='execute',
                        message='先保存独立原图；空间补图仍待来源确认，不会生成未知结构。')
        if not result.get('blocking_state') and self.allow_models and work and pending:
            fid = pending[0]
            view = next(v for v in work['plan']['views'] if v['id'] == fid)
            current = (s.get('current') or {}).get('records', {}).get(fid, {})
            if view['engine'] == 'image' and current.get('base_spec_sha256') != work['base_specs'][fid]:
                recovery = self._recovery_for(s, fid, self._blocked_attempt(s, fid))
                operation = self._saved_image_operation(wid, fid, work['specs'][fid], s)
                saved_response = bool(operation)
                if not operation:
                    operation = 'render_' + digest([wid, fid, work['specs'][fid]] +
                                                    ([recovery['authorization_id']] if recovery else []))
                try:
                    self._validate_new_image_dispatch(wid, s, work, operation)
                except DrawingError:
                    result.update(blocking_state='local_preflight_rejected', blocked_view=fid, route='reply',
                                  message='本地图像预算前置检查未通过，未发送图像请求。请由运营方核对整套估算与剩余额度。')
        result.update(workspace_id=wid,model_calls=0)
        result.setdefault('technical_status','UNKNOWN')
        state=result.get('blocking_state')
        actions={'needs_source':'supply_source_geometry','awaiting_guide_review':'private_guide_review',
                 'local_preflight_rejected':'inspect_operator_budget',
                 'technical_failed':'correct_source_or_reviewed_candidate','provider_rejected':'qualifying_evidence_and_private_recovery',
                 'result_unknown':'inspect_original_result','dispatching':'inspect_original_result',
                 'awaiting_visual_review':'private_actual_image_review','pending_edit':'private_actual_base_anchor_review'}
        result['resume_action']=actions.get(state,'execute_next_view' if result.get('route')=='execute' else 'inspect_current')
        if saved_response:
            result.update(resume_action='resume_local_composition',
                          message='供应商响应已保存，待本地合成；续作使用已保存响应。')
        result['done']='yes' if state or not self.resume(wid)['queue'] else 'no'
        result.setdefault('message','前置检查通过；尚未调用模型，逐图结果仍须实际审阅。')
        s=self.read(wid);holder=s.get('work')or s.get('current')or{};plan=holder.get('plan',{})
        images=[v for v in plan.get('views',[])if v['engine']=='image']
        focus=next((v for v in images if v['id']==result.get('blocked_view')),images[0]if images else{})
        result['expression_mode']=focus.get('expression_mode','source_contour'if images else'')
        actual={k:v['record']for k,v in self.partial(wid)['figures'].items()}if s.get('work')else holder.get('records',{})
        result['supplier_state']='response_saved'if saved_response or any(actual.get(v['id'],{}).get('request_id')for v in images)else'not_called'
        if state=='local_preflight_rejected':result['supplier_state']='not_called'
        elif state in {'result_unknown','dispatching'}:result['supplier_state']='result_unknown'
        elif state=='provider_rejected':
            blocked=self._blocked_attempt(s,focus.get('id'))
            historical=any(x.get('previous_operation_id')==(blocked or{}).get('operation_id')for x in s.get('external_attempt_history',[]))
            result['supplier_state']='historical_success'if historical else'provider_rejected'
        elif result['supplier_state']=='not_called' and s.get('external_attempt_history'):
            result['supplier_state']='historical_success'
        if state=='technical_failed':semantic='FAIL'
        elif state in {'awaiting_visual_review','result_unknown','dispatching'}:semantic='UNKNOWN'
        elif state=='pending_edit':semantic='PENDING_EDIT'
        elif state:semantic='BLOCKED'
        elif images and all(v['id']in actual for v in images):semantic='PASS'
        else:semantic='NOT_RENDERED'
        result['semantic_state']=semantic
        action={'needs_expression':'private_review','needs_source':'inspect_original','technical_failed':'private_review',
                'local_preflight_rejected':'operator_budget',
                'awaiting_visual_review':'actual_review','pending_edit':'linked_edit','provider_rejected':'inspect_original',
                'result_unknown':'inspect_original','dispatching':'inspect_original'}.get(state,'none')
        if state=='awaiting_guide_review':
            fid = result['blocked_view']
            guide_id = s.get('prepared_guides', {}).get(fid)
            action = 'prepare_guide'
            if guide_id:
                try:
                    guide = self._prepared_guide(s, wid, focus, guide_id)
                    if digest(guide) != guide_id:
                        raise ValueError('guide review stale')
                    geometry = self.source_geometry(s, wid, focus, plan) if result['expression_mode'] == 'relational_schematic' else None
                except ValueError:
                    pass
                else:
                    action = 'private_review'
                    from .source_profiles import registered_relation_convention_ids
                    if registered_relation_convention_ids((geometry or {}).get('assembly_registration')):
                        action = result['resume_action'] = 'professional_relation_review'
                        result['message'] = '局部连接示意需绑定原交底和本版引导图的专业审阅回执。'
            if action == 'prepare_guide':
                result['resume_action'] = action
        result['pending_action']=action
        from .visibility import review_todos, review_todo_message
        result.update(review_todos(s, holder, actual))
        if result.get('stale_parent_review') and result['blocked_view'] not in result['unreviewed_view_ids']:
            result['unreviewed_view_ids'].append(result['blocked_view'])
        result['missing_ids'] = self.partial(wid)['missing_ids'] if s.get('work') else []
        if state in {'technical_failed', 'awaiting_visual_review'}:
            result['message'] += ' ' + review_todo_message(result)
        return result

    def _saved_image_operation(self, wid, fid, spec, state):
        ordinary = 'render_' + digest([wid, fid, spec])
        if self.store.operation_status(self.owner, ordinary)['state'] == 'response_saved':
            return ordinary
        uses = state.get('recovery_uses', {})
        for attempt in reversed(state.get('image_attempts', {}).get(fid, [])):
            authority = attempt.get('recovery_authorization_id')
            if not authority or attempt['spec_sha256'] != spec:
                continue
            operation = 'render_' + digest([wid, fid, spec, authority])
            if (attempt['operation_id'] == uses.get(authority) == operation
                    and self.store.operation_status(self.owner, operation)['state'] == 'response_saved'):
                return operation
        return None

    def _validate_new_image_dispatch(self, wid, state, work, operation_id):
        if (not hasattr(self.image_client, 'validate_dispatch_plan')
                or self.store.operation_status(self.owner, operation_id)['state'] not in {
                    'not_started', 'local_preflight_rejected'}):
            return
        from .budget_policy import estimate_dispatches
        current = (state.get('current') or {}).get('records', {})
        views = estimate_dispatches(work['plan'], current, work['base_specs'])['required_views']
        remaining = sum(not self._saved_image_operation(wid, view, work['specs'][view], state) for view in views)
        try:
            self.image_client.validate_dispatch_plan(digest(work['plan']), remaining)
        except (ValueError, OSError, sqlite3.Error) as error:
            message = str(error) if isinstance(error, ValueError) else 'private image estimate or ledger unreadable'
            raise DrawingError('local_preflight_rejected', message) from error
        return remaining

    @serialized
    def review_status(self, wid):
        return self.preflight(wid)

    @serialized
    def approve_expression(self, wid, fid, contract, reviewer):
        """Private source/expression review; absent from public tool actions."""
        from .expression_contract import validate_expression_contract, expression_context
        s = self.read(wid); work = s.get('work')
        if not work or not isinstance(reviewer, str) or not reviewer.strip():
            raise ValueError('staged expression and private reviewer required')
        view = next(v for v in work['plan']['views'] if v['id'] == fid)
        check = validate_expression_contract(contract, s['bundle'], work['plan'])
        if (check['status'] != 'PASS' or view['engine'] != 'image'
                or view.get('expression_mode', 'source_contour') != contract['mode']
                or fid not in {v['view_id'] for v in contract['view_requirements']}):
            raise ValueError('expression/source scope not approved: ' + canonical(check['issues']))
        receipt = {'contract': copy.deepcopy(contract), 'required_plan': copy.deepcopy(work['plan']),
                   'context_sha256': expression_context(work['plan']), 'source_sha256': s['source_sha256'],
                   'view_id': fid, 'reviewer': reviewer,
                   'reviewed_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}
        approval = digest(receipt)
        self.store.save(self.owner, 'expression:' + wid + ':' + fid + ':' + approval, receipt)
        s.setdefault('expression_approvals', {})[fid] = approval
        s.setdefault('expression_history', []).append({'approval_id': approval,
            'artifact': self.store.put_artifact(self.owner, canonical(receipt).encode())})
        self.save(wid, s)
        return {'approval_id': approval, 'model_calls': 0, 'professional_acceptance': 'not_reviewed'}

    def expression_for(self, s, wid, view, plan):
        from .expression_contract import validate_expression_contract, expression_context
        approval = s.get('expression_approvals', {}).get(view['id'])
        if not approval: return None
        identity(approval)
        receipt = self.store.load(self.owner, 'expression:' + wid + ':' + view['id'] + ':' + approval)
        check = validate_expression_contract(receipt['contract'], s['bundle'], receipt['required_plan'])
        if (digest(receipt) != approval or receipt['source_sha256'] != s['source_sha256']
                or receipt['view_id'] != view['id'] or check['status'] != 'PASS'
                or receipt['context_sha256'] != expression_context(plan)):
            raise ValueError('expression approval stale')
        return receipt['contract']

    def member_requirements_for(self,s,wid,view,plan):
        scene=view.get('expression_scene',{})
        if not any(isinstance(r,dict)and r.get('shape')=='sleeved_support' for r in scene.get('instances',[])):
            return None
        # Reuse the verified original receipt, preserving display-only identity
        # semantics instead of rewriting its complete required-plan hash.
        expression=self.expression_for(s,wid,view,plan)
        if not expression:raise ValueError('private member expression required')
        receipt=self.store.load(self.owner,'expression:'+wid+':'+view['id']+':'+s['expression_approvals'][view['id']])
        from .member_visibility import derive_member_visibility_requirements
        return derive_member_visibility_requirements(s['bundle'],plan,view,expression,scene,
                                                       approved_plan=receipt['required_plan'])

    @serialized
    def approve_source_geometry(self, wid, fid, geometry, reviewer):
        # Private operator entry: never public plan/tool authority.
        s = self.read(wid); work = s.get('work')
        if not work or not isinstance(reviewer, str) or not reviewer.strip(): raise ValueError('staged source geometry and reviewer required')
        view = next(v for v in work['plan']['views'] if v['id'] == fid)
        if view['engine'] != 'image': raise ValueError('image source geometry required')
        from .source_geometry import validate_source_geometry
        if validate_source_geometry(geometry, s['bundle'], work['plan']): raise ValueError('source geometry identity invalid')
        if set(view['parts']) - {a['part_id'] for a in geometry['assets']}: raise ValueError('source geometry does not cover requested parts')
        receipt = {'geometry': copy.deepcopy(geometry), 'reviewer': reviewer, 'source_sha256': s['source_sha256'],
                   'view_id': fid, 'reviewed_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}
        approval = digest(receipt); artifact = self.store.put_artifact(self.owner, canonical(receipt).encode())
        self.store.save(self.owner, 'source-geometry:' + wid + ':' + fid + ':' + approval, receipt)
        s.setdefault('source_geometry_history', []).append({'approval_id': approval, 'artifact': artifact})
        self.save(wid, s)
        p = copy.deepcopy(work['plan']); next(v for v in p['views'] if v['id'] == fid)['source_geometry_approval'] = approval
        self.stage(wid, p, work['image_budget'])
        return {'approval_id': approval, 'model_calls': 0, 'professional_acceptance': 'not_reviewed'}

    def source_geometry(self, s, wid, view, plan):
        approval = view.get('source_geometry_approval')
        if not approval: return None
        identity(approval)
        receipt = self.store.load(self.owner, 'source-geometry:' + wid + ':' + view['id'] + ':' + approval)
        from .source_geometry import validate_source_geometry
        if (receipt['view_id'] != view['id'] or receipt['source_sha256'] != s['source_sha256']
                or digest(receipt) != approval or validate_source_geometry(receipt['geometry'], s['bundle'], plan)):
            raise ValueError('source geometry approval stale')
        return receipt['geometry']

    @staticmethod
    def guide_context(view):
        return digest({k: v for k, v in view.items() if k not in {'annotations', 'reference_bundle_approval'}})

    def _prepared_guide(self, s, wid, view, guide_id):
        fid = view['id']
        guide = self.store.load(self.owner, 'prepared-guide:' + wid + ':' + fid + ':' + guide_id)
        if (guide['source_sha256'] != s['source_sha256']
                or guide['view_context_sha256'] != self.guide_context(view)
                or guide['geometry_approval'] != view.get('source_geometry_approval')):
            raise ValueError('guide review stale')
        if (view.get('expression_mode') == 'relational_schematic'
                and guide.get('expression_approval') != s.get('expression_approvals', {}).get(fid)):
            raise ValueError('expression guide approval stale')
        return guide

    def source_reference_blob(self, s, region_id, crop=None):
        region = next((r for r in s['bundle']['figure_candidates'] if r['id'] == region_id), None)
        if not region: raise ValueError('reference source region unavailable')
        page = next(p for p in s['bundle']['pages'] if p['id'] == region['page_id'])
        with Image.open(io.BytesIO(self.store.get_artifact(self.owner, page['image']))) as im:
            im = im.convert('RGB').crop(region['bbox'])
            if crop is not None:
                from .spatial_guide import box_valid
                if not box_valid(crop, *im.size): raise ValueError('component reference outside source crop')
                im = im.crop(crop)
            out = io.BytesIO(); im.save(out, format='PNG'); return out.getvalue()

    def source_profile_check(self, s, wid, view, plan):
        from .source_profiles import validate_source_profiles, trace_ink_check
        scene=view.get('expression_scene', {})
        rows=[r for r in scene.get('instances', [])if r.get('shape')=='source_profile']
        if not rows and not view.get('source_geometry_approval'):
            return {'status':'PASS','bindings':[],'issues':[],'ink_checks':[]}
        try:geometry=self.source_geometry(s,wid,view,plan)
        except ValueError as e:return {'status':'FAIL','bindings':[],'issues':[{'rule':'SOURCE-PROFILE','verdict':'FAIL','message':str(e)}]}
        expression = self.expression_for(s, wid, view, plan) if (geometry or {}).get('assembly_registration') else None
        result=validate_source_profiles(scene,geometry,s['bundle'],plan,view=view,
                                        expression_contract=expression);result['ink_checks']=[]
        if result['status']=='PASS':
            for row in rows:
                check=trace_ink_check(self.source_reference_blob(s,row['source_region_id']),row['source_paths'],
                                      registered=result['assembly_registration'] is not None)
                result['ink_checks'].append({'instance_id':row['id'],**check})
                if check['status']!='PASS':
                    result['status']='FAIL';result['issues'].append({'rule':'SOURCE-PROFILE-INK','verdict':'FAIL','message':'trace does not follow actual source crop ink: '+row['id']})
        return result

    @staticmethod
    def scene_derivation_for(view, plan, expression):
        if view.get('reference_selection') != 'guide_and_parent': return None
        parents = [v for v in plan['views'] if v['id'] in view.get('depends_on', []) and v['engine'] == 'image']
        if (view.get('expression_mode') != 'relational_schematic' or not expression or len(parents) != 1
                or parents[0].get('expression_mode') != 'relational_schematic'
                or (view.get('scene_derivation') or {}).get('parent_view_id') != parents[0]['id']):
            raise ValueError('explicit same-scene relational image parent required')
        from .scene_derivation import validate_scene_derivation
        result = validate_scene_derivation(parents[0].get('expression_scene', {}),
                    view.get('expression_scene', {}), view.get('scene_derivation'), expression)
        if result['status'] != 'PASS': raise ValueError('scene derivation invalid: ' + canonical(result['issues']))
        return result

    def reviewed_image_parent(self, s, wid, view, work, _seen=None):
        seen=set(_seen or ())
        if view['id'] in seen or len(seen)>=24: raise ValueError('image parent ancestor cycle or capacity')
        seen.add(view['id'])
        parents = [v['id'] for v in work['plan']['views']
                   if v['id'] in view.get('depends_on', []) and v['engine'] == 'image']
        if len(parents) != 1: raise ValueError('exactly one actual image parent required')
        parent = parents[0]
        try: record = self.store.load(self.owner, self.record_key(wid, parent, work['specs'][parent]))
        except ValueError: record = (s.get('current') or {}).get('records', {}).get(parent)
        if not record or record['spec_sha256'] != work['specs'][parent]:
            raise ValueError('image parent must be complete before dependent view')
        from .visibility import review_passed
        contract = work['plan'].get('visibility_contract', {}).get(parent)
        if not contract or not review_passed(s.get('visual_reviews', {}).get(parent), record, contract):
            raise ValueError('visual review required: image parent has not passed actual-image technical checks')
        parent_view=next(v for v in work['plan']['views'] if v['id']==parent)
        if any(v['id'] in parent_view.get('depends_on',[]) and v['engine']=='image' for v in work['plan']['views']):
            ancestor,ancestor_record=self.reviewed_image_parent(s,wid,parent_view,work,seen)
            self.validate_parent_binding(s,wid,parent_view,work,record,ancestor,ancestor_record)
        return parent, record

    def validate_parent_binding(self,s,wid,view,work,record,parent,parent_record):
        if view.get('reference_selection')!='guide_and_parent':return
        bound=(record.get('reference_bundle_receipt')or{}).get('parent_binding')or{}
        from .images import prepare_reference
        actual=prepare_reference(self.store.get_artifact(self.owner,parent_record['base']))
        derivation=self.scene_derivation_for(view,work['plan'],self.expression_for(s,wid,view,work['plan']))
        expected={'source_sha256':s['source_sha256'],'parent_view_id':parent,
                  'parent_base_sha256':parent_record['base']['sha256'],
                  'parent_base_spec_sha256':parent_record['base_spec_sha256'],
                  'parent_reference_sha256':actual.sha256,'scene_derivation':derivation}
        from .visibility import ACTUAL_CONTRACT
        if work['plan'].get('visibility_contract',{}).get(view['id'],{}).get('contract') == ACTUAL_CONTRACT:
            expected['parent_review_sha256'] = digest(s['visual_reviews'][parent])
        if any(bound.get(k)!=value for k,value in expected.items()):
            raise ValueError('image parent ancestor reference lineage stale')

    @serialized
    def prepare_spatial(self, wid, fid):
        s = self.read(wid); work = s.get('work')
        if not work: raise ValueError('staged source view required')
        view = next(v for v in work['plan']['views'] if v['id'] == fid)
        if view.get('expression_mode') == 'relational_schematic':
            expression = self.expression_for(s, wid, view, work['plan'])
            if not expression:
                return {'blocking_state':'needs_expression','blocked_view':fid,'model_calls':0,
                        'message':'表达合同需私有来源核对；公开图单不能自批。'}
            try: self.scene_derivation_for(view, work['plan'], expression)
            except ValueError as error:
                return {'blocking_state':'needs_source','blocked_view':fid,'model_calls':0,
                        'issues':[{'rule':'SCENE-DERIVATION','verdict':'FAIL','message':str(error)}],
                        'message':'子图必须观察同一来源场景，不能修改物理结构。'}
            profiles=self.source_profile_check(s,wid,view,work['plan'])
            if profiles['status']!='PASS':
                return {'blocking_state':'needs_source','blocked_view':fid,'model_calls':0,
                        'issues':profiles['issues'],'message':'来源线段尚未与当前私有来源几何及原图像素核对。'}
            from .spatial_guide import build_expression_guide
            try:
                members=self.member_requirements_for(s,wid,view,work['plan'])
            except (ValueError,TypeError,KeyError) as error:
                return {'blocking_state':'needs_source','blocked_view':fid,'model_calls':0,
                        'issues':[{'rule':'GUIDE-MEMBER-CONTEXT','verdict':'FAIL','message':str(error)}],
                        'message':'构件来源及视图可见义务尚未核对，不生成未知结构。'}
            guide = build_expression_guide(expression, view.get('expression_scene', {}),
                                           _member_requirements=members,
                                           _assembly_registration=profiles.get('assembly_registration'))
            from .guide_readiness import validate_guide_readiness
            readiness = validate_guide_readiness(s['bundle'], work['plan'], view, guide,expression_contract=expression,
                                                member_requirements=members,
                                                assembly_registration=profiles.get('assembly_registration'))
            if readiness['status'] != 'PASS':
                return {'blocking_state':'needs_source','blocked_view':fid,'model_calls':0,
                        'issues':readiness['checks'],'missing_features':readiness['missing_features'],
                        'message':'引导未覆盖本视图全部必要内容，先补全来源约束的见证。'}
            guide['readiness'] = readiness
            guide['source_region_bindings'] = profiles['bindings']
            guide['source_ink_checks'] = profiles['ink_checks']
            binding = {'source_sha256':s['source_sha256'], 'geometry_approval':view.get('source_geometry_approval'),
                       'expression_approval':s['expression_approvals'][fid],
                       'view_context_sha256':self.guide_context(view),
                       'guide':self.store.put_artifact(self.owner,guide.pop('png')),
                       'svg':self.store.put_artifact(self.owner,guide.pop('svg')),
                       'markup':None,'metadata':guide}
            sha=digest(binding);self.store.save(self.owner,'prepared-guide:'+wid+':'+fid+':'+sha,binding)
            s.setdefault('prepared_guides',{})[fid]=sha
            s.setdefault('spatial_guide_history',[]).append({'id':sha,'artifact':self.store.put_artifact(self.owner,canonical(binding).encode())})
            self.save(wid,s)
            result = {'blocking_state':'awaiting_guide_review','blocked_view':fid,'model_calls':0,
                      'guide':binding['guide'],'metadata':guide,'message':'空间示意引导已保存，待实际预览与私有审阅。'}
            from .source_profiles import relation_section_review_binding
            review_binding = relation_section_review_binding(binding, sha, expression, profiles.get('assembly_registration'))
            if review_binding:
                result.update(pending_action='professional_relation_review',
                              relation_review_binding=review_binding,
                              message='局部连接示意需由专业人员核对原交底与本版引导后才能用于出图。')
            return result
        geometry = self.source_geometry(s, wid, view, work['plan'])
        if not geometry: return {'blocking_state': 'needs_source', 'blocked_view': fid, 'model_calls': 0, 'message': '需要核对来源外包络和未确认轮廓。'}
        from .spatial_guide import make_guide, default_layout
        layout = view.get('guide_layout') or default_layout(geometry)
        guide = make_guide(geometry, layout)
        binding = {'source_sha256': s['source_sha256'], 'geometry_approval': view['source_geometry_approval'],
                   'view_context_sha256': self.guide_context(view), 'layout': layout,
                   'guide': self.store.put_artifact(self.owner, guide.pop('png')),
                   'markup': self.store.put_artifact(self.owner, guide.pop('markup')),
                   'svg': self.store.put_artifact(self.owner, guide.pop('svg')), 'metadata': guide}
        sha = digest(binding); self.store.save(self.owner, 'prepared-guide:' + wid + ':' + fid + ':' + sha, binding)
        s.setdefault('prepared_guides', {})[fid] = sha
        history = s.setdefault('spatial_guide_history', [])
        if not any(x['id'] == sha for x in history): history.append({'id': sha, 'artifact': self.store.put_artifact(self.owner, canonical(binding).encode())})
        self.save(wid, s)
        return {'blocking_state': 'awaiting_guide_review', 'blocked_view': fid, 'guide': binding['guide'],
                'markup': binding['markup'], 'metadata': guide, 'model_calls': 0, 'message': '来源轮廓引导已保存，待操作员核对布局与参考图。'}

    @serialized
    def approve_reference_bundle(self, wid, fid, bundle, reviewer, *, relation_review=None):
        s = self.read(wid); work = s.get('work')
        if not work or not isinstance(reviewer, str) or not reviewer.strip(): raise ValueError('staged reference review required')
        view = next(v for v in work['plan']['views'] if v['id'] == fid)
        self.source_geometry(s, wid, view, work['plan'])
        expression_mode = view.get('expression_mode') == 'relational_schematic'
        expression = self.expression_for(s, wid, view, work['plan']) if expression_mode else None
        if expression_mode and not expression:
            raise ValueError('private expression review required')
        if expression_mode:
            self.scene_derivation_for(view, work['plan'], expression)
        guide_id = s.get('prepared_guides', {}).get(fid)
        if not guide_id: raise ValueError('programmatic source guide required')
        guide = self._prepared_guide(s, wid, view, guide_id)
        review_binding = None
        if expression_mode:
            profiles=self.source_profile_check(s,wid,view,work['plan'])
            if profiles['status']!='PASS':raise ValueError('source profile not privately bound: '+canonical(profiles['issues']))
            from .guide_readiness import validate_guide_readiness
            readiness = validate_guide_readiness(s['bundle'], work['plan'], view, guide['metadata'],
                                                expression_contract=expression,
                                                member_requirements=self.member_requirements_for(s,wid,view,work['plan']),
                                                assembly_registration=profiles.get('assembly_registration'))
            if readiness['status'] != 'PASS':
                raise ValueError('incomplete expression guide: ' + canonical(readiness))
        if digest(guide) != guide_id:
            raise ValueError('incomplete expression guide: stored guide identity differs')
        if expression_mode:
            from .source_profiles import relation_section_review_binding, validate_relation_section_review
            review_binding = relation_section_review_binding(guide, guide_id, expression, profiles.get('assembly_registration'))
            if review_binding:
                validate_relation_section_review(relation_review, review_binding)
        if relation_review is not None and not review_binding:
            raise ValueError('relation review requires a guide with relation sections')
        from .reference_bundle import prepare_reference_bundle
        prepared = prepare_reference_bundle(bundle['items'], bundle.get('markup'), bundle.get('mask'))
        if expression_mode and s['bundle']['figure_presence']=='present':
            if not any(item.get('role')=='source_overview' and item.get('source_region_id')==view.get('source_region_id')
                       and not item.get('crop_bbox') for item in bundle['items'][1:]):
                raise ValueError('actual uncropped source overview required alongside expression guide')
        if prepared['receipt']['items'][0]['source_sha256'] != s['source_sha256'] or bundle['items'][0]['blob'] != self.store.get_artifact(self.owner, guide['guide']): raise ValueError('guide actual bytes differ')
        if bundle.get('markup') is not None and bundle['markup'] != self.store.get_artifact(self.owner, guide['markup']): raise ValueError('markup differs from source-bound guide')
        for item in bundle['items'][1:]:
            if item['blob'] != self.source_reference_blob(s, item['source_region_id'], item.get('crop_bbox')): raise ValueError('reference is not the actual source crop')
        receipt = {'source_sha256': s['source_sha256'], 'view_id': fid, 'guide_id': guide_id,
                   'view_context_sha256': self.guide_context(view), 'geometry_approval': view.get('source_geometry_approval'),
                   **({'expression_approval':s['expression_approvals'][fid]} if expression_mode else {}),
                   'receipt': prepared['receipt'], 'references': [self.store.put_artifact(self.owner, r.blob) for r in prepared['references']],
                   'markup': self.store.put_artifact(self.owner, prepared['markup'].blob) if prepared['markup'] else None,
                   'mask': self.store.put_artifact(self.owner, prepared['mask'].blob) if prepared['mask'] else None,
                   'reviewer': reviewer, 'reviewed_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}
        if review_binding:
            receipt['relation_section_review'] = self.store.put_artifact(self.owner, relation_review)
        approval = digest(receipt); self.store.save(self.owner, 'reference-bundle:' + wid + ':' + fid + ':' + approval, receipt)
        s.setdefault('reference_bundle_history', []).append({'approval_id': approval, 'artifact': self.store.put_artifact(self.owner, canonical(receipt).encode())})
        self.save(wid, s)
        plan = copy.deepcopy(work['plan']); next(v for v in plan['views'] if v['id'] == fid)['reference_bundle_approval'] = approval
        self.stage(wid, plan, work['image_budget'])
        return {'approval_id': approval, 'model_calls': 0}

    def reference_bundle(self, s, wid, view, plan):
        approval = view.get('reference_bundle_approval')
        if not approval: return None
        identity(approval); receipt = self.store.load(self.owner, 'reference-bundle:' + wid + ':' + view['id'] + ':' + approval)
        self.source_geometry(s, wid, view, plan)
        if view.get('expression_mode') == 'relational_schematic':
            if (not self.expression_for(s,wid,view,plan)
                    or receipt.get('expression_approval') != s.get('expression_approvals',{}).get(view['id'])):
                raise ValueError('expression reference approval stale')
            self.scene_derivation_for(view, plan, self.expression_for(s, wid, view, plan))
        if (digest(receipt) != approval or receipt['source_sha256'] != s['source_sha256']
                or receipt['geometry_approval'] != view.get('source_geometry_approval') or receipt['view_context_sha256'] != self.guide_context(view)):
            raise ValueError('reference bundle approval stale')
        if view.get('expression_mode') == 'relational_schematic':
            profiles=self.source_profile_check(s,wid,view,plan)
            if profiles['status']!='PASS':raise ValueError('source profile not privately bound: '+canonical(profiles['issues']))
            from .guide_readiness import validate_guide_readiness
            prepared = self.store.load(self.owner, 'prepared-guide:' + wid + ':' + view['id'] + ':' + receipt['guide_id'])
            readiness = validate_guide_readiness(s['bundle'], plan, view, prepared['metadata'],
                                                expression_contract=self.expression_for(s,wid,view,plan),
                                                member_requirements=self.member_requirements_for(s,wid,view,plan),
                                                assembly_registration=profiles.get('assembly_registration'))
            if readiness['status'] != 'PASS':
                raise ValueError('incomplete expression guide: ' + canonical(readiness))
            if digest(prepared) != receipt['guide_id']:
                raise ValueError('incomplete expression guide: stored guide identity differs')
            from .source_profiles import relation_section_review_binding, validate_relation_section_review
            review_binding = relation_section_review_binding(
                prepared, receipt['guide_id'], self.expression_for(s, wid, view, plan), profiles.get('assembly_registration'))
            if review_binding:
                artifact = receipt.get('relation_section_review')
                raw = self.store.get_artifact(self.owner, artifact) if artifact else None
                validate_relation_section_review(raw, review_binding)
        from .images import prepare_reference, prepare_markup, prepare_mask
        refs = tuple(prepare_reference(self.store.get_artifact(self.owner, r)) for r in receipt['references'])
        if [r.sha256 for r in refs] != [r['normalized_sha256'] for r in receipt['receipt']['items']]: raise ValueError('normalized reference identity differs')
        if view.get('expression_mode')=='relational_schematic' and s['bundle']['figure_presence']=='present':
            original=prepare_reference(self.source_reference_blob(s,view['source_region_id']))
            if not any(i>0 and row.get('role')=='source_overview' and row.get('source_region_id')==view['source_region_id']
                       and not row.get('crop_bbox') and refs[i].sha256==original.sha256
                       for i,row in enumerate(receipt['receipt']['items'])):
                raise ValueError('actual source overview missing from expression references')
        return {'references': refs, 'markup': prepare_markup(self.store.get_artifact(self.owner, receipt['markup']), refs[0]) if receipt['markup'] else None,
                'mask': prepare_mask(self.store.get_artifact(self.owner, receipt['mask']), refs[0]) if receipt['mask'] else None, 'receipt': receipt}

    @serialized
    def review_visibility(self, wid, fid, review):
        # Operator-only Python entry, like approve_source_label; never a tool action.
        s = self.read(wid); w = s.get('work') or s.get('current')
        if not w or fid not in w['plan'].get('visibility_contract', {}): raise ValueError('image contract missing')
        record = (self.partial(wid)['figures'][fid]['record'] if s.get('work') else w['records'][fid])
        from .visibility import ACTUAL_CONTRACT, check_record_review
        contract = w['plan']['visibility_contract'][fid]
        with Image.open(io.BytesIO(self.store.get_artifact(self.owner, record['base']))) as im:
            issues = check_record_review(review, record, contract, base_image_size=im.size)
            if issues: raise ValueError(canonical(issues))
            for item in review['features']:
                if item['verdict'] == 'PASS' and (item['region'][2] > im.width or item['region'][3] > im.height):
                    raise ValueError('visual evidence region outside actual image')
        if contract['contract'] == ACTUAL_CONTRACT and contract['requires_parent'] and all(
                row['verdict'] == 'PASS' for row in review['features']):
            view = next(v for v in w['plan']['views'] if v['id'] == fid)
            holder = w if s.get('work') else {**w, 'specs': {k: v['spec_sha256'] for k, v in w['records'].items()}}
            parent, parent_record = self.reviewed_image_parent(s, wid, view, holder)
            self.validate_parent_binding(s, wid, view, holder, record, parent, parent_record)
        s.setdefault('visual_reviews', {})[fid] = copy.deepcopy(review)
        artifact = self.store.put_artifact(self.owner, canonical(review).encode())
        s.setdefault('visual_review_history', []).append({'review': copy.deepcopy(review), 'artifact': artifact})
        self.save(wid, s)
        return {'view_id': fid, 'technical_review': 'PASS' if all(x['verdict'] == 'PASS' for x in review['features']) else 'BLOCKED',
                'professional_acceptance': 'not_reviewed', 'model_calls': 0}

    @serialized
    def approve_reference_preparation(self, wid, fid, preparation, mask=None):
        # A private operator action; public plan JSON cannot grant preparation review.
        s = self.read(wid); work = s.get('work')
        if not work: raise ValueError('staged plan required')
        p = copy.deepcopy(work['plan']); view = next(v for v in p['views'] if v['id'] == fid)
        if view['engine'] != 'image': raise ValueError('image reference preparation required')
        region, page = self.source_region(s, view)
        with Image.open(io.BytesIO(self.store.get_artifact(self.owner, page['image']))) as im:
            out = io.BytesIO(); im.convert('RGB').crop(region['bbox']).save(out, format='PNG'); image = out.getvalue()
        source = {'source_sha256': s['source_sha256'], 'source_region_id': region['id'], 'base_sha256': hashlib.sha256(image).hexdigest()}
        from .reference_preparation import validate_preparation
        receipt = validate_preparation(preparation, source, image, mask)
        approval = digest(receipt)
        self.store.save(self.owner, 'reference-preparation:' + wid + ':' + approval,
                        {'receipt': receipt, 'mask': self.store.put_artifact(self.owner, mask) if mask else None})
        view['reference_preparation'] = {'approval_id': approval}
        result = self.stage(wid, p, work['image_budget'])
        return {**result, 'reference_preparation': receipt}

    @serialized
    def approve_image_annotations(self, wid, fid, annotations, reviewer):
        # Private operator entry. Model-supplied anchor_status is not an approval.
        s = self.read(wid); current = s.get('current'); holder = s.get('work') or current
        if not holder or not isinstance(reviewer, str) or not reviewer.strip(): raise ValueError('reviewer and actual image required')
        view = next(v for v in holder['plan']['views'] if v['id'] == fid)
        if view['engine'] != 'image': raise ValueError('actual image required')
        partial = self.partial(wid)['figures'].get(fid) if s.get('work') else None
        record = partial['record'] if partial else (current or {}).get('records', {}).get(fid)
        pipeline = holder.get('spec_pipeline') if s.get('work') else self.verified_previous_pipeline(current)
        if not record or self.base_specs(holder['plan'], pipeline=pipeline)[fid] != record['base_spec_sha256']:
            raise ValueError('actual candidate base required; old geometry cannot receive new anchors')
        base = self.store.get_artifact(self.owner, record['base'])
        clean = copy.deepcopy(annotations)
        parts = {p['id']: p for p in holder['plan']['parts']}
        for a in clean:
            if a.get('part_id') not in view['parts']: raise ValueError('image anchor part outside view')
            a.pop('label', None); a['anchor_status'] = 'reviewed'
        composed = compose(base, [{**a, 'label': parts[a['part_id']]['label']} for a in clean])
        receipt = {'view_id': fid, 'base_sha256': record['base']['sha256'], 'base_spec_sha256': record['base_spec_sha256'],
                   'source_sha256': s['source_sha256'], 'annotations': clean, 'reviewer': reviewer,
                   'reviewed_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}
        approval_id = digest(receipt)
        artifact = self.store.put_artifact(self.owner, canonical(receipt).encode())
        receipt['approval_id'] = approval_id
        self.store.save(self.owner, 'image-anchor-approval:' + wid + ':' + approval_id, receipt)
        self.store.save(self.owner, 'image-anchors:' + wid + ':' + fid + ':' + record['base']['sha256'], receipt)
        s.setdefault('anchor_review_history', []).append({'approval_id': approval_id, 'artifact': artifact})
        if s.get('work'):
            # Compose only the approved display layer; preserve candidate/base and old receipts.
            plan = copy.deepcopy(holder['plan'])
            next(v for v in plan['views'] if v['id'] == fid)['annotations'] = clean
            specs = self.specs(plan, pipeline=pipeline); bases = self.base_specs(plan, pipeline=pipeline)
            updated = {**copy.deepcopy(record), 'spec_sha256': specs[fid], 'png': self.store.put_artifact(self.owner, composed['png']),
                       'regions': composed['regions'], 'base_reused': True, 'anchor_approval_sha256': approval_id,
                       'annotations_sha256': digest(clean), 'annotation_outside_unchanged': outside_regions_unchanged(
                           self.store.get_artifact(self.owner, record['png']), composed['png'], record.get('regions', []) + composed['regions'])}
            if updated['annotation_outside_unchanged'] is not True: raise ValueError('approved anchor composition changed pixels outside display regions')
            s['work'].update(plan=plan, specs=specs, base_specs=bases, plan_sha256=digest(plan))
            ref = self.store.put_artifact(self.owner, canonical(updated).encode())
            s.setdefault('record_history', []).append({'view_id': fid, 'record': ref})
            self.store.save(self.owner, self.record_key(wid, fid, specs[fid]), updated)
        self.save(wid, s)
        return receipt

    def _anchor_receipt(self, state, wid, fid, record, annotations, require_output=False):
        clean = [{k: val for k, val in a.items() if k != 'label'} for a in annotations]
        if not clean: return None
        try: receipt = self.store.load(self.owner, 'image-anchors:' + wid + ':' + fid + ':' + record['base']['sha256'])
        except ValueError: raise ValueError('actual-base image anchor approval required') from None
        if (receipt['source_sha256'] != state['source_sha256'] or receipt['base_spec_sha256'] != record['base_spec_sha256']
                or receipt['annotations'] != clean): raise ValueError('image anchor approval differs from actual base/annotations')
        if require_output and (record.get('anchor_approval_sha256') != receipt.get('approval_id') or record.get('annotations_sha256') != digest(clean)):
            raise ValueError('cached image uses a superseded anchor approval; recompose before promotion')
        return receipt

    @serialized
    def resume(self, wid):
        s = self.read(wid); w = s.get('work')
        if not w: return {'workspace_id': wid, 'queue': []}
        missing = []
        for fid in w['queue']:
            try: self.store.load(self.owner, self.record_key(wid, fid, w['specs'][fid]))
            except ValueError: missing.append(fid)
        views = {v['id']: v for v in w['plan']['views']}
        old_parts = {p['id']: p for p in (s.get('current') or {}).get('plan', {}).get('parts', [])}
        parts = {p['id']: p for p in w['plan']['parts']}
        # list.sort temporarily exposes an empty list while evaluating keys.
        # Capture pending membership before sorting so dependencies stay visible.
        pending_ids = frozenset(missing)
        def priority(fid):
            v = views[fid]
            blocked_source = v['engine'] == 'source' and bool(old_parts) and any(old_parts.get(pid) != parts[pid] for pid in v['parts'])
            source_first=s.get('bundle',{}).get('figure_presence')=='present' and any(views[x]['engine']=='source' for x in pending_ids)
            return (bool(set(v.get('depends_on', [])) & pending_ids), blocked_source,
                    (source_first or not self.allow_models) and v['engine'] == 'image')
        missing.sort(key=priority)
        return {'workspace_id': wid, 'queue': missing}

    @serialized
    def partial(self, wid):
        s = self.read(wid); w = s.get('work')
        if not w: raise ValueError('no pending work')
        figures, missing = {}, []
        for v in w['plan']['views']:
            fid = v['id']
            try:
                record = self.store.load(self.owner, self.record_key(wid, fid, w['specs'][fid])) if fid in w['queue'] else s['current']['records'][fid]
                figures[fid] = {'record': record, 'png': self.store.get_artifact(self.owner, record['png'])}
            except ValueError:
                missing.append(fid)
        from .visibility import matching_review
        reviews = [matching_review(s, fid, data['record'], w['plan'].get('visibility_contract', {}).get(fid)) for fid, data in figures.items()]
        failed = any(any(x['verdict'] == 'FAIL' for x in r['features']) for r in reviews if r)
        from .visibility import review_todos
        todos = review_todos(s, w, {fid: data['record'] for fid, data in figures.items()})
        return {'workspace_id': wid, 'revision': w['plan']['revision'], 'figures': figures, 'missing_ids': missing,
            'deliverable_status': 'incomplete_draft', 'technical_status': 'FAIL' if failed else 'UNKNOWN', 'model_calls': 0,
            **todos}

    def _packable_partial(self, wid, work):
        plan=work['plan']
        pipeline = work.get('spec_pipeline')
        if work['specs']!=self.specs(plan, pipeline=pipeline) or work['base_specs']!=self.base_specs(plan, pipeline=pipeline):
            raise ValueError('stale pipeline; re-stage before partial export')
        partial=self.partial(wid)
        if not partial['figures']:raise ValueError('no completed pending figures to export')
        figures=[]
        for fid,value in partial['figures'].items():
            record=value['record']
            if record['spec_sha256']!=work['specs'][fid] or record['base_spec_sha256']!=work['base_specs'][fid]:
                raise ValueError('stale figure cannot enter partial export')
            figures.append({'id':fid,'record':record,'png':value['png'],
                'base_png':self.store.get_artifact(self.owner,record['base']),
                'svg':self.store.get_artifact(self.owner,record['svg']) if record.get('svg') else None})
        return partial, figures

    @serialized
    def package_partial(self,wid):
        work=self.read(wid).get('work')
        if not work:raise ValueError('no completed pending figures to export')
        plan=work['plan']
        partial, figures = self._packable_partial(wid, work)
        subset=copy.deepcopy(plan);subset['views']=[v for v in subset['views'] if v['id']in partial['figures']]
        scope={'status':'incomplete_draft','required_ids':[v['id'] for v in plan['views']],
            'missing_ids':partial['missing_ids'],'figure_numbers':{v['id']:i for i,v in enumerate(plan['views'],1)}}
        quality={'technical_status':partial['technical_status'],'deliverable_status':'incomplete_draft',
            'filing_ready':False,'professional_acceptance':'not_reviewed','missing_ids':partial['missing_ids']}
        packed=package_drawings(subset,figures,quality,extra_files={
            'evidence/required-plan.json':canonical(plan).encode(),
            'evidence/delivery-scope.json':canonical(scope).encode()},delivery_scope=scope)
        packed.update(quality=quality,missing_ids=partial['missing_ids'],model_calls=0)
        return packed

    @serialized
    def register_engineering_reference(self, wid, fid, *, png, spec, identity_record, review):
        """Private operator entry for the reviewed TC-001 source relationship guide."""
        state = self.read(wid)
        work = state.get('work')
        if not work:
            raise ValueError('engineering reference requires a staged plan')
        evidence = {'spec': spec, 'identity': identity_record, 'review': review}
        if (not isinstance(png, bytes) or len(png) > 16 * 1024 * 1024
                or any(not isinstance(raw, bytes) or len(raw) > 65536 for raw in evidence.values())):
            raise ValueError('engineering reference evidence capacity exceeded')
        try:
            guide, binding, decision = (json.loads(raw) for raw in evidence.values())
        except (ValueError, RecursionError) as exc:
            raise ValueError('invalid engineering reference evidence') from exc
        if not all(isinstance(obj, dict) for obj in (guide, binding, decision)):
            raise ValueError('invalid engineering reference evidence')

        plan_basis = copy.deepcopy(work['plan'])
        view = next((v for v in plan_basis['views'] if v['id'] == fid), {})
        view.pop('source_geometry_approval', None)
        kind = 'offline_editorial_guide_not_installed'
        scope = 'source_relation_guide_only'
        png_hash = hashlib.sha256(png).hexdigest()
        spec_hash = digest(guide)
        guide_id = digest({'kind': kind, 'spec': spec_hash, 'png': png_hash})
        source_hash = source_digest(state['bundle'])
        raw_pdf_hash = guide.get('source_pdf_raw_sha256')
        proof = decision.get('verification_note')
        endpoints = decision.get('endpoint_review')
        expected = {
            'source_sha256': source_hash, 'review_scope': scope, 'guide_id': guide_id,
            'guide_png_sha256': png_hash, 'guide_spec_sha256': spec_hash,
        }
        if (view.get('engine') != 'image' or view.get('expression_mode') != 'relational_schematic'
                or not view.get('source_region_id') or view['source_region_id'] != guide.get('source_region_id')
                or state['source_sha256'] != source_hash or guide.get('source_sha256') != source_hash
                or [s['sha256'] for s in state['bundle']['sources']] != [raw_pdf_hash]
                or binding.get('source_pdf_raw_sha256') != raw_pdf_hash
                or binding.get('source_png_raw_sha256') != guide.get('reference_png_sha256')
                or guide.get('source_plan_sha256') != digest(plan_basis)
                or binding.get('source_plan_sha256') != guide['source_plan_sha256']
                or guide.get('schema') != 'tc001-offline-source-relation-guide-1'
                or guide.get('scope') != scope or guide.get('candidate_kind') != kind
                or binding.get('candidate_kind') != kind
                or any(obj.get(key) != value for key, value in expected.items() for obj in (binding, decision))
                or decision.get('decision') != 'APPROVE_FOR_BOUNDED_REFERENCE'
                or decision.get('spatial_geometry_status') not in ('FAIL', 'UNKNOWN')
                or not isinstance(endpoints, dict)
                or [endpoints.get(key) for key in ('PASS', 'FAIL', 'UNKNOWN')] != [8, 0, 0]
                or not isinstance(proof, dict) or proof.get('png_spec_guide_id_match_identity_record') is not True
                or proof.get('guide_png_raw_sha256_independently_verified') is not True
                or proof.get('guide_spec_raw_sha256') != hashlib.sha256(spec).hexdigest()):
            raise ValueError('engineering reference source, plan or review binding mismatch')
        try:
            with Image.open(io.BytesIO(png), formats=['PNG']) as im:
                if im.width * im.height > 12_000_000:
                    raise ValueError('engineering reference image capacity exceeded')
                im.verify()
        except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
            raise ValueError('invalid engineering reference PNG') from exc

        partial, figures = self._packable_partial(wid, work)
        originals = [f for f in figures if f['record'].get('engine') == 'source']
        if (partial['missing_ids'] != [fid] or len(originals) != len(figures)
                or not any(f['record']['png']['sha256'] == guide['reference_png_sha256'] for f in originals)):
            raise ValueError('engineering reference requires the exact reviewed source drawing')
        receipt = {
            'view_id': fid, 'plan_sha256': digest(work['plan']), 'guide_id': guide_id,
            'source_sha256': source_hash, 'spatial_geometry_status': decision['spatial_geometry_status'],
            'original_png_sha256': {f['id']: f['record']['png']['sha256'] for f in originals},
            'artifacts': {key: self.store.put_artifact(self.owner, raw)
                          for key, raw in {**evidence, 'png': png}.items()},
        }
        history = state.setdefault('engineering_reference_history', [])
        if receipt not in history:
            history.append(receipt)
        if state.get('engineering_reference') != receipt:
            state['engineering_reference'] = receipt
            self.save(wid, state)
        return receipt

    @serialized
    def package_engineering_reference(self, wid):
        state = self.read(wid)
        work = state.get('work')
        receipt = state.get('engineering_reference')
        if (not work or not receipt or receipt['plan_sha256'] != digest(work['plan'])
                or receipt['source_sha256'] != state['source_sha256']):
            raise ValueError('engineering reference is unavailable for the staged plan')
        partial, figures = self._packable_partial(wid, work)
        fid = receipt['view_id']
        originals = {f['id']: f['record']['png']['sha256'] for f in figures if f['record'].get('engine') == 'source'}
        if (partial['missing_ids'] != [fid] or len(originals) != len(figures)
                or originals != receipt['original_png_sha256']):
            raise ValueError('engineering reference requires the bound original drawings and one pending spatial view')

        plan = work['plan']
        export_plan = copy.deepcopy(plan)
        view = next(v for v in plan['views'] if v['id'] == fid)
        reference_view = {
            'id': fid, 'engine': 'engineering_reference', 'delivered_type': 'engineering_reference',
            'requested_type': view['requested_type'], 'parts': view['parts'],
            'title': '弓头组件四处支承关系示意图',
            'purpose': '表达原图可见轮廓和交底已确认的共同支承关系。实体空间图待验收。',
        }
        export_plan['views'] = [reference_view if v['id'] == fid else v for v in export_plan['views']]
        blobs = {key: self.store.get_artifact(self.owner, ref) for key, ref in receipt['artifacts'].items()}
        figures.append({'id': fid, 'png': blobs['png'], 'record': {
            'engine': 'engineering_reference', 'guide_id': receipt['guide_id'],
            'source_sha256': receipt['source_sha256'], 'technical_review': 'bounded_reference_only'}})
        numbers = {v['id']: i for i, v in enumerate(plan['views'], 1)}
        scope = {'status': 'engineering_reference', 'required_ids': list(numbers), 'figure_numbers': numbers,
                 'missing_ids': [fid], 'reference_only_ids': [fid]}
        quality = {'technical_status': partial['technical_status'], 'deliverable_status': 'engineering_reference',
                   'spatial_geometry_review': receipt['spatial_geometry_status'],
                   'filing_ready': False, 'professional_acceptance': 'not_reviewed', 'missing_ids': [fid]}
        packed = package_drawings(export_plan, figures, quality, extra_files={
            'evidence/required-plan.json': canonical(plan).encode(),
            'evidence/delivery-scope.json': canonical(scope).encode(),
            'evidence/engineering-reference-spec.json': blobs['spec'],
            'evidence/engineering-reference-identity.json': blobs['identity'],
            'evidence/engineering-reference-review.json': blobs['review'],
        }, delivery_scope=scope)
        packed.update(quality=quality, missing_ids=[fid], model_calls=0)
        return packed

    @serialized
    def approve_source_label(self,wid,fid,part_id,new_label,roi,at,font_size,reviewer):
        # Operator-only Python entry; never exposed as a model/public tool action.
        state=self.read(wid);current=state.get('current')
        if not current:raise ValueError('complete source set required before review')
        view=next(v for v in current['plan']['views']if v['id']==fid)
        part=next(p for p in current['plan']['parts']if p['id']==part_id)
        if view['engine']!='source'or part_id not in view['parts']:raise ValueError('source part required')
        if current['records'][fid].get('source_label_migration'):raise ValueError('one reviewed migration per source figure; further changes require a new reviewed source')
        base=self.store.get_artifact(self.owner,current['records'][fid]['base'])
        receipt={'base_sha256':hashlib.sha256(base).hexdigest(),'old_label':part['label'],'new_label':new_label,'roi':roi,'at':at,'font_size':font_size,'reviewer':reviewer,'source_sha256':state['source_sha256'],'part_id':part_id,'revision_from':current['plan']['revision'],'source_region_id':view['source_region_id']}
        from .source_labels import apply
        apply(base,receipt)
        key='source-label:'+wid+':'+fid+':'+str(current['plan']['revision']+1)+':'+part_id
        self.store.save(self.owner,key,receipt)
        return receipt

    @serialized
    def prepare_candidate(self, wid, fid):
        return self.render(wid,fid,_prepare_only=True)

    @serialized
    def render(self, wid, fid, _prepare_only=False):
        s = self.read(wid); w = s.get('work')
        if not w or fid not in w['queue']: raise ValueError('figure is not pending in current work')
        p = w['plan']
        pipeline = w.get('spec_pipeline')
        if pipeline and pipeline != self.verified_previous_pipeline(s.get('current')):
            raise ValueError('previous pipeline baseline changed; no automatic redraw')
        if w['specs']!=self.specs(p,pipeline=pipeline)or w['base_specs']!=self.base_specs(p,pipeline=pipeline):raise ValueError('pipeline changed; re-stage plan and review budget; no automatic redraw')
        view = next(v for v in p['views'] if v['id'] == fid); spec = w['specs'][fid]
        blocked = self._blocked_attempt(s, fid)
        recovery = self._recovery_for(s, fid, blocked)
        if view['engine'] == 'image' and blocked and not recovery and not _prepare_only:
            raise ValueError('recovery authorization required; original rejection/UNKNOWN cannot be reset by plan wording')
        contract = p.get('visibility_contract', {}).get(fid)
        if contract and contract['drawability'] == 'needs_source':
            raise ValueError('needs_source: four-support geometry requires confirmed source relations before dispatch')
        old = (s.get('current') or {}).get('records', {}).get(fid)
        reuse_image_base = view['engine'] == 'image' and old and old.get('base_spec_sha256') == w['base_specs'][fid]
        if pipeline and view['engine'] == 'image' and not reuse_image_base:
            raise ValueError('previous pipeline cannot generate or replace a base')
        expression = None
        if view['engine']=='image' and view.get('expression_mode') == 'relational_schematic':
            expression = self.expression_for(s,wid,view,p)
            if not expression: raise ValueError('needs_expression: private source/expression approval required')
            if not reuse_image_base and not self.reference_bundle(s,wid,view,p):
                raise ValueError('awaiting_guide_review: actual expression guide approval required')
        if view['engine']=='image' and view.get('expression_mode') != 'relational_schematic' and not reuse_image_base and s['bundle']['figure_presence']=='present' and (view.get('delivered_type')=='spatial' or any(f['feature_id'].startswith('spring-support-') for f in (contract or {}).get('features',[]))):
            if not self.source_geometry(s,wid,view,p):raise ValueError('needs_source: privately reviewed source geometry required')
            if not self.reference_bundle(s,wid,view,p):raise ValueError('awaiting_guide_review: private source reference bundle required')
        combined_parent = None; parent_binding = None
        if view.get('reference_selection') == 'guide_and_parent' and not reuse_image_base:
            derivation = self.scene_derivation_for(view, p, expression)
            combined_parent = self.reviewed_image_parent(s, wid, view, w)
            parent_id, parent_record = combined_parent
            from .images import prepare_reference
            actual_parent = prepare_reference(self.store.get_artifact(self.owner, parent_record['base']))
            parent_binding = {'source_sha256':s['source_sha256'], 'parent_view_id':parent_id,
                              'parent_spec_sha256':parent_record['spec_sha256'],
                              'parent_base_spec_sha256':parent_record['base_spec_sha256'],
                              'parent_base_sha256':parent_record['base']['sha256'],
                              'parent_reference_sha256':actual_parent.sha256,
                              'parent_review_sha256':digest(s['visual_reviews'][parent_id]),
                              'scene_derivation':derivation}
        key = self.record_key(wid, fid, spec)
        try:
            record = self.store.load(self.owner, key)
            if parent_binding and (record.get('reference_bundle_receipt') or {}).get('parent_binding') != parent_binding:
                raise ValueError('image parent binding changed; re-stage and review the dependent candidate')
            if view['engine'] == 'image' and view.get('annotations'):
                self._anchor_receipt(s, wid, fid, record, view['annotations'], require_output=True)
            return {'record': record, 'png': self.store.get_artifact(self.owner, record['png']), 'model_calls': 0}
        except ValueError as error:
            if 'anchor' in str(error) or 'image parent binding' in str(error): raise
        if view['engine'] == 'image' and not reuse_image_base and not _prepare_only and (not self.allow_models or not self.image_client):
            raise ValueError('zero-model gate: image execution disabled')
        if view['engine'] == 'image' and not _prepare_only and w['required_image_calls'] > w['image_budget']:
            raise ValueError('image operation budget not approved')
        parts = {x['id']: x for x in p['parts']}; annotations = copy.deepcopy(view.get('annotations', [])); svg = None; reference = None; mask = None
        for a in annotations: a['label'] = parts[a['part_id']]['label']
        if reuse_image_base and not annotations:
            raise ValueError('renumber requires reviewed annotation anchors; base not redrawn')
        anchor_receipt = self._anchor_receipt(s, wid, fid, old, annotations) if reuse_image_base else None
        operation_id = 'render_' + digest([wid, fid, spec] + ([recovery['authorization_id']] if recovery else []))
        if view['engine'] == 'image' and not reuse_image_base and not recovery:
            operation_id = self._saved_image_operation(wid, fid, spec, s) or operation_id
        dispatched_here = False
        request = None
        if view['engine'] == 'image' and not reuse_image_base:
            from .prompts import build_prompt
            from .images import prepare_reference
            if view.get('source_region_id'):
                region, page = self.source_region(s, view)
                with Image.open(io.BytesIO(self.store.get_artifact(self.owner, page['image']))) as im:
                    out = io.BytesIO(); im.crop(region['bbox']).save(out, format='PNG'); reference = prepare_reference(out.getvalue())
            reference_parent = None
            reference_selected = 'source' if reference else None
            parents=[other for other in view.get('depends_on',[]) if next(x for x in p['views']if x['id']==other)['engine']=='image']
            if len(parents)>1:raise ValueError('multiple image parents require explicit reference selection')
            if parents:
                parent,parent_record=combined_parent or self.reviewed_image_parent(s,wid,view,w)
                selection = view.get('reference_selection')
                if reference and selection not in {'source', parent, 'guide_and_parent'}:raise ValueError('source crop and image parent require explicit reference selection; cannot silently omit either')
                if not reference or selection in {parent,'guide_and_parent'}:
                    reference=prepare_reference(self.store.get_artifact(self.owner,parent_record['base']))
                    reference_parent=parent
                    reference_selected=parent
            if view.get('reference_preparation'):
                if not reference or reference_parent: raise ValueError('reviewed source reference required for preparation')
                approval = view['reference_preparation'].get('approval_id'); identity(approval)
                saved = self.store.load(self.owner, 'reference-preparation:' + wid + ':' + approval)
                region, _ = self.source_region(s, view)
                mask_blob = self.store.get_artifact(self.owner, saved['mask']) if saved.get('mask') else None
                from .reference_preparation import validate_preparation
                receipt = validate_preparation(saved['receipt'], {'source_sha256': s['source_sha256'], 'source_region_id': region['id'], 'base_sha256': reference.sha256}, reference.blob, mask_blob)
                if digest(receipt) != approval: raise ValueError('reference approval identity differs')
                if mask_blob:
                    from .images import prepare_mask
                    mask = prepare_mask(mask_blob, reference)
            prepared_bundle = self.reference_bundle(s, wid, view, p)
            if prepared_bundle:
                if view.get('reference_selection') == 'guide_and_parent':
                    if not reference_parent or view.get('reference_preparation'):
                        raise ValueError('actual parent plus reviewed guide required without source mask preparation')
                    from .reference_bundle import compose_parent_guide_bundle
                    prepared_bundle = compose_parent_guide_bundle(prepared_bundle, reference, parent_binding)
                    reference_selected = 'guide_and_parent'
                else:
                    if reference_parent or view.get('reference_preparation'): raise ValueError('source bundle cannot silently replace parent/mask selection')
                    reference_selected = 'approved_guide_bundle'
                reference = prepared_bundle['references'][0]; mask = prepared_bundle['mask']
            from .spatial_brief import requirements
            spatial=requirements(s['bundle'],p,view)
            geometry = self.source_geometry(s, wid, view, p)
            from .visual_request import compile_visual_request
            compiled = compile_visual_request(s['bundle'], p, view, geometry)
            prompt = compiled['prompt']
            if expression:
                prompt += '\n受控表达合同（绘图省略不是实体缺口，不能闭合未知轮廓；非制造尺寸）：'+canonical(expression)
                prompt += '\n按已核对的空间引导表达实体、分隔及接触。不得添加侧耳、螺柱、垫片、独立盖板或把导线当分隔壁。实际图仍需独立逐项审阅。'
            prompt += '\n空间可见性清单：'+canonical(spatial)
            if contract: prompt += '\n逐实例可见性合同（不是已通过声明）：' + canonical(contract)
            if reference_selected == 'guide_and_parent':
                prompt += '\n参考顺序：第1张是本视图已审引导图，第2张是已通过实际图审阅的母图无编号底图，后续为已核对原始来源图。只改变观察方式，保持同一来源场景的实体和关系。母图不提供来源外新结构。'
            elif reference_parent:prompt += '\n关联新视图：参考图为已完成母图的实际无编号底图。按本次视图目的调整观察视角，保持可核对的部件身份和已披露结构，不重新设计；参考母图中的缺陷或未知不自动成为来源事实。'
            request = build_prompt({'prompt': prompt, 'size': '1536x1024', 'quality': 'high', 'complexity': 'high' if geometry or expression else 'low', 'output_format': 'png'}, 'edit' if reference and not reference_parent else 'generate')
        def produce():
            nonlocal svg, reference, annotations, dispatched_here
            if reuse_image_base:
                return {'base': base64.b64encode(self.store.get_artifact(self.owner, old['base'])).decode(),
                    'annotations': annotations, 'request_id': old.get('request_id'), 'usage':old.get('usage'), 'usage_projection':old.get('usage_projection'), 'base_reused': True}
            if view['engine'] == 'source':
                region, page = self.source_region(s, view)
                with Image.open(io.BytesIO(self.store.get_artifact(self.owner, page['image']))) as im:
                    cropped = im.convert('RGB').crop(region['bbox']); out = io.BytesIO(); cropped.save(out, format='PNG'); base = out.getvalue()
                if annotations: raise ValueError('source embedded labels require reviewed migration')
                return {'base': base64.b64encode(base).decode(), 'source_region_id': region['id'], 'embedded_labels': True}
            if view['engine'] == 'vector':
                from .diagrams import split_drafting_notes
                diagram,drafting_notes=split_drafting_notes(view['diagram']);primitives = diagram.get('primitives', [])
                shapes = {x.get('id'): x for x in primitives if x.get('type') in {'rect', 'ellipse', 'polyline'}}
                for item in primitives:
                    if item.get('type') == 'text' and '.label}}' in item.get('text', ''):
                        raise ValueError('inline label requires migration to annotation plane')
                    if item.get('type') == 'label':
                        shape = shapes.get(item['target_shape'])
                        if not shape: raise ValueError('annotation part region unavailable')
                        region = shape.get('box')
                        if region is None:
                            points = shape['points'];region = [min(x[0] for x in points), min(x[1] for x in points), max(x[0] for x in points), max(x[1] for x in points)]
                        annotations.append({'id': 'a' + str(len(annotations)), 'part_id': item['part'],
                            'label': parts[item['part']]['label'], 'at': item['at'], 'target': item['target'],
                            'target_region': region, 'anchor_status': 'reviewed', 'font_size': item.get('font_size', 36)})
                diagram['primitives'] = [x for x in primitives if x.get('type') != 'label']
                rendered = render_diagram({'parts': p['parts']}, {'id': fid, 'parts': view['parts'], 'diagram': diagram}, font_factory=regular_font, layered=True)
                return {'foreground':base64.b64encode(rendered['png']).decode(),'text_regions':rendered['text_regions'], 'base': base64.b64encode(rendered['base_png']).decode(), 'svg': base64.b64encode(rendered['svg']).decode(), 'annotations': annotations,'drafting_notes':drafting_notes}
            if view['engine'] == 'scene':
                result = render_view(p['scene'], view)
                return {'base': base64.b64encode(result['png']).decode(), 'annotations': annotations, 'geometry_status': result['geometry_status']}
            evidence = {}
            def capture(kind, blob):
                evidence[kind] = self.store.put_artifact(self.owner, blob)
                self.store.save(self.owner, operation_id + ':evidence', evidence)
            dispatched_here = True
            if recovery and recovery.get('candidate_sha256')!=candidate_sha:raise ValueError('recovery candidate bytes changed')
            account,family=self.lineage_context(s)
            if hasattr(self.image_client,'bind_lineage'):self.image_client.bind_lineage(s['source_sha256'],family)
            if hasattr(self.image_client,'bind_recovery'):self.image_client.bind_recovery(recovery)
            if hasattr(self.image_client,'bind_plan'):self.image_client.bind_plan(digest(p), required_images)
            if prepared_bundle:
                r = self.image_client.render(request, reference, mask=mask, references=prepared_bundle['references'][1:], markup=prepared_bundle['markup'], evidence_sink=capture)
            elif mask is not None:
                r = self.image_client.render(request, reference, mask=mask, evidence_sink=capture)
            else:
                r = self.image_client.render(request, reference, evidence_sink=capture)
            return {'base': base64.b64encode(r.image.blob).decode(), 'request_id': r.request_id, 'usage': r.usage,
                'usage_projection':getattr(r,'usage_projection',None), 'raw_response': base64.b64encode(r.raw_response or b'').decode(), 'evidence': evidence,
                'reference_sha256': reference.sha256 if reference else None, 'mask_sha256': mask.sha256 if mask else None,
                'reference_selection': reference_selected, 'reference_parent':reference_parent, 'annotations': [], 'reference_bundle_approval': view.get('reference_bundle_approval'), 'source_geometry_approval': view.get('source_geometry_approval'), 'visual_request_sha256': compiled['request_context_sha'], 'reference_bundle_receipt': prepared_bundle['receipt']['receipt'] if prepared_bundle else None,
                'expression_guide_sha256':self.store.load(self.owner,'prepared-guide:'+wid+':'+fid+':'+prepared_bundle['receipt']['guide_id'])['metadata']['guide_sha256'] if expression and prepared_bundle else None}
        candidate_sha=None
        if view['engine']=='image' and not reuse_image_base:
            from .client import prepare_image_payload
            candidate_raw=prepare_image_payload(request,reference,mask,references=prepared_bundle['references'][1:] if prepared_bundle else (),markup=prepared_bundle['markup'] if prepared_bundle else None)
            candidate_sha=hashlib.sha256(candidate_raw).hexdigest()
            self.store.save(self.owner,'candidate:' + wid + ':' + fid,{'request_sha256':candidate_sha,'request':self.store.put_artifact(self.owner,candidate_raw),'source_sha256':s['source_sha256'],'lineage_sha256':self.lineage_context(s)[1],'facts_sha256':digest(p.get('facts',[])),'source_components':[x['sha256'] for x in s['bundle']['sources']]})
            if _prepare_only:return {'request_sha256':candidate_sha,'source_sha256':s['source_sha256'],'lineage_sha256':self.lineage_context(s)[1],'model_calls':0,'dispatched':False}
            if recovery and recovery.get('candidate_sha256')!=candidate_sha:raise ValueError('recovery candidate bytes changed; approval invalid')
        budget = {'scope': wid + ':r' + str(p['revision']), 'kind': 'image', 'limit': w['image_budget']} if view['engine'] == 'image' and not reuse_image_base else None
        if view['engine'] == 'image' and not reuse_image_base:
            required_images = self._validate_new_image_dispatch(wid, s, w, operation_id) or 1
            if recovery: budget = {'scope': wid + ':recovery:' + recovery['authorization_id'], 'kind': 'image', 'limit': 1}
            attempts = s.setdefault('image_attempts', {}).setdefault(fid, [])
            if not any(x['operation_id'] == operation_id for x in attempts):
                attempts.append({'operation_id': operation_id, 'spec_sha256': spec,
                                 'previous_operation_id': blocked['operation_id'] if blocked else None,
                                 'recovery_authorization_id': recovery['authorization_id'] if recovery else None})
            self._remember_lineage(s,{'operation_id':operation_id,'spec_sha256':spec,'candidate_sha256':candidate_sha,'facts_sha256':digest(p.get('facts',[])),'source_components':[x['sha256'] for x in s['bundle']['sources']]})
            if recovery: s.setdefault('recovery_uses', {})[recovery['authorization_id']] = operation_id
            self.save(wid, s)
            try:
                output = self.store.run_once(self.owner, operation_id, {'spec': spec, 'workspace': wid}, produce, budget=budget)
            except ImageDispatchNotStarted:
                if recovery:
                    del s['recovery_uses'][recovery['authorization_id']]
                    self.save(wid, s)
                raise
        else:
            # Deterministic work has no paid side effect; a transient render
            # error must remain resumable, rather than become a paid UNKNOWN.
            output = produce()
        base = base64.b64decode(output['base'], validate=True)
        # Dispatch result persisted above, before the following potentially failing IO/packaging.
        if output.get('embedded_labels'):
            current = s.get('current'); migration = None
            if current:
                prior_parts = {x['id']: x for x in current['plan']['parts']}
                changed=[pid for pid in view['parts']if prior_parts.get(pid)!=parts[pid]]
                if changed:
                    if old and old.get('source_label_migration'):raise ValueError('one reviewed migration per source figure; further changes require a new reviewed source')
                    if len(changed)!=1:raise ValueError('source embedded labels require reviewed migration')
                    pid=changed[0];before=prior_parts[pid];after=parts[pid]
                    if any(before.get(k)!=after.get(k)for k in set(before)|set(after)if k!='label'):raise ValueError('source embedded labels require reviewed migration')
                    try:receipt=self.store.load(self.owner,'source-label:'+wid+':'+fid+':'+str(p['revision'])+':'+pid)
                    except ValueError:raise ValueError('source embedded labels require reviewed migration')from None
                    if receipt['new_label']!=after['label']or receipt['old_label']!=before['label']or receipt['source_sha256']!=s['source_sha256']:raise ValueError('migration approval identity differs')
                    from .source_labels import apply
                    migration=apply(base,receipt)
                elif old and old.get('source_label_migration'):
                    receipt=old['source_label_migration']
                    pid=receipt['part_id']
                    if receipt['source_sha256']!=s['source_sha256']or parts[pid]['label']!=receipt['new_label']:
                        raise ValueError('retained migration identity differs')
                    from .source_labels import apply
                    migration=apply(base,receipt)
            composed = migration or {'png': base, 'regions': []}
        else:
            foreground=base64.b64decode(output['foreground'],validate=True)if output.get('foreground')else base
            composed = compose(foreground, output.get('annotations', []))
            composed['regions'] = output.get('text_regions', []) + composed.get('regions', [])
        unchanged = None
        if old and old.get('base', {}).get('sha256') == hashlib.sha256(base).hexdigest():
            unchanged = outside_regions_unchanged(self.store.get_artifact(self.owner, old['png']), composed['png'], old.get('regions', []) + composed.get('regions', []))
            if not unchanged: raise ValueError('pixels changed outside annotation plane')
        record = {'id': fid, 'spec_sha256': spec, 'engine': view['engine'], 'base': self.store.put_artifact(self.owner, base),
            'base_spec_sha256': w['base_specs'][fid], 'base_reused': bool(output.get('base_reused')),
            'png': self.store.put_artifact(self.owner, composed['png']), 'regions': composed.get('regions', []),
            'annotation_outside_unchanged': unchanged, 'annotations': output.get('annotations',[]), 'renderer_fingerprint': fingerprint(), 'drafting_notes':output.get('drafting_notes',[]),
            'request_id': output.get('request_id'), 'usage': output.get('usage'), 'usage_projection':output.get('usage_projection'), 'reference_sha256': output.get('reference_sha256'),
            'source_region_id': output.get('source_region_id'), 'evidence': output.get('evidence', {}), 'technical_review': 'not_reviewed'}
        record['reference_preparation'] = view.get('reference_preparation')
        record['mask_sha256'] = output.get('mask_sha256')
        record['reference_selection'] = output.get('reference_selection')
        record['reference_parent'] = output.get('reference_parent', old.get('reference_parent') if reuse_image_base else None)
        from .visibility import ACTUAL_CONTRACT
        if contract and contract.get('contract') == ACTUAL_CONTRACT:
            with Image.open(io.BytesIO(base)) as image:
                record['base_image_size'] = list(image.size)
        for field in ['reference_bundle_approval', 'source_geometry_approval', 'visual_request_sha256', 'reference_bundle_receipt','expression_guide_sha256']: record[field] = output.get(field,old.get(field) if reuse_image_base else None)
        if anchor_receipt:
            record['anchor_approval_sha256'] = anchor_receipt['approval_id']
            record['annotations_sha256'] = digest(anchor_receipt['annotations'])
        elif view['engine'] == 'image' and annotations:
            record['pending_anchor_review'] = True
        if output.get('embedded_labels')and migration:record['source_label_migration']=migration['receipt']
        if output.get('svg'): record['svg'] = self.store.put_artifact(self.owner, base64.b64decode(output['svg'], validate=True))
        self.store.save(self.owner, key, record)
        latest = self.read(wid)
        latest.setdefault('record_history', []).append({'view_id': fid, 'record': self.store.put_artifact(self.owner, canonical(record).encode())})
        self.save(wid, latest)
        return {'record': record, 'png': composed['png'], 'model_calls': int(dispatched_here)}

    @serialized
    def package(self, wid, export_current=False):
        try:
            return self._package(wid, export_current)
        except Exception:
            s = self.read(wid)
            if not export_current and (s.get('work') or {}).get('transaction'):
                s['work']['transaction']['state'] = 'blocked'
                self.save(wid, s)
            raise

    def _package(self, wid, export_current=False):
        s = self.read(wid); work = s.get('current') if export_current else s.get('work')
        if not work: raise ValueError('no complete/staged set')
        if export_current:
            blocked = self.current_review_block(wid)
            if blocked:
                raise ValueError('actual review no longer supports complete export: ' + blocked['message'])
        p = work['plan']; records = {}; expected_specs = (work.get('specs') or {fid:r['spec_sha256']for fid,r in work['records'].items()}) if export_current else self.specs(p, pipeline=work.get('spec_pipeline'))
        for v in p['views']:
            fid = v['id']
            if export_current:
                record = work['records'][fid]
            elif fid in work['queue']:
                record = self.store.load(self.owner, self.record_key(wid, fid, work['specs'][fid]))
            else:
                record = s['current']['records'][fid]
            if record['spec_sha256'] != expected_specs[fid]: raise ValueError('stale figure spec')
            records[fid] = record
        transaction = work.get('transaction')
        if transaction and not export_current:
            current = s.get('current')
            if not current or digest(current['plan']) != transaction['source_plan_sha256'] or current['plan']['revision'] != transaction['source_revision']:
                raise ValueError('linked edit baseline changed; promotion blocked')
            checks = []
            for approval in transaction['required_approvals']:
                fid = approval['view_id']; record = records[fid]
                bound = bool(record.get('source_label_migration')) if approval['kind'] == 'source_roi' else False
                if approval['kind'] == 'image_anchor':
                    receipt = self.store.load(self.owner, 'image-anchors:' + wid + ':' + fid + ':' + record['base']['sha256'])
                    bound = receipt['base_spec_sha256'] == record['base_spec_sha256'] and receipt.get('approval_id') == record.get('anchor_approval_sha256')
                checks.append({'view_id': fid, 'base_sha256': record['base']['sha256'], 'approval_bound': bound})
            from .linked_edits import validate_edit_completion
            issues = validate_edit_completion(transaction, records, checks)
            if issues: raise ValueError(canonical(issues))
        if not export_current:
            from .visibility import review_passed, matching_review
            for fid, contract in p.get('visibility_contract', {}).items():
                view = next(v for v in p['views'] if v['id'] == fid)
                if view.get('annotations'): self._anchor_receipt(s, wid, fid, records[fid], view['annotations'], require_output=True)
                if not review_passed(matching_review(s, fid, records[fid], contract), records[fid], contract):
                    raise ValueError('visual review required before promoting the complete figure set: ' + fid)
                if view.get('reference_selection')=='guide_and_parent':
                    holder={**work,'specs':expected_specs}
                    parent,parent_record=self.reviewed_image_parent(s,wid,view,holder)
                    self.validate_parent_binding(s,wid,view,holder,records[fid],parent,parent_record)
        quality = check_set(s['bundle'], p, records); figures = []
        from .linked_edits import audit_active_references
        actual_texts={}
        import xml.etree.ElementTree as ET
        for fid,record in records.items():
            if record.get('svg'):
                root=ET.fromstring(self.store.get_artifact(self.owner,record['svg']))
                actual_texts[fid]=' '.join(x.text or '' for x in root.iter() if x.tag.endswith('text'))+' '+' '.join(a['label'] for a in record.get('annotations',[]))
        active_audit=audit_active_references(p,records,actual_texts)
        quality['active_reference_audit']=active_audit
        if active_audit['status']=='FAIL':
            quality['findings']+= [{'rule':'ACTIVE-DISPLAY-REFERENCES',**f} for f in active_audit['findings'] if f['verdict']=='FAIL']
            if not export_current:raise ValueError('stale active display references; promotion blocked')
        from .visibility import matching_review
        matching_reviews = {}
        for fid, contract in p.get('visibility_contract', {}).items():
            actual = matching_review(s, fid, records[fid], contract)
            if actual:
                matching_reviews[fid] = actual
                for feature in actual['features']:
                    quality['findings'].append({'rule': 'ACTUAL-VISIBILITY-REVIEW', 'verdict': feature['verdict'],
                                               'view_id': fid, 'feature_id': feature['feature_id'], 'message': feature['note'],
                                               'base_sha256': actual['base_sha256'], 'reviewer': actual['reviewer']})
        if any(x['verdict'] == 'FAIL' for x in quality['findings']): quality['technical_status'] = 'FAIL'
        for fid, record in records.items():
            figures.append({'id': fid, 'record': record, 'png': self.store.get_artifact(self.owner, record['png']),
                'base_png': self.store.get_artifact(self.owner, record['base']),
                'svg': self.store.get_artifact(self.owner, record['svg']) if record.get('svg') else None})
        from .coverage import coverage_requirements,change_impact
        prior_plan=((s.get('history')or[{}])[-1].get('plan',{})if export_current else(s.get('current')or{}).get('plan',{}))
        extra = {'evidence/source-bundle.json': canonical(s['bundle']).encode(),
            'evidence/coverage.json':canonical(coverage_requirements(s['bundle'],p)).encode(),
            'evidence/change-impact.json':canonical(change_impact(prior_plan,p)).encode()}
        planner_history=[]
        for index,entry in enumerate(s.get('planning_history',[]),1):
            if entry['revision']>p['revision']:continue
            row=copy.deepcopy(entry);row['matches_current_plan']=entry['candidate_plan_sha256']==digest(p)
            row['archive_members']={}
            for kind,ref in entry.get('provider_evidence',{}).items():
                if kind not in {'request','response'}:raise ValueError('unexpected planner evidence type')
                member='evidence/planner-'+str(index)+'-'+kind+'.bin'
                extra[member]=self.store.get_artifact(self.owner,ref);row['archive_members'][kind]=member
            planner_history.append(row)
        extra['evidence/active-references.json']=canonical(active_audit).encode()
        extra['evidence/planner-history.json']=canonical(planner_history).encode()
        extra['evidence/visual-reviews.json'] = canonical(matching_reviews).encode()
        extra['evidence/visual-review-history.json'] = canonical(s.get('visual_review_history', [])).encode()
        extra['evidence/anchor-review-history.json'] = canonical(s.get('anchor_review_history', [])).encode()
        extra['evidence/image-attempt-history.json'] = canonical(s.get('image_attempts', {})).encode()
        if transaction: extra['evidence/linked-edit-transaction.json'] = canonical({**transaction, 'state': 'committed' if not export_current else transaction['state']}).encode()
        extra['evidence/drafting-notes.json']=canonical({fid:r.get('drafting_notes',[])for fid,r in records.items()if r.get('drafting_notes')}).encode()
        original_index = []
        for index, ref in enumerate(s['original_files'], 1):
            suffix = Path(ref.get('name', '')).suffix.lower()
            if suffix not in {'.pdf', '.png', '.jpg', '.jpeg', '.txt', '.md'}: suffix = '.bin'
            name = 'evidence/original-source-' + str(index) + suffix
            extra[name] = self.store.get_artifact(self.owner, ref)
            original_index.append({**ref, 'archive_member': name})
        extra['evidence/original-source-index.json'] = canonical(original_index).encode()
        from .spatial_brief import requirements
        extra['evidence/spatial-requirements.json']=canonical([requirements(s['bundle'],p,v)for v in p['views']if v['engine']=='image']).encode()
        result = package_drawings(p, figures, quality, extra)
        result['quality'] = quality
        if not export_current:
            if s.get('current'): s['history'].append(s['current'])
            s['current'] = {'plan': p, 'specs':expected_specs, 'records': records, 'quality': quality}
            if transaction: s['current']['transaction'] = {**transaction, 'state': 'committed'}
            s['work'] = None
            s['proposal'] = None
            self.save(wid, s)
        return result

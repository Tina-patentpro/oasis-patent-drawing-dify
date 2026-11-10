"""CN drawing pages separated from explanatory evidence; no readiness claim."""
import hashlib
import io
import re
import zipfile
from PIL import Image
from .operation_store import canonical
from .set_delivery import _pdf
from .annotations import fingerprint


def package_drawings(plan, figures, quality=None, extra_files=None, *, delivery_scope=None):
    ids = [v['id'] for v in plan['views']]
    if not ids or len(ids) > 24 or len(set(ids)) != len(ids) or {f['id'] for f in figures} != set(ids) or len(figures) != len(ids):
        raise ValueError('incomplete or duplicate figure set')
    if sum(len(f['png']) for f in figures) > 48 * 1024 * 1024:
        raise ValueError('drawing total capacity exceeded')
    ordered = {f['id']: f for f in figures}; pages = []; payload = {}; records = []; descriptions = []
    scope_status = None
    references = []
    if delivery_scope is not None:
        required=delivery_scope.get('required_ids',[]);numbers=delivery_scope.get('figure_numbers',{})
        scope_status = delivery_scope.get('status')
        if (not required or len(set(required)) != len(required)
                or not set(ids) <= set(required) or numbers != {fid:i for i,fid in enumerate(required,1)}):
            raise ValueError('invalid partial delivery scope')
        missing = delivery_scope.get('missing_ids')
        if scope_status == 'engineering_reference':
            references = [fid for fid in ids if ordered[fid].get('record', {}).get('engine') == scope_status]
            if (ids != required or len(references) != 1 or missing != references
                    or delivery_scope.get('reference_only_ids') != references
                    or ordered[references[0]].get('base_png')):
                raise ValueError('invalid engineering reference scope')
        elif scope_status != 'incomplete_draft' or missing != [fid for fid in required if fid not in ids]:
            raise ValueError('invalid partial delivery scope')
    pixels = 0
    for i, view in enumerate(plan['views'], 1):
        fid = view['id']
        if delivery_scope is not None:i=delivery_scope['figure_numbers'][fid]
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', fid): raise ValueError('unsafe figure id')
        f = ordered[fid]; blob = f['png']
        with Image.open(io.BytesIO(blob)) as im:
            pixels += im.width * im.height
            if im.format != 'PNG' or pixels > 80_000_000: raise ValueError('drawing decoded capacity exceeded')
            im.verify()
        payload['figures/' + fid + '.png'] = blob
        if f.get('base_png'): payload['bases/' + fid + '.png'] = f['base_png']
        if f.get('svg'): payload['figures/' + fid + '.svg'] = f['svg']
        pages.append((blob, '图' + str(i) + (' 工程示意' if fid in references else '')))
        title = re.sub(r'^\s*图\s*\d+\s*[：:、.\-]?\s*', '', view.get('title', ''))
        index={p['id']:p for p in plan.get('parts',[])}
        names='；'.join((index[pid]['label']+'—' if index[pid]['label'] else '')+index[pid]['name'] for pid in view.get('parts',[]) if pid in index)
        descriptions.append(f'图{i}：{title}。{view.get("purpose", "")}'+(' 涉及部件：'+names+'。' if names else ''))
        records.append({'id': fid, 'png_sha256': hashlib.sha256(blob).hexdigest(), **f.get('record', {})})
    manifest = {'schema_version': 'drawing-set-result-2', 'figures': records, 'renderer_fingerprint': fingerprint(),
        'professional_acceptance': 'not_reviewed', 'user_acceptance': False, 'filing_ready': False,
        'representation': 'Raster A4 drawing pages; any supplied SVG kept separately',
        'cn_page_geometry': {'size_mm': [210, 297], 'minimum_margins_mm': {'top': 25, 'left': 25, 'right': 15, 'bottom': 15}}}
    if delivery_scope is not None:
        manifest.update(deliverable_status=scope_status,complete_set=False,
            missing_ids=delivery_scope['missing_ids'],required_ids=delivery_scope['required_ids'])
        if scope_status == 'engineering_reference':
            manifest['reference_only_ids'] = references
            payload['delivery-notice.txt'] = (
                '工程示意交付。标记“工程示意”的图页只用于说明经核对的支承关系。\n'
                '实体空间图待验收，工作区完整版本保持原状。\n').encode()
        else:
            payload['delivery-notice.txt']=('incomplete_draft：仅交付已完成图件，完整图集尚未完成；不提升当前完整版本。\n待处理图件：'
                +'、'.join(delivery_scope['missing_ids'])+'\n技术审阅、完整说明书图文核查与专业验收仍需完成。\n').encode()
    payload.update({'plan.json': canonical(plan).encode(), 'manifest.json': canonical(manifest).encode(),
        'quality.json': canonical(quality if quality is not None else {'status': 'UNKNOWN', 'reason': 'not reviewed'}).encode(),
        'figure-descriptions.txt': ('\n'.join(descriptions) + '\n').encode()})
    for name, blob in (extra_files or {}).items():
        if not re.fullmatch(r'evidence/[A-Za-z0-9_.-]{1,100}', name): raise ValueError('unsafe evidence name')
        payload[name] = blob
    payload['drawings.pdf'] = _pdf(pages, cn_layout=True)
    review_state={'source_sha256':plan.get('source_sha256'),'current':{'plan':plan,'records':{r['id']:r for r in records}},'review_files':dict(payload)}
    review_bundle=build_review_bundle(review_state,'current')
    if delivery_scope is not None:
        review_bundle.update(delivery_scope=delivery_scope, status=(
            'bounded_reference_only' if references else 'incomplete_draft_not_reviewed'))
    payload['evidence/actual-review-bundle.json']=canonical(review_bundle).encode()
    payload['SHA256SUMS.json'] = canonical({n: hashlib.sha256(b).hexdigest() for n, b in payload.items()}).encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, blob in sorted(payload.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0));info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, blob)
    return {'pdf': payload['drawings.pdf'], 'zip': output.getvalue(), 'manifest': manifest,
            'page_captions': [p[1] for p in pages], 'member_hashes': {n: hashlib.sha256(b).hexdigest() for n, b in payload.items()}}


def build_review_bundle(state:dict,scope:str)->dict:
    """Actual delivery bytes and source/base identities, never a plan-only review."""
    if scope not in {'current','work'}:raise ValueError('review scope invalid')
    selection=state.get(scope) or {};plan=selection.get('plan',{});files=state.get('review_files',{})
    required={'drawings.pdf','figure-descriptions.txt','plan.json','manifest.json'}
    for view in plan.get('views',[]):required.add('figures/'+view['id']+'.png')
    missing=sorted(required-set(files));rows={};comparisons=[]
    for name,blob in files.items():
        if not isinstance(blob,bytes):raise ValueError('actual review file bytes required')
        rows[name]={'sha256':hashlib.sha256(blob).hexdigest(),'bytes':len(blob)}
    for fid,raw in state.get('raw_base_files',{}).items():
        actual=files.get('bases/'+fid+'.png')
        if actual is None:continue
        with Image.open(io.BytesIO(raw)) as a,Image.open(io.BytesIO(actual)) as b:
            a=a.convert('RGB');b=b.convert('RGB');same=a.size==b.size and a.tobytes()==b.tobytes()
        comparisons.append({'view_id':fid,'raw_sha256':hashlib.sha256(raw).hexdigest(),'base_sha256':hashlib.sha256(actual).hexdigest(),'rgb_equivalent':same})
    return {'schema':'actual-review-bundle-036-r1','scope':scope,'source_sha256':state.get('source_sha256'),
        'revision':plan.get('revision'),'plan_sha256':hashlib.sha256(canonical(plan).encode()).hexdigest(),
        'files':rows,'records':selection.get('records',{}),'missing_actual_files':missing,
        'status':'UNKNOWN' if missing else 'materials_complete_not_reviewed','rgb_comparisons':comparisons,
        'specification_sync':'UNKNOWN','professional_acceptance':'not_reviewed','user_acceptance':False,'filing_ready':False}

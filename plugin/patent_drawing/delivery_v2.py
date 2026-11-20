"""CN drawing pages separated from explanatory evidence; no readiness claim."""
import hashlib
import io
import re
import zipfile
from PIL import Image
from .filing_review import evaluate_filing, filing_scale_samples
from .marking import visible_vector_marks
from .operation_store import canonical, digest
from .set_delivery import _pdf
from .annotations import fingerprint
from .source_contract import compact, validate_facts


COORDINATE_ALIGNMENT = re.compile(
    r'(?:(?:需要|需|应|必须)保持双方对|双方(?:需要|需|应|必须)保持对)'
    r'参考坐标(?:系)?(?:及|与|和)面板方向的?一致理解'
)


def _public_limitations(plan, source_bundle):
    # Only publish fixed wording. Planner notes and fact evidence can contain source or audit data.
    if 'communication' not in plan.get('technical_types', []):
        return {}
    unknown = plan.get('unresolved', []) + [
        fact.get('text', '') for fact in plan.get('facts', []) if fact.get('status') == 'unknown'
    ]
    timing_unknown = any('参考信号' in note and '反馈' in note
                         and any(term in note for term in ('时序', '空口调度', '时隙')) for note in unknown)
    alignment_facts = {
        fact['id'] for fact in plan.get('facts', [])
        if source_bundle is not None and fact['status'] == 'confirmed'
        and any(COORDINATE_ALIGNMENT.fullmatch(clause) for ref in fact.get('evidence', [])
                for clause in re.split(r'[。；;！？!?]', compact(ref.get('quote', ''))))
        and not validate_facts(source_bundle, [fact])
    }
    result = {}
    for view in plan['views']:
        context = ' '.join(view.get(field, '') for field in ('title', 'purpose', 'instruction'))
        if '参考信号' in context and '反馈' in context:
            notes = []
            if timing_unknown:
                notes.append('仅示意逻辑先后和消息方向。参考信号与测量反馈的具体空口时序待核。')
            if alignment_facts.intersection(view.get('required_facts', [])):
                notes.append('双方需对参考坐标及面板方向保持一致理解。')
            if notes:
                result[view['id']] = notes
    return result


def _public_plan(plan, limitations):
    displayed = {pid for view in plan['views'] for pid in view.get('parts', [])}
    return {
        **{k: plan[k] for k in ('title', 'revision', 'part_mark_mode') if k in plan},
        'parts': [{k: part[k] for k in ('id', 'label', 'name') if k in part}
                  for part in plan.get('parts', []) if part['id'] in displayed],
        'views': [
            {**{k: view[k] for k in ('id', 'title', 'purpose', 'parts', 'engine', 'delivered_type') if k in view},
             **({'limitations': limitations[view['id']]} if view['id'] in limitations else {})}
            for view in plan['views']
        ],
    }


def format_part_reference(part):
    return (part['label'] + '—' if part['label'] else '') + part['name']


def _material_part_mark_status(plan, view, record, parts):
    if not record.get('material_relation') or plan.get('part_mark_mode') != 'numbered':
        return None
    expected = {pid for pid in view.get('parts', []) if pid in parts and parts[pid]['label']}
    if not expected:
        return None
    annotations = record.get('annotations', [])
    if record.get('engine') == 'image':
        annotations = [{k: value for k, value in item.items() if k != 'label'}
                       for item in view.get('annotations', [])]
        if not record.get('anchor_approval_sha256') or record.get('annotations_sha256') != digest(annotations):
            annotations = []
    return 'present' if expected <= {item['part_id'] for item in annotations} else 'pending'


def _delivery_review(quality, scope, view_ids):
    status = quality.get('technical_status', 'UNKNOWN')
    failed_features = quality.get('failed_feature_ids', [])
    failures = {fid.split('/', 1)[0] for fid in failed_features}
    failures.update(row.get('view_id') for row in quality.get('findings', []) if row.get('verdict') == 'FAIL')
    failed_ids = [fid for fid in view_ids if fid in failures]
    missing_ids = scope['missing_ids'] if scope else []
    numbers = scope['figure_numbers'] if scope else {fid: i for i, fid in enumerate(view_ids, 1)}

    def figures(ids):
        labels = '、'.join(f'图{numbers[fid]}' for fid in ids[:6])
        return labels + (f'等共{len(ids)}图' if len(ids) > 6 else '')

    if scope and scope['status'] == 'structural_reference':
        label = '结构关系示意待审'
    elif scope and scope['status'] == 'engineering_reference':
        label = ('工程示意改号稿待复审' if scope.get('reference_revision_status') == 'reference_revision_not_reviewed'
                 else '工程示意')
    elif status == 'FAIL':
        label = '审核未通过'
    else:
        label = '图集草稿'
    details = []
    if status == 'FAIL':
        affected = figures(failed_ids) or '图集'
        reasons = {'source-fidelity': '来源保真检查未通过', 'centerline': '剖视中心线检查未通过',
                   'panel-beam-common-origin': '波束范围共同基准点检查未通过',
                   'material-flake-contact': '邻片接合线检查未通过',
                   'material-in-plane-heat-path': '热流箭跨接合检查未通过',
                   'material-thickness-direction': '独立厚度方向检查未通过'}
        reason = next((reasons[feature] for feature in (item.rsplit('/', 1)[-1] for item in failed_features)
                       if feature in reasons), '图面或图文技术检查未通过')
        details.append(f'{affected}：{reason}')
    if missing_ids:
        details.append('待完成 ' + figures(missing_ids))
    if not details:
        details.append('待完成技术及专业核查')
    banner = (label + '｜' + '；'.join(details), label + '｜技术状态 ' + status + '｜工程参考')
    return failed_ids, banner


def package_drawings(plan, figures, quality=None, extra_files=None, *, delivery_scope=None, audit=False,
                     source_bundle=None, filing_candidate=False, filing_inputs=None):
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
        elif scope_status == 'structural_reference':
            references = [fid for fid in ids if ordered[fid].get('record', {}).get('engine') == scope_status]
            if (ids != required or references != ids or missing != ids
                    or delivery_scope.get('reference_only_ids') != ids
                    or any(ordered[fid].get('base_png') for fid in ids)):
                raise ValueError('invalid structural reference scope')
        elif scope_status != 'incomplete_draft' or missing != [fid for fid in required if fid not in ids]:
            raise ValueError('invalid partial delivery scope')
    if filing_candidate and delivery_scope is not None:
        raise ValueError('partial/reference sets cannot become filing candidates')
    quality = quality or {}
    failed_ids, banner = _delivery_review(quality, delivery_scope, ids)
    public_quality = {k: quality[k] for k in ('technical_status', 'deliverable_status', 'file_identity',
        'geometry_constraints', 'source_fidelity', 'spatial_geometry_review') if k in quality}
    public_quality.setdefault('technical_status', 'UNKNOWN')
    public_quality.update(filing_ready=False, professional_acceptance='not_reviewed', user_acceptance=False)
    if delivery_scope is not None or failed_ids:
        public_quality.update(failed_ids=failed_ids, missing_ids=delivery_scope['missing_ids'] if delivery_scope else [])
    limitations = _public_limitations(plan, source_bundle)
    from .visibility import BEAM_CLEAN_CONTRACTS, MATERIAL_HEAT_CONTRACT
    contracts = plan.get('visibility_contract', {})
    index = {part['id']: part for part in plan.get('parts', [])}
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
        if audit and f.get('base_png'): payload['bases/' + fid + '.png'] = f['base_png']
        if f.get('svg'): payload['figures/' + fid + '.svg'] = f['svg']
        pages.append((blob, '图' + str(i) + (' 工程示意' if fid in references else '')))
        title = re.sub(r'^\s*图\s*\d+\s*[：:、.\-]?\s*', '', view.get('title', ''))
        record = f.get('record', {})
        marks = _material_part_mark_status(plan, view, record, index)
        if view.get('engine') == 'vector':
            expected = {pid for pid in view.get('parts', []) if pid in index and index[pid].get('label')}
            if expected:
                observed = visible_vector_marks(view, index, f['svg']) if f.get('svg') else {}
                marks = 'present' if expected <= observed.keys() else 'pending'
        names = '；'.join(format_part_reference(index[pid]) for pid in view.get('parts', []) if pid in index)
        purpose = view.get('purpose', '')
        if record.get('material_relation') == 'parallel_conditions':
            purpose = ('图中膜面取向和邻片接触分别指向连续传热网络，再由连续传热网络指向局部热量沿膜面扩散。'
                       '箭线示意拟议作用，不表示制备先后或实测结果。')
        description = f'图{i}：{title}。{purpose}'
        if names:
            register = '部件登记（图面编号待核）' if marks == 'pending' else '涉及部件'
            description += f' {register}：{names}。'
        if contracts.get(fid, {}).get('contract') in BEAM_CLEAN_CONTRACTS:
            description += ' 矩形表示面板，无箭尖的两线表示相对角度范围，两支内箭分别表示示意波束方向。'
        if contracts.get(fid, {}).get('contract') == MATERIAL_HEAT_CONTRACT:
            description += ' 热流为面内扩散的定性示意，厚度轴仅作方向对照。实际施热位置、方向及实测性能待核。'
            if record.get('material_axis_labeling'):
                description += ' 独立方向标识的横轴表示膜面方向，竖轴表示厚度方向。'
        notes = limitations.get(fid)
        descriptions.append(description + (' ' + ' '.join(notes) if notes else ''))
        if not audit:
            record = {k: record[k] for k in ('engine', 'technical_review') if k in record}
            if scope_status == 'structural_reference' and f.get('record', {}).get('relation_detail_kind'):
                record['relation_detail_kind'] = f['record']['relation_detail_kind']
        if marks:
            record = {**record, 'part_mark_status': marks}
        records.append({**record, 'id': fid, 'png_sha256': hashlib.sha256(blob).hexdigest()})
    manifest = {'schema_version': 'drawing-set-result-2', 'figures': records, 'renderer_fingerprint': fingerprint(),
        'professional_acceptance': 'not_reviewed', 'user_acceptance': False, 'filing_ready': False,
        'technical_status': public_quality['technical_status'],
        'representation': 'Raster A4 drawing pages; any supplied SVG kept separately',
        'cn_page_geometry': {'size_mm': [210, 297], 'minimum_margins_mm': {'top': 25, 'left': 25, 'right': 15, 'bottom': 15}}}
    if delivery_scope is not None:
        manifest.update(deliverable_status=scope_status,complete_set=False,
            missing_ids=delivery_scope['missing_ids'],required_ids=delivery_scope['required_ids'])
        if scope_status == 'structural_reference':
            manifest['reference_only_ids'] = references
            payload['delivery-notice.txt'] = (
                '来源关系结构示意。图形、数量和尺寸为示意表达，具体装配外形及未确认参数待核。\n'
                '实体空间图与专业审阅待验收。\n').encode()
        elif scope_status == 'engineering_reference':
            manifest['reference_only_ids'] = references
            if delivery_scope.get('reference_revision_status') == 'reference_revision_not_reviewed':
                manifest['reference_revision_status'] = 'reference_revision_not_reviewed'
                introduction = '工程示意改号稿。改号稿待复审，图页仅用于说明支承关系。\n'
            else:
                introduction = '工程示意交付。标记“工程示意”的图页只用于说明经核对的支承关系。\n'
            payload['delivery-notice.txt'] = (introduction +
                '实体空间图待验收，工作区完整版本保持原状。\n').encode()
        else:
            manifest['failed_ids'] = failed_ids
            payload['delivery-notice.txt'] = (
                banner[0] + '。\n技术审阅、完整说明书图文核查与专业验收仍需完成。\n').encode()
    payload.update({'plan.json': canonical(plan if audit else _public_plan(plan, limitations)).encode(),
        'manifest.json': canonical(manifest).encode(),
        'quality.json': canonical(quality if audit else public_quality).encode(),
        'figure-descriptions.txt': ('\n'.join(descriptions) + '\n').encode()})
    for name, blob in (extra_files or {}).items():
        if not re.fullmatch(r'evidence/[A-Za-z0-9_.-]{1,100}', name): raise ValueError('unsafe evidence name')
        if audit:
            payload[name] = blob
    payload['drawings.pdf'] = _pdf(pages, cn_layout=True,
                                   review_banner=None if filing_candidate else banner if delivery_scope or quality else None)
    if filing_candidate:
        inputs = filing_inputs or {}
        pdf_sha256 = hashlib.sha256(payload['drawings.pdf']).hexdigest()
        samples = filing_scale_samples(payload['drawings.pdf'], ids, png_files=payload)
        payload['filing-samples/index.json'] = canonical({'pdf_sha256': pdf_sha256, 'pages': samples}).encode()
        rendered = {'pdf_sha256': pdf_sha256,
                    'captions_sha256': hashlib.sha256(payload['figure-descriptions.txt']).hexdigest(),
                    'scale_samples': samples,
                    'views': {fid: {'sha256': hashlib.sha256(f['png']).hexdigest(),
                                    'svg_sha256': hashlib.sha256(f['svg']).hexdigest() if f.get('svg') else None,
                                    'marks': {row['part_id']: row['label']
                                              for row in f.get('record', {}).get('annotations', [])}}
                              for fid, f in ordered.items()}}
        # The reviewer records visual marks for embedded source ink and inline SVG text.
        visible_review = inputs.get('visible_mark_review', {})
        for fid, figure in rendered['views'].items():
            view = next(row for row in plan['views'] if row['id'] == fid)
            svg = ordered[fid].get('svg')
            if svg:
                figure['marks'].update(visible_vector_marks(view, index, svg))
            reviewed = visible_review.get(fid, {})
            if reviewed.get('sha256') == figure['sha256']:
                figure['marks'].update(reviewed.get('marks', {}))
        specification = inputs.get('specification', {})
        sources = inputs.get('source_evidence', {})
        review = evaluate_filing(plan, rendered, specification, sources, inputs.get('review_receipts', {}),
                                 physical_scale_scans=inputs.get('physical_scale_scans', {}),
                                 printable_pages=payload, signoff_artifacts=inputs.get('signoff_artifacts', {}))
        has_nonpassing_findings = any(row.get('verdict') == 'FAIL' for row in quality.get('findings', ()))
        if quality.get('technical_status') == 'FAIL' or has_nonpassing_findings:
            verdict = 'FAIL'
            review['status'] = verdict
            review['findings'].append({'rule_id': 'F4', 'verdict': verdict, 'view_id': None, 'feature_id': None,
                                       'evidence_ids': [], 'next_action': '先修复并复核实际技术检查失败项。'})
        elif quality.get('technical_status') != 'PASS' and review['status'] != 'PASS':
            verdict = 'FAIL' if quality.get('technical_status') == 'FAIL' else 'UNKNOWN'
            if review['status'] != 'FAIL':
                review['status'] = verdict
            review['findings'].append({'rule_id': 'F4', 'verdict': verdict, 'view_id': None, 'feature_id': None,
                                       'evidence_ids': [], 'next_action': '先完成成套图的实际技术审核。'})
        ready = review['status'] == 'PASS'
        state = {'filing_status': review['status'], 'filing_ready': ready,
                 'professional_acceptance': 'reviewed' if ready else 'not_reviewed',
                 'deliverable_status': 'filing_drawings' if ready else 'filing_review_candidate'}
        manifest.update(state)
        public_quality.update(state)
        payload['manifest.json'] = canonical(manifest).encode()
        payload['quality.json'] = canonical({**quality, **public_quality} if audit else public_quality).encode()
        payload['filing-review.json'] = canonical(review).encode()
        payload['delivery-notice.txt'] = (
            '申报图件通过当前专业核对，电子申请按 XML 要求办理。\n' if ready
            else '供逐页审阅的候选图件，申报验收待完成。\n').encode()
    if audit:
        review_state = {'source_sha256': plan.get('source_sha256'),
            'current': {'plan': plan, 'records': {r['id']: r for r in records}}, 'review_files': dict(payload)}
        review_bundle = build_review_bundle(review_state, 'current')
        if delivery_scope is not None:
            status = ('structural_projection_not_reviewed' if scope_status == 'structural_reference'
                      else delivery_scope.get('reference_revision_status', 'bounded_reference_only') if references
                      else 'incomplete_draft_not_reviewed')
            review_bundle.update(delivery_scope=delivery_scope, status=status)
        payload['evidence/actual-review-bundle.json'] = canonical(review_bundle).encode()
    payload['SHA256SUMS.json'] = canonical({n: hashlib.sha256(b).hexdigest() for n, b in payload.items()}).encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, blob in sorted(payload.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0));info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, blob)
    return {**({'filing_review': review, 'filing_review_rendered': rendered} if filing_candidate else {}),
            'pdf': payload['drawings.pdf'], 'zip': output.getvalue(), 'manifest': manifest,
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

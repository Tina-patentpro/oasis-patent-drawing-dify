"""Evidence-bound dialogue intake. This module never dispatches a model."""
import copy
import hashlib
import re
import time
import uuid

from .cn_policy import PROFILES, TECHNICAL_TERMS, parse_technical_types, suggest_types
from .linked_edits import LINKED_CONFIRMATION
from .materials import ingest_material
from .operation_store import digest


FIELDS = ('patent_type', 'application_stage', 'technical_type')
LABELS = {'patent_type': '申请类型', 'application_stage': '申请阶段', 'technical_type': '技术领域'}
QUESTIONS = {
    'materials': '请描述技术方案，或上传交底书及已有附图。',
    'patent_type': '这件准备申请发明、实用新型还是外观设计？',
    'application_stage': '这件申请已经提交了吗？可回答“还没提交”或“已提交”。',
    'technical_type': '这套图要表达什么关系？例如结构配合、电路与信号、流程条件、封装层间连接或材料取向。',
    'figure_presence': '请核对图页，上传原图，或明确回答“没有附图”。',
}
NAMES = {'invention': '发明', 'utility_model': '实用新型', 'design': '外观设计',
         'draft': '申请前', 'filed': '已申请', 'mechanical': '机械结构', 'electrical': '电气电路',
         'computer_method': '软件/计算机方法', 'communication': '通信', 'manufacturing_process': '制造工艺',
         'chemical_material': '化学材料', 'bio_medical': '生物医学', 'semiconductor': '半导体'}
TECHNICAL_ALIASES = {'结构连接': ['mechanical'], '机电结构': ['mechanical', 'electrical'],
                     '电路连接': ['electrical'], '方法流程': ['computer_method'], '计算机方法': ['computer_method']}
TECHNICAL_LABELS = {NAMES[key] for key in PROFILES if key != 'unknown'} | TECHNICAL_ALIASES.keys()
COMMANDS = {'预览', '规划', '导入计划', '执行单图', '生成', '继续', '续做', '查看进度', '确认无需附图',
            '继续准备复杂图',
            '查看已完成', '导出已完成', '导出工程示意', '确认工程示意改号',
            '导出结构示意', '确认结构示意改号', '核查申报图件', '导出申报图件', '导出'}
PLAN_REQUEST = re.compile(r'^(?:(?:请帮我|请你|请|帮我)\s*)?规划(?:一套|一组)(?:专利)?附图(?=$|[，。；！、：:\s])')
PLAN_WITHOUT_MODELS = re.compile(
    r'(?:不要|不用|不必|无需|禁止|暂不|先不|别)\s*(?:使用|调用|运行)?\s*(?:任何)?(?:模型|AI(?![A-Za-z0-9_-]))'
    r'|(?:不要|不必|无需|禁止|暂不|先不|别)\s*(?:再)?规划',
    re.IGNORECASE,
)


def planning_command(query):
    return (query == '规划' or query.startswith(('规划：', '规划:'))
            or bool(PLAN_REQUEST.match(query) and not PLAN_WITHOUT_MODELS.search(query)))


def fixed_command(query):
    if re.fullmatch(r'(?:确认图单并生成|确认图单|生成)(?:（直连对照）|\s+直连对照)', query):
        return True
    return query in COMMANDS or query == '查看原图候选' or bool(
        LINKED_CONFIRMATION.fullmatch(query) or re.fullmatch(r'选择原图\s*[1-9][0-9]{0,2}', query))


def command(query):
    return fixed_command(query) or planning_command(query) or query.startswith(('生成：', '导出版本', '导出上次来源版本', '导出工程示意版本', '导出结构示意版本', '修改', '改号', '改名',
        '整套将', '整套把', '全套将', '全套把', '全部附图'))


def is_source_supplement(query, pending, *, supplied_source=False):
    if query.startswith(('补充：', '补充说明：')):
        return True
    if pending != 'technical_type' or len(query) < 12 or command(query):
        return False
    answer = re.sub(r'^(?:技术领域|技术类型|更正技术(?:领域|类型)?)\s*[：:为是]\s*',
                    '', query.strip(' 。！!'))
    labels = re.split(r'\s*[、，,和及]\s*', answer)
    return supplied_source or not all(label in TECHNICAL_LABELS for label in labels)


def explicit_fields(text, pending=''):
    """Only current user statements or direct short answers establish facts."""
    result = {}
    clauses = re.split(r'[。；\n]', text)
    text = '。'.join(c for c in clauses if not re.search(r'背景|引用|现有技术|对比文件', c) or re.search(r'本案|本申请', c))
    intent = r'(?<!不)(?<!未)(?<!并非)(?<!没有)(?<!不是)(?:准备|拟|希望|我要|我想)\s*申请'
    prefix = r'(?:^申请|^更正[：:]|申请类型[为是：:]|(?:本案|本申请)(?:拟)?申请|' + intent + r')\s*'
    ending = r'(?:专利)?(?=$|[，。；：:\s])'
    patterns = {
        'patent_type': [('utility_model', prefix + '实用新型' + ending + r'|^实用新型(?:专利)?$'),
                        ('invention', prefix + '发明' + ending + r'|^发明(?:专利)?$'),
                        ('design', prefix + '外观设计' + ending + r'|^外观设计(?:专利)?$')],
        'application_stage': [('draft', r'(?:尚未|还没|没有|未)提交(?:专利)?申请|^申请前$|^还没提交$'),
                              ('filed', r'(?<!不)(?<!未)(?<!没有)(?<!尚未)(?:(?:已经|已)提交(?:专利)?申请|(?:本申请|专利申请)(?:已经|已)提交)(?=$|[，。；\s])|^已申请$')],
    }
    for field, pairs in patterns.items():
        matches = [(value, m) for value, pattern in pairs for m in re.finditer(pattern, text)]
        if len({v for v, _ in matches}) == 1:
            value, found = matches[0]
            result[field] = {'value': value, 'status': 'confirmed', 'evidence': found.group(),
                             'source': 'user', 'start': found.start(), 'end': found.end()}
    if pending == 'application_stage':
        answer = text.strip(' 。！!')
        short = {'还没提交': 'draft', '尚未提交': 'draft', '没有提交': 'draft', '没有': 'draft',
                 '还没有': 'draft', '未提交': 'draft', '已提交': 'filed', '已经提交': 'filed'}
        if answer in short:
            result['application_stage'] = {'value': short[answer], 'status': 'confirmed',
                'source': 'user', 'evidence': text, 'start': 0, 'end': len(text)}
    if pending == 'technical_type' or text.startswith(('技术领域', '技术类型', '更正技术')):
        found = [k for k in PROFILES if k != 'unknown' and NAMES[k] in text]
        found = list(dict.fromkeys(found + [value for term, values in TECHNICAL_ALIASES.items()
                                           if term in text for value in values]))
        if found:
            result['technical_type'] = {'value': ','.join(found), 'status': 'confirmed',
                                        'evidence': text, 'source': 'user', 'start': 0, 'end': len(text)}
    return result


class Intake:
    def __init__(self, workspace, clock=time.time, classifier=None):
        self.workspace = workspace
        self.store = workspace.store
        self.owner = workspace.owner
        self.clock = clock
        self.classifier = classifier

    def new(self):
        return {'intake_id': uuid.uuid4().hex, 'revision': 0, 'fields': {}, 'bundle': None,
                'originals': [], 'workspace_id': '', 'previous_workspace': None,
                'confirmed_revision': None, 'pending_question': 'materials', 'form': {},
                'updated_at': self.clock(), 'history': [], 'deferred': []}

    def read(self):
        try:
            return self.store.load(self.owner, 'intake:active')
        except ValueError as error:
            if str(error) != 'state unavailable for this owner':
                raise
            return self.new()

    def value(self, state, field):
        return state['fields'].get(field, {}).get('value', '')

    def snapshot(self, raw, *, claims_only=False, mixed_materials=False):
        bundle = ingest_material(raw)
        if claims_only:
            from .claims_input import read_claims_text
            body, _ = read_claims_text('\n'.join(page['text'] for page in bundle['pages']))
            bundle.update(claims_body=body, text=body, figure_candidates=[],
                          figure_presence='absent', strategy='create_new')
        elif bundle['figure_presence'] == 'absent' and re.search(r'附图说明|图\s*[0-9一二三四五六七八九十]', bundle['text']):
            bundle.update(figure_presence='unknown', strategy='review_source_figures')
        if mixed_materials:
            from .material_sections import catalog_material, only_claims

            catalog = catalog_material(bundle)
            bundle.update(entry_mode='mixed_materials', material_catalog=catalog, material_selection={})
            if (body := only_claims(bundle, catalog)) is not None:
                bundle.update(claims_body=body, text=body, figure_candidates=[],
                              figure_presence='absent', strategy='create_new')
        originals = [{**self.store.put_artifact(self.owner, f['blob']), 'name': f['name']} for f in raw]
        for page in bundle['pages']:
            png = page.pop('png')
            page['image'] = self.store.put_artifact(self.owner, png) if png and not claims_only else None
        return bundle, originals

    def suggest(self, state):
        bundle = state['bundle']
        if state['fields'].get('technical_type', {}).get('source') == 'user':
            return
        types = suggest_types(bundle['text'])
        evidence = []
        for technology, terms in TECHNICAL_TERMS.items():
            for page in bundle['pages']:
                term = next((term for term in terms if term in page['text']), None)
                if term:
                    start = page['text'].index(term)
                    evidence.append({'technical_type': technology, 'page_id': page['id'],
                        'source_sha256': page['source_sha256'], 'text_sha256': page['text_sha256'],
                        'start': start, 'end': start + len(term), 'quote': term})
                    break
        types = list(dict.fromkeys([t for t in types if t != 'unknown'] + [e['technical_type'] for e in evidence]))
        state['fields'].pop('technical_type', None)
        if types:
            state['fields']['technical_type'] = {'value': ','.join(types), 'status': 'inferred',
                'source': 'material', 'source_fingerprint': state['source_fingerprint'], 'evidence': evidence}

    def source_image_candidates(self, state):
        return [c for c in (state.get('bundle') or {}).get('figure_candidates', ())
                if c.get('kind') == 'image' and c.get('object_number')]

    def source_image(self, state, candidate):
        from .materials import extract_pdf_image

        page = next(p for p in state['bundle']['pages'] if p['id'] == candidate['page_id'])
        original = next(item for item in state['originals'] if item['sha256'] == page['source_sha256'])
        raw = self.store.get_artifact(self.owner, original)
        return extract_pdf_image(raw, page['page'], candidate['object_number']), page

    def response(self, state, message='', route='reply'):
        missing = 'materials' if not state['bundle'] else next((f for f in FIELDS if not self.value(state, f) and f not in state['deferred']), '')
        if not missing and state['bundle'] and state['bundle']['figure_presence'] == 'unknown':
            missing = 'figure_presence'
        state['pending_question'] = missing
        fields = {f: self.value(state, f) for f in FIELDS}
        lines = [message] if message else []
        if state.get('intake_requests'):
            lines.append('用户需求 ' + '\n'.join(state['intake_requests']))
        for f, value in fields.items():
            names = '、'.join(NAMES.get(v, v) for v in value.split(',')) if value else '待确认'
            lines.append(LABELS[f] + ' ' + names)
        if state['bundle']:
            presence = state['bundle']['figure_presence']
            lines.append({'present': '已有原图，将核对修改和补图范围。',
                          'absent': '当前材料未检出附图，将按技术内容规划图集。',
                          'unknown': '附图状态待核，请核对原件中的图页。'}[presence])
            if presence == 'unknown':
                count = len(self.source_image_candidates(state))
                if count:
                    lines.append(f'检测到 {count} 个 PDF 图像对象。回复“查看原图候选”预览，核对后回复“选择原图 1”等对应编号。')
        if missing:
            lines.append(QUESTIONS[missing])
        elif any(not self.value(state, f) for f in FIELDS):
            lines.append('仍有待确认项，可继续补充，涉及这些信息的操作将在确认后进行。')
        elif not state['workspace_id']:
            lines.append('请核对以上摘要，回复“确认”保存材料。需要更正时直接说明。')
        else:
            lines.append('材料已保存。可发送“预览”或“规划”核对来源和图单。')
        return {'workspace_id': state['workspace_id'], 'intake_id': state['intake_id'],
                'intake_revision': str(state['revision']), 'pending_question': missing,
                'source_fingerprint': state.get('source_fingerprint', ''),
                'route': route, 'done': 'yes', 'model_calls': 0, 'message': '\n'.join(lines), **fields}

    def apply(self, params, raw=None):
        with self.store.owner_lock(self.owner):
            return self._apply(params, raw)

    def _apply(self, params, raw):
        state = self.read()
        mode = params.get('source_mode')
        claims_only = mode == 'claims_only'
        mixed_materials = mode == 'mixed_materials'
        query = (params.get('query') or '').strip()
        if state['bundle'] and query not in {'新案件', '重置'}:
            bundle = state['bundle']
            old_mode = bundle.get('entry_mode') or ('claims_only' if 'claims_body' in bundle else None)
            if old_mode != (mode if claims_only or mixed_materials else None):
                raise ValueError('当前工作区来源类型已锁定。')
        if claims_only and (params.get('plan_json') or raw and len(raw) != 1):
            raise ValueError('请提交一份权利要求正文。')
        operation = params.get('operation_id') or ''
        file_refs = []
        for file in params.get('files') or []:
            value = file if isinstance(file, dict) else file.model_dump()
            reference = {k: str(value[k]) for k in ['id', 'related_id', 'upload_file_id', 'filename', 'name', 'url'] if k in value}
            if isinstance(value.get('blob'), bytes):
                reference['blob_sha256'] = hashlib.sha256(value['blob']).hexdigest()
            file_refs.append(reference)
        identity = digest({'params': {k: v for k, v in params.items() if k != 'files'}, 'files': file_refs})
        if operation:
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', operation):
                raise ValueError('invalid intake operation id')
            try:
                saved = self.store.load(self.owner, 'intake-op:' + operation)
            except ValueError:
                saved = None
            if saved:
                if saved['identity'] != identity:
                    raise ValueError('intake operation identity mismatch')
                if (saved['result']['intake_id'] != state['intake_id']
                        or saved['result']['intake_revision'] != str(state['revision'])):
                    return self.response(state, '这是此前采集的操作，请核对当前案件。')
                return saved['result']
        expected = params.get('expected_revision')
        if (expected in (None, '') and state['revision'] > 0
                or expected not in (None, '') and str(state['revision']) != str(expected)):
            return self.response(state, '采集信息已更新，请核对本版后再确认。')
        if self.clock() - state['updated_at'] > 86400 and not state['workspace_id']:
            state = self.new()
            query = ''
            raw = None
        resetting = query in {'重置', '新案件'}
        if resetting:
            self.store.save(self.owner, 'intake-archive:' + state['intake_id'], state)
            state = self.new()
            state['form'] = {f: params.get(f) for f in FIELDS if params.get(f)}
            material = (params.get('materials') or '').strip()
            state['form_material_sha'] = hashlib.sha256(material.encode()).hexdigest() if material else ''
            query = ''
            raw = None
        before = copy.deepcopy(state)
        wid = '' if resetting else params.get('workspace_id') or ''
        if wid and state['workspace_id'] and wid != state['workspace_id']:
            raise ValueError('intake workspace binding mismatch')
        if wid and not state['bundle']:
            old = self.workspace.read(wid)
            state.update(bundle=old['bundle'], originals=old['original_files'], workspace_id=wid,
                         source_fingerprint=old['source_sha256'], confirmed_revision=0)
            for f, value in [('patent_type', old['patent_type']), ('application_stage', old['application_stage']),
                             ('technical_type', ','.join(old['technical_types']))]:
                state['fields'][f] = {'value': value, 'status': 'confirmed', 'source': 'workspace'}
            before = copy.deepcopy(state)
        notice = ''
        analysis_calls = 0
        explicit = explicit_fields(query, state['pending_question'])
        if raw:
            raw_fingerprint = digest(sorted(hashlib.sha256(f['blob']).hexdigest() for f in raw))
            supplement = is_source_supplement(query, state['pending_question'],
                                              supplied_source=bool(params.get('files') or params.get('materials')))
            if state['bundle'] and raw_fingerprint != state.get('raw_fingerprint') and query != '更新材料' and not supplement:
                return self.response(state, '替换交底请明确发送“更新材料”并附新材料。')
            if raw_fingerprint != state.get('raw_fingerprint'):
                state['bundle'], state['originals'] = self.snapshot(raw, claims_only=claims_only, mixed_materials=mixed_materials)
                state['raw_fingerprint'] = raw_fingerprint
                state['source_fingerprint'] = digest(state['bundle']['sources'])
                self.suggest(state)
                if params.get('materials'):
                    state['form_material_sha'] = hashlib.sha256(params['materials'].strip().encode()).hexdigest()
                if (not self.value(state, 'technical_type') and self.classifier
                        and not params.get('technical_type') and 'technical_type' not in explicit):
                    analysis = self.classifier.classify(state['bundle']['text'], state['source_fingerprint'])
                    state['analysis'] = analysis
                    analysis_calls = analysis['model_calls']
                    value = analysis.get('technical_type')
                    if analysis['state'] == 'suggested' and value and value != 'unknown':
                        parse_technical_types(value)
                        evidence = [{'page_id': p['id'], 'source_sha256': p['source_sha256'],
                            'text_sha256': p['text_sha256'], 'start': 0, 'end': len(p['text']),
                            'quote': p['text']} for p in state['bundle']['pages'] if p['text']]
                        state['fields']['technical_type'] = {'value': value, 'status': 'inferred',
                            'source': 'model', 'source_fingerprint': state['source_fingerprint'], 'evidence': evidence}
        pending = state['pending_question']
        if params.get('files') and query and not fixed_command(query) and query not in {'确认', '更新材料', '重置', '新案件'}:
            requests = state.setdefault('intake_requests', [])
            original_query = params['query']
            if original_query not in requests:
                if len(requests) >= 50 or sum(len(text) for text in requests) + len(original_query) > 64000:
                    raise ValueError('用户需求过长，请整理为材料后提交；原记录保留')
                requests.append(original_query)
        for f in FIELDS:
            value = params.get(f) or ''
            if value and value != state['form'].get(f) and value != 'auto':
                if f == 'technical_type':
                    parse_technical_types(value)
                elif value not in ({'invention', 'utility_model', 'design'} if f == 'patent_type' else {'draft', 'filed'}):
                    raise ValueError('invalid intake field')
                state['fields'][f] = {'value': value, 'source': 'user', 'status': 'confirmed'}
            if value:
                state['form'][f] = value
        state['fields'].update(explicit)
        if pending == 'figure_presence' and query in {'没有附图', '无附图'}:
            state['bundle'].update(figure_presence='absent', strategy='create_new')
            state['fields']['figure_presence'] = {'value': 'absent', 'status': 'confirmed',
                'source': 'user', 'evidence': query, 'source_fingerprint': state['source_fingerprint']}
        selection = re.fullmatch(r'选择原图\s*([1-9][0-9]{0,2})', query)
        if selection and pending == 'figure_presence':
            candidates = self.source_image_candidates(state)
            index = int(selection[1]) - 1
            if index >= len(candidates):
                return self.response(state, '待核图像对象中没有此编号。')
            candidate = candidates[index]
            blob, page = self.source_image(state, candidate)
            from PIL import Image
            from io import BytesIO
            with Image.open(BytesIO(blob)) as image:
                dimensions = list(image.size)
            artifact = self.store.put_artifact(self.owner, blob)
            candidate['selected_object'] = {'artifact': artifact, 'source_sha256': page['source_sha256'],
                                            'page': page['page'], 'object_number': candidate['object_number'],
                                            'dimensions': dimensions, 'coordinate_space': 'intrinsic_bitmap'}
            state['bundle'].update(figure_presence='present', strategy='modify_supplement')
            state['fields']['figure_presence'] = {'value': 'present', 'status': 'confirmed',
                'source': 'user', 'evidence': query, 'source_fingerprint': state['source_fingerprint']}
            notice = f'已登记第 {page["page"]} 页的选定图像对象。'
        if query in {'不知道', '不确定'} and pending in FIELDS and pending not in state['deferred']:
            state['deferred'].append(pending)
        state['deferred'] = [f for f in state['deferred'] if not self.value(state, f)]
        if mixed_materials and (selected := re.fullmatch(r'选用材料\s+(s[0-9a-f]{16})', query)):
            bundle = state['bundle']
            catalog = bundle['material_catalog']
            if state['workspace_id']:
                try:
                    classified = self.store.load(self.owner, 'material-catalog:' + state['workspace_id'])
                    if classified['source'] == digest(bundle['sources']):
                        catalog = classified['catalog']
                except (ValueError, KeyError):
                    pass
            for kind, choices in catalog['version_choices'].items():
                if selected[1] in choices:
                    bundle['material_selection'][kind] = selected[1]
                    notice = '已选择材料版本。请确认新修订后再规划。'
                    break
            else:
                raise ValueError('该材料片段不属于当前可选版本。')
        changed = (any(self.value(before, f) != self.value(state, f) for f in FIELDS)
                   or any(before.get(k) != state.get(k) for k in ['bundle', 'deferred', 'intake_requests']))
        if changed:
            if len(state['history']) >= 200:
                raise ValueError('本轮采集修订过多，请核对记录后开始新案件')
            if state['workspace_id']:
                state['previous_workspace'] = state['workspace_id']
                state['workspace_id'] = ''
            state['confirmed_revision'] = None
            state['revision'] += 1
            state['history'].append({'revision': before['revision'], 'fields': before['fields'],
                                     'source_fingerprint': before.get('source_fingerprint', ''),
                                     'workspace_id': before['workspace_id']})
        complete = state['bundle'] and (
            bool(state['bundle']['material_catalog']['sections']) if mixed_materials else
            state['bundle']['figure_presence'] != 'unknown'
            and (claims_only or all(self.value(state, f) for f in FIELDS)))
        if query == '确认' and complete and not state['workspace_id']:
            state['updated_at'] = self.clock()
            self.store.save(self.owner, 'intake:active', state)
            wid = digest({'intake_id': state['intake_id'], 'revision': state['revision'],
                'bundle': digest(state['bundle']), 'fields': {f: self.value(state, f) for f in FIELDS},
                'requests': state.get('intake_requests', [])})[:32]
            # Stable workspace identity allows recovery after a commit/receipt interruption.
            try:
                self.workspace.read(wid)
            except ValueError as error:
                if str(error) != 'state unavailable for this owner':
                    raise
                self.workspace.ingest_prepared(state['bundle'], state['originals'],
                    previous_workspace=state['previous_workspace'],
                    patent_type=('invention' if claims_only else self.value(state, 'patent_type') or 'invention'),
                    application_stage=('draft' if claims_only else self.value(state, 'application_stage') or 'draft'),
                    technical_types=(parse_technical_types(self.value(state, 'technical_type'))
                                     if not claims_only and self.value(state, 'technical_type') else None),
                    workspace_id=wid,
                    intake_request='\n'.join(state.get('intake_requests', [])))
            state['workspace_id'] = wid
            state['confirmed_revision'] = state['revision']
        if query in {'不知道', '不确定'}:
            notice = '已记录此项待确认，需要该信息的操作暂留在预览。'
        route = 'forward' if state['workspace_id'] and command(query) and not changed else 'reply'
        result = self.response(state, notice, route)
        if mixed_materials and state['bundle'] and 'claims_body' not in state['bundle']:
            catalog = state['bundle']['material_catalog']
            names = {'claims':'权利要求','specification':'说明书','disclosure':'交底书',
                     'other':'其他','uncertain':'待判别'}
            lines = [notice] if notice else []
            lines.extend(f"{item['id']} {names[item['type']]}，第 "
                         + '、'.join(str(p['page']) for p in item['spans']) + ' 页'
                         for item in catalog['sections'])
            lines.extend(f"{names[kind]}有多个版本。回复“选用材料 {ids[0]}”或其他候选编号。"
                         for kind, ids in catalog['version_choices'].items()
                         if kind not in state['bundle']['material_selection'])
            if catalog['review_pages']:
                lines.append('存在需核对的扫描页或读序。可先规划已读部分。')
            if not catalog['sections']:
                lines.append('请粘贴可核对正文，或提供能读取文字的材料。')
            else:
                lines.append('可发送“规划”。' if state['workspace_id'] else '核对材料后回复“确认”。')
            result['message'] = '\n'.join(lines)
            if not state['workspace_id']:
                result['material_catalog'] = catalog
        if claims_only or mixed_materials and state['bundle'] and 'claims_body' in state['bundle']:
            if state['bundle']:
                from .claims_input import read_claims_text
                body, ranges = read_claims_text(state['bundle']['claims_body'])
                result['message'] = (f'已提取 {len(ranges)} 项权利要求。正文 SHA-256 '
                                     f'{hashlib.sha256(body.encode()).hexdigest()}。')
                result['message'] += ' 可发送“规划”。' if state['workspace_id'] else ' 核对正文后回复“确认”。'
            else:
                result['message'] = '请粘贴或上传连续编号的权利要求正文。'
        result['model_calls'] = analysis_calls
        result['intake_model_state'] = state.get('analysis', {}).get('state', 'not_used')
        state['updated_at'] = self.clock()
        self.store.save(self.owner, 'intake:active', state)
        self.store.save(self.owner, 'intake-revision:' + state['intake_id'] + ':' + str(state['revision']), state)
        if operation:
            self.store.save(self.owner, 'intake-op:' + operation, {'identity': identity, 'result': result})
        return result

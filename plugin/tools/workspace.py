"""Public SDK boundary: owner-bound persistent v2 workspace operations."""
import copy
import json
import os
import re
import uuid
import hashlib
from pathlib import Path

from patent_drawing import safe_tool
from patent_drawing.errors import DrawingError, ToolInvokeError
from patent_drawing.file_input import read_file, FileSettings
from patent_drawing.operation_store import OperationStore, canonical
from patent_drawing.workspace import Workspace
from patent_drawing.visibility import ACTUAL_CONTRACT, review_todo_message


def load_plan(value):
    if not isinstance(value, str) or len(value.encode()) > 400_000:
        raise ValueError('plan JSON capacity')
    def pairs(items):
        d = {}
        for k, v in items:
            if k in d: raise ValueError('duplicate plan JSON key')
            d[k] = v
        return d
    result = json.loads(value, object_pairs_hook=pairs)
    if not isinstance(result, dict): raise ValueError('plan must be object')
    return result


def namespace(store, storage):
    # SDK KV is tenant/plugin scoped. Serialize first initialization on the
    # verified shared daemon volume; no namespace supplied by workflow/model.
    key = 'patent-drawing-workspace-namespace-v3'
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        present = storage.exist(key)
        if type(present) is not bool: raise ValueError('tenant storage unavailable')
        if not present: storage.set(key, uuid.uuid4().hex.encode())
        value = storage.get(key)
        if not isinstance(value, bytes) or not re.fullmatch(b'[a-f0-9]{32}', value):
            raise ValueError('invalid tenant storage namespace')
    return value.decode()


def preview_message(result):
    patent = {'invention': '发明', 'utility_model': '实用新型', 'design': '外观设计'}
    types = {'mechanical': '机械结构', 'electrical': '电气电路', 'computer_method': '软件/计算机方法',
        'communication': '通信', 'manufacturing_process': '制造工艺', 'chemical_material': '化学材料',
        'bio_medical': '生物医学', 'semiconductor': '半导体', 'unknown': '待确认'}
    lines = [result.get('message', ''), '申请类型：' + patent.get(result.get('patent_type'), '待确认'),
        '技术类型：' + '、'.join(types.get(x, x) for x in result.get('technical_types', []))]
    if result.get('views'):
        lines += [v['id'] + '：' + v.get('purpose', '') + '（要求' + v['requested_type'] + '，交付' + v['delivered_type'] + '）' for v in result['views']]
        lines += ['部件/标号：' + '；'.join((p['label'] + '—' if p['label'] else '') + p['name'] for p in result.get('parts', [])),
            '待确认：' + ('；'.join(result.get('unresolved', [])) or '仍须核查实际图文，不代表已无风险'),
            '本图单图片调用上限需求：' + str(result.get('budget', {}).get('image', 0))]
    else:
        lines += ['建议视图（按条件选取，不是法定最少图数）：' + '；'.join(x['type'] + '：' + x['condition'] for x in result.get('recommendations', []))]
    if not result.get('model_execution_enabled'):
        lines.append('当前为材料规划与校验模式，尚未执行模型绘图。')
    if result.get('blocking_state'):
        lines.append('当前停止原因：' + result['blocking_state'] + '；完整版本与未完成修订分别保留。')
    lines.append('结果为附图辅助草稿；完整说明书图文核查与实际图形审阅另行记录。')
    return '\n'.join(lines)


class PatentDrawingWorkspace(safe_tool.SafeDrawingTool):
    def _invoke(self, tool_parameters):
        try:
            params = tool_parameters
            if not isinstance(params, dict): raise ValueError('parameters invalid')
            context = params.get('context_id', '')
            if not self.runtime.user_id or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', context):
                raise ValueError('runtime user/context binding required')
            store = OperationStore(Path(os.environ.get('PATENT_DRAWING_STATE_ROOT', '/app/storage/patent-drawing-v3')))
            owner = namespace(store, self.session.storage) + ':' + self.runtime.user_id + ':' + context
            from patent_drawing.operator_policy import model_execution_enabled
            enabled = model_execution_enabled()
            from patent_drawing.operator_policy import effective_image_budget
            stage_budget=effective_image_budget(int(params.get('image_budget')or 0))
            with store.owner_lock(owner):
                from patent_drawing.client import OpenAIImagesClient
                from patent_drawing.text_client import OpenAITextClient
                key = self.runtime.credentials.get('openai_api_key')
                w = Workspace(store, owner, allow_models=enabled,
                    image_client=OpenAIImagesClient(key) if enabled else None,
                    text_client=OpenAITextClient(key) if enabled else None)
                action = params.get('action', 'receive'); wid = params.get('workspace_id', '')
                result = {}; blobs = []
                if action == 'receive':
                    query = (params.get('query') or '').strip(); files = params.get('files') or []; materials = (params.get('materials') or '').strip()
                    model_calls = 0
                    if wid and materials and query != '更新材料':
                        state = w.read(wid)
                        text_sha = hashlib.sha256(materials.encode()).hexdigest()
                        if any(src.get('name') == 'user-material.txt' and src['sha256'] == text_sha for src in state['bundle']['sources']): materials = ''
                    if not materials and not wid and not files:
                        m = re.match(r'^(?:规划|生成)[：:]\s*([\s\S]+)$', query)
                        if m: materials = m.group(1)
                    if query == '重置':
                        wid = '';result = {'workspace_id': '', 'route': 'reply', 'message': '已清除当前会话引用；原材料、图集和历史记录保留。'}
                    elif files or materials:
                        if wid and query != '更新材料': raise ValueError('来源更新需明确发送“更新材料”；旧工作区保持')
                        raw = []; remaining = 16 * 1024 * 1024
                        settings = FileSettings(self.runtime.credentials.get('dify_file_origin') or '', self.runtime.credentials.get('dify_internal_files_url') or '')
                        if not isinstance(files, list) or len(files) > 6: raise ValueError('source file count exceeded')
                        for file in files:
                            blob = read_file(file, remaining, settings); remaining -= len(blob)
                            if remaining < 0: raise ValueError('source aggregate capacity')
                            name = (file.get('filename') or file.get('name')) if isinstance(file, dict) else getattr(file, 'filename', None)
                            if not name: raise ValueError('source filename/type required')
                            raw.append({'name': name, 'blob': blob})
                        if materials: raw.append({'name': 'user-material.txt', 'blob': materials.encode()})
                        previous = w.read(wid) if wid else {}
                        technical = params.get('technical_type') or 'auto'
                        result = w.ingest(raw, previous_workspace=wid or None,
                            patent_type=params.get('patent_type') or previous.get('patent_type', 'invention'),
                            technical_types=__import__('patent_drawing.cn_policy',fromlist=['parse_technical_types']).parse_technical_types(technical),
                            application_stage=params.get('application_stage') or previous.get('application_stage', 'draft'))
                        wid = result['workspace_id']
                        for page in w.read(wid)['bundle']['pages']:
                            if page.get('image'): blobs.append((store.get_artifact(owner, page['image']), page['id'] + '.png', 'image/png'))
                    elif wid:
                        result = w.preview(wid)
                    else:
                        result = {'message': '请上传原始交底PDF/图片或填写材料；先规划图集，再生成或修改。', 'route': 'reply', 'workspace_id': ''}
                    result.update(route='reply', queue=[])
                    if wid and params.get('plan_json') and (query in {'规划', '生成', '导入计划'} or query.startswith(('规划：', '生成：'))):
                        result.update(w.stage(wid, load_plan(params['plan_json']), image_budget=stage_budget))
                        result['route'] = 'execute' if query == '生成' else 'reply'
                        result['message'] = '已校验图单；' + w.preview(wid)['message']
                    elif wid and query=='确认图单':
                        proposal=w.read(wid).get('proposal')
                        if proposal is None:raise ValueError('没有已校验的候选图单')
                        result.update(w.stage(wid,proposal,image_budget=stage_budget));result.update(route='reply',message='已确认图单；可执行单图，先核对总图再继续局部图。')
                    elif wid and query=='执行单图':
                        pending=w.resume(wid)['queue']
                        if not pending:raise ValueError('没有待执行图件')
                        gate=w.preflight(wid)
                        if gate.get('blocking_state'):result.update(gate)
                        else:
                            before_dispatches=w.image_dispatch_count(wid)
                            try:
                                r=w.render(wid,pending[0]);blobs.append((r['png'],pending[0]+'.png','image/png'))
                                result.update(workspace_id=wid,model_calls=r['model_calls'],route='reply',queue=w.resume(wid)['queue'],message='已保存本图；核对后再执行下一张。')
                                model_calls+=r['model_calls']
                            except DrawingError as error:
                                if error.category == 'local_preflight_rejected': raise
                                gate=w.preflight(wid)
                                if not gate.get('blocking_state'):raise
                                result.update(gate)
                                model_calls+=w.image_dispatch_count(wid)-before_dispatches
                    elif wid and query in {'生成', '继续', '续做'}:
                        state = w.read(wid)
                        if query == '生成' and not state.get('work'):
                            proposal = state.get('proposal')
                            if proposal is None and enabled:
                                if state.get('current'):
                                    result.update(message='已有完整图集；可导出或按具体要求修改，不重复生成。')
                                else:
                                    planned = w.plan(wid, query);proposal = planned['plan'];model_calls += planned['model_calls']
                            if proposal is not None:
                                w.stage(wid, proposal, image_budget=stage_budget)
                        result.update(w.resume(wid));result['route'] = 'execute' if result['queue'] or w.read(wid).get('work') else 'reply'
                        if result['route'] == 'reply': result['message'] = '暂无已确认待执行图单；现阶段先查看来源、技术类型建议和具体待确认事项。'
                    elif wid and query == '导出':
                        state=w.read(wid)
                        gate=w.preflight(wid)
                        use_work = state.get('work') and not w.resume(wid)['queue'] and not gate.get('blocking_state')
                        current_block = w.current_review_block(wid) if state.get('current') and not use_work else None
                        if gate.get('blocking_state')and not state.get('current'):
                            result.update(gate,message=gate['message']+' 可用“导出已完成”取回部分PDF/ZIP；尚无可提升的完整版本。')
                        elif current_block:
                            result.update(current_block, route='reply', done='yes')
                        else:
                            packed=w.package(wid) if use_work else w.package(wid,export_current=True)
                            blobs += [(packed['pdf'], 'drawings.pdf', 'application/pdf'), (packed['zip'], 'drawings.zip', 'application/zip')]
                            state = w.read(wid);revision = state['current']['plan']['revision']
                            pending = '；当前修订' + str(state['work']['plan']['revision']) + '尚未完成' if state.get('work') else ''
                            result.update(message='已导出最近完整版本' + str(revision) + pending + '；技术状态'+packed['quality']['technical_status']+'；未调用模型。',quality=packed['quality'],export_revision=revision)
                    elif wid and query == '导出已完成':
                        packed=w.package_partial(wid)
                        blobs += [(packed['pdf'],'incomplete-drawings.pdf','application/pdf'),(packed['zip'],'incomplete-drawings.zip','application/zip')]
                        result.update(deliverable_status='incomplete_draft',missing_ids=packed['missing_ids'],quality=packed['quality'],
                            route='reply',done='yes',message='已导出已完成图件的部分PDF/ZIP；完整图集未完成，当前完整版本不变。'+review_todo_message(w.preflight(wid)))
                    elif wid and query == '导出工程示意':
                        packed = w.package_engineering_reference(wid)
                        blobs += [(packed['pdf'], 'engineering-reference.pdf', 'application/pdf'),
                                  (packed['zip'], 'engineering-reference.zip', 'application/zip')]
                        result.update(deliverable_status='engineering_reference', missing_ids=packed['missing_ids'],
                            quality=packed['quality'], route='reply', done='yes',
                            message='已导出原图和经复审的支承关系工程示意。实体空间图待验收，工作区完整版本保持原状。')
                    elif wid and query == '查看已完成':
                        partial = w.partial(wid)
                        for fid, value in partial['figures'].items():blobs.append((value['png'], 'partial-r' + str(partial['revision']) + '-' + fid + '.png', 'image/png'))
                        result.update(revision=partial['revision'], missing_ids=partial['missing_ids'], deliverable_status='incomplete_draft',
                            message='已取回当前修订已完成图件；整套尚未完成。' + review_todo_message(partial))
                    elif wid and query.startswith(('修改', '改号', '改名', '整套将', '整套把', '全套将', '全套把', '全部附图')):
                        state = w.read(wid)
                        if not state.get('current'): raise ValueError('没有完整图集可修改')
                        p = copy.deepcopy(state['current']['plan'])
                        matches = list(re.finditer(r'(\d+[a-zA-Z]?)\s*(?:改为|改成|→|->|调整为)\s*(\d+[a-zA-Z]?)', query))
                        if len(matches)>1:raise ValueError('一次整套事务仅接受一个明确标号变更；未只执行第一项。')
                        match = matches[0] if matches else None
                        if match:
                            if p.get('part_mark_mode')=='name_only':raise ValueError('name_only无既有数字标号；先审阅编号方案，未调用模型。')
                            part = next((x for x in p['parts'] if x['label'] == match.group(1)), None)
                            if not part: raise ValueError('待修改标号不存在')
                            part['label'] = match.group(2);p['revision'] += 1
                        elif enabled:
                            planned = w.plan(wid, query);p = planned['plan'];model_calls += planned['model_calls']
                        else:
                            raise ValueError('当前可用明确改号，例如“修改：标号10改为110”；其他修改先保留为待核方案')
                        result.update(w.stage(wid, p, image_budget=stage_budget))
                        result.update(route='execute', message='已定位关联图，优先仅更新标注层。')
                    elif wid and enabled and query.startswith(('规划', '生成')):
                        planned = w.plan(wid, query);result.update(planned);model_calls += planned['model_calls']
                        if query.startswith('生成'):
                            result.update(w.stage(wid, planned['plan'], image_budget=stage_budget))
                            result['route'] = 'execute'
                    if wid:
                        engineering_export = result.get('deliverable_status') == 'engineering_reference'
                        if not engineering_export:
                            preview = w.preview(wid, current_only=query == '导出')
                            result.update({k: v for k, v in preview.items() if k not in {'model_calls', 'queue', 'message'}})
                            result['message'] = preview_message({**preview, 'message': result.get('message', preview['message'])})
                        gate=w.preflight(wid)
                        result.update({k:gate[k] for k in ['deferred_blocking_state','deferred_blocked_view'] if k in gate})
                        if gate.get('blocking_state'):
                            result.update({k:v for k,v in gate.items()if k not in {'model_calls','message'}})
                            result['done']='yes';result['message']+='\n'+gate['message']
                    result['model_calls'] = model_calls
                elif action == 'stage':
                    result = w.stage(wid, load_plan(params.get('plan_json')), image_budget=stage_budget)
                    result.update(route='execute', message='图单和来源身份校验通过；技术内容仍待审阅。')
                elif action == 'step':
                    pending = w.resume(wid)['queue']
                    gate=w.preflight(wid)
                    if pending and not gate.get('blocking_state'):
                        before_dispatches=w.image_dispatch_count(wid)
                        try:
                            rendered = w.render(wid, pending[0]);result['record'] = rendered['record']
                            result['model_calls'] = rendered['model_calls']
                            blobs.append((rendered['png'], pending[0] + '.png', 'image/png'))
                        except DrawingError as error:
                            if error.category == 'local_preflight_rejected': raise
                            if not w.preflight(wid).get('blocking_state'):raise
                            result['model_calls']=w.image_dispatch_count(wid)-before_dispatches
                    queue = w.resume(wid)['queue']
                    result.update(workspace_id=wid, queue=queue, done='yes' if not queue else 'no', message='已保存本图结果，失败续做不重做已完成图。')
                    gate=w.preflight(wid)
                    if gate.get('blocking_state'):
                        result.update({k:v for k,v in gate.items()if k!='model_calls'},done='yes')
                elif action == 'package':
                    gate=w.preflight(wid)
                    if gate.get('blocking_state'):
                        result.update(gate,workspace_id=wid,done='yes')
                        holder = w.read(wid).get('work') or {}
                        contract = holder.get('plan', {}).get('visibility_contract', {}).get(gate.get('blocked_view'), {})
                        manual_partial = (gate['blocking_state'] == 'local_preflight_rejected'
                                          or contract.get('contract') == ACTUAL_CONTRACT and gate['blocking_state'] in {
                                              'awaiting_visual_review', 'technical_failed'})
                        if manual_partial:
                            result['message'] += ' 可用“导出已完成”取回部分PDF/ZIP。'
                        elif holder and w.partial(wid)['figures']:
                            packed=w.package_partial(wid)
                            blobs=[(packed['pdf'],'incomplete-drawings.pdf','application/pdf'),(packed['zip'],'incomplete-drawings.zip','application/zip')]
                            result.update(deliverable_status='incomplete_draft',missing_ids=packed['missing_ids'],quality=packed['quality'],
                                message=gate['message']+' 已交付已完成图件的部分PDF/ZIP；完整图集尚未完成。'+review_todo_message(gate))
                    else:
                        packed = w.package(wid);blobs = [(packed['pdf'], 'drawings.pdf', 'application/pdf'), (packed['zip'], 'drawings.zip', 'application/zip')]
                        result.update(workspace_id=wid, queue=[], done='yes', route='reply', quality=packed['quality'],
                            message='已交付成套附图草稿、标号/附图说明及核查记录。文件完成不等于技术或专业审阅通过。')
                elif action in {'preflight','review_status'}:
                    result=getattr(w,action)(wid) if wid else {'workspace_id':'','route':'reply','done':'yes','model_calls':0,'message':'请先提供原始技术材料。'}
                elif action == 'preview':
                    result = w.preview(wid);result.update(route='reply')
                    result['message'] = preview_message(result)
                else:
                    raise ValueError('unsupported workspace action')
                if wid:
                    gate=w.preflight(wid)
                    for field in ['expression_mode','supplier_state','semantic_state','pending_action',
                                  'failed_feature_ids','unknown_feature_ids','unreviewed_view_ids']:
                        result[field]=gate[field]
                result.setdefault('workspace_id', wid);result.setdefault('done', 'no');result.setdefault('queue', []);result.setdefault('model_calls', 0)
                result['model_execution_enabled'] = enabled
                messages = [self.create_blob_message(blob=blob, meta={'filename': name, 'file_name': name, 'mime_type': mime}) for blob, name, mime in blobs]
                messages.append(self.create_json_message(result))
        except Exception as error:
            if isinstance(error, DrawingError):
                raise ToolInvokeError(str(error)) from None
            if isinstance(error, ValueError):
                safe = str(error)
                if len(safe) > 1200: safe = '合同或来源检查失败；原件与旧图集保持，未自动重试。'
            else:
                safe = '处理或文件封装失败；原始记录与已有结果保留，未自动重试。'
            raise ToolInvokeError('[drawing_workspace_error] ' + safe) from None
        yield from messages

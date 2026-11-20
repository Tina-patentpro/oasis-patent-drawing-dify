"""Public SDK boundary: owner-bound persistent v2 workspace operations."""
import copy
import json
import os
import re
import uuid
import hashlib
from pathlib import Path

from patent_drawing import safe_tool
from patent_drawing.errors import (DrawingError, DuplicateImageRequest, PlannerResultUnknown,
                                   SourceCoverageIncomplete, ToolInvokeError)
from patent_drawing.file_input import read_file, FileSettings
from patent_drawing.intake import PLAN_WITHOUT_MODELS, planning_command
from patent_drawing.linked_edits import LINKED_CONFIRMATION
from patent_drawing.operation_store import OperationStore, canonical, digest
from patent_drawing.workspace import Workspace
from patent_drawing.visibility import ACTUAL_CONTRACT, review_todo_message
from patent_drawing.work_queue import (DOWNLOAD_TASK, EDIT, INSPECT_TASK, LABEL_EDIT,
                                       WorkActor, WorkConfig, WorkQueue)


DIRECT_COMMAND = re.compile(r'(确认图单并生成|确认图单|生成)(?:（直连对照）|\s+直连对照)')


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
    # SDK KV is tenant/plugin scoped. Only the first initialization needs
    # the shared local lock. SDK calls do not run inside a database writer.
    key = 'patent-drawing-workspace-namespace-v3'
    present = storage.exist(key)
    if type(present) is not bool:
        raise ValueError('tenant storage unavailable')
    if not present:
        with store.owner_lock('tenant-namespace-initialization'):
            present = storage.exist(key)
            if type(present) is not bool:
                raise ValueError('tenant storage unavailable')
            if not present:
                storage.set(key, uuid.uuid4().hex.encode())
    value = storage.get(key)
    if not isinstance(value, bytes) or not re.fullmatch(b'[a-f0-9]{32}', value):
        raise ValueError('invalid tenant storage namespace')
    return value.decode()


def preparation_owner(store, bridge, context, wid, scope, owner=None):
    """Exchange a conversation-bound capability for the original workspace owner."""
    from patent_drawing.image_route import valid_scope

    if not valid_scope(scope):
        raise ValueError('当前复杂图准备范围无效。')
    key = 'agent-preparation-owner:' + scope
    binding = {'context': context, 'workspace_id': wid}
    if owner is not None:
        store.save(bridge, key, {**binding, 'owner': owner})
        return owner
    try:
        receipt = store.load(bridge, key)
    except ValueError:
        raise ValueError('复杂图准备授权已失效。') from None
    if any(receipt.get(k) != v for k, v in binding.items()):
        raise ValueError('复杂图准备授权与当前会话或工作区不一致。')
    return receipt['owner']


def preview_message(result):
    patent = {'invention': '发明', 'utility_model': '实用新型', 'design': '外观设计'}
    types = {'mechanical': '机械结构', 'electrical': '电气电路', 'computer_method': '软件/计算机方法',
        'communication': '通信', 'manufacturing_process': '制造工艺', 'chemical_material': '化学材料',
        'bio_medical': '生物医学', 'semiconductor': '半导体', 'unknown': '待确认'}
    lines = [result.get('message', ''), '申请类型：' + patent.get(result.get('patent_type'), '待确认'),
        '技术类型：' + '、'.join(types.get(x, x) for x in result.get('technical_types', []))]
    if result.get('views'):
        lines += [v['id'] + '：' + v.get('purpose', '') + '（要求' + v['requested_type'] + '，交付' + v['delivered_type'] + '）' for v in result['views']]
        local_details = [v['id'] for v in result['views'] if v.get('engine') == 'parent_crop']
        if local_details:
            lines.append('按已审母图等比例放大的局部图：' + '、'.join(local_details) + '。局部放大不调用图像模型。')
        material_notice = result.get('material_relationship_mark_notice')
        heading = '部件登记（规划标号）：' if material_notice else '部件/标号：'
        lines += [heading + '；'.join((p['label'] + '—' if p['label'] else '') + p['name'] for p in result.get('parts', [])),
            '待确认：' + ('；'.join(result.get('unresolved', [])) or '仍须核查实际图文，不代表已无风险'),
            '本图单图片调用上限需求：' + str(result.get('budget', {}).get('image', 0))]
        if material_notice:
            lines.append('关系节点保留图中文字；部件表的规划标号仍须核对实际锚点，图面编号待核。')
    else:
        lines += ['建议视图（按条件选取，不是法定最少图数）：' + '；'.join(x['type'] + '：' + x['condition'] for x in result.get('recommendations', []))]
    if not result.get('model_execution_enabled'):
        lines.append('当前模型派发已关闭。')
    if result.get('blocking_state'):
        lines.append('当前停止原因：' + result['blocking_state'] + '；完整版本与未完成修订分别保留。')
    lines.append('结果为附图辅助草稿；完整说明书图文核查与实际图形审阅另行记录。')
    return '\n'.join(lines)


def linked_edit_message(edit):
    fields = {'title': '图名', 'purpose': '附图说明', 'instruction': '制图说明'}
    lines = [f'拟将 {edit["old_label"]} 改为 {edit["new_label"]}，修订由第 {edit["source_revision"]} 版进入第 {edit["target_revision"]} 版。']
    for view in edit['views']:
        prefix = f'图{view["figure_number"]}（{view["view_id"]}）'
        if view['status'] == 'ready':
            boxes = '、'.join(str(box) for box in view['allowed_boxes'])
            lines.append(f'{prefix} 允许改动像素范围 {boxes}（左、上、右、下，右和下边界不包含）。')
            for pos in view['positions']:
                if 'at' in pos:
                    text = f'标号位置 {pos["at"]}'
                    if 'target' in pos:
                        text += f'，引线端点 {pos["target"]}'
                else:
                    text = f'标号所在文字框 {pos["box"]}'
                lines.append(f'{prefix} {text}。')
        elif view['status'] == 'pending_review':
            lines.append(f'{prefix} 标号位置或可改动区域待核。')
        else:
            lines.append(f'{prefix} 同步登记或说明，图面保持原样。')
        for change in view['text_updates']:
            lines.append(f'{prefix} {fields[change["field"]]}从「{change["before"]}」更新为「{change["after"]}」。')
        if change := view.get('registration_update'):
            lines.append(f'{prefix} 图说中的部件登记从「{change["before"]}」更新为「{change["after"]}」。')
    lines.append('同步部件登记。')
    if edit['ready']:
        code = edit['confirmation_code']
        lines.append(f'请核对本版范围后回复“确认图单并生成 {code}”。仅确认范围可回复“确认图单 {code}”。')
    else:
        lines.append('请补齐图面核对，完整范围显示后再确认。')
    return '\n'.join(lines)


def show_linked_edit(result, edit):
    result.update(linked_edit=edit, route='reply', message=linked_edit_message(edit))


class PatentDrawingWorkspace(safe_tool.SafeDrawingTool):
    def _durable(self, jobs, actor, params, action, wid, enabled):
        from patent_drawing.operator_policy import effective_image_budget, load_policy

        query = (params.get('query') or '').strip()
        revision = params.get('expected_revision')
        scope = params.get('scope')
        blobs = []
        if action == 'inspect_work':
            download = DOWNLOAD_TASK.fullmatch(query)
            explicit = download or INSPECT_TASK.fullmatch(query)
            task_id = params.get('task_id') or (explicit[1] if explicit else None)
            kind = 'agent_prepare' if scope else None
            result = jobs.inspect(actor, None if task_id else wid, task_id=task_id, kind=kind, scope=scope)
            if download and result.get('file_hashes'):
                blobs = self._work_files(jobs.files(actor, task_id=task_id))
                if result.get('export_revision') and 'drawings.pdf' in result['file_hashes']:
                    result['deliverable_status'] = 'complete_draft'
                else:
                    result['deliverable_status'] = 'incomplete_draft' if result['job_status'] != 'completed' else 'engineering_reference'
            if result['job_status'] == 'not_started' and wid and not scope:
                result.update(Workspace(jobs.store, actor.owner).unlocked_progress(wid))
                result['message'] = '当前会话尚无持久任务。' + result['message']
            return result, blobs
        if action == 'accept_image_preparation':
            result = jobs.inspect(actor, wid, task_id=params['task_id'], kind='agent_prepare', scope=scope)
            job, receipt = jobs.preparation_receipt(actor, wid, result['task_id'], scope)
            intake = jobs.store.load(actor.owner, 'intake:active')
            if (intake['workspace_id'] != wid or intake['confirmed_revision'] != intake['revision']
                    or intake['revision'] != job['intake_revision'] or str(intake['revision']) != str(revision)):
                raise ValueError('复杂图准备回执跨越了已确认来源修订。')
            workspace = Workspace(jobs.store, actor.owner)
            with jobs.store.owner_lock(actor.owner):
                latest = workspace.read(wid)
                accepted = latest.get('image_route_proposal') or {}
                if accepted.get('scope') != scope or accepted.get('preparation_receipt') != digest(receipt):
                    verified = workspace.accept_image_preparation(
                        wid, scope, canonical({'scope': scope, 'views': receipt['views']}),
                        durable_receipt=receipt)
                    result['message'] = verified['message'] + ' 可回复“确认图单并生成”。'
            result['preparation_status'] = 'accepted'
            return result, blobs
        budget = effective_image_budget(int(params.get('image_budget') or 0))
        policy = None
        if enabled:
            source = load_policy(os.environ['PATENT_DRAWING_OPERATOR_POLICY'])
            policy = {'sha256': source.sha256, 'run_id': source.run_id, 'limits': source.limits}
        kind = params.get('work_kind')
        expected_work = params.get('expected_work_identity', '')
        branch, intake_wid = 'agent', wid
        if action == 'admit_work':
            workspace = Workspace(jobs.store, actor.owner)
            state = jobs.store.load(actor.owner, 'workspace:' + wid)
            current_job = jobs.inspect(actor, wid)
            if (direct := DIRECT_COMMAND.fullmatch(query)):
                branch = 'direct'
                with jobs.store.owner_lock(actor.owner):
                    wid = jobs.comparison_workspace(actor, intake_wid)
                    intake = jobs.store.load(actor.owner, 'intake:active')
                    if (intake['workspace_id'] != intake_wid or str(intake['revision']) != str(revision)
                            or intake['revision'] != intake['confirmed_revision']):
                        raise ValueError('对照确认范围已变化，请核对当前来源。')
                    branch_state = workspace.read(wid)
                    if direct[1] != '生成' and branch_state.get('proposal') and not branch_state.get('work'):
                        origin = workspace.read(intake_wid)
                        comparison = branch_state['comparison']
                        approved = (origin.get('work') or origin.get('current') or {}).get('image_budget')
                        if approved is not None and approved != budget:
                            raise ValueError('同版对照必须沿用原图单确认的图像预算。')
                        workspace.stage(wid, branch_state['proposal'], image_budget=budget,
                                        require_image_route=True, require_agent_preparation=False,
                                        require_complete_marks=True)
                    if direct[1] == '确认图单':
                        return {'workspace_id': wid, 'route': 'reply', 'job_status': 'waiting_input',
                                'comparison_branch': branch, 'work_identity': workspace.work_identity(wid),
                                'message': '已确认同版直连对照图单。可回复“生成（直连对照）”。'}, []
                    kind = 'draw'
            elif query == '继续准备复杂图':
                route = state.get('image_route_proposal') or {}
                scope = route.get('scope')
                if route.get('complex_ids') and scope:
                    jobs.agent_actor(actor.tenant, actor.conversation, wid, scope, revision, user=actor.user)
                    pending = jobs.inspect(actor, wid, kind='agent_prepare', scope=scope)
                    if pending['job_status'] != 'not_started':
                        return {**pending, 'route': 'agent_poll'}, []
                    return {'workspace_id': wid, 'route': 'agent', 'scope': scope,
                            'message': '准备已核复杂图范围。'}, []
                if current_job['job_status'] in {'queued', 'running', 'unknown'}:
                    return current_job, []
                kind = 'image_route'
            elif query in {'继续', '续做', '生成', '执行单图'} and state.get('work') and not params.get('plan_json'):
                if query in {'继续', '续做', '执行单图'} and current_job.get('work_kind') == 'draw':
                    return jobs.continue_work(actor, wid), []
                kind = 'draw'
            elif (confirmation := LINKED_CONFIRMATION.fullmatch(query)):
                prior = jobs.confirmed_draw(actor, wid, state, confirmation[2])
                if prior:
                    return prior, []
                return {'workspace_id': wid, 'route': 'receive', 'message': ''}, []
            elif query in {'继续', '续做'}:
                return current_job, []
            elif query in {'确认工程示意改号', '确认结构示意改号'}:
                kind = 'engineering_edit' if query == '确认工程示意改号' else 'structural_edit'
            elif (Workspace.figure_route_query(state, query, bool(params.get('plan_json')))
                  and not (EDIT.match(query) and LABEL_EDIT.search(query))):
                kind = 'plan'
            else:
                return {'workspace_id': wid, 'route': 'receive', 'message': ''}, []
        return jobs.enqueue(actor, wid, revision, kind, query=query, scope=scope,
                            expected_work=expected_work, image_budget=budget,
                            allow_models=enabled, policy=policy, branch=branch, intake_wid=intake_wid), blobs

    @staticmethod
    def _work_files(files):
        kinds = {'png': 'image/png', 'pdf': 'application/pdf', 'zip': 'application/zip',
                 'manifest': 'application/json', 'json': 'application/json'}
        return [(blob, name, kinds[name.rsplit('.', 1)[-1]]) for name, blob in files.items()]

    def _intake(self, workspace, params, *, durable=False):
        from patent_drawing.intake import Intake, command, is_source_supplement
        claims_only = params.get('source_mode') == 'claims_only'
        if claims_only or params.get('source_mode') == 'mixed_materials':
            if not durable:
                raise ValueError('权利要求规划须通过持久任务工作流。')
        classifier = None
        if not durable:
            from patent_drawing.intake_model import IntakeClassifier
            classifier = IntakeClassifier(workspace.store, workspace.owner, self.runtime.credentials.get('openai_api_key'))
        intake = Intake(workspace, classifier=classifier)
        current = intake.read()
        query = (params.get('query') or '').strip()
        materials = (params.get('materials') or '').strip()
        if materials and hashlib.sha256(materials.encode()).hexdigest() == current.get('form_material_sha'):
            materials = ''
        if query in {'重置', '新案件'}:
            return intake.apply(params)
        operation = params.get('operation_id')
        if operation and re.fullmatch(r'[A-Za-z0-9_-]{1,120}', operation):
            try:
                workspace.store.load(workspace.owner, 'intake-op:' + operation)
            except ValueError:
                pass
            else:
                return intake.apply(params)
        files = params.get('files') or []
        if not isinstance(files, list) or len(files) > 6:
            raise ValueError('source file count exceeded')
        raw = []
        remaining = 16 * 1024 * 1024
        settings = FileSettings(self.runtime.credentials.get('dify_file_origin') or '',
                                self.runtime.credentials.get('dify_internal_files_url') or '')
        for file in files:
            blob = read_file(file, remaining, settings)
            remaining -= len(blob)
            name = (file.get('filename') or file.get('name')) if isinstance(file, dict) else getattr(file, 'filename', None)
            if not name or remaining < 0:
                raise ValueError('invalid source name or aggregate capacity')
            raw.append({'name': name, 'blob': blob})
        if not current['bundle'] and not materials and not files and query and not command(query) and query not in {'确认', '重置', '新案件'}:
            materials = query
        if materials:
            raw.append({'name': 'user-material.txt', 'blob': materials.encode()})
        if not claims_only and current['bundle'] and is_source_supplement(query, current['pending_question'],
                                                      supplied_source=bool(files or materials)):
            if not files and not materials:
                raw.append({'name': 'supplement-' + hashlib.sha256(query.encode()).hexdigest()[:16] + '.txt', 'blob': query.encode()})
            originals = [{'name': f['name'], 'blob': workspace.store.get_artifact(workspace.owner, f)} for f in current['originals']]
            raw = originals + raw
        if len(raw) > 6 or sum(len(f['blob']) for f in raw) > 16 * 1024 * 1024:
            raise ValueError('source aggregate capacity exceeded')
        return intake.apply(params, raw or None)

    def _invoke(self, tool_parameters):
        try:
            params = tool_parameters
            if not isinstance(params, dict): raise ValueError('parameters invalid')
            context = params.get('context_id', '')
            if not self.runtime.user_id or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', context):
                raise ValueError('runtime user/context binding required')
            store = OperationStore(Path(os.environ.get('PATENT_DRAWING_STATE_ROOT', '/app/storage/patent-drawing-v3')))
            tenant_namespace = namespace(store, self.session.storage)
            owner = tenant_namespace + ':' + self.runtime.user_id + ':' + context
            bridge = tenant_namespace + ':agent-preparation'
            action = params.get('action', 'receive')
            wid = params.get('workspace_id', '')
            durable = params.get('durable_work') == 'yes' or action in {
                'enqueue_work', 'inspect_work', 'admit_work'}
            if durable:
                config = WorkConfig.load()
                jobs = WorkQueue(store, config)
                credential_app = self.runtime.credentials.get('work_app_id')
                actor = WorkActor(tenant_namespace, self.runtime.user_id, context)
                config.check_session(self.session, credential_app, context)
                if action == 'enqueue_work' and params.get('work_kind') == 'agent_prepare':
                    raw_handoff = params.get('preparation_json')
                    if not isinstance(raw_handoff, str) or not 1 <= len(raw_handoff) <= 512:
                        raise ValueError('缺少本轮 Agent 交接回执。')
                    try:
                        handoff = json.loads(raw_handoff)
                    except json.JSONDecodeError as error:
                        raise ValueError('Agent 交接回执格式无效。') from error
                    scope, revision = params.get('scope'), params.get('expected_revision')
                    if handoff != {'context_id': context, 'workspace_id': wid,
                                   'scope': scope, 'expected_revision': revision}:
                        raise ValueError('Agent 交接与当前会话、工作区或修订不一致。')
                    jobs.agent_actor(tenant_namespace, context, wid, scope, revision, user=self.runtime.user_id)
            if action == 'prepare_complex_images' and not durable:
                owner = preparation_owner(store, bridge, context, wid, params.get('scope'))
            from patent_drawing.operator_policy import model_execution_enabled
            enabled = action != 'inspect_work' and model_execution_enabled()
            if durable and action not in {'receive', 'intake'}:
                result, blobs = self._durable(jobs, actor, params, action, wid, enabled)
                result['model_execution_enabled'] = enabled
                result.setdefault('model_calls', 0)
                for blob, name, mime in blobs:
                    yield self.create_blob_message(blob=blob, meta={'filename': name, 'file_name': name, 'mime_type': mime})
                yield self.create_json_message(result)
                return
            if durable and action == 'intake' and wid and not params.get('files'):
                from patent_drawing.intake import Intake, command
                active = Intake(Workspace(store, owner)).read()
                query = (params.get('query') or '').strip()
                materials = (params.get('materials') or '').strip()
                unchanged_material = not materials or active.get('form_material_sha') == hashlib.sha256(materials.encode()).hexdigest()
                if (command(query) and unchanged_material and wid == active['workspace_id']
                        and str(active['revision']) == str(params.get('expected_revision'))
                        and active['confirmed_revision'] == active['revision']):
                    result = Intake(Workspace(store, owner)).response(active, route='forward')
                    result['model_execution_enabled'] = enabled
                    yield self.create_json_message(result)
                    return
            from patent_drawing.operator_policy import effective_image_budget
            stage_budget=effective_image_budget(int(params.get('image_budget')or 0))
            if action in {'intake', 'receive'} and (params.get('query') or '').strip() == '查看进度':
                from patent_drawing.intake import Intake
                candidate = Workspace(store, owner)
                active = Intake(candidate).read()
                if (wid and active['workspace_id'] == wid
                        and str(active['revision']) == str(params.get('expected_revision'))
                        and active['confirmed_revision'] == active['revision']):
                    try:
                        with store.owner_lock(owner):
                            pass
                    except ValueError as error:
                        if not str(error).startswith('workspace busy;'):
                            raise
                        snapshot = candidate.unlocked_progress(wid)
                        if action == 'intake':
                            snapshot.update(intake_id=active['intake_id'], intake_revision=str(active['revision']),
                                            **{field: active['fields'].get(field, {}).get('value', '')
                                               for field in ('patent_type', 'technical_type', 'application_stage')})
                        snapshot['model_execution_enabled'] = enabled
                        yield self.create_json_message(snapshot)
                        return
            with store.owner_lock(owner):
                from patent_drawing.client import OpenAIImagesClient
                from patent_drawing.text_client import OpenAITextClient
                from patent_drawing.figure_routing import DecisionClient
                key = self.runtime.credentials.get('openai_api_key')
                w = Workspace(store, owner, allow_models=enabled and not durable,
                    image_client=OpenAIImagesClient(key) if enabled and not durable else None,
                    text_client=OpenAITextClient(key) if enabled and not durable else None,
                    decision_client=DecisionClient(key) if enabled and not durable and action in {'route_figures', 'route_images'} else None)
                result = {}; blobs = []
                expected = params.get('expected_revision')
                expected_work = params.get('expected_work_identity')
                stale_intake = False
                if action != 'intake' and expected not in (None, ''):
                    from patent_drawing.intake import Intake
                    intake_state = Intake(w).read()
                    stale_intake = (str(intake_state['revision']) != str(expected)
                        or intake_state['workspace_id'] != wid
                        or intake_state['confirmed_revision'] != intake_state['revision'])
                if stale_intake:
                    wid = intake_state['workspace_id']
                    result = {'workspace_id': wid, 'route': 'reply', 'done': 'yes', 'model_calls': 0,
                        'blocking_state': 'intake_revision_changed', 'pending_action': 'confirm_intake',
                        'message': '采集信息已更新，本次操作停止。请核对并确认当前材料后重新发出指令。'}
                elif (action in {'step', 'preflight', 'review_status', 'package'}
                      and expected_work and wid and expected_work != w.work_identity(wid)):
                    result = {'workspace_id': wid, 'route': 'reply', 'done': 'yes', 'model_calls': 0,
                              'blocking_state': 'work_revision_changed', 'execution_state': 'paused',
                              'message': '图单或确认范围已经更新。可发送“查看进度”核对当前修订。'}
                elif action == 'intake':
                    result = self._intake(w, params, durable=durable)
                    wid = result['workspace_id']
                    if (params.get('query') or '').strip() == '查看原图候选':
                        from patent_drawing.intake import Intake
                        intake = Intake(w)
                        collecting = intake.read()
                        options = intake.source_image_candidates(collecting)
                        if len(options) > 12:
                            result['message'] = '原件候选较多，请先上传需要保留的图页或原图。'
                        elif options and collecting['pending_question'] == 'figure_presence':
                            entries = []
                            for index, candidate in enumerate(options, 1):
                                intrinsic, page = intake.source_image(collecting, candidate)
                                blobs.append((intrinsic, f'original-candidate-{index}.png', 'image/png'))
                                entries.append(f'{index}. 第 {page["page"]} 页，页图区域 {candidate["bbox"]}，预览为对象原始像素，原件 SHA-256 {page["source_sha256"]}')
                            result['message'] = '\n'.join(entries) + '\n请核对后回复“选择原图”及对应编号。'
                elif action == 'route_figures':
                    result = w.route_figures(wid, (params.get('query') or '').strip(),
                                             imported_plan=bool(params.get('plan_json')))
                elif action == 'route_images':
                    result = w.route_images(wid, (params.get('query') or '').strip())
                    if result.get('route') == 'agent':
                        preparation_owner(store, bridge, context, wid, result['scope'], owner=owner)
                elif action == 'prepare_complex_images':
                    result = {'workspace_id': wid, **w.prepare_complex_images(wid, params.get('scope'))}
                elif action == 'accept_image_preparation':
                    result = w.accept_image_preparation(wid, params.get('scope'), params.get('preparation_json'))
                elif action == 'receive':
                    query = (params.get('query') or '').strip(); files = params.get('files') or []; materials = (params.get('materials') or '').strip()
                    confirmation = LINKED_CONFIRMATION.fullmatch(query)
                    routing = load_plan(params['figure_routing_json']) if params.get('figure_routing_json') else None
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
                        result.update(w.stage(wid, load_plan(params['plan_json']), image_budget=stage_budget,
                                              require_linked_confirmation=True, require_complete_marks=expected not in (None, '')))
                        if not result.get('blocking_state'):
                            result['route'] = 'execute' if query == '生成' else 'reply'
                            result['message'] = '已校验图单。' + w.preview(wid)['message']
                            if result.get('linked_edit'):
                                show_linked_edit(result, w.linked_edit_preview(wid))
                    elif wid and PLAN_WITHOUT_MODELS.search(query) and (planning_command(query) or query.startswith(('生成', '修改'))):
                        result.update(message='可以预览交底及现有图单，或导入已核图单。')
                    elif wid and confirmation:
                        verb, scope_code = confirmation.groups()
                        state = w.read(wid)
                        if (state.get('work') or {}).get('transaction'):
                            result.update(w.confirm_linked_edit(wid, scope_code))
                            if result['confirmed']:
                                result.update(route='execute' if verb == '确认图单并生成' else 'reply',
                                              message='已确认本版关联改号。可逐张执行并核对图件。')
                            else:
                                show_linked_edit(result, result['linked_edit'])
                        elif scope_code:
                            result.update(route='reply', done='yes', message='当前没有与此确认码匹配的关联改号。')
                        elif state.get('planning_feedback'):
                            result.update(state['planning_feedback'], route='reply', done='yes')
                        else:
                            proposal = state.get('proposal')
                            if proposal is None:
                                raise ValueError('没有已校验的候选图单')
                            if (durable and enabled and state.get('image_route_proposal', {}).get('complex_ids')
                                    and not state['image_route_proposal'].get('preparation_receipt')):
                                raise ValueError('请先在本版原生 Agent 准备后发送“查看进度”，核对已保存的准备回执。')
                            result.update(w.stage(wid, proposal, image_budget=stage_budget, require_linked_confirmation=True,
                                                  require_image_route=enabled and params.get('require_image_route') in {'agent', 'direct'},
                                                  require_agent_preparation=params.get('require_image_route') != 'direct',
                                                  require_complete_marks=expected not in (None, '')))
                            result.update(route='execute' if query == '确认图单并生成' else 'reply',
                                          message='已确认图单。可逐张执行并核对图件。')
                            if result.get('linked_edit'):
                                show_linked_edit(result, w.linked_edit_preview(wid))
                    elif wid and query == '预览' and (w.read(wid).get('work') or {}).get('transaction'):
                        show_linked_edit(result, w.linked_edit_preview(wid))
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
                                if error.category == 'local_preflight_rejected' and not isinstance(error, DuplicateImageRequest):
                                    raise
                                gate=w.preflight(wid)
                                if not gate.get('blocking_state'):raise
                                result.update(gate)
                                model_calls+=w.image_dispatch_count(wid)-before_dispatches
                    elif wid and query == '查看进度':
                        result.update(w.progress(wid), route='reply', execution_stage='inspect')
                    elif wid and query == '确认无需附图':
                        result.update(w.confirm_no_drawings(wid), execution_stage='inspect')
                    elif wid and query == '继续准备复杂图':
                        state = w.read(wid)
                        if state.get('proposal'):
                            result.update(plan=state['proposal'], execution_stage='proposed',
                                          message='已读取当前候选图单，继续核对复杂图准备回执。')
                        else:
                            result.update(message='没有待准备的候选图单。可发送“查看进度”。')
                    elif wid and query in {'生成', '继续', '续做'}:
                        state = w.read(wid)
                        if state.get('work'):
                            result.update(w.resume(wid), route='execute')
                        elif query in {'继续', '续做'} and state.get('pending_plan') and not durable:
                            result.update(w.resume_planning(wid), route='reply')
                        elif (not durable and query in {'继续', '续做'} and routing
                              and (pending := w._pending_figure_decision(state))):
                            receipt = store.load(owner, pending['operation_id'] + ':receipt')
                            if receipt['state'] == 'selected' and not enabled:
                                result.update(w.progress(wid), route='reply')
                            else:
                                planned = w.plan(wid, pending['query'], figure_routing=routing)
                                model_calls += planned['model_calls']
                                result.update(planned, route='reply')
                        elif state.get('planning_feedback'):
                            result.update(state['planning_feedback'], route='reply', done='yes')
                        elif query == '生成' and enabled and not durable and not state.get('current'):
                            if state.get('proposal') is None:
                                planned = w.plan(wid, '规划', figure_routing=routing)
                                model_calls += planned['model_calls']
                                result.update(planned)
                            if result.get('plan') or state.get('proposal'):
                                result.update(route='reply', message='候选图单已准备。请核对后回复“确认图单并生成”。')
                        elif state.get('proposal'):
                            result.update(message='请先核对候选图单，再回复“确认图单并生成”。')
                        elif state.get('current'):
                            result.update(message='已有完整图集，可导出或提出具体修改。')
                        else:
                            result.update(message='暂无待执行图单。请先查看来源和待确认事项。')
                    elif wid and query in {'核查申报图件', '导出申报图件'}:
                        release = query == '导出申报图件'
                        filing = w.filing_candidate(wid, release=release)
                        manifest = filing['manifest']
                        result.update(route='reply', done='yes', filing_status=manifest['filing_status'],
                                      deliverable_status=manifest['deliverable_status'],
                                      filing_next_actions=list(dict.fromkeys(
                                          row['rule_id'] + ' ' + row['next_action']
                                          for row in filing['filing_review']['findings'] if row['verdict'] != 'PASS')))
                        if 'pdf' in filing:
                            name = 'filing-drawings' if manifest['filing_ready'] else 'filing-review-candidate'
                            blobs += [(filing['pdf'], name + '.pdf', 'application/pdf'),
                                      (filing['zip'], name + '.zip', 'application/zip')]
                        result['message'] = ('当前同版图件通过申报图件核查。' if manifest['filing_ready']
                                             else '当前图件须完成以下核查才可作申报图件。')
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
                            pending = f' 当前修订{state["work"]["plan"]["revision"]}尚未完成。' if state.get('work') else ''
                            figure_count = len(packed['manifest']['figures'])
                            status = packed['quality']['technical_status']
                            result.update(route='reply', done='yes', deliverable_status='complete_draft',
                                          technical_status=status, quality=packed['quality'], export_revision=revision,
                                          message=f'已导出最近完整版本{revision}，共 {figure_count} 张图。技术状态 {status}。'
                                                  f'本次导出未调用模型。图单与附图说明见下载文件。{pending}')
                    elif wid and (history_command := re.fullmatch(r'导出(上次来源)?版本\s*([1-9][0-9]{0,6})', query)):
                        revision = int(history_command[2])
                        prior_source = bool(history_command[1])
                        selected = w.read(wid).get('previous_workspace') if prior_source else wid
                        if not selected:
                            raise ValueError('当前会话未保存上次来源的工作区。')
                        packed = w.package(selected, export_current=True, revision=revision)
                        provenance = '上次来源的' if prior_source else ''
                        blobs += [(packed['pdf'], f'drawings-r{revision}.pdf', 'application/pdf'),
                                  (packed['zip'], f'drawings-r{revision}.zip', 'application/zip')]
                        result.update(route='reply', done='yes', export_revision=revision,
                                      deliverable_status='historical_revision',
                                      technical_status=packed['quality']['technical_status'],
                                      message=f'已取回{provenance}完整图集第 {revision} 版。')
                    elif wid and query == '导出已完成':
                        packed=w.package_partial(wid)
                        if 'pdf' not in packed:
                            result.update(packed)
                        else:
                            blobs += [(packed['pdf'],'incomplete-drawings.pdf','application/pdf'),(packed['zip'],'incomplete-drawings.zip','application/zip')]
                            result.update(deliverable_status='incomplete_draft',missing_ids=packed['missing_ids'],quality=packed['quality'],
                                route='reply', done='yes',
                                message='已导出已完成图件的部分 PDF／ZIP。本次导出未新增模型调用，当前完整版本不变。\n'
                                        + review_todo_message(w.preflight(wid)))
                    elif wid and (query in {'导出结构示意', '确认结构示意改号'}
                                  or re.fullmatch(r'导出结构示意版本\s+[a-f0-9]{64}', query)):
                        if query == '确认结构示意改号':
                            w.confirm_structural_label_edit(wid)
                        reference_id = query.split()[-1] if query.startswith('导出结构示意版本') else None
                        packed = w.package_structural_reference(wid, reference_id=reference_id)
                        if 'pdf' in packed:
                            blobs += [(packed['pdf'], 'structural-reference.pdf', 'application/pdf'),
                                      (packed['zip'], 'structural-reference.zip', 'application/zip')]
                            result.update(deliverable_status='structural_reference', missing_ids=packed['missing_ids'],
                                          quality=packed['quality'],
                                          message='已导出来源关系结构示意。实体空间图和专业审阅待验收。')
                            if packed['quality'].get('detail_binding_status'):
                                result['message'] += ' 总图与局部图位置核查：' + packed['quality']['detail_binding_status'] + '。'
                            if any('relation_detail_kind' in row for row in packed['manifest']['figures']):
                                result['message'] += ' 部分图页附有无比例的局部关系示意。'
                            if packed['quality'].get('opening_parent_location_status') == 'UNKNOWN':
                                result['message'] += ' 开口在总图中的具体位置待核。'
                            result['message'] += ' 结构示意版本：' + packed['reference_id'] + '。'
                        else:
                            result.update(deliverable_status='structural_reference_pending', message=packed['message'])
                        result.update(route='reply', done='yes')
                    elif wid and (query in {'导出工程示意', '确认工程示意改号'}
                                  or re.fullmatch(r'导出工程示意版本\s*[1-9][0-9]{0,6}', query)):
                        if query == '确认工程示意改号':
                            packed = w.confirm_engineering_label_edit(wid)
                        else:
                            revision = int(query.removeprefix('导出工程示意版本').strip()) if query != '导出工程示意' else None
                            packed = w.package_engineering_reference(wid, revision=revision)
                        blobs += [(packed['pdf'], 'engineering-reference.pdf', 'application/pdf'),
                                  (packed['zip'], 'engineering-reference.zip', 'application/zip')]
                        revised = packed['reference_revision_status'] == 'reference_revision_not_reviewed'
                        detail = '已导出关联改号稿，改号稿待复审。' if revised else '已导出原图和经复审的支承关系工程示意。'
                        result.update(deliverable_status='engineering_reference', missing_ids=packed['missing_ids'],
                            quality=packed['quality'], route='reply', done='yes',
                            export_revision=packed['reference_revision'],
                            message=f'工程示意第 {packed["reference_revision"]} 版。{detail}实体空间图待验收。')
                    elif wid and query == '查看已完成':
                        partial = w.partial(wid)
                        for fid, value in partial['figures'].items():blobs.append((value['png'], 'partial-r' + str(partial['revision']) + '-' + fid + '.png', 'image/png'))
                        result.update(revision=partial['revision'], missing_ids=partial['missing_ids'], deliverable_status='incomplete_draft',
                            message='已取回当前修订已完成图件；整套尚未完成。' + review_todo_message(partial))
                    elif wid and query.startswith(('修改', '改号', '改名', '整套将', '整套把', '全套将', '全套把', '全部附图')):
                        state = w.read(wid)
                        matches = list(re.finditer(r'(\d+[a-zA-Z]?)\s*(?:改为|改成|→|->|调整为)\s*(\d+[a-zA-Z]?)', query))
                        if len(matches)>1:raise ValueError('一次整套事务仅接受一个明确标号变更；未只执行第一项。')
                        match = matches[0] if matches else None
                        p = copy.deepcopy((state.get('current') or {}).get('plan'))
                        if p is None and state.get('engineering_reference') and match:
                            change = w.stage_engineering_label_edit(wid, *match.groups())
                            numbers = change['figure_numbers']
                            scope = '、'.join(f'图{numbers[fid]}' for fid in change['visible_edit_view_ids'])
                            regions = '；'.join(f'图{numbers[fid]} {boxes}' for fid, boxes in change['regions'].items())
                            status = (f'核定像素范围 {regions}。将同步图面、部件登记和附图说明。'
                                      '请核对后回复“确认工程示意改号”。') if change['ready'] else '待核定原嵌字及示意图允许改动的区域。'
                            result.update(route='reply', done='yes', deliverable_status='engineering_reference',
                                engineering_label_edit=change,
                                message=f'工程示意拟将 {change["old_label"]} 改为 {change["new_label"]}，涉及{scope}。{status}')
                        elif p is None and state.get('structural_reference') and match:
                            change = w.stage_structural_label_edit(wid, *match.groups())
                            numbers = {v['id']: i for i, v in enumerate(state['work']['plan']['views'], 1)}
                            scope = '、'.join(f'图{numbers[fid]}' for fid in change['visible_edit_view_ids'])
                            result.update(route='reply', done='yes', deliverable_status='structural_reference',
                                message=f'结构示意拟将 {change["old_label"]} 改为 {change["new_label"]}，'
                                        f'涉及{scope}。请核对后回复“确认结构示意改号”。')
                        elif p is None:
                            raise ValueError('没有完整图集可修改')
                        elif match:
                            if p.get('part_mark_mode')=='name_only':raise ValueError('name_only无既有数字标号；先审阅编号方案，未调用模型。')
                            part = next((x for x in p['parts'] if x['label'] == match.group(1)), None)
                            if not part: raise ValueError('待修改标号不存在')
                            part['label'] = match.group(2);p['revision'] += 1
                        elif enabled and not durable:
                            planned = w.plan(wid, query, figure_routing=routing)
                            p = planned.get('plan')
                            model_calls += planned['model_calls']
                            if not p:
                                result.update(planned)
                        else:
                            raise ValueError('当前可用明确改号，例如“修改：标号10改为110”；其他修改先保留为待核方案')
                        if p:
                            result.update(w.stage(wid, p, image_budget=stage_budget, require_linked_confirmation=True,
                                                  require_complete_marks=expected not in (None, ''),
                                                  require_image_route=params.get('require_image_route') in {'agent', 'direct'}))
                            if result.get('linked_edit'):
                                show_linked_edit(result, w.linked_edit_preview(wid))
                            else:
                                result.update(route='execute', message='已定位关联图，优先仅更新标注层。')
                    elif wid and enabled and not durable and (planning_command(query) or query.startswith('生成')):
                        planned = w.plan(wid, query, figure_routing=routing);result.update(planned);model_calls += planned['model_calls']
                        if query.startswith('生成') and planned.get('plan'):
                            result['message'] += ' 请核对后回复“确认图单并生成”。'
                    if wid and result.get('deliverable_status') != 'historical_revision':
                        engineering_export = result.get('deliverable_status') in {
                            'engineering_reference', 'structural_reference', 'structural_reference_pending'}
                        if not engineering_export:
                            preview = w.preview(wid, current_only=query == '导出')
                            result.update({k: v for k, v in preview.items() if k not in {'model_calls', 'queue', 'message'}})
                            if result.get('deliverable_status') not in {'incomplete_draft', 'complete_draft'}:
                                result['message'] = preview_message({**preview, 'message': result.get('message', preview['message'])})
                        gate=w.preflight(wid) if not durable or result.get('route') != 'execute' else {}
                        result.update({k:gate[k] for k in ['deferred_blocking_state','deferred_blocked_view'] if k in gate})
                        if gate.get('blocking_state') and query != '继续准备复杂图':
                            result.update({k:v for k,v in gate.items()if k not in {'model_calls','message'}})
                            result['done']='yes';result['message']+='\n'+gate['message']
                    result['model_calls'] = model_calls
                elif action == 'stage':
                    result = w.stage(wid, load_plan(params.get('plan_json')), image_budget=stage_budget,
                                     require_linked_confirmation=True, require_complete_marks=expected not in (None, ''))
                    if not result.get('blocking_state'):
                        result.update(route='execute', message='图单和来源身份校验通过，技术内容仍待审阅。')
                        if result.get('linked_edit'):
                            show_linked_edit(result, w.linked_edit_preview(wid))
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
                            if error.category == 'local_preflight_rejected' and not isinstance(error, DuplicateImageRequest):
                                raise
                            if not w.preflight(wid).get('blocking_state'):raise
                            result['model_calls']=w.image_dispatch_count(wid)-before_dispatches
                    queue = w.resume(wid)['queue']
                    result.update(workspace_id=wid, queue=queue, done='yes' if not queue else 'no', message='已保存本图结果，失败续做不重做已完成图。')
                    gate=w.preflight(wid)
                    if gate.get('blocking_state'):
                        result.update({k:v for k,v in gate.items()if k!='model_calls'},done='yes')
                elif action == 'package':
                    gate = w.progress(wid)
                    if gate.get('blocking_state'):
                        result.update(gate,workspace_id=wid,done='yes')
                        holder = w.read(wid).get('work') or {}
                        contract = holder.get('plan', {}).get('visibility_contract', {}).get(gate.get('blocked_view'), {})
                        manual_partial = (gate['blocking_state'] == 'local_preflight_rejected'
                                          or contract.get('contract') == ACTUAL_CONTRACT and gate['blocking_state'] in {
                                              'awaiting_visual_review', 'technical_failed'})
                        if manual_partial and holder and w.partial(wid)['figures']:
                            result['message'] += ' 可用“导出已完成”取回部分PDF/ZIP。'
                        else:
                            packed=w.package_partial(wid)
                            if 'pdf' in packed and 'zip' in packed:
                                blobs=[(packed['pdf'],'incomplete-drawings.pdf','application/pdf'),(packed['zip'],'incomplete-drawings.zip','application/zip')]
                                result.update(deliverable_status='incomplete_draft',missing_ids=packed['missing_ids'],quality=packed['quality'],
                                    message=gate['message']+' 已交付已完成图件的部分PDF/ZIP；完整图集尚未完成。'+review_todo_message(gate))
                            else:
                                result.update(packed)
                    elif gate['execution_state'] == 'ready_to_package':
                        packed = w.package(wid);blobs = [(packed['pdf'], 'drawings.pdf', 'application/pdf'), (packed['zip'], 'drawings.zip', 'application/zip')]
                        result.update(workspace_id=wid, queue=[], done='yes', route='reply', quality=packed['quality'],
                            execution_state='completed', execution_stage='package', deliverable_status='complete_draft',
                            export_revision=w.read(wid)['current']['plan']['revision'],
                            message='已保存成套附图草稿、标号与附图说明及核查记录。技术及专业审阅状态随包记录。')
                    else:
                        result.update(gate, route='reply', done='yes', execution_stage='package')
                elif action in {'preflight','review_status'}:
                    result=getattr(w,action)(wid) if wid else {'workspace_id':'','route':'reply','done':'yes','model_calls':0,'message':'请先提供原始技术材料。'}
                elif action == 'preview':
                    result = w.preview(wid);result.update(route='reply')
                    result['message'] = preview_message(result)
                else:
                    raise ValueError('unsupported workspace action')
                if (wid and action == 'receive' and result.get('plan') and not result.get('blocking_state')
                        and result.get('route') != 'execute'
                        and (planning_command(query) or query.startswith('生成')
                             or query in {'继续', '续做'})):
                    result['execution_stage'] = 'proposed'
                if (wid and action in {'receive', 'stage', 'step', 'preview'}
                        and result.get('deliverable_status') != 'historical_revision'
                        and not result.get('blocking_state') == 'intake_revision_changed'
                        and not (durable and result.get('route') == 'execute')):
                    gate=w.preflight(wid)
                    for field in ['expression_mode','supplier_state','semantic_state','pending_action',
                                  'failed_feature_ids','unknown_feature_ids','unreviewed_view_ids']:
                        result[field]=gate[field]
                    for field in ('supplier_event', 'original_request_id'):
                        if field in gate:
                            result.setdefault(field, gate[field])
                result.setdefault('workspace_id', wid);result.setdefault('done', 'no');result.setdefault('queue', []);result.setdefault('model_calls', 0)
                if wid and action == 'receive' and result.get('route') == 'execute':
                    result['work_identity'] = w.work_identity(wid)
                result['model_execution_enabled'] = enabled
                messages = [self.create_blob_message(blob=blob, meta={'filename': name, 'file_name': name, 'mime_type': mime}) for blob, name, mime in blobs]
                messages.append(self.create_json_message(result))
        except SourceCoverageIncomplete as error:
            messages = [self.create_json_message({
                'workspace_id': wid, 'route': 'reply', 'done': 'yes', 'queue': [],
                'blocking_state': 'planning_coverage_incomplete', 'technical_status': 'UNKNOWN',
                'domain_findings': error.findings, 'resume_action': 'refine_drawing_plan',
                'planning_input': 'imported' if action == 'stage' or params.get('plan_json') else 'existing',
                'message': '图单需修订。' + ''.join(row['message'] for row in error.findings),
                'model_calls': 0, 'model_execution_enabled': enabled,
            })]
        except PlannerResultUnknown as error:
            messages = [self.create_json_message({
                'workspace_id': wid, 'route': 'reply', 'done': 'yes', 'queue': [],
                'blocking_state': 'planning_result_unknown', 'technical_status': 'UNKNOWN',
                'supplier_state': 'result_unknown', 'pending_action': 'inspect_original',
                'resume_action': 'inspect_original_result',
                'message': error.public_message + ' 操作标识 ' + error.operation_id + '。',
                'operation_id': error.operation_id, 'request_id': error.request_id,
                'model_calls': error.model_calls, 'model_execution_enabled': enabled,
            })]
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

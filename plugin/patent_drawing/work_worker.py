"""Execute one durable phase at a time using the existing provider ledger."""
import argparse
import hashlib
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .claims_relations import RELATION_CONTRACT
from .errors import DrawingError, PlannerResultUnknown, SourceCoverageIncomplete
from .operation_store import OperationStore, WorkspaceBusy, canonical, digest
from .operator_policy import bind_work_operation, bound_work_task, load_policy, read_audited_work_response
from .work_queue import WorkActor, WorkConfig, WorkQueue, private_file, source_hold_status, work_scope
from .workspace import Workspace


log = logging.getLogger(__name__)


class WorkWorker:
    def __init__(self, queue, workspace_factory):
        self.queue = queue
        self.store = queue.store
        self.workspace_factory = workspace_factory

    def run_one(self):
        for task_id, job in self.queue.candidates():
            actor = WorkActor(job['tenant'], job['user'], job['conversation'])
            try:
                with self.store.owner_lock(actor.owner):
                    claimed = self.queue.claim(task_id)
                    if not claimed:
                        continue
                    token, snapshot = claimed
                    checkpoint = self._checkpoint(task_id, token, snapshot)
                    workspace = self.workspace_factory(actor.owner)
                    workspace.allow_models = workspace.allow_models and job['allow_models']
                    context = self._audit_context(actor, job)
                    with bound_work_task(*context):
                        self._run_phase(actor, workspace, job, snapshot, checkpoint)
            except WorkspaceBusy:
                continue
            return True
        return False

    def _checkpoint(self, task_id, token, snapshot):
        def update(status='running', **details):
            snapshot.update(details)
            bind_work_operation(details.get('operation_id'))
            self.queue.checkpoint(task_id, token, snapshot, status=status)
        return update

    def _run_phase(self, actor, workspace, job, snapshot, checkpoint):
        wid = job['workspace_id']
        try:
            intake = self.store.load(actor.owner, 'intake:active')
            if (intake['workspace_id'] != job['intake_workspace_id'] or intake['revision'] != job['intake_revision']
                    or intake['confirmed_revision'] != intake['revision']):
                checkpoint('waiting_input', blocking_state='intake_revision_changed',
                           resume_action='confirm_intake', message='来源或采集修订已变，当前任务保留原进度。')
                return
            state = workspace.read(wid)
            current = state.get('current') or {}
            current_matches = digest(current.get('plan')) == job['current_plan_sha256']
            if (state['source_sha256'] != job['source_sha256']
                    or (not current_matches and not (
                        job['kind'] == 'draw' and current.get('delivery_snapshot')
                        and digest(current['plan']) == job['work_plan_sha256']))):
                checkpoint('waiting_input', blocking_state='work_revision_changed',
                           resume_action='review_new_revision', message='当前图单修订已变，请重新核对。')
                return
            if self.queue.config.source_is_held(state):
                workspace.allow_models = False
                if job['kind'] in {'plan', 'image_route', 'agent_prepare'}:
                    hold = source_hold_status(wid, job['kind'], snapshot['stage'])
                    checkpoint('waiting_input', **{key: hold[key] for key in ('blocking_state', 'resume_action', 'message')})
                    return
            if not self._policy_matches(job):
                workspace.allow_models = False
            grant = self.queue.config.comparison_scope
            if job['kind'] == 'draw' and grant and (
                    job['branch'] == 'direct' or wid == grant['workspace_id']):
                try:
                    bound_wid = job['intake_workspace_id']
                    binding = self.store.load(actor.owner, 'comparison:' + bound_wid)
                    original = state if wid == bound_wid else workspace.read(bound_wid)
                    self.queue.config.check_comparison_scope(
                        actor, bound_wid, original, intake['revision'],
                        saved_proposal=binding['proposal_sha256'], run_id=(job.get('policy') or {}).get('run_id', ''))
                except ValueError:
                    checkpoint('waiting_input', blocking_state='comparison_authorization_changed',
                               resume_action='inspect_original_result',
                               message='同版对照授权已失效或变化，已保存原任务及请求记录。')
                    return
            self._restore_audit_response(actor, job, snapshot)
            if job.get('material_profile'):
                from .material_work import advance_material

                advance_material(workspace, job, state, snapshot, checkpoint)
            elif job['kind'] in {'plan', 'image_route'}:
                self._plan(actor, workspace, job, state, snapshot, checkpoint)
            elif job['kind'] == 'agent_prepare':
                self._prepare(workspace, job, state, checkpoint)
            elif job['kind'] == 'draw':
                self._draw(workspace, job, state, snapshot, checkpoint)
            else:
                self._edit(workspace, job, state, checkpoint)
        except PlannerResultUnknown as error:
            checkpoint('unknown', operation_id=error.operation_id, blocking_state='planning_result_unknown',
                       resume_action='inspect_original_result', message=error.public_message)
        except SourceCoverageIncomplete as error:
            checkpoint('waiting_input', blocking_state='planning_coverage_incomplete',
                       resume_action='refine_drawing_plan',
                       domain_findings=error.findings,
                       message='候选图单需核对来源覆盖。' + ''.join(item['message'] for item in error.findings))
        except (ValueError, OSError) as error:
            self._failure(workspace, job, snapshot, checkpoint, error)

    @staticmethod
    def _policy_matches(job):
        if not job.get('policy'):
            return True
        path = os.environ.get('PATENT_DRAWING_OPERATOR_POLICY')
        return path and load_policy(path).sha256 == job['policy']['sha256']

    def _audit_context(self, actor, job):
        return self.queue.config.app_id, self.queue.config.build, job['task_id'], self.store._owner(actor.owner)

    def _restore_audit_response(self, actor, job, snapshot):
        operation = snapshot.get('operation_id')
        if not operation or not job.get('policy'):
            return
        if self.store.operation_status(actor.owner, operation, check_checkpoint=False)['state'] != 'dispatching':
            return
        stage = snapshot['stage']
        materials = {'material_catalog','material_rank','material_planning'}
        if stage not in {'figure_decision', 'planning', 'image_route', 'agent_prepare', 'render'} | materials:
            return
        group = stage in {'figure_decision', 'planning', 'render'} | materials
        prefix = operation + ':'
        try:
            evidence = self.store.load(actor.owner, prefix + 'evidence') if group else {
                'request': self.store.load(actor.owner, prefix + 'request')}
            expected = self.store.get_artifact(actor.owner, evidence['request'])
        except (KeyError, ValueError):
            return
        path = os.environ.get('PATENT_DRAWING_OPERATOR_POLICY')
        if not path:
            return
        root = 'https://api.openai.com/v1/'
        endpoints = {'figure_decision': ('decisions',), 'image_route': ('decisions',),
                     'material_catalog':('decisions',), 'material_rank':('decisions',),
                     'material_planning':('responses',),
                     'planning': ('responses',), 'agent_prepare': ('responses',),
                     'render': ('images/edits', 'images/generations')}
        allowed = {root + endpoint: 'image' if stage == 'render' else 'text' for endpoint in endpoints[stage]}
        request_matches = ((lambda raw: json.loads(raw) == json.loads(expected))
                           if stage in {'planning', 'agent_prepare','material_planning'} else lambda raw: raw == expected)
        result = read_audited_work_response(load_policy(path), job['policy'],
                                            self._audit_context(actor, job), operation, request_matches, allowed)
        if not result:
            return
        kind = 'headers' if stage == 'render' else 'receipt'
        receipt = {key: result[key] for key in ('status', 'headers', 'body_sha256')}
        if kind == 'receipt':
            receipt = {'http_status': result['status'], 'request_id': result['headers'].get('x-request-id'),
                       'body_sha256': result['body_sha256']}
        captured = {'response': self.store.put_artifact(actor.owner, result['body']),
                    kind: self.store.put_artifact(actor.owner, canonical(receipt).encode())}
        if group:
            self.store.save(actor.owner, prefix + 'evidence', {**evidence, **captured})
        else:
            for name, value in captured.items():
                self.store.save(actor.owner, prefix + name, value)

    def _failure(self, workspace, job, snapshot, checkpoint, error):
        wid = job['workspace_id']
        state = workspace.read(wid)
        stage = snapshot['stage']
        pending = {'planning': state.get('pending_plan'),
                   'figure_decision': state.get('pending_figure_decision')}.get(stage) or {}
        operation = pending.get('operation_id') or snapshot.get('operation_id')
        status = self.store.operation_status(workspace.owner, operation)['state'] if operation else ''
        if (stage == 'planning' and job.get('claims_contract', 2) >= 17 and not snapshot.get('claims_recovery')
                and isinstance(error, DrawingError) and error.category == 'planner_output_truncated'
                and job['allow_models'] and job['budgets']['text'] >= snapshot.get('claims_decision_calls', 0) + 2):
            primary_cap = job.get('claims_output_cap') if job['claims_contract'] == RELATION_CONTRACT else None
            if recovery := workspace.truncated_plan_record(operation, primary_cap=primary_cap):
                checkpoint('queued', claims_recovery=recovery, operation_id=None,
                           message='首次图单已确认截断，原任务排队续做一次。')
                return
        if 'UNKNOWN' in str(error) or status in {'dispatching', 'result_unknown'} or (
                isinstance(error, DrawingError) and error.category == 'result_unknown'):
            checkpoint('unknown', operation_id=operation, blocking_state='result_unknown',
                       resume_action='inspect_original_result', message='原派发结果待核，请按原操作标识检查回执。')
        elif isinstance(error, DrawingError) and error.category == 'precise_edit_rejected':
            gate = workspace.execution_state(wid)
            if gate.get('blocking_state') == error.category:
                self._block(workspace, state, gate, snapshot, checkpoint)
            else:
                checkpoint('partial' if snapshot['completed'] else 'failed', operation_id=operation,
                           blocking_state=error.category, message=error.public_message,
                           resume_action='review_saved_result')
        elif isinstance(error, DrawingError):
            message = error.public_message
            if snapshot.get('claims_recovery') and error.category == 'planner_output_truncated':
                message = '续做仍达到输出上限，已保留两次原始响应。'
            checkpoint('partial' if snapshot['completed'] else 'failed', operation_id=operation,
                       blocking_state=error.category, message=message,
                       resume_action=('inspect_original_result'
                                      if status in {'provider_rejected','response_saved_unusable'} else 'review_saved_result'),
                       **({'original_request_id': error.request_id}
                          if error.category in {'planner_output_truncated', 'planner_invalid_output',
                                                'planner_incomplete'} and error.request_id else {}))
        elif stage == 'package' and '本版文件超过回取容量' in str(error):
            checkpoint('partial', blocking_state='delivery_capacity_exceeded',
                       resume_action='confirm_smaller_scope', message=str(error))
        else:
            log.warning('phase failed task=%s phase=%s error=%s', job['task_id'], stage, type(error).__name__)
            checkpoint('partial' if snapshot['completed'] else 'failed',
                       operation_id=operation, blocking_state='work_phase_failed',
                       message='本阶段未提交，请核对已保存进度及执行记录。', resume_action='review_saved_result')

    def _plan(self, actor, w, job, state, snapshot, checkpoint):
        from .figure_routing import FAMILIES, semantic_summary

        wid, query, stage = job['workspace_id'], job['query'], snapshot['stage']
        claims_only = 'claims_body' in state['bundle']
        claims_options = {'claims_contract': job.get('claims_contract', 2)} if claims_only else {}
        if stage == 'figure_decision':
            decision_options = {}
            paired = claims_options.get('claims_contract', 2) >= 15
            plan_reserve = 2 if claims_options.get('claims_contract', 2) >= 17 else 1
            w.allow_models = w.allow_models and job['budgets']['text'] >= (2 + plan_reserve if paired else 1)
            if paired:
                decision_options = {
                    'decision_limit': min(3, job['budgets']['text'] - plan_reserve),
                    'question_revision': job.get('luna_question_revision', 1),
                    'recover_audit': lambda op: self._restore_audit_response(
                        actor, job, {**snapshot, 'operation_id': op}),
                }
            result = w.route_figures(wid, query, before_dispatch=lambda op: checkpoint(operation_id=op),
                                     **claims_options, **decision_options)
            operation = result.get('operation_id')
            routing = None
            if operation:
                routing = {'operation_id': operation, 'instructions': {
                    family: FAMILIES[family]['instruction'] for family in result['selected']}}
            if result['state'] == 'result_unknown':
                checkpoint('unknown', operation_id=operation, blocking_state='figure_decision_result_unknown',
                           resume_action='inspect_original_result', message='图型判断原派发结果待核。')
            elif not operation and not w.allow_models:
                checkpoint('waiting_input', resume_action='await_operator_policy', blocking_state='model_execution_disabled',
                           message='已确认材料保留，当前策略关闭模型派发。')
            else:
                decisions = result.get('semantic_choices') or {}
                semantics = {'claims_semantic_summary': semantic_summary(decisions)} if decisions else {}
                if vote := result.get('vote'):
                    semantics.update(claims_decision_calls=vote['attempts'],
                                     claims_vote_review={'disagreements': len(vote['disagreements']),
                                                         'rejudged': len(vote['rejudged']),
                                                         'uncertain': len(vote['uncertain'])})
                    if watch := result.get('confidence_watch'):
                        semantics['claims_vote_review']['low_confidence'] = len(watch['same_low'])
                checkpoint('queued', stage='planning', routing=routing, operation_id=operation,
                           message='图型判断已保存，等待图单规划。', **semantics)
            return
        if stage == 'planning':
            if job.get('claims_contract') == RELATION_CONTRACT and 'claims_output_cap' in job:
                claims_options['claims_output_cap'] = job['claims_output_cap']
            pending = w._pending_plan(state)
            operation = snapshot.get('operation_id')
            recorded = self.store.operation_status(w.owner, operation) if operation and operation.startswith('plan_') else {}
            figure_calls = snapshot.get('claims_decision_calls', int(bool(snapshot.get('routing'))))
            recovery = snapshot.get('claims_recovery')
            if recovery:
                claims_options['claims_recovery'] = recovery
            can_dispatch = w.allow_models and job['budgets']['text'] >= figure_calls + (2 if recovery else 1)
            w.allow_models = can_dispatch
            def save_operation(operation_id):
                details = {'message': '正在原任务续做一次。'} if recovery else {}
                checkpoint(operation_id=operation_id, **details)
            if pending:
                if pending['query'] != query:
                    raise ValueError('原规划操作与本次确认指令不一致。')
                planned = w.resume_planning(wid, before_dispatch=save_operation)
            elif recorded and (recorded['response_saved'] or claims_only and recorded['state'] != 'not_started'):
                planned = w.plan(wid, query, figure_routing=snapshot.get('routing'),
                                 saved_operation=operation, **claims_options)
            elif can_dispatch:
                planned = w.plan(wid, query, figure_routing=snapshot.get('routing'),
                                 before_dispatch=save_operation, **claims_options)
            else:
                checkpoint('waiting_input', blocking_state='model_execution_disabled', resume_action='await_operator_policy',
                           message='当前策略关闭新规划请求，可核对已保存的原响应。')
                return
            if plan := planned.get('claims_plan'):
                artifact = self.store.put_artifact(actor.owner, canonical(plan).encode())
                views = [{'id': view['id'], 'title': view['title'], 'purpose': view['purpose'],
                          'delivered_type': view['type'], 'engine': 'claims_only'} for view in plan['views']]
                labels = {'plan_ready': '图单就绪', 'partial': '部分可规划',
                          'no_drawing_recommended': '建议无需附图'}
                groups = {'required': '必需关系缺证', 'geometry': '画法限制',
                          'optional': '可选细节', 'review': '待独立复核'}
                review = _claims_relation_summary(plan)
                sections = []
                for impact, heading in ({} if 'review_groups' in review else groups).items():
                    rows = [item['detail'] + ' ' + item['question'] for item in plan['limitations']
                            if 'candidate_id' not in item and 'flow_candidate_id' not in item
                            if item['impact'] == impact]
                    if rows:
                        sections.extend([heading, *rows])
                gaps = sum(row['disposition'] in {'partial', 'unverified'} for row in plan['coverage'])
                if coverage := plan.get('relationship_review'):
                    covered = sum(row['state'] == 'covered' for row in coverage['rows'])
                    pending_count = sum(row['state'] == 'pending' for row in coverage['rows'])
                    sections.append(f'关系表达已核 {covered} 项、待核 {pending_count} 项。')
                if type(tokens := planned.get('output_tokens')) is int:
                    from .text_client import CLAIMS_WIRE_TARGET_TOKENS, CLAIMS_WIRE_OUTPUT_TOKENS
                    note = '，超过验收目标' if tokens > CLAIMS_WIRE_TARGET_TOKENS else ''
                    sections.append(f'规划输出 {tokens:,} token，上限 {CLAIMS_WIRE_OUTPUT_TOKENS:,}{note}。')
                message = '\n'.join([labels[plan['status']], _candidate_message(views),
                                     f'权项覆盖 {len(plan["coverage"])} 项，待核映射 {gaps} 项。',
                                     *review['message'], *sections])
                review_indices = {'claims_review_groups': [group['limitation_indices']
                                  for group in review['review_groups'][:8]]} if 'review_groups' in review else {}
                if 'guard_pending' in review:
                    review_indices['claims_guard_pending'] = review['guard_pending']
                view_limits = {}
                if problems := plan.get('view_limit_issues'):
                    view_limits = {'completed':[view['id'] for view in views],
                                   'claims_view_limit_issues':[
                                       {key:issue[key] for key in ('view_id','logical_count','limit')}
                                       for issue in problems]}
                checkpoint('completed', stage='claims_plan', files={'claims-plan.json': artifact},
                           operation_id=planned['operation_id'], candidate_views=views,
                           plan_sha256=digest(plan), message=message, resume_action='inspect_work',
                           claims_plan_status=plan['status'], relation_counts=review['counts'],
                           claims_flow_count=len(plan.get('flow_edges', [])), **review_indices, **view_limits)
                return
            if not planned.get('plan'):
                checkpoint('waiting_input', blocking_state=planned.get('blocking_state'),
                           resume_action=planned.get('resume_action', 'confirm_no_drawings'),
                           **{key: planned[key] for key in ('domain_findings', 'planning_command', 'planning_input')
                              if key in planned},
                           message=planned.get('message', '请检查本次图单规划。'))
                return
            plan = planned['plan']
            views = [{key: view[key] for key in ('id', 'title', 'purpose', 'delivered_type', 'engine')}
                     for view in plan['views']]
            checkpoint('queued', stage='image_route', plan_sha256=digest(plan),
                       **{key: planned[key] for key in ('domain_findings',) if key in planned},
                       candidate_views=views, message=planned['message'])
            return
        if stage != 'image_route' or digest(state.get('proposal')) != snapshot.get('plan_sha256'):
            checkpoint('waiting_input', blocking_state='work_revision_changed', resume_action='review_new_revision',
                       message='候选图单已经变化，请核对最新修订。')
            return
        allow_route = job['budgets']['text'] >= (3 if job['kind'] == 'plan' else 1)
        routing = (w.route_images(wid, '继续准备复杂图', required_budget=job['budgets']['image'],
                                  before_dispatch=lambda operation: checkpoint(operation_id=operation))
                   if allow_route else {'view_routing': {}, 'message': ''})
        scope, operation = routing.get('scope'), routing.get('operation_id')
        message = '\n'.join(part for part in (snapshot['message'], _candidate_message(snapshot['candidate_views']),
                                               routing['message']) if part)
        next_action = 'confirm_plan'
        if routing.get('route') == 'agent':
            self.queue.authorize_agent(actor, wid, scope, job['intake_revision'], job['task_id'])
            next_action = 'prepare_complex_images'
            message += '\n请核对图单，再发送“继续准备复杂图”。'
        else:
            message += '\n请核对图单，再发送“确认图单并生成”。'
        status = 'unknown' if routing.get('state') == 'result_unknown' else 'completed'
        checkpoint(status, stage='proposed', scope=scope, operation_id=operation,
                   image_route_state=routing.get('state'), view_routing=routing.get('view_routing', {}),
                   resume_action='inspect_original_result' if status == 'unknown' else next_action,
                   message=message)

    def _prepare(self, w, job, state, checkpoint):
        wid, scope = job['workspace_id'], job['image_scope']
        if digest(state.get('proposal')) != job['proposal_sha256']:
            checkpoint('waiting_input', blocking_state='preparation_scope_changed', resume_action='review_new_revision',
                       message='复杂图候选来源或范围已更新，请核对当前图单。')
            return
        operation = 'agent_prepare_' + scope
        checkpoint(operation_id=operation, message='正在准备已核复杂图。')
        result = w.prepare_complex_images(wid, scope, durable=True,
                                          permit_dispatch=job['budgets']['text'] > 0)
        artifact = self.store.put_artifact(w.owner, canonical(result).encode())
        listing = '、'.join(f'{fid}（{focus}）' for fid, focus in result['views'].items())
        checkpoint('completed', preparation_artifact=artifact, preparation_status='prepared',
                   resume_action='confirm_plan', message='复杂图准备已保存。' + listing + '。请核对后确认图单。')

    def _draw(self, w, job, state, snapshot, checkpoint):
        wid = job['workspace_id']
        current = state.get('current') or {}
        if digest(current.get('plan')) == job['work_plan_sha256'] and current.get('delivery_snapshot'):
            self._complete_draw(current, snapshot, checkpoint)
            return
        if work_scope(wid, state) != job['work_identity']:
            checkpoint('waiting_input', blocking_state='work_revision_changed',
                       resume_action='review_new_revision', message='执行范围已变化，请重新核对图单。')
            return
        work = state['work']
        if work['required_image_calls'] > job['budgets']['image']:
            w.allow_models = False
        gate = w.preflight(wid)
        self._save_figures(w, state, checkpoint)
        if gate.get('blocking_state'):
            self._block(w, state, gate, snapshot, checkpoint)
            return
        pending = w.resume(wid)['queue']
        if pending:
            # The state remains eligible for reclaim after this phase. The
            # provider slot and any rendered figure are independently durable.
            fid = pending[0]
            spec = work['specs'][fid]
            blocked = w._blocked_attempt(state, fid)
            recovery = w._recovery_for(state, fid, blocked)
            operation = 'render_' + digest([wid, fid, spec] + ([recovery['authorization_id']] if recovery else []))
            operations = {**snapshot.get('operations', {}), fid: operation}
            checkpoint(stage='render', operation_id=operation, operations=operations,
                       message='正在处理图件 ' + fid + '。')
            w.render(wid, fid)
            self._save_figures(w, w.read(wid), checkpoint)
            checkpoint('queued', stage='render' if w.resume(wid)['queue'] else 'package',
                       message='本阶段图件已保存，下一阶段可继续。')
            return
        checkpoint(stage='package', message='正在提交本修订的 PDF、ZIP 和文件摘要。')
        w.package(wid)
        self._complete_draw(w.read(wid)['current'], snapshot, checkpoint)

    def _save_figures(self, w, state, checkpoint):
        wid = state['workspace_id']
        figures = w.partial(wid)['figures']
        files = {key + '.png': item['record']['png'] for key, item in figures.items()}
        checkpoint(completed=list(figures), files=files)

    def _block(self, w, state, gate, snapshot, checkpoint):
        reason = gate['blocking_state']
        unknown = reason in {'result_unknown', 'dispatching'}
        partial = bool(snapshot['completed'])
        label = 'unknown' if unknown else 'partial' if partial else 'waiting_input'
        files = dict(snapshot['files'])
        if partial and not unknown and reason not in {'local_preflight_rejected', 'source_lineage_mismatch'}:
            packed = w.package_partial(state['workspace_id'])
            if all(isinstance(packed.get(name), bytes) for name in ('pdf', 'zip')):
                files.update({'incomplete-drawings.' + ext: self.store.put_artifact(w.owner, packed[ext])
                              for ext in ('pdf', 'zip')})
        missing = gate.get('missing_ids')
        if missing is None:
            blocked_view = gate.get('blocked_view')
            missing = [blocked_view] if blocked_view else []
        checkpoint(label, files=files, missing_ids=missing,
                   **{key: gate.get(key) for key in ('blocking_state', 'blocked_view', 'resume_action',
                                                    'technical_status', 'message')},
                   **{key: gate[key] for key in ('domain_findings', 'view_mark_gaps') if key in gate})

    @staticmethod
    def _complete_draw(current, snapshot, checkpoint):
        delivery = current['delivery_snapshot']
        files = {key + '.png': record['png'] for key, record in current['records'].items()}
        files.update({'drawings.' + name: ref for name, ref in delivery['files'].items()
                      if name in {'pdf', 'zip', 'manifest'}})
        checkpoint('completed', stage='package', files=files, completed=list(current['records']),
                   export_revision=current['plan']['revision'], blocking_state=None, blocked_view=None,
                   missing_ids=[], technical_status=current['quality']['technical_status'],
                   resume_action='download_same_revision', message='本版图件与成套文件已保存。')

    def _edit(self, w, job, state, checkpoint):
        wid, kind = job['workspace_id'], job['kind']
        pending = state.get(kind.removesuffix('_edit') + '_label_pending')
        archived = state.get('engineering_label_revision') if kind == 'engineering_edit' else state.get('structural_reference')
        if pending and digest(pending) != job['edit_sha256']:
            checkpoint('waiting_input', blocking_state='work_revision_changed', resume_action='review_new_revision',
                       message='关联修改范围已变化，请核对本版确认。')
            return
        if kind == 'engineering_edit':
            packed = w.confirm_engineering_label_edit(wid)
        else:
            if pending:
                w.confirm_structural_label_edit(wid)
            elif not archived:
                raise ValueError('结构改号回执未找到。')
            packed = w.package_structural_reference(wid)
        files = {('engineering-reference.' if kind == 'engineering_edit' else 'structural-reference.') + ext:
                 self.store.put_artifact(w.owner, packed[ext]) for ext in ('pdf', 'zip') if isinstance(packed.get(ext), bytes)}
        checkpoint('completed' if files else 'waiting_input', stage='package', files=files,
                   export_revision=packed.get('reference_revision'), resume_action='download_same_revision',
                   message='已保存本次关联修改图件。' if files else packed['message'])


def _claims_relation_summary(plan):
    if 'relation_candidates' not in plan:
        return {'message': [], 'counts': {}}
    counts = dict.fromkeys(('accepted', 'rejected', 'equivalent'), 0)
    for item in plan['relation_candidates']:
        counts[item['status']] += 1
    lines = [f"关系引证通过 {counts['accepted']} 条，拒绝 {counts['rejected']} 条，"
             f"等价合并 {counts['equivalent']} 条。"]
    if 'flow_edges' in plan:
        lines.append(f"已核流程边 {len(plan['flow_edges'])} 条，对象实例 {len(plan['instances'])} 个。")
        instances = {row['id']: row['name'] for row in plan['instances']}
        bindings = [f"{instances[row['owner_id']]} → {row['name']}"
                    for row in plan['instances'] if row.get('owner_id') in instances]
        if bindings:
            lines.append('对象归属 ' + '、'.join(bindings[:8]) + '。')
    schema = plan.get('schema_version')
    compact = schema in {'claims-drawing-plan-10', 'claims-drawing-plan-11', 'claims-drawing-plan-12',
                         'claims-drawing-plan-13', 'claims-drawing-plan-14', 'claims-drawing-plan-15',
                         'claims-drawing-plan-16', 'claims-drawing-plan-17'}
    unified = compact or schema in {'claims-drawing-plan-7', 'claims-drawing-plan-8', 'claims-drawing-plan-9'}
    if compact or schema == 'claims-drawing-plan-9':
        derived = [row for row in plan.get('source_derivations', []) if row['status']=='accepted']
        rebound = [row for row in plan['relation_candidates'] if row.get('evidence_rebindings') and row['status']=='accepted']
        locations = '、'.join(dict.fromkeys(f"权{row['claim']}／{row['view_id']}" for row in derived))
        if derived:
            lines.append(f'规则已按原文补齐 {len(derived)} 条显式关系（{locations}），逐条依据见图单 source_derivations。')
        if rebound:
            lines.append(f'同权项角色引证已校正 {len(rebound)} 条，原锚点与补绑依据随候选保存。')
        if compact:
            corrected = sum(row['status'] in {'accepted', 'equivalent'} and
                            any(key in row for key in ('action_citation_rebinding', 'condition_rebinding',
                                                       'scope_recovery'))
                            for row in plan['relation_candidates'])
            if corrected:
                lines.append(f'原文引证或守卫已校正 {corrected} 条，依据见同版图单。')
    previous_summary = plan.get('schema_version') == 'claims-drawing-plan-5'
    if unified or previous_summary:
        checked = list(dict.fromkeys(view_id for r in plan['relations'] for view_id in r['view_ids']))
        if checked:
            lines.append('有通过引证的图位 ' + '、'.join('图 ' + value.removeprefix('fig') for value in checked) + '。')
    if unified:
        pending = []
        if schema in {'claims-drawing-plan-13', 'claims-drawing-plan-14', 'claims-drawing-plan-15',
                      'claims-drawing-plan-16', 'claims-drawing-plan-17'}:
            guards = [r for r in plan['relation_candidates'] if r.get('wire_guard')]
            pending = [r for r in guards if r['wire_guard']['status'] == 'unresolved']
            if guards:
                lines.append(f'守卫引证按原文恢复 {len(guards) - len(pending)} 条，局部缺证 {len(pending)} 条。')
            for row in pending[:3]:
                figures = '／'.join(row['view_ids']) or '图位待核'
                lines.append(f"权{row['claim']}／{figures} 候选 {row['source_index'] + 1} 守卫引证待核。")
        report = _claims_review_summary(plan, lines, counts)
        if pending:
            report['guard_pending'] = len(pending)
        return report
    issues = [item for item in plan['limitations'] if 'candidate_id' in item or 'flow_candidate_id' in item
              or previous_summary and item['impact'] in {'required', 'review'}]
    issues.sort(key=lambda item: (item['impact'] != 'required', item['impact'] == 'optional'))
    for item in issues[:8]:
        scope = ['权利要求 ' + str(n) for n in item['claims']]
        scope.extend('图 ' + ident.removeprefix('fig') for ident in item['view_ids'])
        necessity = {True: '模型列为必需', False: '模型列为可选', None: '必需性待核'}[item.get('model_required')]
        where = '／'.join(scope) or '范围待确认'
        lines.append(f"{where}（{necessity}）{item['detail']} {item['question']}")
    if len(issues) > 8:
        lines.append(f"其余 {len(issues) - 8} 条可在图单的逐条记录中核对。")
    return {'message': lines, 'counts': counts}


def _claims_review_summary(plan, lines, counts):
    relations = {item['candidate_id']: item for item in plan['relation_candidates']}
    edges = {item['id']: item for item in plan.get('flow_candidates', [])}
    priorities = {'required': 0, 'review': 1, 'geometry': 2, 'optional': 3}
    grouped = {}
    candidate_groups = {}
    generic = []
    compact = plan.get('schema_version') in {
        'claims-drawing-plan-10', 'claims-drawing-plan-11', 'claims-drawing-plan-12', 'claims-drawing-plan-13',
        'claims-drawing-plan-14', 'claims-drawing-plan-15', 'claims-drawing-plan-16', 'claims-drawing-plan-17',
    }

    def add(key, index, item, scope):
        group = grouped.setdefault(key, {'issue': item, 'count': 0, 'references': [],
                                         'relation_scope': scope, 'limitation_indices': []})
        group['count'] += 1
        group['limitation_indices'].append(index)
        ref = {name: item[name] for name in ('candidate_id', 'flow_candidate_id', 'link_index') if name in item}
        if ref and ref not in group['references']:
            group['references'].append(ref)

    for index, item in enumerate(plan['limitations']):
        edge = edges.get(item.get('flow_candidate_id'), {})
        relation = relations.get(item.get('candidate_id') or edge.get('relation_candidate_id'), {})
        reasons = tuple(sorted((r['code'], r['field']) for r in relation.get('reasons', ())))
        raw = relation.get('raw')
        relation_scope = ({name: raw.get(name) for name in ('claim_path', 'subject', 'object', 'direction')}
                          if isinstance(raw, dict) else {})
        display = (item['impact'], tuple(sorted(item['claims'])), tuple(sorted(item['view_ids'])),
                   tuple(sorted(item.get('figure_roles', {}).items())),
                   item['detail'].strip(), item['question'].strip())
        key = (display, item.get('code', ''), item.get('link_anchor', ''), canonical(relation_scope),
               reasons, tuple(edge.get('reasons', ())), item.get('model_required'))
        if compact and isinstance(raw, dict):
            key += (canonical([raw.get('condition'), raw.get('evidence')]),)
            if guard := relation.get('wire_guard'):
                key += (canonical(guard['raw']),)
        if 'candidate_id' in item or 'flow_candidate_id' in item:
            candidate_groups.setdefault(display, set()).add(key)
        elif 'link_index' not in item and not item.get('code'):
            generic.append((display, key, index, item))
            continue
        add(key, index, item, relation_scope)
    for display, key, index, item in generic:
        candidates = candidate_groups.get(display, ())
        add(next(iter(candidates)) if len(candidates) == 1 else key, index, item, {})
    for group in grouped.values():
        group['limitation_indices'].sort()
    groups = sorted(grouped.values(), key=lambda group: (priorities[group['issue']['impact']],
                    group['limitation_indices'][0]))
    headings = {'required': '必需关系缺证', 'review': '待核', 'geometry': '画法限制', 'optional': '可选细节'}
    if compact and groups:
        lines.append(f'{len(groups)} 组待核项，完整问题及来源见同版图单。')
    for index, group in enumerate(groups[:8], 1):
        item = group['issue']
        where = [f'权{n}' for n in item['claims']]
        roles = item.get('figure_roles', {})
        where.extend(f'{caption} {roles[role]}' for role, caption in (('parent', '总图'), ('child', '局部图'))
                     if role in roles)
        where.extend(view for view in item['view_ids'] if view not in roles.values())
        scope = group['relation_scope']
        if all(isinstance(scope.get(key), str) and scope[key] for key in ('subject', 'object')):
            connector = '与' if scope.get('direction') == 'symmetric' else '→'
            where.append('候选 ' + scope['subject'] + connector + scope['object'])
        if item.get('link_anchor'):
            where.append('锚点 ' + item['link_anchor'][:80])
        scope = '／'.join(where)
        records = f"（{group['count']} 条原始记录）" if group['count'] > 1 else ''
        question = '' if compact else ' ' + item['question'].strip()
        heading = f"待核 {index} · {headings[item['impact']]}" if compact else headings[item['impact']]
        lines.append(f"{heading} {scope} {item['detail'].strip()}{records}{question}")
    if not compact and len(groups) > 8:
        lines.append(f'其余 {len(groups) - 8} 组待核项可在图单逐条核对。')
    return {'message': lines, 'counts': counts, 'review_groups': groups}


def _candidate_message(views):
    return '\n'.join(f"{view['id']} {view['title']}（{view['delivered_type']}）{view['purpose']}" for view in views)


def build_workspace(store, owner):
    from .client import OpenAIImagesClient
    from .figure_routing import DecisionClient
    from .operator_policy import model_execution_enabled
    from .text_client import OpenAITextClient

    if not model_execution_enabled():
        return Workspace(store, owner)
    key = private_file(os.environ.get('PATENT_DRAWING_WORKER_API_KEY_FILE'), 4096, 0o077).decode().strip()
    return Workspace(store, owner, allow_models=True, image_client=OpenAIImagesClient(key),
                     text_client=OpenAITextClient(key), decision_client=DecisionClient(key))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--concurrency', type=int, choices=(1, 2), default=1)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    config = WorkConfig.load()
    worker_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if config.hashes['worker'] != worker_sha:
        raise SystemExit('installed work process entrypoint differs from authorized build')
    root = os.environ.get('PATENT_DRAWING_STATE_ROOT')
    if not root:
        raise SystemExit('missing shared task volume')
    store = OperationStore(root)
    worker = WorkWorker(WorkQueue(store, config), lambda owner: build_workspace(store, owner))
    if args.once:
        print(json.dumps({'app_id': config.app_id, 'build_hashes': config.hashes,
                          'processed': worker.run_one()}), flush=True)
        return

    def consume():
        while True:
            if not worker.run_one():
                time.sleep(0.3)

    if args.concurrency == 1:
        consume()
    else:
        with ThreadPoolExecutor(max_workers=2) as executor:
            for task in [executor.submit(consume) for _ in range(2)]:
                task.result()


if __name__ == '__main__':
    main()

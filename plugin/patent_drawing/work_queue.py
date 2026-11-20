"""Admission and readback for durable, application-bound workspace work."""
import json
import os
import re
import stat
import time
import uuid
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

from .operation_store import canonical, digest


SHAS = ('plugin', 'dsl', 'agent', 'worker')
TASK_ID = re.compile(r'work_[a-f0-9]{64}')
IDENTIFIER = re.compile(r'[A-Za-z0-9_.-]{1,100}')
EDIT = re.compile(r'^(?:修改|改号|改名|整套将|整套把|全套将|全套把|全部附图)')
LABEL_EDIT = re.compile(r'[0-9]+[a-zA-Z]?\s*(?:改为|改成|→|->|调整为)\s*[0-9]+[a-zA-Z]?')
DOWNLOAD_TASK = re.compile(r'导出任务\s+(work_[a-f0-9]{64})')
INSPECT_TASK = re.compile(r'查看任务\s+(work_[a-f0-9]{64})')
ACTIVE = {'queued', 'running'}


def private_file(path, maximum, forbidden):
    if not path or not Path(path).is_absolute():
        raise ValueError('工作配置或凭据文件未绑定。')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as file:
        metadata = os.fstat(file.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & forbidden
                or not 1 <= metadata.st_size <= maximum):
            raise ValueError('工作配置或凭据文件的权限、类型或容量无效。')
        value = file.read(maximum + 1)
        if len(value) != metadata.st_size:
            raise ValueError('读取时工作配置或凭据文件已变化。')
        return value


@dataclass(frozen=True)
class WorkActor:
    tenant: str
    user: str
    conversation: str

    def __post_init__(self):
        if (not re.fullmatch(r'[a-f0-9]{32}', self.tenant)
                or not all(isinstance(value, str) and IDENTIFIER.fullmatch(value)
                           for value in (self.user, self.conversation))):
            raise ValueError('任务需要服务端租户、用户和原会话。')

    @property
    def owner(self):
        return ':'.join((self.tenant, self.user, self.conversation))


@dataclass(frozen=True)
class WorkConfig:
    app_id: str
    hashes: dict
    direct_comparison: dict = field(default_factory=dict)
    source_holds: tuple = ()
    comparison_scope: dict = field(default_factory=dict)
    claims_output_caps: dict = field(default_factory=dict)
    material_profiles: dict = field(default_factory=dict)

    @classmethod
    def load(cls):
        from .build_identity import runtime_build_sha256

        if os.environ.get('PATENT_DRAWING_ISOLATED_WORKER') != '1':
            raise ValueError('持久任务需要指定的隔离应用。')
        data = json.loads(private_file(os.environ.get('PATENT_DRAWING_WORK_CONFIG'), 32768, 0o022))
        hashes = {name: data.get(name + '_sha256') for name in SHAS}
        grants = data.get('direct_comparison', {})
        holds = data.get('source_holds', [])
        comparison = data.get('comparison_scope', {})
        caps = data.get('claims_output_caps', {})
        profiles = data.get('material_profiles', {})
        from .text_client import CLAIMS_EXTENDED_OUTPUT_TOKENS

        if (not isinstance(data.get('app_id'), str) or not IDENTIFIER.fullmatch(data['app_id'])
                or any(not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value)
                       for value in hashes.values())
                or not isinstance(grants, dict)
                or any(not IDENTIFIER.fullmatch(user) or not isinstance(sessions, list)
                       or any(not isinstance(session, str) or not IDENTIFIER.fullmatch(session)
                              for session in sessions) for user, sessions in grants.items())
                or not isinstance(holds, list) or len(holds) > 100
                or any(not isinstance(sha, str) or not re.fullmatch(r'[a-f0-9]{64}', sha) for sha in holds)
                or not isinstance(comparison, dict)
                or not isinstance(caps, dict)
                or any(not re.fullmatch(r'[a-f0-9]{64}', key) or type(cap) is not int
                       or cap != CLAIMS_EXTENDED_OUTPUT_TOKENS for key, cap in caps.items())
                or not isinstance(profiles, dict)
                or any(not re.fullmatch('[a-f0-9]{64}', key) or not isinstance(profile, dict)
                       or set(profile) != {'luna', 'sol', 'output_cap'}
                       or type(profile['luna']) is not int or not 0 <= profile['luna'] <= 6
                       or type(profile['sol']) is not int or not 1 <= profile['sol'] <= 2
                       or profile['output_cap'] not in (20000, 25000)
                       for key, profile in profiles.items())
                or set(data) - {'app_id', 'direct_comparison', 'comparison_scope', 'source_holds', 'claims_output_caps',
                                'material_profiles',
                                *(name + '_sha256' for name in SHAS)}):
            raise ValueError('持久任务配置缺少隔离应用或制品指纹。')
        if comparison and (
                grants or set(comparison) != {'user', 'conversation', 'workspace_id', 'source_sha256',
                                               'proposal_sha256', 'intake_revision', 'run_id', 'expires_at'}
                or any(not isinstance(comparison[key], str) or not IDENTIFIER.fullmatch(comparison[key])
                       for key in ('user', 'conversation', 'run_id', 'workspace_id'))
                or any(not isinstance(comparison[key], str) or not re.fullmatch('[a-f0-9]{64}', comparison[key])
                       for key in ('source_sha256', 'proposal_sha256'))
                or type(comparison['intake_revision']) is not int or comparison['intake_revision'] < 1
                or type(comparison['expires_at']) is not int or comparison['expires_at'] < 1):
            raise ValueError('同版对照授权范围无效或旧授权未退场。')
        if hashes['plugin'] != runtime_build_sha256():
            raise ValueError('当前插件与持久任务配置的构建不一致。')
        return cls(data['app_id'], hashes, grants, tuple(holds), comparison, caps, profiles)

    @property
    def build(self):
        return digest(self.hashes)

    def check_session(self, session, credential_app, conversation):
        sdk_app = getattr(session, 'app_id', None)
        sdk_conversation = getattr(session, 'conversation_id', None)
        if (sdk_app != self.app_id or sdk_conversation != conversation
                or (credential_app and self.app_id != credential_app)):
            raise ValueError('当前工具调用与隔离应用或会话绑定不一致。')

    def may_compare(self, actor):
        if self.comparison_scope:
            grant = self.comparison_scope
            return (grant['user'] == actor.user and grant['conversation'] == actor.conversation
                    and time.time() < grant['expires_at'])
        return actor.conversation in self.direct_comparison.get(actor.user, ())

    def check_comparison_scope(self, actor, wid, state, intake_revision, *, saved_proposal=None, run_id=None):
        grant = self.comparison_scope
        if grant and (
                not self.may_compare(actor) or grant['workspace_id'] != wid
                or grant['source_sha256'] != state['source_sha256']
                or grant['proposal_sha256'] != (saved_proposal if saved_proposal else digest(state.get('proposal')))
                or grant['intake_revision'] != intake_revision
                or (run_id is not None and grant['run_id'] != run_id)):
            raise ValueError('同版对照授权已失效或与来源、会话、修订或场次不符。')

    def source_is_held(self, state):
        identities = {state['source_sha256'], state.get('lineage_root_source_sha256'),
                      *(source['sha256'] for source in state['bundle']['sources'])}
        return bool(identities.intersection(self.source_holds))


def work_scope(wid, state):
    work = state.get('work')
    if not work:
        return ''
    transaction = work.get('transaction') or {}
    return digest([wid, state['source_sha256'], work['plan_sha256'], work['specs'],
                   transaction.get('confirmed_scope_sha256')])


def bound_item(connection, owner_key, name):
    row = connection.execute('SELECT value FROM items WHERE owner=? AND name=?', (owner_key, name)).fetchone()
    return json.loads(row[0]) if row else {}


class WorkQueue:
    def __init__(self, store, config, *, lease_seconds=5):
        self.store = store
        self.config = config
        self.lease_seconds = lease_seconds

    def _read(self, actor, wid=None, task_id=None, kind=None, scope=None):
        sql = ('SELECT status,payload,snapshot FROM work_jobs WHERE app_id=? AND owner=?')
        args = [self.config.app_id, self.store._owner(actor.owner)]
        for field_name, value in (('workspace', wid), ('task_id', task_id), ('kind', kind)):
            if value:
                sql += ' AND ' + field_name + '=?'
                args.append(value)
        if scope:
            sql += " AND json_extract(payload, '$.image_scope')=?"
            args.append(scope)
        with self.store.connect() as connection:
            row = connection.execute(sql + ' ORDER BY created DESC,rowid DESC LIMIT 1', args).fetchone()
        if row:
            return row[0], json.loads(row[1]), json.loads(row[2])
        if task_id:
            raise ValueError('无法在当前会话读取此任务。')
        return None

    def enqueue(self, actor, wid, revision, kind, *, query='', expected_work='', scope='',
                image_budget=0, allow_models=False, policy=None, branch='agent', intake_wid=None):
        from .image_route import valid_scope
        from .intake import PLAN_WITHOUT_MODELS
        from .workspace import Workspace, identity

        if kind not in {'plan', 'draw', 'image_route', 'agent_prepare', 'engineering_edit', 'structural_edit'}:
            raise ValueError('未知持久任务类型。')
        if not isinstance(query, str) or len(query.encode()) > 16000:
            raise ValueError('任务指令超出容量。')
        if type(image_budget) is not int or not 0 <= image_budget <= 24:
            raise ValueError('图像预算超出任务范围。')
        if branch not in {'agent', 'direct'} or (branch == 'direct' and not self.config.may_compare(actor)):
            raise ValueError('当前会话无同版直连对照许可。')
        identity(wid)
        intake_wid = identity(intake_wid) if intake_wid else wid
        owner_key = self.store._owner(actor.owner)
        with self.store.connect() as connection:
            connection.execute('PRAGMA busy_timeout=3000')
            connection.execute('BEGIN IMMEDIATE')
            intake = bound_item(connection, owner_key, 'intake:active')
            if (intake.get('workspace_id') != intake_wid or str(intake.get('revision')) != str(revision)
                    or intake.get('revision') != intake.get('confirmed_revision')):
                raise ValueError('采集修订或来源已变化，请回到原会话核对并确认。')
            if (intake_wid != wid and (branch != 'direct' or
                    bound_item(connection, owner_key, 'comparison:' + intake_wid).get('branch_workspace_id') != wid)):
                raise ValueError('同版对照工作区与原确认范围不符。')
            state = bound_item(connection, owner_key, 'workspace:' + wid)
            if not state:
                raise ValueError('当前会话没有可用的已确认工作区。')
            source_hold = self.config.source_is_held(state)
            work = state.get('work') or {}
            current = state.get('current') or {}
            proposal = state.get('proposal')
            prepared = state.get('image_route_proposal') or {}
            claims_only = 'claims_body' in state['bundle']
            mixed = not claims_only and state['bundle'].get('entry_mode') == 'mixed_materials'
            if (claims_only or mixed) and kind != 'plan':
                raise ValueError('本入口仅允许规划图单。')
            base = {'source_sha256': state['source_sha256'], 'intake_revision': intake['revision'], 'intake_workspace_id': intake_wid,
                    'current_plan_sha256': digest(current.get('plan')), 'workspace_id': wid}
            slot = [self.config.app_id, owner_key, kind, base]
            start = kind
            view_ids = []
            image_count = 0
            if kind == 'draw':
                if not work:
                    return self._repeat_completed(connection, owner_key, wid, kind, current, expected_work)
                scope = work_scope(wid, state)
                if expected_work and expected_work != scope:
                    raise ValueError('图单或确认范围已变化，请重新核对。')
                transaction = work.get('transaction') or {}
                if transaction.get('confirmation_required') and not transaction.get('confirmed_scope_sha256'):
                    raise ValueError('请先确认本版关联修改的范围。')
                slot.extend([scope, work['image_budget']])
                image_count = work['image_budget']
                start = 'render'
                view_ids = list(work['specs'])
                base.update(work_identity=scope, work_plan_sha256=work['plan_sha256'],
                            target_revision=work['plan']['revision'])
            elif kind == 'plan':
                feedback = state.get('planning_feedback') or {}
                if work and not feedback:
                    raise ValueError('当前还有已确认的执行修订，请先核对任务进度。')
                query = query.strip()
                admitted = Workspace.figure_route_query(state, query, False)
                if admitted is None and query == '生成' and proposal:
                    admitted = '规划'
                if not admitted or PLAN_WITHOUT_MODELS.search(query):
                    raise ValueError('请发送明确的图单规划或修改指令。')
                query = admitted
                if feedback and re.match(r'^(?:规划|修改)\s*[：:]\s*\S', query):
                    # A revision is allowed only for a documented omission. Once a
                    # refinement is queued, paraphrasing it reuses the same slot.
                    slot.append(digest({key: feedback.get(key) for key in (
                        'blocking_state', 'revision', 'domain_findings', 'missing_ids')}))
                image_count = 0 if claims_only else image_budget
                start = 'figure_decision'
            elif kind == 'image_route':
                if not proposal:
                    raise ValueError('没有已核的候选图单可判断复杂度。')
                base['proposal_sha256'] = digest(proposal)
                slot.append(base['proposal_sha256'])
                image_count = image_budget
            elif kind == 'agent_prepare':
                if (branch != 'agent' or not valid_scope(scope) or prepared.get('scope') != scope
                        or not proposal or not prepared.get('complex_ids')
                        or scope != digest([actor.owner, wid, state['source_sha256'], proposal])):
                    raise ValueError('复杂图准备范围已失效，请核对当前图单。')
                slot.append(scope)
                base['proposal_sha256'] = digest(proposal)
                view_ids = sorted(prepared['complex_ids'])
            else:
                pending = state.get(kind.removesuffix('_edit') + '_label_pending')
                if not pending:
                    return self._repeat_completed(connection, owner_key, wid, kind, current, expected_work)
                base['edit_sha256'] = digest(pending)
                slot.append(base['edit_sha256'])
            task_id = 'work_' + digest(slot)
            row = connection.execute(
                'SELECT status,payload,snapshot FROM work_jobs WHERE task_id=?', (task_id,)
            ).fetchone()
            if row:
                return self._public(row[0], json.loads(row[1]), json.loads(row[2]))
            if source_hold and kind in {'plan', 'image_route', 'agent_prepare'}:
                return source_hold_status(wid, kind, start)
            allow_models = allow_models and not source_hold
            if kind in {'plan', 'image_route', 'agent_prepare'} and not allow_models:
                return {'workspace_id': wid, 'job_status': 'waiting_input', 'work_kind': kind,
                        'execution_stage': start, 'completed_view_ids': [], 'file_hashes': {},
                        'blocking_state': 'model_execution_disabled', 'resume_action': 'await_operator_policy',
                        'message': '本轮模型派发已关闭，可预览或导入已核图单。'}
            if kind == 'plan' and not feedback and proposal:
                raise ValueError('候选图单已存在，请先预览并确认本版图单。')
            previous = connection.execute(
                "SELECT task_id FROM work_jobs WHERE app_id=? AND owner=? AND workspace=? AND status IN ('queued','running')",
                (self.config.app_id, owner_key, wid),
            ).fetchone()
            if previous:
                raise ValueError('当前工作区已有执行中的任务，请先查看进度。')
            reused = bound_item(connection, owner_key, 'claims-route-reuse:' + wid) if claims_only else None
            if reused:
                from .figure_routing import LUNA_QUESTION_REVISION

                if (reused.get('source_sha256') != state['source_sha256']
                        or reused.get('claims_sha256') != sha256(state['bundle']['claims_body'].encode()).hexdigest()
                        or reused.get('intake_revision') != intake['revision'] or reused.get('query') != query
                        or reused.get('luna_question_revision') != LUNA_QUESTION_REVISION
                        or not re.fullmatch(r'[a-f0-9]{64}', reused.get('proof_sha256', ''))):
                    return {'workspace_id': wid, 'job_status': 'waiting_input', 'work_kind': kind,
                            'execution_stage': start, 'blocking_state': 'claims_route_reuse_unverified',
                            'message': '已保存的 Luna 回执与本次确认范围不一致，请核对来源。'}
                start = 'planning'
            text_calls = (2 if reused else 5 if claims_only else 3) if kind == 'plan' else int(
                kind in {'image_route', 'agent_prepare'})
            if mixed:
                material_profile = self.config.material_profiles.get(
                    state['source_sha256'], {'luna':6, 'sol':1, 'output_cap':20000})
                text_calls = material_profile['luna'] + material_profile['sol']
                image_count = 0
                start = 'material_catalog'
            limits = {'image': image_count if allow_models else 0, 'text': text_calls if allow_models else 0}
            if policy:
                for key in limits:
                    available = policy['limits'].get(key)
                    if available is not None:
                        limits[key] = min(limits[key], available)
            payload = {**base, 'task_id': task_id, 'kind': kind, 'query': query,
                       'tenant': actor.tenant, 'user': actor.user, 'conversation': actor.conversation,
                       'app_id': self.config.app_id, 'build_hashes': self.config.hashes,
                       'allow_models': bool(allow_models), 'policy': policy, 'budgets': limits,
                       'image_scope': scope if kind == 'agent_prepare' else '', 'branch': branch,
                       'input_sha256': digest([base, query, scope, view_ids]), 'view_ids': view_ids}
            if mixed:
                sol = min(material_profile['sol'], limits['text'])
                payload['material_profile'] = {**material_profile, 'sol':sol,
                                               'luna':min(material_profile['luna'], limits['text']-sol)}
                payload['input_sha256'] = digest([payload['input_sha256'], payload['material_profile']])
            if claims_only:
                from .claims_relations import RELATION_CONTRACT
                from .figure_routing import LUNA_QUESTION_REVISION

                from .text_client import CLAIMS_WIRE_OUTPUT_TOKENS

                source = sha256(state['bundle']['claims_body'].encode()).hexdigest()
                cap = self.config.claims_output_caps.get(source, CLAIMS_WIRE_OUTPUT_TOKENS)
                payload['claims_output_cap'] = cap
                payload['claims_contract'] = RELATION_CONTRACT
                payload['luna_question_revision'] = LUNA_QUESTION_REVISION
                payload['input_sha256'] = digest([payload['input_sha256'], RELATION_CONTRACT, LUNA_QUESTION_REVISION, cap])
            snapshot = {'stage': start, 'completed': [], 'files': {},
                        'message': '已受理任务，可在原会话发送“查看进度”。'}
            if reused:
                snapshot.update(routing=reused['routing'], claims_decision_calls=0,
                                claims_reuse_sha256=reused['proof_sha256'],
                                message='已复用核验过的 Luna 回执，等待图单规划。')
            if kind == 'image_route':
                snapshot.update(plan_sha256=base['proposal_sha256'],
                                candidate_views=[{name: view[name] for name in (
                                    'id', 'title', 'purpose', 'delivered_type', 'engine')}
                                    for view in proposal['views']])
            now = time.time()
            connection.execute('''INSERT INTO work_jobs(task_id,app_id,build,owner,workspace,kind,status,
                               created,updated,payload,snapshot) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                               (task_id, self.config.app_id, self.config.build, owner_key, wid, kind,
                                'queued', now, now, canonical(payload), canonical(snapshot)))
        return self._public('queued', payload, snapshot)

    def _repeat_completed(self, connection, owner, wid, kind, current, expected_work):
        row = connection.execute(
            '''SELECT status,payload,snapshot FROM work_jobs WHERE app_id=? AND owner=? AND workspace=?
               AND kind=? ORDER BY created DESC,rowid DESC LIMIT 1''',
            (self.config.app_id, owner, wid, kind),
        ).fetchone()
        if row:
            status, raw_payload, raw_snapshot = row
            payload, snapshot = json.loads(raw_payload), json.loads(raw_snapshot)
            if ((expected_work and expected_work == payload.get('work_identity'))
                    or (kind == 'draw' and payload.get('work_plan_sha256') == digest(current.get('plan')))
                    or (kind in {'engineering_edit', 'structural_edit'} and status == 'completed')):
                return self._public(status, payload, snapshot)
        raise ValueError('没有可执行的已确认修订，请先核对并确认图单或修改范围。')

    def inspect(self, actor, wid=None, *, task_id=None, kind=None, scope=None):
        row = self._read(actor, wid, task_id, kind, scope)
        if row:
            return self._public(*row, include_ledger=True)
        return {'workspace_id': wid or '', 'job_status': 'not_started', 'completed_view_ids': [],
                'file_hashes': {}, 'message': '当前会话尚无此任务，可先核对图单或确认执行。'}

    def confirmed_draw(self, actor, wid, state, confirmation_code):
        """Replay only a receipt for the exact confirmed drawing revision."""
        row = self._read(actor, wid, kind='draw')
        if not row:
            return None
        status, job, snapshot = row
        if job['build_hashes'] != self.config.hashes:
            return None
        work = state.get('work')
        current = state.get('current')
        if work:
            selected = work
            matching_job = job['work_identity'] == work_scope(wid, state)
        elif current and not state.get('proposal'):
            selected = current
            matching_job = job['work_plan_sha256'] == digest(current['plan'])
        else:
            return None
        transaction = selected.get('transaction')
        if transaction:
            stamp = transaction.get('confirmed_scope_sha256')
            matching_code = bool(stamp) and confirmation_code == stamp[:16]
        else:
            matching_code = confirmation_code is None
        if matching_job and matching_code:
            return self._public(status, job, snapshot, include_ledger=True)
        return None

    def _public(self, status, job, snapshot, *, include_ledger=False):
        result = {'workspace_id': job['workspace_id'], 'task_id': job['task_id'],
                  'work_kind': job['kind'], 'job_status': status,
                  **({'claims_output_cap': job['claims_output_cap']} if 'claims_output_cap' in job else {}),
                  'build_hashes': job['build_hashes'], 'intake_revision': str(job['intake_revision']),
                  'execution_stage': snapshot['stage'], 'completed_view_ids': snapshot['completed'],
                  'file_hashes': {name: ref['sha256'] for name, ref in snapshot['files'].items()},
                  'message': snapshot['message'], 'model_calls': 0, 'route': 'reply', 'done': 'yes',
                  'scope': snapshot.get('scope', job['image_scope']),
                  **{key: snapshot[key] for key in ('operation_id', 'operations', 'resume_action', 'blocking_state', 'export_revision',
                                                     'original_request_id',
                                                     'blocked_view', 'technical_status', 'missing_ids',
                                                     'preparation_status', 'comparison_branch', 'candidate_views',
                                                     'image_route_state', 'view_routing', 'domain_findings',
                                                     'planning_command', 'planning_input', 'view_mark_gaps', 'claims_plan_status', 'relation_counts', 'claims_flow_count',
                                                     'claims_review_groups', 'claims_guard_pending', 'claims_semantic_summary', 'claims_view_limit_issues',
                                                     'claims_vote_review', 'claims_recovery', 'claims_reuse_sha256', 'material_summary')
                     if key in snapshot}}
        if include_ledger and (operation := snapshot.get('operation_id')):
            actor = WorkActor(job['tenant'], job['user'], job['conversation'])
            if operation.startswith('figure_route_') and 'claims_vote_review' in snapshot:
                result['dispatch_state'] = 'response_saved'
            else:
                ledger = self.store.operation_status(actor.owner, operation, check_checkpoint=False)
                result['dispatch_state'] = 'result_unknown' if status == 'unknown' and operation.startswith(
                    'figure_route_') else ledger['state']
        if 'resume_action' not in result:
            result['resume_action'] = 'inspect_work' if status in ACTIVE else 'review_saved_result'
        if result.get('file_hashes'):
            result['message'] += f' 下载本任务文件可发送“导出任务 {job["task_id"]}”。'
        if snapshot.get('blocking_state') == 'planner_output_truncated':
            result['message'] += f' 可发送“查看任务 {job["task_id"]}”核对原记录。'
        elif status in ACTIVE or status == 'unknown':
            result['message'] += f' 任务 {job["task_id"]}。'
        return result

    def files(self, actor, wid=None, task_id=None):
        row = self._read(actor, wid, task_id)
        if not row:
            return {}
        _, _, snapshot = row
        return {name: self.store.get_artifact(actor.owner, ref) for name, ref in snapshot['files'].items()}

    def fork_comparison(self, actor, wid, *, branch_wid=None):
        """Operator entry point. Copy one verified Luna decision and its evidence."""
        import copy
        from .client import SUNBURST
        from .workspace import Workspace, identity

        if not self.config.may_compare(actor):
            raise ValueError('当前会话无同版直连对照许可。')
        workspace = Workspace(self.store, actor.owner)
        with self.store.owner_lock(actor.owner):
            state = workspace.read(wid)
            if self.config.comparison_scope:
                intake = self.store.load(actor.owner, 'intake:active')
                path = os.environ.get('PATENT_DRAWING_OPERATOR_POLICY')
                from .operator_policy import load_policy
                if intake['workspace_id'] != wid or intake['confirmed_revision'] != intake['revision'] or not path:
                    raise ValueError('同版对照需要有效的已确认来源及派发场次。')
                self.config.check_comparison_scope(actor, wid, state, intake['revision'],
                                                   run_id=load_policy(path).run_id)
            proposal = state.get('proposal')
            receipt = state.get('image_route_proposal') or {}
            scope = workspace._image_route_binding(wid, state)
            if (self.config.source_is_held(state)
                    or not proposal or state.get('work') or state.get('current') or receipt.get('scope') != scope
                    or not receipt.get('complex_ids') or any(
                        receipt['models'].get(fid) != SUNBURST for fid in receipt['complex_ids'])
                    or set(receipt['models']) != {v['id'] for v in proposal['views'] if v['engine'] == 'image'}
                    or workspace._blocked_attempt(state, receipt['complex_ids'][0])):
                raise ValueError('对照需要未派发、同来源且已由 Luna 确认的复杂候选图单。')
            original = receipt['operation_id']
            if original != 'image_route_' + scope or not self.store.operation_status(actor.owner, original)['response_saved']:
                raise ValueError('对照缺少原始 Luna 响应。')
            for key in ('request', 'receipt', 'response'):
                self.store.get_artifact(actor.owner, self.store.load(actor.owner, original + ':' + key))
            branch_wid = identity(branch_wid or digest([self.config.build, wid, scope, 'direct'])[:32])
            if branch_wid == wid:
                raise ValueError('对照工作区必须有独立快照。')
            family = workspace.lineage_context(state)[1]
            binding = {'workspace_id': wid, 'branch_workspace_id': branch_wid,
                       'source_sha256': state['source_sha256'], 'proposal_sha256': digest(proposal),
                       'route_operation_id': original, 'build': self.config.build, 'lineage': family}
            try:
                saved = self.store.load(actor.owner, 'comparison:' + wid)
            except ValueError:
                saved = None
            if saved == binding:
                return binding
            if saved or workspace._blocked_attempt(state, receipt['complex_ids'][0]):
                raise ValueError('同版对照已经绑定，请核对原始记录。')
            fork = copy.deepcopy(state)
            fork['workspace_id'] = branch_wid
            fork['comparison'] = binding
            fork['image_route_proposal']['scope'] = workspace._image_route_binding(branch_wid, fork)
            fork['image_route_proposal']['focus'] = {}
            fork['image_route_proposal'].pop('preparation_receipt', None)
            self.store.copy_items_for_workspace(actor.owner, wid, branch_wid)
            workspace.save(branch_wid, fork)
            self.store.save(actor.owner, 'comparison:' + wid, binding)
            return binding

    def comparison_workspace(self, actor, wid):
        from .workspace import Workspace

        if not self.config.may_compare(actor):
            raise ValueError('当前会话无同版直连对照许可。')
        binding = self.store.load(actor.owner, 'comparison:' + wid)
        if binding['build'] != self.config.build:
            raise ValueError('对照范围与构建不一致。')
        workspace = Workspace(self.store, actor.owner)
        branch = workspace.read(binding['branch_workspace_id'])
        original = workspace.read(wid)
        if self.config.comparison_scope:
            intake = self.store.load(actor.owner, 'intake:active')
            self.config.check_comparison_scope(actor, wid, original, intake['revision'],
                                               saved_proposal=binding['proposal_sha256'])
        if (branch.get('comparison') != binding or branch['source_sha256'] != binding['source_sha256']
                or original['source_sha256'] != binding['source_sha256']
                or digest(original.get('proposal')) not in {binding['proposal_sha256'], digest(None)}
                or (original.get('image_route_proposal') or {}).get('operation_id') != binding['route_operation_id']):
            raise ValueError('对照来源、图单或原始 Luna 响应已变化。')
        return binding['branch_workspace_id']

    def continue_work(self, actor, wid, *, task_id=None):
        row = self._read(actor, wid, task_id)
        if not row:
            return self.inspect(actor, wid)
        status, job, snapshot = row
        if (status in {'waiting_input', 'partial'} and job['kind'] == 'draw'
                and job['build_hashes'] == self.config.hashes
                and snapshot.get('resume_action') not in {'confirm_intake', 'review_new_revision'}):
            latest = self.store.load(actor.owner, 'intake:active')
            if (latest['workspace_id'] == job['intake_workspace_id']
                    and latest['revision'] == job['intake_revision'] == latest['confirmed_revision']):
                with self.store.connect() as connection:
                    connection.execute('PRAGMA busy_timeout=3000')
                    connection.execute('UPDATE work_jobs SET status=?,updated=? WHERE task_id=? AND status=?',
                                       ('queued', time.time(), job['task_id'], status))
        return self.inspect(actor, wid, task_id=job['task_id'])

    def authorize_agent(self, actor, wid, scope, revision, planning_task):
        value = {'workspace_id': wid, 'app_id': self.config.app_id, 'build': self.config.build,
                 'conversation': actor.conversation, 'revision': revision, 'user': actor.user,
                 'planning_task': planning_task}
        self.store.save(actor.tenant + ':durable-agent', 'agent:' + scope, value)

    def agent_actor(self, tenant, conversation, wid, scope, revision, *, user=None):
        from .image_route import valid_scope

        if not valid_scope(scope):
            raise ValueError('复杂图准备范围无效。')
        value = self.store.load(tenant + ':durable-agent', 'agent:' + scope)
        if (value['app_id'] != self.config.app_id or value['build'] != self.config.build
                or value['conversation'] != conversation or value['workspace_id'] != wid
                or str(revision) != str(value['revision'])
                or (user is not None and value['user'] != user)):
            raise ValueError('复杂图准备授权与当前应用、会话或修订不一致。')
        return WorkActor(tenant, value['user'], conversation)

    def preparation_receipt(self, actor, wid, task_id, scope):
        row = self._read(actor, wid, task_id, 'agent_prepare', scope)
        status, job, snapshot = row
        if (status != 'completed' or job['build_hashes'] != self.config.hashes
                or not snapshot.get('preparation_artifact') or job['image_scope'] != scope):
            raise ValueError('复杂图准备回执待核或已过期。')
        return job, json.loads(self.store.get_artifact(actor.owner, snapshot['preparation_artifact']))

    def candidates(self):
        with self.store.connect() as connection:
            rows = connection.execute('''SELECT task_id,payload FROM work_jobs WHERE app_id=? AND build=?
                AND (status='queued' OR (status='running' AND lease_until<=?))
                ORDER BY updated,rowid LIMIT 128''', (self.config.app_id, self.config.build, time.time())).fetchall()
        return [(task_id, json.loads(payload)) for task_id, payload in rows]

    def claim(self, task_id):
        now, token = time.time(), uuid.uuid4().hex
        with self.store.connect() as connection:
            connection.execute('PRAGMA busy_timeout=3000')
            row = connection.execute('''UPDATE work_jobs SET status='running',lease_token=?,lease_until=?,updated=?
                WHERE task_id=? AND app_id=? AND build=? AND
                (status='queued' OR (status='running' AND lease_until<=?)) RETURNING snapshot''',
                (token, now + self.lease_seconds, now, task_id, self.config.app_id, self.config.build, now)).fetchone()
        return (token, json.loads(row[0])) if row else None

    def checkpoint(self, task_id, token, snapshot, *, status='running'):
        with self.store.connect() as connection:
            connection.execute('PRAGMA busy_timeout=3000')
            row = connection.execute('''UPDATE work_jobs SET status=?,updated=?,snapshot=?,
                lease_token=CASE WHEN ?='running' THEN lease_token END,
                lease_until=CASE WHEN ?='running' THEN lease_until ELSE 0 END
                WHERE task_id=? AND status='running' AND lease_token=?''',
                (status, time.time(), canonical(snapshot), status, status, task_id, token))
            if row.rowcount != 1:
                raise ValueError('任务已转交，请核对已提交的状态。')


def source_hold_status(wid, kind, stage):
    return {'workspace_id': wid, 'job_status': 'waiting_input', 'work_kind': kind,
            'execution_stage': stage, 'completed_view_ids': [], 'file_hashes': {},
            'blocking_state': 'source_external_hold', 'resume_action': 'inspect_original_result',
            'message': '此来源的外部调用处于保护状态，可使用已保存原图、既有证据和本地制图。'}

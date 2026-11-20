"""Private worker policy and audit transport; never enabled by tool/model JSON."""
import hashlib,json,os,re,sqlite3,time,datetime
import stat
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
import httpx
from .errors import DrawingError, DuplicateImageRequest, ImageDispatchNotStarted, SourceLineageMismatch

@dataclass(frozen=True)
class OperatorPolicy:
    run_id:str
    enabled:bool
    ledger_root:Path
    limits:dict
    sha256:str
    recovery_authorizations:tuple=()
    blocked_lineages:tuple=()
    continuity:dict|None=None


_work_context = ContextVar('patent_drawing_work_context', default=None)
_work_operation = ContextVar('patent_drawing_work_operation', default=None)


@contextmanager
def bound_work_task(app_id, build_sha, task_id, owner_key):
    token = _work_context.set((app_id, build_sha, task_id, owner_key))
    operation = _work_operation.set(None)
    try:
        yield
    finally:
        _work_operation.reset(operation)
        _work_context.reset(token)


def bind_work_operation(operation_id):
    if _work_context.get() and operation_id:
        _work_operation.set(operation_id)


def work_audit_headers(transport):
    if isinstance(transport, AuditedTransport) and _work_context.get() and _work_operation.get():
        return {'x-patent-work-context': '/'.join((*_work_context.get(), _work_operation.get()))}
    return {}


def _read_audit_file(path, size):
    if any(parent.is_symlink() for parent in [path, *path.parents]):
        raise ValueError('unsafe audited response path')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o022 or not 1 <= metadata.st_size <= size:
            raise ValueError('unsafe audited response size or ownership')
        content = stream.read(size + 1)
        if len(content) != metadata.st_size:
            raise ValueError('audited response changed during read')
        return content


def read_audited_work_response(policy, original_policy, context, operation_id, request_matches, endpoints):
    """Read one exact terminal HTTP pair. This path never enters the transport."""
    from .ledger_continuity import guard

    if original_policy is None:
        return None
    run = original_policy['run_id']
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', run):
        return None
    root = policy.ledger_root
    db = root / 'http.sqlite3'
    try:
        if db.is_symlink():
            return None
        with guard(root, policy.continuity), sqlite3.connect(db.as_uri() + '?mode=ro', uri=True) as ledger:
            rows = ledger.execute(
                '''SELECT c.slot,c.kind,c.state,c.status,c.request_id,r.policy,w.response_sha256,
                          w.headers_sha256,s.request_slot
                   FROM work_audit w JOIN calls c ON c.run=w.run AND c.slot=w.slot
                   JOIN runs r ON r.run=c.run
                   JOIN request_slots s ON s.run=c.run AND s.slot=c.slot
                   WHERE w.app=? AND w.build=? AND w.task=? AND w.owner=? AND w.operation=? AND w.run=?''',
                (*context, operation_id, run)).fetchall()
            if len(rows) != 1:
                return None
            slot, kind, state, status, request_id, policy_sha, response_sha, headers_sha, request_slot = rows[0]
            if (not re.fullmatch(r'[a-f0-9]{64}', slot) or policy_sha != original_policy['sha256']
                    or state not in {'response_saved', 'provider_rejected'} or not response_sha or not headers_sha):
                return None
            folder = root / run / slot
            intent = json.loads(_read_audit_file(folder / 'intent.json', 8192))
            url = intent.get('url')
            if (endpoints.get(url) != kind or intent.get('run_id') != run or intent.get('kind') != kind
                    or intent.get('policy_sha256') != policy_sha or intent.get('automatic_retries') != 0):
                return None
            raw = _read_audit_file(folder / 'request.json', 64 * 1024 * 1024)
            if (hashlib.sha256(raw).hexdigest() != intent.get('request_sha256')
                    or hashlib.sha256((url + '\n').encode() + raw).hexdigest() != request_slot
                    or not request_matches(raw)):
                return None
            raw_headers = _read_audit_file(folder / 'headers.json', 8192)
            body = _read_audit_file(folder / 'response.bin', 32 * 1024 * 1024)
            if (hashlib.sha256(raw_headers).hexdigest() != headers_sha
                    or hashlib.sha256(body).hexdigest() != response_sha):
                return None
            receipt = json.loads(raw_headers)
            if (receipt.get('status') != status or not isinstance(receipt.get('headers'), dict)
                    or receipt['headers'].get('x-request-id') != request_id
                    or (state == 'response_saved' and status != 200)
                    or (state == 'provider_rejected' and not (400 <= status < 500 and status != 408))):
                return None
            return {'status': status, 'headers': receipt['headers'], 'body_sha256': response_sha,
                    'request_sha256': intent['request_sha256'], 'body': body, 'request': raw}
    except (OSError, ValueError, TypeError, AttributeError, KeyError, sqlite3.Error):
        return None


def authorize_dispatch(lineage:dict,candidate_sha:str,authorization:dict|None)->dict:
    if lineage.get('state') != 'provider_rejected':raise ValueError('UNKNOWN original lineage; inspect original result')
    if not authorization:raise ValueError('private recovery lineage authorization required')
    q=authorization.get('qualification',{})
    expected={'previous_operation_id':lineage.get('operation_id'),'source_sha256':lineage.get('source_sha256'),
              'lineage_sha256':lineage.get('lineage_sha256'),'candidate_sha256':candidate_sha,'evidence_sha256':authorization.get('evidence_sha256')}
    if not q.get('eligible') or not expected['evidence_sha256'] or any(authorization.get(k)!=v or q.get(k)!=v for k,v in expected.items()):
        raise ValueError('recovery candidate/source/lineage/evidence mismatch')
    if authorization.get('kind')=='approved_repair_run':
        from .build_identity import runtime_build_sha256
        fields=['external_attempt_sha256','operator_approval_sha256','contract_sha256','guide_sha256','build_sha256']
        if (q.get('kind')!='approved_repair_run' or q.get('official_feedback')is not False
                or q.get('model')!='gpt-image-2.6-flare'
                or any(not re.fullmatch(r'[a-f0-9]{64}',str(q.get(k,'')))for k in fields)
                or q['build_sha256']!=runtime_build_sha256()):
            raise ValueError('repair authority requires verified history, source/guide and current build')
    return {'permitted':True,'candidate_sha256':candidate_sha}


def load_policy(path):
    p=Path(path)
    if not p.is_absolute()or any(x.is_symlink()for x in [p,*p.parents])or p.stat().st_mode&0o022:raise ValueError('operator policy must be private, absolute and not symlinked')
    if p.stat().st_size>8192:raise ValueError('operator policy capacity')
    raw=p.read_bytes();d=json.loads(raw)
    required={'schema','run_id','enabled','ledger_root','limits'}
    if not required<=set(d)or set(d)-required-{'recovery_authorizations','blocked_lineages','continuity'}or d['schema']!='patent-run-policy-1':raise ValueError('unsupported operator policy')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',d['run_id'])or type(d['enabled'])is not bool:raise ValueError('operator scope invalid')
    root=Path(d['ledger_root'])
    if not root.is_absolute()or any(x.is_symlink()for x in [root,*root.parents]):raise ValueError('unsafe operator ledger')
    if set(d['limits'])!={'image','text'}or any(v is not None and(type(v)is not int or v<0)for v in d['limits'].values()):raise ValueError('invalid trusted limits')
    authorizations=d.get('recovery_authorizations',[])
    if not isinstance(authorizations,list)or len(authorizations)>20:raise ValueError('recovery authorization capacity')
    ids=set()
    for a in authorizations:
        if not isinstance(a,dict)or not {'authorization_id','previous_operation_id','reason','evidence_sha256','allowed_action','issued_at'}<=set(a) or set(a)-{'authorization_id','previous_operation_id','reason','evidence_sha256','allowed_action','issued_at','source_sha256','lineage_sha256','candidate_sha256','qualification','kind'}:raise ValueError('invalid recovery authorization')
        if 'kind'in a and a['kind']!='approved_repair_run':raise ValueError('unknown repair authority kind')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',a['authorization_id'])or a['authorization_id']in ids:raise ValueError('invalid recovery identity')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}/[a-f0-9]{64}',a['previous_operation_id'])or not re.fullmatch(r'[a-f0-9]{64}',a['evidence_sha256']):raise ValueError('recovery evidence identity missing')
        if a['allowed_action']!='image_root'or not isinstance(a['reason'],str)or not 10<=len(a['reason'])<=1000:raise ValueError('invalid recovery scope/reason')
        if datetime.datetime.fromisoformat(a['issued_at']).tzinfo is None:raise ValueError('recovery timestamp needs timezone')
        ids.add(a['authorization_id'])
    blocks=d.get('blocked_lineages',[])
    if not isinstance(blocks,list) or len(blocks)>20:raise ValueError('blocked lineage capacity')
    verified=[]
    for b in blocks:
        if set(b)!={'operation_id','lineage_sha256','source_sha256','state','evidence_path','evidence_sha256'} or b['state'] not in {'provider_rejected','result_unknown'}:raise ValueError('invalid historical lineage')
        ep=Path(b['evidence_path'])
        if not ep.is_absolute() or any(x.is_symlink() for x in [ep,*ep.parents]) or not ep.is_file() or ep.stat().st_size>4*1024*1024 or hashlib.sha256(ep.read_bytes()).hexdigest()!=b['evidence_sha256']:raise ValueError('historical rejection evidence unreadable')
        if any(not re.fullmatch(r'[a-f0-9]{64}',b[k]) for k in ['lineage_sha256','source_sha256']):raise ValueError('lineage identity invalid')
        verified.append(b)
    from .ledger_continuity import validate_config
    if d['enabled'] or d.get('continuity'):validate_config(root,d.get('continuity'))
    return OperatorPolicy(d['run_id'],d['enabled'],root,d['limits'],hashlib.sha256(raw).hexdigest(),tuple(authorizations),tuple(verified),d.get('continuity'))


def model_execution_enabled():
    if os.environ.get('PATENT_DRAWING_ALLOW_MODELS')!='1'or os.environ.get('PATENT_DRAWING_ISOLATED_WORKER')!='1':return False
    path=os.environ.get('PATENT_DRAWING_OPERATOR_POLICY')
    return bool(path and load_policy(path).enabled)


def configured_transport():
    path=os.environ.get('PATENT_DRAWING_OPERATOR_POLICY')
    if not path:return None
    if os.environ.get('PATENT_DRAWING_ISOLATED_WORKER')!='1':raise ValueError('operator policy requires isolated worker')
    p=load_policy(path)
    if os.environ.get('PATENT_DRAWING_ALLOW_MODELS')!='1'or not p.enabled:return ClosedTransport()
    return AuditedTransport(p)

def verify_budget_non_dispatch(workspace_id, view_id, evidence, native_terminal_raw, enabled_policy_raw, request_raw):
    """Verify an inspected finite-estimate rejection on the continuous local audit ledger."""
    return _verify_local_non_dispatch(workspace_id, view_id, evidence, native_terminal_raw,
                                      enabled_policy_raw, request_raw, duplicate=False)


def verify_duplicate_non_dispatch(workspace_id, view_id, evidence, native_terminal_raw, enabled_policy_raw, request_raw):
    """Verify a legacy same-run duplicate against its exact earlier successful request."""
    return _verify_local_non_dispatch(workspace_id, view_id, evidence, native_terminal_raw,
                                      enabled_policy_raw, request_raw, duplicate=True)


def _verify_local_non_dispatch(workspace_id, view_id, evidence, native_terminal_raw, enabled_policy_raw, request_raw, *, duplicate):
    if os.environ.get('PATENT_DRAWING_ISOLATED_WORKER') != '1':
        raise ValueError('non-dispatch reconciliation requires the private isolated worker')
    path = os.environ.get('PATENT_DRAWING_OPERATOR_POLICY')
    policy = load_policy(path) if path else None
    if not policy or policy.enabled:
        raise ValueError('non-dispatch reconciliation requires models closed')
    if (not isinstance(native_terminal_raw, bytes) or not 1 <= len(native_terminal_raw) <= 262144
            or not isinstance(enabled_policy_raw, bytes) or not 1 <= len(enabled_policy_raw) <= 8192
            or hashlib.sha256(native_terminal_raw).hexdigest() != evidence['native_terminal_sha256']
            or hashlib.sha256(enabled_policy_raw).hexdigest() != evidence['policy_sha256']):
        raise ValueError('non-dispatch archival evidence identity differs')
    run_id = evidence['run_id']
    enabled = json.loads(enabled_policy_raw)
    if (not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', run_id)
            or not isinstance(enabled, dict)
            or enabled.get('schema') != 'patent-run-policy-1' or enabled.get('enabled') is not True
            or enabled.get('run_id') != run_id or enabled.get('ledger_root') != str(policy.ledger_root)
            or not isinstance(enabled.get('limits'), dict)
            or 'image' not in enabled['limits']):
        raise ValueError('original operator policy differs')
    cap = enabled['limits']['image']
    if (cap is not None or not duplicate) and (type(cap) is not int or cap < 0):
        raise ValueError('original image limit differs')
    native = json.loads(native_terminal_raw)
    out = native.get('out') if isinstance(native, dict) else None
    terminal = out.get('terminal') if isinstance(out, dict) else None
    workflow_id = out.get('workflow_run_id') if isinstance(out, dict) else None
    if (not isinstance(terminal, dict) or not isinstance(workflow_id, str)
            or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', workflow_id)
            or out.get('kind') != 'native_real'
            or type(out.get('provider_dispatches')) is not int or out['provider_dispatches'] < 1
            or (not duplicate and out['provider_dispatches'] != 1)
            or type(out.get('llm_dispatches')) is not int or out['llm_dispatches'] != 0
            or out.get('invocation_returned') is not True
            or terminal.get('id') != workflow_id or terminal.get('status') != 'failed'
            or terminal.get('outputs') != {}
            or type(terminal.get('total_tokens')) is not int or terminal['total_tokens'] != 0
            or not isinstance(terminal.get('error'), str)
            or not terminal['error'].endswith('[drawing_workspace_error] ' + (
                'request already dispatched; automatic retry forbidden' if duplicate else
                'finite group requires private whole-plan estimate'))):
        raise ValueError('native terminal does not prove the known local preflight rejection')
    if duplicate:
        metadata = native.get('metadata')
        if (native.get('artifacts') != [] or type(terminal.get('created_at')) is not int
                or not isinstance(metadata, list) or not metadata
                or any(not isinstance(item, dict) or item.get('workspace_id') != workspace_id
                       or type(item.get('model_calls')) is not int or item['model_calls'] != 0 for item in metadata)):
            raise ValueError('native duplicate evidence does not bind a zero-call workspace')
    from .ledger_continuity import guard, validate_config
    root = policy.ledger_root
    continuity = enabled.get('continuity')
    validate_config(root, continuity)
    attestation = _read_no_dispatch_attestation(Path(path), evidence['operation_id'])
    expected = {'workspace_id': workspace_id, 'view_id': view_id, 'evidence': evidence,
                'workflow_run_id': workflow_id, 'closed_policy_sha256': policy.sha256,
                'ledger_id': continuity['ledger_id']}
    if json.loads(attestation) != expected:
        raise ValueError('trusted non-dispatch attestation identity differs')
    db = root / 'http.sqlite3'
    if db.is_symlink() or (not duplicate and (root / run_id).exists()):
        raise ValueError('unexpected original dispatch or estimate directory')
    slots = [hashlib.sha256((f'https://api.openai.com/v1/images/{endpoint}\n').encode() + request_raw).hexdigest()
             for endpoint in ('edits', 'generations')]
    with guard(root, continuity), sqlite3.connect(db.as_uri()+'?mode=ro', uri=True) as ledger:
        if ledger.execute('select policy from runs where run=?', (run_id,)).fetchone() != (evidence['policy_sha256'],):
            raise ValueError('original local rejection cannot be reconciled with the HTTP audit ledger')
        prior_slot = None
        if duplicate:
            rows = ledger.execute(
                'select c.run,c.slot,c.kind,c.state,c.status,c.started,l.source,l.lineage '
                'from calls c left join request_slots s on c.run=s.run and c.slot=s.slot '
                'left join lineage_calls l on c.run=l.run and c.slot=l.slot '
                'where coalesce(s.request_slot,c.slot) in (?, ?)', slots).fetchall()
            same_run = [r for r in rows if r[0] == run_id]
            if (len(same_run) != 1 or any(r[2:5] != ('image', 'response_saved', 200) for r in rows)
                    or same_run[0][1] not in slots or same_run[0][5] >= terminal['created_at']
                    or same_run[0][6:] != (evidence['source_sha256'], evidence['lineage_sha256'])):
                raise ValueError('duplicate cannot be reconciled with one earlier successful same-run request')
            prior_slot = same_run[0][1]
        elif (ledger.execute('select 1 from calls where run=?', (run_id,)).fetchone()
              or ledger.execute('select 1 from calls c left join request_slots s on c.run=s.run and c.slot=s.slot '
                                'where coalesce(s.request_slot,c.slot) in (?, ?)', slots).fetchone()):
            raise ValueError('original local rejection cannot be reconciled with the HTTP audit ledger')
        count = ledger.execute('select count(*) from calls').fetchone()[0]
    proof = {'workflow_run_id': workflow_id, 'audit_calls_at_review': count,
             'closed_policy_sha256': policy.sha256, 'ledger_id': continuity['ledger_id'],
             'attestation_sha256': hashlib.sha256(attestation).hexdigest()}
    if duplicate:
        proof['prior_success_slot'] = prior_slot
    return proof


def _read_no_dispatch_attestation(policy_path, operation_id):
    if not re.fullmatch(r'render_[a-f0-9]{64}', operation_id):
        raise ValueError('non-dispatch operation identity invalid')
    directory = policy_path.parent / 'local-no-send'
    path = directory / (operation_id + '.json')
    try:
        if (directory.is_symlink() or not directory.is_dir() or directory.stat().st_mode & 0o022
                or policy_path.parent.stat().st_mode & 0o022):
            raise ValueError('unsafe operator attestation directory')
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as f:
            metadata = os.fstat(f.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o022
                    or not 1 <= metadata.st_size <= 8192):
                raise ValueError('unsafe operator attestation file')
            raw = f.read(8193)
            if len(raw) != metadata.st_size:
                raise ValueError('operator attestation changed during read')
            return raw
    except OSError as error:
        raise ValueError('trusted non-dispatch attestation unavailable') from error


class ClosedTransport(httpx.BaseTransport):
    def handle_request(self,request):raise ValueError('zero-model worker gate is closed')


class AuditedTransport(httpx.BaseTransport):
    def __init__(self,policy,inner=None):
        self.policy=policy;root=policy.ledger_root;root.mkdir(parents=True,exist_ok=True,mode=0o700);self.db=root/'http.sqlite3'
        if self.db.is_symlink():raise ValueError('unsafe audit database')
        from .ledger_continuity import bind_ledger
        if not policy.enabled:
            self.inner=inner;return
        bind_ledger(root,policy.continuity)
        with sqlite3.connect(self.db)as c:
            c.execute('create table if not exists runs(run text primary key,policy text)')
            c.execute('create table if not exists estimates(run text primary key,sha text)')
            c.execute('create table if not exists plan_calls(run text,slot text,plan text,primary key(run,slot))')
            c.execute('create table if not exists calls(run text,slot text,kind text,state text,status integer,request_id text,usage text,started real,primary key(run,slot))')
            c.execute('create table if not exists request_slots(run text,slot text,request_slot text,primary key(run,slot))')
            c.execute('create table if not exists lineage_calls(run text,slot text,lineage text,source text,primary key(run,slot))')
            c.execute('create table if not exists recovery_uses(id text primary key,previous text,run text,slot text)')
            c.execute('''CREATE TABLE IF NOT EXISTS work_audit(run TEXT, slot TEXT, app TEXT, build TEXT,
                        task TEXT, owner TEXT, operation TEXT, response_sha256 TEXT, headers_sha256 TEXT,
                        PRIMARY KEY(run,slot), UNIQUE(app,build,task,owner,operation))''')
            row=c.execute('select policy from runs where run=?',(policy.run_id,)).fetchone()
            if row and row[0]!=policy.sha256:raise ValueError('operator policy changed for an existing run')
            for a in policy.recovery_authorizations:
                if a.get('kind')!='approved_repair_run':continue
                imported=any(b['operation_id']==a['previous_operation_id']and b['source_sha256']==a['source_sha256']
                             and b['lineage_sha256']==a['lineage_sha256']and b['state']=='provider_rejected'for b in policy.blocked_lineages)
                run,slot=a['previous_operation_id'].split('/',1)
                old=c.execute('select c.state,l.source,l.lineage from calls c left join lineage_calls l on c.run=l.run and c.slot=l.slot where c.run=?and c.slot=?',(run,slot)).fetchone()
                if old and old[0]in {'dispatching','result_unknown'}:raise ValueError('UNKNOWN original lineage must be inspected')
                if not imported and not(old and old==('provider_rejected',a['source_sha256'],a['lineage_sha256'])):
                    raise ValueError('repair policy missing verified old lineage history')
            c.execute('insert or ignore into runs values(?,?)',(policy.run_id,policy.sha256))
        self.db.chmod(0o600)
        self.inner=inner
    def handle_request(self,request):
        if not self.policy.enabled:raise ValueError('zero-model worker gate is closed')
        from .ledger_continuity import guard
        with guard(self.policy.ledger_root,self.policy.continuity):
            return self._handle_request(request)

    def validate_image_lineage(self, source_sha, lineage_sha):
        policy = self.policy
        if not (policy.blocked_lineages or any(
                row.get('kind') == 'approved_repair_run' for row in policy.recovery_authorizations)):
            return
        expected = {row['lineage_sha256']
                    for row in (*policy.blocked_lineages, *policy.recovery_authorizations)
                    if row.get('source_sha256') == source_sha}
        if not source_sha or (expected and lineage_sha not in expected):
            raise SourceLineageMismatch()

    def validate_image_plan(self, plan_sha, required_images):
        if not self.policy.enabled:
            raise ValueError('zero-model worker gate is closed')
        estimate, estimate_sha = self._image_estimate(plan_sha)
        if estimate is None:
            return
        from .ledger_continuity import guard
        p = self.policy
        with guard(p.ledger_root, p.continuity), sqlite3.connect(self.db.as_uri()+'?mode=ro', uri=True) as ledger:
            ledger.execute('begin')
            self._check_image_allocation(ledger, plan_sha, estimate, estimate_sha, required_images)
            used = ledger.execute('select count(*) from calls where run=? and kind=?', (p.run_id, 'image')).fetchone()[0]
            if p.limits['image'] - used < required_images:
                raise ValueError('finite group estimate exceeds remaining operator image budget')

    def _check_image_allocation(self, ledger, plan_sha, estimate, estimate_sha, required_images):
        run_id = self.policy.run_id
        prior = ledger.execute('select sha from estimates where run=?', (run_id,)).fetchone()
        if prior and prior[0] != estimate_sha:
            raise ValueError('finite group estimate changed after dispatch')
        used = ledger.execute('select count(*) from plan_calls where run=? and plan=?', (run_id, plan_sha)).fetchone()[0]
        if estimate['plans'][plan_sha] - used < required_images:
            raise ValueError('finite plan allocation exhausted for remaining image work')

    def _image_estimate(self, plan_sha):
        p = self.policy
        cap = p.limits['image']
        if cap is None:
            return None, None
        path = p.ledger_root / p.run_id / 'operator-image-estimate.json'
        if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o022 or path.stat().st_size > 65536:
            raise ValueError('finite group requires private whole-plan estimate')
        raw = path.read_bytes()
        estimate = json.loads(raw)
        plans = estimate.get('plans', {}) if isinstance(estimate, dict) else {}
        if (not isinstance(estimate, dict) or set(estimate) != {'run_id', 'plans'}
                or estimate['run_id'] != p.run_id or not isinstance(plans, dict) or not plans
                or any(not re.fullmatch(r'[a-f0-9]{64}', k) or type(v) is not int or not 0 <= v <= 24
                       for k, v in plans.items())):
            raise ValueError('invalid finite group estimate')
        if sum(plans.values()) > cap:
            raise ValueError('whole group image requirement exceeds approved cap')
        if plan_sha not in plans:
            raise ValueError('image plan is not in approved group estimate')
        return estimate, hashlib.sha256(raw).hexdigest()

    def _handle_request(self,request):
        p=self.policy
        if not p.enabled:raise ValueError('zero-model worker gate is closed')
        if request.method!='POST'or request.url.scheme!='https'or request.url.host!='api.openai.com'or request.url.port not in(None,443):raise ValueError('unapproved inference destination')
        raw=request.read()
        if len(raw)>64*1024*1024:raise ValueError('request evidence capacity')
        payload = json.loads(raw)
        from .client import IMAGE_MODELS
        endpoint = {
            '/v1/responses': ('text', 'gpt-6.1-sol'),
            '/v1/decisions': ('text', 'gpt-6-luna'),
            '/v1/images/generations': ('image', IMAGE_MODELS),
            '/v1/images/edits': ('image', IMAGE_MODELS),
        }.get(request.url.path)
        if (endpoint is None or not isinstance(payload.get('model'), str)
                or payload['model'] not in (endpoint[1] if isinstance(endpoint[1], frozenset) else (endpoint[1],))):
            raise ValueError('unapproved inference endpoint/model')
        kind = endpoint[0]
        work = request.headers.pop('x-patent-work-context', '').split('/')
        if work != [''] and (len(work) != 5 or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', work[0])
                             or not all(re.fullmatch(r'[a-f0-9]{64}', work[i]) for i in (1, 3))
                             or not re.fullmatch(r'work_[a-f0-9]{64}', work[2])
                             or not re.fullmatch(
                                 r'(?:plan_|figure_route_|figure_vote_|image_route_|agent_prepare_|material_|render_)[a-f0-9]{64}',
                                 work[4])
                             or (work[4].startswith(('figure_vote_', 'material_')) and request.url.path != '/v1/decisions')):
            raise ValueError('invalid internal task context')
        plan_sha=request.headers.get('x-patent-plan-sha256');estimate=None;estimate_sha=None
        required_images = 1
        if kind=='image':
            try:
                remaining = request.headers.get('x-patent-plan-remaining', '1')
                if not re.fullmatch(r'[1-9]|1[0-9]|2[0-4]', remaining):
                    raise ValueError('invalid internal remaining image count')
                required_images = int(remaining)
                estimate,estimate_sha=self._image_estimate(plan_sha)
            except (ValueError, OSError) as error:
                raise ImageDispatchNotStarted('图像预算检查未通过，未发送图像请求。') from error
        if 'x-patent-plan-sha256'in request.headers:del request.headers['x-patent-plan-sha256']
        if 'x-patent-plan-remaining' in request.headers:del request.headers['x-patent-plan-remaining']
        source_sha=request.headers.get('x-patent-source-sha256');lineage_sha=request.headers.get('x-patent-lineage-sha256')
        if any(v is not None and not re.fullmatch(r'[a-f0-9]{64}',v) for v in [source_sha,lineage_sha]) or bool(source_sha)!=bool(lineage_sha):raise ValueError('private lineage context invalid')
        if kind=='image':
            self.validate_image_lineage(source_sha, lineage_sha)
        recovery_id=request.headers.get('x-patent-recovery-id');recovery_evidence=request.headers.get('x-patent-recovery-evidence-sha256')
        for name in ['x-patent-source-sha256','x-patent-lineage-sha256','x-patent-recovery-id','x-patent-recovery-evidence-sha256']:
            if name in request.headers:del request.headers[name]
        request_slot=hashlib.sha256((str(request.url)+'\n').encode()+raw).hexdigest();slot=request_slot;recovery=None
        with sqlite3.connect(self.db,timeout=20)as c:
            from .ledger_continuity import attach_and_check,record_claim
            attach_and_check(c,p.ledger_root,p.continuity)
            if recovery_id:
                declared=next((a for a in p.recovery_authorizations if a['authorization_id']==recovery_id),None)
                if not declared or declared['evidence_sha256']!=recovery_evidence:
                    raise ValueError('private recovery header authorization mismatch')
                if declared.get('kind')=='approved_repair_run'and(source_sha!=declared['source_sha256']or lineage_sha!=declared['lineage_sha256']
                        or hashlib.sha256(raw).hexdigest()!=declared['candidate_sha256']):
                    raise ValueError('repair candidate/source/lineage scope mismatch')
                if c.execute('select 1 from recovery_uses where id=?',(recovery_id,)).fetchone():
                    raise ValueError('recovery authorization already consumed')
            if c.execute('select 1 from calls where run=? and slot=?', (p.run_id, slot)).fetchone():
                if kind == 'image':
                    raise DuplicateImageRequest()
                raise ValueError('request already dispatched; automatic retry forbidden')
            blocked=c.execute("select c.run,c.slot,c.state from calls c left join request_slots s on c.run=s.run and c.slot=s.slot where coalesce(s.request_slot,c.slot)=? and c.state in ('dispatching','result_unknown','provider_rejected') order by c.started desc limit 1",(request_slot,)).fetchone()
            lineage_block=None
            if kind=='image' and lineage_sha:
                row=c.execute("select c.run,c.slot,c.state from calls c join lineage_calls l on c.run=l.run and c.slot=l.slot where l.lineage=? order by c.started desc limit 1",(lineage_sha,)).fetchone()
                if row and row[2] in {'dispatching','result_unknown','provider_rejected'}:lineage_block={'operation_id':row[0]+'/'+row[1],'state':row[2],'lineage_sha256':lineage_sha,'source_sha256':source_sha}
                if not row:lineage_block=next((b for b in p.blocked_lineages if b['lineage_sha256']==lineage_sha),None)
                if lineage_block:
                    recovery=next((a for a in p.recovery_authorizations if a['previous_operation_id']==lineage_block['operation_id']),None)
                    authorize_dispatch({**lineage_block,'source_sha256':source_sha},hashlib.sha256(raw).hexdigest(),recovery)
                    if recovery_id!=recovery['authorization_id'] or recovery_evidence!=recovery['evidence_sha256']:raise ValueError('paired Workspace/transport recovery approval missing')
                    blocked=(None,None,lineage_block['state'])
            if recovery_id and not recovery:raise ValueError('original recovery lineage proof missing')
            if blocked:
                if blocked[2]!='provider_rejected':raise ValueError('UNKNOWN original request; inspect original result before any new dispatch')
                previous=lineage_block['operation_id'] if lineage_block else blocked[0]+'/'+blocked[1]
                recovery=recovery or next((a for a in p.recovery_authorizations if a['previous_operation_id']==previous and kind=='image'),None)
                if not recovery:raise ValueError('private recovery authorization required; changing run_id does not reset a rejected request')
                if c.execute('select 1 from recovery_uses where id=?',(recovery['authorization_id'],)).fetchone():raise ValueError('recovery authorization already consumed')
                slot=hashlib.sha256((request_slot+'/'+recovery['authorization_id']).encode()).hexdigest()
            if estimate is not None:
                try:
                    self._check_image_allocation(c, plan_sha, estimate, estimate_sha, required_images)
                except (ValueError, sqlite3.Error) as error:
                    raise ImageDispatchNotStarted('图像预算检查未通过，未发送图像请求。') from error
                c.execute('insert or ignore into estimates values(?,?)',(p.run_id,estimate_sha))
            used=c.execute('select count(*)from calls where run=? and kind=?',(p.run_id,kind)).fetchone()[0];cap=p.limits[kind]
            if cap is not None and cap - used < required_images:
                if kind == 'image':
                    raise ImageDispatchNotStarted('图像预算额度已用完，未发送图像请求。')
                raise ValueError('operator inference budget exhausted')
            folder=p.ledger_root/p.run_id/slot
            folder.mkdir(parents=True,exist_ok=False,mode=0o700);(folder/'request.json').write_bytes(raw)
            (folder/'intent.json').write_text(json.dumps({'url':str(request.url),'kind':kind,'run_id':p.run_id,'policy_sha256':p.sha256,'request_sha256':hashlib.sha256(raw).hexdigest(),'automatic_retries':0,'recovery_authorization_id':recovery['authorization_id']if recovery else None,'previous_operation_id':recovery['previous_operation_id']if recovery else None,'source_sha256':source_sha,'lineage_sha256':lineage_sha},indent=2))
            c.execute('insert into calls values(?,?,?,?,?,?,?,?)',(p.run_id,slot,kind,'dispatching',None,None,None,time.time()))
            c.execute('insert into request_slots values(?,?,?)',(p.run_id,slot,request_slot))
            if work != ['']:
                c.execute('INSERT INTO work_audit(run,slot,app,build,task,owner,operation) VALUES(?,?,?,?,?,?,?)',
                          (p.run_id, slot, *work))
            if lineage_sha:c.execute('insert into lineage_calls values(?,?,?,?)',(p.run_id,slot,lineage_sha,source_sha))
            if recovery:c.execute('insert into recovery_uses values(?,?,?,?)',(recovery['authorization_id'],recovery['previous_operation_id'],p.run_id,slot))
            if estimate is not None:c.execute('insert into plan_calls values(?,?,?)',(p.run_id,slot,plan_sha))
            record_claim(c,p.continuity)
        response=None;transport=None
        try:
            transport=self.inner or httpx.HTTPTransport(retries=0,proxy=os.environ.get('HTTPS_PROXY')or os.environ.get('https_proxy'))
            response=transport.handle_request(request);headers={k:v for k,v in response.headers.items()if k.lower()in{'x-request-id','content-type','date','retry-after'}}
            encoded_headers = json.dumps({'status':response.status_code,'headers':headers},indent=2).encode()
            with (folder/'headers.json').open('xb') as evidence:
                evidence.write(encoded_headers)
                evidence.flush()
                os.fsync(evidence.fileno())
            chunks=[];total=0
            with(folder/'response.bin').open('xb')as f:
                for chunk in ([response.content]if response.is_stream_consumed else response.iter_raw()):
                    f.write(chunk);f.flush();total+=len(chunk)
                    if total>32*1024*1024:raise ValueError('response evidence capacity')
                    chunks.append(chunk)
                os.fsync(f.fileno())
            body=b''.join(chunks)
            if work != ['']:
                for target in (folder, folder.parent):
                    descriptor = os.open(target, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
            try:usage=json.loads(body).get('usage')
            except (ValueError,AttributeError):usage=None
            state='response_saved'if response.status_code==200 else'provider_rejected'if 400<=response.status_code<500 and response.status_code!=408 else'result_unknown'
            with sqlite3.connect(self.db)as c:
                c.execute('update calls set state=?,status=?,request_id=?,usage=?where run=? and slot=?',
                          (state,response.status_code,response.headers.get('x-request-id'),json.dumps(usage),p.run_id,slot))
                if work != ['']:
                    c.execute('UPDATE work_audit SET response_sha256=?,headers_sha256=? WHERE run=? AND slot=?',
                              (hashlib.sha256(body).hexdigest(), hashlib.sha256(encoded_headers).hexdigest(), p.run_id, slot))
            return httpx.Response(response.status_code,headers=response.headers,content=body,request=request)
        except BaseException as error:
            with sqlite3.connect(self.db)as c:
                c.execute("update calls set state='result_unknown' where run=? and slot=? and state='dispatching'",
                          (p.run_id,slot))
            if isinstance(error, ImageDispatchNotStarted):
                raise DrawingError('result_unknown', '传输申领后响应未知，请核对原始记录。') from error
            raise
        finally:
            if response is not None:response.close()
            if transport is not None and self.inner is None:transport.close()
    def close(self):
        if self.inner is not None:self.inner.close()


def effective_image_budget(requested):
    if type(requested)is not int or not 0<=requested<=24:raise ValueError('invalid image budget')
    if not model_execution_enabled():return requested
    policy=load_policy(os.environ['PATENT_DRAWING_OPERATOR_POLICY']);cap=policy.limits['image']
    used=0
    db=policy.ledger_root/'http.sqlite3'
    if cap is not None and db.exists():
        with sqlite3.connect(db)as c:used=c.execute('select count(*)from calls where run=? and kind=?',(policy.run_id,'image')).fetchone()[0]
    available=24 if cap is None else max(0,min(24,cap-used))
    return min(requested,available)if requested else available

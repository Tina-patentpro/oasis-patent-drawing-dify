"""Private persistent state on an operator-owned shared volume, never URL identity.

SQLite's unique insert claims dispatch across processes. A lost response stays
UNKNOWN: deliberately no lease expiry or retry path that can repeat a paid POST.
"""
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import fcntl
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


from gevent.local import local as execution_local
_owner_locks = execution_local()


class WorkspaceBusy(ValueError):
    pass


class OperationStore:
    def __init__(self, root):
        self.root = Path(root).absolute()
        if any(p.is_symlink() for p in [self.root, *self.root.parents]):
            raise ValueError('symlink storage root')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = self.root / 'operations.sqlite3'
        if self.db.is_symlink():
            raise ValueError('symlink database')
        with self.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS ops(owner TEXT, op TEXT, identity TEXT, state TEXT, result BLOB, PRIMARY KEY(owner,op))')
            c.execute('CREATE TABLE IF NOT EXISTS items(owner TEXT, name TEXT, value BLOB, PRIMARY KEY(owner,name))')
            c.execute('CREATE TABLE IF NOT EXISTS budgets(owner TEXT, scope TEXT, kind TEXT, cap INTEGER, used INTEGER, PRIMARY KEY(owner,scope,kind))')
            c.execute('''CREATE TABLE IF NOT EXISTS work_jobs(
                task_id TEXT PRIMARY KEY, app_id TEXT NOT NULL, build TEXT NOT NULL,
                owner TEXT NOT NULL, workspace TEXT NOT NULL, kind TEXT NOT NULL,
                status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                lease_until REAL NOT NULL DEFAULT 0, lease_token TEXT,
                payload BLOB NOT NULL, snapshot BLOB NOT NULL)''')
            c.execute('''CREATE INDEX IF NOT EXISTS work_jobs_claim
                         ON work_jobs(app_id,build,status,lease_until,updated)''')
            c.execute('''CREATE INDEX IF NOT EXISTS work_jobs_owner
                         ON work_jobs(app_id,owner,workspace,created)''')
        self.db.chmod(0o600)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.db, timeout=20)
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _owner(self, owner):
        if not isinstance(owner, str) or not 1 <= len(owner) <= 300:
            raise ValueError('owner identity required')
        return hashlib.sha256(owner.encode()).hexdigest()

    @contextmanager
    def owner_lock(self, owner):
        # One host/shared volume, cross-process flock. Reentrant in this caller;
        # competing invocations reject explicitly before reading mutable state.
        key = str(self.root) + ':' + self._owner(owner)
        active = getattr(_owner_locks, 'active', None)
        if active is None: active = {}; _owner_locks.active = active
        if key in active:
            yield
            return
        directory = self.root / 'locks'
        if directory.is_symlink(): raise ValueError('symlink lock directory')
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / (self._owner(owner) + '.lock')
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            try: fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise WorkspaceBusy('workspace busy; concurrent action rejected before changing state') from None
            active[key] = descriptor
            try: yield
            finally: active.pop(key, None);fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def run_once(self, owner, operation_id, identity, producer, budget=None):
        from .errors import DuplicateImageRequest
        artifact_owner=owner
        owner = self._owner(owner)
        if not isinstance(operation_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', operation_id):
            raise ValueError('invalid operation id')
        key = digest(identity)
        params = (owner, operation_id)
        select = 'SELECT identity,state,result FROM ops WHERE owner=? AND op=?'
        with self.connect() as c:
            row = c.execute(select, params).fetchone()
        if row and row[1] != 'local_preflight_rejected':
            return self._read_operation_result(artifact_owner, operation_id, key, row)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute(select, params).fetchone()
            reclaimed = row is not None and row[:2] == (key, 'local_preflight_rejected') and (budget or {}).get('kind') == 'image'
            if reclaimed and (json.loads(row[2]).get('error') or {}).get('reason') == DuplicateImageRequest.reason:
                raise DuplicateImageRequest()
            inserted = row is None or reclaimed
            if inserted:
                if budget is not None:
                    cap, kind, scope = budget.get('limit'), budget.get('kind'), budget.get('scope')
                    if (cap is not None and(type(cap) is not int or not 0 <= cap <= 100)) or kind not in {'image', 'text', 'vision'} or not isinstance(scope, str) or not 1 <= len(scope) <= 160:
                        raise ValueError('invalid operation budget')
                    c.execute('INSERT OR IGNORE INTO budgets VALUES(?,?,?,?,0)', (owner, scope, kind, cap))
                    configured, used = c.execute('SELECT cap,used FROM budgets WHERE owner=? AND scope=? AND kind=?', (owner, scope, kind)).fetchone()
                    if configured != cap or (configured is not None and used >= configured):
                        raise ValueError('budget exhausted or changed after dispatch')
                    c.execute('UPDATE budgets SET used=used+1 WHERE owner=? AND scope=? AND kind=?', (owner, scope, kind))
                if reclaimed:
                    c.execute('UPDATE ops SET state=?,result=NULL WHERE owner=? AND op=?',
                              ('dispatching', owner, operation_id))
                else:
                    c.execute('INSERT INTO ops VALUES(?,?,?,?,?)', (owner, operation_id, key, 'dispatching', None))
                row = (key, 'dispatching', None)
        if not inserted:
            return self._read_operation_result(artifact_owner, operation_id, key, row)
        # Intent has been committed BEFORE invoking an external side effect.
        try:
            result = producer()
        except Exception as error:
            from .errors import DrawingError, ImageDispatchNotStarted
            terminal='result_unknown';summary={'category':'result_unknown'}
            if isinstance(error, ImageDispatchNotStarted) and (budget or {}).get('kind') == 'image':
                summary = {'category': error.category, 'message': error.public_message}
                if isinstance(error, DuplicateImageRequest):
                    summary['reason'] = error.reason
                terminal = error.category
                result = canonical({'error': summary}).encode()
                receipt = canonical({'op': operation_id, 'identity_sha256': key, 'result': summary,
                                     'recorded_at': time.time()}).encode()
                with self.connect() as c:
                    c.execute('BEGIN IMMEDIATE')
                    c.execute('UPDATE ops SET state=?,result=? WHERE owner=? AND op=?',
                              (terminal, result, owner, operation_id))
                    c.execute('UPDATE budgets SET used=used-1 WHERE owner=? AND scope=? AND kind=?',
                              (owner, budget['scope'], 'image'))
                    c.execute('INSERT INTO items VALUES(?,?,?)',
                              (owner, 'local-image-preflight:' + operation_id + ':' + uuid.uuid4().hex, receipt))
                raise
            if isinstance(error,DrawingError):
                summary={'category':error.category,'request_id':error.request_id,'http_status':error.http_status,'code':error.upstream_code}
                raw=getattr(error,'_response_body',None)
                if isinstance(raw,bytes)and type(error.http_status)is int:
                    try:
                        summary['response_ref']=self.put_artifact(artifact_owner,raw)
                        if 400<=error.http_status<500 and error.http_status!=408 and error.category!='result_unknown':terminal='provider_rejected'
                        elif error.http_status==200:terminal='response_saved_unusable'
                    except Exception:summary={'category':'result_unknown','reason':'error_evidence_not_saved'}
            with self.connect()as c:
                c.execute('UPDATE ops SET state=?,result=? WHERE owner=? AND op=?',(terminal,canonical({'error':summary}).encode(),owner,operation_id))
            raise
        encoded = canonical(result).encode()
        if len(encoded) > 80 * 1024 * 1024:
            raise ValueError('response evidence exceeds storage limit; UNKNOWN')
        # Persist the completed response independently of SQLite's writer.
        # The receipt is scoped to the already claimed owner and operation.
        self._save_response_checkpoint(artifact_owner, operation_id, key, encoded)
        self._commit_response(owner, operation_id, key, encoded)
        return result

    def _read_operation_result(self, owner, operation_id, expected, row):
        identity, state, payload = row
        if identity != expected:
            raise ValueError('operation identity mismatch')
        if state == 'response_saved':
            return json.loads(payload)
        if state in {'provider_rejected', 'response_saved_unusable'}:
            from .errors import DrawingError
            error = json.loads(payload)['error']
            message = ('规划返回截断，已保存原响应及请求回执。'
                       if error['category'] == 'planner_output_truncated' else '已保存本次失败结果；未自动重试')
            raise DrawingError(error['category'], message, error.get('request_id'),
                               upstream_code=error.get('code'), http_status=error.get('http_status'))
        if state == 'dispatching':
            encoded = self._read_response_checkpoint(owner, operation_id, identity)
            if encoded is not None:
                result = json.loads(encoded)
                self._commit_response(self._owner(owner), operation_id, identity, encoded)
                return result
        raise ValueError('UNKNOWN: operation was dispatched; never retry automatically')

    def _response_checkpoint(self, owner, operation_id, identity):
        folder = self.root / self._owner(owner)
        if folder.is_symlink():
            raise ValueError('symlink owner directory')
        return folder / ('response-' + digest([operation_id, identity]) + '.json')

    def _save_response_checkpoint(self, owner, operation_id, identity, encoded):
        path = self._response_checkpoint(owner, operation_id, identity)
        path.parent.mkdir(mode=0o700, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as file:
                file.write(hashlib.sha256(encoded).hexdigest().encode() + b'\n')
                file.write(encoded)
                file.flush()
                os.fsync(file.fileno())
            os.replace(name, path)
            self._sync_directory(path.parent)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _read_response_checkpoint(self, owner, operation_id, identity):
        path = self._response_checkpoint(owner, operation_id, identity)
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 80 * 1024 * 1024 + 65:
                return None
            saved_hash, _, encoded = path.read_bytes().partition(b'\n')
        except OSError:
            return None
        if hashlib.sha256(encoded).hexdigest().encode() != saved_hash:
            return None
        return encoded

    def _commit_response(self, owner, operation_id, identity, encoded, *, allow_unknown=False):
        with self.connect() as connection:
            updated = connection.execute(
                "UPDATE ops SET state='response_saved',result=? "
                "WHERE owner=? AND op=? AND identity=? AND state IN ('dispatching',?)",
                (encoded, owner, operation_id, identity, 'result_unknown' if allow_unknown else 'dispatching'),
            )
            if updated.rowcount != 1:
                row = connection.execute('SELECT identity,state,result FROM ops WHERE owner=? AND op=?',
                                         (owner, operation_id)).fetchone()
                if row != (identity, 'response_saved', encoded):
                    raise ValueError('saved response cannot be bound to the interrupted operation')

    def recover_response(self, owner, operation_id, identity, result, *, allow_unknown=False):
        """Finalize a verified saved response without a new dispatch or budget claim."""
        encoded = canonical(result).encode()
        if len(encoded) > 80 * 1024 * 1024:
            raise ValueError('response evidence exceeds storage limit; UNKNOWN')
        self._commit_response(self._owner(owner), operation_id, digest(identity), encoded, allow_unknown=allow_unknown)

    def recover_truncated_response(self, owner, operation_id, identity, error):
        """Record a verified HTTP 200 truncation against its original dispatch."""
        if error.category != 'planner_output_truncated' or error.http_status != 200:
            raise ValueError('saved response does not prove truncation')
        self.recover_unusable_plan_response(owner, operation_id, identity, error)

    def recover_unusable_plan_response(self, owner, operation_id, identity, error):
        if (error.http_status != 200 or error.category not in {
            'planner_output_truncated', 'planner_invalid_output', 'planner_incomplete'
        }):
            raise ValueError('saved response does not prove an unusable plan')
        captured = self.put_artifact(owner, error._response_body)
        summary = {'category': error.category, 'request_id': error.request_id,
                   'http_status': error.http_status, 'code': error.upstream_code, 'response_ref': captured}
        with self.connect() as connection:
            updated = connection.execute(
                "UPDATE ops SET state='response_saved_unusable',result=? "
                "WHERE owner=? AND op=? AND identity=? AND state IN ('dispatching', 'result_unknown')",
                (canonical({'error': summary}).encode(), self._owner(owner), operation_id, digest(identity)),
            )
            if updated.rowcount != 1:
                raise ValueError('saved unusable response no longer matches its operation')

    def recover_refusal(self, owner, operation_id, identity, summary):
        """Project a verified refusal into the same claimed operation."""
        with self.connect() as connection:
            updated = connection.execute(
                "UPDATE ops SET state='provider_rejected',result=? WHERE owner=? AND op=? AND identity=? AND state='dispatching'",
                (canonical({'error': summary}).encode(), self._owner(owner), operation_id, digest(identity)),
            )
            if updated.rowcount != 1:
                raise ValueError('saved refusal no longer matches its operation')

    def operation_status(self,owner,operation_id, *, check_checkpoint=True):
        with self.connect()as c:
            row=c.execute('SELECT state,result,identity FROM ops WHERE owner=? AND op=?',(self._owner(owner),operation_id)).fetchone()
        if row is None:return {'state':'not_started','error':None,'response_saved':False}
        result=json.loads(row[1])if row[1]is not None else {}
        state = row[0]
        if check_checkpoint and state == 'dispatching' and self._read_response_checkpoint(owner, operation_id, row[2]) is not None:
            state = 'response_saved'
        return {'state':state,'error':result.get('error'),'response_saved':state=='response_saved'}

    def dispatch_count(self, owner, operation_id):
        with self.connect() as c:
            return c.execute('SELECT COUNT(*) FROM ops WHERE owner=? AND op=? AND state!=?',
                             (self._owner(owner), operation_id, 'local_preflight_rejected')).fetchone()[0]

    def put_artifact(self, owner, blob):
        if not isinstance(blob, bytes) or len(blob) > 64 * 1024 * 1024:
            raise ValueError('invalid or oversized artifact')
        owner = self._owner(owner)
        directory = self.root / owner
        if directory.is_symlink():
            raise ValueError('symlink owner directory')
        directory.mkdir(mode=0o700, exist_ok=True)
        h = hashlib.sha256(blob).hexdigest()
        target = directory / h
        if target.is_symlink():
            raise ValueError('symlink artifact')
        previous = target.read_bytes() if target.is_file() and target.stat().st_size <= 64 * 1024 * 1024 else None
        if target.exists() and previous is None:
            raise ValueError('oversized or non-file artifact; restore manually from exact backup')
        if previous is not None and previous != blob:
            # Keep invalid bytes for diagnosis before restoring from verified input.
            with tempfile.NamedTemporaryFile(dir=directory, prefix='invalid-' + h + '-', delete=False) as original:
                original.write(previous)
                original.flush()
                os.fsync(original.fileno())
        if previous != blob:
            fd, tmp = tempfile.mkstemp(dir=directory)
            try:
                with os.fdopen(fd, 'wb') as f:
                    f.write(blob); f.flush(); os.fsync(f.fileno())
                os.replace(tmp, target)
                self._sync_directory(directory)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        return {'artifact_id': owner + '/' + h, 'sha256': h, 'bytes': len(blob)}

    @staticmethod
    def _sync_directory(directory):
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def get_artifact(self, owner, reference):
        if not isinstance(reference, dict):
            raise ValueError('artifact reference required')
        h = reference.get('sha256', '')
        expected_id = self._owner(owner) + '/' + h
        if not re.fullmatch('[a-f0-9]{64}', h) or reference.get('artifact_id') != expected_id:
            raise ValueError('artifact ownership/hash mismatch')
        path = self.root / expected_id
        if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
            raise ValueError('artifact unavailable')
        if path.stat().st_size > 64 * 1024 * 1024:
            raise ValueError('artifact exceeds read bound')
        blob = path.read_bytes()
        if hashlib.sha256(blob).hexdigest() != h:
            raise ValueError('artifact bytes corrupted')
        return blob

    def save(self, owner, name, value):
        encoded = canonical(value).encode()
        if len(encoded) > 4 * 1024 * 1024:
            raise ValueError('state capacity exceeded')
        with self.connect() as c:
            c.execute('INSERT OR REPLACE INTO items VALUES(?,?,?)', (self._owner(owner), name, encoded))

    def copy_items_for_workspace(self, owner, original, fork):
        """Copy only reviewed, reusable reference inputs when forking a comparison."""
        owner = self._owner(owner)
        prefixes = ('reference-preparation', 'reference-bundle', 'prepared-guide',
                    'source-geometry', 'expression', 'member-requirements', 'source-profile')
        with self.connect() as connection:
            for prefix in prefixes:
                before, after = prefix + ':' + original + ':', prefix + ':' + fork + ':'
                connection.execute('''INSERT OR IGNORE INTO items(owner,name,value)
                    SELECT owner,?||substr(name,?),value FROM items
                    WHERE owner=? AND substr(name,1,?)=?''', (after, len(before) + 1, owner, len(before), before))

    def load(self, owner, name):
        with self.connect() as c:
            row = c.execute('SELECT value FROM items WHERE owner=? AND name=?', (self._owner(owner), name)).fetchone()
        if row is None:
            raise ValueError('state unavailable for this owner')
        return json.loads(row[0])

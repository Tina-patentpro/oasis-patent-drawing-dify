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
            except BlockingIOError: raise ValueError('workspace busy; concurrent action rejected before changing state') from None
            active[key] = descriptor
            try: yield
            finally: active.pop(key, None);fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def run_once(self, owner, operation_id, identity, producer, budget=None):
        artifact_owner=owner
        owner = self._owner(owner)
        if not isinstance(operation_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', operation_id):
            raise ValueError('invalid operation id')
        key = digest(identity)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT identity,state,result FROM ops WHERE owner=? AND op=?', (owner, operation_id)).fetchone()
            reclaimed = row is not None and row[:2] == (key, 'local_preflight_rejected') and (budget or {}).get('kind') == 'image'
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
        if row[0] != key:
            raise ValueError('operation identity mismatch')
        if not inserted:
            if row[1] == 'response_saved':
                return json.loads(row[2])
            if row[1]in{'provider_rejected','response_saved_unusable'}:
                from .errors import DrawingError
                saved=json.loads(row[2]);error=saved['error']
                raise DrawingError(error['category'],'已保存本次失败结果；未自动重试',error.get('request_id'))
            raise ValueError('UNKNOWN: operation was dispatched; never retry automatically')
        # Intent has been committed BEFORE invoking an external side effect.
        try:
            result = producer()
        except Exception as error:
            from .errors import DrawingError, ImageDispatchNotStarted
            terminal='result_unknown';summary={'category':'result_unknown'}
            if isinstance(error, ImageDispatchNotStarted) and (budget or {}).get('kind') == 'image':
                summary = {'category': error.category, 'message': error.public_message}
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
        with self.connect() as c:
            c.execute('UPDATE ops SET state=?,result=? WHERE owner=? AND op=?', ('response_saved', encoded, owner, operation_id))
        return result

    def operation_status(self,owner,operation_id):
        with self.connect()as c:
            row=c.execute('SELECT state,result FROM ops WHERE owner=? AND op=?',(self._owner(owner),operation_id)).fetchone()
        if row is None:return {'state':'not_started'}
        result=json.loads(row[1])if row[1]is not None else {}
        return {'state':row[0],'error':result.get('error'),'response_saved':row[0]=='response_saved'}

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
        if not target.exists():
            fd, tmp = tempfile.mkstemp(dir=directory)
            try:
                with os.fdopen(fd, 'wb') as f:
                    f.write(blob); f.flush(); os.fsync(f.fileno())
                os.replace(tmp, target)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        return {'artifact_id': owner + '/' + h, 'sha256': h, 'bytes': len(blob)}

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

    def load(self, owner, name):
        with self.connect() as c:
            row = c.execute('SELECT value FROM items WHERE owner=? AND name=?', (self._owner(owner), name)).fetchone()
        if row is None:
            raise ValueError('state unavailable for this owner')
        return json.loads(row[0])

"""Private audit identity and verified migration; an empty DB is not a reset.

The small registry lives outside the audit directory. A claim updates its
high-water mark in the same SQLite multi-database transaction as the intent.
Shared leases prevent migration while a paid request is in flight.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import re
import sqlite3


def validate_config(root, config):
    if not isinstance(config,dict) or not {'registry_path','ledger_id','mode'}<=config.keys():
        raise ValueError('private audit ledger continuity proof required')
    if set(config)-{'registry_path','ledger_id','mode','previous_db_sha256'}:
        raise ValueError('unsupported ledger continuity fields')
    root=Path(root);registry=Path(config['registry_path'])
    if (not root.is_absolute() or not registry.is_absolute() or registry.is_relative_to(root)
            or any(p.is_symlink()for p in [root,*root.parents,registry,*registry.parents])
            or not re.fullmatch(r'[a-f0-9]{64}',str(config.get('ledger_id','')))
            or config.get('mode')not in {'initialize','resume','adopt_existing','migrate'}):
        raise ValueError('unsafe ledger continuity identity/location/mode')
    if config['mode']in {'adopt_existing','migrate'}and not re.fullmatch(r'[a-f0-9]{64}',str(config.get('previous_db_sha256',''))):
        raise ValueError('verified previous ledger SHA required for migration/adoption')
    return root/'http.sqlite3',registry


@contextmanager
def lease(registry, exclusive=False):
    registry=Path(registry);registry.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    lock=registry.with_name(registry.name+'.lock')
    fd=os.open(lock,os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        try:fcntl.flock(fd,(fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('audit ledger continuity busy; no dispatch')from None
        yield
    finally:
        fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd)


def _sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb')as handle:
        for block in iter(lambda:handle.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def _count(connection):
    exists=connection.execute("select 1 from sqlite_master where type='table' and name='calls'").fetchone()
    return connection.execute('select count(*)from calls').fetchone()[0]if exists else 0


def _verify_db(db, ident, minimum):
    if not db.is_file():raise ValueError('expected audit ledger database missing')
    try:
        with sqlite3.connect('file:'+str(db)+'?mode=ro',uri=True)as c:
            row=c.execute('select id from audit_identity').fetchone()
            if row is None or row[0]!=ident or _count(c)<minimum:
                raise ValueError('audit ledger continuity identity/history rollback detected')
    except sqlite3.Error as e:raise ValueError('audit ledger continuity identity/history missing')from e


def bind_ledger(root, config):
    db,registry=validate_config(root,config);ident=config['ledger_id'];mode=config['mode']
    with lease(registry,exclusive=not registry.exists() or mode=='migrate'):
        if not registry.exists():
            if mode not in {'initialize','adopt_existing'}:
                raise ValueError('expected ledger continuity registry missing')
            if mode=='initialize' and db.exists():
                raise ValueError('existing ledger needs explicit verified adoption, not initialization')
            if mode=='adopt_existing' and (not db.is_file()or _sha(db)!=config['previous_db_sha256']):
                raise ValueError('legacy ledger adoption SHA mismatch')
            db.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            with sqlite3.connect(db)as c:
                c.execute('create table if not exists audit_identity(id text primary key)')
                row=c.execute('select id from audit_identity').fetchone()
                if row and row[0]!=ident:raise ValueError('legacy ledger identity differs')
                c.execute('insert or ignore into audit_identity values(?)',(ident,));minimum=_count(c)
            db.chmod(0o600)
            with sqlite3.connect(registry)as c:
                c.execute('create table bindings(id text primary key,database_path text,minimum_calls integer)')
                c.execute('create table migrations(old_path text,new_path text,previous_sha256 text)')
                c.execute('insert into bindings values(?,?,?)',(ident,str(db),minimum))
            registry.chmod(0o600)
        else:
            try:
                with sqlite3.connect('file:'+str(registry)+'?mode=ro',uri=True)as c:
                    row=c.execute('select id,database_path,minimum_calls from bindings').fetchone()
            except sqlite3.Error as e:raise ValueError('ledger continuity registry unreadable')from e
            if not row or row[0]!=ident:raise ValueError('ledger continuity identity mismatch')
            if row[1]!=str(db):
                if mode!='migrate':raise ValueError('ledger continuity location changed; verified migration required')
                old=Path(row[1]);_verify_db(old,ident,row[2])
                if (not db.is_file()or _sha(old)!=config['previous_db_sha256']or _sha(db)!=config['previous_db_sha256']
                        or any(p.exists()and p.stat().st_size for p in [Path(str(old)+'-wal'),Path(str(old)+'-journal')])):
                    raise ValueError('ledger migration is not an exact quiescent copy')
                _verify_db(db,ident,row[2])
                with sqlite3.connect(registry)as c:
                    c.execute('update bindings set database_path=?where id=?',(str(db),ident))
                    c.execute('insert into migrations values(?,?,?)',(str(old),str(db),config['previous_db_sha256']))
            _verify_db(db,ident,row[2])
    return registry


@contextmanager
def guard(root, config):
    db,registry=validate_config(root,config)
    with lease(registry):
        if not registry.is_file():raise ValueError('expected ledger continuity registry missing')
        try:
            with sqlite3.connect('file:'+str(registry)+'?mode=ro',uri=True)as c:
                row=c.execute('select database_path,minimum_calls from bindings where id=?',(config['ledger_id'],)).fetchone()
            if not row or row[0]!=str(db):raise ValueError('ledger continuity location changed')
            _verify_db(db,config['ledger_id'],row[1])
        except sqlite3.Error as e:raise ValueError('ledger continuity proof unreadable')from e
        yield


def attach_and_check(connection,root,config):
    db,registry=validate_config(root,config)
    connection.execute('attach database ? as audit_guard',(str(registry),))
    connection.execute('begin immediate')
    row=connection.execute('select database_path,minimum_calls from audit_guard.bindings where id=?',(config['ledger_id'],)).fetchone()
    if not row or row[0]!=str(db)or _count(connection)<row[1]:
        raise ValueError('audit ledger continuity/history mismatch before claim')


def record_claim(connection,config):
    connection.execute('update audit_guard.bindings set minimum_calls=?where id=?',(_count(connection),config['ledger_id']))

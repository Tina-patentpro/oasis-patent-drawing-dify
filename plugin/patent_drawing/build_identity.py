"""Identical content identity in source, staged package and installed worker."""
import hashlib
import json
from pathlib import Path, PurePosixPath

from .operation_store import digest


def runtime_build_sha256(root=None) -> str:
    root=Path(root) if root is not None else Path(__file__).resolve().parents[1]
    try:
        manifest=root/'_assets/runtime-members.json'
        if manifest.stat().st_size>32768:raise ValueError('build manifest capacity')
        names=json.loads(manifest.read_text())
        if not isinstance(names,list) or not 1<=len(names)<=160 or len(set(names))!=len(names):
            raise ValueError('build member manifest invalid')
        members={}
        for name in names:
            if not isinstance(name,str) or PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts:
                raise ValueError('build path outside root')
            p=root/name
            if not p.is_file() or p.stat().st_size>24*1024*1024 or any(x.is_symlink() for x in [p,*p.parents]):
                raise ValueError('build member missing or unsafe')
            members[name]=hashlib.sha256(p.read_bytes()).hexdigest()
        return digest(members)
    except (OSError,ValueError,TypeError)as e:
        raise ValueError('runtime build cannot be verified: '+str(e)) from e

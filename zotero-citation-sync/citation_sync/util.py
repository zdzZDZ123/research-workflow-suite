from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


class SyncError(Exception):
    def __init__(self, code, detail=''):
        self.code, self.detail = code, detail or code
        super().__init__(self.detail)


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def digest(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(value).hexdigest()


def file_hash(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (ValueError, OSError) as exc:
        raise SyncError('STATE_UNREADABLE', str(path)) from exc


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8', newline='\n') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def copy_exact(source, target, expected):
    """Exclusive output; verify the source stayed stable throughout copying."""
    source, target = Path(source), Path(target)
    if file_hash(source) != expected:
        raise SyncError('SOURCE_CHANGED', '源文件已变化，请重新扫描')
    data = source.read_bytes()
    if digest(data) != expected or file_hash(source) != expected:
        raise SyncError('SOURCE_CHANGED', '复制期间源文件发生变化')
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('xb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    if file_hash(target) != expected:
        raise SyncError('COPY_VERIFICATION_FAILED')


from __future__ import annotations

import copy
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .util import SyncError, digest, file_hash, now, read_json, write_json


def process_alive(pid):
    """False only when provably absent. No Windows signals are sent."""
    if not isinstance(pid, int) or not 0 < pid <= 2147483647:
        return None
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False if ctypes.get_last_error() == 87 else None
        try:
            code = wintypes.DWORD()
            return (code.value == 259) if kernel.GetExitCodeProcess(handle, ctypes.byref(code)) else None
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return None


class Store:
    def __init__(self, home):
        self.home = Path(home).expanduser().resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        if os.name != 'nt':
            self.home.chmod(0o700)
        self.path = self.home / 'state.json'
        self.pending = self.home / 'journal/pending.json'
        self._writes = self._events = None

    @contextmanager
    def _guard(self):
        # OS ownership is released on crashes; also keep the old-version sentinel.
        with (self.home / 'operation.lock').open('a+b') as handle:
            if handle.seek(0, 2) == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise SyncError('OPERATION_IN_PROGRESS', '另一个本地操作正在运行') from exc
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def _sentinel(self):
        lock = self.home / 'state.lock'
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise SyncError('OPERATION_IN_PROGRESS', '存在运行中或中断的操作；请先检查中断恢复') from exc
        try:
            with os.fdopen(fd, 'w') as handle:
                handle.write(str(os.getpid()))
                handle.flush()
                os.fsync(handle.fileno())
            yield
        finally:
            lock.unlink(missing_ok=True)

    def _state(self):
        state = read_json(self.path) if self.path.exists() else {'schema': 1, 'documents': {}, 'aliases': {}, 'sources': {}, 'sessions': {}}
        if not isinstance(state, dict) or state.get('schema') != 1 or any(
                not isinstance(state.get(k), dict) for k in ('documents', 'aliases', 'sources', 'sessions')):
            raise SyncError('STATE_SCHEMA_UNSUPPORTED')
        return state

    def _target(self, relative):
        if not isinstance(relative, str) or not re.fullmatch(r'state\.json|scans/[a-f0-9]{32}\.json|sessions/[a-f0-9]{32}/session\.json', relative):
            raise SyncError('JOURNAL_TARGET_INVALID')
        target = self.home / relative
        if not target.resolve().is_relative_to(self.home) or target.is_symlink():
            raise SyncError('JOURNAL_TARGET_INVALID')
        return target

    def stage_json(self, path, value):
        if self._writes is None:
            raise SyncError('TRANSACTION_REQUIRED')
        relative = Path(path).resolve().relative_to(self.home).as_posix()
        self._target(relative)
        self._writes[relative] = copy.deepcopy(value)

    @contextmanager
    def transaction(self):
        with self._guard(), self._sentinel():
            if self.pending.exists():
                raise SyncError('RECOVERY_REQUIRED', '检测到未完成的提交，请先检查并恢复中断记录')
            state = self._state()
            before = digest(state)
            self._writes, self._events = {}, []
            try:
                yield state
                if digest(state) != before:
                    self.stage_json(self.path, state)
                if self._writes or self._events:
                    writes = []
                    for relative, value in self._writes.items():
                        target = self._target(relative)
                        writes.append({'path': relative, 'before': file_hash(target) if target.exists() else None,
                                       'after': digest(json.dumps(value, ensure_ascii=False, indent=2).encode()), 'value': value})
                    journal = {'schema': 1, 'id': uuid4().hex, 'at': now(), 'writes': writes, 'events': self._events}
                    write_json(self.pending, journal)
                    self._apply(journal)
            finally:
                self._writes = self._events = None

    def _validate_journal(self, journal):
        if (not isinstance(journal, dict) or journal.get('schema') != 1
                or not re.fullmatch('[a-f0-9]{32}', str(journal.get('id', '')))
                or not isinstance(journal.get('writes'), list) or not isinstance(journal.get('events'), list)):
            raise SyncError('JOURNAL_INVALID')
        seen = set()
        for entry in journal['writes']:
            target = self._target(entry.get('path'))
            if entry['path'] in seen or digest(json.dumps(entry.get('value'), ensure_ascii=False, indent=2).encode()) != entry.get('after'):
                raise SyncError('JOURNAL_INVALID')
            seen.add(entry['path'])
            actual = file_hash(target) if target.exists() else None
            if actual not in (entry.get('before'), entry['after']):
                raise SyncError('RECOVERY_TARGET_CHANGED', '中断后记录被其他操作修改，不能自动重放：' + entry['path'])
            if entry['path'].startswith('sessions/'):
                session = entry['value']
                backup = target.parent / 'original.docx'
                working = target.parent / 'working.docx'
                if (str(backup) != session.get('backup_path') or str(working) != session.get('working_path')
                        or not backup.is_file() or file_hash(backup) != session.get('source_sha256')):
                    raise SyncError('RECOVERY_DOCUMENT_CHANGED', '备份文件缺失或发生变化，请保留现场核查')
                if session.get('status') == 'verified_by_user':
                    expected = session.get('verification', {}).get('working_sha256')
                    if not working.is_file() or file_hash(working) != expected:
                        raise SyncError('RECOVERY_DOCUMENT_CHANGED', '待登记的验证结果对应副本已变化，不能复用旧验证')

    def _write_target(self, path, value):
        write_json(path, value)

    def _apply(self, journal):
        self._validate_journal(journal)
        for entry in journal['writes']:
            target = self._target(entry['path'])
            actual = file_hash(target) if target.exists() else None
            if actual not in (entry.get('before'), entry['after']):
                raise SyncError('RECOVERY_TARGET_CHANGED')
            if actual != entry['after']:
                self._write_target(target, entry['value'])
            if file_hash(target) != entry['after']:
                raise SyncError('JOURNAL_WRITE_VERIFICATION_FAILED')
        receipt = {**journal, 'completed_at': now(), 'writes': [{k: v for k, v in e.items() if k != 'value'} for e in journal['writes']]}
        receipt_path = self.home / 'journal/receipts' / (journal['id'] + '.json')
        if not receipt_path.exists():
            write_json(receipt_path, receipt)
            self._append_log({'at': now(), 'event': 'transaction_committed', 'commit_id': journal['id'], 'events': journal['events']})
        self.pending.unlink()

    def _inspect_recovery(self):
        lock = self.home / 'state.lock'
        blockers, fingerprint = [], {}
        legacy = None
        if lock.exists():
            fingerprint['lock'] = file_hash(lock)
            try:
                pid = int(lock.read_text().strip())
            except (ValueError, OSError):
                pid = None
            alive = process_alive(pid)
            legacy = {'pid': pid, 'owner_alive': alive}
            if alive is not False:
                blockers.append('LOCK_OWNER_RUNNING_OR_UNKNOWN')
        journal, writes = None, []
        if self.pending.exists():
            fingerprint['journal'] = file_hash(self.pending)
            try:
                journal = read_json(self.pending)
                self._validate_journal(journal)
                for entry in journal['writes']:
                    target = self._target(entry['path'])
                    actual = file_hash(target) if target.exists() else None
                    fingerprint[entry['path']] = actual
                    writes.append({'path': entry['path'], 'applied': actual == entry['after']})
            except (SyncError, TypeError, AttributeError, KeyError) as exc:
                blockers.append(exc.code if isinstance(exc, SyncError) else 'JOURNAL_INVALID')
        try:
            state = self._state()
            orphans = [p.name for p in (self.home / 'sessions').glob('*')
                       if p.is_dir() and re.fullmatch('[a-f0-9]{32}', p.name) and p.name not in state['sessions']]
        except SyncError as exc:
            blockers.append(exc.code)
            orphans = []
        required = bool(legacy or self.pending.exists())
        return {'required': required, 'can_recover': required and not blockers,
                'plan_id': digest(fingerprint), 'lock': legacy, 'writes': writes,
                'blockers': blockers, 'preserved_unregistered_directories': orphans,
                'journal_id': journal.get('id') if isinstance(journal, dict) else None}

    def inspect_recovery(self):
        try:
            with self._guard():
                return self._inspect_recovery()
        except SyncError as exc:
            if exc.code != 'OPERATION_IN_PROGRESS':
                raise
            return {'required': False, 'can_recover': False, 'plan_id': None, 'lock': None,
                    'writes': [], 'blockers': [exc.code], 'preserved_unregistered_directories': []}

    def recover(self, plan_id):
        with self._guard():
            report = self._inspect_recovery()
            if report['plan_id'] != plan_id:
                raise SyncError('RECOVERY_PREVIEW_STALE', '中断记录已变化，请重新检查')
            if not report['can_recover']:
                raise SyncError('RECOVERY_BLOCKED', '不能恢复：' + ', '.join(report['blockers']))
            lock = self.home / 'state.lock'
            if lock.exists():
                archive = self.home / 'journal/abandoned-locks' / (uuid4().hex + '.txt')
                archive.parent.mkdir(parents=True, exist_ok=True)
                os.replace(lock, archive)
            with self._sentinel():
                if self.pending.exists():
                    self._apply(read_json(self.pending))
            self._append_log({'at': now(), 'event': 'interrupted_commit_recovered', 'journal_id': report['journal_id']})
            return self._inspect_recovery()

    def _append_log(self, entry):
        with (self.home / 'operations.jsonl').open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + '\n')
            handle.flush()
            os.fsync(handle.fileno())

    def log(self, event, **data):
        entry = {'at': now(), 'event': event, **data}
        if self._events is not None:
            self._events.append(entry)
        else:
            self._append_log(entry)

"""Immutable snapshots and a recoverable file/Zotero two-phase commit.

Only Zotero JS may acknowledge a database update. No SQLite library access.
"""
from __future__ import annotations

import copy
import os
import re
import shutil
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .contracts import (FormatterError, annotations_digest, attachment_identity, canonical,
                        fingerprint, read_json, utc_now, verify_annotation_preservation, write_json)

PENDING = {"replacing", "awaiting_zotero", "rolling_back", "awaiting_rollback_ack"}
STATUSES = PENDING | {"prepared", "converting", "needs_review", "validated", "committed", "rolled_back", "failed"}


def replace_with_backup(destination: Path, replacement: Path, backup: Path):
    """Windows atomically replaces the name and retains the actual displaced version."""
    if os.name != 'nt':
        raise FormatterError('WINDOWS_REQUIRED', '此版本的原位提交仅在 Windows 上提供')
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    replace = kernel.ReplaceFileW
    replace.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID]
    replace.restype = wintypes.BOOL
    def long_path(path):
        value = str(path.absolute())
        return value if value.startswith('\\\\?\\') else ('\\\\?\\UNC\\' + value[2:] if value.startswith('\\\\') else '\\\\?\\' + value)
    if not replace(long_path(destination), long_path(replacement), long_path(backup), 0, None, None):
        raise ctypes.WinError(ctypes.get_last_error())


@contextmanager
def file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise FormatterError("ATTACHMENT_BUSY") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class JobStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def run_dir(self, run_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise FormatterError("INVALID_RUN_ID")
        return self.root / run_id

    def load(self, run_id: str) -> dict:
        directory = self.run_dir(run_id)
        job = read_json(directory / "manifest.json")
        try:
            if job.get("schema_version") != 1 or job.get("run_id") != run_id or Path(job["run_dir"]).resolve() != directory:
                raise ValueError("任务标识或版本不匹配")
            if job.get("status") not in STATUSES:
                raise ValueError("任务状态缺失或无效")
            created = datetime.fromisoformat(job["created_at"])
            if created.tzinfo is None or created.utcoffset() is None:
                raise ValueError("创建时间必须包含时区")
            identity = job["attachment"]
            if type(identity.get("library_id")) is not int or attachment_identity(identity) != identity:
                raise ValueError("附件身份无效")
            original = Path(job["original_path"])
            if original.resolve().parent != directory or original.suffix.lower() != Path(identity["path"]).suffix.lower():
                raise ValueError("原始备份位置无效")
            for field in ("source_sha256", "annotations_original_hash"):
                if not isinstance(job.get(field), str) or not re.fullmatch(r"[a-f0-9]{64}", job[field]):
                    raise ValueError("快照指纹缺失或无效")
            if not isinstance(job.get("annotations_original"), list) or annotations_digest(job["annotations_original"]) != job["annotations_original_hash"]:
                raise ValueError("原始批注快照无效")
            if not isinstance(job.get("report"), dict):
                raise ValueError("检查报告缺失或无效")
            if job["status"] in PENDING | {"validated", "committed", "rolled_back"}:
                candidate = Path(job["candidate_path"])
                if not candidate.resolve().is_relative_to(directory) or candidate.suffix.lower() != original.suffix.lower():
                    raise ValueError("转换结果位置无效")
                if not isinstance(job.get("candidate_sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", job["candidate_sha256"]):
                    raise ValueError("转换结果指纹缺失或无效")
                if not isinstance(job.get("annotations_target"), list) or annotations_digest(job["annotations_target"]) != job.get("annotations_target_hash"):
                    raise ValueError("目标批注快照无效")
        except (KeyError, TypeError, ValueError, AttributeError, OSError, FormatterError) as exc:
            raise FormatterError("INVALID_MANIFEST", "任务记录字段无效，请检查本地记录：" + str(directory / "manifest.json")) from exc
        return job

    def save(self, job: dict, event: str) -> dict:
        job["updated_at"] = utc_now()
        directory = self.run_dir(job["run_id"])
        write_json(directory / "manifest.json", job)
        with (directory / "events.jsonl").open("a", encoding="utf-8") as log:
            log.write(canonical({"time": job["updated_at"], "event": event, "status": job["status"]}) + "\n")
            log.flush()
            os.fsync(log.fileno())
        return job

    def _lock(self, identity: dict):
        return file_lock(self.root / "locks" / f"{identity['library_id']}-{identity['key']}.lock")

    def scan_history(self, identity: dict | None = None) -> dict:
        """Read every record independently; diagnostics never discard or repair a record.

        Unknown records remain in errors even with an attachment filter. Their
        identity and transaction state cannot be trusted, so mutation guards must
        not infer that the requested attachment is safe from an empty jobs list.
        """
        jobs = []
        errors = []
        for path in sorted(self.root.glob("*/manifest.json")):
            try:
                job = self.load(path.parent.name)
            except (FormatterError, OSError, ValueError, TypeError) as exc:
                errors.append({"run_id": path.parent.name, "manifest_path": str(path),
                               "code": exc.code if isinstance(exc, FormatterError) else "MANIFEST_READ_FAILED",
                               "detail": "任务记录无法读取或校验，原文件已保留；请检查此记录后再开始新的覆盖。"})
                continue
            if identity is None or all(job["attachment"].get(k) == identity.get(k) for k in ("key", "library_id")):
                jobs.append(job)
        jobs.sort(key=lambda job: (datetime.fromisoformat(job["created_at"]), job["run_id"]), reverse=True)
        return {"jobs": jobs, "errors": errors}

    def history(self, identity: dict | None = None) -> list[dict]:
        scan = self.scan_history(identity)
        if scan["errors"]:
            raise FormatterError("HISTORY_CORRUPT", "发现无法校验的任务记录，已停止新建和覆盖；请先检查：" +
                                 ", ".join(error["manifest_path"] for error in scan["errors"]))
        return scan["jobs"]

    def pending(self, identity: dict | None = None) -> list[dict]:
        return [job for job in self.history(identity) if job["status"] in PENDING]

    def create(self, request: dict) -> dict:
        if request.get("schema_version") != 1:
            raise FormatterError("REQUEST_VERSION")
        identity = attachment_identity(request["attachment"])
        source = Path(identity["path"])
        annotations = request.get("annotations", [])
        annotations_digest(annotations)
        with self._lock(identity):
            if self.pending(identity):
                raise FormatterError("RECOVERY_REQUIRED", "存在尚未完成的覆盖事务，请先恢复")
            before = fingerprint(source)
            if before != request.get("source_sha256"):
                raise FormatterError("SOURCE_CHANGED")
            rid = uuid.uuid4().hex
            directory = self.run_dir(rid)
            directory.mkdir()
            backup = directory / ("original" + source.suffix.lower())
            shutil.copy2(source, backup)
            if fingerprint(backup) != before or fingerprint(source) != before:
                raise FormatterError("SOURCE_CHANGED_DURING_SNAPSHOT")
            job = {"schema_version": 1, "run_id": rid, "run_dir": str(directory), "status": "prepared",
                   "created_at": utc_now(), "attachment": identity, "source_sha256": before,
                   "original_path": str(backup), "annotations_original": copy.deepcopy(annotations),
                   "annotations_original_hash": annotations_digest(annotations), "report": {"passed": False, "issues": []}}
            write_json(directory / "request.json", request)
            return self.save(job, "snapshot_created")

    def stage(self, run_id: str, candidate: str | Path, report: dict, annotations: list[dict]) -> dict:
        job = self.load(run_id)
        with self._lock(job["attachment"]):
            job = self.load(run_id)
            if job["status"] not in {"prepared", "converting", "needs_review", "validated", "failed"}:
                raise FormatterError("INVALID_STAGE_STATE", job["status"])
            candidate = Path(candidate).resolve()
            if not candidate.is_relative_to(self.run_dir(run_id)) or candidate.suffix.lower() != Path(job["original_path"]).suffix:
                raise FormatterError("INVALID_CANDIDATE_PATH")
            passed = report.get("passed") is True and not any(x.get("blocking", True) for x in report.get("issues", []))
            if passed:
                self._verify_target(job, annotations)
            job.update(candidate_path=str(candidate), candidate_sha256=fingerprint(candidate), report=report,
                       annotations_target=copy.deepcopy(annotations), annotations_target_hash=annotations_digest(annotations),
                       status="validated" if passed else "needs_review")
            return self.save(job, "candidate_validated" if passed else "review_required")

    def _identity_check(self, job: dict, identity: dict) -> None:
        if attachment_identity(identity) != job["attachment"]:
            raise FormatterError("IDENTITY_CHANGED")

    def _verify_target(self, job, annotations):
        if job.get('restores_run'):
            previous=self.load(job['restores_run'])
            self._identity_check(previous,job['attachment'])
            if previous['status']!='committed' or annotations_digest(annotations)!=previous['annotations_original_hash']:
                raise FormatterError('RESTORE_SNAPSHOT_MISMATCH')
        else:
            verify_annotation_preservation(job['annotations_original'],annotations)

    def _hash_check(self, path: str, expected: str, error: str) -> None:
        if not Path(path).is_file() or fingerprint(path) != expected:
            raise FormatterError(error)

    def _replace(self, source: Path, destination: Path, expected_destination: str, expected_source: str,
                 job: dict, *, restoring_conflict=False) -> None:
        # Temp is always on the destination volume. Recheck after the potentially slow copy.
        temporary = destination.with_name(".paper-format-" + uuid.uuid4().hex + destination.suffix)
        displaced = destination.with_name('.paper-format-displaced-' + uuid.uuid4().hex + destination.suffix)
        try:
            with source.open("rb") as reader, temporary.open("xb") as writer:
                shutil.copyfileobj(reader, writer)
                writer.flush()
                os.fsync(writer.fileno())
            self._hash_check(str(temporary),expected_source,'COPY_VERIFICATION_FAILED')
            entry = {'temporary': str(temporary), 'displaced': str(displaced),
                     'expected_before': expected_destination, 'expected_after': expected_source, 'phase': 'intent'}
            job.setdefault('replacements', []).append(entry)
            self.save(job, 'atomic_replace_intent')
            self._hash_check(str(destination), expected_destination, "SOURCE_CHANGED")
            replace_with_backup(destination, temporary, displaced)
            actual = fingerprint(displaced)
            entry.update(displaced_sha256=actual, phase='replaced')
            self.save(job, 'displaced_file_captured')
            archived = self.run_dir(job['run_id']) / displaced.name.lstrip('.')
            with displaced.open('rb') as reader, archived.open('xb') as writer:
                shutil.copyfileobj(reader, writer)
                writer.flush()
                os.fsync(writer.fileno())
            self._hash_check(str(archived), actual, 'BACKUP_CHANGED')
            entry.update(archived=str(archived), phase='verified')
            self.save(job, 'displaced_file_archived')
            displaced.unlink()
            if actual != expected_destination:
                # A concurrent atomic save can race the final hash check. The real displaced
                # bytes are still present. Restore them only while our own output remains current.
                if not restoring_conflict and fingerprint(destination) == expected_source:
                    self._replace(archived, destination, expected_source, actual, job, restoring_conflict=True)
                    job['status'] = 'failed'
                    job['error'] = {'code': 'SOURCE_CHANGED', 'detail': '检测到覆盖瞬间的并发保存，已恢复该次保存的文件；请建立新任务。'}
                    self.save(job, 'concurrent_save_restored')
                raise FormatterError('SOURCE_CHANGED', '发现并发保存，所有实际版本已保留；请查看任务记录。')
            os.utime(destination, None)
        except PermissionError as exc:
            raise FormatterError("FILE_IN_USE", "请关闭正在编辑该附件的程序后重试") from exc
        finally:
            if temporary.exists():
                temporary.unlink()

    def _check_displaced(self, job):
        """Recovery must never erase an unexpected version captured before a crash."""
        for entry in job.get('replacements', []):
            captured = Path(entry.get('archived') or entry['displaced'])
            if entry.get('phase') == 'verified' and not captured.is_file():
                raise FormatterError('BACKUP_CHANGED')
            if captured.exists() and fingerprint(captured) != entry['expected_before']:
                raise FormatterError('RECOVERY_CONCURRENT_EDIT', '覆盖瞬间有新的文件版本，已保留在 ' + str(captured) + '；需要本地核对。')

    def _restore_missing_destination(self, job, destination):
        # ReplaceFileW error 1177 can leave the destination only under the backup
        # name. Recover only when the recorded displacement proves this case.
        if not any(Path(e.get('archived') or e['displaced']).is_file() for e in job.get('replacements', [])):
            raise FormatterError('RECOVERY_SOURCE_MISSING')
        temporary = destination.with_name('.paper-format-recovery-' + uuid.uuid4().hex + destination.suffix)
        try:
            with Path(job['original_path']).open('rb') as reader, temporary.open('xb') as writer:
                shutil.copyfileobj(reader, writer)
                writer.flush()
                os.fsync(writer.fileno())
            self._hash_check(str(temporary), job['source_sha256'], 'BACKUP_CHANGED')
            if os.name != 'nt':
                raise FormatterError('WINDOWS_REQUIRED')
            # Unlike os.replace, Windows rename fails if another writer has recreated
            # the original name. Never overwrite that writer during recovery.
            try:
                os.rename(temporary, destination)
            except FileExistsError as exc:
                raise FormatterError('RECOVERY_SOURCE_CHANGED') from exc
        finally:
            if temporary.exists():
                temporary.unlink()

    def begin_commit(self, run_id: str, identity: dict, annotations: list[dict]) -> dict:
        job = self.load(run_id)
        with file_lock(self.run_dir(run_id) / 'conversion.lock'), self._lock(job["attachment"]):
            job = self.load(run_id)
            self._identity_check(job, identity)
            if job["status"] == "committed":
                self._hash_check(identity["path"], job["candidate_sha256"], "SOURCE_CHANGED")
                if annotations_digest(annotations) != job["annotations_target_hash"]:
                    raise FormatterError("ANNOTATIONS_CHANGED")
                return job
            if job["status"] != "validated" or job["report"].get("passed") is not True:
                raise FormatterError("NOT_VALIDATED", job["status"])
            if any(x["run_id"] != run_id for x in self.pending(identity)):
                raise FormatterError("RECOVERY_REQUIRED")
            self._hash_check(identity["path"], job["source_sha256"], "SOURCE_CHANGED")
            self._hash_check(job["original_path"], job["source_sha256"], "BACKUP_CHANGED")
            self._hash_check(job["candidate_path"], job["candidate_sha256"], "CANDIDATE_CHANGED")
            if annotations_digest(annotations) != job["annotations_original_hash"]:
                raise FormatterError("ANNOTATIONS_CHANGED")
            self._verify_target(job, job['annotations_target'])
            job["status"] = "replacing"
            self.save(job, "file_replace_intent")
            try:
                self._replace(Path(job["candidate_path"]), Path(identity["path"]), job["source_sha256"],job['candidate_sha256'], job)
            except Exception:
                # If replacement succeeded but a subsequent operation failed, recovery still owns the file.
                if Path(identity["path"]).is_file() and fingerprint(identity["path"]) == job["source_sha256"]:
                    job["status"] = "validated"
                    self.save(job, "file_replace_aborted")
                raise
            job["status"] = "awaiting_zotero"
            return self.save(job, "file_replaced")

    def finish_commit(self, run_id: str, identity: dict, annotations: list[dict]) -> dict:
        job = self.load(run_id)
        with self._lock(job["attachment"]):
            job = self.load(run_id)
            self._identity_check(job, identity)
            if job["status"] not in {"awaiting_zotero", "committed"}:
                raise FormatterError("INVALID_ACK_STATE")
            self._check_displaced(job)
            self._hash_check(identity["path"], job["candidate_sha256"], "SOURCE_CHANGED")
            if annotations_digest(annotations) != job["annotations_target_hash"]:
                raise FormatterError("ANNOTATION_ACK_MISMATCH")
            job["status"] = "committed"
            return self.save(job, "zotero_acknowledged")

    def begin_rollback(self, run_id: str, identity: dict, annotations: list[dict]) -> dict:
        job = self.load(run_id)
        with self._lock(job["attachment"]):
            job = self.load(run_id)
            self._identity_check(job, identity)
            if job["status"] not in PENDING:
                raise FormatterError("INVALID_ROLLBACK_STATE")
            self._check_displaced(job)
            if annotations_digest(annotations) not in {job["annotations_original_hash"], job["annotations_target_hash"]}:
                raise FormatterError("RECOVERY_ANNOTATIONS_CHANGED", "批注有新的修改，需要人工核对")
            destination = Path(identity['path'])
            current = fingerprint(destination) if destination.is_file() else None
            if current not in {None, job["source_sha256"], job["candidate_sha256"]}:
                raise FormatterError("RECOVERY_SOURCE_CHANGED")
            self._hash_check(job["original_path"], job["source_sha256"], "BACKUP_CHANGED")
            job["status"] = "rolling_back"
            self.save(job, "rollback_intent")
            if current is None:
                self._restore_missing_destination(job, destination)
            elif current != job["source_sha256"]:
                self._replace(Path(job["original_path"]), Path(identity["path"]), current,job['source_sha256'], job)
            job["status"] = "awaiting_rollback_ack"
            return self.save(job, "file_restored")

    def finish_rollback(self, run_id: str, identity: dict, annotations: list[dict]) -> dict:
        job = self.load(run_id)
        with self._lock(job["attachment"]):
            job = self.load(run_id)
            self._identity_check(job, identity)
            if job["status"] not in {"awaiting_rollback_ack", "rolled_back"}:
                raise FormatterError("INVALID_ROLLBACK_ACK_STATE")
            self._hash_check(identity["path"], job["source_sha256"], "SOURCE_CHANGED")
            if annotations_digest(annotations) != job["annotations_original_hash"]:
                raise FormatterError("ANNOTATION_ACK_MISMATCH")
            job["status"] = "rolled_back"
            return self.save(job, "rollback_acknowledged")

    def prepare_restore(self, run_id: str, identity: dict, annotations: list[dict]) -> dict:
        previous = self.load(run_id)
        self._identity_check(previous, identity)
        if previous["status"] != "committed":
            raise FormatterError("RESTORE_REQUIRES_COMMITTED_VERSION")
        self._hash_check(previous["original_path"], previous["source_sha256"], "BACKUP_CHANGED")
        # A restore is a new transaction, so the pre-restore version is never discarded.
        job = self.create({"schema_version": 1, "attachment": identity,
                           "source_sha256": fingerprint(identity["path"]), "annotations": annotations})
        candidate = Path(job["run_dir"]) / ("restored" + Path(identity["path"]).suffix.lower())
        shutil.copy2(previous["original_path"], candidate)
        job["restores_run"] = run_id
        self.save(job, "restore_prepared")
        return self.stage(job["run_id"], candidate, {"passed": True, "issues": [], "kind": "verified_backup_restore"}, previous["annotations_original"])

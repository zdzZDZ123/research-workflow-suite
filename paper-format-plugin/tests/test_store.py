import hashlib
from pathlib import Path

import pytest

from paper_formatter.contracts import FormatterError, fingerprint
from paper_formatter.store import JobStore


def setup_job(tmp_path):
    source = tmp_path / "original.pdf"
    source.write_bytes(b"%PDF-1.4 original fixture")
    identity = {"library_id": 1, "key": "ABCD1234", "parent_key": "PARENT01", "path": str(source)}
    annotations = [{"key": "NOTE1234", "type": "highlight", "text": "original", "color": "#ffd400", "position": {"pageIndex": 0, "rects": [[1, 2, 3, 4]]}}]
    store = JobStore(tmp_path / "jobs")
    job = store.create({"schema_version": 1, "attachment": identity, "source_sha256": fingerprint(source), "annotations": annotations})
    candidate = Path(job["run_dir"]) / "candidate.pdf"
    candidate.write_bytes(b"%PDF-1.4 formatted fixture")
    mapped = [{**annotations[0], "position": {"pageIndex": 1, "rects": [[10, 20, 30, 40]]}}]
    store.stage(job["run_id"], candidate, {"passed": True, "issues": []}, mapped)
    return store, job["run_id"], source, identity, annotations, mapped


def test_commit_requires_zotero_ack_and_keeps_original(tmp_path):
    store, rid, source, identity, annotations, mapped = setup_job(tmp_path)
    result = store.begin_commit(rid, identity, annotations)
    assert result["status"] == "awaiting_zotero"
    assert source.read_bytes().endswith(b"formatted fixture")
    assert Path(result["original_path"]).read_bytes().endswith(b"original fixture")
    assert store.finish_commit(rid, identity, mapped)["status"] == "committed"
    assert store.finish_commit(rid, identity, mapped)["status"] == "committed"


def test_changed_original_cannot_be_overwritten(tmp_path):
    store, rid, source, identity, annotations, _ = setup_job(tmp_path)
    source.write_bytes(b"%PDF-1.4 user just edited")
    with pytest.raises(FormatterError, match="SOURCE_CHANGED"):
        store.begin_commit(rid, identity, annotations)
    assert source.read_bytes().endswith(b"user just edited")


def test_changed_annotation_or_identity_blocks_commit(tmp_path):
    store, rid, source, identity, annotations, _ = setup_job(tmp_path)
    with pytest.raises(FormatterError, match="ANNOTATIONS_CHANGED"):
        store.begin_commit(rid, identity, [{**annotations[0], "comment": "new note"}])
    with pytest.raises(FormatterError, match="IDENTITY_CHANGED"):
        store.begin_commit(rid, {**identity, "key": "WRONG001"}, annotations)
    assert source.read_bytes().endswith(b"original fixture")


def test_changed_candidate_is_not_committed(tmp_path):
    store, rid, source, identity, annotations, _ = setup_job(tmp_path)
    Path(store.load(rid)["candidate_path"]).write_bytes(b"tampered")
    with pytest.raises(FormatterError, match="CANDIDATE_CHANGED"):
        store.begin_commit(rid, identity, annotations)
    assert source.read_bytes().endswith(b"original fixture")


def test_recovery_restores_file_and_requires_annotation_ack(tmp_path):
    store, rid, source, identity, annotations, mapped = setup_job(tmp_path)
    store.begin_commit(rid, identity, annotations)
    recovered = store.begin_rollback(rid, identity, mapped)
    assert recovered["status"] == "awaiting_rollback_ack"
    assert source.read_bytes().endswith(b"original fixture")
    assert store.finish_rollback(rid, identity, annotations)["status"] == "rolled_back"


def test_unresolved_checks_cannot_be_approved_by_flag(tmp_path):
    store, rid, source, identity, annotations, mapped = setup_job(tmp_path)
    candidate = Path(store.load(rid)["candidate_path"])
    store.stage(rid, candidate, {"passed": False, "issues": [{"code": "TEXT_LOSS", "blocking": True}]}, mapped)
    with pytest.raises(FormatterError, match="NOT_VALIDATED"):
        store.begin_commit(rid, identity, annotations)


def test_annotation_identity_loss_is_blocking(tmp_path):
    store, rid, source, identity, annotations, mapped = setup_job(tmp_path)
    candidate = Path(store.load(rid)["candidate_path"])
    with pytest.raises(FormatterError, match="ANNOTATION_SET_CHANGED"):
        store.stage(rid, candidate, {"passed": True, "issues": []}, [])


def test_restore_snapshots_new_annotations_before_restoring_old_set(tmp_path):
    store, rid, source, identity, annotations, mapped = setup_job(tmp_path)
    store.begin_commit(rid, identity, annotations)
    store.finish_commit(rid, identity, mapped)
    current=[{**mapped[0], 'comment':'edited after formatting'},
             {**mapped[0], 'key':'NEWNOTE1','comment':'new annotation'}]
    restored=store.prepare_restore(rid,identity,current)
    assert restored['annotations_original']==current
    assert restored['annotations_target']==annotations
    store.begin_commit(restored['run_id'],identity,current)
    store.finish_commit(restored['run_id'],identity,annotations)


def test_replace_detects_candidate_changed_during_copy(tmp_path,monkeypatch):
    import paper_formatter.store as module
    store,rid,source,identity,annotations,mapped=setup_job(tmp_path)
    real_copy=module.shutil.copyfileobj
    def broken(reader,writer):
        writer.write(b'changed during copy')
    monkeypatch.setattr(module.shutil,'copyfileobj',broken)
    with pytest.raises(FormatterError,match='COPY_VERIFICATION_FAILED'):
        store.begin_commit(rid,identity,annotations)
    assert source.read_bytes().endswith(b'original fixture')


@pytest.mark.skipif(__import__('os').name!='nt',reason='Windows writer sharing semantics')
def test_open_writer_blocks_replacement(tmp_path):
    store,rid,source,identity,annotations,mapped=setup_job(tmp_path)
    with source.open('r+b'):
        with pytest.raises(FormatterError,match='FILE_IN_USE'):
            store.begin_commit(rid,identity,annotations)
    assert source.read_bytes().endswith(b'original fixture')


def test_history_and_restore_preserve_attachment_and_both_versions(tmp_path):
    store, rid, source, identity, annotations, mapped = setup_job(tmp_path)
    store.begin_commit(rid, identity, annotations)
    store.finish_commit(rid, identity, mapped)
    restored = store.prepare_restore(rid, identity, mapped)
    assert restored["attachment"] == identity
    assert Path(restored["original_path"]).read_bytes().endswith(b"formatted fixture")
    store.begin_commit(restored["run_id"], identity, mapped)
    store.finish_commit(restored["run_id"], identity, annotations)
    assert source.read_bytes().endswith(b"original fixture")
    assert len(store.history(identity)) == 2

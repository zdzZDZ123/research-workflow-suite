import copy
from pathlib import Path

import pytest

from paper_formatter.contracts import FormatterError, fingerprint, write_json
from paper_formatter.store import JobStore, PENDING


def create_job(tmp_path):
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF original")
    identity = {"library_id": 1, "key": "PAPER001", "parent_key": "PARENT01", "path": str(source)}
    store = JobStore(tmp_path / "jobs")
    request = {"schema_version": 1, "attachment": identity, "source_sha256": fingerprint(source), "annotations": []}
    job = store.create(request)
    candidate = Path(job["run_dir"]) / "candidate.pdf"
    candidate.write_bytes(b"%PDF formatted")
    job = store.stage(job["run_id"], candidate, {"passed": True, "issues": []}, [])
    return store, job, source, identity, request


def corrupt_record(store):
    path = store.run_dir("f" * 32) / "manifest.json"
    path.parent.mkdir()
    path.write_bytes(b'{"status":"replacing",')
    return path


def test_scan_retains_valid_pending_and_diagnostic_without_touching_corruption(tmp_path):
    store, job, source, identity, request = create_job(tmp_path)
    store.begin_commit(job["run_id"], identity, [])
    broken = corrupt_record(store)
    before = broken.read_bytes(), broken.stat().st_mtime_ns

    scan = store.scan_history(identity)
    assert [entry["run_id"] for entry in scan["jobs"]] == [job["run_id"]]
    assert scan["jobs"][0]["status"] in PENDING
    assert len(scan["errors"]) == 1
    assert scan["errors"][0]["manifest_path"] == str(broken)
    assert scan["errors"][0]["run_id"] == "f" * 32
    assert scan["errors"][0]["code"] == "MANIFEST_READ_FAILED"
    assert (broken.read_bytes(), broken.stat().st_mtime_ns) == before

    # Recovery of the readable transaction must remain possible in isolation.
    store.begin_rollback(job["run_id"], identity, [])
    assert store.finish_rollback(job["run_id"], identity, [])["status"] == "rolled_back"
    assert source.read_bytes() == b"%PDF original"
    assert (broken.read_bytes(), broken.stat().st_mtime_ns) == before


@pytest.mark.parametrize("action", ["history", "pending", "create", "commit"])
def test_unknown_corrupt_transaction_blocks_new_mutations(tmp_path, action):
    store, job, source, identity, request = create_job(tmp_path)
    broken = corrupt_record(store)
    before = source.read_bytes(), broken.read_bytes()
    with pytest.raises(FormatterError, match="HISTORY_CORRUPT"):
        if action in {"history", "pending"}:
            getattr(store, action)(identity)
        elif action == "create":
            store.create(request)
        else:
            store.begin_commit(job["run_id"], identity, [])
    assert (source.read_bytes(), broken.read_bytes()) == before
    assert store.load(job["run_id"])["status"] == "validated"
    assert len(list(store.root.glob("*/manifest.json"))) == 2


def test_attachment_filter_does_not_hide_corrupt_unknown_identity(tmp_path):
    store, job, source, identity, request = create_job(tmp_path)
    corrupt_record(store)
    unrelated = {**identity, "key": "OTHER001"}
    scan = store.scan_history(unrelated)
    assert scan["jobs"] == []
    assert len(scan["errors"]) == 1
    with pytest.raises(FormatterError, match="HISTORY_CORRUPT"):
        store.pending(unrelated)


@pytest.mark.parametrize("field,value", [
    ("schema_version", 2),
    ("run_id", "e" * 32),
    ("run_dir", "wrong-folder"),
    ("status", "unknown"),
    ("created_at", "yesterday"),
    ("created_at", "2026-10-23T12:30:00"),
    ("attachment", {}),
    ("attachment", None),
    ("source_sha256", "broken"),
    ("annotations_original", [{"key": "bad"}]),
    ("report", None),
    ("candidate_path", "outside.pdf"),
    ("candidate_sha256", None),
    ("annotations_target", [{"key": "bad"}]),
])
def test_scan_quarantines_malformed_fields_logically_without_changing_files(tmp_path, field, value):
    store, job, source, identity, request = create_job(tmp_path)
    malformed = copy.deepcopy(job)
    malformed[field] = value
    manifest = Path(job["run_dir"]) / "manifest.json"
    write_json(manifest, malformed)
    before = manifest.read_bytes()
    scan = store.scan_history()
    assert scan["jobs"] == []
    assert len(scan["errors"]) == 1
    assert scan["errors"][0]["code"] == "INVALID_MANIFEST"
    with pytest.raises(FormatterError, match="INVALID_MANIFEST"):
        store.load(job["run_id"])
    assert manifest.read_bytes() == before


def test_scan_exposes_nonobject_record_and_invalid_directory_name(tmp_path):
    store = JobStore(tmp_path / "jobs")
    for name, content in [("a" * 32, "[]"), ("unknown-job", "{}")]:
        path = store.root / name / "manifest.json"
        path.parent.mkdir()
        path.write_text(content, encoding="utf-8")
    scan = store.scan_history()
    assert scan["jobs"] == []
    assert {error["code"] for error in scan["errors"]} == {"JSON_OBJECT_REQUIRED", "INVALID_RUN_ID"}


def test_scan_orders_by_timestamp_including_offsets(tmp_path):
    store, job, source, identity, request = create_job(tmp_path)
    earlier = store.create(request)
    job["created_at"] = "2026-10-23T06:00:00+00:00"
    earlier["created_at"] = "2026-10-23T12:00:00+08:00"
    store.save(job, "test_order")
    store.save(earlier, "test_order")
    scan = store.scan_history(identity)
    assert scan["errors"] == []
    assert [entry["run_id"] for entry in scan["jobs"]] == [job["run_id"], earlier["run_id"]]


def test_missing_attachment_file_does_not_hide_recoverable_transaction(tmp_path):
    store, job, source, identity, request = create_job(tmp_path)
    store.begin_commit(job["run_id"], identity, [])
    source.unlink()
    scan = store.scan_history(identity)
    assert scan["errors"] == []
    assert scan["jobs"][0]["status"] == "awaiting_zotero"

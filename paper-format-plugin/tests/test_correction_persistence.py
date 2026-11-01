from pathlib import Path

import pytest
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt
from PIL import Image

import paper_formatter.engine as module
from paper_formatter.cli import dispatch
from paper_formatter.contracts import FormatterError, fingerprint
from paper_formatter.engine import Engine
from paper_formatter.profiles import blank_profile


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    # Keep real OOXML transformations, integrity/layout validation and disk job
    # storage; substitute only rendering and empty-annotation PDF inspection.
    def render(source, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / 'proof.pdf'
        target.write_bytes(b'%PDF-1.4 mocked rendering only')
        return {'pdf': str(target)}
    monkeypatch.setattr(module, 'render_docx', render)
    def inspect(pdf, directory):
        directory=Path(directory)
        directory.mkdir(parents=True,exist_ok=True)
        image=directory/'page.png'
        Image.new('RGB',(10,10),'white').save(image)
        return {'issues': [], 'pages': [{'index':0,'width':10,'height':10,'image':str(image)}], 'inspection': {}}
    monkeypatch.setattr(module, 'inspect_render', inspect)
    monkeypatch.setattr(module, 'render_content_issues', lambda *a: [])
    monkeypatch.setattr(module, 'font_issues', lambda *a: [])
    monkeypatch.setattr(module, 'migrate_annotations', lambda *a, **kw: {'complete': True, 'annotations': [], 'unresolved': []})
    source = tmp_path / 'paper.docx'
    document = Document()
    document.add_paragraph('Measured result 42.01 in all 17 participants.')
    document.save(source)
    home = tmp_path / 'home'
    engine = Engine(home, tmp_path / 'models')
    job = engine.prepare({'schema_version': 1,
        'attachment': {'library_id': 1, 'key': 'ABCDEFGH', 'path': str(source)},
        'source_sha256': fingerprint(source), 'annotations': []}, blank_profile({'body': {'size_pt': 12}}))
    result = engine.run(job['run_id'])
    assert result['status'] == 'validated'
    correction = tmp_path / 'user-corrected.docx'
    document = Document(result['editable_docx'])
    document.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
    document.save(correction)
    return engine, job['run_id'], correction, home, source


def test_correction_survives_reload_and_external_file_changes(prepared):
    engine, rid, correction, home, source = prepared
    original = fingerprint(source)
    first = engine.run(rid, corrected_docx=str(correction))
    assert first['status'] == 'validated'
    snapshot = first['manual_document']
    assert Path(snapshot['path']).parent == engine.store.run_dir(rid) / 'corrections'
    correction.write_bytes(b'The editor subsequently changed this external file.')
    loaded = dispatch({'command': 'load', 'run_id': rid}, home)
    assert loaded['manual_document'] == snapshot
    resumed = dispatch({'command': 'review', 'run_id': rid}, home)
    assert resumed['status'] == 'validated'
    assert resumed['candidate_sha256'] == snapshot['sha256']
    assert resumed['manual_document'] == snapshot
    assert Document(resumed['editable_docx']).paragraphs[0].alignment == WD_ALIGN_PARAGRAPH.CENTER
    assert fingerprint(source) == original


def test_explicit_clear_retains_snapshot_and_regenerates(prepared):
    engine, rid, correction, home, _ = prepared
    first = engine.run(rid, corrected_docx=str(correction))
    snapshot = first['manual_document']
    cleared = dispatch({'command': 'review', 'run_id': rid, 'clear_correction': True}, home)
    assert cleared['status'] == 'validated'
    assert 'manual_document' not in cleared
    assert cleared['manual_document_history'][-1]['reason'] == 'cleared'
    assert fingerprint(snapshot['path']) == snapshot['sha256']
    assert Document(cleared['editable_docx']).paragraphs[0].alignment != WD_ALIGN_PARAGRAPH.CENTER
    assert 'manual_document' not in engine.run(rid)


def test_replacement_archives_snapshot_and_failed_choice_preserves_active(prepared):
    engine, rid, correction, _, _ = prepared
    first = engine.run(rid, corrected_docx=str(correction))
    first_snapshot = first['manual_document']
    document = Document(correction)
    document.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT
    document.save(correction)
    second = engine.run(rid, corrected_docx=str(correction))
    assert second['manual_document']['path'] != first_snapshot['path']
    assert second['manual_document_history'][-1]['path'] == first_snapshot['path']
    assert fingerprint(first_snapshot['path']) == first_snapshot['sha256']
    with pytest.raises(FormatterError, match='CORRECTION_FILE_MISSING'):
        engine.run(rid, corrected_docx=str(correction.with_name('missing.docx')))
    failed = engine.store.load(rid)
    assert failed['status'] == 'failed' and failed['error']
    assert failed['manual_document'] == second['manual_document']
    retried = engine.run(rid)
    assert retried['status'] == 'validated' and 'error' not in retried
    assert retried['candidate_sha256'] == second['manual_document']['sha256']


def test_tampered_snapshot_never_falls_back_to_generated_document(prepared):
    engine, rid, correction, _, _ = prepared
    first = engine.run(rid, corrected_docx=str(correction))
    Path(first['manual_document']['path']).write_bytes(b'tampered')
    with pytest.raises(FormatterError, match='CORRECTION_SNAPSHOT_CHANGED'):
        engine.run(rid)
    assert engine.store.load(rid)['status'] == 'failed'
    cleared = engine.run(rid, clear_correction=True)
    assert cleared['status'] == 'validated'
    assert 'manual_document' not in cleared


def test_snapshot_rejects_source_changed_during_copy(prepared, monkeypatch):
    engine, rid, correction, _, source = prepared
    before = fingerprint(source)
    real_copy = module.shutil.copyfileobj
    def racing_copy(reader, writer, *args):
        real_copy(reader, writer, *args)
        if Path(reader.name) == correction:
            correction.write_bytes(b'changed during copy')
    monkeypatch.setattr(module.shutil, 'copyfileobj', racing_copy)
    with pytest.raises(FormatterError, match='CORRECTION_CHANGED_DURING_SNAPSHOT'):
        engine.run(rid, corrected_docx=str(correction))
    failed = engine.store.load(rid)
    assert failed['status'] == 'failed' and 'manual_document' not in failed
    assert fingerprint(source) == before


def test_snapshot_rejects_tamper_between_validation_and_render(prepared, monkeypatch):
    engine, rid, correction, _, _ = prepared
    first = engine.run(rid, corrected_docx=str(correction))
    path = Path(first['manual_document']['path'])
    real_format = module.format_docx
    def racing_format(*args, **kwargs):
        result = real_format(*args, **kwargs)
        path.write_bytes(b'changed after snapshot validation')
        return result
    monkeypatch.setattr(module, 'format_docx', racing_format)
    with pytest.raises(FormatterError, match='CORRECTION_SNAPSHOT_CHANGED'):
        engine.run(rid)


@pytest.mark.parametrize('mismatch', [False, True])
def test_legacy_hash_only_correction_migration(prepared, mismatch):
    engine, rid, correction, _, _ = prepared
    result = engine.run(rid, corrected_docx=str(correction))
    job = engine.store.load(rid)
    job['manual_document'] = {k: job['manual_document'][k] for k in ('sha256', 'recorded_at')}
    engine.store.save(job, 'simulate_legacy_manifest')
    if mismatch:
        Path(job['editable_docx']).write_bytes(b'changed legacy output')
        with pytest.raises(FormatterError, match='CORRECTION_SNAPSHOT_CHANGED'):
            engine.run(rid)
        return
    migrated = engine.run(rid)
    assert migrated['status'] == 'validated'
    assert migrated['manual_document']['migrated_at']
    assert migrated['candidate_sha256'] == result['candidate_sha256']
    assert Path(migrated['manual_document']['path']).is_file()


@pytest.mark.parametrize('change,code', [('content', 'CONTENT_CHANGED'), ('layout', 'FORMAT_RULE_NOT_APPLIED')])
def test_persisted_correction_cannot_bypass_content_or_format_checks(prepared, change, code):
    engine, rid, correction, _, _ = prepared
    document = Document(correction)
    if change == 'content':
        document.paragraphs[0].runs[0].text = 'An invented result.'
    else:
        document.paragraphs[0].runs[0].font.size = Pt(16)
    document.save(correction)
    first = engine.run(rid, corrected_docx=str(correction))
    assert first['status'] == 'needs_review'
    assert code in {i['code'] for i in first['report']['issues']}
    resumed = engine.run(rid)
    assert resumed['status'] == 'needs_review'
    assert code in {i['code'] for i in resumed['report']['issues']}


def test_clear_requires_explicit_boolean_and_cannot_select_at_same_time(prepared):
    engine, rid, correction, _, _ = prepared
    with pytest.raises(FormatterError, match='CLEAR_CORRECTION_BOOLEAN_REQUIRED'):
        engine.run(rid, clear_correction='true')
    with pytest.raises(FormatterError, match='CORRECTION_ACTION_CONFLICT'):
        engine.run(rid, clear_correction=True, corrected_docx=str(correction))
    assert engine.store.load(rid)['status'] == 'validated'

from pathlib import Path

import pytest
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from paper_formatter.contracts import FormatterError
from paper_formatter.docx_io import format_docx, inspect_docx
from paper_formatter.pdf_io import apply_review
from paper_formatter.profiles import blank_profile
from test_store import setup_job


def test_mixed_pdf_never_waives_native_page_coverage():
    extraction = {'blocks': [
        {'id': 'scan', 'page': 0, 'kind': 'text', 'text': 'Scanned cover', 'review_required': True},
        {'id': 'native', 'page': 1, 'kind': 'text', 'text': 'Native paragraph.', 'review_required': False}],
        'issues': [], 'scanned_pages': [0], 'page_count': 2,
        'native_text': ['', 'Native paragraph. Missing result 42.01.']}
    reviews = {'scan': {'confirmed': True}}
    result = apply_review(extraction, reviews)
    assert any(i['code'] == 'PDF_TEXT_COVERAGE' and i['page'] == 1 for i in result['issues'])
    reviews['native'] = {'confirmed': True, 'text': 'Native paragraph. Missing result 42.01.'}
    assert not apply_review(extraction, reviews)['issues']


def test_pdf_coverage_cannot_move_native_content_to_another_page():
    extraction = {'blocks': [
        {'id': 'one', 'page': 0, 'kind': 'text', 'text': 'Page two result.', 'review_required': False},
        {'id': 'two', 'page': 1, 'kind': 'text', 'text': 'Page one result.', 'review_required': False}],
        'issues': [], 'scanned_pages': [], 'page_count': 2,
        'native_text': ['Page one result.', 'Page two result.']}
    assert len([i for i in apply_review(extraction, {})['issues'] if i['code'] == 'PDF_TEXT_COVERAGE']) == 2


def test_empty_scanned_page_cannot_be_confirmed_by_other_blocks():
    extraction = {'blocks': [{'id': 'one', 'page': 1, 'kind': 'text', 'text': 'Native result.', 'review_required': False}],
        'issues': [], 'scanned_pages': [0], 'page_count': 2, 'native_text': ['', 'Native result.']}
    assert any(i['code'] == 'PDF_PAGE_EMPTY' for i in apply_review(extraction, {})['issues'])


def test_page_number_preserves_default_first_even_footer_and_logo(tmp_path):
    from PIL import Image
    source, target = tmp_path / 'source.docx', tmp_path / 'formatted.docx'
    doc = Document()
    doc.add_paragraph('Scientific manuscript data 42.01.')
    section = doc.sections[0]
    section.different_first_page_header_footer = True
    doc.settings.odd_and_even_pages_header_footer = True
    footers = (section.footer, section.first_page_footer, section.even_page_footer)
    for footer, text in zip(footers, ('Confidential data', 'First page attribution', 'Even page notice')):
        footer.paragraphs[0].text = text
    logo = tmp_path / 'logo.png'
    Image.new('RGB', (24, 24), 'blue').save(logo)
    footers[0].paragraphs[0].add_run().add_picture(str(logo))
    doc.save(source)
    report = format_docx(source, target, blank_profile({'footer': {'page_number': True, 'align': 'center'}}))
    assert report['passed'], report['issues']
    result = Document(target).sections[0]
    for footer, text in zip((result.footer, result.first_page_footer, result.even_page_footer),
                            ('Confidential data', 'First page attribution', 'Even page notice')):
        assert text in '\n'.join(p.text for p in footer.paragraphs)
        assert footer._element.xpath('.//w:fldSimple[@w:instr=" PAGE "]')
    assert result.footer._element.xpath('.//w:drawing')


def test_correction_rejects_flattened_ref_with_same_visible_text(tmp_path):
    from paper_formatter.docx_io import preservation_issues
    original, flattened = tmp_path / 'original.docx', tmp_path / 'flattened.docx'
    doc = Document()
    p = doc.add_paragraph('See Figure ')
    for tag, attr, value in [('fldChar', 'fldCharType', 'begin'), ('instrText', None, ' REF FigureOne '),
                             ('fldChar', 'fldCharType', 'separate'), ('t', None, '1'), ('fldChar', 'fldCharType', 'end')]:
        node = OxmlElement('w:' + tag)
        if attr: node.set(qn('w:' + attr), value)
        else: node.text = value
        p.add_run()._r.append(node)
    doc.save(original)
    p.text = 'See Figure 1'
    doc.save(flattened)
    before, after = inspect_docx(original), inspect_docx(flattened)
    assert before['content_hash'] == after['content_hash']
    assert any(i['code'] == 'NATIVE_FIELDS_CHANGED' for i in preservation_issues(before, after))


def test_correction_rejects_changed_citation_item_with_same_display(tmp_path):
    from paper_formatter.docx_io import preservation_issues, read_package, write_package, xml, serialize, NS
    from test_documents import rich_docx
    original = rich_docx(tmp_path / 'original.docx')
    parts = read_package(original)
    root = xml(parts['word/document.xml'])
    node = root.find('.//w:instrText', NS)
    node.text = node.text.replace('fixture-ref', 'wrong-reference')
    parts['word/document.xml'] = serialize(root)
    changed = tmp_path / 'changed.docx'
    write_package(changed, parts)
    assert any(i['code'] == 'NATIVE_FIELDS_CHANGED' for i in preservation_issues(inspect_docx(original), inspect_docx(changed)))


@pytest.mark.skipif(__import__('os').name != 'nt', reason='Windows replacement protocol')
def test_concurrent_edit_after_final_hash_check_is_preserved(tmp_path, monkeypatch):
    store, rid, source, identity, annotations, _ = setup_job(tmp_path)
    real_check = store._hash_check
    checks = 0
    changed = b'%PDF-1.4 concurrent editor saved new data'
    def interpose(path, expected, error):
        nonlocal checks
        real_check(path, expected, error)
        if Path(path) == source and error == 'SOURCE_CHANGED':
            checks += 1
            if checks == 2:
                source.write_bytes(changed)
    monkeypatch.setattr(store, '_hash_check', interpose)
    with pytest.raises(FormatterError, match='SOURCE_CHANGED'):
        store.begin_commit(rid, identity, annotations)
    # The concurrent version must still be available, even if the file swap raced.
    assert source.read_bytes() == changed
    assert any(p.read_bytes() == changed for p in store.run_dir(rid).glob('paper-format-displaced-*'))
    assert store.load(rid)['status'] != 'committed'


def test_crash_after_racing_swap_retains_edit_and_blocks_old_snapshot_rollback(tmp_path, monkeypatch):
    import paper_formatter.store as module
    store, rid, source, identity, annotations, _ = setup_job(tmp_path)
    real_replace = module.replace_with_backup
    changed = b'%PDF-1.4 concurrent version before simulated power loss'
    def crash(destination, replacement, backup):
        destination.write_bytes(changed)
        real_replace(destination, replacement, backup)
        raise RuntimeError('simulated crash after atomic replacement')
    monkeypatch.setattr(module, 'replace_with_backup', crash)
    with pytest.raises(RuntimeError, match='simulated crash'):
        store.begin_commit(rid, identity, annotations)
    pending = store.load(rid)
    assert pending['status'] == 'replacing'
    assert Path(pending['replacements'][0]['displaced']).read_bytes() == changed
    with pytest.raises(FormatterError, match='RECOVERY_CONCURRENT_EDIT'):
        store.begin_rollback(rid, identity, annotations)


def test_disk_copy_failure_never_replaces_source(tmp_path, monkeypatch):
    import paper_formatter.store as module
    store, rid, source, identity, annotations, _ = setup_job(tmp_path)
    def disk_full(reader, writer):
        writer.write(b'partial')
        raise OSError('simulated disk full')
    monkeypatch.setattr(module.shutil, 'copyfileobj', disk_full)
    with pytest.raises(OSError, match='disk full'):
        store.begin_commit(rid, identity, annotations)
    assert source.read_bytes().endswith(b'original fixture')
    assert store.load(rid)['status'] == 'validated'


def test_active_conversion_prevents_a_second_process_from_committing(tmp_path):
    from paper_formatter.store import file_lock
    store, rid, source, identity, annotations, _ = setup_job(tmp_path)
    with file_lock(store.run_dir(rid) / 'conversion.lock'):
        with pytest.raises(FormatterError, match='ATTACHMENT_BUSY'):
            store.begin_commit(rid, identity, annotations)
    assert source.read_bytes().endswith(b'original fixture')


def test_partial_windows_replace_failure_can_restore_missing_destination(tmp_path, monkeypatch):
    import os
    import paper_formatter.store as module
    store, rid, source, identity, annotations, _ = setup_job(tmp_path)
    def partial_failure(destination, replacement, backup):
        os.rename(destination, backup)
        raise OSError(1177, 'simulated ERROR_UNABLE_TO_MOVE_REPLACEMENT_2')
    monkeypatch.setattr(module, 'replace_with_backup', partial_failure)
    with pytest.raises(OSError, match='REPLACEMENT_2'):
        store.begin_commit(rid, identity, annotations)
    assert not source.exists()
    recovered = store.begin_rollback(rid, identity, annotations)
    assert recovered['status'] == 'awaiting_rollback_ack'
    assert source.read_bytes().endswith(b'original fixture')
    assert store.finish_rollback(rid, identity, annotations)['status'] == 'rolled_back'

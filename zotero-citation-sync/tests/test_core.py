import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from citation_sync.docx_read import locate, read_docx
from citation_sync.requests import build_tasks, extract, normalize_doi, sources
from citation_sync.service import Service
from citation_sync.util import SyncError, file_hash, read_json
from citation_sync.word_review import ATTESTATIONS, verify_copy
from fixtures import FakeAPI, annotation, docx, field, paper, run, snapshot


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = docx(self.root / 'original.docx')
    def tearDown(self):
        self.temp.cleanup()
    def tasks(self, data=None, path=None):
        return build_tasks(data or snapshot(), read_docx(path or self.source))[0]

    def test_owner_a_is_separate_from_requested_b(self):
        task = self.tasks()[0]
        self.assertEqual(task['source']['owner']['key'], 'PAPERAAA')
        self.assertEqual(task['selected']['key'], 'PAPERBBB')
        self.assertEqual(task['source']['page'], '3')
        self.assertEqual(task['status'], 'needs_confirmation')

    def test_doi_normalization_keeps_balanced_parentheses(self):
        self.assertEqual(normalize_doi('https://doi.org/10.5555/ABC(1).'), '10.5555/abc(1)')
        self.assertEqual(normalize_doi('(10.5555/abc).'), '10.5555/abc')

    def test_doi_in_highlight_is_not_requested_identity(self):
        data = snapshot(comment='[cite]\nTarget: A target statement.')
        data['items'][-1]['data']['annotationText'] = '10.5555/synthetic-a'
        task = self.tasks(data)[0]
        self.assertIsNone(task['selected'])
        self.assertIn('missing_identity', task['reasons'])

    def test_duplicate_doi_requires_explicit_resolution(self):
        task = self.tasks(snapshot([paper('DUPLBBBB')]))[0]
        self.assertIsNone(task['selected'])
        self.assertEqual(len(task['candidates']), 2)
        self.assertIn('duplicate_identity', task['reasons'])

    def test_doi_not_in_library_is_not_fabricated(self):
        task = self.tasks(snapshot(comment='[cite]\nDOI: 10.5555/missing\nTarget: A target statement.'))[0]
        self.assertIsNone(task['selected'])
        self.assertIn('identity_not_found', task['reasons'])

    def test_title_is_only_candidate_even_when_exact(self):
        task = self.tasks(snapshot(comment='[cite]\nTitle: Synthetic paper B\nTarget: A target statement.'))[0]
        self.assertIsNone(task['selected'])
        self.assertIn('title_needs_confirmation', task['reasons'])

    def test_missing_target_never_selects_a_random_paragraph(self):
        task = self.tasks(snapshot(comment='新增引用 DOI: 10.5555/synthetic-b'))[0]
        self.assertIsNone(task['target'])

    def test_repeated_quote_is_ambiguous(self):
        docx(self.source, [run('A target statement.'), run('A target statement.')])
        self.assertIn('target_ambiguous', self.tasks()[0]['reasons'])

    def test_two_dois_are_not_silently_reduced_to_first(self):
        task = self.tasks(snapshot(comment='[cite] 10.5555/synthetic-b 10.5555/synthetic-a\nTarget: A target statement.'))[0]
        self.assertIsNone(task['selected'])
        self.assertIn('multiple_requested_identities', task['reasons'])

    def test_pmids_and_arxiv_need_exact_metadata(self):
        for label, value in [('PMID', '123456'), ('arXiv', '2401.12345v2')]:
            data = snapshot(comment=f'[cite]\n{label}: {value}\nTarget: A target statement.')
            data['items'][1]['data']['extra'] = f'{label}: {value}'
            self.assertEqual(self.tasks(data)[0]['selected']['key'], 'PAPERBBB')

    def test_note_html_and_negated_requests(self):
        data = snapshot(comment='普通评论')
        data['items'].append({'key': 'NOTESAAA', 'data': {'itemType': 'note', 'parentItem': 'PAPERAAA',
                             'note': '<p>[cite]</p><p>DOI: 10.5555/synthetic-b</p><p>Target: A target statement.</p>'}})
        self.assertEqual(len(self.tasks(data)), 1)
        data['items'][-1]['data']['note'] = '<p>[cite] do not cite 10.5555/synthetic-b</p>'
        self.assertFalse(self.tasks(data))

    def test_exact_existing_citation_is_not_reinserted(self):
        docx(self.source, [run('A target statement.') + field()])
        self.assertEqual(self.tasks()[0]['status'], 'already_cited')

    def test_same_paper_in_another_paragraph_is_new_position(self):
        docx(self.source, [run('Another paragraph.') + field(), run('A target statement.')])
        self.assertEqual(self.tasks()[0]['status'], 'needs_confirmation')

    def test_target_with_tracked_changes_is_blocked(self):
        docx(self.source, ['<w:ins w:id="1">' + run('A target statement.') + '</w:ins>'])
        self.assertIn('target_contains_tracked_changes', self.tasks()[0]['reasons'])

    def test_broken_field_is_not_dynamic(self):
        docx(self.source, [run('A target statement.') + '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'])
        inventory = read_docx(self.source)
        self.assertIn('FIELD_UNCLOSED', inventory['issues'])
        self.assertFalse(inventory['word_dynamic_verified'])

    def test_plain_text_number_is_not_a_zotero_citation(self):
        candidate = docx(self.root / 'plain.docx', [run('A target statement.[1]')], bib=True)
        result = verify_copy(self.source, candidate, self.tasks())
        self.assertFalse(result['structural_checks_passed'])
        self.assertIn('INSERTED_CITATIONS_DO_NOT_MATCH_APPROVED_TARGETS', result['issues'])

    def test_synthetic_structural_pass_does_not_prove_word_compatibility(self):
        candidate = docx(self.root / 'structural.docx', [run('A target statement.') + field()], bib=True)
        result = verify_copy(self.source, candidate, self.tasks())
        self.assertTrue(result['structural_checks_passed'], result)
        self.assertEqual(result['official_word_compatibility'], 'not_proven_by_xml')

    def test_body_style_and_assets_are_protected(self):
        parts = {'word/media/image1.png': b'fixture-image', 'word/comments.xml': b'<comments/>'}
        docx(self.source, extra_parts=parts)
        for change in ('body', 'style', 'asset', 'comment'):
            para = run('Changed target statement.' if change == 'body' else 'A target statement.') + field()
            if change == 'style':
                para = '<w:pPr><w:spacing w:after="120"/></w:pPr>' + para
            changed_parts = parts.copy()
            if change == 'asset': changed_parts['word/media/image1.png'] = b'new-image'
            if change == 'comment': changed_parts['word/comments.xml'] = b'<comments>changed</comments>'
            candidate = docx(self.root / (change + '.docx'), [para], bib=True, extra_parts=changed_parts)
            self.assertFalse(verify_copy(self.source, candidate, self.tasks())['structural_checks_passed'], change)

    def test_equation_and_table_geometry_are_protected(self):
        original = '<m:oMath><m:r><m:t>x=1</m:t></m:r></m:oMath>'
        docx(self.source, [run('A target statement.'), original])
        candidate = docx(self.root / 'math.docx', [run('A target statement.') + field(), original.replace('x=1', 'x=2')], bib=True)
        self.assertIn('EQUATION_TABLE_OR_DRAWING_CHANGED', verify_copy(self.source, candidate, self.tasks())['issues'])

    def test_existing_citation_removal_and_locator_change_are_blocked(self):
        docx(self.source, [run('A target statement.'), run('Old statement.') + field('PAPERAAA', 'old', options={'locator': '4'})])
        tasks = self.tasks()
        for old in ('', field('PAPERAAA', 'old', options={'locator': '5'})):
            candidate = docx(self.root / 'removed.docx', [run('A target statement.') + field(), run('Old statement.') + old], bib=True)
            self.assertFalse(verify_copy(self.source, candidate, tasks)['structural_checks_passed'])

    def test_insert_at_beginning_allows_number_display_to_refresh(self):
        docx(self.source, [run('A target statement.'), run('Old statement.') + field('PAPERAAA', 'old', '[1]')], bib=True)
        candidate = docx(self.root / 'renumber.docx', [run('A target statement.') + field(), run('Old statement.') + field('PAPERAAA', 'old', '[2]')], bib=True)
        self.assertTrue(verify_copy(self.source, candidate, self.tasks())['structural_checks_passed'])

    def test_unapproved_citation_and_style_mismatch_are_blocked(self):
        candidate = docx(self.root / 'wrong.docx', [run('A target statement.') + field('PAPERAAA')], bib=True, style='ieee')
        result = verify_copy(self.source, candidate, self.tasks(), 'apa')
        self.assertIn('INSERTED_CITATIONS_DO_NOT_MATCH_APPROVED_TARGETS', result['issues'])
        self.assertIn('CSL_STYLE_MISMATCH', result['issues'])

    def service(self):
        return Service(self.root / 'state', FakeAPI())
    def approved(self, scan):
        return [{'id': scan['tasks'][0]['id'], 'identity': True, 'target': True, 'claim': True}]

    def test_backup_preserves_bytes_and_double_click_is_blocked(self):
        service = self.service()
        scan = service.scan(self.source)
        session = service.prepare(scan['id'], self.approved(scan), saved_in_word=True)
        self.assertEqual(file_hash(self.source), file_hash(session['backup_path']))
        self.assertEqual(file_hash(self.source), file_hash(session['working_path']))
        with self.assertRaisesRegex(SyncError, '已有工作副本'):
            service.prepare(scan['id'], self.approved(scan), saved_in_word=True)

    def test_original_and_library_changes_stop_preparation(self):
        service = self.service()
        scan = service.scan(self.source)
        docx(self.source, [run('Updated manuscript.')])
        with self.assertRaises(SyncError):
            service.prepare(scan['id'], self.approved(scan), saved_in_word=True)
        service.api.data['version'] = '2'
        with self.assertRaises(SyncError):
            service.prepare(scan['id'], self.approved(scan), saved_in_word=True)

    def test_restore_creates_exact_new_copy_and_keeps_edited_copy(self):
        service = self.service()
        scan = service.scan(self.source)
        session = service.prepare(scan['id'], self.approved(scan), saved_in_word=True)
        docx(Path(session['working_path']), [run('User changed candidate.')])
        edited_hash = file_hash(session['working_path'])
        restored = service.restore(session['id'])
        self.assertEqual(file_hash(restored['restored_copies'][-1]), file_hash(self.source))
        self.assertEqual(file_hash(session['working_path']), edited_hash)

    def test_no_attestation_or_no_mac_does_not_mark_synced(self):
        service = self.service()
        scan = service.scan(self.source)
        session = service.prepare(scan['id'], self.approved(scan), saved_in_word=True)
        docx(Path(session['working_path']), [run('A target statement.') + field()], bib=True)
        with patch('citation_sync.service.supported_mac', return_value=False):
            result = service.verify(session['id'], {k: True for k in ATTESTATIONS})
        self.assertEqual(result['status'], 'awaiting_mac_official_confirmation')

    def test_repeat_sync_ledger_and_actual_field_prevent_duplicates(self):
        service = self.service()
        scan = service.scan(self.source)
        session = service.prepare(scan['id'], self.approved(scan), saved_in_word=True)
        docx(Path(session['working_path']), [run('A target statement.') + field()], bib=True)
        # Mocked state-machine test only; not evidence of real Mac Word behavior.
        with patch('citation_sync.service.supported_mac', return_value=True):
            service.verify(session['id'], {k: True for k in ATTESTATIONS})
        second = service.scan(session['working_path'])
        self.assertEqual(second['tasks'][0]['status'], 'already_cited')
        original_again = service.scan(self.source)
        self.assertIn('previous_sync_output_exists', original_again['tasks'][0]['reasons'])
        with self.assertRaises(SyncError):
            service.prepare(original_again['id'], self.approved(original_again), saved_in_word=True)

    def test_deleted_request_persists_conflict_without_document_changes(self):
        service = self.service()
        original_hash = file_hash(self.source)
        service.scan(self.source)
        service.api.data['items'] = [i for i in service.api.data['items'] if i['key'] != 'ANNOTAAA']
        second = service.scan(self.source)
        third = service.scan(self.source)
        self.assertEqual(second['conflicts'][0]['change'], 'deleted')
        self.assertEqual(len(third['conflicts']), 1)
        self.assertEqual(file_hash(self.source), original_hash)

    def test_corrupt_state_and_concurrent_lock_do_not_reset_history(self):
        service = self.service()
        service.store.path.write_text('invalid-json')
        with self.assertRaises(SyncError): service.history()
        self.assertEqual(service.store.path.read_text(), 'invalid-json')
        service.store.path.unlink()
        (service.store.home / 'state.lock').write_text('12345')
        with self.assertRaises(SyncError): service.history()

    def test_resolution_cannot_override_explicit_doi(self):
        service = self.service()
        scan = service.scan(self.source)
        resolved = service.resolve(scan['id'], scan['tasks'][0]['id'], 'users/123/items/PAPERAAA', 'A target statement.', 0)
        self.assertIn('explicit_identifier_conflict', resolved['tasks'][0]['reasons'])

    def test_changed_annotation_invalidates_manual_resolution(self):
        service = self.service()
        scan = service.scan(self.source)
        service.resolve(scan['id'], scan['tasks'][0]['id'], 'users/123/items/PAPERBBB', 'A target statement.', 0)
        service.api.data['items'][-1]['data']['annotationComment'] = '[cite]\nDOI: 10.5555/missing'
        result = service.scan(self.source)
        self.assertIsNone(result['tasks'][0]['selected'])


if __name__ == '__main__': unittest.main()

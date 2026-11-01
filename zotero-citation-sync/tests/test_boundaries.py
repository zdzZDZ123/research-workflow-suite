"""Portable regression tests. No real Word or Zotero library writes occur here."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from citation_sync.docx_read import locate, read_docx
from citation_sync.local_api import LocalAPI
from citation_sync.requests import build_tasks
from citation_sync.server import AppServer
from citation_sync.service import Service
from citation_sync.util import SyncError, file_hash
from citation_sync.word_review import ATTESTATIONS, verify_copy
from fixtures import FakeAPI, annotation, docx, field, paper, run, snapshot


@contextmanager
def running(server):
    worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


class LocalApiTests(unittest.TestCase):
    def server(self, mode='normal'):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                parsed = urllib.parse.urlsplit(self.path)
                query = urllib.parse.parse_qs(parsed.query)
                start = int(query.get('start', ['0'])[0])
                self.server.seen.append((self.command, self.path, self.headers.get('Zotero-API-Version')))
                if mode == 'redirect':
                    self.send_response(302)
                    self.send_header('Location', 'http://example.invalid/leak')
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header('X-Zotero-Version', '10.0.6-fixture')
                self.send_header('Zotero-API-Version', '3')
                if mode != 'missing_id':
                    self.send_header('Zotero-Server-ID', 'OTHER' if mode == 'instance' and start else 'LOCAL-TEST')
                self.send_header('Last-Modified-Version', '2' if mode == 'drift' and start else '1')
                self.send_header('Total-Results', '101')
                body = b'Nothing to see here.'
                if parsed.path != '/api/':
                    count = 100 if start == 0 else 1
                    if mode == 'short': count = 1
                    items = [paper(f'K{i:07d}') for i in range(start, start + count)]
                    if mode == 'duplicate' and start: items[0]['key'] = 'K0000000'
                    body = json.dumps(items).encode()
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = HTTPServer(('127.0.0.1', 0), Handler)
        server.seen = []
        return server

    def test_plain_root_full_pagination_and_get_only(self):
        with running(self.server()) as server:
            api = LocalAPI(f'http://127.0.0.1:{server.server_port}')
            result = api.snapshot()
            self.assertEqual(len(result['items']), 101)
            self.assertTrue(result['complete'])
            self.assertTrue(all(method == 'GET' and version == '3' for method, _, version in server.seen))
            self.assertTrue(any('start=100' in url for _, url, _ in server.seen))

    def test_unsafe_origins_and_library_paths_refused(self):
        for base in ('https://example.com:443', 'http://localhost:23119', 'http://user@127.0.0.1:23119',
                     'http://127.0.0.1:23119/api', 'http://127.0.0.1:23119/?q=x'):
            with self.assertRaises(SyncError): LocalAPI(base)
        with self.assertRaises(SyncError): LocalAPI(library='users/0/../../items')

    def test_inconsistent_snapshots_and_redirects_fail_closed(self):
        expected = {'drift': 'LIBRARY_CHANGED_DURING_SCAN', 'instance': 'ZOTERO_INSTANCE_CHANGED',
                    'short': 'SNAPSHOT_INCOMPLETE', 'duplicate': 'SNAPSHOT_INVALID',
                    'missing_id': 'SERVER_ID_REQUIRED', 'redirect': 'LOCAL_API_REDIRECT'}
        for mode, code in expected.items():
            with self.subTest(mode=mode), running(self.server(mode)) as server:
                with self.assertRaises(SyncError) as caught:
                    LocalAPI(f'http://127.0.0.1:{server.server_port}').snapshot()
                self.assertEqual(caught.exception.code, code)


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = docx(self.root / 'source.docx')
        self.service = Service(self.root / 'state', FakeAPI())

    def prepare(self):
        scan = self.service.scan(self.source)
        approvals = [{'id': scan['tasks'][0]['id'], 'identity': True, 'target': True, 'claim': True}]
        return self.service.prepare(scan['id'], approvals, saved_in_word=True)

    def test_two_fields_at_the_same_approved_position_are_not_one_insertion(self):
        tasks = build_tasks(snapshot(), read_docx(self.source))[0]
        candidate = docx(self.root / 'two.docx', [run('A target statement.') + field() + field(cid='duplicate')], bib=True)
        self.assertIn('INSERTED_CITATIONS_DO_NOT_MATCH_APPROVED_TARGETS', verify_copy(self.source, candidate, tasks)['issues'])

    def test_same_paper_at_two_distinct_positions_is_allowed_structurally(self):
        docx(self.source, [run('A target statement.'), run('A different statement.')])
        data = snapshot([annotation('[cite]\nDOI: 10.5555/synthetic-b\nTarget: A different statement.', 'ANNOTBBB')])
        tasks = build_tasks(data, read_docx(self.source))[0]
        candidate = docx(self.root / 'two.docx', [run('A target statement.') + field(),
                        run('A different statement.') + field(cid='second')], bib=True)
        self.assertTrue(verify_copy(self.source, candidate, tasks)['structural_checks_passed'])

    def test_verified_file_mutation_is_not_presented_as_currently_valid(self):
        session = self.prepare()
        candidate = Path(session['working_path'])
        docx(candidate, [run('A target statement.') + field()], bib=True)
        with patch('citation_sync.service.supported_mac', return_value=True):
            self.service.verify(session['id'], {k: True for k in ATTESTATIONS})
        docx(candidate, [run('Changed later.')])
        with self.assertRaises(SyncError) as caught: self.service.verify(session['id'], {})
        self.assertEqual(caught.exception.code, 'VERIFIED_COPY_CHANGED')
        self.assertEqual(self.service.history()[0]['status'], 'verified_copy_changed')

    def test_deletion_is_detected_independently_for_two_manuscripts(self):
        other = docx(self.root / 'other.docx')
        self.service.scan(self.source)
        self.service.scan(other)
        self.service.api.data['items'] = [i for i in self.service.api.data['items'] if i['key'] != 'ANNOTAAA']
        self.assertEqual(len(self.service.scan(other)['conflicts']), 1)
        self.assertEqual(len(self.service.scan(self.source)['conflicts']), 1)

    def test_section_does_not_extend_past_next_heading(self):
        heading = '<w:pPr><w:outlineLvl w:val="0"/></w:pPr>'
        docx(self.source, [heading + run('Methods'), run('Not the target.'), heading + run('Results'), run('A target statement.')])
        self.assertEqual(locate(read_docx(self.source), 'A target statement.', 'Methods'), [])
        self.assertEqual(len(locate(read_docx(self.source), 'A target statement.', 'Results')), 1)

    def test_named_style_and_hyperlink_relationship_changes_are_detected(self):
        parts = {'word/styles.xml': '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:style w:styleId="Normal"><w:rPr><w:sz w:val="24"/></w:rPr></w:style></w:styles>',
                 'word/_rels/document.xml.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="hyperlink" Target="https://example.invalid/a" TargetMode="External"/></Relationships>'}
        docx(self.source, extra_parts=parts)
        tasks = build_tasks(snapshot(), read_docx(self.source))[0]
        for name in parts:
            changes = {**parts, name: parts[name].replace('24', '30').replace('invalid/a', 'invalid/b')}
            candidate = docx(self.root / 'style.docx', [run('A target statement.') + field()], bib=True, extra_parts=changes)
            self.assertIn('STYLE_RELATIONSHIP_OR_DOCUMENT_SETTING_CHANGED', verify_copy(self.source, candidate, tasks)['issues'])

    def test_revision_identity_and_table_cell_merge_are_preserved(self):
        for original, changed in [('<w:ins w:id="1" w:author="A">' + run('Revision') + '</w:ins>',
                                   '<w:ins w:id="1" w:author="B">' + run('Revision') + '</w:ins>'),
                                  ('<w:tcPr><w:gridSpan w:val="2"/></w:tcPr>', '<w:tcPr><w:gridSpan w:val="3"/></w:tcPr>')]:
            docx(self.source, [run('A target statement.'), original])
            candidate = docx(self.root / 'changed.docx', [run('A target statement.') + field(), changed], bib=True)
            tasks = build_tasks(snapshot(), read_docx(self.source))[0]
            self.assertIn('EQUATION_TABLE_OR_DRAWING_CHANGED', verify_copy(self.source, candidate, tasks)['issues'])

    def test_comment_anchor_cannot_move_to_another_paragraph(self):
        anchor = '<w:commentRangeStart w:id="1"/>'
        docx(self.source, [run('A target statement.'), anchor + run('Second paragraph.')])
        candidate = docx(self.root / 'moved.docx', [anchor + run('A target statement.') + field(), run('Second paragraph.')], bib=True)
        tasks = build_tasks(snapshot(), read_docx(self.source))[0]
        self.assertIn('EQUATION_TABLE_OR_DRAWING_CHANGED', verify_copy(self.source, candidate, tasks)['issues'])

    def test_cross_reference_instructions_are_not_lost_if_display_is_same(self):
        ref = '<w:fldSimple w:instr="REF figure1">' + run('Figure 1') + '</w:fldSimple>'
        docx(self.source, [run('A target statement.'), ref])
        candidate = docx(self.root / 'ref.docx', [run('A target statement.') + field(), ref.replace('figure1', 'figure2')], bib=True)
        tasks = build_tasks(snapshot(), read_docx(self.source))[0]
        self.assertIn('OTHER_WORD_FIELD_CHANGED', verify_copy(self.source, candidate, tasks)['issues'])

    def test_word_cursor_bookmark_is_not_manuscript_content(self):
        tasks = build_tasks(snapshot(), read_docx(self.source))[0]
        cursor = '<w:bookmarkStart w:id="1" w:name="_GoBack"/><w:bookmarkEnd w:id="1"/>'
        candidate = docx(self.root / 'cursor.docx', [run('A target statement.') + field() + cursor], bib=True)
        self.assertTrue(verify_copy(self.source, candidate, tasks)['structural_checks_passed'])

    def test_missing_user_identity_is_not_guessed_as_user_zero(self):
        data = snapshot()
        data['items'][1]['library']['id'] = 0
        task = build_tasks(data, read_docx(self.source))[0][0]
        self.assertIn('library_identity_unresolved', task['reasons'])

    def test_disk_failure_does_not_modify_source_or_commit_session(self):
        original = file_hash(self.source)
        scan = self.service.scan(self.source)
        with patch('citation_sync.service.copy_exact', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.service.prepare(scan['id'], [{'id': scan['tasks'][0]['id'], 'identity': True, 'target': True, 'claim': True}], saved_in_word=True)
        self.assertEqual(file_hash(self.source), original)
        self.assertEqual(self.service.history(), [])

    def test_backup_tampering_blocks_restoration(self):
        session = self.prepare()
        Path(session['backup_path']).write_bytes(b'changed')
        with self.assertRaises(SyncError) as caught: self.service.restore(session['id'])
        self.assertEqual(caught.exception.code, 'BACKUP_CHANGED')

    def test_interrupted_session_journal_is_not_accepted_as_completed(self):
        session = self.prepare()
        path = Path(session['backup_path']).parent / 'session.json'
        session['status'] = 'verified_by_user'
        path.write_text(json.dumps(session), encoding='utf-8')
        with self.assertRaises(SyncError) as caught: self.service.history()
        self.assertEqual(caught.exception.code, 'SESSION_JOURNAL_INCOMPLETE')

    def test_restoring_old_session_does_not_unlock_a_new_active_session(self):
        old = self.prepare()
        self.service.restore(old['id'])
        new = self.prepare()
        self.service.restore(old['id'])
        with self.assertRaises(SyncError) as caught: self.prepare()
        self.assertEqual(caught.exception.code, 'ACTIVE_SESSION_EXISTS')

    def test_http_token_origin_host_and_path_access(self):
        with running(AppServer(self.root / 'server', service=self.service)) as server:
            def request(route, token=True, origin=None, host=None, data=None):
                headers = {'Content-Type': 'application/json'}
                if token: headers['X-Citation-Sync-Token'] = server.token
                if origin: headers['Origin'] = origin
                if host: headers['Host'] = host
                req = urllib.request.Request(server.origin + route, headers=headers,
                      data=json.dumps(data).encode() if data is not None else None)
                try:
                    with urllib.request.urlopen(req) as response: return response.status, response.read(), response.headers
                except urllib.error.HTTPError as exc: return exc.code, exc.read(), exc.headers
            self.assertEqual(request('/api/status', token=False)[0], 403)
            self.assertEqual(request('/api/scan', origin='https://example.invalid', data={'path': str(self.source)})[0], 403)
            self.assertEqual(request('/', host='evil.example:80')[0], 403)
            self.assertEqual(request('/../state/state.json')[0], 404)
            status, body, headers = request('/')
            self.assertEqual(status, 200)
            self.assertIn(b'Citation Sync', body)
            self.assertIn("connect-src 'self'", headers['Content-Security-Policy'])
            self.assertEqual(request('/api/connect', data={})[0], 200)
            self.assertEqual(request('/api/scan', data={'path': str(self.source)})[0], 200)
            self.assertNotIn('A target statement.', (self.service.store.home / 'operations.jsonl').read_text())


if __name__ == '__main__': unittest.main()

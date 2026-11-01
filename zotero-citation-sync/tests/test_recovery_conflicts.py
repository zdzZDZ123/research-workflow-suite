import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from citation_sync.service import Service
from citation_sync.store import Store, process_alive
from citation_sync.util import SyncError, digest, file_hash, read_json, write_json
from citation_sync.word_review import ATTESTATIONS
from fixtures import FakeAPI, docx, field, run
from citation_sync.server import AppServer
from test_boundaries import running


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root)

    def interrupted(self):
        original = self.store._write_target
        calls = 0
        def failing(path, value):
            nonlocal calls
            calls += 1
            if calls == 2: raise OSError('simulated disk failure')
            original(path, value)
        with patch.object(self.store, '_write_target', side_effect=failing):
            with self.assertRaises(OSError):
                with self.store.transaction() as state:
                    self.store.stage_json(self.root / 'scans' / ('a' * 32 + '.json'), {'safe': True})
                    state['documents']['proof'] = {'recovered': True}
                    self.store.log('fixture_commit')

    def test_partial_commit_replays_exactly_and_keeps_receipt(self):
        self.interrupted()
        report = self.store.inspect_recovery()
        self.assertTrue(report['can_recover'])
        self.assertEqual([w['applied'] for w in report['writes']], [True, False])
        with self.assertRaises(SyncError) as caught:
            with self.store.transaction(): pass
        self.assertEqual(caught.exception.code, 'RECOVERY_REQUIRED')
        self.assertFalse(self.store.recover(report['plan_id'])['required'])
        with self.store.transaction() as state:
            self.assertTrue(state['documents']['proof']['recovered'])
        receipts = list((self.root / 'journal/receipts').glob('*.json'))
        self.assertEqual(len(receipts), 1)
        self.assertNotIn('value', read_json(receipts[0])['writes'][0])

    def test_changed_metadata_after_interruption_blocks_replay(self):
        self.interrupted()
        report = self.store.inspect_recovery()
        path = self.root / 'scans' / ('a' * 32 + '.json')
        path.write_text('{"user_change":true}', encoding='utf-8')
        expected = file_hash(path)
        self.assertIn('RECOVERY_TARGET_CHANGED', self.store.inspect_recovery()['blockers'])
        with self.assertRaises(SyncError): self.store.recover(report['plan_id'])
        self.assertEqual(file_hash(path), expected)
        self.assertTrue(self.store.pending.exists())

    def test_actual_process_exit_releases_os_lock_and_recovers_pid_marker(self):
        program = '''
import os, sys
from citation_sync.store import Store
s=Store(sys.argv[1])
original=s._write_target
def crash(path, value):
    original(path,value)
    os._exit(23)
s._write_target=crash
with s.transaction() as state:
    s.stage_json(s.home/'scans'/('b'*32+'.json'), {'fixture':True})
    state['documents']['proof']={'recovered':True}
'''
        result = subprocess.run([sys.executable, '-c', program, str(self.root)], capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertTrue((self.root / 'state.lock').exists())
        report = self.store.inspect_recovery()
        self.assertIs(report['lock']['owner_alive'], False)
        self.assertTrue(report['can_recover'], report)
        self.store.recover(report['plan_id'])
        self.assertFalse((self.root / 'state.lock').exists())
        self.assertEqual(len(list((self.root / 'journal/abandoned-locks').glob('*.txt'))), 1)
        self.assertTrue(read_json(self.store.path)['documents']['proof']['recovered'])

    def test_running_or_unknown_owner_is_never_unlocked(self):
        self.assertIs(process_alive(os.getpid()), True)
        for content in (str(os.getpid()), 'not-a-pid', '0'):
            (self.root / 'state.lock').write_text(content)
            report = self.store.inspect_recovery()
            self.assertFalse(report['can_recover'])
            with self.assertRaises(SyncError): self.store.recover(report['plan_id'])
            self.assertEqual((self.root / 'state.lock').read_text(), content)

    def test_legacy_dead_owner_marker_can_be_archived_without_a_journal(self):
        result = subprocess.Popen([sys.executable, '-c', 'pass'])
        result.wait(timeout=10)
        (self.root / 'state.lock').write_text(str(result.pid))
        report = self.store.inspect_recovery()
        self.assertTrue(report['can_recover'])
        self.assertFalse(self.store.recover(report['plan_id'])['required'])

    def test_concurrent_store_cannot_enter_or_recover(self):
        other = Store(self.root)
        with self.store.transaction():
            self.assertIn('OPERATION_IN_PROGRESS', other.inspect_recovery()['blockers'])
            with self.assertRaises(SyncError):
                with other.transaction(): pass

    def test_journal_cannot_write_outside_application_metadata(self):
        value = {'bad': True}
        for path in ('../outside.json', 'working.docx', 'sessions/not-an-id/session.json'):
            write_json(self.store.pending, {'schema': 1, 'id': 'b' * 32, 'events': [], 'writes': [
                {'path': path, 'value': value, 'before': None, 'after': digest(json.dumps(value, ensure_ascii=False, indent=2).encode())}]})
            self.assertIn('JOURNAL_TARGET_INVALID', self.store.inspect_recovery()['blockers'])
        self.assertFalse((self.root.parent / 'outside.json').exists())

    def test_corrupt_journal_and_state_are_not_reset(self):
        self.store.pending.parent.mkdir(parents=True)
        self.store.pending.write_text('not json')
        self.assertIn('STATE_UNREADABLE', self.store.inspect_recovery()['blockers'])
        self.assertEqual(self.store.pending.read_text(), 'not json')

    def test_unregistered_copy_directory_is_preserved_and_reported(self):
        orphan = self.root / 'sessions' / ('f' * 32)
        orphan.mkdir(parents=True)
        (orphan / 'original.docx').write_bytes(b'partial-test-fixture')
        result = self.store.inspect_recovery()
        self.assertEqual(result['preserved_unregistered_directories'], ['f' * 32])
        self.assertFalse(result['can_recover'])
        self.assertEqual((orphan / 'original.docx').read_bytes(), b'partial-test-fixture')

    def test_read_only_transactions_do_not_create_commit_receipts(self):
        with self.store.transaction(): pass
        self.assertFalse((self.root / 'journal/receipts').exists())

    def test_after_all_writes_recovery_does_not_duplicate_transaction_receipt(self):
        with patch.object(self.store, '_append_log', side_effect=OSError('audit disk full')):
            with self.assertRaises(OSError):
                with self.store.transaction() as state: state['documents']['test'] = {}
        report = self.store.inspect_recovery()
        self.assertTrue(all(w['applied'] for w in report['writes']))
        self.store.recover(report['plan_id'])
        self.assertEqual(len(list((self.root / 'journal/receipts').glob('*.json'))), 1)

    def test_cli_requires_current_plan_and_can_recover_without_browser(self):
        self.interrupted()
        command = [sys.executable, '-m', 'citation_sync', '--home', str(self.root), 'recover']
        preview = subprocess.run(command, capture_output=True, text=True, timeout=20)
        self.assertEqual(preview.returncode, 0, preview.stderr)
        report = json.loads(preview.stdout)
        self.assertTrue(self.store.pending.exists())
        wrong = subprocess.run(command + ['--apply-plan', 'old-plan'], capture_output=True, text=True, timeout=20)
        self.assertNotEqual(wrong.returncode, 0)
        result = subprocess.run(command + ['--apply-plan', report['plan_id']], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)['required'])

    def test_http_recovery_status_and_apply_keep_token_boundary(self):
        self.interrupted()
        service = Service(self.root, FakeAPI())
        with running(AppServer(self.root, service=service)) as server:
            def request(path, body=None, token=True):
                headers = {'Content-Type': 'application/json'}
                if token: headers['X-Citation-Sync-Token'] = server.token
                req = urllib.request.Request(server.origin + path, headers=headers,
                     data=json.dumps(body).encode() if body is not None else None)
                with urllib.request.urlopen(req) as result: return json.load(result)
            status = request('/api/status')
            self.assertTrue(status['recovery']['required'])
            self.assertEqual(status['history'], [])
            report = request('/api/recovery/status')
            with self.assertRaises(urllib.error.HTTPError) as caught:
                request('/api/recovery/apply', {'plan_id': report['plan_id']}, token=False)
            self.assertEqual(caught.exception.code, 403)
            self.assertFalse(request('/api/recovery/apply', {'plan_id': report['plan_id']})['required'])


class ConflictTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = docx(self.root / 'source.docx', [run('A target statement.'), run('Second target statement.')])
        self.service = Service(self.root / 'state', FakeAPI())

    def approved(self, scan):
        return [{'id': scan['tasks'][0]['id'], 'identity': True, 'target': True, 'claim': True}]

    def modified_completed(self):
        scan = self.service.scan(self.source)
        session = self.service.prepare(scan['id'], self.approved(scan), saved_in_word=True)
        path = Path(session['working_path'])
        docx(path, [run('A target statement.') + field(), run('Second target statement.')], bib=True)
        with patch('citation_sync.service.supported_mac', return_value=True):
            self.service.verify(session['id'], {k: True for k in ATTESTATIONS})
        self.service.api.data['items'][3]['data']['annotationComment'] = '[cite]\nDOI: 10.5555/synthetic-b\nTarget: Second target statement.'
        return path, self.service.scan(path)

    def test_manual_target_resolution_cannot_bypass_modified_request_conflict(self):
        _, scan = self.modified_completed()
        result = self.service.resolve(scan['id'], scan['tasks'][0]['id'], 'users/123/items/PAPERBBB', 'Second target statement.', 1)
        self.assertIn('previously_synced_request_changed', result['tasks'][0]['reasons'])
        with self.assertRaises(SyncError): self.service.prepare(scan['id'], self.approved(scan), saved_in_word=True)

    def test_keep_existing_closes_conflict_without_changing_doc_or_inserting(self):
        path, scan = self.modified_completed()
        before = file_hash(path)
        conflict = scan['conflicts'][0]
        result = self.service.decide_conflict(scan['id'], conflict['id'], 'keep_existing', True)
        self.assertEqual(result['tasks'][0]['status'], 'kept_existing')
        self.assertEqual(result['conflicts'][0]['status'], 'resolved')
        repeated = self.service.scan(path)
        self.assertEqual(repeated['tasks'][0]['status'], 'kept_existing')
        self.assertEqual(file_hash(path), before)
        with self.assertRaises(SyncError): self.service.prepare(repeated['id'], self.approved(repeated), saved_in_word=True)

    def test_review_as_new_requires_full_approval_and_preserves_old_citation(self):
        path, scan = self.modified_completed()
        before = file_hash(path)
        result = self.service.decide_conflict(scan['id'], scan['conflicts'][0]['id'], 'review_as_new', True)
        self.assertEqual(result['tasks'][0]['status'], 'needs_confirmation')
        with self.assertRaises(SyncError): self.service.prepare(scan['id'], [], saved_in_word=True)
        session = self.service.prepare(scan['id'], self.approved(result), saved_in_word=True)
        self.assertEqual(file_hash(session['working_path']), before)
        self.assertEqual(file_hash(path), before)

    def test_deleted_request_can_be_acknowledged_but_not_turned_into_new_item(self):
        self.service.scan(self.source)
        self.service.api.data['items'].pop(3)
        scan = self.service.scan(self.source)
        conflict = scan['conflicts'][0]
        with self.assertRaises(SyncError): self.service.decide_conflict(scan['id'], conflict['id'], 'review_as_new', True)
        result = self.service.decide_conflict(scan['id'], conflict['id'], 'keep_existing', True)
        self.assertEqual(result['conflicts'][0]['decision'], 'keep_existing')
        self.assertEqual(self.service.scan(self.source)['conflicts'][0]['status'], 'resolved')

    def test_changed_doc_or_library_blocks_conflict_decision(self):
        path, scan = self.modified_completed()
        self.service.api.data['version'] = '2'
        with self.assertRaises(SyncError): self.service.decide_conflict(scan['id'], scan['conflicts'][0]['id'], 'keep_existing', True)
        self.service.api.data['version'] = '1'
        docx(path, [run('Later user edit.')])
        with self.assertRaises(SyncError): self.service.decide_conflict(scan['id'], scan['conflicts'][0]['id'], 'keep_existing', True)

    def test_missing_confirmation_cannot_close_conflict(self):
        _, scan = self.modified_completed()
        with self.assertRaises(SyncError): self.service.decide_conflict(scan['id'], scan['conflicts'][0]['id'], 'keep_existing')

    def test_new_change_after_keep_existing_requires_new_confirmation(self):
        path, scan = self.modified_completed()
        self.service.decide_conflict(scan['id'], scan['conflicts'][0]['id'], 'keep_existing', True)
        self.service.api.data['items'][3]['data']['annotationComment'] += '\nClaim: changed evidence request'
        next_scan = self.service.scan(path)
        self.assertEqual(next_scan['tasks'][0]['status'], 'needs_resolution')
        self.assertEqual(len([c for c in next_scan['conflicts'] if c['status'] == 'open']), 1)

    def test_unresolved_older_change_is_superseded_when_source_changes_again(self):
        path, scan = self.modified_completed()
        self.service.api.data['items'][3]['data']['annotationComment'] += '\nClaim: latest request'
        result = self.service.scan(path)
        self.assertEqual([c['status'] for c in result['conflicts']], ['superseded', 'open'])

    def test_recovery_refuses_verified_result_if_working_copy_changed_after_crash(self):
        scan = self.service.scan(self.source)
        session = self.service.prepare(scan['id'], self.approved(scan), saved_in_word=True)
        path = Path(session['working_path'])
        docx(path, [run('A target statement.') + field(), run('Second target statement.')], bib=True)
        original = self.service.store._write_target
        def fail_state(target, value):
            if target == self.service.store.path: raise OSError('interrupted')
            original(target, value)
        with patch.object(self.service.store, '_write_target', side_effect=fail_state), patch('citation_sync.service.supported_mac', return_value=True):
            with self.assertRaises(OSError): self.service.verify(session['id'], {k: True for k in ATTESTATIONS})
        docx(path, [run('Later user editing.')])
        report = self.service.store.inspect_recovery()
        self.assertIn('RECOVERY_DOCUMENT_CHANGED', report['blockers'])
        self.assertFalse(report['can_recover'])


if __name__ == '__main__': unittest.main()

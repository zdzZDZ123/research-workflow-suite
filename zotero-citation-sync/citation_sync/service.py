from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from .docx_read import read_docx
from .local_api import LocalAPI
from .requests import build_tasks, title_key
from .store import Store
from .util import SyncError, copy_exact, digest, file_hash, now, read_json, write_json
from .word_review import ATTESTATIONS, STYLES, official_steps, open_working_copy, supported_mac, verify_copy


class Service:
    def __init__(self, home, api=None):
        self.store, self.api = Store(home), api or LocalAPI()

    def _scan_path(self, ident):
        if not ident or any(c not in '0123456789abcdef' for c in ident) or len(ident) != 32:
            raise SyncError('SCAN_ID_INVALID')
        return self.store.home / 'scans' / (ident + '.json')

    def _session(self, ident, state):
        if ident not in state['sessions']:
            raise SyncError('SESSION_NOT_FOUND')
        session = read_json(self.store.home / 'sessions' / ident / 'session.json')
        if session.get('id') != ident or session.get('status') != state['sessions'][ident]['status']:
            raise SyncError('SESSION_JOURNAL_INCOMPLETE', '任务记录与状态不一致，可能发生过中断；请保留数据目录并核查后恢复')
        return session

    def _live_matches(self, snapshot):
        self.api.probe()
        if self.api.server_id != snapshot['server_id']:
            raise SyncError('ZOTERO_INSTANCE_CHANGED')
        _, meta = self.api.get('/api/' + snapshot['library'] + '/items', limit=1)
        if meta.get('last-modified-version') != snapshot['version']:
            raise SyncError('LIBRARY_CHANGED_RESCAN', '文库已变化，请重新扫描后确认')

    def _conflict(self, record, source_id, change, before, after, summary=None):
        ident = digest([source_id, change, before, after])[:24]
        conflicts = record.setdefault('conflicts', {})
        conflicts.setdefault(ident, {'id': ident, 'source_id': source_id, 'change': change,
                                    'before_fingerprint': before, 'source_fingerprint': after,
                                    'summary': summary or {}, 'status': 'open', 'detected_at': now()})
        return conflicts[ident]

    @staticmethod
    def _source_summary(source):
        owner = source.get('owner')
        return {'key': source['key'], 'page': source.get('page'),
                'owner': {'key': owner['key'], 'title': owner['title']} if owner else None}

    def _decorate_tasks(self, record, scan):
        # Apply these constraints after every rebuild, including manual resolution.
        current = {t['source']['source_id']: t['source']['fingerprint'] for t in scan['tasks']}
        for conflict in record.get('conflicts', {}).values():
            if (conflict.get('status', 'open') == 'open' and conflict['change'] == 'modified_request'
                    and current.get(conflict['source_id']) != conflict.get('source_fingerprint')):
                conflict.update(status='superseded', superseded_at=now())
        for task in scan['tasks']:
            source = task['source']
            prior = record['completed'].get(source['source_id'])
            decision = record.get('source_decisions', {}).get(source['source_id'], {})
            changed = prior and prior['source_fingerprint'] != source['fingerprint']
            if changed:
                self._conflict(record, source['source_id'], 'modified_request', prior['source_fingerprint'],
                               source['fingerprint'], self._source_summary(source))
            decided = decision.get('source_fingerprint') == source['fingerprint']
            if decided and decision.get('decision') == 'keep_existing':
                task['status'] = 'kept_existing'
                task['reasons'].append('user_kept_document')
            elif changed and not (decided and decision.get('decision') == 'review_as_new'):
                task['reasons'].append('previously_synced_request_changed')
                task['status'] = 'needs_resolution'
            elif prior and not changed and not task['already_at_target']:
                task['reasons'].append('previous_sync_output_exists')
                task['status'] = 'needs_resolution'
                task['previous_session'] = prior['session_id']
        # Upgrade earlier conflict records without erasing their audit history.
        for key, conflict in record.get('conflicts', {}).items():
            conflict.setdefault('id', key)
            conflict.setdefault('status', 'open')
        scan['conflicts'] = list(record.get('conflicts', {}).values())

    def scan(self, document_path):
        with self.store.transaction() as state:
            doc = read_docx(document_path)
            snapshot = self.api.snapshot()
            doc_id = state['aliases'].get(doc['path']) or digest(doc['path'])
            record = state['documents'].setdefault(doc_id, {'resolutions': {}, 'completed': {}})
            state['aliases'][doc['path']] = doc_id
            tasks, sources, _ = build_tasks(snapshot, doc, record['resolutions'])
            source_snapshots = record.setdefault('source_snapshots', {})
            previous = source_snapshots.get(snapshot['scope'], {})
            current = {s['source_id']: {'fingerprint': s['fingerprint'], 'requested': s['requests_citation'] and not s['withdrawn'],
                                       'summary': self._source_summary(s)} for s in sources}
            changes = [{'source_id': key, 'change': 'added' if key not in previous else 'modified'}
                       for key, item in current.items() if key not in previous or previous[key]['fingerprint'] != item['fingerprint']]
            changes += [{'source_id': key, 'change': 'deleted', 'was_request': value['requested']}
                        for key, value in previous.items() if key not in current]
            source_snapshots[snapshot['scope']] = current
            # Keep deletions as durable conflicts; never automatically delete document citations.
            for change in changes:
                if change['change'] == 'deleted' and change.get('was_request'):
                    old = previous[change['source_id']]
                    self._conflict(record, change['source_id'], 'deleted', old['fingerprint'], None, old.get('summary'))
                elif (change['change'] == 'modified' and previous[change['source_id']]['requested']
                      and not current[change['source_id']]['requested']):
                    old, new = previous[change['source_id']], current[change['source_id']]
                    self._conflict(record, change['source_id'], 'request_withdrawn', old['fingerprint'], new['fingerprint'], new.get('summary'))
            scan = {'id': uuid4().hex, 'at': now(), 'doc_id': doc_id, 'document': doc, 'snapshot': snapshot,
                    'tasks': tasks, 'changes': changes}
            self._decorate_tasks(record, scan)
            record['scan_id'] = scan['id']
            self.store.stage_json(self._scan_path(scan['id']), scan)
            self.store.log('scan', scan_id=scan['id'], doc_id=doc_id, tasks=len(tasks), source_count=len(sources))
            return self.preview(scan)

    def preview(self, scan):
        doc = scan['document']
        return {'id': scan['id'], 'document': {'path': doc['path'], 'sha256': doc['sha256'], 'issues': doc['issues'],
                                              'citations': len(doc['citations']), 'style_id': doc['style_id'],
                                              'word_dynamic_verified': False},
                'tasks': scan['tasks'], 'changes': scan['changes'], 'conflicts': scan.get('conflicts', []),
                'paragraphs': [{'index': p['index'], 'text': p['text'], 'has_revision': p['has_revision']}
                               for p in doc['paragraphs'] if p['part'] == 'word/document.xml' and p['text']],
                'connection': scan['snapshot']['connection']}

    def search(self, scan_id, query):
        scan = read_json(self._scan_path(scan_id))
        _, _, papers = build_tasks(scan['snapshot'], scan['document'])
        key = title_key(query)
        if len(key) < 3:
            return []
        return [p for p in papers if key in title_key(p['title']) or query.lower() in str(p['identifiers']).lower()][:30]

    def resolve(self, scan_id, task_id, item_identity, target_quote, paragraph):
        with self.store.transaction() as state:
            scan = read_json(self._scan_path(scan_id))
            record = state['documents'][scan['doc_id']]
            if record.get('scan_id') != scan_id or file_hash(scan['document']['path']) != scan['document']['sha256']:
                raise SyncError('STALE_PREVIEW')
            task = next((t for t in scan['tasks'] if t['id'] == task_id), None)
            if not task:
                raise SyncError('TASK_NOT_FOUND')
            record['resolutions'][task['source']['source_id']] = {
                'source_fingerprint': task['source']['fingerprint'], 'item_identity': item_identity,
                'target_quote': target_quote, 'paragraph': int(paragraph)}
            tasks, _, _ = build_tasks(scan['snapshot'], scan['document'], record['resolutions'])
            scan['tasks'] = tasks
            self._decorate_tasks(record, scan)
            self.store.stage_json(self._scan_path(scan_id), scan)
            self.store.log('resolve', scan_id=scan_id, source_id=task['source']['source_id'])
            return self.preview(scan)

    def decide_conflict(self, scan_id, conflict_id, decision, confirmed=False):
        if confirmed is not True or decision not in ('keep_existing', 'review_as_new'):
            raise SyncError('CONFLICT_CONFIRMATION_REQUIRED', '请先核对来源变化并选择处理方式')
        with self.store.transaction() as state:
            scan = read_json(self._scan_path(scan_id))
            record = state['documents'][scan['doc_id']]
            if record.get('scan_id') != scan_id or file_hash(scan['document']['path']) != scan['document']['sha256']:
                raise SyncError('STALE_PREVIEW')
            if record.get('active_session'):
                raise SyncError('ACTIVE_SESSION_EXISTS', '请先完成或恢复现有工作副本，再处理来源冲突')
            self._live_matches(scan['snapshot'])
            conflict = next((c for c in record.get('conflicts', {}).values() if c.get('id') == conflict_id), None)
            if not conflict or conflict.get('status') != 'open':
                raise SyncError('CONFLICT_NOT_OPEN')
            if decision == 'review_as_new' and conflict['change'] != 'modified_request':
                raise SyncError('CONFLICT_ACTION_INVALID')
            if conflict['change'] == 'modified_request':
                task = next((t for t in scan['tasks'] if t['source']['source_id'] == conflict['source_id']), None)
                if not task or task['source']['fingerprint'] != conflict.get('source_fingerprint'):
                    raise SyncError('CONFLICT_SOURCE_CHANGED')
                record.setdefault('source_decisions', {})[conflict['source_id']] = {
                    'source_fingerprint': conflict['source_fingerprint'], 'decision': decision, 'conflict_id': conflict_id}
            conflict.update(status='resolved', decision=decision, resolved_at=now(), document_sha256=scan['document']['sha256'])
            scan['tasks'], _, _ = build_tasks(scan['snapshot'], scan['document'], record['resolutions'])
            self._decorate_tasks(record, scan)
            self.store.stage_json(self._scan_path(scan_id), scan)
            self.store.log('conflict_decision', conflict_id=conflict_id, decision=decision, doc_id=scan['doc_id'])
            return self.preview(scan)

    def prepare(self, scan_id, approvals, style='preserve', saved_in_word=False):
        if not saved_in_word or style not in STYLES or not approvals:
            raise SyncError('CONFIRMATION_REQUIRED', '请保存原稿并确认至少一项身份、位置和研究证据')
        with self.store.transaction() as state:
            scan = read_json(self._scan_path(scan_id))
            doc, doc_id = scan['document'], scan['doc_id']
            record = state['documents'][doc_id]
            if record.get('scan_id') != scan_id or doc['issues']:
                raise SyncError('STALE_OR_INVALID_DOCUMENT')
            if record.get('active_session'):
                raise SyncError('ACTIVE_SESSION_EXISTS', '已有工作副本，请先完成或恢复该任务')
            self._live_matches(scan['snapshot'])
            selected, unique = [], set()
            for approval in approvals:
                task = next((t for t in scan['tasks'] if t['id'] == approval.get('id')), None)
                if (not task or task['status'] != 'needs_confirmation'
                        or not all(approval.get(flag) is True for flag in ('identity', 'target', 'claim'))):
                    raise SyncError('TASK_NOT_APPROVED')
                previous = record['completed'].get(task['source']['source_id'])
                if previous and previous['source_fingerprint'] == task['source']['fingerprint']:
                    raise SyncError('PREVIOUS_SYNC_OUTPUT_EXISTS', '该请求已有已确认结果，请使用历史工作副本')
                target_key = (task['selected']['identity'], task['target']['paragraph'], task['target']['offset'])
                if target_key in unique:
                    raise SyncError('DUPLICATE_TARGET_REQUEST', '多个批注请求同一位置的同一文献，请只选择一项')
                unique.add(target_key)
                selected.append(task)
            ident = uuid4().hex
            directory = self.store.home / 'sessions' / ident
            copy_exact(doc['path'], directory / 'original.docx', doc['sha256'])
            copy_exact(directory / 'original.docx', directory / 'working.docx', doc['sha256'])
            if file_hash(doc['path']) != doc['sha256']:
                raise SyncError('SOURCE_CHANGED')
            session = {'id': ident, 'doc_id': doc_id, 'created_at': now(), 'status': 'awaiting_official_word',
                       'source_path': doc['path'], 'source_sha256': doc['sha256'],
                       'backup_path': str(directory / 'original.docx'), 'working_path': str(directory / 'working.docx'),
                       'tasks': selected, 'style': style, 'approvals': approvals,
                       'snapshot': {'server_id': scan['snapshot']['server_id'], 'library': scan['snapshot']['library'],
                                    'version': scan['snapshot']['version']}}
            session['steps'] = official_steps(session)
            self.store.stage_json(directory / 'session.json', session)
            state['sessions'][ident] = {'doc_id': doc_id, 'status': session['status']}
            record['active_session'] = ident
            state['aliases'][session['working_path']] = doc_id
            self.store.log('prepared', session_id=ident, source_sha256=doc['sha256'], approved=len(selected))
            return session

    def verify(self, session_id, attestations):
        with self.store.transaction() as state:
            session = self._session(session_id, state)
            if session['status'] == 'verified_by_user':
                if file_hash(session['working_path']) != session['verification']['working_sha256']:
                    raise SyncError('VERIFIED_COPY_CHANGED', '已验证副本后来被修改；旧检查结果不适用于当前文件，请保留副本并重新扫描')
                return session
            if session['status'] == 'restored_copy':
                return session
            if file_hash(session['source_path']) != session['source_sha256']:
                raise SyncError('ORIGINAL_CHANGED', '原稿已被其他操作保存，请保留工作副本并重新核对')
            if file_hash(session['backup_path']) != session['source_sha256']:
                raise SyncError('BACKUP_CHANGED')
            self._live_matches(session['snapshot'])
            check = verify_copy(session['backup_path'], session['working_path'], session['tasks'], session['style'])
            confirmation = all(attestations.get(key) is True for key in ATTESTATIONS)
            check['user_confirmed_official_checks'] = confirmation
            check['mac_word_installed'] = supported_mac()
            if check['structural_checks_passed'] and confirmation and check['mac_word_installed']:
                if file_hash(session['working_path']) != check['working_sha256']:
                    raise SyncError('WORKING_COPY_CHANGED')
                session['status'] = 'verified_by_user'
                record = state['documents'][session['doc_id']]
                for task in session['tasks']:
                    record['completed'][task['source']['source_id']] = {
                        'source_fingerprint': task['source']['fingerprint'], 'session_id': session_id,
                        'item_identity': task['selected']['identity'], 'target': task['target']['quote'],
                        'working_sha256': check['working_sha256']}
                record.pop('active_session', None)
            else:
                session['status'] = 'needs_review' if not check['structural_checks_passed'] else 'awaiting_mac_official_confirmation'
            session['verification'], session['attestations'] = check, attestations
            self.store.stage_json(Path(session['backup_path']).parent / 'session.json', session)
            state['sessions'][session_id]['status'] = session['status']
            self.store.log('verification', session_id=session_id, status=session['status'], issues=check['issues'])
            return session

    def restore(self, session_id):
        with self.store.transaction() as state:
            session = self._session(session_id, state)
            if file_hash(session['backup_path']) != session['source_sha256']:
                raise SyncError('BACKUP_CHANGED')
            target = Path(session['backup_path']).parent / ('restored-' + uuid4().hex[:8] + '.docx')
            copy_exact(session['backup_path'], target, session['source_sha256'])
            session.setdefault('restored_copies', []).append(str(target))
            session['status'] = 'restored_copy'
            record = state['documents'][session['doc_id']]
            if record.get('active_session') == session_id:
                record.pop('active_session', None)
            # The edited copy remains auditable. Restoring never deletes source annotations or citations.
            for source_id, entry in list(record['completed'].items()):
                if entry['session_id'] == session_id:
                    del record['completed'][source_id]
            state['aliases'][str(target)] = session['doc_id']
            state['sessions'][session_id]['status'] = session['status']
            self.store.stage_json(Path(session['backup_path']).parent / 'session.json', session)
            self.store.log('restore_copy', session_id=session_id, sha256=session['source_sha256'])
            return session

    def history(self):
        with self.store.transaction() as state:
            sessions = [self._session(key, state) for key in reversed(list(state['sessions']))]
            for session in sessions:
                if session['status'] == 'verified_by_user':
                    candidate = Path(session['working_path'])
                    if not candidate.is_file() or file_hash(candidate) != session['verification']['working_sha256']:
                        session['status'] = 'verified_copy_changed'
            return sessions

    def open_word(self, session_id):
        with self.store.transaction() as state:
            session = self._session(session_id, state)
            open_working_copy(session['working_path'])
            self.store.log('opened_working_copy', session_id=session_id)
            return {'opened': True}

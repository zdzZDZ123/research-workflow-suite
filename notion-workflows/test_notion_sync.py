import copy
import json
import tempfile
import unittest
from pathlib import Path
from notion_sync import EXPECTED, Store, StopSync, sync, stable_match, paper_identity, canonical
from test_paper_reading import item


class FakeNotion:
    """In-memory official-API-shaped server; inject errors after writes persist."""
    def __init__(self, paper):
        self.paper = paper
        self.pages = {'original-page': {'id': 'original-page', 'parent':{'data_source_id':'original-source'}, 'properties': {
            'Paper Key': {'type': 'rich_text', 'rich_text': [{'type': 'text', 'text': {'content': paper_identity(paper)}}]}}}}
        self.blocks = {}
        self.deep = []
        self.fail_after = None
        self.fail_before_append = False
        self.mutations = 0
        self.sequence = 0

    def props(self, properties):
        return {k: dict(v, type=next(iter(v))) for k, v in properties.items()}

    def identifier(self):
        self.sequence += 1
        return 'id-' + str(self.sequence)

    def children(self, parent): return copy.deepcopy([b for b in self.blocks.get(parent, []) if not b.get('archived')])

    def rows(self, source, filter=None):
        return copy.deepcopy([self.pages['original-page']] if source == 'original-source' else [self.pages[p] for p in self.deep])

    def append(self, parent, children):
        result = []
        for b in children:
            b = copy.deepcopy(b); b['id'] = self.identifier()
            kids = b[b['type']].pop('children', [])
            b['has_children'] = bool(kids)
            self.blocks.setdefault(parent, []).append(b)
            if kids: self.append(b['id'], kids)
            result.append(copy.deepcopy(b))
        # Reflect has_children for already-created parent blocks.
        for siblings in self.blocks.values():
            for b in siblings:
                if b['id'] == parent: b['has_children'] = True
        return result

    def request(self, method, path, body=None):
        if method == 'GET' and path.startswith('databases/'):
            return {'data_sources': [{'id': 'original-source' if path.endswith('original-db') else 'deep-source'}]}
        if method == 'GET' and path.startswith('data_sources/'):
            sid = path.split('/')[1]
            props = {k: {'type': v} for k, v in EXPECTED.items()}
            props['关联论文']['relation'] = {'type': 'single_property', 'data_source_id': 'original-source'}
            return {'id': sid, 'properties': props}
        if method == 'GET' and path.startswith('pages/'): return copy.deepcopy(self.pages[path.split('/')[1]])
        if method == 'POST' and path == 'pages':
            page_id = self.identifier()
            self.pages[page_id] = {'id': page_id, 'properties': self.props(body['properties'])}
            self.deep.append(page_id)
            self.append(page_id, body.get('children', []))
            result = copy.deepcopy(self.pages[page_id]); stage = 'create'
        elif method == 'PATCH' and path.startswith('pages/'):
            page_id = path.split('/')[1]
            self.pages[page_id]['properties'].update(self.props(body['properties']))
            result = copy.deepcopy(self.pages[page_id]); stage = 'commit'
        elif method == 'PATCH' and path.endswith('/children'):
            if self.fail_before_append: raise ConnectionError('offline before write')
            parent = path.split('/')[1]
            result = {'results': self.append(parent, body['children'])}; stage = 'append'
            if body.get('after'):
                siblings = self.blocks[parent]
                added = siblings[-len(body['children']):]
                del siblings[-len(body['children']):]
                pos = next(i for i,b in enumerate(siblings) if b['id']==body['after'])+1
                siblings[pos:pos] = added
        elif method == 'PATCH' and path.startswith('blocks/'):
            b = next(b for rows in self.blocks.values() for b in rows if b['id']==path.split('/')[1])
            b.update(body); result=copy.deepcopy(b); stage='archive'
        else: raise AssertionError((method, path))
        self.mutations += 1
        if self.fail_after == stage:
            self.fail_after = None
            raise ConnectionError('response lost after ' + stage)
        return result


class SyncTests(unittest.TestCase):
    def test_notion_coalesced_spans_and_optional_defaults(self):
        a={'type':'paragraph','paragraph':{'rich_text':[{'type':'text','text':{'content':'ab'}}], 'icon':None,'color':'default'}}
        b={'type':'paragraph','paragraph':{'rich_text':[{'type':'text','text':{'content':'a'}},{'type':'text','text':{'content':'b'}}]}}
        self.assertEqual(canonical(a),canonical(b))
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.paper = item('PAPER123', 'journalArticle')
        self.paper['data']['title'] = 'Sample'
        self.note = item('NOTE0001')
        self.api = FakeNotion(self.paper)
        self.config = {'original_database_id': 'original-db', 'database_id': 'deep-db'}

    def run_sync(self, **kwargs): return sync(self.api, self.config, self.paper, self.note, self.folder.name, **kwargs)

    def test_repeat_change_and_personal_notes(self):
        a = self.run_sync()
        before = self.api.mutations
        self.assertEqual(self.run_sync()['status'], 'UNCHANGED')
        self.assertEqual(self.api.mutations, before)
        personal = {'object': 'block', 'type': 'paragraph', 'paragraph': {'rich_text': [{'type':'text','text':{'content':'my personal note'}}]}}
        self.api.append(a['page_id'], [personal])
        old = self.api.children(a['page_id'])
        self.note['data']['note'] += '<p>new source</p>'
        b = self.run_sync()
        self.assertEqual(b['version'], 2)
        self.assertEqual(len(self.api.deep), 1)
        self.assertIn(old[-1], self.api.children(a['page_id']))
        self.assertEqual(a['page_id'], 'original-page')
        self.assertEqual(a['page_id'], b['page_id'])
        self.assertEqual(len([b for b in self.api.children(a['page_id']) if b['type']=='paragraph' and '单篇论文深度精读' in str(b)]),1)

    def test_lost_create_response_does_not_duplicate(self):
        self.api.fail_after = 'create'
        with self.assertRaises(ConnectionError): self.run_sync()
        a = self.run_sync()
        self.assertEqual(a['version'], 1)
        self.assertEqual(len(self.api.deep), 1)

    def test_lost_container_response_does_not_duplicate(self):
        self.api.fail_after = 'append'
        with self.assertRaises(ConnectionError): self.run_sync()
        a = self.run_sync()
        self.assertEqual(len([b for b in self.api.children(a['page_id']) if b['type']=='paragraph']), 1)

    def test_offline_keeps_successful_version(self):
        a = self.run_sync()
        old = copy.deepcopy(self.api.blocks)
        self.note['data']['note'] += '<p>change</p>'
        self.api.fail_before_append = True
        with self.assertRaises(ConnectionError): self.run_sync()
        self.assertEqual(self.api.blocks, old)
        self.assertEqual(self.api.pages[a['index_page_id']]['properties']['精读版本']['number'], 1)

    def test_lost_commit_response_recovers_once(self):
        self.api.fail_after = 'commit'
        with self.assertRaises(ConnectionError): self.run_sync()
        a = self.run_sync()
        self.assertEqual(a['version'], 1)
        self.assertEqual(self.run_sync()['status'], 'UNCHANGED')

    def test_conflict_is_read_only(self):
        self.api.pages['original-page']['properties'] = {'Title': {'type':'title','title':[{'type':'text','text':{'content':'Sample'}}]}}
        with self.assertRaisesRegex(StopSync, 'NOT_FOUND'): self.run_sync()
        self.assertEqual(self.api.mutations, 0)

    def test_bare_key_and_cross_library_are_not_matches(self):
        for value in ('PAPER123', 'group:1:PAPER123', 'user:2:PAPER123', 'zotero://select/library/items/PAPER123'):
            self.assertFalse(stable_match({'Paper Key': {'type':'rich_text','rich_text':[{'type':'text','text':{'content':value}}]}}, self.paper))

    def test_rollback_adds_version_without_deleting_history(self):
        a = self.run_sync()
        self.note['data']['note'] += '<p>new</p>'
        self.run_sync()
        c = self.run_sync(rollback_version=1)
        self.assertEqual(c['version'], 3)
        self.assertEqual(len([b for b in self.api.blocks[a['page_id']] if b['type']=='paragraph']), 3)
        self.assertEqual(len([b for b in self.api.children(a['page_id']) if b['type']=='paragraph']), 1)

    def test_relation_revalidated_even_unchanged(self):
        a=self.run_sync()
        self.api.pages[a['index_page_id']]['properties']['关联论文']['relation']=[{'id':'wrong'}]
        before=self.api.mutations
        with self.assertRaisesRegex(StopSync,'RELATION_CHANGED'): self.run_sync()
        self.assertEqual(self.api.mutations,before)

    def test_user_edit_in_owned_report_is_retained(self):
        a=self.run_sync()
        root=self.api.children(a['page_id'])[0]
        self.api.append(root['id'], [{'type':'paragraph','paragraph':{'rich_text':[]}}])
        self.note['data']['note'] += '<p>new version</p>'
        b=self.run_sync()
        self.assertTrue(b['warnings'])
        self.assertIn(root['id'],[r['id'] for r in self.api.children(a['page_id'])])

    def test_original_properties_untouched_and_index_has_no_report(self):
        old=copy.deepcopy(self.api.pages['original-page']['properties'])
        a=self.run_sync()
        self.assertEqual(old,self.api.pages['original-page']['properties'])
        self.assertEqual([b['type'] for b in self.api.children(a['index_page_id'])],['link_to_page'])

    def test_same_hash_legacy_index_report_migrates_once(self):
        a=self.run_sync()
        root=self.api.blocks[a['page_id']].pop(0)
        self.api.blocks[a['index_page_id']].append(root)
        store=Store(Path(self.folder.name)/'state.sqlite3')
        old=json.loads(store.db.execute('select data from versions where version=1').fetchone()[0])
        old.pop('renderer'); old.pop('body_page_id')
        with store.db: store.db.execute('update versions set data=? where version=1',(json.dumps(old),))
        store.db.close()
        b=self.run_sync()
        self.assertEqual(b['version'],2)
        self.assertEqual(b['index_page_id'],a['index_page_id'])
        self.assertEqual(self.run_sync()['status'],'UNCHANGED')
        self.assertNotIn(root['id'],[x['id'] for x in self.api.children(a['index_page_id'])])

    def test_wrong_destination_source_rejected_before_writes(self):
        self.run_sync()
        self.api.pages['original-page']['parent']['data_source_id']='different-source'
        count=self.api.mutations
        with self.assertRaisesRegex(StopSync,'BODY_PAGE_SOURCE_CHANGED'): self.run_sync()
        self.assertEqual(count,self.api.mutations)

    def test_source_change_during_failed_attempt_stops(self):
        self.api.fail_after = 'create'
        with self.assertRaises(ConnectionError): self.run_sync()
        self.note['data']['note'] += '<p>new</p>'
        count = self.api.mutations
        with self.assertRaisesRegex(StopSync, 'PENDING_SOURCE_CHANGED'): self.run_sync()
        self.assertEqual(count, self.api.mutations)


if __name__ == '__main__': unittest.main()

import copy
import json
import tempfile
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from selection_sync import convert_selection, preview_selection, read_selection, sync_selection
from notion_sync import sync, StopSync
from test_notion_sync import FakeNotion
from test_paper_reading import item


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.folder=Path(self.tmp.name)
        self.paper=item('PAPER123','journalArticle');self.paper['data']['title']='Sample'
        self.note=item('NOTE0001');self.api=FakeNotion(self.paper)
        self.config={'original_database_id':'original-db','database_id':'deep-db'}
        self.full=sync(self.api,self.config,self.paper,self.note,self.folder)
        self.d={'schema_version':1,'request_id':str(uuid.uuid4()),'library_type':self.paper['library']['type'],
            'library_id':str(self.paper['library']['id']),'item_key':self.paper['key'],
            'message_id':'message-1','captured_at':'2026-10-15T04:00:00Z',
            'text':'Only this answer','html':'<p>Only <strong>this</strong> answer</p>'}
    def run_selection(self):
        return sync_selection(self.api,self.config,self.paper,self.d,self.folder,preview_selection(self.d)['hash'])
    def test_exact_selection_repeat_and_other_content(self):
        original=copy.deepcopy(self.api.blocks);properties=copy.deepcopy(self.api.pages)
        self.assertEqual(self.run_selection()['status'],'SELECTION_SYNCED')
        count=self.api.mutations
        self.d['request_id']=str(uuid.uuid4())
        self.assertEqual(self.run_selection()['status'],'SELECTION_UNCHANGED')
        self.assertEqual(count,self.api.mutations)
        self.assertEqual(properties,self.api.pages)
        for parent,blocks in original.items():
            for b in blocks:self.assertIn(b,self.api.blocks[parent])
        self.assertEqual(len(self.api.deep),1)
    def test_new_piece_append_and_full_sync_preserves(self):
        self.run_selection();before=copy.deepcopy(self.api.blocks)
        self.d.update(request_id=str(uuid.uuid4()),text='Different',html='<p>Different</p>')
        self.run_selection()
        self.note['data']['note']+='<p>Full report changed</p>'
        sync(self.api,self.config,self.paper,self.note,self.folder)
        collection=next(b for b in self.api.children('original-page') if 'Paper Chat 精选内容' in str(b))
        self.assertEqual(len(self.api.children(collection['id'])),2)
    def test_lost_collection_create_response(self):
        self.api.fail_after='append'
        with self.assertRaises(ConnectionError): self.run_selection()
        self.assertEqual(self.run_selection()['status'],'SELECTION_SYNCED')
        roots=[b for b in self.api.children('original-page') if 'Paper Chat 精选内容' in str(b)]
        self.assertEqual(len(roots),1)
    def test_lost_fragment_create_response(self):
        self.run_selection()
        self.d.update(request_id=str(uuid.uuid4()),text='New',html='<p>New</p>')
        self.api.fail_after='append'
        with self.assertRaises(ConnectionError):self.run_selection()
        self.assertEqual(self.run_selection()['status'],'SELECTION_SYNCED')
    def test_lost_finalize_response(self):
        request=self.api.request
        lost=[False]
        def finalize(method,path,body=None):
            result=request(method,path,body)
            if method=='PATCH' and body and '已同步' in str(body.get('paragraph')) and not lost[0]:
                lost[0]=True
                raise ConnectionError('response lost after fragment finalize')
            return result
        self.api.request=finalize
        with self.assertRaises(ConnectionError):self.run_selection()
        self.assertTrue(lost[0])
        self.assertEqual(self.run_selection()['status'],'SELECTION_SYNCED')
    def test_offline_keeps_report(self):
        before=copy.deepcopy(self.api.blocks);self.api.fail_before_append=True
        with self.assertRaises(ConnectionError):self.run_selection()
        self.assertEqual(before,self.api.blocks)
    def test_unknown_creation_does_not_adopt_user_heading(self):
        self.api.fail_before_append=True
        with self.assertRaises(ConnectionError):self.run_selection()
        self.api.fail_before_append=False
        from paper_reading import block,rich
        self.api.request('PATCH','blocks/original-page/children',{'children':[block('heading_1',rich('Paper Chat 精选内容'))]})
        n=self.api.mutations
        with self.assertRaisesRegex(StopSync,'结果不明确'):self.run_selection()
        self.assertEqual(n,self.api.mutations)
    def test_known_not_sent_can_retry(self):
        request=self.api.request; rejected=[False]
        def disconnected(method,path,body=None):
            if path.endswith('/children') and method=='PATCH' and not rejected[0]:
                rejected[0]=True
                raise StopSync('NOTION_NOT_SENT: PATCH '+path)
            return request(method,path,body)
        self.api.request=disconnected
        with self.assertRaises(StopSync):self.run_selection()
        self.assertEqual(self.run_selection()['status'],'SELECTION_SYNCED')
    def test_explicit_api_rejection_retry(self):
        request=self.api.request
        rejected=[False]
        def temporary_error(method,path,body=None):
            if path.endswith('/children') and method=='PATCH' and not rejected[0]:
                rejected[0]=True
                raise StopSync('NOTION_HTTP_403: PATCH '+path)
            return request(method,path,body)
        self.api.request=temporary_error
        with self.assertRaises(StopSync): self.run_selection()
        self.assertEqual(self.run_selection()['status'],'SELECTION_SYNCED')
    def test_relation_conflict_no_write(self):
        self.api.pages[self.full['index_page_id']]['properties']['关联论文']['relation']=[{'id':'wrong'}]
        n=self.api.mutations
        with self.assertRaises(StopSync):self.run_selection()
        self.assertEqual(n,self.api.mutations)
    def test_user_deleted_or_edited_fragment_not_overwritten(self):
        self.run_selection()
        col=next(b for b in self.api.children('original-page') if 'Paper Chat 精选内容' in str(b))
        fragment=self.api.children(col['id'])[0]
        self.api.blocks[fragment['id']][1]['paragraph']['rich_text'][0]['text']['content']='USER EDIT'
        n=self.api.mutations
        self.assertEqual(self.run_selection()['status'],'SELECTION_UNCHANGED');self.assertEqual(n,self.api.mutations)
        self.api.request('PATCH','blocks/'+fragment['id'],{'archived':True})
        with self.assertRaisesRegex(StopSync,'删除或移动'):self.run_selection()
    def test_html_mismatch_falls_back_to_selected_text(self):
        self.d['html']='<p>unselected secret</p>'
        p=convert_selection(self.d)
        self.assertTrue(p['warnings']);self.assertNotIn('unselected secret',str(p['blocks']))
        self.assertIn('Only this answer',str(p['blocks']))
    def test_equivalent_formatting_hash(self):
        self.d.update(text='hello',html='<p><b>hello</b></p>')
        a=preview_selection(self.d)['hash']
        self.d['html']='<div><p><strong>he</strong><b>llo</b></p></div>'
        self.assertEqual(a,preview_selection(self.d)['hash'])
    def test_old_hash_migrates_without_duplicate(self):
        self.run_selection()
        import sqlite3
        with closing(sqlite3.connect(self.folder/'state.sqlite3')) as db:
            row=db.execute('SELECT data FROM selection_entries').fetchone()
            data=json.loads(row[0]);data['hash']='legacy-hash'
            with db:db.execute('UPDATE selection_entries SET hash=?,data=?',('legacy-hash',json.dumps(data)))
        n=self.api.mutations
        self.assertEqual(self.run_selection()['status'],'SELECTION_UNCHANGED')
        self.assertEqual(n,self.api.mutations)
    def test_collection_accepts_children_and_failures_logged(self):
        self.run_selection()
        col=next(b for b in self.api.children('original-page') if 'Paper Chat 精选内容' in str(b))
        self.assertTrue(col['heading_1']['is_toggleable'])
        with self.assertRaises(StopSync):sync_selection(self.api,self.config,self.paper,self.d,self.folder,'wrong')
        import sqlite3
        with closing(sqlite3.connect(self.folder/'state.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT status FROM selection_runs ORDER BY id').fetchall(),[('SELECTION_SYNCED',),('paused',)])
    def test_preview_is_local_and_formula_supported(self):
        self.d.update(text='x',html='<math><annotation encoding="application/x-tex">x</annotation></math>')
        n=self.api.mutations;p=convert_selection(self.d)
        self.assertIn('equation',str(p['blocks']));self.assertEqual(n,self.api.mutations)
    def test_identity_and_preview_hash_rejected(self):
        n=self.api.mutations
        with self.assertRaises(StopSync):sync_selection(self.api,self.config,self.paper,self.d,self.folder,'wrong')
        self.d['library_id']='99999'
        with self.assertRaises(StopSync):self.run_selection()
        self.assertEqual(n,self.api.mutations)
    def test_request_path_and_script_rejected(self):
        f=self.folder/'request.json';f.write_text(json.dumps(self.d),encoding='utf-8')
        with self.assertRaises(StopSync):read_selection(f,self.folder)
        self.d['html']='<script>bad</script>'
        with self.assertRaises(StopSync):preview_selection(self.d)

    def test_oversized_utf8_selection_stops_before_remote_write(self):
        self.d.update(text='长'*150000,html='<p>'+('长'*150000)+'</p>')
        n=self.api.mutations
        with self.assertRaisesRegex(StopSync,'分段选择'):preview_selection(self.d)
        self.assertEqual(n,self.api.mutations)


if __name__=='__main__':unittest.main()

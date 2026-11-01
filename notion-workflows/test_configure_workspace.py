import tempfile
import unittest
from pathlib import Path
from configure_workspace import ensure_database, page_id
from notion_sync import StopSync

class SetupTests(unittest.TestCase):
    def test_page_url(self):
        self.assertEqual(page_id('https://www.notion.so/Test-' + 'a'*32 + '?pvs=4'), 'a'*32)
        with self.assertRaises(ValueError): page_id('not-a-page')

    def test_lost_create_response_is_read_before_retry(self):
        class API:
            rows = []
            creates = 0
            def children(self, _): return self.rows
            def request(self, method, path, body=None):
                if method == 'POST':
                    self.creates += 1
                    self.rows = [{'type':'child_database','id':'db','child_database':{'title':'Fixture'}}]
                    raise StopSync('NOTION_OUTCOME_UNKNOWN')
                return {'id':'db','data_sources':[{'id':'source'}]}
        api=API()
        with tempfile.TemporaryDirectory() as folder:
            journal=Path(folder)/'intent.json'
            with self.assertRaises(StopSync): ensure_database(api,'parent','Fixture',{},journal)
            self.assertTrue(journal.exists())
            self.assertEqual(ensure_database(api,'parent','Fixture',{},journal),('db','source'))
            self.assertEqual(api.creates,1)
            self.assertFalse(journal.exists())

    def test_unknown_missing_remote_never_recreates(self):
        class API:
            def children(self, _): return []
            def request(self,*_): raise AssertionError('Must not create')
        with tempfile.TemporaryDirectory() as folder:
            journal=Path(folder)/'intent.json';journal.write_text('{}')
            with self.assertRaisesRegex(StopSync,'SETUP_WRITE_UNKNOWN'):
                ensure_database(API(),'parent','Fixture',{},journal)

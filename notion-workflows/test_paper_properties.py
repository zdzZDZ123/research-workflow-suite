import unittest, tempfile, sqlite3
from pathlib import Path
from paper_properties import desired_properties, fill_properties
from test_notion_sync import FakeNotion
from test_paper_reading import item

class PropertyTests(unittest.TestCase):
    def test_missing_numbers_and_url_never_fabricated(self):
        p=item('PAPER123','journalArticle');p['data']['title']='Paper'
        schema={'Journal':{'type':'rich_text'},'影响因子':{'type':'number'},'Match Score':{'type':'number'},'PDF URL':{'type':'url'},'JCR分区':{'type':'select'}}
        with tempfile.TemporaryDirectory() as f:
            props,ui=desired_properties(p,{'sha256':'none','blocks':[]},schema,f,[])
        self.assertNotIn('影响因子',props);self.assertNotIn('Match Score',props);self.assertNotIn('PDF URL',props)
        self.assertEqual(props['JCR分区']['select']['name'],'待核实')
        self.assertIn('无',str(ui))

    def test_fill_empty_update_owned_preserve_user_edits(self):
        p=item('PAPER123','journalArticle');api=FakeNotion(p)
        api.pages['original-page']['properties']['Journal']={'type':'rich_text','rich_text':[]}
        db=sqlite3.connect(':memory:')
        with tempfile.TemporaryDirectory() as f:
            def run(value): fill_properties(api,db,'identity','original-page',{'Journal':{'rich_text':[{'type':'text','text':{'content':value}}]}},f)
            run('First');run('Second')
            self.assertIn('Second',str(api.pages['original-page']))
            api.pages['original-page']['properties']['Journal']['rich_text'][0]['text']['content']='User choice'
            run('Third')
            self.assertIn('User choice',str(api.pages['original-page']))
        db.close()

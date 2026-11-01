import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from paper_reading import Converter, choose_note, prepare


def item(key, kind='note', parent='PAPER123', library=1, html='<h1>AI深度摘要</h1><p>原文</p>'):
    return {'key': key, 'library': {'type': 'user', 'id': library}, 'data': {'itemType': kind, 'parentItem': parent, 'note': html, 'tags': []}}


class Tests(unittest.TestCase):
    def test_no_arbitrary_candidate(self):
        paper = item('PAPER123', 'journalArticle')
        with self.assertRaisesRegex(ValueError, 'NOTE_SELECTION_REQUIRED'):
            choose_note(paper, [item('NOTE0001'), item('NOTE0002')])

    def test_bound_note_does_not_fallback(self):
        with self.assertRaisesRegex(ValueError, 'no fallback'):
            choose_note(item('PAPER123', 'journalArticle'), [item('NOTE0001')], 'NOTE0002')

    def test_cross_library_note_rejected(self):
        with self.assertRaises(ValueError):
            choose_note(item('PAPER123', 'journalArticle'), [item('NOTE0001', library=2)], 'NOTE0001')

    def test_html_formatting_equation_and_quote(self):
        c = Converter()
        blocks = c.convert('<h2>结果</h2><p><b>强</b><a href="https://example.org">证据</a><math><semantics><mi>x</mi><annotation encoding="application/x-tex">x^2</annotation></semantics></math></p><ul><li>一</li></ul><blockquote>引文</blockquote>')
        self.assertEqual([b['type'] for b in blocks], ['heading_2','paragraph','bulleted_list_item','quote'])
        spans = blocks[1]['paragraph']['rich_text']
        self.assertTrue(spans[0]['annotations']['bold'])
        self.assertEqual(spans[1]['text']['link']['url'], 'https://example.org')
        self.assertEqual(spans[2]['equation']['expression'], 'x^2')

    def test_unsupported_content_is_reported(self):
        c = Converter()
        blocks = c.convert('<table><tr><td>99</td></tr></table><img src="file:///private.png"><math><mi>x</mi></math>')
        self.assertIn('99', json.dumps(blocks))
        self.assertTrue(any('Image' in w for w in c.warnings))
        self.assertTrue(any('MathML' in w for w in c.warnings))
        self.assertEqual(blocks[0]['type'], 'table')
        self.assertEqual(blocks[0]['table']['children'][0]['table_row']['cells'][0][0]['text']['content'], '99')

    def test_long_text_is_not_truncated(self):
        original = '中' * 5000
        blocks = Converter().convert('<p>' + original + '</p>')
        self.assertEqual(''.join(r['text']['content'] for r in blocks[0]['paragraph']['rich_text']), original)

    def test_preview_idempotency_backup_and_changed_source(self):
        with tempfile.TemporaryDirectory() as d:
            paper = item('PAPER123', 'journalArticle')
            note = item('NOTE0001')
            a = prepare(paper, note, d)
            b = prepare(paper, note, d)
            self.assertEqual(a['sha256'], b['sha256'])
            self.assertEqual((Path(a['path']) / 'source.html').read_text(encoding='utf-8'), note['data']['note'])
            note['data']['note'] += '<p>新增</p>'
            changed = prepare(paper, note, d)
            self.assertNotEqual(a['sha256'], changed['sha256'])
            with closing(sqlite3.connect(Path(d) / 'state.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM previews').fetchone()[0], 2)
            payload = json.loads((Path(a['path']) / 'notion-preview.json').read_text(encoding='utf-8'))
            self.assertEqual(payload['status'], 'PREVIEW_ONLY')
            self.assertNotIn(payload['user_section'], payload['blocks'])


if __name__ == '__main__': unittest.main()

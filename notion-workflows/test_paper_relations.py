"""Two real PDF fixtures and a fake Notion transport for relationship acceptance."""
import io
import json
import tempfile
import unittest
import urllib.error
import uuid
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from notion_sync import StopSync, plain
from paper_reading import rich
from relation_evidence import (eligible_record, evidence_cards, pdf_pages, read_paper,
                               verify_relationships, zotero_pdf)
from relation_notion import RelationPublisher, RelationStore, _value, candidate_body
from relation_model import CompatibleModel, ModelError
import paper_relations
import relation_zotero


QUOTE_A = 'Theta alpha coupling predicts cooperation.'
QUOTE_B = 'Theta alpha coupling predicts social engagement.'
DOI_B = '10.1234/target.2026'


def sample_pdf(text):
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = writer._add_object(DictionaryObject({NameObject('/Type'): NameObject('/Font'),
        NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')}))
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):
        DictionaryObject({NameObject('/F1'): font})})
    lines = text.split('\n')
    commands = ['BT /F1 10 Tf 30 760 Td 13 TL']
    for line in lines:
        escaped = line.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
        commands.append('(' + escaped + ') Tj T*')
    commands.append('ET')
    stream = DecodedStreamObject(); stream.set_data('\n'.join(commands).encode('ascii'))
    page[NameObject('/Contents')] = writer._add_object(stream)
    output = io.BytesIO(); writer.write(output)
    return output.getvalue()


def page_record(number, title, doi=None, pdf_url=None):
    props = {'原标题': {'type': 'title', 'title': rich(title)},
             'Paper Key': {'type': 'rich_text', 'rich_text': rich(f'user:12345678:TEST{number:04d}')},
             'DOI': {'type': 'url', 'url': doi and 'https://doi.org/' + doi},
             'PDF URL': {'type': 'url', 'url': pdf_url}}
    return {'id': f'00000000-0000-4000-8000-{number:012d}', 'properties': props,
            'url': f'https://www.notion.so/{number:032x}'}


def cards_for(paper, kind, concept, quote):
    return [{'id': kind + '-1', 'kind': kind, 'concept': concept, 'page': 1, 'quote': quote}]


class CardModel:
    def __init__(self, quote): self.quote = quote
    def complete_json(self, system, content):
        return {'cards': [{'kind': 'topic', 'concept': 'Theta alpha coupling', 'page': 1, 'quote': self.quote},
                          {'kind': 'topic', 'concept': 'invented', 'page': 1, 'quote': 'This was never in either PDF.'}]}


class FakeNotion:
    def __init__(self):
        self.pages = {}
        self.blocks = {}
        self.original_rows = []
        self.lose_page = False
        self.lose_block = False

    def children(self, owner):
        return [x for x in self.blocks.get(owner, []) if not x.get('archived')]

    def rows(self, source, filter=None):
        if source == 'original':
            return self.original_rows
        rows = list(self.pages.values())
        if filter:
            key = filter['rich_text']['equals']
            rows = [x for x in rows if _value(x, '关系键') == key]
        return rows

    def _append(self, owner, values):
        created = []
        for raw in values:
            item = dict(raw)
            item['id'] = str(uuid.uuid4())
            item['last_edited_time'] = '2026-10-20T00:00:00Z'
            nested = item[item['type']].pop('children', [])
            created.append(item)
            self._append(item['id'], nested)
        self.blocks.setdefault(owner, []).extend(created)
        return created

    def request(self, method, path, body=None):
        if method == 'POST' and path == 'pages':
            row = {'id': str(uuid.uuid4()), 'properties': {}, 'url': 'https://www.notion.so/test'}
            for name, value in body['properties'].items():
                kind = next(iter(value))
                row['properties'][name] = {'type': kind, **value}
            self.pages[row['id']] = row
            self._append(row['id'], body.get('children', []))
            if self.lose_page:
                self.lose_page = False
                raise StopSync('NOTION_OUTCOME_UNKNOWN: POST pages')
            return row
        if method == 'GET' and path.startswith('pages/'):
            return self.pages[path.split('/')[1]]
        if method == 'PATCH' and path.startswith('pages/'):
            row = self.pages[path.split('/')[1]]
            row['properties'].update({k: {'type': next(iter(v)), **v} for k, v in body['properties'].items()})
            return row
        if method == 'PATCH' and path.startswith('blocks/') and path.endswith('/children'):
            owner = path.split('/')[1]
            result = self._append(owner, body['children'])
            if self.lose_block:
                self.lose_block = False
                raise StopSync('NOTION_OUTCOME_UNKNOWN: PATCH blocks')
            return {'results': result}
        if method == 'PATCH' and path.startswith('blocks/'):
            target = path.split('/')[1]
            for blocks in self.blocks.values():
                for item in blocks:
                    if item['id'] == target:
                        item.update(body)
                        return item
        raise AssertionError((method, path))


class PaperRelationshipTests(unittest.TestCase):
    def setUp(self):
        config_patch = patch.object(paper_relations, '_original_config', return_value={})
        config_patch.start()
        self.addCleanup(config_patch.stop)
        self.raw_a = sample_pdf((QUOTE_A + '\nA cites ' + DOI_B + ' in its references.\n') * 22)
        self.raw_b = sample_pdf((QUOTE_B + '\nThe target paper reports task cooperation.\n') * 22)

    def test_zotero_attachment_redirect_reports_missing_file(self):
        class LocalZotero:
            base = 'http://127.0.0.1:23119/api/users/12345678'
            def __init__(self, *_): pass
            def item(self, key):
                return {'library': {'type': 'user', 'id': 12345678}, 'key': key}
            def children(self, _):
                return [{'key': 'ATT00001', 'data': {'itemType': 'attachment',
                    'contentType': 'application/pdf', 'linkMode': 'imported_file',
                    'filename': 'absent.pdf'}}]
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'ATT00001' / 'absent.pdf'
            def opener(request, timeout):
                raise urllib.error.HTTPError(request.full_url, 302, 'Found',
                                             {'Location': target.as_uri()}, None)
            with patch('relation_evidence.Zotero', LocalZotero), \
                 self.assertRaisesRegex(ValueError, 'ZOTERO_PDF_FILE_MISSING'):
                zotero_pdf({'identity': 'user:12345678:TEST0001'}, opener=opener)

    def test_fulltext_page_evidence_and_publication_gate(self):
        self.assertEqual(len(pdf_pages(self.raw_a)), 1)
        page_a = page_record(1, 'Paper A')
        page_b = page_record(2, 'Paper B', DOI_B, 'https://example.org/b.pdf')
        a = read_paper(None, page_a, zotero_reader=lambda _: (self.raw_a, {'kind': 'zotero'}))
        b = read_paper(type('API', (), {'children': lambda _, __: []})(), page_b,
                       zotero_reader=lambda _: (_ for _ in ()).throw(ValueError('Zotero offline')),
                       url_reader=lambda _: self.raw_b)
        self.assertEqual(a['page_count'], 1)
        self.assertEqual(b['source']['kind'], 'notion_pdf')

        self.assertEqual(len(evidence_cards(CardModel(QUOTE_A), a)), 1)
        ca = cards_for(a, 'topic', 'Theta alpha coupling', QUOTE_A)
        ca.append({'id': 'citation-1', 'kind': 'citation', 'concept': DOI_B, 'page': 1,
                   'quote': 'A cites ' + DOI_B + ' in its references.'})
        cb = cards_for(b, 'topic', 'Theta alpha coupling', QUOTE_B)
        proposed = [
            {'type': 'shared_topic', 'concept': 'Theta alpha coupling', 'summary': 'Both discuss it.',
             'evidence_a': 'topic-1', 'evidence_b': 'topic-1', 'direction': 'symmetric'},
            {'type': 'citation', 'concept': DOI_B, 'summary': 'A cites B.',
             'evidence_a': 'citation-1', 'evidence_b': 'topic-1', 'direction': 'a_to_b'},
            {'type': 'supports', 'concept': 'cooperation', 'summary': 'Possible support.',
             'evidence_a': 'topic-1', 'evidence_b': 'topic-1', 'direction': 'b_to_a'},
            {'type': 'citation', 'concept': DOI_B, 'summary': 'Wrong direction.',
             'evidence_a': 'citation-1', 'evidence_b': 'topic-1', 'direction': 'b_to_a'},
            {'type': 'citation', 'concept': DOI_B, 'summary': 'Missing citation direction.',
             'evidence_a': 'citation-1', 'evidence_b': 'topic-1', 'direction': 'symmetric'}]
        result = verify_relationships(a, b, ca, cb, proposed)
        self.assertEqual([x['auto_publish'] for x in result], [True, True, False, False])
        self.assertEqual(result[1]['direction'], 'a_to_b')
        self.assertIn(DOI_B, result[1]['decision_reason'])
        self.assertIn('人工', result[2]['decision_reason'])
        noncitation = [dict(card, kind='topic') for card in ca]
        mention = verify_relationships(a, b, noncitation, cb, [proposed[1]])
        self.assertFalse(mention[0]['auto_publish'])
        self.assertFalse(eligible_record({'id': page_a['id'], 'properties': {'原标题': page_a['properties']['原标题']}}))
        with self.assertRaisesRegex(ValueError, 'FULLTEXT_UNAVAILABLE'):
            read_paper(type('API', (), {'children': lambda _, __: []})(), page_a,
                       zotero_reader=lambda _: (_ for _ in ()).throw(ValueError('no PDF')))

    def test_notion_pdf_block_is_eligible_without_zotero_key(self):
        page = page_record(4, 'Paper with PDF block')
        page['properties']['Paper Key']['rich_text'] = []
        api = FakeNotion()
        api.blocks[page['id']] = [{'type': 'pdf', 'pdf': {'type': 'external',
            'external': {'url': 'https://example.org/paper.pdf'}}}]
        self.assertTrue(paper_relations._eligible(api, page))
        paper = read_paper(api, page, url_reader=lambda _: self.raw_a)
        self.assertEqual(paper['source']['kind'], 'notion_pdf')

    def test_zotero_collection_scope_and_notion_identity_recovery(self):
        item = {'key': 'TEST0001', 'library': {'type': 'user', 'id': 12345678},
                'data': {'itemType': 'journalArticle', 'title': 'Scoped paper',
                         'DOI': '10.1234/scoped', 'collections': ['TESTCOL1']}}
        def fake_get(url):
            if url.endswith('/collections/TESTCOL1'):
                return {'data': {'name': 'Scoped collection'}}
            if '/items/top?' in url:
                return [item]
            raise AssertionError(url)
        with patch('relation_zotero.get_json', side_effect=fake_get):
            _, items = relation_zotero.collection_items('TESTCOL1')
        self.assertEqual(items, [item])
        page = relation_zotero.provisional_page(item)
        self.assertEqual(plain(page['properties']['Paper Key']), 'user:12345678:TEST0001')
        class RecoveringAPI(FakeNotion):
            def request(self, method, path, body=None):
                if method == 'POST' and path == 'pages':
                    self.original_rows.append({'id': 'paper-page', 'properties': {
                        name: {'type': next(iter(value)), **value} for name, value in body['properties'].items()}})
                    raise StopSync('NOTION_OUTCOME_UNKNOWN')
                if method == 'GET' and path == 'pages/paper-page':
                    return self.original_rows[0]
                return super().request(method, path, body)
            def rows(self, source, filter=None):
                return self.original_rows if source == 'original' else super().rows(source, filter)
        api = RecoveringAPI()
        found, created = relation_zotero.reconcile(api, 'original', item, api.original_rows)
        self.assertTrue(created)
        self.assertEqual(found['id'], 'paper-page')
        self.assertEqual(relation_zotero.reconcile(api, 'original', item, api.original_rows)[1], False)
        self.assertEqual(len(api.original_rows), 1)

    def test_relationship_page_contains_both_pdf_sources_and_review_reason(self):
        a = read_paper(None, page_record(1, 'Paper A'), zotero_reader=lambda _: (self.raw_a,
            {'kind': 'zotero', 'attachment_key': 'ATTACH01', 'url': 'https://www.zotero.org/users/12345678/items/ATTACH01'}))
        b = read_paper(None, page_record(2, 'Paper B'), zotero_reader=lambda _: (self.raw_b,
            {'kind': 'notion_pdf', 'url': 'https://example.org/b.pdf'}))
        ca = cards_for(a, 'topic', 'Theta alpha coupling', QUOTE_A)
        cb = cards_for(b, 'topic', 'Theta alpha coupling', QUOTE_B)
        candidate = verify_relationships(a, b, ca, cb, [{'type': 'shared_topic',
            'concept': 'Theta alpha coupling', 'summary': 'Both use this term.',
            'evidence_a': 'topic-1', 'evidence_b': 'topic-1', 'direction': 'symmetric'}])[0]
        body = candidate_body(candidate, a, b, 'fixture')
        texts = [plain({'type': 'rich_text', 'rich_text': item[item['type']]['rich_text']}) for item in body]
        content = '\n'.join(texts)
        for expected in (a['pdf_sha256'], b['pdf_sha256'], 'ATTACH01', '共同主题',
                         '双向关联', candidate['decision_reason'], '第 1 页 / 共 1 页'):
            self.assertIn(expected, content)
        self.assertEqual([text for item, text in zip(body, texts) if item['type'] == 'quote'], [QUOTE_A, QUOTE_B])
        links = [span['text']['link']['url'] for item in body
                 for span in item[item['type']]['rich_text'] if span['text'].get('link')]
        self.assertIn(a['source']['url'], links)
        self.assertIn(b['source']['url'], links)
        b['source']['url'] = 'file:///private/local.pdf'
        self.assertNotIn('file:///private/local.pdf', json.dumps(candidate_body(candidate, a, b, 'fixture')))

    def test_lost_responses_repeat_stale_manual_edits_and_backlinks(self):
        page_a = page_record(1, 'Paper A'); page_b = page_record(2, 'Paper B', DOI_B)
        a = read_paper(None, page_a, zotero_reader=lambda _: (self.raw_a, {'kind': 'zotero'}))
        b = read_paper(None, page_b, zotero_reader=lambda _: (self.raw_b, {'kind': 'zotero'}))
        ca = cards_for(a, 'topic', 'Theta alpha coupling', QUOTE_A)
        cb = cards_for(b, 'topic', 'Theta alpha coupling', QUOTE_B)
        candidate = verify_relationships(a, b, ca, cb, [{'type': 'shared_topic', 'concept': 'Theta alpha coupling',
            'summary': 'A shared term.', 'evidence_a': 'topic-1', 'evidence_b': 'topic-1', 'direction': 'symmetric'}])[0]
        with tempfile.TemporaryDirectory() as folder:
            api = FakeNotion(); store = RelationStore(Path(folder) / 'state.sqlite3')
            self.addCleanup(store.db.close)
            pub = RelationPublisher(api, {'relation_data_source_id': 'source', 'overview_page_id': 'overview',
                                           'backup_dir': folder}, store)
            api.lose_page = True
            page_id, created = pub.publish(candidate, a, b, 'fixture-model')
            self.assertTrue(created)
            self.assertEqual(len(api.pages), 1)
            self.assertEqual(pub.publish(candidate, a, b, 'fixture-model'), (page_id, False))
            self.assertEqual(len(api.pages), 1)
            papers = [a, b]
            api.lose_block = True
            self.assertEqual(pub.backlinks(papers, pub.published()), 2)
            self.assertEqual(len(api.children(a['page_id'])), 1)
            self.assertEqual(len(api.children(b['page_id'])), 1)
            with patch('relation_notion.upload_png', return_value='uploaded-file'):
                self.assertTrue(pub.graph(papers, pub.published()))
                self.assertFalse(pub.graph(papers, pub.published()))
            root = api.children('overview')[0]
            self.assertEqual(root['type'], 'paragraph')
            self.assertEqual(api.children(root['id'])[1]['type'], 'image')
            self.assertEqual(pub.backlinks(papers, pub.published()), 0)
            self.assertEqual(len(api.children(a['page_id'])), 1)
            api.pages[page_id]['properties']['审核状态'] = {'type': 'select', 'select': {'name': '已驳回'}}
            self.assertEqual(pub.publish(candidate, a, b, 'fixture-model'), (page_id, False))
            self.assertEqual(_value(api.pages[page_id], '审核状态'), '已驳回')
            changed = dict(candidate, source_hash='new-pdf-hash')
            self.assertEqual(pub.stale_pair(a, b, 'new-pdf-hash'), 1)
            self.assertEqual(_value(api.pages[page_id], '证据状态'), '需复核')
            pub.publish(changed, a, b, 'fixture-model')
            self.assertEqual(_value(api.pages[page_id], '证据状态'), '需复核')
            revision = [item for item in api.children(page_id)
                        if item['type'] == 'paragraph' and plain({'type': 'rich_text',
                            'rich_text': item['paragraph']['rich_text']}).startswith('论文关系 · candidate:')]
            self.assertEqual(len(revision), 1)
            self.assertIn('new-pdf-hash', json.dumps(api.children(revision[0]['id']), ensure_ascii=False))
            self.assertEqual(pub.backlinks(papers, pub.published()), 2)
            self.assertEqual(len(api.children(a['page_id'])), 0)
            self.assertEqual(len(api.pages), 1)
            store.db.close()

    def test_incremental_pair_missing_fulltext_and_source_change(self):
        a_page = page_record(1, 'Paper A')
        b_page = page_record(2, 'Paper B', DOI_B)
        missing_page = page_record(3, 'Paper without PDF')
        api = FakeNotion(); api.original_rows = [a_page, b_page, missing_page]
        papers = {
            a_page['id']: read_paper(None, a_page, zotero_reader=lambda _: (self.raw_a, {'kind': 'zotero'})),
            b_page['id']: read_paper(None, b_page, zotero_reader=lambda _: (self.raw_b, {'kind': 'zotero'}))}

        class Model:
            config = {'model': 'fixture-model'}
            def __init__(self): self.calls = 0; self.checks = 0
            def preflight(self): self.checks += 1
            def complete_json(self, system, content):
                self.calls += 1
                if system.startswith('Extract only'):
                    title = json.loads(content)['title']
                    quote = QUOTE_A if title == 'Paper A' else QUOTE_B
                    return {'cards': [{'kind': 'topic', 'concept': 'Theta alpha coupling', 'page': 1, 'quote': quote}]}
                pair = json.loads(content)
                return {'relationships': [{'type': 'shared_topic', 'concept': 'Theta alpha coupling',
                    'summary': 'Both use the term.', 'evidence_a': pair['paper_a']['cards'][0]['id'],
                    'evidence_b': pair['paper_b']['cards'][0]['id'], 'direction': 'symmetric'}]}

        model = Model()
        config = {'relation_data_source_id': 'relations', 'overview_page_id': 'overview'}
        def reader(_, row):
            if row['id'] not in papers: raise ValueError('FULLTEXT_UNAVAILABLE')
            return papers[row['id']]
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(paper_relations, 'RUNTIME', Path(folder)), \
             patch.object(paper_relations, 'preflight', return_value={'original_source': {'id': 'original'}}), \
             patch.object(paper_relations, '_relation_config', return_value=config), \
             patch('relation_notion.upload_png', return_value='uploaded-file'):
            first = paper_relations.run(api=api, model=model, reader=reader)
            self.assertEqual((first['eligible'], first['readable'], first['skipped']), (3, 2, 1))
            self.assertEqual((first['pairs_examined'], first['auto_published'], first['remaining_pairs']), (1, 1, 0))
            self.assertEqual(len(api.pages), 1)
            self.assertEqual(model.calls, 3)
            second = paper_relations.run(api=api, model=model, reader=reader)
            self.assertEqual(second['pairs_examined'], 0)
            self.assertEqual(model.calls, 3)
            papers[b_page['id']] = dict(papers[b_page['id']], pdf_sha256='revised-pdf-hash')
            class UnavailableModel(Model):
                def preflight(self): raise ModelError('MODEL_UNAVAILABLE')
            with self.assertRaisesRegex(ModelError, 'MODEL_UNAVAILABLE'):
                paper_relations.run(api=api, model=UnavailableModel(), reader=reader)
            self.assertEqual(_value(next(iter(api.pages.values())), '证据状态'), '需复核')
            self.assertEqual(len(api.children(a_page['id'])), 0)
            third = paper_relations.run(api=api, model=model, reader=reader)
            self.assertEqual((third['pairs_examined'], third['published_total']), (1, 0))
            self.assertEqual(len(api.pages), 1)
            self.assertEqual(_value(next(iter(api.pages.values())), '证据状态'), '需复核')
            self.assertEqual(model.calls, 5)  # Only B's card and the pair are recalculated.
            reviewed = next(iter(api.pages.values()))
            reviewed['properties']['审核状态'] = {'type': 'select', 'select': {'name': '人工确认'}}
            fourth = paper_relations.run(api=api, reader=reader)
            self.assertEqual(fourth['published_total'], 0)
            self.assertEqual(_value(reviewed, '证据状态'), '需复核')
            reviewed['properties']['证据状态'] = {'type': 'select', 'select': {'name': '当前'}}
            fifth = paper_relations.run(api=api, reader=reader)
            self.assertEqual((fifth['pairs_examined'], fifth['published_total']), (0, 1))
            self.assertEqual(_value(reviewed, '来源指纹'), paper_relations._source_digest(
                papers[a_page['id']], papers[b_page['id']]))
            self.assertEqual(len(api.children(a_page['id'])), 1)

    def test_fulltext_loss_revokes_published_relation_and_restore_rechecks_pair(self):
        a_page = page_record(1, 'Paper A'); b_page = page_record(2, 'Paper B')
        api = FakeNotion(); api.original_rows = [a_page, b_page]
        papers = {
            a_page['id']: read_paper(None, a_page, zotero_reader=lambda _: (self.raw_a, {'kind': 'zotero'})),
            b_page['id']: read_paper(None, b_page, zotero_reader=lambda _: (self.raw_b, {'kind': 'zotero'}))}
        class Model:
            config = {'model': 'fixture'}
            def preflight(self): pass
            def complete_json(self, system, content):
                if system.startswith('Extract only'):
                    quote = QUOTE_A if json.loads(content)['title'] == 'Paper A' else QUOTE_B
                    return {'cards': [{'kind': 'topic', 'concept': 'Theta alpha coupling', 'page': 1, 'quote': quote}]}
                pair = json.loads(content)
                return {'relationships': [{'type': 'shared_topic', 'concept': 'Theta alpha coupling',
                    'summary': 'Both use the term.', 'evidence_a': pair['paper_a']['cards'][0]['id'],
                    'evidence_b': pair['paper_b']['cards'][0]['id'], 'direction': 'symmetric'}]}
        def reader(_, row):
            if row['id'] not in papers: raise ValueError('FULLTEXT_UNAVAILABLE')
            return papers[row['id']]
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(paper_relations, 'RUNTIME', Path(folder)), \
             patch.object(paper_relations, 'preflight', return_value={'original_source': {'id': 'original'}}), \
             patch.object(paper_relations, '_relation_config', return_value={
                 'relation_data_source_id': 'relations', 'overview_page_id': 'overview'}), \
             patch('relation_notion.upload_png', return_value='uploaded-file'):
            first = paper_relations.run(api=api, model=Model(), reader=reader)
            self.assertEqual(first['published_total'], 1)
            lost = papers.pop(b_page['id'])
            second = paper_relations.run(api=api, reader=reader)
            self.assertEqual((second['readable'], second['published_total']), (1, 0))
            self.assertEqual(_value(next(iter(api.pages.values())), '证据状态'), '需复核')
            self.assertEqual(len(api.children(a_page['id'])), 0)
            papers[b_page['id']] = lost
            third = paper_relations.run(api=api, model=Model(), reader=reader)
            self.assertEqual(third['pairs_examined'], 1)
            self.assertEqual(len(api.pages), 1)

    def test_compatible_model_preflight_for_both_interfaces(self):
        class Response:
            def __init__(self, value): self.value = value
            def __enter__(self): return io.BytesIO(json.dumps(self.value).encode())
            def __exit__(self, *_): pass
        for style, output in (
                ('chat_completions', {'choices': [{'message': {'content': '{"ok":true}'}}]}),
                ('responses', {'output': [{'content': [{'type': 'output_text', 'text': '{"ok":true}'}]}]})):
            with self.subTest(style=style):
                calls = []
                def opener(request, timeout):
                    calls.append((request.full_url, json.loads(request.data)))
                    return Response(output)
                model = CompatibleModel({'base_url': 'https://example.org/v1',
                    'model': 'fixture', 'api_style': style}, key='fixture-key', opener=opener)
                self.assertTrue(model.preflight())
                self.assertEqual(calls[0][0], 'https://example.org/v1/' +
                                 ('responses' if style == 'responses' else 'chat/completions'))
                self.assertEqual(calls[0][1]['model'], 'fixture')

    def test_twenty_pair_cap_continues_on_next_run(self):
        api = FakeNotion()
        api.original_rows = [page_record(n, f'Paper {n}') for n in range(1, 8)]
        class Model:
            config = {'model': 'fixture'}
            def preflight(self): pass
        def reader(_, row):
            return dict(page_id=row['id'], title=plain(row['properties']['原标题']),
                        pdf_sha256=row['id'], source={'kind': 'fixture'}, pages=['content'],
                        url=row['url'], doi='', page_count=1)
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(paper_relations, 'RUNTIME', Path(folder)), \
             patch.object(paper_relations, 'preflight', return_value={'original_source': {'id': 'original'}}), \
             patch.object(paper_relations, '_relation_config', return_value={
                 'relation_data_source_id': 'relations', 'overview_page_id': 'overview'}), \
             patch.object(paper_relations, '_cards', return_value=[]), \
             patch.object(paper_relations, 'compare_cards', return_value=[]), \
             patch('relation_notion.upload_png', return_value='uploaded-file'):
            first = paper_relations.run(api=api, model=Model(), reader=reader)
            self.assertEqual((first['pairs_examined'], first['remaining_pairs']), (20, 1))
            second = paper_relations.run(api=api, model=Model(), reader=reader)
            self.assertEqual((second['pairs_examined'], second['remaining_pairs']), (1, 0))

    def test_graph_respects_reverse_citation_direction(self):
        a = {'page_id': page_record(1, 'Paper A')['id'], 'title': 'Paper A'}
        b = {'page_id': page_record(2, 'Paper B')['id'], 'title': 'Paper B'}
        row = {'id': str(uuid.uuid4()), 'properties': {
            '论文A': {'type': 'relation', 'relation': [{'id': a['page_id']}]},
            '论文B': {'type': 'relation', 'relation': [{'id': b['page_id']}]},
            '关系类型': {'type': 'select', 'select': {'name': 'citation'}},
            '方向': {'type': 'select', 'select': {'name': 'b_to_a'}}}}
        with tempfile.TemporaryDirectory() as folder:
            store = RelationStore(Path(folder) / 'state.sqlite3')
            pub = RelationPublisher(FakeNotion(), {'relation_data_source_id': 'relations',
                'overview_page_id': 'overview', 'backup_dir': folder}, store)
            edges = []
            def capture(nodes, passed_edges):
                edges.extend(passed_edges)
                return b'PNG'
            with patch('relation_notion.graph_png', side_effect=capture), \
                 patch('relation_notion.upload_png', return_value='uploaded-file'):
                pub.graph([a, b], [row])
            self.assertEqual(edges[0][:2], (b['page_id'], a['page_id']))
            store.db.close()

    def test_one_real_paper_needs_no_model_and_creates_only_one_node(self):
        page = page_record(1, 'Only real paper')
        empty = {'id': page_record(2, '')['id'], 'properties': {'原标题': {'type': 'title', 'title': []}}}
        design = {'id': page_record(3, 'Field design')['id'],
                  'properties': {'原标题': {'type': 'title', 'title': rich('Field design')}}}
        api = FakeNotion(); api.original_rows = [page, empty, design]
        paper = read_paper(None, page, zotero_reader=lambda _: (self.raw_a, {'kind': 'zotero'}))
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(paper_relations, 'RUNTIME', Path(folder)), \
             patch.object(paper_relations, 'preflight', return_value={'original_source': {'id': 'original'}}), \
             patch.object(paper_relations, '_relation_config', return_value={
                 'relation_data_source_id': 'relations', 'overview_page_id': 'overview'}), \
             patch('relation_notion.upload_png', return_value='uploaded-file'):
            result = paper_relations.run(api=api, reader=lambda _, __: paper)
        self.assertEqual(result['status'], 'NO_PAIR')
        self.assertEqual((result['eligible'], result['pairs_examined'], result['published_total']), (1, 0, 0))
        self.assertEqual(len(api.pages), 0)
        self.assertEqual(len(api.children('overview')), 1)


if __name__ == '__main__': unittest.main()

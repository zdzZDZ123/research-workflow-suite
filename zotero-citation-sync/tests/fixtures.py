"""Synthetic parser fixtures only. These fields are NOT official Zotero compatibility evidence."""
import json
import zipfile
from xml.sax.saxutils import escape


def paper(key='PAPERBBB', doi='10.5555/synthetic-b', title='Synthetic paper B'):
    return {'key': key, 'version': 1, 'library': {'type': 'user', 'id': 123},
            'data': {'key': key, 'itemType': 'journalArticle', 'DOI': doi, 'title': title,
                     'creators': [{'lastName': 'Synthetic'}], 'date': '2020', 'abstractNote': 'Fixture evidence only.'}}


def annotation(comment='[cite]\nDOI: 10.5555/synthetic-b\nTarget: A target statement.', key='ANNOTAAA'):
    return {'key': key, 'version': 1, 'data': {'key': key, 'itemType': 'annotation', 'parentItem': 'ATTACAAA',
                                             'annotationComment': comment, 'annotationText': 'Source A quote', 'annotationPageLabel': '3'}}


def snapshot(extra=(), comment=None):
    source = paper('PAPERAAA', '10.5555/synthetic-a', 'Synthetic source A')
    attach = {'key': 'ATTACAAA', 'version': 1, 'data': {'itemType': 'attachment', 'parentItem': 'PAPERAAA'}}
    return {'scope': 'test-scope', 'server_id': 'TEST', 'version': '1', 'library': 'users/0', 'complete': True,
            'connection': {'available': True, 'version': 'fixture', 'api_version': '3'},
            'items': [source, paper(), attach, annotation() if comment is None else annotation(comment), *extra]}


def run(text):
    return '<w:r><w:t xml:space="preserve">' + escape(text) + '</w:t></w:r>'


def field(key='PAPERBBB', cid='test-citation-1', display='[1]', options=None):
    item = {'id': 1, 'uris': ['http://zotero.org/users/123/items/' + key], **(options or {})}
    payload = {'citationID': cid, 'citationItems': [item], 'properties': {'plainCitation': display}}
    code = ' ADDIN ZOTERO_ITEM CSL_CITATION ' + json.dumps(payload) + ' '
    return ('<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve">' + escape(code)
            + '</w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r>' + run(display)
            + '<w:r><w:fldChar w:fldCharType="end"/></w:r>')


def bibliography():
    return ('<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
            '<w:r><w:instrText> ADDIN ZOTERO_BIBL {} </w:instrText></w:r>'
            '<w:r><w:fldChar w:fldCharType="separate"/></w:r>' + run('Synthetic test bibliography')
            + '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>')


def docx(path, paras=None, bib=False, style='apa', extra_parts=None, suffix=''):
    paras = paras if paras is not None else [run('A target statement.')]
    body = ''.join('<w:p>' + p + '</w:p>' for p in paras) + (bibliography() if bib else '') + suffix
    main = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
            'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"><w:body>'
            + body + '<w:sectPr/></w:body></w:document>')
    prefs = escape(json.dumps({'style': {'styleID': 'http://www.zotero.org/styles/' + style}}))
    custom = ('<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties" '
              'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
              '<property name="ZOTERO_PREF_1"><vt:lpwstr>' + prefs + '</vt:lpwstr></property></Properties>')
    parts = {'word/document.xml': main, 'docProps/custom.xml': custom,
             '[Content_Types].xml': '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
             '_rels/.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'}
    parts.update(extra_parts or {})
    with zipfile.ZipFile(path, 'w') as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    return path


class FakeAPI:
    def __init__(self, data=None):
        self.data = data or snapshot()
        self.server_id = self.data['server_id']
    def snapshot(self):
        return self.data
    def probe(self):
        self.server_id = self.data['server_id']
        return self.data['connection']
    def get(self, *args, **kwargs):
        return [], {'last-modified-version': self.data['version']}

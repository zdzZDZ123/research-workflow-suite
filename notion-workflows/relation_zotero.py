"""Read a single Zotero collection and reconcile its papers with Notion."""
import re
import urllib.parse

from notion_sync import StopSync, plain
from paper_reading import Zotero, get_json, rich
from relation_evidence import paper_metadata, read_paper


PAPER_TYPES = {'journalArticle', 'conferencePaper', 'preprint', 'report', 'bookSection', 'thesis'}


def collection_items(collection_key, library_type='user', library_id='0'):
    if not re.fullmatch(r'[A-Z0-9]{8}', collection_key):
        raise ValueError('ZOTERO_COLLECTION_KEY_INVALID')
    zotero = Zotero(library_type, library_id)
    collection = get_json(zotero.base + '/collections/' + collection_key)
    rows = []
    while True:
        query = urllib.parse.urlencode({'limit': 100, 'start': len(rows)})
        batch = get_json(zotero.base + '/collections/' + collection_key + '/items/top?' + query)
        rows.extend(batch)
        if len(batch) < 100:
            break
    papers = [row for row in rows if row.get('data', {}).get('itemType') in PAPER_TYPES
              and row['data'].get('title', '').strip()]
    return collection, papers


def identity(item):
    library = item['library']
    if library['type'] not in ('user', 'group') or not str(library['id']).isdigit():
        raise ValueError('ZOTERO_LIBRARY_IDENTITY_INVALID')
    return f"{library['type']}:{library['id']}:{item['key']}"


def provisional_page(item):
    """A local source record for PDF validation; never sent to Notion."""
    return {'id': 'zotero-' + identity(item), 'properties': {
        '原标题': {'type': 'title', 'title': rich(item['data']['title'])},
        'Paper Key': {'type': 'rich_text', 'rich_text': rich(identity(item))},
        'DOI': {'type': 'url', 'url': ('https://doi.org/' + item['data']['DOI']) if item['data'].get('DOI') else None},
        'PDF URL': {'type': 'url', 'url': None}}}


def reconcile(api, source_id, item, existing_rows):
    ident = identity(item)
    matches = [row for row in existing_rows if paper_metadata(row)['identity'] == ident]
    if len(matches) > 1:
        raise StopSync('ZOTERO_NOTION_DUPLICATE_IDENTITY: ' + ident)
    if matches:
        return matches[0], False
    props = {'原标题': {'title': rich(item['data']['title'])},
             'Paper Key': {'rich_text': rich(ident)}}
    if item['data'].get('DOI'):
        props['DOI'] = {'url': 'https://doi.org/' + item['data']['DOI']}
    try:
        page = api.request('POST', 'pages', {'parent': {'type': 'data_source_id', 'data_source_id': source_id},
                                             'properties': props})
    except StopSync as error:
        if 'NOTION_OUTCOME_UNKNOWN' not in str(error):
            raise
        matches = [row for row in api.rows(source_id) if paper_metadata(row)['identity'] == ident]
        if len(matches) != 1:
            raise StopSync('ZOTERO_NOTION_CREATE_OUTCOME_UNKNOWN: ' + ident) from None
        page = matches[0]
    check = api.request('GET', 'pages/' + page['id'])
    if paper_metadata(check)['identity'] != ident:
        raise StopSync('ZOTERO_NOTION_IDENTITY_READBACK_MISMATCH: ' + ident)
    if not any(row['id'] == check['id'] for row in existing_rows):
        existing_rows.append(check)
    return check, True


def collection_papers(api, source_id, collection_key, existing_rows):
    collection, items = collection_items(collection_key)
    readable, skipped = [], []
    for item in items:
        ident = identity(item)
        try:
            # Check the actual PDF before creating an original-library record.
            paper = read_paper(api, provisional_page(item))
            notion_page, _ = reconcile(api, source_id, item, existing_rows)
            paper.update(page_id=notion_page['id'], url=notion_page.get('url') or
                         'https://www.notion.so/' + notion_page['id'].replace('-', ''))
            readable.append(paper)
        except StopSync:
            raise
        except Exception as error:
            skipped.append({'zotero_identity': ident, 'reason': str(error)[:240]})
    return collection, items, readable, skipped

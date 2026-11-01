"""On-demand, incremental cross-paper analysis for the existing Notion library."""
import argparse
import getpass
import hashlib
import json
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from notion_sync import Notion, StopSync, lock, preflight
from paper_reading import ROOT
from relation_evidence import (eligible_record, read_paper, evidence_cards,
                               compare_cards, paper_metadata, paper_title, notion_pdf_urls)
from relation_model import CompatibleModel, ModelError, load_config, validate_config
from relation_notion import RelationPublisher, RelationStore, bootstrap, validate_schema
from relation_zotero import collection_papers, collection_items, provisional_page
from secure_credentials import MODEL_TARGET, model_api_key, save_secret


RUNTIME = ROOT / '.runtime'
MODEL_CONFIG = RUNTIME / 'relation-model.json'
NOTION_CONFIG = RUNTIME / 'relation-notion.json'
COLLECTION_CONFIG = RUNTIME / 'relation-zotero.json'
MAX_NEW_PAIRS = 20


def _original_config():
    return json.loads((ROOT / 'deep-reading-database.json').read_text(encoding='utf-8'))


def _relation_config(api, original_source_id, create=False, overview_page_id=None):
    if create:
        return bootstrap(api, original_source_id, NOTION_CONFIG, overview_page_id)
    if not NOTION_CONFIG.exists():
        raise StopSync('RELATION_NOTION_SETUP_REQUIRED: run setup first')
    return validate_schema(api, json.loads(NOTION_CONFIG.read_text(encoding='utf-8')), original_source_id)


def _source_digest(a, b):
    return hashlib.sha256((a['pdf_sha256'] + b['pdf_sha256']).encode()).hexdigest()


def _pair_key(a, b):
    return '|'.join(sorted((a['page_id'], b['page_id'])))


def _eligible(api, page):
    if eligible_record(page):
        return True
    # A PDF block is also a stable source, even when the database row has no
    # Zotero key or PDF URL property. Never count title-only design rows.
    return bool(paper_title(page) and notion_pdf_urls(api, page['id']))


def _cards(store, model, paper):
    cached = store.db.execute('SELECT pdf_hash,cards FROM relation_papers WHERE page_id=?', (paper['page_id'],)).fetchone()
    if cached and cached['pdf_hash'] == paper['pdf_sha256']:
        return json.loads(cached['cards'])
    cards = evidence_cards(model, paper)
    with store.db:
        store.db.execute('INSERT OR REPLACE INTO relation_papers VALUES(?,?,?,?)',
                         (paper['page_id'], paper['pdf_sha256'], json.dumps(cards, ensure_ascii=False),
                          json.dumps(paper['source'], ensure_ascii=False)))
    return cards


def run(api=None, model=None, max_pairs=MAX_NEW_PAIRS, create_schema=True, reader=read_paper,
        collection_key=None):
    api = api or Notion()
    RUNTIME.mkdir(exist_ok=True)
    if collection_key is None and (RUNTIME / 'relation-zotero.json').exists():
        collection_key = json.loads((RUNTIME / 'relation-zotero.json').read_text(encoding='utf-8'))['collection_key']
    with lock(RUNTIME), closing(RelationStore(RUNTIME / 'state.sqlite3').db) as db:
        store = RelationStore.__new__(RelationStore); store.db = db
        original = preflight(api, _original_config())['original_source']
        config = _relation_config(api, original['id'], create=create_schema)
        config['backup_dir'] = str(RUNTIME / 'relation-backups')
        publisher = RelationPublisher(api, config, store)
        rows = api.rows(original['id'])
        if collection_key:
            if model is None:
                model = CompatibleModel(load_config(MODEL_CONFIG))
                model.preflight()
            collection, items, readable, skipped = collection_papers(api, original['id'], collection_key, rows)
            eligible_count = len(items)
            metadata = [{k: p[k] for k in ('page_id', 'title', 'url')} for p in readable]
        else:
            eligible = [r for r in rows if not r.get('archived') and not r.get('in_trash') and _eligible(api, r)]
            skipped = [{'page_id': r['id'], 'reason': 'NOT_A_PAPER_OR_NO_SOURCE'} for r in rows if r not in eligible]
            metadata = [paper_metadata(r) for r in eligible]
            readable = []
            for page in eligible:
                try:
                    readable.append(reader(api, page))
                except Exception as error:
                    skipped.append({'page_id': page['id'], 'reason': str(error)[:240]})
            eligible_count = len(eligible)
        backlink_targets = [paper_metadata(r) for r in rows if not r.get('archived') and not r.get('in_trash')]
        readable.sort(key=lambda p: p['page_id'])
        valid_pairs = {}
        for index, a in enumerate(readable):
            for b in readable[index + 1:]:
                valid_pairs[(a['page_id'], b['page_id'])] = _source_digest(a, b)
        revoked = publisher.stale_unmatched(valid_pairs)
        if revoked:
            with db:
                for a_id, b_id in revoked:
                    db.execute("UPDATE relation_pairs SET status='stale' WHERE pair_key=?", (a_id + '|' + b_id,))
        pairs = []
        for index, a in enumerate(readable):
            for b in readable[index + 1:]:
                key = _pair_key(a, b)
                digest = valid_pairs[(a['page_id'], b['page_id'])]
                previous = db.execute('SELECT source_hash,status FROM relation_pairs WHERE pair_key=?', (key,)).fetchone()
                if not previous or previous['source_hash'] != digest or previous['status'] != 'committed':
                    pairs.append((key, digest, a, b))
        selected = pairs[:max_pairs]
        counts = {'eligible': eligible_count, 'readable': len(readable), 'skipped': len(skipped),
                  'pairs_examined': 0, 'auto_published': 0, 'pending': 0,
                  'remaining_pairs': len(pairs) - len(selected), 'new_records': 0}
        if selected:
            try:
                model = model or CompatibleModel(load_config(MODEL_CONFIG))
                if hasattr(model, 'preflight'):
                    model.preflight()
            except Exception:
                # Revoked evidence must leave the graph and backlinks even if
                # a new model judgment cannot yet be made.
                current = publisher.published()
                publisher.backlinks(backlink_targets, current)
                publisher.graph(metadata, current)
                raise
            for key, digest, a, b in selected:
                cards_a = _cards(store, model, a)
                cards_b = _cards(store, model, b)
                candidates = compare_cards(model, a, b, cards_a, cards_b)
                for candidate in candidates:
                    _, created = publisher.publish(candidate, a, b, model.config['model'] if hasattr(model, 'config') else 'test-model')
                    counts['new_records'] += bool(created)
                    counts['auto_published' if candidate['auto_publish'] else 'pending'] += bool(created)
                with db:
                    db.execute('INSERT OR REPLACE INTO relation_pairs VALUES(?,?,?,?)',
                               (key, digest, 'committed', datetime.now(timezone.utc).isoformat()))
                counts['pairs_examined'] += 1
        published = publisher.published()
        counts['backlinks_updated'] = publisher.backlinks(backlink_targets, published)
        counts['graph_updated'] = publisher.graph(metadata, published)
        counts['published_total'] = len(published)
        counts['status'] = 'NO_PAIR' if len(readable) < 2 else 'SYNCED' if counts['pairs_examined'] or counts['graph_updated'] else 'UNCHANGED'
        counts['url'] = 'https://www.notion.so/' + config['overview_page_id'].replace('-', '')
        if collection_key:
            counts['collection'] = {'key': collection_key, 'name': collection['data']['name']}
        counts['skipped_details'] = skipped
        return counts


def preflight_only(api=None):
    api = api or Notion()
    source = preflight(api, _original_config())['original_source']
    rows = api.rows(source['id'])
    result = {'status': 'PREFLIGHT_OK', 'original_rows': len(rows),
              'eligible_metadata': sum(_eligible(api, r) for r in rows if not r.get('archived') and not r.get('in_trash')),
              'model_configured': MODEL_CONFIG.exists(), 'model_key_available': bool(model_api_key()),
              'notion_setup': NOTION_CONFIG.exists()}
    if COLLECTION_CONFIG.exists():
        result['collection_key'] = json.loads(COLLECTION_CONFIG.read_text(encoding='utf-8'))['collection_key']
    if NOTION_CONFIG.exists():
        config = _relation_config(api, source['id'])
        result['overview_url'] = 'https://www.notion.so/' + config['overview_page_id'].replace('-', '')
    return result


def configure(base_url, model, api_style, api_key_stdin=False, prompt_api_key=False):
    config = validate_config({'base_url': base_url, 'model': model, 'api_style': api_style})
    RUNTIME.mkdir(exist_ok=True)
    path = MODEL_CONFIG.with_suffix('.tmp')
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
    path.replace(MODEL_CONFIG)
    if api_key_stdin or prompt_api_key:
        key = getpass.getpass('Model API key: ') if prompt_api_key else sys.stdin.readline().rstrip('\r\n')
        save_secret(MODEL_TARGET, key)
    return {'status': 'MODEL_CONFIGURED', 'api_style': api_style, 'model': model,
            'key_saved': bool(api_key_stdin or prompt_api_key)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('preflight')
    commands.add_parser('model-preflight')
    setup = commands.add_parser('setup'); setup.add_argument('--overview-page-id')
    running = commands.add_parser('run'); running.add_argument('--max-pairs', type=int, default=MAX_NEW_PAIRS)
    scope = running.add_mutually_exclusive_group()
    scope.add_argument('--collection-key')
    scope.add_argument('--original-library', action='store_true',
                       help='Check the existing Notion paper library, ignoring the saved Zotero collection')
    preview = commands.add_parser('collection-preview'); preview.add_argument('--collection-key', required=True)
    selected_collection = commands.add_parser('use-collection'); selected_collection.add_argument('--collection-key', required=True)
    configuring = commands.add_parser('configure')
    configuring.add_argument('--base-url', required=True)
    configuring.add_argument('--model', required=True)
    configuring.add_argument('--api-style', choices=['chat_completions', 'responses'], required=True)
    configuring.add_argument('--api-key-stdin', action='store_true')
    configuring.add_argument('--prompt-api-key', action='store_true')
    args = parser.parse_args()
    if args.command == 'preflight':
        return preflight_only()
    if args.command == 'collection-preview':
        collection, items = collection_items(args.collection_key)
        readable, skipped = [], []
        for item in items:
            try:
                paper = read_paper(None, provisional_page(item))
                readable.append({'zotero_key': item['key'], 'title': paper['title'],
                                 'pages': paper['page_count'], 'pdf_sha256': paper['pdf_sha256']})
            except Exception as error:
                skipped.append({'zotero_key': item['key'], 'reason': str(error)[:160]})
        return {'status': 'COLLECTION_READY', 'collection_key': args.collection_key,
                'collection_name': collection['data']['name'], 'papers': len(items),
                'readable': len(readable), 'skipped': len(skipped), 'pdfs': readable,
                'skipped_details': skipped}
    if args.command == 'use-collection':
        collection, items = collection_items(args.collection_key)
        RUNTIME.mkdir(exist_ok=True)
        COLLECTION_CONFIG.write_text(json.dumps({'collection_key': args.collection_key}, indent=2), encoding='utf-8')
        return {'status': 'COLLECTION_SELECTED', 'collection_key': args.collection_key,
                'collection_name': collection['data']['name'], 'papers': len(items)}
    if args.command == 'model-preflight':
        model = CompatibleModel(load_config(MODEL_CONFIG))
        model.preflight()
        return {'status': 'MODEL_PREFLIGHT_OK', 'model': model.config['model'],
                'api_style': model.config['api_style']}
    if args.command == 'configure':
        if args.api_key_stdin and args.prompt_api_key:
            raise ValueError('Choose only one API key input method')
        return configure(args.base_url, args.model, args.api_style, args.api_key_stdin, args.prompt_api_key)
    if args.command == 'setup':
        api = Notion(); original = preflight(api, _original_config())['original_source']
        config = _relation_config(api, original['id'], create=True, overview_page_id=args.overview_page_id)
        return {'status': 'SETUP_READY', 'overview_page_id': config['overview_page_id'],
                'relation_database_id': config['relation_database_id'],
                'relation_data_source_id': config['relation_data_source_id']}
    if not 1 <= args.max_pairs <= 20:
        raise ValueError('--max-pairs must be 1..20')
    return run(max_pairs=args.max_pairs,
               collection_key='' if args.original_library else args.collection_key)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        result = main()
    except Exception as error:
        result = {'status': 'PAUSED', 'error': str(error)}
    print(json.dumps(result, ensure_ascii=False))

"""Interactive setup for a new user's Notion workspace; no bundled account IDs."""
import getpass
import json
import re
from pathlib import Path
from notion_sync import Notion, StopSync, EXPECTED, preflight, lock
from paper_reading import ROOT, rich, get_json, Zotero
from secure_credentials import save_token
from relation_notion import bootstrap
from relation_zotero import collection_papers


def page_id(value):
    match = re.search(r"([a-fA-F0-9]{32}|[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12})(?:[?/#]|$)", value.strip())
    if not match:
        raise ValueError("Enter a Notion page URL or page ID")
    return match.group(1)


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def ensure_database(api, parent, title, properties, journal):
    # Inspect children even after a successful write: a crash before saving the
    # local ID must not create the database twice.
    matches = [b for b in api.children(parent) if b.get('type') == 'child_database'
               and b['child_database']['title'] == title]
    if len(matches) > 1:
        raise StopSync('SETUP_DATABASE_AMBIGUOUS: ' + title)
    if matches:
        result = api.request('GET', 'databases/' + matches[0]['id'])
    else:
        if journal.exists():
            raise StopSync('SETUP_WRITE_UNKNOWN: inspect Notion before retry: ' + title)
        save(journal, {'parent': parent, 'title': title})
        try:
            result = api.request('POST', 'databases', {
                'parent': {'type': 'page_id', 'page_id': parent}, 'title': rich(title),
                'initial_data_source': {'properties': properties}})
        except StopSync as error:
            if 'NOTION_OUTCOME_UNKNOWN' not in str(error):
                journal.unlink(missing_ok=True)
            raise
    sources = result.get('data_sources', [])
    if len(sources) != 1:
        raise StopSync('SETUP_DATA_SOURCE_AMBIGUOUS')
    journal.unlink(missing_ok=True)
    return result['id'], sources[0]['id']


def initialize(api, parent, root=ROOT):
    runtime = root / '.runtime'
    runtime.mkdir(exist_ok=True)
    config_path = root / 'deep-reading-database.json'
    with lock(runtime):
        if config_path.exists():
            config = json.loads(config_path.read_text(encoding='utf-8'))
            preflight(api, config)
            return config
        state_path = runtime / 'setup-state.json'
        state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {'parent': parent}
        if state['parent'] != parent:
            raise StopSync('SETUP_PARENT_CHANGED: finish existing setup first')
        api.request('GET', 'pages/' + parent)
        if 'original_database_id' not in state:
            original = {'原标题': {'title': {}}, 'Paper Key': {'rich_text': {}},
                        'DOI': {'url': {}}, 'PDF URL': {'url': {}}}
            state['original_database_id'], state['original_data_source_id'] = ensure_database(
                api, parent, '原论文库（Research Workflow Suite）', original, runtime / 'setup-original.intent.json')
            save(state_path, state)
        if 'database_id' not in state:
            props = {name: {kind: {}} for name, kind in EXPECTED.items()}
            props['关联论文'] = {'relation': {'data_source_id': state['original_data_source_id'],
                'type': 'single_property', 'single_property': {}}}
            state['database_id'], state['data_source_id'] = ensure_database(
                api, parent, '单篇论文精读知识库（Research Workflow Suite）', props, runtime / 'setup-reading.intent.json')
            save(state_path, state)
        config = {k: v for k, v in state.items() if k != 'parent'}
        preflight(api, config)
        # Use the explicitly connected parent as the overview. Relations are
        # created only there; never search another person's similarly named page.
        bootstrap(api, state['original_data_source_id'], runtime / 'relation-notion.json', parent)
        save(config_path, config)
        return config


def main():
    print('Research Workflow Suite: Notion configuration. Enter q to skip.')
    answer = input('Configure Notion now? [y/N] ').strip().lower()
    if answer != 'y':
        return
    secret = getpass.getpass('Your Notion integration token (blank = saved credential): ')
    if secret:
        save_token(secret)
    api = Notion()
    config_file = ROOT / 'deep-reading-database.json'
    if config_file.exists():
        config = json.loads(config_file.read_text(encoding='utf-8'))
        preflight(api, config)
    else:
        parent = page_id(input('Connected parent page URL or ID: '))
        print('Create paper library, reading index and relation database under this page.')
        if input('Confirm [y/N]: ').strip().lower() != 'y':
            return
        config = initialize(api, parent)
    print('Notion schema verified.')
    if input('Select Zotero collection now? [y/N] ').strip().lower() != 'y':
        return
    rows = []; z = Zotero()
    while True:
        batch = get_json(z.base + '/collections?limit=100&start=' + str(len(rows)))
        rows.extend(batch)
        if len(batch) < 100: break
    for i, row in enumerate(rows, 1):
        print(i, row['data']['name'], '[' + row['key'] + ']')
    choice = int(input('Collection number: '))
    if choice < 1 or choice > len(rows): raise ValueError('Invalid collection number')
    key = rows[choice - 1]['key']
    save(ROOT / '.runtime/relation-zotero.json', {'collection_key': key})
    if input('Register readable PDF papers in your Notion library (metadata only)? [y/N] ').strip().lower() == 'y':
        with lock(ROOT / '.runtime'):
            _, items, readable, skipped = collection_papers(api, config['original_data_source_id'], key,
                                                          api.rows(config['original_data_source_id']))
        print(json.dumps({'papers': len(items), 'readable': len(readable), 'skipped': skipped}, ensure_ascii=False))
    print('Setup saved. Configure your compatible model API before relationship analysis.')


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print('Setup paused:', str(error))
        raise SystemExit(1)

"""Official Notion API sync with durable intent journal and immutable report versions.

No title/DOI matching, no schema/Relation writes, no whole-page replacement.
An ambiguous mutation is reconciled from reads, never blindly repeated.
"""
import argparse
import copy
import hashlib
import http.client
import json
import os
import sqlite3
import ssl
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager, closing
from datetime import datetime, timezone
from pathlib import Path
from paper_reading import ROOT, Zotero, choose_note, prepare, rich, block
from secure_credentials import notion_token
from visual_report import RENDERER, render
from paper_properties import desired_properties, fill_properties, inspect_fulltext

API_VERSION = '2025-09-03'
EXPECTED = {'论文名称': 'title', '关联论文': 'relation', '精读状态': 'select',
            '核心研究问题': 'rich_text', '核心研究发现': 'rich_text', '学术评价': 'rich_text',
            '证据核查状态': 'select', 'Zotero URI': 'url', 'Zotero Library ID': 'rich_text',
            'Zotero Item Key': 'rich_text', 'Paper Chat Note Key': 'rich_text',
            '精读版本': 'number', '最近同步时间': 'date', '同步日志': 'rich_text'}


class StopSync(RuntimeError): pass


def plain(prop):
    kind = prop.get('type')
    if kind == 'url': return prop.get('url') or ''
    return ''.join(s.get('plain_text', s.get('text', {}).get('content', '')) for s in prop.get(kind, [])) if kind in ('title', 'rich_text') else ''


def normalize_id(value): return str(value).replace('-', '').lower()


class Notion:
    def __init__(self):
        self.token = notion_token()
        if not self.token: raise StopSync('LOCAL_API_CREDENTIAL_REQUIRED: set NOTION_TOKEN in the local process or user environment')
        self.last_request = 0

    def request(self, method, path, body=None):
        # Retry read/query requests and explicit 429 responses only. Other mutation
        # outcomes may have succeeded remotely and must first be reconciled.
        read = method == 'GET' or path.endswith('/query') or path == 'search'
        for attempt in range(4):
            time.sleep(max(0, 0.36 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            req = urllib.request.Request('https://api.notion.com/v1/' + path,
                  data=None if body is None else json.dumps(body, ensure_ascii=False).encode(), method=method,
                  headers={'Authorization': 'Bearer ' + self.token, 'Notion-Version': API_VERSION, 'Content-Type': 'application/json'})
            try:
                with urllib.request.urlopen(req, timeout=30) as response: return json.load(response)
            except urllib.error.HTTPError as error:
                if attempt < 3 and (error.code == 429 or (read and error.code >= 500)):
                    time.sleep(min(15, max(1, float(error.headers.get('Retry-After', 2 ** attempt)))))
                    continue
                # Do not log request bodies, credentials, or returned private content.
                raise StopSync(f'NOTION_HTTP_{error.code}: {method} {path}') from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, ssl.SSLError, http.client.HTTPException) as error:
                if read and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                reason=getattr(error,'reason',error)
                if isinstance(reason,(socket.gaierror,ConnectionRefusedError)):
                    raise StopSync(f'NOTION_NOT_SENT: {method} {path}') from None
                raise StopSync(f'NOTION_OUTCOME_UNKNOWN: {method} {path}') from None

    def all(self, path, method='GET', body=None):
        result, cursor = [], None
        while True:
            args = dict(body or {}, page_size=100)
            if cursor: args['start_cursor'] = cursor
            data = self.request(method, path + ('?' + urllib.parse.urlencode(args) if method == 'GET' else ''), None if method == 'GET' else args)
            result.extend(data['results'])
            if not data.get('has_more'): return result
            cursor = data['next_cursor']

    def children(self, block_id): return self.all(f'blocks/{block_id}/children')
    def rows(self, source, filter=None): return self.all(f'data_sources/{source}/query', 'POST', {'filter': filter} if filter else {})


@contextmanager
def lock(folder):
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / 'sync.lock', 'a+b') as f:
        f.seek(0); f.write(b'0'); f.flush(); f.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError: raise StopSync('SYNC_ALREADY_RUNNING') from None
        try: yield
        finally:
            f.seek(0)
            if os.name == 'nt': msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else: fcntl.flock(f, fcntl.LOCK_UN)


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS mappings(identity TEXT PRIMARY KEY, note_key TEXT, original_page TEXT, page_id TEXT, hash TEXT, version INTEGER, synced TEXT);
        CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY, identity TEXT, hash TEXT, version INTEGER, state TEXT, data TEXT, error TEXT);
        CREATE TABLE IF NOT EXISTS versions(identity TEXT, version INTEGER, hash TEXT, container TEXT, data TEXT, PRIMARY KEY(identity,version));
        ''')

    def mapping(self, identity):
        row = self.db.execute('SELECT * FROM mappings WHERE identity=?', (identity,)).fetchone()
        return dict(row) if row else None

    def pending(self, identity):
        rows = self.db.execute("SELECT * FROM attempts WHERE identity=? AND state != 'committed'", (identity,)).fetchall()
        if len(rows) > 1: raise StopSync('MULTIPLE_PENDING_ATTEMPTS')
        return json.loads(rows[0]['data']) if rows else None

    def save(self, attempt, error=None):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO attempts VALUES(?,?,?,?,?,?,?)',
                (attempt['id'], attempt['identity'], attempt['hash'], attempt['version'], attempt['state'], json.dumps(attempt, ensure_ascii=False), error))

    def commit(self, a):
        a['state'] = 'committed'
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO mappings VALUES(?,?,?,?,?,?,?)',
                (a['identity'], a['note_key'], a['original_page'], a['page_id'], a['hash'], a['version'], a['synced']))
            self.db.execute('INSERT OR REPLACE INTO versions VALUES(?,?,?,?,?)',
                (a['identity'], a['version'], a['hash'], a['container'], json.dumps(a, ensure_ascii=False)))
            self.db.execute("UPDATE attempts SET state='committed',data=?,error=NULL WHERE id=?", (json.dumps(a, ensure_ascii=False), a['id']))


def preflight(api, config):
    result = {}
    for prefix in ('original_', ''):
        database = api.request('GET', 'databases/' + config[prefix + 'database_id'])
        sources = database.get('data_sources', [])
        configured = config.get(prefix + 'data_source_id')
        candidates = [s for s in sources if not configured or normalize_id(s['id']) == normalize_id(configured)]
        if len(candidates) != 1: raise StopSync('DATA_SOURCE_SELECTION_REQUIRED: ' + prefix)
        source = api.request('GET', 'data_sources/' + candidates[0]['id'])
        result[prefix + 'source'] = source
    props = result['source']['properties']
    for name, kind in EXPECTED.items():
        if props.get(name, {}).get('type') != kind: raise StopSync('SCHEMA_MISMATCH: ' + name)
    relation = props['关联论文']['relation']
    target = relation.get('data_source_id')
    if relation.get('type') != 'single_property' or normalize_id(target) != normalize_id(result['original_source']['id']):
        raise StopSync('RELATION_MISMATCH: expected one-way relation to the original data source')
    return result


def paper_identity(paper):
    return f"{paper['library']['type']}:{paper['library']['id']}:{paper['key']}"


def stable_match(props, paper):
    kind, library_id, key = paper['library']['type'], str(paper['library']['id']), paper['key']
    library = plain(props.get('Zotero Library ID', {}))
    # Bare account IDs omit library type and are insufficient across user/group scopes.
    if library == f'{kind}:{library_id}' and plain(props.get('Zotero Item Key', {})) == key: return True
    for name in ('Zotero URI', 'Paper Key'):
        value = plain(props.get(name, {}))
        if value == f'{kind}:{library_id}:{key}': return True
        expected = f'https://www.zotero.org/{kind}s/{library_id}/items/{key}'
        if value.rstrip('/') == expected: return True
        if kind == 'group' and value == f'zotero://select/groups/{library_id}/items/{key}': return True
    return False


def match_original(api, source, paper):
    # Query full identity only; no title or DOI fallback and no original-library mutation.
    matches = [p for p in api.rows(source['id']) if stable_match(p['properties'], paper)]
    if len(matches) != 1:
        raise StopSync('ORIGINAL_MATCH_CONFLICT' if matches else 'ORIGINAL_PAPER_NOT_FOUND_BY_STABLE_ID: check Notero destination; original library is never populated automatically')
    return matches[0]['id']


def canonical_span(s):
    kind = s['type']
    if kind == 'text':
        value = {'type': kind, 'text': {'content': s['text']['content']}}
        if s['text'].get('link'): value['text']['link'] = s['text']['link']
        styles = {k: v for k, v in s.get('annotations', {}).items() if v and v != 'default'}
        if styles: value['annotations'] = styles
        return value
    if kind == 'equation': return {'type': kind, 'equation': s['equation']}
    raise StopSync('UNEXPECTED_RICH_TEXT_TYPE: ' + kind)


def canonical(b):
    kind = b['type']; data = b[kind]
    value = {'type': kind, kind: {}}
    for k, v in data.items():
        if k == 'rich_text': value[kind][k] = canonical_rich(v)
        elif k == 'cells': value[kind][k] = [canonical_rich(cell) for cell in v]
        elif k not in ('children', 'color', 'is_toggleable'):
            # Notion may materialize optional defaults absent from the write payload.
            if v is not None and not (k == 'caption' and v == []): value[kind][k] = v
    return value


def canonical_rich(spans):
    result = []
    for raw in spans:
        s = canonical_span(raw)
        if result and s['type'] == result[-1]['type'] == 'text' and s.get('annotations') == result[-1].get('annotations') and s['text'].get('link') == result[-1]['text'].get('link'):
            result[-1]['text']['content'] += s['text']['content']
        else: result.append(s)
    return result


def reconcile_children(api, parent, expected):
    """Only append missing content after verifying the full existing prefix.

    A lost append response is safe: the next run reads the actual prefix first.
    User edits or extra blocks cause a conflict, never a destructive rewrite.
    """
    current = api.children(parent)
    if len(current) > len(expected): raise StopSync('STAGING_CONTENT_CONFLICT: extra blocks')
    for i, actual in enumerate(current):
        if canonical(actual) != canonical(expected[i]): raise StopSync('STAGING_CONTENT_CONFLICT: modified block')
    if len(current) < len(expected):
        # Limit each payload to 30 shells and keep nested insertion to one level.
        for offset in range(len(current), len(expected), 30):
            shells = []
            for b in expected[offset:offset+30]:
                shell = copy.deepcopy(b); data = shell[shell['type']]
                nested = data.pop('children', None)
                if shell['type'] == 'table':
                    if not nested: raise StopSync('EMPTY_TABLE')
                    data['children'] = nested[:1]  # Notion requires one initial table row.
                elif shell['type'] == 'column_list':
                    # Columns must be created together, each with an initial child.
                    data['children'] = []
                    for column in nested:
                        col = copy.deepcopy(column)
                        col['column']['children'] = col['column']['children'][:1]
                        data['children'].append(col)
                shells.append(shell)
            api.request('PATCH', f'blocks/{parent}/children', {'children': shells})
        current = api.children(parent)
    if len(current) != len(expected): raise StopSync('STAGING_READBACK_INCOMPLETE')
    for actual, desired in zip(current, expected):
        if canonical(actual) != canonical(desired): raise StopSync('STAGING_READBACK_MISMATCH')
        nested = desired[desired['type']].get('children')
        if nested: reconcile_children(api, actual['id'], nested)
        elif actual.get('has_children'): raise StopSync('STAGING_CONTENT_CONFLICT: unexpected children')


def rich_property(value): return {'rich_text': rich(value)}


def report_properties(blocks):
    """Copy excerpts, without model inference or claims of evidence verification."""
    targets = {'研究问题与研究假设':'核心研究问题', '图表、公式及实验结果':'核心研究发现',
               '研究局限与批判性思考':'学术评价'}
    result, current = {}, None
    for b in blocks:
        kind = b['type']
        text = plain({'type':'rich_text','rich_text':b[kind].get('rich_text',[])})
        if kind == 'heading_1': current = targets.get(text)
        elif current and kind in ('paragraph','bulleted_list_item','numbered_list_item') and text and not text.startswith('原报告没有'):
            result[current] = rich_property('原报告摘录：' + text[:1600] + ('〔完整内容见正文〕' if len(text)>1600 else ''))
            current = None
    return result


def identity_properties(paper, note_key, original_page):
    kind, library = paper['library']['type'], paper['library']['id']
    return {'论文名称': {'title': rich(paper['data']['title'])},
            '关联论文': {'relation': [{'id': original_page}]},
            'Zotero Library ID': rich_property(f'{kind}:{library}'),
            'Zotero Item Key': rich_property(paper['key']), 'Paper Chat Note Key': rich_property(note_key),
            'Zotero URI': {'url': f'https://www.zotero.org/{kind}s/{library}/items/{paper["key"]}'}}


def find_deep(api, source, paper):
    rows = api.rows(source, {'and': [
        {'property': 'Zotero Library ID', 'rich_text': {'equals': f"{paper['library']['type']}:{paper['library']['id']}"}},
        {'property': 'Zotero Item Key', 'rich_text': {'equals': paper['key']}}]})
    if len(rows) > 1: raise StopSync('DUPLICATE_DEEP_PAGES')
    return rows[0] if rows else None


def snapshot_tree(api, parent):
    rows = api.children(parent)
    for b in rows:
        if b.get('has_children'):
            b[b['type']]['children'] = snapshot_tree(api, b['id'])
    return rows


def same_tree(actual, expected):
    return len(actual) == len(expected) and all(
        canonical(a) == canonical(b) and same_tree(a[a['type']].get('children', []), b[b['type']].get('children', []))
        for a, b in zip(actual, expected))


def validate_relation(api, page, original, paper, original_source):
    if page.get('archived') or page.get('in_trash') or not stable_match(page['properties'], paper):
        raise StopSync('BOUND_PAGE_IDENTITY_CHANGED')
    rel = page['properties'].get('关联论文', {}).get('relation', [])
    if len(rel) != 1 or normalize_id(rel[0]['id']) != normalize_id(original):
        raise StopSync('BOUND_PAGE_RELATION_CHANGED')
    target = api.request('GET', 'pages/' + rel[0]['id'])
    if target.get('archived') or target.get('in_trash') or not stable_match(target['properties'], paper):
        raise StopSync('BODY_PAGE_IDENTITY_CHANGED')
    if normalize_id(target.get('parent', {}).get('data_source_id')) != normalize_id(original_source):
        raise StopSync('BODY_PAGE_SOURCE_CHANGED')
    return target['id']


def backup_pages(api, folder, attempt):
    dest = Path(folder) / 'page-backups' / attempt['id']
    dest.mkdir(parents=True, exist_ok=True)
    for pid in (attempt['page_id'], attempt['body_page_id']):
        path = dest / (normalize_id(pid) + '.json')
        if not path.exists():
            data = {'page': api.request('GET', 'pages/' + pid), 'children': snapshot_tree(api, pid)}
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(path)


def retire_old_reports(api, db, a, folder):
    """Reversibly archive only journal-owned, unmodified report roots.

    Never delete individual blocks. Their IDs and comments remain recoverable.
    User edits inside an old report cause retention, never replacement.
    """
    for row in db.execute('SELECT data FROM versions WHERE identity=? AND version<?', (a['identity'], a['version'])):
        old = json.loads(row['data'])
        parent = old.get('body_page_id', old['page_id'])
        found = [b for b in api.children(parent) if b['id'] == old['container']]
        if not found: continue
        root = found[0]
        expected_marker = old.get('root_marker', f'Paper Deep Reading · v{old["version"]} · {old["id"]}')
        if plain({'type':'rich_text','rich_text':root[root['type']].get('rich_text',[])}) != expected_marker:
            a['warnings'].append(f'保留已编辑的历史容器 v{old["version"]}'); continue
        children = snapshot_tree(api, root['id'])
        if not same_tree(children, old['blocks']):
            a['warnings'].append(f'保留含用户改动的历史报告 v{old["version"]}'); continue
        dest = Path(folder) / 'page-backups' / a['id'] / (root['id'] + '-retired.json')
        dest.write_text(json.dumps({'root':root, 'children':children}, ensure_ascii=False, indent=2), encoding='utf-8')
        api.request('PATCH', 'blocks/' + root['id'], {'archived': True})


def sync(api, config, paper, note, folder, rollback_version=None):
    folder = Path(folder)
    with lock(folder), closing(Store(folder / 'state.sqlite3').db) as db:
        store = Store.__new__(Store); store.db = db
        schema = preflight(api, config)
        identity = paper_identity(paper)
        previous = store.mapping(identity)
        last_row = db.execute('SELECT data FROM versions WHERE identity=? AND version=?',
                              (identity, previous['version'] if previous else 0)).fetchone()
        last = json.loads(last_row['data']) if last_row else {}
        if previous and previous['note_key'] != note['key']: raise StopSync('BOUND_NOTE_CHANGED: explicit rebind required')
        original = match_original(api, schema['original_source'], paper)
        if previous and normalize_id(previous['original_page']) != normalize_id(original): raise StopSync('ORIGINAL_MAPPING_CONFLICT')
        prepared = prepare(paper, note, folder)
        payload = json.loads((Path(prepared['path']) / 'notion-preview.json').read_text(encoding='utf-8'))
        attribute_props, payload['property_overview'] = desired_properties(paper, payload, schema['original_source']['properties'], folder, paper.get('_attachments', []))
        a = store.pending(identity)
        if rollback_version is not None:
            if a: raise StopSync('FINISH_PENDING_SYNC_BEFORE_ROLLBACK')
            row = db.execute('SELECT data FROM versions WHERE identity=? AND version=?', (identity, rollback_version)).fetchone()
            if not row: raise StopSync('ROLLBACK_VERSION_NOT_FOUND')
            old = json.loads(row['data'])
            payload['sha256'], payload['blocks'] = old['hash'], old.get('source_blocks', old['blocks'])
        if a and (a['hash'] != payload['sha256'] or a['note_key'] != note['key']):
            raise StopSync('PENDING_SOURCE_CHANGED: retry with the saved source before advancing')
        existing = find_deep(api, schema['source']['id'], paper)
        if previous and (not existing or normalize_id(existing['id']) != normalize_id(previous['page_id'])):
            raise StopSync('REMOTE_MAPPING_CHANGED')
        if existing:
            validate_relation(api, api.request('GET','pages/'+existing['id']), original, paper, schema['original_source']['id'])
        if not a and previous and previous['hash'] == payload['sha256'] and last.get('renderer') == RENDERER and last.get('body_page_id') == original:
            remote_version = existing['properties'].get('精读版本', {}).get('number')
            if remote_version != previous['version']: raise StopSync('REMOTE_VERSION_CHANGED')
            roots = api.children(original)
            if not any(b['id'] == last['container'] for b in roots): raise StopSync('MANAGED_REPORT_MISSING')
            fill_properties(api, db, identity, original, attribute_props, folder)
            return {'status': 'UNCHANGED', 'page_id': original, 'index_page_id': previous['page_id'],
                    'url':'https://www.notion.so/'+normalize_id(original), 'version': previous['version']}
        if not a:
            if existing and not previous: raise StopSync('UNMAPPED_EXISTING_PAGE: explicit adoption required')
            a = {'id': str(uuid.uuid4()), 'identity': identity, 'hash': payload['sha256'],
                 'version': (previous['version'] if previous else 0) + 1,
                 'note_key': note['key'], 'original_page': original,
                 'page_id': previous['page_id'] if previous else None,
                 'body_page_id': original, 'renderer': RENDERER, 'source_blocks': payload['blocks'],
                 'state': 'prepared', 'blocks': render(paper, payload, (previous['version'] if previous else 0)+1, folder), 'warnings': payload['warnings'],
                 'properties': identity_properties(paper, note['key'], original)}
            a['root_marker'] = f'单篇论文深度精读 · V{a["version"]}'
            if rollback_version is None:
                raw = note['data']['note']
                backup = block('toggle', rich('原始 Zotero HTML 备份'))
                backup['toggle']['children'] = [{'object': 'block', 'type': 'code', 'code': {'rich_text': rich(raw[i:i+90000]), 'language': 'html'}} for i in range(0, len(raw), 90000)]
                a['blocks'] = a['blocks'] + [backup]
            if a['warnings']:
                a['blocks'] = [block('paragraph', rich('转换说明：' + '；'.join(a['warnings'])))] + a['blocks']
            store.save(a)
        try:
            if not a['page_id']:
                if existing:
                    a['page_id'] = existing['id']
                elif a['state'] == 'creating_page':
                    raise StopSync('PAGE_CREATION_OUTCOME_UNKNOWN: no blind retry; verify Notion before releasing this journal entry')
                else:
                    a['state'] = 'creating_page'; store.save(a)
                    page = api.request('POST', 'pages', {'parent': {'type': 'data_source_id', 'data_source_id': schema['source']['id']},
                        'properties': a['properties']})
                    a['page_id'] = page['id']
                a['state'] = 'page_ready'; store.save(a)
            # Always validate live identity and relation before writing a bound page.
            page = api.request('GET', 'pages/' + a['page_id'])
            body_page = validate_relation(api, page, original, paper, schema['original_source']['id'])
            if a.get('renderer') != RENDERER or a.get('body_page_id') != body_page:
                raise StopSync('PENDING_RENDERER_MIGRATION_REQUIRED')
            backup_pages(api, folder, a)
            marker = a.get('root_marker', f'Paper Deep Reading · v{a["version"]} · {a["id"]}')
            containers = [b for b in api.children(body_page) if b['type'] == 'paragraph' and plain({'type':'rich_text','rich_text':b['paragraph']['rich_text']}) == marker]
            if len(containers) > 1: raise StopSync('DUPLICATE_MANAGED_CONTAINERS')
            if not containers:
                if a['state'] == 'creating_container': raise StopSync('CONTAINER_CREATION_OUTCOME_UNKNOWN')
                a['state'] = 'creating_container'; store.save(a)
                # An ordinary paragraph container renders children expanded.
                insertion = {'children': [block('paragraph', rich(marker))]}
                if last.get('body_page_id') == body_page:
                    if not any(b['id'] == last['container'] for b in api.children(body_page)):
                        raise StopSync('PREVIOUS_MANAGED_REPORT_MISSING')
                    insertion['after'] = last['container']
                result = api.request('PATCH', f'blocks/{body_page}/children', insertion)
                a['container'] = result['results'][0]['id']
            else: a['container'] = containers[0]['id']
            a['state'] = 'staging'; store.save(a)
            reconcile_children(api, a['container'], a['blocks'])
            fill_properties(api, db, identity, body_page, attribute_props, folder)
            personal = [b for b in api.children(body_page) if b['type']=='heading_1' and plain({'type':'rich_text','rich_text':b['heading_1']['rich_text']})=='用户个人补充笔记']
            if not personal:
                api.request('PATCH', f'blocks/{body_page}/children', {'children':[payload['user_section']]})
            index_links = [b for b in api.children(a['page_id']) if b['type']=='link_to_page' and normalize_id(b['link_to_page'].get('page_id'))==normalize_id(body_page)]
            if not index_links:
                api.request('PATCH', f'blocks/{a["page_id"]}/children', {'children':[
                    {'object':'block','type':'link_to_page','link_to_page':{'type':'page_id','page_id':body_page}}]})
            a['synced'] = a.get('synced') or datetime.now(timezone.utc).isoformat()
            props = {'精读版本': {'number': a['version']}, '最近同步时间': {'date': {'start': a['synced']}},
                     '同步日志': rich_property(f'完整版本 v{a["version"]}; SHA256={a["hash"]}; container={a["container"]}; 警告={len(a["warnings"])}')}
            props.update(report_properties(a['source_blocks']))
            props['精读状态'] = {'select': {'name': '已同步'}}
            if not previous: props['证据核查状态'] = {'select': {'name': '未核查'}}
            # Commit only after complete content readback. A lost response is
            # reconciled by reading the version property on the next invocation.
            a['state'] = 'committing'; store.save(a)
            live = api.request('GET', 'pages/' + a['page_id'])
            live_version = live['properties'].get('精读版本', {}).get('number') or 0
            if live_version not in (a['version'] - 1, a['version']): raise StopSync('REMOTE_VERSION_CONFLICT')
            if live_version != a['version']:
                api.request('PATCH', 'pages/' + a['page_id'], {'properties': props})
            verified = api.request('GET', 'pages/' + a['page_id'])
            if verified['properties'].get('精读版本', {}).get('number') != a['version'] or a['hash'] not in plain(verified['properties'].get('同步日志', {})):
                raise StopSync('COMMIT_NOT_VERIFIED')
            retire_old_reports(api, db, a, folder)
            store.commit(a)
            return {'status': 'SYNCED', 'page_id': body_page, 'index_page_id': a['page_id'], 'url': 'https://www.notion.so/' + normalize_id(body_page), 'version': a['version'], 'warnings': a['warnings']}
        except Exception as error:
            store.save(a, str(error))
            raise


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['preflight', 'sync', 'rollback'])
    p.add_argument('--item-key'); p.add_argument('--note-key')
    p.add_argument('--library-type', default='user', choices=['user', 'group']); p.add_argument('--library-id', default='0')
    p.add_argument('--version', type=int); p.add_argument('--state-dir', default=str(ROOT / '.runtime'))
    args = p.parse_args()
    config = json.loads((ROOT / 'deep-reading-database.json').read_text(encoding='utf-8'))
    api = Notion()
    if args.command == 'preflight': return preflight(api, config)
    if not args.item_key: p.error('--item-key required')
    if args.command == 'rollback' and args.version is None: p.error('--version required')
    z = Zotero(args.library_type, args.library_id); paper = z.item(args.item_key)
    paper['_attachments'] = [n for n in z.children(args.item_key) if n['data']['itemType']=='attachment']
    paper['_fulltext'] = inspect_fulltext(z, paper['_attachments'])
    if paper['data']['itemType'] in ('note', 'attachment'): raise StopSync('SELECT_PARENT_PAPER')
    bound = args.note_key
    dbpath = Path(args.state_dir) / 'state.sqlite3'
    if dbpath.exists():
        store = Store(dbpath)
        try:
            mapping = store.mapping(paper_identity(paper))
            if mapping: bound = mapping['note_key']
        finally: store.db.close()
    note = choose_note(paper, z.children(args.item_key), bound)
    return sync(api, config, paper, note, args.state_dir, args.version if args.command == 'rollback' else None)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    try: print(json.dumps(main(), ensure_ascii=False, indent=2))
    except Exception as error:
        print(json.dumps({'status': 'PAUSED', 'error': str(error)}, ensure_ascii=False)); sys.exit(1)

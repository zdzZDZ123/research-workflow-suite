"""Notion relationship database, evidence pages, backlinks and image graph."""
import hashlib
import io
import json
import math
import re
import sqlite3
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

import networkx as nx
from PIL import Image, ImageDraw, ImageFont

from notion_sync import StopSync, normalize_id, plain
from paper_reading import block, rich
from relation_evidence import paper_metadata, safe_pdf_url


OVERVIEW_TITLE = '论文关系图谱'
DATABASE_TITLE = '论文关系知识库（Paper Relationship Library）'
FIELDS = {'关系名称': 'title', '论文A': 'relation', '论文B': 'relation',
          '关系类型': 'select', '方向': 'select', '审核状态': 'select', '证据状态': 'select',
          '关系键': 'rich_text', '来源指纹': 'rich_text', '最近分析': 'date'}
PUBLISHED = {'自动发布', '人工确认'}
RELATION_LABELS = {'shared_topic': '共同主题', 'shared_variable': '共同变量',
                   'shared_method': '共同方法', 'citation': '引用',
                   'supports': '支持', 'conflicts': '冲突', 'extends': '扩展'}


def relation_schema(original_source_id):
    relation = {'relation': {'data_source_id': original_source_id, 'type': 'single_property', 'single_property': {}}}
    select = lambda names: {'select': {'options': [{'name': n} for n in names]}}
    return {'关系名称': {'title': {}}, '论文A': relation, '论文B': relation,
            '关系类型': select(['shared_topic', 'shared_variable', 'shared_method', 'citation', 'supports', 'conflicts', 'extends']),
            '方向': select(['a_to_b', 'b_to_a', 'symmetric']),
            '审核状态': select(['自动发布', '待审核', '人工确认', '已驳回']),
            '证据状态': select(['当前', '需复核']), '关系键': {'rich_text': {}},
            '来源指纹': {'rich_text': {}}, '最近分析': {'date': {}}}


def _search_exact(api, query, kind):
    rows = api.all('search', 'POST', {'query': query})
    result = []
    for row in rows:
        if row.get('object') != kind:
            continue
        if kind == 'page':
            title = ''.join(x.get('plain_text', x.get('text', {}).get('content', ''))
                            for prop in row.get('properties', {}).values() if prop.get('type') == 'title'
                            for x in prop.get('title', []))
        else:
            title = ''.join(x.get('plain_text', x.get('text', {}).get('content', '')) for x in row.get('title', []))
        if title == query:
            result.append(row)
    return result


def bootstrap(api, original_source_id, config_path, overview_page_id=None):
    """Create once, recording each successful ID before the next remote mutation."""
    path = Path(config_path)
    intent_path = path.with_suffix('.intent.json')
    current = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    intent = json.loads(intent_path.read_text(encoding='utf-8')) if intent_path.exists() else None
    def create(kind, method, endpoint, body):
        intent_path.parent.mkdir(parents=True, exist_ok=True)
        intent_path.write_text(json.dumps({'kind': kind}, ensure_ascii=False), encoding='utf-8')
        try:
            return api.request(method, endpoint, body)
        except StopSync as error:
            if 'NOTION_OUTCOME_UNKNOWN' not in str(error):
                intent_path.unlink(missing_ok=True)
            if kind == 'overview' and 'NOTION_HTTP_400' in str(error):
                raise StopSync('OVERVIEW_PAGE_ACCESS_REQUIRED: create a Private Notion page, connect the local Research Projects integration, then run setup --overview-page-id <page-id>') from None
            raise
    if not current.get('overview_page_id'):
        if overview_page_id:
            overview = api.request('GET', 'pages/' + overview_page_id)
            if overview.get('archived') or overview.get('in_trash'):
                raise StopSync('OVERVIEW_PAGE_ARCHIVED')
            current['overview_page_id'] = overview['id']
        else:
            matches = _search_exact(api, OVERVIEW_TITLE, 'page')
            if len(matches) > 1:
                raise StopSync('OVERVIEW_PAGE_AMBIGUOUS')
            if matches:
                current['overview_page_id'] = matches[0]['id']
            else:
                if intent and intent['kind'] == 'overview':
                    raise StopSync('OVERVIEW_CREATE_OUTCOME_UNKNOWN: inspect Notion before retry')
                # Internal connections can create a top-level workspace page.
                page = create('overview', 'POST', 'pages', {'parent': {'type': 'workspace', 'workspace': True},
                    'properties': {'title': {'type': 'title', 'title': rich(OVERVIEW_TITLE)}}})
                current['overview_page_id'] = page['id']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding='utf-8')
        intent_path.unlink(missing_ok=True)
        intent = None
    if not current.get('relation_database_id'):
        children = api.children(current['overview_page_id'])
        candidates = [b for b in children if b.get('type') == 'child_database' and b.get('child_database', {}).get('title') == DATABASE_TITLE]
        if len(candidates) > 1:
            raise StopSync('RELATION_DATABASE_AMBIGUOUS')
        if candidates:
            database = api.request('GET', 'databases/' + candidates[0]['id'])
        else:
            if intent and intent['kind'] == 'database':
                raise StopSync('RELATION_DATABASE_CREATE_OUTCOME_UNKNOWN: inspect Notion before retry')
            database = create('database', 'POST', 'databases', {'parent': {'type': 'page_id', 'page_id': current['overview_page_id']},
                'title': rich(DATABASE_TITLE), 'initial_data_source': {'properties': relation_schema(original_source_id)}})
        current['relation_database_id'] = database['id']
        sources = database.get('data_sources', [])
        if len(sources) != 1:
            raise StopSync('RELATION_DATA_SOURCE_AMBIGUOUS')
        current['relation_data_source_id'] = sources[0]['id']
        path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding='utf-8')
        intent_path.unlink(missing_ok=True)
    return validate_schema(api, current, original_source_id)


def validate_schema(api, config, original_source_id):
    database = api.request('GET', 'databases/' + config['relation_database_id'])
    source_id = config['relation_data_source_id']
    if len([x for x in database.get('data_sources', []) if normalize_id(x['id']) == normalize_id(source_id)]) != 1:
        raise StopSync('RELATION_DATA_SOURCE_CHANGED')
    source = api.request('GET', 'data_sources/' + source_id)
    props = source['properties']
    for name, kind in FIELDS.items():
        if props.get(name, {}).get('type') != kind:
            raise StopSync('RELATION_SCHEMA_MISMATCH: ' + name)
    for name in ('论文A', '论文B'):
        relation = props[name]['relation']
        if relation.get('type') != 'single_property' or normalize_id(relation.get('data_source_id')) != normalize_id(original_source_id):
            raise StopSync('RELATION_TARGET_MISMATCH: ' + name)
    return config


def _value(page, name):
    prop = page.get('properties', {}).get(name, {})
    kind = prop.get('type')
    if kind == 'select':
        return (prop.get('select') or {}).get('name')
    if kind == 'relation':
        return [x['id'] for x in prop.get('relation', [])]
    return plain(prop)


def _paragraph(text, url=None):
    return block('paragraph', rich(text, link=url))


def _paper_evidence(label, paper, evidence):
    source = paper.get('source') or {}
    kind = {'zotero': 'Zotero 附件', 'notion_pdf': '原论文页 PDF'}.get(source.get('kind'), 'PDF')
    blocks = [block('heading_2', rich('论文 ' + label + ' 的证据')),
              _paragraph(paper['title'], paper['url']),
              _paragraph(f"PDF 顺序页码：第 {evidence['page']} 页 / 共 {paper['page_count']} 页"),
              block('quote', rich(evidence['quote'])),
              _paragraph('全文来源：' + kind)]
    if source.get('attachment_key'):
        blocks.append(_paragraph('Zotero 附件标识：' + source['attachment_key']))
    if source.get('url') and safe_pdf_url(source['url']):
        blocks.append(_paragraph('打开全文来源', source['url']))
    blocks.append(_paragraph('此 PDF 的 SHA256：' + paper['pdf_sha256']))
    return blocks


def candidate_body(candidate, a, b, model_name):
    direction = {'a_to_b': '论文 A → 论文 B', 'b_to_a': '论文 B → 论文 A',
                 'symmetric': '双向关联'}[candidate['direction']]
    blocks = [block('heading_2', rich('关系与审核依据')),
              _paragraph('模型提出的关系摘要：' + candidate['summary']),
              _paragraph('类型：' + RELATION_LABELS[candidate['type']] + '；方向：' + direction + '；概念：' + candidate['concept']),
              _paragraph('程序核对结果：' + candidate.get('decision_reason', '两端摘录已定位；请结合审核状态判断是否采用。'))]
    blocks.extend(_paper_evidence('A', a, candidate['evidence_a']))
    blocks.extend(_paper_evidence('B', b, candidate['evidence_b']))
    blocks.extend([_paragraph('页码按 PDF 文件顺序计数，可能与印刷页码不同。来源链接失效时，可从上方原论文页重新打开附件。'),
                   _paragraph('审核后，将“审核状态”设为“人工确认”或“已驳回”，再运行一次关系发现以更新图谱。来源变化后的修订还需将“证据状态”设为“当前”。'),
                   _paragraph('模型：' + model_name + '；来源指纹：' + candidate['source_hash'])])
    return blocks


def _snapshot(blocks):
    """Record enough of each managed child to detect user edits before archival."""
    result = []
    for item in blocks:
        kind = item.get('type')
        value = item.get(kind, {})
        texts = value.get('rich_text', value.get('caption', []))
        result.append({'id': item.get('id'), 'type': kind,
                       'edited': item.get('last_edited_time'),
                       'text': [[x.get('plain_text', x.get('text', {}).get('content', '')),
                                 (x.get('href') or (x.get('text', {}).get('link') or {}).get('url'))] for x in texts]})
    return result


class RelationStore:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS relation_papers(page_id TEXT PRIMARY KEY, pdf_hash TEXT, cards TEXT, source TEXT);
        CREATE TABLE IF NOT EXISTS relation_pairs(pair_key TEXT PRIMARY KEY, source_hash TEXT, status TEXT, checked_at TEXT);
        CREATE TABLE IF NOT EXISTS relation_entries(key TEXT PRIMARY KEY, notion_page TEXT, source_hash TEXT, state TEXT);
        CREATE TABLE IF NOT EXISTS relation_uncertain(op TEXT PRIMARY KEY, created TEXT);
        CREATE TABLE IF NOT EXISTS relation_managed(owner TEXT PRIMARY KEY, digest TEXT, block_id TEXT, expected TEXT);
        ''')

    def uncertain(self, key):
        return bool(self.db.execute('SELECT 1 FROM relation_uncertain WHERE op=?', (key,)).fetchone())

    def mark_uncertain(self, key):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO relation_uncertain VALUES(?,?)', (key, datetime.now(timezone.utc).isoformat()))

    def clear_uncertain(self, key):
        with self.db:
            self.db.execute('DELETE FROM relation_uncertain WHERE op=?', (key,))


class RelationPublisher:
    def __init__(self, api, config, store):
        self.api, self.config, self.store = api, config, store
        self.source = config['relation_data_source_id']

    def rows(self):
        return self.api.rows(self.source)

    def find(self, key):
        rows = self.api.rows(self.source, {'property': '关系键', 'rich_text': {'equals': key}})
        if len(rows) > 1:
            raise StopSync('DUPLICATE_RELATION_KEY: ' + key)
        return rows[0] if rows else None

    def publish(self, candidate, a, b, model_name):
        key = candidate['key']
        op = 'create:' + key
        existing = self.find(key)
        if self.store.uncertain(op) and not existing:
            raise StopSync('RELATION_CREATE_OUTCOME_UNKNOWN: inspect Notion before retry')
        if existing:
            if set(_value(existing, '论文A')) != {a['page_id']} or set(_value(existing, '论文B')) != {b['page_id']}:
                raise StopSync('RELATION_ENDPOINT_CHANGED')
            if _value(existing, '来源指纹') != candidate['source_hash']:
                if _value(existing, '证据状态') != '需复核':
                    backup = {'page': existing, 'children': self.api.children(existing['id'])}
                    dest = Path(self.config['backup_dir']); dest.mkdir(parents=True, exist_ok=True)
                    (dest / (existing['id'] + '-stale.json')).write_text(json.dumps(backup, ensure_ascii=False, indent=2), encoding='utf-8')
                    self.api.request('PATCH', 'pages/' + existing['id'], {'properties': {'证据状态': {'select': {'name': '需复核'}}}})
                # Keep the original excerpts and any user edits. A separate
                # managed revision lets a reviewer inspect the newly checked
                # PDF evidence on the same stable relationship record.
                self._managed('candidate:' + key, existing['id'], candidate['source_hash'],
                              candidate_body(candidate, a, b, model_name))
            with self.store.db:
                self.store.db.execute('INSERT OR REPLACE INTO relation_entries VALUES(?,?,?,?)',
                                      (key, existing['id'], candidate['source_hash'], 'committed'))
            self.store.clear_uncertain(op)
            return existing['id'], False
        prior = self.store.db.execute('SELECT notion_page FROM relation_entries WHERE key=?', (key,)).fetchone()
        if prior:
            raise StopSync('MANAGED_RELATION_MISSING: ' + key)
        props = {'关系名称': {'title': rich(candidate['title'])},
                 '论文A': {'relation': [{'id': a['page_id']}]},
                 '论文B': {'relation': [{'id': b['page_id']}]},
                 '关系类型': {'select': {'name': candidate['type']}},
                 '方向': {'select': {'name': candidate['direction']}},
                 '审核状态': {'select': {'name': '自动发布' if candidate['auto_publish'] else '待审核'}},
                 '证据状态': {'select': {'name': '当前'}},
                 '关系键': {'rich_text': rich(key)},
                 '来源指纹': {'rich_text': rich(candidate['source_hash'])},
                 '最近分析': {'date': {'start': datetime.now(timezone.utc).isoformat()}}}
        body = {'parent': {'type': 'data_source_id', 'data_source_id': self.source},
                'properties': props, 'children': candidate_body(candidate, a, b, model_name)}
        try:
            created = self.api.request('POST', 'pages', body)
        except StopSync as error:
            if 'NOTION_OUTCOME_UNKNOWN' not in str(error):
                raise
            created = self.find(key)
            if not created:
                self.store.mark_uncertain(op)
                raise StopSync('RELATION_CREATE_OUTCOME_UNKNOWN: inspect Notion before retry') from None
        check = self.api.request('GET', 'pages/' + created['id'])
        if _value(check, '关系键') != key or set(_value(check, '论文A')) != {a['page_id']} or set(_value(check, '论文B')) != {b['page_id']}:
            raise StopSync('RELATION_READBACK_MISMATCH')
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO relation_entries VALUES(?,?,?,?)',
                                  (key, created['id'], candidate['source_hash'], 'committed'))
        self.store.clear_uncertain(op)
        return created['id'], True

    def stale_pair(self, a, b, current_hash):
        """Keep old conclusions but remove them from the published graph after source changes."""
        endpoints = {a['page_id'], b['page_id']}
        changed = 0
        for row in self.rows():
            if ({*_value(row, '论文A'), *_value(row, '论文B')} != endpoints
                    or _value(row, '来源指纹') == current_hash
                    or _value(row, '证据状态') == '需复核'):
                continue
            backup = {'page': row, 'children': self.api.children(row['id'])}
            dest = Path(self.config['backup_dir']); dest.mkdir(parents=True, exist_ok=True)
            (dest / (row['id'] + '-stale.json')).write_text(json.dumps(backup, ensure_ascii=False, indent=2), encoding='utf-8')
            self.api.request('PATCH', 'pages/' + row['id'], {'properties': {'证据状态': {'select': {'name': '需复核'}}}})
            changed += 1
        return changed

    def stale_unmatched(self, valid_pairs):
        """Revoke managed evidence when a PDF changes or either full text vanishes.

        valid_pairs maps a sorted pair of original page IDs to its current PDF
        fingerprint. Review status and existing evidence blocks are untouched.
        """
        changed = set()
        for row in self.rows():
            # Rows authored in Notion without our stable key and fingerprint
            # are outside this synchronizer's ownership.
            if (not re.fullmatch(r'[0-9a-f]{64}', _value(row, '关系键') or '')
                    or not re.fullmatch(r'[0-9a-f]{64}', _value(row, '来源指纹') or '')):
                continue
            left, right = _value(row, '论文A'), _value(row, '论文B')
            if len(left) != 1 or len(right) != 1:
                continue
            pair = tuple(sorted((left[0], right[0])))
            current_hash = valid_pairs.get(pair)
            if _value(row, '来源指纹') == current_hash:
                continue
            # The reviewer explicitly accepted the new evidence in Notion.
            # Bind that approval to the exact managed revision we published;
            # changing only the review label cannot revive stale evidence.
            if (current_hash and _value(row, '审核状态') == '人工确认'
                    and _value(row, '证据状态') == '当前'):
                key = _value(row, '关系键')
                entry = self.store.db.execute('SELECT source_hash FROM relation_entries WHERE key=?', (key,)).fetchone()
                revision = self.store.db.execute('SELECT * FROM relation_managed WHERE owner=?', ('candidate:' + key,)).fetchone()
                if (entry and entry['source_hash'] == current_hash and revision
                        and revision['digest'] == current_hash
                        and _snapshot(self.api.children(revision['block_id'])) == json.loads(revision['expected'])):
                    try:
                        self.api.request('PATCH', 'pages/' + row['id'],
                                         {'properties': {'来源指纹': {'rich_text': rich(current_hash)}}})
                    except StopSync as error:
                        if ('NOTION_OUTCOME_UNKNOWN' not in str(error)
                                or _value(self.api.request('GET', 'pages/' + row['id']), '来源指纹') != current_hash):
                            raise
                    continue
            if _value(row, '证据状态') == '需复核':
                continue
            backup = {'page': row, 'children': self.api.children(row['id'])}
            dest = Path(self.config['backup_dir']); dest.mkdir(parents=True, exist_ok=True)
            (dest / (row['id'] + '-stale.json')).write_text(json.dumps(backup, ensure_ascii=False, indent=2), encoding='utf-8')
            self.api.request('PATCH', 'pages/' + row['id'], {'properties': {'证据状态': {'select': {'name': '需复核'}}}})
            changed.add(pair)
        return changed

    def published(self):
        return [r for r in self.rows() if _value(r, '审核状态') in PUBLISHED and _value(r, '证据状态') == '当前'
                and len(_value(r, '论文A')) == len(_value(r, '论文B')) == 1]

    def _managed(self, owner, parent, digest, children):
        marker = f'论文关系 · {owner} · {digest[:16]}'
        expected_content = [(x['type'], x['text']) for x in _snapshot(children)]
        def checked_children(block_id):
            observed = _snapshot(self.api.children(block_id))
            if [(x['type'], x['text']) for x in observed] != expected_content:
                raise StopSync('MANAGED_BLOCK_READBACK_MISMATCH: ' + owner)
            return observed
        roots = self.api.children(parent)
        found = [b for b in roots if b.get('type') == 'paragraph' and plain({'type': 'rich_text', 'rich_text': b['paragraph'].get('rich_text', [])}) == marker]
        if len(found) > 1:
            raise StopSync('DUPLICATE_MANAGED_BLOCK')
        previous = self.store.db.execute('SELECT * FROM relation_managed WHERE owner=?', (owner,)).fetchone()
        if found:
            child_snapshot = checked_children(found[0]['id'])
            if previous and previous['block_id'] == found[0]['id'] and json.loads(previous['expected']) != child_snapshot:
                raise StopSync('MANAGED_BLOCK_MODIFIED: ' + owner)
            if not previous or previous['block_id'] != found[0]['id']:
                self._retire_old(owner, parent, previous, found[0]['id'])
                with self.store.db:
                    self.store.db.execute('INSERT OR REPLACE INTO relation_managed VALUES(?,?,?,?)',
                                          (owner, digest, found[0]['id'], json.dumps(child_snapshot, ensure_ascii=False)))
            self.store.clear_uncertain('block:' + owner + ':' + digest)
            return found[0]['id'], False
        op = 'block:' + owner + ':' + digest
        if self.store.uncertain(op):
            raise StopSync('MANAGED_BLOCK_OUTCOME_UNKNOWN: inspect Notion before retry')
        root = block('paragraph', rich(marker))
        root['paragraph']['children'] = children
        try:
            response = self.api.request('PATCH', 'blocks/' + parent + '/children', {'children': [root]})
            block_id = response['results'][0]['id']
        except StopSync as error:
            if 'NOTION_OUTCOME_UNKNOWN' not in str(error):
                raise
            found = [b for b in self.api.children(parent) if b.get('type') == 'paragraph'
                     and plain({'type': 'rich_text', 'rich_text': b['paragraph'].get('rich_text', [])}) == marker]
            if len(found) != 1:
                self.store.mark_uncertain(op)
                raise StopSync('MANAGED_BLOCK_OUTCOME_UNKNOWN: inspect Notion before retry') from None
            block_id = found[0]['id']
        child_snapshot = checked_children(block_id)
        self._retire_old(owner, parent, previous, block_id)
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO relation_managed VALUES(?,?,?,?)',
                                  (owner, digest, block_id, json.dumps(child_snapshot, ensure_ascii=False)))
        self.store.clear_uncertain(op)
        return block_id, True

    def _retire_old(self, owner, parent, previous, replacement=None):
        if not previous or previous['block_id'] == replacement:
            return False
        older = next((b for b in self.api.children(parent) if b['id'] == previous['block_id']), None)
        if not older:
            return False
        old_marker = f'论文关系 · {owner} · {previous["digest"][:16]}'
        if older.get('type') != 'paragraph' or plain({'type': 'rich_text', 'rich_text': older['paragraph'].get('rich_text', [])}) != old_marker:
            raise StopSync('MANAGED_BLOCK_OWNERSHIP_CHANGED: ' + owner)
        if _snapshot(self.api.children(older['id'])) != json.loads(previous['expected']):
            raise StopSync('MANAGED_BLOCK_MODIFIED: ' + owner)
        try:
            self.api.request('PATCH', 'blocks/' + older['id'], {'archived': True})
        except StopSync as error:
            if 'NOTION_OUTCOME_UNKNOWN' not in str(error) or any(b['id'] == older['id'] for b in self.api.children(parent)):
                raise
        return True

    def backlinks(self, papers, published):
        links = {p['page_id']: [] for p in papers}
        for row in published:
            for field in ('论文A', '论文B'):
                page_id = _value(row, field)[0]
                if page_id in links:
                    links[page_id].append(row)
        changed = 0
        for paper in papers:
            rows = sorted(links[paper['page_id']], key=lambda r: r['id'])
            if not rows:
                owner = 'backlink:' + paper['page_id']
                previous = self.store.db.execute('SELECT * FROM relation_managed WHERE owner=?', (owner,)).fetchone()
                if self._retire_old(owner, paper['page_id'], previous):
                    with self.store.db:
                        self.store.db.execute('DELETE FROM relation_managed WHERE owner=?', (owner,))
                    changed += 1
                continue
            digest = hashlib.sha256(json.dumps([(r['id'], _value(r, '关系类型')) for r in rows]).encode()).hexdigest()
            children = [block('bulleted_list_item', rich(_value(r, '关系名称'), link='https://www.notion.so/' + normalize_id(r['id']))) for r in rows]
            _, did = self._managed('backlink:' + paper['page_id'], paper['page_id'], digest, children)
            changed += did
        return changed

    def graph(self, papers, published):
        overview = self.config['overview_page_id']
        nodes = sorted((p['page_id'], p['title']) for p in papers)
        edges = sorted(((_value(r, '论文B')[0], _value(r, '论文A')[0]) if _value(r, '方向') == 'b_to_a'
                        else (_value(r, '论文A')[0], _value(r, '论文B')[0])) + (_value(r, '关系类型'), r['id'])
                       for r in published)
        digest = hashlib.sha256(json.dumps([nodes, edges], ensure_ascii=False).encode()).hexdigest()
        current = self.store.db.execute('SELECT digest,block_id FROM relation_managed WHERE owner=?', ('graph',)).fetchone()
        if current and current['digest'] == digest and any(b['id'] == current['block_id'] for b in self.api.children(overview)):
            if _snapshot(self.api.children(current['block_id'])) != json.loads(self.store.db.execute(
                    'SELECT expected FROM relation_managed WHERE owner=?', ('graph',)).fetchone()['expected']):
                raise StopSync('MANAGED_BLOCK_MODIFIED: graph')
            return False
        png = graph_png(nodes, edges)
        file_id = upload_png(self.api, png)
        image = {'object': 'block', 'type': 'image', 'image': {'type': 'file_upload', 'file_upload': {'id': file_id}}}
        caption = _paragraph(f'已发布关系 {len(edges)} 条；待审核关系不进入图谱。更新时间：{datetime.now().astimezone().isoformat(timespec="minutes")}')
        _, changed = self._managed('graph', overview, digest, [caption, image])
        return changed


def graph_png(nodes, edges):
    graph = nx.MultiDiGraph()
    graph.add_nodes_from(id for id, _ in nodes)
    graph.add_edges_from((a, b) for a, b, _, _ in edges)
    pos = nx.spring_layout(graph, seed=19) if len(nodes) > 1 else {nodes[0][0]: (0, 0)} if nodes else {}
    image = Image.new('RGB', (1600, 1000), '#fbfcfe')
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 24)
        small = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 18)
    except OSError:
        font = ImageFont.load_default(); small = font
    draw.text((55, 32), '论文关系图谱', fill='#152238', font=font)
    xy = {id: (int(800 + float(p[0]) * 540), int(530 + float(p[1]) * 360)) for id, p in pos.items()}
    for a, b, kind, _ in edges:
        if a not in xy or b not in xy:
            continue
        x1, y1 = xy[a]; x2, y2 = xy[b]
        draw.line((x1, y1, x2, y2), fill='#5972a5', width=4)
        if kind in ('citation', 'supports', 'conflicts', 'extends'):
            angle = math.atan2(y2-y1, x2-x1)
            px, py = x2 - 54*math.cos(angle), y2 - 54*math.sin(angle)
            draw.polygon([(px, py), (px-18*math.cos(angle-.6), py-18*math.sin(angle-.6)),
                          (px-18*math.cos(angle+.6), py-18*math.sin(angle+.6))], fill='#5972a5')
        draw.text(((x1+x2)/2 + 8, (y1+y2)/2 + 8), kind, fill='#244675', font=small)
    for id, title in nodes:
        x, y = xy[id]
        draw.ellipse((x-43, y-43, x+43, y+43), fill='#2864a1', outline='#173c62', width=3)
        label = title[:36] + ('…' if len(title) > 36 else '')
        draw.text((x-100, y+50), label, fill='#172c46', font=small)
    if not nodes:
        draw.text((55, 120), '暂无可读取全文的论文', fill='#4a5a6f', font=font)
    output = io.BytesIO(); image.save(output, format='PNG', optimize=True)
    value = output.getvalue()
    if len(value) > 5 * 1024 * 1024:
        raise StopSync('GRAPH_IMAGE_TOO_LARGE')
    return value


def upload_png(api, data):
    created = api.request('POST', 'file_uploads', {'filename': 'paper-relations.png', 'content_type': 'image/png'})
    file_id = created['id']
    boundary = 'PaperRelations' + uuid.uuid4().hex
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="paper-relations.png"\r\n'
            'Content-Type: image/png\r\n\r\n').encode() + data + f'\r\n--{boundary}--\r\n'.encode()
    request = urllib.request.Request('https://api.notion.com/v1/file_uploads/' + file_id + '/send',
        data=body, method='POST', headers={'Authorization': 'Bearer ' + api.token,
        'Notion-Version': '2025-09-03', 'Content-Type': 'multipart/form-data; boundary=' + boundary})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            uploaded = json.load(response)
    except urllib.error.HTTPError as error:
        raise StopSync('NOTION_FILE_UPLOAD_HTTP_' + str(error.code)) from None
    if uploaded.get('status') != 'uploaded':
        raise StopSync('NOTION_FILE_UPLOAD_UNCONFIRMED')
    return file_id

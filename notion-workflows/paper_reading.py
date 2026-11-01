"""Local Zotero intake and loss-aware Notion block conversion."""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import urllib.request
from contextlib import closing
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SECTIONS = ['论文核心摘要', '研究背景与研究空白', '研究问题与研究假设', '研究变量及其关系',
            '研究设计与研究方法', '数据与统计分析', '图表、公式及实验结果', '研究结论',
            '研究创新与学术贡献', '研究局限与批判性思考', '后续科研启发', '原文证据及 PDF 页码',
            'Paper Chat 未归类的原始内容']


def get_json(url, headers=None):
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


class Zotero:
    def __init__(self, library_type='user', library_id='0'):
        if library_type not in ('user', 'group') or not str(library_id).isdigit():
            raise ValueError('Invalid library identity')
        self.base = f'http://127.0.0.1:23119/api/{library_type}s/{library_id}'

    def item(self, key):
        if not re.fullmatch(r'[A-Z0-9]{8}', key):
            raise ValueError('Invalid Zotero Item Key')
        return get_json(f'{self.base}/items/{key}')

    def children(self, key):
        self.item(key)  # validate key and library access
        result = []
        while True:
            page = get_json(f'{self.base}/items/{key}/children?limit=100&start={len(result)}')
            result.extend(page)
            if len(page) < 100:
                return result


class Node:
    def __init__(self, tag='', attrs=None):
        self.tag, self.attrs, self.children = tag, dict(attrs or []), []

    def text(self):
        return ''.join(c if isinstance(c, str) else c.text() for c in self.children)


class Tree(HTMLParser):
    VOID = {'img', 'br', 'hr', 'meta', 'link', 'input', 'source', 'wbr'}

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack)-1, 0, -1):
            if self.stack[i].tag == tag:
                self.stack = self.stack[:i]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def rich(text, annotations=None, link=None):
    result = []
    for i in range(0, len(text), 1900):
        value = {'type': 'text', 'text': {'content': text[i:i+1900]}}
        if annotations:
            value['annotations'] = annotations
        if link:
            value['text']['link'] = {'url': link}
        result.append(value)
    return result


def block(kind, spans):
    return {'object': 'block', 'type': kind, kind: {'rich_text': spans}}


class Converter:
    def __init__(self):
        self.warnings = []

    def warn(self, message):
        if message not in self.warnings:
            self.warnings.append(message)

    def inline(self, node, styles=None, link=None):
        styles = dict(styles or {})
        if isinstance(node, str):
            return rich(node, styles, link)
        tag = node.tag
        formats = {'b': 'bold', 'strong': 'bold', 'em': 'italic', 'i': 'italic',
                   'u': 'underline', 's': 'strikethrough', 'del': 'strikethrough', 'code': 'code'}
        if tag in formats:
            styles[formats[tag]] = True
        if tag == 'a':
            href = node.attrs.get('href', '')
            if href.startswith(('https://', 'http://', 'mailto:')) and len(href) <= 2000:
                link = href
            elif href:
                self.warn('Unsupported link retained in raw HTML: ' + href[:80])
                return rich(node.text() + ' [' + href + ']', styles)
        if tag == 'br':
            return rich('\n', styles)
        if tag == 'img':
            self.warn('Image requires asset transfer or URL validation; raw HTML preserves source')
            return rich('[图片：' + node.attrs.get('alt', '见原始 HTML') + ']', styles)
        if tag == 'math':
            def find_tex(n):
                if isinstance(n, str):
                    return None
                if n.tag == 'annotation' and n.attrs.get('encoding') == 'application/x-tex':
                    return n.text()
                return next((v for c in n.children if (v := find_tex(c))), None)
            tex = find_tex(node)
            if tex and len(tex) <= 1000:
                return [{'type': 'equation', 'equation': {'expression': tex}}]
            self.warn('MathML lacks usable TeX or exceeds equation limit; retained as text and raw HTML')
            return rich(node.text(), styles)
        if tag == 'span' and 'math' in node.attrs.get('class', '').split():
            tex = node.text().strip()
            for delimiter in ('$$', '$', '\\(', '\\['):
                end = {'\\(': '\\)', '\\[': '\\]'}.get(delimiter, delimiter)
                if tex.startswith(delimiter) and tex.endswith(end):
                    tex = tex[len(delimiter):-len(end)].strip()
                    break
            if tex and len(tex) <= 1000:
                return [{'type': 'equation', 'equation': {'expression': tex}}]
            self.warn('Zotero formula exceeds Notion limit; retained as text')
        if tag not in {'', 'span', 'p', 'div', 'a', 'li', 'blockquote', 'math', 'td', 'th'} | set(formats) and not re.fullmatch('h[1-6]', tag):
            self.warn('Unsupported inline element retained as text and raw HTML: ' + tag)
        return [r for c in node.children for r in self.inline(c, styles, link)]

    def table(self, node):
        def rows(n):
            if isinstance(n, str): return []
            if n.tag == 'tr': return [n]
            return [r for c in n.children for r in rows(c)]
        data = [[c for c in row.children if isinstance(c, Node) and c.tag in ('td', 'th')] for row in rows(node)]
        if not data or not max(map(len, data)): return []
        width = max(map(len, data))
        if any(c.attrs.get('rowspan', '1') != '1' or c.attrs.get('colspan', '1') != '1' for row in data for c in row):
            self.warn('Merged table cells expanded as text; original spans preserved in HTML')
        result = []
        for row in data:
            cells = [self.inline(c) for c in row]
            if any(len(c) > 100 for c in cells):
                self.warn('Oversized table cell converted to paragraphs; original HTML retained')
                return self.blocks([c for row in data for c in row])
            cells.extend([[] for _ in range(width-len(cells))])
            result.append({'object': 'block', 'type': 'table_row', 'table_row': {'cells': cells}})
        return [{'object': 'block', 'type': 'table', 'table': {'table_width': width, 'has_column_header': all(c.tag == 'th' for c in data[0]), 'has_row_header': False, 'children': result}}]

    def blocks(self, nodes, list_kind=None):
        result, pending = [], []
        def flush():
            nonlocal pending
            if pending:
                # Notion rich_text arrays are limited to 100 elements.
                for i in range(0, len(pending), 90):
                    result.append(block('paragraph', pending[i:i+90]))
                pending = []
        for n in nodes:
            if isinstance(n, str):
                if n.strip(): pending.extend(rich(n))
                continue
            if n.tag in ('div', 'section', 'article'):
                flush(); result.extend(self.blocks(n.children))
            elif n.tag in ('ul', 'ol'):
                flush(); result.extend(self.blocks(n.children, 'bulleted_list_item' if n.tag == 'ul' else 'numbered_list_item'))
            elif n.tag == 'table':
                flush(); result.extend(self.table(n))
            elif n.tag in ('p', 'li', 'blockquote', 'pre') or re.fullmatch('h[1-6]', n.tag):
                flush()
                kind = {'p': 'paragraph', 'li': list_kind or 'paragraph', 'blockquote': 'quote', 'pre': 'paragraph'}.get(n.tag)
                if kind is None: kind = 'heading_' + str(min(int(n.tag[1]), 3))
                nested = [c for c in n.children if isinstance(c, Node) and c.tag in ('ul', 'ol')]
                spans = [r for c in n.children if c not in nested for r in self.inline(c)]
                if n.tag == 'pre': self.warn('Code block retained as text; original formatting in HTML')
                for i in range(0, max(1, len(spans)), 90):
                    result.append(block(kind, spans[i:i+90]))
                if nested:
                    children = self.blocks(nested)
                    if result and kind in ('bulleted_list_item', 'numbered_list_item', 'paragraph', 'quote'):
                        result[-1][kind]['children'] = children
                    else:
                        self.warn('Nested list outside a supported parent; hierarchy flattened')
                        result.extend(children)
            elif n.tag == 'hr':
                flush(); result.append({'object': 'block', 'type': 'divider', 'divider': {}})
            else:
                pending.extend(self.inline(n))
        flush()
        return result

    def convert(self, html):
        return self.blocks(Tree(html).root.children)


def note_summary(item):
    d = item['data']
    tree = Tree(d.get('note', '')).root
    def first_heading(n):
        if isinstance(n, str): return None
        if re.fullmatch('h[1-6]', n.tag): return n.text()
        return next((v for c in n.children if (v := first_heading(c))), None)
    return {'key': item['key'], 'parent': d.get('parentItem'), 'type': d['itemType'],
            'title': d.get('title') or first_heading(tree) or tree.text().strip().split('\n')[0][:180],
            'modified': d.get('dateModified'), 'html_chars': len(d.get('note', ''))}


def choose_note(paper, children, bound_key=None):
    notes = [n for n in children if n['data']['itemType'] == 'note' and n['data'].get('parentItem') == paper['key'] and n['library']['id'] == paper['library']['id'] and n['library']['type'] == paper['library']['type']]
    if bound_key:
        found = [n for n in notes if n['key'] == bound_key]
        if len(found) != 1: raise ValueError('Bound note missing or not a child of this paper; no fallback')
        return found[0]
    candidates = [n for n in notes if any(x['tag'] == 'ai-deep-summary' for x in n['data'].get('tags', [])) or any(s in note_summary(n)['title'].lower() for s in ('ai深度摘要', 'ai deep summary', 'paperchat notes', '精读'))]
    if len(candidates) != 1:
        raise ValueError(json.dumps({'status': 'NOTE_REQUIRED' if not candidates else 'NOTE_SELECTION_REQUIRED', 'notes': [note_summary(n) for n in notes]}, ensure_ascii=False))
    return candidates[0]


def organize(content):
    """Assign only recognized source headings; never synthesize research claims."""
    aliases = {0: ['概览', 'overview', '论文核心摘要', '摘要'], 1: ['研究背景与研究空白'],
               2: ['研究问题', '研究问题与研究假设'], 3: ['研究变量及其关系'],
               4: ['方法', '研究设计与研究方法'], 5: ['数据与统计分析'],
               6: ['关键发现', '图表、公式及实验结果'], 7: ['一句话总结', '研究结论'],
               8: ['为什么这篇论文重要', '研究创新与学术贡献'],
               9: ['局限', '研究局限与批判性思考'], 10: ['后续科研启发'],
               11: ['原文证据及 PDF 页码']}
    grouped = [[] for _ in SECTIONS]
    current = 12
    for b in content:
        if b['type'] in ('heading_1', 'heading_2'):
            heading = ''.join(r.get('text', {}).get('content', '') for r in b[b['type']]['rich_text'])
            heading = re.sub(r'^[一二三四五六七八九十\d]+[、.．]\s*', '', heading).strip()
            # A new unknown major heading resets routing, preserving checkpoint provenance.
            current = next((i for i, names in aliases.items() if any(heading == n or heading.startswith(n + '（') or heading.startswith(n + ' (') for n in names)), 12)
        grouped[current].append(b)
    result = []
    for i, title in enumerate(SECTIONS):
        result.append(block('heading_1', rich(title)))
        result.extend(grouped[i] or [block('paragraph', rich('原报告没有可直接归入本节的独立章节；未补写内容。'))])
    return result


def prepare(paper, note, state_dir):
    html = note['data']['note']
    if not html.strip(): raise ValueError('Empty note')
    digest = hashlib.sha256(html.encode('utf-8')).hexdigest()
    identity = f"{paper['library']['type']}:{paper['library']['id']}:{paper['key']}"
    converter = Converter()
    content = converter.convert(html)
    blocks = organize(content)
    payload = {'status': 'PREVIEW_ONLY', 'identity': identity, 'paper_key': paper['key'],
               'note_key': note['key'], 'source_modified': note['data'].get('dateModified'),
               'sha256': digest, 'warnings': converter.warnings, 'blocks': blocks,
               'user_section': block('heading_1', rich('用户个人补充笔记'))}
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(state_dir / 'state.sqlite3', timeout=30)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS previews(identity TEXT, note_key TEXT, hash TEXT, created TEXT, payload TEXT, raw_html TEXT, PRIMARY KEY(identity,note_key,hash))')
        db.execute('INSERT INTO previews VALUES(?,?,?,?,?,?) ON CONFLICT(identity,note_key,hash) DO UPDATE SET payload=excluded.payload', (identity, note['key'], digest, datetime.now(timezone.utc).isoformat(), json.dumps(payload, ensure_ascii=False), html))
    # Files are derived from the committed SQLite preview; no successful sync version is advanced.
    folder = state_dir / 'previews' / identity.replace(':', '_') / note['key'] / digest
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'source.html').write_text(html, encoding='utf-8')
    (folder / 'notion-preview.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'status': 'PREVIEW_ONLY', 'identity': identity, 'note_key': note['key'], 'sha256': digest, 'block_count': len(blocks), 'warnings': converter.warnings, 'path': str(folder.resolve())}


def notion_preflight(config):
    from secure_credentials import notion_token
    token = notion_token()
    if not token: return {'status': 'LOCAL_API_CREDENTIAL_REQUIRED', 'remote_writes': False}
    headers = {'Authorization': 'Bearer ' + token, 'Notion-Version': '2025-09-03'}
    results = {}
    for field in ('original_database_id', 'database_id'):
        database = get_json('https://api.notion.com/v1/databases/' + config[field], headers)
        sources = database.get('data_sources', [])
        if len(sources) != 1: raise ValueError(f'{field}: select an explicit Data Source ID')
        source = get_json('https://api.notion.com/v1/data_sources/' + sources[0]['id'], headers)
        results[field] = {'database_id': database['id'], 'data_source_id': source['id'], 'properties': source['properties']}
    return {'status': 'READ_ACCESS_VERIFIED', 'remote_writes': False, 'databases': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['inspect', 'preview', 'notion-preflight'])
    parser.add_argument('--item-key')
    parser.add_argument('--note-key')
    parser.add_argument('--library-type', choices=['user', 'group'], default='user')
    parser.add_argument('--library-id', default='0')
    parser.add_argument('--state-dir', default=str(ROOT / '.runtime'))
    args = parser.parse_args()
    if args.command == 'notion-preflight':
        result = notion_preflight(json.loads((ROOT / 'deep-reading-database.json').read_text(encoding='utf-8')))
    else:
        if not args.item_key: parser.error('--item-key required')
        z = Zotero(args.library_type, args.library_id)
        paper = z.item(args.item_key)
        if paper['data']['itemType'] in ('note', 'attachment'): raise ValueError('Select the parent paper')
        children = z.children(args.item_key)
        if args.command == 'inspect':
            result = {'paper': note_summary(paper), 'library': paper['library'], 'children': [note_summary(n) for n in children]}
        else:
            result = prepare(paper, choose_note(paper, children, args.note_key), args.state_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    try: main()
    except Exception as error:
        print(json.dumps({'status': 'FAILED', 'error_type': type(error).__name__, 'message': str(error)}, ensure_ascii=False))
        sys.exit(1)

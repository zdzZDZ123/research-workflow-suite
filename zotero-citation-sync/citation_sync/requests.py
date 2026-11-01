"""Conservative local request extraction. Source A is never an implicit citation B."""
from __future__ import annotations

import difflib
import re
import unicodedata
from html.parser import HTMLParser

from .docx_read import locate, uri_identity
from .util import digest

REQUEST = re.compile(r'\[cite\]|新增引用|引用请求|补充引用|添加引用|\badd (?:a )?(?:citation|reference)\b|\bcite\s*:', re.I)
NEGATIVE = re.compile(r'不要(?:新增|添加)?引用|无需(?:新增|添加)?引用|do not cite|no citation needed', re.I)
DOI = re.compile(r'10\.\d{4,9}/[^\s<>"“”]+', re.I)


class TextOnly(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.output, self.skip = [], 0
    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.skip += 1
        if tag in ('p', 'br', 'div', 'li') and not self.skip:
            self.output.append('\n')
    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.skip = max(0, self.skip - 1)
        if tag in ('p', 'div', 'li') and not self.skip:
            self.output.append('\n')
    def handle_data(self, data):
        if not self.skip:
            self.output.append(data)


def plain(value):
    parser = TextOnly()
    parser.feed(value or '')
    return ''.join(parser.output).strip()


def normalize_doi(value):
    found = DOI.search(value or '')
    if not found:
        return ''
    doi = found.group().rstrip('.,;:，。；：')
    while doi.endswith(')') and doi.count(')') > doi.count('('):
        doi = doi[:-1]
    return doi.lower()


def title_key(value):
    return re.sub(r'[^\w]+', '', unicodedata.normalize('NFKC', value).casefold())


def line_value(text, labels):
    match = re.search(r'(?im)^\s*(?:' + labels + r')\s*[:：]\s*(.+)$', text)
    return match.group(1).strip().strip('"“”\'') if match else ''


def identifiers(data):
    extra = data.get('extra', '')
    return {'doi': normalize_doi(data.get('DOI', '')),
            'pmid': line_value(extra, 'PMID'),
            'arxiv': (line_value(extra, 'arXiv(?: ID)?') or
                      (data.get('archiveID', '') if 'arxiv' in data.get('archive', '').lower() else '')).removeprefix('arXiv:').strip()}


def item_summary(item):
    data = item['data']
    library = item.get('library', {})
    prefix = 'groups' if library.get('type') == 'group' else 'users'
    alternate = item.get('links', {}).get('alternate', {}).get('href', '')
    ident = uri_identity(alternate)
    if not ident and isinstance(library.get('id'), int) and library['id'] > 0:
        ident = f"{prefix}/{library['id']}/items/{item['key']}"
    return {'key': item['key'], 'identity': ident, 'title': data.get('title', ''),
            'authors': ', '.join(c.get('name') or ' '.join(filter(None, (c.get('firstName'), c.get('lastName')))) for c in data.get('creators', [])),
            'year': (re.search(r'\b(?:19|20)\d{2}\b', data.get('date', '')) or [''])[0],
            'identifiers': identifiers(data), 'abstract': plain(data.get('abstractNote', ''))}


def sources(snapshot):
    by_key = {i['key']: i for i in snapshot['items']}
    output = []
    for item in snapshot['items']:
        data = item['data']
        kind = data.get('itemType')
        if kind not in ('annotation', 'note') or data.get('deleted'):
            continue
        # Highlighted text describes A; only the user's comment is a request.
        text = plain(data.get('annotationComment', '')) if kind == 'annotation' else plain(data.get('note', ''))
        owner_key, parent = data.get('parentItem'), None
        visited = set()
        while owner_key and owner_key not in visited:
            visited.add(owner_key)
            parent = by_key.get(owner_key)
            if not parent or parent['data'].get('itemType') not in ('attachment', 'note'):
                break
            owner_key = parent['data'].get('parentItem')
        owner = item_summary(parent) if parent and parent['data'].get('itemType') not in ('attachment', 'note') else None
        output.append({'source_id': snapshot['scope'] + ':' + item['key'], 'key': item['key'],
                       'kind': kind, 'version': item.get('version'), 'fingerprint': digest(data),
                       'parent_key': data.get('parentItem'), 'owner': owner, 'request_text': text,
                       'source_quote': data.get('annotationText', ''), 'page': data.get('annotationPageLabel', ''),
                       'requests_citation': bool(REQUEST.search(text)), 'withdrawn': bool(NEGATIVE.search(text))})
    return output


def extract(source):
    text = source['request_text']
    dois = sorted(set(normalize_doi(m.group()) for m in DOI.finditer(text)))
    pmids = sorted(set(re.findall(r'\bPMID\s*[:：]?\s*(\d{1,10})\b', text, re.I)))
    arxiv = sorted(set(re.findall(r'\barXiv\s*[:：]?\s*(\d{4}\.\d{4,5}(?:v\d+)?|[a-z-]+/\d{7}(?:v\d+)?)', text, re.I)))
    return {'identifiers': {'doi': dois, 'pmid': pmids, 'arxiv': arxiv},
            'title': line_value(text, 'title|标题|文献标题'),
            'target_quote': line_value(text, 'target|位置|目标原文|正文定位'),
            'section': line_value(text, 'section|章节|目标章节'),
            'claim': line_value(text, 'claim|论点|研究结论'),
            'ambiguous_identifiers': any(len(values) > 1 for values in (dois, pmids, arxiv))}


def match_items(request, papers):
    supplied = {key: values[0] for key, values in request['identifiers'].items() if len(values) == 1}
    if request['ambiguous_identifiers']:
        return [], 'multiple_requested_identities'
    if supplied:
        candidates = [p for p in papers if all(p['identifiers'].get(k, '').casefold() == v.casefold() for k, v in supplied.items())]
        return candidates, 'exact_identifier' if len(candidates) == 1 else 'duplicate_identity' if candidates else 'identity_not_found'
    if request['title']:
        key = title_key(request['title'])
        candidates = sorted(((difflib.SequenceMatcher(None, key, title_key(p['title'])).ratio(), p) for p in papers),
                            key=lambda pair: pair[0], reverse=True)
        return [p for ratio, p in candidates[:5] if ratio >= .55], 'title_needs_confirmation'
    return [], 'missing_identity'


def build_tasks(snapshot, document, resolutions=None):
    resolutions = resolutions or {}
    papers = [item_summary(i) for i in snapshot['items']
              if i['data'].get('itemType') not in ('annotation', 'note', 'attachment') and not i['data'].get('deleted')]
    all_sources = sources(snapshot)
    tasks = []
    for source in all_sources:
        if not source['requests_citation'] or source['withdrawn']:
            continue
        request = extract(source)
        candidates, identity_status = match_items(request, papers)
        resolution = resolutions.get(source['source_id'], {})
        if resolution.get('source_fingerprint') != source['fingerprint']:
            resolution = {}
        selected = candidates[0] if identity_status == 'exact_identifier' else None
        if resolution.get('item_identity'):
            selected = next((p for p in papers if p['identity'] == resolution['item_identity']), None)
            # An explicit DOI cannot be silently overridden by choosing another paper.
            if selected and any(values and selected['identifiers'].get(k, '').casefold() not in [v.casefold() for v in values]
                                for k, values in request['identifiers'].items()):
                selected = None
                identity_status = 'explicit_identifier_conflict'
            elif selected:
                identity_status = 'user_selected_identity'
        quote = resolution.get('target_quote') or request['target_quote']
        matches = locate(document, quote, '' if resolution.get('target_quote') else request['section'])
        if resolution.get('paragraph') is not None:
            matches = [m for m in matches if m['paragraph'] == resolution['paragraph']]
        target = matches[0] if len(matches) == 1 else None
        reasons = []
        if not source['owner']:
            reasons.append('source_owner_unresolved')
        if request['ambiguous_identifiers']:
            reasons.append('multiple_requested_identities')
        if selected is None:
            reasons.append(identity_status)
        elif not selected['identity']:
            reasons.append('library_identity_unresolved')
        if target is None:
            reasons.append('target_missing' if not matches else 'target_ambiguous')
        elif target['has_revision']:
            reasons.append('target_contains_tracked_changes')
        if document['issues']:
            reasons.append('document_field_problem')
        existing = []
        if target and selected:
            existing = [f for f in target['citations'] if selected['identity'] in f.get('identities', [])
                        and f['offset'] == target['offset']]
        tasks.append({'id': digest([source['source_id'], source['fingerprint'], document['sha256']])[:24],
                      'source': source, 'request': request, 'candidates': candidates,
                      'selected': selected, 'identity_status': identity_status, 'target': target,
                      'reasons': reasons, 'already_at_target': bool(existing),
                      'status': 'already_cited' if existing else 'needs_resolution' if reasons else 'needs_confirmation',
                      'claim_match': 'requires_human_evidence_review'})
    return tasks, all_sources, papers

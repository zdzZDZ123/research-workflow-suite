"""Read paper PDFs and verify model-proposed, page-located evidence."""
import hashlib
import io
import ipaddress
import json
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request

from pypdf import PdfReader
from notion_sync import plain
from paper_reading import Zotero


CARD_KINDS = {'question', 'population', 'variable', 'method', 'result', 'citation', 'topic'}
RELATION_TYPES = {'shared_topic', 'shared_variable', 'shared_method', 'citation',
                  'supports', 'conflicts', 'extends'}
DIRECTED_TYPES = {'citation', 'supports', 'conflicts', 'extends'}
MAX_PDF_BYTES = 40 * 1024 * 1024


def normalized(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip().casefold()


def paper_title(page):
    props = page.get('properties', {})
    for name in ('原标题', 'Name', 'Title', '论文名称'):
        if props.get(name, {}).get('type') == 'title':
            return plain(props[name]).strip()
    return ''


def paper_metadata(page):
    props = page.get('properties', {})
    title = paper_title(page)
    identity = plain(props.get('Paper Key', {})).strip()
    if not re.fullmatch(r'(user|group):\d+:[A-Z0-9]{8}', identity):
        identity = None
    doi = (props.get('DOI', {}).get('url') or '').strip()
    doi = re.sub(r'^https?://(?:dx\.)?doi\.org/', '', doi, flags=re.I).lower()
    pdf_url = (props.get('PDF URL', {}).get('url') or '').strip()
    return {'page_id': page['id'], 'title': title, 'identity': identity,
            'doi': doi, 'pdf_url': pdf_url, 'url': page.get('url') or 'https://www.notion.so/' + page['id'].replace('-', '')}


def eligible_record(page):
    meta = paper_metadata(page)
    # A title alone can be a Notion design row; require a stable source locator.
    return bool(meta['title'] and (meta['identity'] or meta['pdf_url']))


def safe_pdf_url(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        return False
    try:
        if not ipaddress.ip_address(parsed.hostname).is_global:
            return False
    except ValueError:
        if parsed.hostname.lower() in ('localhost',) or parsed.hostname.lower().endswith('.local'):
            return False
    return True


def fetch_pdf(url, opener=None):
    if not safe_pdf_url(url):
        raise ValueError('PDF_URL_UNSAFE')
    if opener is None:
        class SafeRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, request, fp, code, msg, headers, newurl):
                if not safe_pdf_url(newurl):
                    raise ValueError('PDF_REDIRECT_UNSAFE')
                return super().redirect_request(request, fp, code, msg, headers, newurl)
        opener = urllib.request.build_opener(SafeRedirect).open
    request = urllib.request.Request(url, headers={'User-Agent': 'PaperRelations/0.3'})
    with opener(request, timeout=60) as response:
        raw = response.read(MAX_PDF_BYTES + 1)
    if len(raw) > MAX_PDF_BYTES or not raw.startswith(b'%PDF-'):
        raise ValueError('PDF_INVALID_OR_TOO_LARGE')
    return raw


def zotero_pdf(meta, opener=None):
    if not meta['identity']:
        raise ValueError('ZOTERO_IDENTITY_MISSING')
    kind, library, key = meta['identity'].split(':')
    zotero = Zotero(kind, '0' if kind == 'user' else library)
    paper = zotero.item(key)
    actual = paper.get('library', {})
    if str(actual.get('id')) != library or actual.get('type') != kind or paper.get('key') != key:
        raise ValueError('ZOTERO_IDENTITY_MISMATCH')
    attachments = [a for a in zotero.children(key)
                   if a.get('data', {}).get('itemType') == 'attachment'
                   and a['data'].get('contentType') == 'application/pdf']
    if not attachments:
        raise ValueError('ZOTERO_PDF_MISSING')
    attachments.sort(key=lambda a: (a['data'].get('linkMode') not in ('imported_file', 'imported_url'), a['key']))
    attachment = attachments[0]
    base = zotero.base
    url = f"{base}/items/{attachment['key']}/file"
    opener = opener or urllib.request.urlopen
    try:
        with opener(urllib.request.Request(url, headers={'Zotero-API-Version': '3'}), timeout=60) as response:
            raw = response.read(MAX_PDF_BYTES + 1)
    except urllib.error.HTTPError as error:
        # The local API redirects imported attachments to their local file.
        # urllib intentionally refuses file:// redirects, so validate the
        # Zotero storage identity before opening that exact file ourselves.
        if error.code not in (301, 302, 303, 307, 308):
            raise
        target = urllib.parse.urlparse(error.headers.get('Location', ''))
        if target.scheme != 'file' or target.netloc not in ('', 'localhost') or target.query or target.fragment:
            raise ValueError('ZOTERO_FILE_REDIRECT_UNSAFE') from None
        path = Path(urllib.request.url2pathname(target.path)).resolve()
        filename = attachment['data'].get('filename')
        if path.parent.name != attachment['key'] or (filename and path.name != filename):
            raise ValueError('ZOTERO_FILE_REDIRECT_MISMATCH') from None
        if not path.is_file():
            raise ValueError('ZOTERO_PDF_FILE_MISSING') from None
        with path.open('rb') as file:
            raw = file.read(MAX_PDF_BYTES + 1)
    if len(raw) > MAX_PDF_BYTES or not raw.startswith(b'%PDF-'):
        raise ValueError('ZOTERO_PDF_INVALID_OR_TOO_LARGE')
    return raw, {'kind': 'zotero', 'attachment_key': attachment['key'],
                 'url': f'https://www.zotero.org/{kind}s/{library}/items/{attachment["key"]}'}


def notion_pdf_urls(api, page_id):
    urls = []
    for block in api.children(page_id):
        if block.get('type') != 'pdf':
            continue
        value = block.get('pdf', {})
        url = (value.get(value.get('type'), {}) or {}).get('url')
        if url:
            urls.append(url)
    return urls


def pdf_pages(raw):
    reader = PdfReader(io.BytesIO(raw), strict=False)
    if reader.is_encrypted:
        raise ValueError('PDF_ENCRYPTED')
    pages = [(p.extract_text() or '').strip() for p in reader.pages]
    if not pages or sum(len(p) for p in pages) < 800:
        raise ValueError('PDF_TEXT_UNAVAILABLE')
    return pages


def read_paper(api, page, zotero_reader=zotero_pdf, url_reader=fetch_pdf):
    meta = paper_metadata(page)
    errors = []
    raw = None
    source = None
    if meta['identity']:
        try:
            candidate, source = zotero_reader(meta)
            pages = pdf_pages(candidate)
            raw = candidate
        except Exception as error:
            errors.append('Zotero PDF: ' + str(error))
    if raw is None:
        block_urls = [] if meta['page_id'].startswith('zotero-') else notion_pdf_urls(api, meta['page_id'])
        candidates = list(dict.fromkeys(([meta['pdf_url']] if meta['pdf_url'] else []) + block_urls))
        for url in candidates:
            try:
                candidate = url_reader(url)
                pages = pdf_pages(candidate)
                raw = candidate
                source = {'kind': 'notion_pdf', 'url': url}
                break
            except Exception as error:
                errors.append('Notion PDF: ' + str(error))
    if raw is None:
        raise ValueError('FULLTEXT_UNAVAILABLE: ' + '; '.join(errors or ['no PDF source']))
    meta.update({'pdf_sha256': hashlib.sha256(raw).hexdigest(), 'pages': pages,
                 'source': source, 'page_count': len(pages)})
    return meta


def page_quote_valid(pages, page_number, quote):
    return (isinstance(page_number, int) and 1 <= page_number <= len(pages)
            and isinstance(quote, str) and len(quote.strip()) >= 12
            and normalized(quote) in normalized(pages[page_number - 1]))


def evidence_cards(model, paper):
    cards = []
    group = []
    size = 0
    def flush():
        nonlocal group, size
        if not group:
            return
        response = model.complete_json(
            'Extract only claims explicitly stated in the supplied PDF page text. Return a JSON object '
            'with cards:[{kind,concept,page,quote}]. Allowed kinds: question,population,variable,method,result,citation,topic. '
            'quote must be a verbatim contiguous passage from the numbered PDF page. No inference or invented citations.',
            json.dumps({'title': paper['title'], 'pages': group}, ensure_ascii=False))
        if not isinstance(response.get('cards'), list):
            raise ValueError('MODEL_CARDS_SCHEMA_INVALID')
        for card in response['cards']:
            if not isinstance(card, dict) or card.get('kind') not in CARD_KINDS:
                continue
            page_no, quote, concept = card.get('page'), card.get('quote'), card.get('concept')
            if not page_quote_valid(paper['pages'], page_no, quote) or not isinstance(concept, str) or not concept.strip():
                continue
            item = {'kind': card['kind'], 'concept': concept.strip()[:200], 'page': page_no, 'quote': quote.strip()}
            item['id'] = hashlib.sha256(json.dumps(item, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
            cards.append(item)
        group, size = [], 0
    for number, content in enumerate(paper['pages'], 1):
        if not content:
            continue
        for offset in range(0, len(content), 12000):
            value = content[offset:offset + 12000]
            if group and size + len(value) > 32000:
                flush()
            group.append({'page': number, 'text': value, 'character_offset': offset})
            size += len(value)
    flush()
    return list({c['id']: c for c in cards}.values())


def compare_cards(model, a, b, cards_a, cards_b):
    result = model.complete_json(
        'Compare only the supplied evidence cards. Return JSON {relationships:[{type,concept,summary,evidence_a,evidence_b,direction}]}. '
        'Allowed types: shared_topic,shared_variable,shared_method,citation,supports,conflicts,extends. '
        'evidence_a and evidence_b are card IDs or null. direction is a_to_b, b_to_a, or symmetric. '
        'No relationship is acceptable unless the cards justify it. Return an empty list when uncertain.',
        json.dumps({'paper_a': {'title': a['title'], 'doi': a['doi'], 'cards': cards_a},
                    'paper_b': {'title': b['title'], 'doi': b['doi'], 'cards': cards_b}}, ensure_ascii=False))
    if not isinstance(result.get('relationships'), list):
        raise ValueError('MODEL_RELATIONSHIPS_SCHEMA_INVALID')
    return verify_relationships(a, b, cards_a, cards_b, result['relationships'])


def verify_relationships(a, b, cards_a, cards_b, proposed):
    index_a = {x['id']: x for x in cards_a}
    index_b = {x['id']: x for x in cards_b}
    accepted = []
    for item in proposed:
        if not isinstance(item, dict) or item.get('type') not in RELATION_TYPES:
            continue
        kind = item['type']
        direction = item.get('direction')
        if direction not in ('a_to_b', 'b_to_a', 'symmetric'):
            continue
        if kind in DIRECTED_TYPES and direction == 'symmetric':
            continue
        if kind not in DIRECTED_TYPES and direction != 'symmetric':
            continue
        concept = str(item.get('concept') or '').strip()[:200]
        summary = str(item.get('summary') or '').strip()[:1600]
        ea = index_a.get(item.get('evidence_a'))
        eb = index_b.get(item.get('evidence_b'))
        if not concept or not summary or not ea or not eb:
            continue
        if not page_quote_valid(a['pages'], ea['page'], ea['quote']) or not page_quote_valid(b['pages'], eb['page'], eb['quote']):
            continue
        expected_kind = {'shared_topic': {'topic', 'question', 'population'},
                         'shared_variable': {'variable'}, 'shared_method': {'method'}}.get(kind)
        phrase = re.escape(normalized(concept))
        direct = bool(expected_kind and ea['kind'] in expected_kind and eb['kind'] in expected_kind
                      and len(normalized(concept)) >= 4
                      and re.search(r'(?<!\w)' + phrase + r'(?!\w)', normalized(ea['quote']))
                      and re.search(r'(?<!\w)' + phrase + r'(?!\w)', normalized(eb['quote'])))
        if kind == 'citation':
            target, source_quote = (b, ea['quote']) if direction == 'a_to_b' else (a, eb['quote'])
            source_card = ea if direction == 'a_to_b' else eb
            direct = bool(source_card['kind'] == 'citation' and target['doi'] and re.search(re.escape(target['doi']) + r'(?![a-z0-9._;/:-])',
                                                      normalized(source_quote)))
            source_label, target_label = ('A', 'B') if direction == 'a_to_b' else ('B', 'A')
            reason = (f"论文 {source_label} 的引文摘录包含论文 {target_label} 的 DOI：{target['doi']}。"
                      if direct else '指定方向的引文证据尚未通过目标 DOI 核对，需要人工审核。')
        elif expected_kind:
            reason = (f'两篇全文的对应类别摘录均可定位，并且均包含术语“{concept}”。'
                      if direct else '两端摘录可定位，但共享术语或证据类别尚未通过自动核对，需要人工审核。')
        else:
            reason = '两端摘录可定位；支持、冲突或扩展判断需要人工比较研究条件、方法与结果。'
        title = f"{a['title']} | {b['title']} | {kind} | {concept}"
        key = hashlib.sha256(json.dumps([a['page_id'], b['page_id'], kind, normalized(concept), direction], ensure_ascii=False).encode()).hexdigest()
        accepted.append({'key': key, 'title': title[:1800], 'type': kind, 'concept': concept,
                         'summary': summary, 'direction': direction, 'evidence_a': ea,
                         'decision_reason': reason,
                         'evidence_b': eb, 'auto_publish': direct, 'page_a': a['page_id'], 'page_b': b['page_id'],
                         'source_hash': hashlib.sha256((a['pdf_sha256'] + b['pdf_sha256']).encode()).hexdigest()})
    return list({r['key']: r for r in accepted}.values())

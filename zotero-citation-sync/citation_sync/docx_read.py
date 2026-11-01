"""Read-only OOXML inventory. A valid field structure is not proof of Word compatibility."""
from __future__ import annotations

import json
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from .util import SyncError, digest, file_hash

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
M = 'http://schemas.openxmlformats.org/officeDocument/2006/math'
q = lambda name: '{' + W + '}' + name


def xml(data):
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise SyncError('XML_ENTITY_FORBIDDEN')
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise SyncError('DOCX_XML_INVALID') from exc


def canonical(node):
    if node is None:
        return None
    attributes = sorted((k, v) for k, v in node.attrib.items()
                        if not k.split('}')[-1].startswith(('rsid', 'paraId', 'textId')))
    return [node.tag, attributes, node.text or '', [canonical(c) for c in node if c.tag != q('proofErr')]]


def uri_identity(uri):
    match = re.fullmatch(r'https?://(?:www\.|api\.)?zotero\.org/((?:users/(?:\d+|local/[^/]+)|groups/\d+)/items/[A-Z0-9]{8})/?', str(uri))
    return match.group(1) if match else None


def citation_identity(item):
    return sorted(set(identity for uri in item.get('uris', []) if (identity := uri_identity(uri))))


def read_docx(path):
    path = Path(path).expanduser().resolve()
    if path.suffix.lower() != '.docx':
        raise SyncError('DOCX_REQUIRED', '请选择 .docx 文件')
    if not path.is_file() or path.stat().st_size > 128 * 1024 * 1024:
        raise SyncError('DOCX_SIZE_OR_PATH_INVALID')
    before_hash = file_hash(path)
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 10000 or sum(i.file_size for i in entries) > 512 * 1024 * 1024:
                raise SyncError('DOCX_PACKAGE_TOO_LARGE')
            if len({i.filename for i in entries}) != len(entries):
                raise SyncError('DOCX_DUPLICATE_PART')
            if any(i.flag_bits & 1 or '..' in Path(i.filename).parts for i in entries):
                raise SyncError('DOCX_PACKAGE_UNSAFE')
            parts = {i.filename: archive.read(i) for i in entries}
    except (zipfile.BadZipFile, RuntimeError) as exc:
        raise SyncError('DOCX_PACKAGE_INVALID') from exc
    if not {'word/document.xml', '[Content_Types].xml', '_rels/.rels'} <= parts.keys():
        raise SyncError('DOCX_MAIN_PART_MISSING')
    if file_hash(path) != before_hash:
        raise SyncError('SOURCE_CHANGED')
    paragraphs, fields, problems = [], [], []
    mutable_parts = {'word/document.xml', 'docProps/core.xml', 'docProps/app.xml', 'docProps/custom.xml',
                     'word/styles.xml', 'word/numbering.xml', 'word/settings.xml', '[Content_Types].xml'}
    protected_parts = {name: digest(value) for name, value in parts.items()
                       if name not in mutable_parts and not name.endswith('.rels')}
    definitions, heading_styles = {}, {}
    for part_name in ('word/styles.xml', 'word/numbering.xml', 'word/settings.xml'):
        if part_name not in parts:
            continue
        tree = xml(parts[part_name])
        for index, element in enumerate(tree):
            # These fields describe editing/proofing state, not manuscript content.
            if part_name == 'word/settings.xml' and element.tag in (q('rsids'), q('proofState'), q('zoom'),
                                                                    q('view'), q('updateFields')):
                continue
            identifier = element.get(q('styleId')) or element.get(q('numId')) or element.get(q('abstractNumId'))
            key = part_name + '|' + element.tag + '|' + (identifier or '')
            if key in definitions:
                key += '|' + str(index)
            definitions[key] = digest(canonical(element))
            if part_name == 'word/styles.xml' and identifier:
                outline = element.find(q('pPr') + '/' + q('outlineLvl'))
                if outline is not None and outline.get(q('val'), '').isdigit():
                    heading_styles[identifier] = int(outline.get(q('val')))
    for part_name, data in parts.items():
        if part_name.endswith('.rels'):
            for relation in xml(data):
                definitions[part_name + '|' + relation.get('Id', '')] = digest(canonical(relation))
    objects = []
    style_id = None
    if 'docProps/custom.xml' in parts:
        props = xml(parts['docProps/custom.xml'])
        for prop in props:
            if not prop.get('name', '').startswith('ZOTERO_PREF_'):
                definitions['docProps/custom.xml|' + prop.get('name', '')] = digest(canonical(prop))
        chunks = sorted([n for n in props if re.fullmatch(r'ZOTERO_PREF_\d+', n.get('name', ''))],
                        key=lambda n: int(n.get('name').rsplit('_', 1)[1]))
        preferences = ''.join(''.join(n.itertext()) for n in chunks)
        if preferences:
            try:
                if preferences.lstrip().startswith('<'):
                    style = xml(preferences.encode()).find('style')
                    style_id = style.get('id') if style is not None else None
                else:
                    style_id = json.loads(preferences).get('style', {}).get('styleID')
            except (ValueError, SyncError):
                problems.append('ZOTERO_PREFERENCES_INVALID')
    if any(name.lower().endswith('vbaproject.bin') for name in parts):
        problems.append('MACRO_DOCUMENT_UNSUPPORTED')
    trees = [(name, xml(data)) for name, data in parts.items()
             if re.fullmatch(r'word/(document|footnotes|endnotes|header\d+|footer\d+)\.xml', name)]
    for name, root in trees:
        stack = []
        local_paragraphs = []
        transient_bookmarks = {n.get(q('id')) for n in root.iter(q('bookmarkStart'))
                               if n.get(q('name')) == '_GoBack'}

        def finish(field):
            instruction = field['instruction'].strip()
            if 'ZOTERO_ITEM' in instruction:
                field['kind'] = 'citation'
                try:
                    if not instruction.startswith('ADDIN ZOTERO_ITEM CSL_CITATION '):
                        raise ValueError()
                    payload = json.loads(instruction[len('ADDIN ZOTERO_ITEM CSL_CITATION '):])
                    if (not payload.get('citationID') or not payload.get('citationItems')
                            or not field['separated'] or not field['display'].strip()):
                        raise ValueError()
                    for item in payload['citationItems']:
                        if not citation_identity(item):
                            raise ValueError()
                    identities = [citation_identity(item) for item in payload['citationItems']]
                    for index, identity in enumerate(identities):
                        if any(set(identity) & set(prior) for prior in identities[:index]):
                            problems.append('DUPLICATE_ITEM_IN_CITATION')
                    field['payload'] = payload
                    field['identities'] = sorted({v for item in payload['citationItems'] for v in citation_identity(item)})
                    if field['end_paragraph'] != field['paragraph']:
                        problems.append('CITATION_CROSSES_PARAGRAPHS')
                except (ValueError, TypeError, KeyError, AttributeError):
                    problems.append('ZOTERO_FIELD_INVALID')
            elif 'ZOTERO_BIBL' in instruction:
                field['kind'] = 'bibliography'
                if not field['separated']:
                    problems.append('BIBLIOGRAPHY_FIELD_INVALID')
            else:
                field['kind'] = 'other'
            fields.append(field)

        def walk(node, paragraph=None, run_style=None, revision=False, in_table=False):
            if node.tag == q('p'):
                pstyle = node.find(q('pPr') + '/' + q('pStyle'))
                outline = node.find(q('pPr') + '/' + q('outlineLvl'))
                heading = heading_styles.get(pstyle.get(q('val'))) if pstyle is not None else None
                if outline is not None and outline.get(q('val'), '').isdigit():
                    heading = int(outline.get(q('val')))
                paragraph = {'part': name, 'index': len(local_paragraphs), 'text': '', 'visible_text': '',
                             'style': canonical(node.find(q('pPr'))), 'runs': [], 'has_revision': False,
                             'in_table': in_table, 'has_zotero': False, 'heading_level': heading}
                local_paragraphs.append(paragraph)
            if node.tag == q('r'):
                run_style = canonical(node.find(q('rPr')))
            revision = revision or node.tag in (q('ins'), q('del'), q('moveFrom'), q('moveTo'))
            in_table = in_table or node.tag == q('tbl')
            if paragraph is not None and revision:
                paragraph['has_revision'] = True
            in_zotero_display = any('ZOTERO_' in f['instruction'] and f['separated'] for f in stack)
            transient_bookmark = node.tag in (q('bookmarkStart'), q('bookmarkEnd')) and node.get(q('id')) in transient_bookmarks
            if not in_zotero_display and not transient_bookmark and node.tag in ('{' + M + '}oMath', q('drawing'), q('pict'), q('tblPr'), q('tblGrid'), q('sectPr'),
                            q('tcPr'), q('trPr'), q('ins'), q('del'), q('moveFrom'), q('moveTo'),
                            q('commentRangeStart'), q('commentRangeEnd'), q('commentReference'),
                            q('footnoteReference'), q('endnoteReference'), q('bookmarkStart'), q('bookmarkEnd')):
                objects.append([name, paragraph['index'] if paragraph else -1,
                                len(paragraph['text']) if paragraph else 0, canonical(node)])
            if node.tag == q('hyperlink') and not in_zotero_display:
                objects.append([name, paragraph['index'] if paragraph else -1,
                                len(paragraph['text']) if paragraph else 0, node.tag, sorted(node.attrib.items())])
            if node.tag == q('bookmarkStart') and node.get(q('name'), '').startswith('ZOTERO_'):
                problems.append('ZOTERO_BOOKMARK_UNSUPPORTED')
            simple = node.tag == q('fldSimple')
            if simple or (node.tag == q('fldChar') and node.get(q('fldCharType')) == 'begin'):
                stack.append({'part': name, 'paragraph': paragraph['index'] if paragraph else -1,
                              'offset': len(paragraph['text']) if paragraph else 0,
                              'instruction': node.get(q('instr'), '') if simple else '',
                              'separated': simple, 'display': '', 'end_paragraph': -1})
                if simple and 'ZOTERO' in stack[-1]['instruction']:
                    problems.append('ZOTERO_SIMPLE_FIELD_UNVERIFIED')
            if node.tag == q('instrText'):
                if not stack:
                    problems.append('ORPHAN_FIELD_INSTRUCTION')
                else:
                    stack[-1]['instruction'] += node.text or ''
            if node.tag == q('fldChar') and node.get(q('fldCharType')) == 'separate':
                if not stack or stack[-1]['separated']:
                    problems.append('FIELD_SEPARATOR_INVALID')
                else:
                    stack[-1]['separated'] = True
            if node.tag in (q('t'), q('delText'), q('tab'), q('br')) and paragraph is not None:
                text = '\t' if node.tag == q('tab') else '\n' if node.tag == q('br') else node.text or ''
                paragraph['visible_text'] += text
                for field in stack:
                    if field['separated']:
                        field['display'] += text
                if any('ZOTERO_' in field['instruction'] for field in stack):
                    paragraph['has_zotero'] = True
                else:
                    paragraph['text'] += text
                    if paragraph['runs'] and paragraph['runs'][-1][0] == run_style:
                        paragraph['runs'][-1][1] += text
                    else:
                        paragraph['runs'].append([run_style, text])
            for child in node:
                walk(child, paragraph, run_style, revision, in_table)
            if simple or (node.tag == q('fldChar') and node.get(q('fldCharType')) == 'end'):
                if not stack:
                    problems.append('FIELD_END_WITHOUT_BEGIN')
                else:
                    field = stack.pop()
                    field['end_paragraph'] = paragraph['index'] if paragraph else -1
                    finish(field)
        walk(root)
        if stack:
            problems.append('FIELD_UNCLOSED')
        paragraphs.extend(local_paragraphs)
    citations = [f for f in fields if f['kind'] == 'citation']
    ids = [f.get('payload', {}).get('citationID') for f in citations]
    if len(ids) != len(set(ids)):
        problems.append('DUPLICATE_CITATION_ID')
    protected = [[p['part'], p['text'], p['style'], p['runs'], p['has_revision'], p['in_table']]
                 for p in paragraphs if p['text'] or not p['has_zotero']]
    for paragraph in paragraphs:
        paragraph['citations'] = [f for f in citations if f['part'] == paragraph['part'] and f['paragraph'] == paragraph['index']]
    return {'path': str(path), 'sha256': before_hash, 'paragraphs': paragraphs, 'fields': fields,
            'citations': citations, 'bibliographies': [f for f in fields if f['kind'] == 'bibliography'],
            'issues': sorted(set(problems)), 'style_id': style_id,
            'protected_content': protected, 'protected_parts': protected_parts,
            'protected_definitions': definitions,
            'objects_hash': digest(objects), 'structurally_valid': not problems,
            'word_dynamic_verified': False}


def locate(document, quote, section=''):
    if not quote or len(quote.strip()) < 6:
        return []
    matches = []
    section = section.strip()
    headings = [p for p in document['paragraphs'] if p['part'] == 'word/document.xml'
                and p['heading_level'] is not None and p['text'].strip() == section]
    if section and len(headings) != 1:
        return []
    active_section, level = not section, None
    for paragraph in document['paragraphs']:
        if paragraph['part'] != 'word/document.xml':
            continue
        # Section text is a filter only; an exact quote is always required.
        if section and paragraph['index'] == headings[0]['index']:
            active_section, level = True, paragraph['heading_level']
            continue
        if section and active_section and paragraph['heading_level'] is not None and paragraph['heading_level'] <= level:
            break
        if not active_section:
            continue
        text = paragraph['text']
        start = 0
        while (position := text.find(quote, start)) >= 0:
            matches.append({'part': paragraph['part'], 'paragraph': paragraph['index'],
                            'quote': quote, 'offset': position + len(quote), 'text': text,
                            'has_revision': paragraph['has_revision'], 'in_table': paragraph['in_table'],
                            'citations': paragraph['citations']})
            start = position + 1
    return matches

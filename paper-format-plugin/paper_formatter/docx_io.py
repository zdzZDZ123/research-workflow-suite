"""Targeted OOXML edits; preserve all other package parts and scientific content."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import zipfile
import posixpath
from pathlib import Path, PurePosixPath

from lxml import etree

from .contracts import FormatterError, digest, issue
from .profiles import validate_profile

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
NS = {"w": W, "r": R, "m": "http://schemas.openxmlformats.org/officeDocument/2006/math",
      "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
      "a": "http://schemas.openxmlformats.org/drawingml/2006/main"}


def q(name):
    return "{" + W + "}" + name


def xml(data: bytes):
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise FormatterError("UNSAFE_DOCX_XML")
    return etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True, remove_blank_text=False))


def serialize(root):
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def read_package(path: str | Path) -> dict[str, bytes]:
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 20000 or sum(x.file_size for x in entries) > 512_000_000:
                raise FormatterError("DOCX_PACKAGE_TOO_LARGE")
            names = [x.filename for x in entries]
            if len(names) != len(set(names)):
                raise FormatterError("DUPLICATE_PACKAGE_PART")
            for name in names:
                if PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts or "\\" in name:
                    raise FormatterError("INVALID_PACKAGE_PART")
            return {info.filename: archive.read(info) for info in entries}
    except zipfile.BadZipFile as exc:
        raise FormatterError("INVALID_OR_ENCRYPTED_DOCX") from exc


def write_package(path, parts):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)


def child(parent, name, *, first=False):
    existing = parent.find(q(name))
    if existing is None:
        existing = etree.Element(q(name))
        if first:
            parent.insert(0, existing)
        else:
            parent.append(existing)
    return existing


def set_attrs(node, **values):
    for key, value in values.items():
        node.set(q(key), str(value))


def fields(root):
    """Return complex fields without altering their XML nodes."""
    stack, found = [], []
    for node in root.iter():
        if node.tag == q("fldChar"):
            kind = node.get(q("fldCharType"))
            if kind == "begin":
                stack.append({"begin": node, "instructions": [], "results": [], "separated": False})
            elif kind == "separate" and stack:
                stack[-1]["separated"] = True
                stack[-1]["separate"] = node
            elif kind == "end" and stack:
                field = stack.pop()
                field["end"] = node
                field["instruction"] = "".join(x.text or "" for x in field["instructions"])
                field["text"] = "".join(x.text or "" for x in field["results"])
                found.append(field)
        elif node.tag == q("instrText") and stack:
            stack[-1]["instructions"].append(node)
        elif node.tag == q("t") and stack and stack[-1]["separated"]:
            stack[-1]["results"].append(node)
    return found


def citation_data(field):
    text = field["instruction"]
    if "ZOTERO_ITEM CSL_CITATION" not in text:
        return None
    try:
        value = json.JSONDecoder().raw_decode(text[text.index("{"):])[0]
        if not value.get("citationID") or not isinstance(value.get("citationItems"), list):
            raise ValueError()
        return value
    except (ValueError, KeyError):
        raise FormatterError("UNREADABLE_CITATION_FIELD")


def bibliography_paragraphs(root):
    paragraphs = root.findall('.//w:p', NS)
    result = set()
    for field in fields(root):
        if 'ZOTERO_BIBL' not in field['instruction']:
            continue
        start = next((n for n in field['begin'].iterancestors() if n.tag == q('p')), None)
        end = next((n for n in field['end'].iterancestors() if n.tag == q('p')), None)
        if start is not None and end is not None:
            result.update(paragraphs[paragraphs.index(start):paragraphs.index(end) + 1])
    return result


def reference_rule(rules):
    typography = {k: v for k, v in rules.get('body', {}).items() if k in ('font', 'latin_font', 'size_pt')}
    return {**typography, **{k: v for k, v in rules.get('references', {}).items() if k not in ('style_id', 'locale', 'csl_xml')}}


def security_issues(parts):
    problems = []
    for name, data in parts.items():
        lower = name.lower()
        if "vbaproject" in lower or lower.startswith("_xmlsignatures/"):
            problems.append(issue("ACTIVE_OR_SIGNED_DOCUMENT", "宏或数字签名文件不能自动改写", part=name))
        if lower.endswith(".rels"):
            for rel in xml(data):
                if rel.get("TargetMode") == "External" and not rel.get("Type", "").endswith("/hyperlink"):
                    problems.append(issue("EXTERNAL_RESOURCE", "文档包含外部链接资源；先在本机嵌入该资源", part=name))
        if lower.endswith(".xml") and lower.startswith("word/"):
            root = xml(data)
            instructions=[n.text or '' for n in root.findall('.//w:instrText',NS)]
            instructions += [n.get(q('instr'),'') for n in root.findall('.//w:fldSimple',NS)]
            for instruction in instructions:
                if re.search(r"\b(DDEAUTO|DDE|INCLUDEPICTURE|INCLUDETEXT|LINK|DATABASE)\b", instruction, re.I):
                    problems.append(issue("EXTERNAL_FIELD", "文档包含可能访问外部资源的域", part=name))
            if root.findall(".//w:altChunk", NS):
                problems.append(issue("UNSUPPORTED_ALTCHUNK", "请先在文字处理软件中将嵌入内容展开", part=name))
    return problems


def inspect_docx(path) -> dict:
    parts = path if isinstance(path,dict) else read_package(path)
    root = xml(parts["word/document.xml"])
    all_fields = fields(root)
    citations, ignored = [], set()
    for field in all_fields:
        citation = citation_data(field)
        if citation:
            citations.append(citation)
            ignored.update(field["results"])
        elif "ZOTERO_BIBL" in field["instruction"]:
            ignored.update(field["results"])
    content = []
    captions = []
    for paragraph in root.findall(".//w:p", NS):
        text = "".join(t.text or "" for t in paragraph.findall(".//w:t", NS) if t not in ignored)
        pstyle=paragraph.find('w:pPr/w:pStyle',NS)
        if re.match(r"^\s*(?:Figure|Fig\.?|Table|图|表)\s*\d+", text, re.I) or (pstyle is not None and pstyle.get(q('val'),'').lower()=='caption'):
            captions.append(re.sub(r"^\s*(?:Figure|Fig\.?|Table|图|表)\s*\d+[.．:：]?\s*", "", text, flags=re.I))
        elif text.strip():
            content.append(text)
    auxiliary = {name: hashlib.sha256(data).hexdigest() for name, data in parts.items()
                 if name in ("word/footnotes.xml", "word/endnotes.xml", "word/comments.xml")}
    math = [etree.tostring(node, method="c14n").decode() for node in root.findall(".//m:oMath", NS)]
    native_fields, stories = {}, {}
    for name, data in parts.items():
        if not name.startswith('word/') or not name.endswith('.xml'):
            continue
        story = xml(data)
        instructions = [f['instruction'] for f in fields(story)]
        instructions += [n.get(q('instr'), '') for n in story.findall('.//w:fldSimple', NS)]
        if instructions:
            native_fields[name] = instructions
        if name.startswith(('word/header', 'word/footer')):
            # Ignore PAGE display results, but retain prose, drawings and references.
            ignored_story = {t for f in fields(story) if re.match(r'^\s*(PAGE|NUMPAGES)\b', f['instruction']) for t in f['results']}
            ignored_story.update(t for f in story.findall('.//w:fldSimple', NS)
                                 if re.match(r'^\s*(PAGE|NUMPAGES)\b', f.get(q('instr'), '')) for t in f.findall('.//w:t', NS))
            stories[name] = {'text': [t.text or '' for t in story.findall('.//w:t', NS) if t not in ignored_story],
                             'objects': story.xpath('.//@r:embed | .//@r:link', namespaces=NS)}
    custom = xml(parts['docProps/custom.xml']) if 'docProps/custom.xml' in parts else []
    zotero_prefs = ''.join(''.join(n.itertext()) for n in sorted(
        [n for n in custom if re.fullmatch(r'ZOTERO_PREF_\d+', n.get('name', ''))],
        key=lambda n: int(n.get('name').rsplit('_', 1)[1])))
    return {"content_hash": digest({"text": content, "captions": sorted(captions), "auxiliary": auxiliary}),
            "text": "\n".join(content + captions + root.xpath('.//m:t/text()',namespaces=NS)), "paragraphs": content, "captions": captions,
            "assets": {name: hashlib.sha256(data).hexdigest() for name, data in parts.items() if name.startswith(("word/media/", "word/embeddings/"))},
            "math": [digest(value) for value in math], "citations": citations,
            "tables": len(root.findall(".//w:tbl", NS)), "fields": [f["instruction"] for f in all_fields],
            'native_fields': native_fields, 'stories': stories, 'zotero_prefs': zotero_prefs,
            'story_references': [dict(n.attrib) for n in root.iter() if n.tag in (q('headerReference'), q('footerReference'))],
            "issues": security_issues(parts)}


def preservation_issues(before, after):
    """Compare a correction to the program's explicitly transformed expectation."""
    problems = []
    for name, code in (('content_hash', 'CONTENT_CHANGED'), ('assets', 'ASSET_CHANGED'),
                       ('math', 'FORMULA_CHANGED'), ('tables', 'TABLE_COUNT_CHANGED'),
                       ('native_fields', 'NATIVE_FIELDS_CHANGED'), ('citations', 'CITATION_METADATA_CHANGED'),
                       ('zotero_prefs', 'CITATION_METADATA_CHANGED'), ('stories', 'HEADER_FOOTER_CHANGED'),
                       ('story_references', 'HEADER_FOOTER_CHANGED')):
        if before.get(name) != after.get(name):
            problems.append(issue(code, '校对稿改变了必须保留的内容或原生字段：' + name))
    return problems


def style_properties(ppr, rpr, rule):
    if "font" in rule or "latin_font" in rule:
        fonts = child(rpr, "rFonts")
        # Explicit fonts must not be overridden by inherited theme fonts.
        for name in ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme"):
            fonts.attrib.pop(q(name), None)
        if "font" in rule:
            set_attrs(fonts, eastAsia=rule["font"], ascii=rule.get("latin_font", rule["font"]), hAnsi=rule.get("latin_font", rule["font"]))
        if "latin_font" in rule:
            set_attrs(fonts, ascii=rule["latin_font"], hAnsi=rule["latin_font"])
    if "size_pt" in rule:
        for name in ("sz", "szCs"):
            set_attrs(child(rpr, name), val=round(rule["size_pt"] * 2))
    for option, name in (("bold", "b"), ("italic", "i")):
        if option in rule:
            set_attrs(child(rpr, name), val="1" if rule[option] else "0")
    if "align" in rule:
        set_attrs(child(ppr, "jc"), val="both" if rule["align"] == "justify" else rule["align"])
    if "first_line_mm" in rule:
        indent = child(ppr, "ind")
        indent.attrib.pop(q("firstLineChars"), None)
        indent.attrib.pop(q("hangingChars"), None)
        indent.attrib.pop(q("firstLine"), None)
        indent.attrib.pop(q("hanging"), None)
        indent.set(q("firstLine" if rule["first_line_mm"] >= 0 else "hanging"), str(round(abs(rule["first_line_mm"]) / 25.4 * 1440)))
    spacing_values = {}
    for name, key in (("before", "space_before_pt"), ("after", "space_after_pt")):
        if key in rule:
            spacing_values[name] = round(rule[key] * 20)
    if "line_spacing" in rule:
        spacing_values.update(line=round(rule["line_spacing"] * 240), lineRule="auto")
    if "line_spacing_pt" in rule:
        spacing_values.update(line=round(rule["line_spacing_pt"] * 20), lineRule="exact")
    if spacing_values:
        set_attrs(child(ppr, "spacing"), **spacing_values)
    if "keep_with_next" in rule:
        set_attrs(child(ppr, "keepNext"), val="1" if rule["keep_with_next"] else "0")


def apply_to_paragraph(paragraph, rule):
    ppr = child(paragraph, "pPr", first=True)
    style_properties(ppr, child(ppr, "rPr"), rule)
    # Keep emphasis, vertical alignment and field instructions intact.
    for run in paragraph.findall(".//w:r", NS):
        style_properties(ppr, child(run, "rPr", first=True), rule)


def page_geometry(section, rule):
    size = child(section, "pgSz")
    for key, attr in (("width_mm", "w"), ("height_mm", "h")):
        if key in rule:
            size.set(q(attr), str(round(rule[key] / 25.4 * 1440)))
    if "width_mm" in rule or "height_mm" in rule:
        size.attrib.pop(q("orient"), None)
    if rule.get("margins_mm"):
        margins = child(section, "pgMar")
        for key, value in rule["margins_mm"].items():
            margins.set(q(key), str(round(value / 25.4 * 1440)))
    if "columns" in rule:
        columns = child(section, "cols")
        for node in list(columns):
            columns.remove(node)
        set_attrs(columns, num=rule["columns"], equalWidth="1")
    if "column_gap_mm" in rule:
        set_attrs(child(section, "cols"), space=round(rule["column_gap_mm"] / 25.4 * 1440))
    if "line_numbers" in rule:
        existing = section.find(q("lnNumType"))
        if rule["line_numbers"]:
            set_attrs(child(section, "lnNumType"), countBy=1, restart="newPage", distance=100)
        elif existing is not None:
            section.remove(existing)


def usable_width(section):
    size = section.find(q("pgSz"))
    margin = section.find(q("pgMar"))
    columns = section.find(q("cols"))
    width = int(size.get(q("w"), 11906)) if size is not None else 11906
    left = int(margin.get(q("left"), 1440)) if margin is not None else 1440
    right = int(margin.get(q("right"), 1440)) if margin is not None else 1440
    count = int(columns.get(q("num"), 1)) if columns is not None else 1
    gap = int(columns.get(q("space"), 720)) if columns is not None else 720
    gutter = int(margin.get(q('gutter'), 0)) if margin is not None else 0
    if count < 1:
        raise FormatterError('PAGE_NO_USABLE_WIDTH')
    available = (width - left - right - gutter - (count - 1) * gap) / count
    if columns is not None and columns.get(q('equalWidth')) == '0':
        explicit = [int(col.get(q('w'), 0)) for col in columns.findall(q('col'))]
        if explicit and all(value > 0 for value in explicit):
            available = min(available, min(explicit))
    if available <= 0:
        raise FormatterError('PAGE_NO_USABLE_WIDTH')
    return available


def section_widths(root):
    """Section properties describe the content preceding their section break."""
    body = root.find(q('body'))
    widths, pending = {}, []
    for block in body:
        pending.append(block)
        section = block if block.tag == q('sectPr') else block.find('w:pPr/w:sectPr', NS)
        if section is not None:
            width = usable_width(section)
            for entry in pending:
                widths[entry] = width
            pending = []
    for entry in pending:
        widths[entry] = 9026
    return widths


def available_width(node, widths):
    """Return the current section/cell content width in twips, after parent fitting."""
    if isinstance(widths, dict):
        block = next((n for n in (node, *node.iterancestors()) if n in widths), None)
        width = widths.get(block, 9026)
    else:
        width = widths
    cell = next((n for n in node.iterancestors() if n.tag == q('tc')), None)
    if cell is not None:
        table = next(n for n in cell.iterancestors() if n.tag == q('tbl'))
        value = cell.find('w:tcPr/w:tcW', NS)
        if value is not None and value.get(q('type'), 'dxa') == 'dxa' and int(value.get(q('w'), 0)) > 0:
            width = min(width, int(value.get(q('w'))))
        # Cell overrides take precedence over table defaults; Word's default is 5.4 pt.
        for side, alias in (('start', 'left'), ('end', 'right')):
            margin = None
            for container in (cell.find('w:tcPr/w:tcMar', NS), table.find('w:tblPr/w:tblCellMar', NS)):
                if container is not None:
                    margin = container.find(q(side))
                    if margin is None:
                        margin = container.find(q(alias))
                    if margin is not None:
                        break
            width -= int(margin.get(q('w'), 0)) if margin is not None else 108
    if width <= 0:
        raise FormatterError('OBJECT_NO_USABLE_WIDTH', '图表所在单元格没有可用宽度，请调整表格或页边距')
    return width


def apply_tables(root, config, width):
    for table in root.findall(".//w:tbl", NS):
        props = child(table, "tblPr", first=True)
        available = available_width(table, width)
        indent = props.find(q('tblInd'))
        if indent is not None and indent.get(q('type'), 'dxa') == 'dxa':
            available -= max(0, int(indent.get(q('w'), 0)))
        if available <= 0:
            raise FormatterError('OBJECT_NO_USABLE_WIDTH', '表格缩进后没有可用宽度')
        border_kind = config.get("borders")
        if border_kind:
            borders = child(props, "tblBorders")
            for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
                val = "single" if border_kind == "grid" or (border_kind == "three_line" and side in ("top", "bottom")) else "nil"
                set_attrs(child(borders, side), val=val, sz=8, color="000000")
            if border_kind == "three_line":
                first = table.find(q("tr"))
                if first is not None:
                    for cell in first.findall(q("tc")):
                        set_attrs(child(child(child(cell, "tcPr", first=True), "tcBorders"), "bottom"), val="single", sz=6, color="000000")
        grid = table.find(q('tblGrid'))
        old = [max(1, int(col.get(q('w'), '1'))) for col in grid] if grid is not None else []
        desired = props.find(q('tblW'))
        requested = int(desired.get(q('w'), 0)) if desired is not None and desired.get(q('type')) == 'dxa' else 0
        # Changing columns must also constrain existing wide objects, even when no
        # separate table-fitting instruction was supplied. Smaller tables stay small.
        if config.get("fit_width") or max(sum(old), requested) > available:
            target = max(1, int(available))
            set_attrs(child(props, "tblW"), w=target, type="dxa")
            set_attrs(child(props, "tblLayout"), type="fixed")
            if grid is not None and len(grid):
                total = sum(old)
                boundaries = [round(target * sum(old[:i]) / total) for i in range(len(old) + 1)]
                widths = [right - left for left, right in zip(boundaries, boundaries[1:])]
                for column, amount in zip(grid, widths):
                    set_attrs(column, w=amount)
                for row in table.findall(q("tr")):
                    before = row.find('w:trPr/w:gridBefore', NS)
                    index = int(before.get(q('val'), 0)) if before is not None else 0
                    for cell in row.findall(q("tc")):
                        properties = child(cell, "tcPr", first=True)
                        span = properties.find(q("gridSpan"))
                        count = int(span.get(q("val"), "1")) if span is not None else 1
                        set_attrs(child(properties, "tcW"), w=sum(widths[index:index + count]), type="dxa")
                        index += count
        if config.get("repeat_header") and table.find(q("tr")) is not None:
            child(child(table.find(q("tr")), "trPr", first=True), "tblHeader")


def apply_images(root, max_width_mm, widths=None):
    for drawing in root.findall(".//w:drawing", NS):
        width = available_width(drawing, widths if widths is not None else max_width_mm * 1440 / 25.4)
        paragraph = next((n for n in drawing.iterancestors() if n.tag == q('p')), None)
        indent = paragraph.find('w:pPr/w:ind', NS) if paragraph is not None else None
        if indent is not None:
            width -= sum(max(0, int(indent.get(q(key), 0))) for key in ('left', 'right', 'firstLine'))
        if width <= 0:
            raise FormatterError('OBJECT_NO_USABLE_WIDTH', '图片所在段落缩进后没有可用宽度')
        maximum = max(1, int(min(max_width_mm * 36000, width * 635)))
        extent = drawing.find(".//wp:extent", NS)
        if extent is None:
            continue
        x, y = int(extent.get("cx", "0")), int(extent.get("cy", "0"))
        if x > maximum and x:
            new_y = round(y * maximum / x)
            extent.set("cx", str(maximum))
            extent.set("cy", str(new_y))
            for inner in drawing.findall(".//a:xfrm/a:ext", NS):
                inner.set("cx", str(maximum))
                inner.set("cy", str(new_y))


def header_footer(parts, sections, kind, rule):
    relname = "word/_rels/document.xml.rels"
    rels = xml(parts[relname]) if relname in parts else etree.Element("{" + PKG + "}Relationships")
    types = xml(parts["[Content_Types].xml"])
    touched, inherited_default = set(), False
    for number, section in enumerate(sections):
        references = list(section.findall(q(kind + 'Reference')))
        if not inherited_default and not any(n.get(q('type')) == 'default' for n in references):
            index = number
            while f'word/{kind}-paper-format-{index}.xml' in parts:
                index += 1
            name = f'{kind}-paper-format-{index}.xml'
            rid = f'rIdPaperFormat{kind.title()}{index}'
            while any(n.get('Id') == rid for n in rels):
                rid += 'x'
            parts['word/' + name] = serialize(etree.Element(q('hdr' if kind == 'header' else 'ftr'), nsmap={'w': W}))
            reference = etree.Element(q(kind + 'Reference'), {q('type'): 'default', '{' + R + '}id': rid})
            section.insert(0, reference)
            references.append(reference)
            etree.SubElement(rels, '{' + PKG + '}Relationship', Id=rid, Type=R + '/' + kind, Target=name)
            etree.SubElement(types, '{' + CT + '}Override', PartName='/word/' + name,
                             ContentType=f'application/vnd.openxmlformats-officedocument.wordprocessingml.{kind}+xml')
        inherited_default |= any(n.get(q('type')) == 'default' for n in references)
        for reference in references:
            relationship = next((n for n in rels if n.get('Id') == reference.get('{' + R + '}id')), None)
            if relationship is None:
                raise FormatterError('HEADER_FOOTER_RELATIONSHIP_MISSING')
            target = relationship.get('Target', '')
            name = posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'word/' + target)
            if name not in parts or not name.startswith('word/'):
                raise FormatterError('HEADER_FOOTER_PART_MISSING')
            if name in touched:
                continue
            touched.add(name)
            root = xml(parts[name])
            paragraphs = root.findall('.//w:p', NS)
            if not paragraphs:
                paragraphs = [etree.SubElement(root, q('p'))]
            if 'text' in rule:
                # Only an explicit text rule replaces prose. Keep fields, logos and every section variant.
                field_text = {t for f in fields(root) for t in f['results']}
                field_text.update(t for f in root.findall('.//w:fldSimple', NS) for t in f.findall('.//w:t', NS))
                for text in root.findall('.//w:t', NS):
                    if text not in field_text:
                        text.text = ''
                etree.SubElement(etree.SubElement(paragraphs[0], q('r')), q('t')).text = rule['text']
            page_fields = [f for f in fields(root) if re.match(r'^\s*PAGE\b', f['instruction'])]
            page_simple = [f for f in root.findall('.//w:fldSimple', NS) if re.match(r'^\s*PAGE\b', f.get(q('instr'), ''))]
            if rule.get('page_number') and not page_fields and not page_simple:
                paragraph = etree.SubElement(root, q('p')) if any(p.xpath('.//w:t/text()', namespaces=NS) or p.findall('.//w:drawing', NS) for p in paragraphs) else paragraphs[0]
                if paragraph not in paragraphs:
                    paragraphs.append(paragraph)
                field = etree.SubElement(paragraph, q('fldSimple'), {q('instr'): ' PAGE '})
                etree.SubElement(etree.SubElement(field, q('r')), q('t')).text = '1'
            elif rule.get('page_number') is False and (page_fields or page_simple):
                # Removing a complex field can destroy adjacent content; require source correction.
                raise FormatterError('PAGE_FIELD_REMOVAL_REQUIRES_CORRECTION', '请在 Word 中删除页码域后重试')
            for paragraph in paragraphs:
                apply_to_paragraph(paragraph, {k: v for k, v in rule.items() if k in ('size_pt', 'align')})
            parts[name] = serialize(root)
    parts[relname], parts["[Content_Types].xml"] = serialize(rels), serialize(types)


def format_docx(source, target, profile, *, citation_updates=None):
    profile = validate_profile(profile)
    parts = read_package(source)
    problems = security_issues(parts)
    if problems:
        return {"passed": False, "issues": problems}
    before = inspect_docx(source)
    root = xml(parts["word/document.xml"])
    rules = profile["rules"]
    from .captions import update_captions
    caption_issues,caption_changes=update_captions(root,rules.get('captions',{}),NS,q)
    problems+=caption_issues
    expected_parts={**parts,'word/document.xml':serialize(root)}
    expected=inspect_docx(expected_parts)
    # Create CSL result runs first, so they receive the same validated typography
    # as the rest of their paragraphs. Bibliography membership is structural.
    if rules.get('references', {}).get('style_id') or rules.get('references', {}).get('csl_xml'):
        if citation_updates is None:
            problems.append(issue('CITATION_DATA_REQUIRED', '需要本地 CSL 引擎处理引用；无结构化字段的引用需先校对对应关系'))
        else:
            from .citations import apply_citation_updates
            problems.extend(apply_citation_updates(parts, root, citation_updates, rules['references']))
    bibliography = bibliography_paragraphs(root)
    styles = xml(parts["word/styles.xml"])
    heading_styles = {}
    for style in styles.findall(q("style")):
        name = style.find(q("name"))
        label = name.get(q("val"), "") if name is not None else ""
        if match := re.fullmatch(r"(?:heading|标题)\s*([1-6])", label, re.I):
            heading_styles[style.get(q("styleId"))] = match[1]
            rule = rules.get("headings", {}).get(match[1], {})
        elif style.get(q("styleId"), "").lower() == "normal":
            rule = rules.get("body", {})
        else:
            continue
        style_properties(child(style, "pPr"), child(style, "rPr"), rule)
    caption_paragraphs = []
    references_section = False
    reference_level = 1
    for paragraph in root.findall(".//w:p", NS):
        pstyle = paragraph.find("w:pPr/w:pStyle", NS)
        sid = pstyle.get(q("val"), "") if pstyle is not None else ""
        text = "".join(paragraph.xpath(".//w:t/text()", namespaces=NS))
        if text.strip().lower() in ("references", "bibliography", "参考文献"):
            references_section = True
            reference_level = int(heading_styles.get(sid, 1))
        elif sid in heading_styles and int(heading_styles[sid]) <= reference_level:
            references_section = False
        if sid in heading_styles:
            rule = rules.get("headings", {}).get(heading_styles[sid], {})
        elif match := re.match(r"^\s*(Figure|Fig\.?|Table|图|表)\s*\d+", text, re.I):
            kind = "table" if match[1].lower() in ("table", "表") else "figure"
            rule = rules.get("captions", {}).get(kind, {})
            caption_paragraphs.append((paragraph, kind, rule))
        elif references_section or paragraph in bibliography:
            rule = reference_rule(rules)
        elif sid.lower() in ("title", "subtitle"):
            rule = {}
        else:
            rule = rules.get("body", {})
        if rule:
            apply_to_paragraph(paragraph, rule)
    sections = root.findall(".//w:sectPr", NS)
    for section in sections:
        page_geometry(section, rules.get("page", {}))
    widths = section_widths(root)
    apply_tables(root, rules.get("tables", {}), widths)
    apply_images(root, rules.get("figures", {}).get("max_width_mm", 550), widths)
    for paragraph, kind, rule in caption_paragraphs:
        if rule.get("position"):
            parent = paragraph.getparent()
            siblings = list(parent)
            index = siblings.index(paragraph)
            candidates = [siblings[i] for i in (index - 1, index + 1) if 0 <= i < len(siblings)]
            objects = [n for n in candidates if (kind == "table" and n.tag == q("tbl")) or (kind == "figure" and n.findall(".//w:drawing", NS))]
            if len(objects) != 1:
                problems.append(issue("CAPTION_OBJECT_AMBIGUOUS", "无法唯一确定图表标题对应的对象", kind=kind))
            else:
                parent.remove(paragraph)
                where = list(parent).index(objects[0]) + (1 if rule["position"] == "below" else 0)
                parent.insert(where, paragraph)
    for kind in ("header", "footer"):
        if kind in rules:
            header_footer(parts, sections, kind, rules[kind])
    if any(r.get("numbered") is not None for r in rules.get("headings", {}).values()):
        from .numbering import apply_heading_numbering
        apply_heading_numbering(parts, root, heading_styles, rules["headings"])
    settings = xml(parts["word/settings.xml"]) if "word/settings.xml" in parts else etree.Element(q("settings"), nsmap={"w": W})
    set_attrs(child(settings, "updateFields"), val="true")
    parts["word/settings.xml"] = serialize(settings)
    parts["word/document.xml"], parts["word/styles.xml"] = serialize(root), serialize(styles)
    write_package(target, parts)
    after = inspect_docx(target)
    for name, code in (("content_hash", "CONTENT_CHANGED"), ("assets", "ASSET_CHANGED"), ("math", "FORMULA_CHANGED"), ("tables", "TABLE_COUNT_CHANGED")):
        if expected[name] != after[name]:
            problems.append(issue(code, "格式转换改变了应保留的内容，请校对"))
    return {"passed": not any(x["blocking"] for x in problems), "issues": problems, "before": before, "after": after,'mechanical_caption_changes':caption_changes}

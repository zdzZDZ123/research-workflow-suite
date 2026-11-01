"""Apply local CSL results while retaining Zotero field identities and itemData."""
import copy
import json
import re
import uuid

from lxml import etree, html

from .contracts import issue
from .docx_io import q, xml, serialize, fields, citation_data, child, set_attrs, bibliography_paragraphs, NS, PKG, CT, R


def plain_html(value):
    return html.fragment_fromstring(value or "", create_parent="div").text_content()


def html_runs(value):
    root = html.fragment_fromstring(value or "", create_parent="div")
    result = []
    def add(text, traits):
        if not text:
            return
        run = etree.Element(q("r"))
        if traits:
            props = child(run, "rPr")
            for trait in traits:
                if trait in ("b", "i", "smallCaps"):
                    child(props, trait)
                elif trait in ("superscript", "subscript"):
                    set_attrs(child(props, "vertAlign"), val=trait)
        for index, line in enumerate(text.split("\n")):
            if index:
                child(run, "br")
            node = etree.SubElement(run, q("t"))
            node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            node.text = line
        result.append(run)
    def visit(node, traits):
        extra = {"b": "b", "strong": "b", "i": "i", "em": "i", "sup": "superscript", "sub": "subscript"}.get(node.tag)
        local = traits + ([extra] if extra else [])
        if "small-caps" in node.get("style", ""):
            local = local + ["smallCaps"]
        if node.tag in ("script", "style", "iframe", "img"):
            return
        classes = node.get('class', '').split()
        if node.tag == 'br' or ('csl-block' in classes and result):
            run = etree.Element(q('r')); child(run, 'br'); result.append(run)
        add(node.text if node.tag != 'div' or (node.text or '').strip() else '', local)
        for sub in node:
            visit(sub, local)
            add(sub.tail if node.tag != 'div' or (sub.tail or '').strip() else '', local)
        if 'csl-left-margin' in classes:
            run = etree.Element(q('r')); child(run, 'tab'); result.append(run)
    visit(root, [])
    return result


def _paragraph(node):
    return next((x for x in node.iterancestors() if x.tag == q("p")), None)


def replace_field_result(field, value):
    separator = field.get("separate")
    if separator is None or _paragraph(separator) is not _paragraph(field["end"]):
        return False
    paragraph = _paragraph(separator)
    sep_run, end_run = separator.getparent(), field["end"].getparent()
    if sep_run.getparent() is not paragraph or end_run.getparent() is not paragraph or sep_run is end_run:
        return False
    start, end = list(paragraph).index(sep_run), list(paragraph).index(end_run)
    if end <= start:
        return False
    for node in list(paragraph)[start + 1:end]:
        paragraph.remove(node)
    for offset, run in enumerate(html_runs(value), start + 1):
        paragraph.insert(offset, run)
    return True


def apply_bibliography_layout(root, updates, rule):
    parameters = updates.get('bibliography_parameters', {})
    size = rule.get('size_pt', 12)
    for paragraph in bibliography_paragraphs(root):
        props = child(paragraph, 'pPr', first=True)
        if 'linespacing' in parameters:
            set_attrs(child(props, 'spacing'), line=round(float(parameters['linespacing']) * 240), lineRule='auto')
        if 'entryspacing' in parameters:
            set_attrs(child(props, 'spacing'), after=round(float(parameters['entryspacing']) * size * 20))
        if parameters.get('hangingindent') or parameters.get('second-field-align'):
            width = max(360, round((int(parameters.get('maxoffset', 3)) + 1) * size * .55 * 20)) if parameters.get('second-field-align') else 720
            indent = child(props, 'ind')
            for attr in ('firstLine', 'firstLineChars', 'hangingChars'):
                indent.attrib.pop(q(attr), None)
            set_attrs(indent, left=width, hanging=width)
            if parameters.get('second-field-align'):
                tabs = child(props, 'tabs')
                if not any(n.get(q('pos')) == str(width) for n in tabs):
                    etree.SubElement(tabs, q('tab'), {q('val'): 'left', q('pos'): str(width)})


def set_document_style(parts, updates):
    custom_ns = "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
    vt = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"
    props = xml(parts["docProps/custom.xml"]) if "docProps/custom.xml" in parts else etree.Element("{" + custom_ns + "}Properties", nsmap={None: custom_ns, "vt": vt})
    chunks = sorted([x for x in props if re.fullmatch(r"ZOTERO_PREF_\d+", x.get("name", ""))], key=lambda x: int(x.get("name").rsplit("_", 1)[1]))
    old = "".join("".join(x.itertext()) for x in chunks)
    style_id = updates.get("style_id", "")
    if style_id and not style_id.startswith(("http://", "https://")):
        style_id = "http://www.zotero.org/styles/" + style_id
    style = {"styleID": style_id, "locale": updates.get("locale", "en-US"), "hasBibliography": bool(updates.get("bibliography")), "bibliographyStyleHasBeenSet": True}
    if old.lstrip().startswith("<"):
        data = xml(old.encode())
        node = data.find("style")
        if node is None:
            node = etree.SubElement(data, "style")
        node.set("id", style_id)
        node.set("locale", style["locale"])
        node.set("hasBibliography", "1" if style["hasBibliography"] else "0")
        node.set("bibliographyStyleHasBeenSet", "1")
        rendered = etree.tostring(data, encoding="unicode")
    else:
        data = json.loads(old) if old else {"dataVersion": 4, "zoteroVersion": "10.0", "sessionID": uuid.uuid4().hex, "prefs": {"fieldType": "Field", "noteType": 0}}
        data["style"] = style
        rendered = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    for node in chunks:
        props.remove(node)
    next_id = max([int(x.get("pid", "1")) for x in props] + [1]) + 1
    for index, offset in enumerate(range(0, len(rendered), 240)):
        prop = etree.SubElement(props, "{" + custom_ns + "}property", fmtid="{D5CDD505-2E9C-101B-9397-08002B2CF9AE}", pid=str(next_id + index), name=f"ZOTERO_PREF_{index + 1}")
        etree.SubElement(prop, "{" + vt + "}lpwstr").text = rendered[offset:offset + 240]
    parts["docProps/custom.xml"] = serialize(props)
    rels = xml(parts["_rels/.rels"])
    if not any(x.get("Type", "").endswith("/custom-properties") for x in rels):
        etree.SubElement(rels, "{" + PKG + "}Relationship", Id="rIdPaperFormatCustom", Type=R + "/custom-properties", Target="docProps/custom.xml")
    parts["_rels/.rels"] = serialize(rels)
    types = xml(parts["[Content_Types].xml"])
    if not any(x.get("PartName") == "/docProps/custom.xml" for x in types):
        etree.SubElement(types, "{" + CT + "}Override", PartName="/docProps/custom.xml", ContentType="application/vnd.openxmlformats-officedocument.custom-properties+xml")
    parts["[Content_Types].xml"] = serialize(types)


def replace_bibliography(root, field, entries):
    if field:
        start, end = _paragraph(field.get("separate")), _paragraph(field["end"])
        if start is None or end is None or start.getparent() is not end.getparent():
            return False
        parent = start.getparent()
        start_run, end_run = field["separate"].getparent(), field["end"].getparent()
        if start_run.getparent() is not start or end_run.getparent() is not end or start_run is end_run:
            return False
        tail = list(end)[list(end).index(end_run):]
        for node in tail:
            end.remove(node)
        position = list(start).index(start_run)
        for node in list(start)[position + 1:]:
            start.remove(node)
        if start is not end:
            a, b = list(parent).index(start), list(parent).index(end)
            for node in list(parent)[a + 1:b + 1]:
                parent.remove(node)
        first = start
    else:
        parent = root.find(q("body"))
        first = etree.Element(q("p"))
        index = len(parent) - 1 if len(parent) and parent[-1].tag == q("sectPr") else len(parent)
        parent.insert(index, first)
        for tag, attr, value in (("fldChar", "fldCharType", "begin"), ("instrText", None, " ADDIN ZOTERO_BIBL {} "), ("fldChar", "fldCharType", "separate")):
            node = etree.SubElement(etree.SubElement(first, q("r")), q(tag))
            if attr: set_attrs(node, **{attr: value})
            else: node.text = value
        end_run = etree.Element(q("r"))
        set_attrs(child(end_run, "fldChar"), fldCharType="end")
        tail = [end_run]
    last = first
    for index, entry in enumerate(entries):
        if index:
            paragraph = etree.Element(q("p"))
            props = first.find(q("pPr"))
            if props is not None:
                paragraph.append(copy.deepcopy(props))
            parent.insert(list(parent).index(last) + 1, paragraph)
            last = paragraph
        last.extend(html_runs(entry))
    last.extend(tail)
    return True


def apply_citation_updates(parts, root, updates, rule):
    problems = []
    all_fields = fields(root)
    cited = [(field, citation_data(field)) for field in all_fields if "ZOTERO_ITEM CSL_CITATION" in field["instruction"]]
    by_id = updates.get("citations", {})
    if {data["citationID"] for _, data in cited} != set(by_id):
        return [issue("CITATION_MAPPING_INCOMPLETE", "引用字段与 CSL 渲染结果不完全对应")]
    if not cited:
        return [issue("NO_STRUCTURED_CITATIONS", "请在本地校对中提供文内引用与参考文献的对应数据")]
    for field, data in cited:
        rendered = by_id[data["citationID"]]
        if not replace_field_result(field, rendered["html"]):
            problems.append(issue("CITATION_FIELD_STRUCTURE", "该引用域的结构需要在文字处理软件中修复", citation=data["citationID"]))
            continue
        data.setdefault("properties", {}).update(formattedCitation=rendered["html"], plainCitation=rendered.get("text") or plain_html(rendered["html"]))
        for index, node in enumerate(field["instructions"]):
            node.text = " ADDIN ZOTERO_ITEM CSL_CITATION " + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + " " if index == 0 else ""
    bibliography = [field for field in all_fields if "ZOTERO_BIBL" in field["instruction"]]
    if len(bibliography) > 1:
        problems.append(issue("MULTIPLE_BIBLIOGRAPHIES", "存在多个参考文献域，需要分别校对"))
    elif updates.get("bibliography"):
        if not replace_bibliography(root, bibliography[0] if bibliography else None, updates["bibliography"]):
            problems.append(issue("BIBLIOGRAPHY_FIELD_STRUCTURE", "参考文献域跨越了不支持的结构"))
    if not problems:
        apply_bibliography_layout(root, updates, rule)
        try:
            set_document_style(parts, updates)
        except (ValueError, etree.XMLSyntaxError):
            problems.append(issue("ZOTERO_DOCUMENT_METADATA", "无法保留 Zotero 文档样式元数据"))
    return problems

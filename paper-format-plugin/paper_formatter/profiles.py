"""Validated layout data and local-only template distillation."""
from __future__ import annotations

import copy
import re
import zipfile
from pathlib import Path

from lxml import etree

from .contracts import FormatterError, canonical, fingerprint, utc_now

STYLE = {"font": str, "latin_font": str, "size_pt": (5, 96), "line_spacing": (.5, 5),
         "line_spacing_pt": (5, 150), "space_before_pt": (0, 150), "space_after_pt": (0, 150),
         "first_line_mm": (-40, 80), "align": {"left", "center", "right", "justify"}, "bold": bool,
         "italic": bool, "keep_with_next": bool}
RULE_SCHEMA = {
    "page": {"width_mm": (50, 600), "height_mm": (50, 600), "margins_mm": {k: (0, 100) for k in ("top", "bottom", "left", "right")},
             "columns": (1, 3), "column_gap_mm": (0, 40), "line_numbers": bool},
    "body": STYLE, "headings": {str(n): {**STYLE, "numbered": bool} for n in range(1, 7)},
    "header": {"text": str, "page_number": bool, "align": {"left", "center", "right"}, "size_pt": (5, 36)},
    "footer": {"text": str, "page_number": bool, "align": {"left", "center", "right"}, "size_pt": (5, 36)},
    "captions": {"figure": {**STYLE, "position": {"above", "below"}, "label": str, "numbered": bool},
                 "table": {**STYLE, "position": {"above", "below"}, "label": str, "numbered": bool}},
    "tables": {"borders": {"three_line", "grid", "none"}, "fit_width": bool, "repeat_header": bool},
    "figures": {"max_width_mm": (10, 550)},
    "references": {**STYLE, "style_id": str, "locale": str, "csl_xml": str},
    "limits": {"max_words": (1, 1000000), "required_sections": list},
}


def _validate(value, schema, path="rules"):
    if isinstance(schema, dict):
        if not isinstance(value, dict):
            raise FormatterError("PROFILE_OBJECT_REQUIRED", path)
        for key, item in value.items():
            if key not in schema:
                raise FormatterError("UNSUPPORTED_RULE", path + "." + key)
            _validate(item, schema[key], path + "." + key)
    elif isinstance(schema, tuple):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not schema[0] <= value <= schema[1]:
            raise FormatterError("PROFILE_NUMBER_RANGE", path)
        if path.endswith(("columns", "max_words")) and not isinstance(value, int):
            raise FormatterError("PROFILE_INTEGER_REQUIRED", path)
    elif isinstance(schema, set):
        if not isinstance(value, str) or value not in schema:
            raise FormatterError("PROFILE_ENUM", path)
    elif schema is str:
        limit = 200000 if path.endswith("csl_xml") else 2000 if path.endswith("text") else 512
        if not isinstance(value, str) or len(value) > limit or "\x00" in value:
            raise FormatterError("PROFILE_STRING", path)
    elif schema is bool and not isinstance(value, bool):
        raise FormatterError("PROFILE_BOOLEAN", path)
    elif schema is list and (not isinstance(value, list) or len(value) > 100 or any(not isinstance(x, str) or len(x) > 200 for x in value)):
        raise FormatterError("PROFILE_LIST", path)


def validate_profile(profile: dict) -> dict:
    if profile.get("schema_version") != 1 or set(profile) - {"schema_version", "rules", "sources", "unresolved", "overrides", "confirmed_at"}:
        raise FormatterError("PROFILE_SCHEMA")
    _validate(profile.get("rules"), RULE_SCHEMA)
    def validate_spacing(value, path="rules"):
        if not isinstance(value, dict):
            return
        if "line_spacing" in value and "line_spacing_pt" in value:
            raise FormatterError("LINE_SPACING_CONFLICT", path + " 同时指定了倍数行距和固定行距")
        for key, child in value.items():
            validate_spacing(child, path + "." + key)
    validate_spacing(profile["rules"])
    if not isinstance(profile.get("sources", []), list) or not isinstance(profile.get("unresolved", []), list):
        raise FormatterError("PROFILE_METADATA")
    if any(not isinstance(x, str) for x in profile.get("unresolved", [])):
        raise FormatterError("PROFILE_UNRESOLVED")
    page = profile["rules"].get("page", {})
    margins = page.get("margins_mm", {})
    if margins.get("left", 0) + margins.get("right", 0) >= page.get("width_mm", 210) - 20:
        raise FormatterError("PAGE_NO_USABLE_WIDTH")
    if margins.get("top", 0) + margins.get("bottom", 0) >= page.get("height_mm", 297) - 20:
        raise FormatterError("PAGE_NO_USABLE_HEIGHT")
    columns = page.get("columns", 1)
    # Missing geometry stays inherited from the document. Reject even the most
    # permissive width when explicit column gaps already consume all space.
    available = page.get("width_mm", 210) - margins.get("left", 0) - margins.get("right", 0)
    if available - (columns - 1) * page.get("column_gap_mm", 0) <= 0:
        raise FormatterError("PAGE_NO_USABLE_COLUMN_WIDTH")
    csl = profile["rules"].get("references", {}).get("csl_xml")
    if csl:
        if "<!DOCTYPE" in csl.upper() or "<!ENTITY" in csl.upper():
            raise FormatterError("UNSAFE_CSL")
        try:
            root = etree.fromstring(csl.encode(), etree.XMLParser(resolve_entities=False, no_network=True))
        except etree.XMLSyntaxError as exc:
            raise FormatterError("INVALID_CSL") from exc
        if root.tag != "{http://purl.org/net/xbiblio/csl}style":
            raise FormatterError("INVALID_CSL_ROOT")
    return copy.deepcopy(profile)


def blank_profile(rules=None) -> dict:
    return {"schema_version": 1, "rules": rules or {}, "sources": [], "unresolved": []}


def merge_profiles(journal=None, template=None, explicit=None) -> dict:
    output = blank_profile()
    output["overrides"] = []
    def merge(dst, src, path, source_name):
        for key, value in src.items():
            full = path + "." + key if path else key
            if isinstance(value, dict):
                merge(dst.setdefault(key, {}), value, full, source_name)
            else:
                alternate = {"line_spacing": "line_spacing_pt", "line_spacing_pt": "line_spacing"}.get(key)
                if alternate and alternate in dst:
                    output["overrides"].append({"path": (path + "." if path else "") + alternate,
                                               "previous": dst.pop(alternate), "value": None,
                                               "authority": source_name, "replaced_by": full})
                if key in dst and dst[key] != value:
                    output["overrides"].append({"path": full, "previous": dst[key], "value": value, "authority": source_name})
                dst[key] = copy.deepcopy(value)
    for name, profile in (("journal", journal), ("template", template), ("explicit", explicit)):
        if profile is not None:
            validate_profile(profile)
            merge(output["rules"], profile["rules"], "", name)
            output["sources"].extend(profile.get("sources", []))
            output["unresolved"].extend(profile.get("unresolved", []))
    return validate_profile(output)


def parse_simple_instructions(text: str) -> dict:
    result = blank_profile()
    rules = result["rules"]
    def style_target(role):
        return rules.setdefault("headings", {}).setdefault({"一级标题": "1", "二级标题": "2", "三级标题": "3"}[role], {}) if role in ("一级标题", "二级标题", "三级标题") else rules.setdefault("references" if role == "参考文献" else "body", {})
    def set_style(clause):
        match = re.fullmatch(r"(?:(正文|一级标题|二级标题|三级标题|参考文献)\s*[:：]?\s*)?([^\d,，]+?)\s+(\d+(?:\.\d+)?)\s*(?:pt|磅)", clause, re.I)
        if not match:
            return False
        role, font, size = match.groups()
        style_target(role).update(font=font.strip(), size_pt=float(size))
        return True
    normalized = text.strip()
    for label, size in (("小四", 12), ("四号", 14), ("五号", 10.5), ("小五", 9), ("三号", 16), ("小三", 15), ("二号", 22)):
        normalized = normalized.replace(label, f" {size}pt")
    for raw in re.split(r"[;；。\n]+", normalized):
        clause = raw.strip(" ，,。")
        if not clause:
            continue
        if clause.upper() in ("A4", "A4纸", "A4纸张"):
            rules.setdefault("page", {}).update(width_mm=210, height_mm=297)
        elif clause in ("单栏", "双栏", "两栏", "三栏"):
            rules.setdefault("page", {})["columns"] = {"单栏": 1, "双栏": 2, "两栏": 2, "三栏": 3}[clause]
        elif match := re.fullmatch(r"(?:所有|四周|上下左右)?页边距\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(cm|mm|厘米|毫米)", clause, re.I):
            value = float(match[1]) * (10 if match[2].lower() in ("cm", "厘米") else 1)
            rules.setdefault("page", {})["margins_mm"] = {k: value for k in ("top", "bottom", "left", "right")}
        elif match := re.fullmatch(r"(?:正文\s*)?(\d+(?:\.\d+)?)\s*倍行距", clause):
            rules.setdefault("body", {})["line_spacing"] = float(match[1])
        elif match := re.fullmatch(r"(?:(正文|一级标题|二级标题|三级标题|参考文献)\s*[:：]?\s*)?(\d+(?:\.\d+)?)\s*(?:pt|磅)", clause, re.I):
            # A role followed only by a size is not a font name (e.g. 正文 12pt).
            style_target(match[1])["size_pt"] = float(match[2])
        elif set_style(clause):
            pass
        elif clause in ("页码居中", "底部页码居中"):
            rules["footer"] = {"page_number": True, "align": "center"}
        elif clause in ("三线表", "表格使用三线表"):
            rules.setdefault("tables", {})["borders"] = "three_line"
        elif clause in ("行号", "添加行号"):
            rules.setdefault("page", {})["line_numbers"] = True
        elif re.fullmatch(r"(?:参考文献\s*)?(APA|IEEE|Vancouver|GB/T\s*7714(?:-2015)?)", clause, re.I):
            style = re.sub(r"参考文献\s*", "", clause).lower()
            rules.setdefault("references", {})["style_id"] = "china-national-standard-gb-t-7714-2015-numeric" if "7714" in style else style
        elif match := re.fullmatch(r"(页眉|页脚)\s*[:：]\s*(.+)", clause):
            rules.setdefault("header" if match[1] == "页眉" else "footer", {})["text"] = match[2]
        else:
            result["unresolved"].append(clause)
    return validate_profile(result)


NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


def _xml(archive, name):
    info = archive.getinfo(name)
    if info.file_size > 30_000_000:
        raise FormatterError("TEMPLATE_TOO_LARGE")
    data = archive.read(name)
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise FormatterError("UNSAFE_TEMPLATE_XML")
    return etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True))


def distill_template(path: str | Path) -> dict:
    path = Path(path).resolve()
    if path.suffix.lower() == ".pdf":
        from .pdf_io import inspect_pdf_template
        return inspect_pdf_template(path)
    if path.suffix.lower() not in (".docx", ".dotx"):
        raise FormatterError("UNSUPPORTED_TEMPLATE")
    result = blank_profile()
    with zipfile.ZipFile(path) as archive:
        doc = _xml(archive, "word/document.xml")
        sections = doc.findall(".//w:sectPr", NS)
        if sections:
            section = sections[-1]
            page = result["rules"].setdefault("page", {})
            size = section.find("w:pgSz", NS)
            if size is not None:
                for src, dst in (("w", "width_mm"), ("h", "height_mm")):
                    if size.get("{" + NS["w"] + "}" + src):
                        page[dst] = round(int(size.get("{" + NS["w"] + "}" + src)) / 1440 * 25.4, 3)
            margins = section.find("w:pgMar", NS)
            if margins is not None:
                page["margins_mm"] = {k: round(int(margins.get("{" + NS["w"] + "}" + k)) / 1440 * 25.4, 3) for k in ("top", "bottom", "left", "right") if margins.get("{" + NS["w"] + "}" + k)}
            columns = section.find("w:cols", NS)
            if columns is not None:
                page["columns"] = int(columns.get("{" + NS["w"] + "}num", "1"))
            if len(sections) > 1:
                result["unresolved"].append("模板含多个节；请核对所提取的末节页面设置")
        styles = _xml(archive, "word/styles.xml")
        for style in styles.findall("w:style", NS):
            sid = style.get("{" + NS["w"] + "}styleId", "")
            name = style.find("w:name", NS)
            label = name.get("{" + NS["w"] + "}val", "") if name is not None else sid
            value = {}
            font = style.find("w:rPr/w:rFonts", NS)
            size = style.find("w:rPr/w:sz", NS)
            if font is not None:
                for src, dst in (("eastAsia", "font"), ("ascii", "latin_font")):
                    if font.get("{" + NS["w"] + "}" + src):
                        value[dst] = font.get("{" + NS["w"] + "}" + src)
            if size is not None:
                value["size_pt"] = int(size.get("{" + NS["w"] + "}val")) / 2
            spacing = style.find("w:pPr/w:spacing", NS)
            if spacing is not None:
                for src, dst in (("before", "space_before_pt"), ("after", "space_after_pt")):
                    if spacing.get("{" + NS["w"] + "}" + src):
                        value[dst] = int(spacing.get("{" + NS["w"] + "}" + src)) / 20
                line = spacing.get("{" + NS["w"] + "}line")
                if line:
                    mode = spacing.get("{" + NS["w"] + "}lineRule", "auto")
                    if mode in ("auto", "exact"):
                        value["line_spacing" if mode == "auto" else "line_spacing_pt"] = int(line) / (240 if mode == "auto" else 20)
                    else:
                        result["unresolved"].append(f"模板样式 {label} 使用 {mode} 行距；请明确选择倍数行距或固定行距")
            if sid.lower() == "normal" or label.lower() in ("normal", "正文"):
                result["rules"]["body"] = value
            elif match := re.fullmatch(r"(?:heading|标题)\s*([1-6])", label, re.I):
                result["rules"].setdefault("headings", {})[match[1]] = value
    result["sources"] = [{"kind": "template", "path": str(path), "sha256": fingerprint(path), "retrieved_at": utc_now(), "prose_imported": False}]
    return validate_profile(result)

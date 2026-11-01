import json
import zipfile
import pytest
from pathlib import Path

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from lxml import etree
from PIL import Image

from paper_formatter.docx_io import inspect_docx, format_docx, read_package
from paper_formatter.profiles import blank_profile
from paper_formatter.render import render_docx, inspect_render


def rich_docx(path):
    doc = Document()
    doc.add_heading("Test manuscript 测试论文", 0)
    doc.add_heading("Methods", 1)
    doc.add_paragraph("A persistent original paragraph. 数据 123.45 remain unchanged.")
    table = doc.add_table(rows=3, cols=3)
    table.cell(0, 0).merge(table.cell(0, 1)).text = "Merged heading"
    table.cell(1, 0).text = "42.01"
    table.cell(2, 2).text = "unchanged cell"
    p = doc.add_paragraph()
    math = OxmlElement("m:oMath")
    run = OxmlElement("m:r")
    text = OxmlElement("m:t")
    text.text = "x+y=42"
    run.append(text)
    math.append(run)
    p._p.append(math)
    image = path.with_suffix(".png")
    Image.new("RGB", (300, 100), "navy").save(image)
    doc.add_picture(str(image))
    doc.add_paragraph("Figure 1. Original image caption", style="Caption")
    p = doc.add_paragraph("A cited finding ")
    citation = {"citationID": "stable-citation", "citationItems": [{"id": "fixture-ref", "itemData": {"id": "fixture-ref", "type": "article-journal", "title": "Fixture reference", "author": [{"family": "Example", "given": "A"}], "issued": {"date-parts": [[2024]]}}}], "properties": {"noteIndex": 0}}
    for tag, attr, value in [("w:fldChar", "w:fldCharType", "begin"), ("w:instrText", None, " ADDIN ZOTERO_ITEM CSL_CITATION " + json.dumps(citation)), ("w:fldChar", "w:fldCharType", "separate"), ("w:t", None, "(Example, 2024)"), ("w:fldChar", "w:fldCharType", "end")]:
        node = OxmlElement(tag)
        if attr: node.set(qn(attr), value)
        else: node.text = value
        run = OxmlElement("w:r")
        run.append(node)
        p._p.append(run)
    doc.save(path)
    return path


def test_docx_changes_geometry_but_keeps_science_assets_and_fields(tmp_path):
    source = rich_docx(tmp_path / "source.docx")
    target = tmp_path / "target.docx"
    before = inspect_docx(source)
    report = format_docx(source, target, blank_profile({"body": {"size_pt": 12, "line_spacing": 1.5}, "page": {"columns": 2, "margins_mm": {"left": 25, "right": 25, "top": 20, "bottom": 20}}, "tables": {"borders": "three_line", "fit_width": True}, "footer": {"page_number": True, "align": "center"}}))
    after = inspect_docx(target)
    assert before["content_hash"] == after["content_hash"]
    assert before["assets"] == after["assets"]
    assert before["math"] == after["math"]
    assert before["citations"] == after["citations"]
    doc = Document(target)
    assert doc.sections[0]._sectPr.find(qn("w:cols")).get(qn("w:num")) == "2"
    assert doc.sections[0].left_margin.mm == pytest.approx(25.0, abs=.01)  # OOXML stores integer twips
    assert len(doc.tables[0].rows[0].cells) == 3
    assert not any(x.get("blocking", True) for x in report["issues"])


def test_external_embedded_resource_prevents_conversion(tmp_path):
    path = rich_docx(tmp_path / "external.docx")
    data = read_package(path)
    root = etree.fromstring(data["word/_rels/document.xml.rels"])
    etree.SubElement(root, "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship", Id="rIdExternal", Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image", TargetMode="External", Target="https://example.org/TRACKING_SENTINEL.png")
    data["word/_rels/document.xml.rels"] = etree.tostring(root)
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in data.items(): archive.writestr(name, content)
    report = format_docx(path, tmp_path / "blocked.docx", blank_profile({"body": {"size_pt": 12}}))
    assert any(x["code"] == "EXTERNAL_RESOURCE" and x["blocking"] for x in report["issues"])
    assert not (tmp_path / "blocked.docx").exists()


def test_real_libreoffice_render_produces_inspectable_pages(tmp_path):
    doc = Document()
    doc.add_heading("Fixture manuscript", 0)
    doc.add_paragraph("The measured value was 42.01. This sentence must survive rendering.")
    source = tmp_path / "render.docx"
    doc.save(source)
    rendered = render_docx(source, tmp_path / "rendered")
    result = inspect_render(rendered["pdf"], tmp_path / "pages")
    assert len(result["pages"]) == 1
    assert "42.01" in result["text"]
    assert Path(result["pages"][0]["image"]).is_file()
    assert not any(x.get("blocking", True) for x in result["issues"])

import json

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from paper_formatter.docx_io import format_docx, inspect_docx
from paper_formatter.profiles import blank_profile
from test_documents import rich_docx


def test_citation_rendering_preserves_item_identity_and_native_field(tmp_path):
    source = rich_docx(tmp_path / "source.docx")
    updates = {"citations": {"stable-citation": {"text": "[1]", "html": "[1]"}}, "bibliography": [], "style_id": "http://www.zotero.org/styles/ieee", "locale": "en-US"}
    target = tmp_path / "target.docx"
    report = format_docx(source, target, blank_profile({"references": {"style_id": "ieee"}}), citation_updates=updates)
    assert report["passed"]
    before, after = inspect_docx(source), inspect_docx(target)
    assert after["citations"][0]["citationID"] == "stable-citation"
    assert after["citations"][0]["citationItems"] == before["citations"][0]["citationItems"]
    assert after["citations"][0]["properties"]["plainCitation"] == "[1]"
    assert "[1]" in "\n".join(p.text for p in Document(target).paragraphs)
    assert any("ZOTERO_ITEM CSL_CITATION" in f for f in after["fields"])


def test_missing_citation_mapping_never_reports_success(tmp_path):
    source = rich_docx(tmp_path / "source.docx")
    report = format_docx(source, tmp_path / "target.docx", blank_profile({"references": {"style_id": "ieee"}}), citation_updates={"citations": {}, "bibliography": [], "style_id": "ieee"})
    assert not report["passed"]
    assert any(x["code"] == "CITATION_MAPPING_INCOMPLETE" for x in report["issues"])


def test_heading_numbering_is_native_and_keeps_heading_text(tmp_path):
    source = rich_docx(tmp_path / "source.docx")
    target = tmp_path / "numbered.docx"
    report = format_docx(source, target, blank_profile({"headings": {"1": {"numbered": True}}}))
    assert report["passed"]
    heading = next(p for p in Document(target).paragraphs if p.text == "Methods")
    assert heading._p.find(qn("w:pPr")).find(qn("w:numPr")) is not None


def test_generated_citation_and_bibliography_receive_requested_typography(tmp_path):
    from paper_formatter.checks import applied_rule_issues
    source = rich_docx(tmp_path / 'source.docx')
    target = tmp_path / 'target.docx'
    profile = blank_profile({'body': {'size_pt': 12, 'latin_font': 'Times New Roman'},
                             'references': {'style_id': 'ieee', 'size_pt': 10}})
    updates = {'citations': {'stable-citation': {'text': '[1]', 'html': '[1]'}},
               'bibliography': ['A. Example, <i>Fixture reference</i>, 2024.'], 'style_id': 'ieee', 'locale': 'en-US'}
    report = format_docx(source, target, profile, citation_updates=updates)
    assert report['passed'], report['issues']
    assert not applied_rule_issues(target, profile)
    d = Document(target)
    citation = next(p for p in d.paragraphs if p.text.startswith('A cited finding'))
    assert next(r for r in citation.runs if r.text == '[1]').font.size.pt == 12
    reference = next(p for p in d.paragraphs if 'A. Example' in p.text)
    assert all(r.font.size.pt == 10 for r in reference.runs if r.text)
    assert any(r.italic for r in reference.runs)


def test_csl_bibliography_alignment_and_spacing_survive_html_conversion(tmp_path):
    source = rich_docx(tmp_path / 'source.docx')
    target = tmp_path / 'target.docx'
    profile = blank_profile({'body': {'size_pt': 12, 'line_spacing': 1.5}, 'references': {'style_id': 'ieee'}})
    updates = {'citations': {'stable-citation': {'text': '[1]', 'html': '[1]'}},
        'bibliography': ['  <div class="csl-entry">\n <div class="csl-left-margin">[1]</div><div class="csl-right-inline">A. Example, <i>Fixture</i>, 2024.</div>\n </div>\n'],
        'bibliography_parameters': {'maxoffset': 3, 'entryspacing': 0, 'linespacing': 1, 'second-field-align': 'flush'},
        'style_id': 'ieee', 'locale': 'en-US'}
    assert format_docx(source, target, profile, citation_updates=updates)['passed']
    reference = next(p for p in Document(target).paragraphs if 'A. Example' in p.text)
    assert reference.text == '[1]\tA. Example, Fixture, 2024.'
    assert reference.paragraph_format.line_spacing == 1
    assert reference.paragraph_format.space_after.pt == 0
    assert reference.paragraph_format.first_line_indent < 0
    assert reference._p.xpath('.//w:tabs/w:tab')

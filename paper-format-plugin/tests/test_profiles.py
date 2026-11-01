import json

import pytest
from docx import Document
from docx.shared import Mm, Pt

from paper_formatter.contracts import FormatterError
from paper_formatter.profiles import validate_profile, merge_profiles, parse_simple_instructions, distill_template


def profile(rules):
    return {"schema_version": 1, "rules": rules, "sources": [], "unresolved": []}


def test_precedence_is_explicit_template_journal_and_conflicts_reported():
    merged = merge_profiles(profile({"body": {"size_pt": 10}, "page": {"columns": 2}}),
                            profile({"body": {"size_pt": 11}}), profile({"body": {"size_pt": 12}}))
    assert merged["rules"] == {"body": {"size_pt": 12}, "page": {"columns": 2}}
    assert any(x["path"] == "body.size_pt" for x in merged["overrides"])


@pytest.mark.parametrize("rules", [{"execute": "delete original"}, {"body": {"size_pt": -1}},
                                   {"page": {"columns": 99}}, {"header": {"text": "x" * 2001}}])
def test_unrecognized_and_unsafe_rules_are_rejected(rules):
    with pytest.raises(FormatterError):
        validate_profile(profile(rules))


def test_simple_chinese_request_parses_exact_geometry_and_font():
    parsed = parse_simple_instructions("A4；页边距 2.5cm；正文 宋体 12pt；1.5倍行距；双栏")
    assert not parsed["unresolved"]
    assert parsed["rules"]["page"]["margins_mm"] == {"top": 25, "bottom": 25, "left": 25, "right": 25}
    assert parsed["rules"]["body"]["font"] == "宋体"
    assert parsed["rules"]["body"]["size_pt"] == 12
    assert parsed["rules"]["body"]["line_spacing"] == 1.5
    assert parsed["rules"]["page"]["columns"] == 2


def test_unknown_instruction_is_not_silently_accepted():
    parsed = parse_simple_instructions("A4；把三线表改成特殊期刊定义的十七种嵌套格式")
    assert parsed["unresolved"]


def test_template_prose_and_header_do_not_enter_profile(tmp_path):
    path = tmp_path / "template.docx"
    doc = Document()
    doc.add_paragraph("PRIVATE_TEMPLATE_SENTINEL")
    doc.sections[0].header.paragraphs[0].text = "SECRET_AUTHOR_SENTINEL"
    doc.sections[0].left_margin = Mm(31)
    doc.styles["Normal"].font.size = Pt(11)
    doc.save(path)
    result = distill_template(path)
    serialized = json.dumps(result)
    assert "PRIVATE_TEMPLATE_SENTINEL" not in serialized
    assert "SECRET_AUTHOR_SENTINEL" not in serialized
    assert result["rules"]["page"]["margins_mm"]["left"] == pytest.approx(31, abs=.03)
    assert result["rules"]["body"]["size_pt"] == 11

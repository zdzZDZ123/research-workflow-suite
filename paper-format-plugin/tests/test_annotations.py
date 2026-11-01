from reportlab.pdfgen import canvas

from paper_formatter.annotations import migrate_annotations
from paper_formatter.render import pdf_pages


def make_pdf(path, lines):
    doc = canvas.Canvas(str(path), pagesize=(595, 842))
    for page_lines in lines:
        for row, line in enumerate(page_lines):
            doc.drawString(72, 750 - row * 22, line)
        doc.showPage()
    doc.save()
    return path


def highlight(key="NOTE1234", text="A unique scientific finding", page=0):
    return {"key": key, "type": "highlight", "text": text, "comment": "retain this note", "color": "#ffd400", "tags": [{"name": "important"}], "position": {"pageIndex": page, "rects": [[72, 744, 230, 763]]}}


def test_unique_quote_moves_to_new_page_preserving_annotation_identity(tmp_path):
    original = make_pdf(tmp_path / "original.pdf", [["A unique scientific finding"]])
    updated = make_pdf(tmp_path / "updated.pdf", [["An introductory page"], ["A unique scientific finding"]])
    result = migrate_annotations([highlight()], original, updated)
    assert result["complete"]
    mapped = result["annotations"][0]
    assert mapped["key"] == "NOTE1234"
    assert mapped["comment"] == "retain this note"
    assert mapped["position"]["pageIndex"] == 1
    assert mapped["tags"] == [{"name": "important"}]


def test_duplicate_quote_with_no_unique_context_requires_review(tmp_path):
    original = make_pdf(tmp_path / "original.pdf", [["A unique scientific finding"]])
    updated = make_pdf(tmp_path / "updated.pdf", [["A unique scientific finding"], ["A unique scientific finding"]])
    result = migrate_annotations([highlight()], original, updated)
    assert not result["complete"]
    assert result["unresolved"][0]["key"] == "NOTE1234"


def test_manual_highlight_must_cover_the_original_quote(tmp_path):
    original = make_pdf(tmp_path / "original.pdf", [["A unique scientific finding"]])
    updated = make_pdf(tmp_path / "updated.pdf", [["Unrelated text"], ["A unique scientific finding"]])
    result = migrate_annotations([highlight()], original, updated, manual={"NOTE1234": {"pageIndex": 0, "rects": [[72, 740, 300, 770]]}})
    assert not result["complete"]
    assert result["unresolved"][0]["code"] == "MANUAL_QUOTE_MISMATCH"


def test_region_annotation_never_guesses_a_destination(tmp_path):
    path = make_pdf(tmp_path / "same.pdf", [["Some text"]])
    value = {"key": "IMAGE001", "type": "image", "comment": "image note", "color": "#ffd400", "position": {"pageIndex": 0, "rects": [[70, 700, 300, 780]]}}
    result = migrate_annotations([value], path, path)
    assert not result["complete"]
    manual = {"IMAGE001": {"pageIndex": 0, "rects": [[70, 700, 300, 780]]}}
    result = migrate_annotations([value], path, path, manual=manual)
    assert result["complete"]

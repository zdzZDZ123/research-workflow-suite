from docx import Document
from paper_formatter.checks import applied_rule_issues
from paper_formatter.profiles import blank_profile
from paper_formatter.docx_io import format_docx
from paper_formatter.checks import render_content_issues


def test_manual_file_cannot_bypass_requested_layout(tmp_path):
    src=tmp_path/'source.docx';out=tmp_path/'out.docx'
    d=Document();d.add_paragraph('This text must remain unchanged.');d.save(src)
    profile=blank_profile({'body':{'size_pt':14},'page':{'columns':2,'margins_mm':{'left':25}}})
    assert applied_rule_issues(src,profile)
    format_docx(src,out,profile)
    assert not applied_rule_issues(out,profile)


def test_unknown_pdf_glyph_requires_independent_complete_text_read(monkeypatch,tmp_path):
    import pypdf
    class Page:
        def __init__(self,text):self.text=text
        def extract_text(self):return self.text
    class Reader:
        def __init__(self,text):self.pages=[Page(text)]
    pdf=tmp_path/'rendered.pdf';pdf.write_bytes(b'%PDF-1.4\n')
    expected={'text':'social-evaluative result 42.01'}
    primary={'pdf':str(pdf),'text':'social\ufffeevaluative result 42.01',
             'pages':[{'index':2,'text':'social\ufffeevaluative result 42.01'}]}
    monkeypatch.setattr(pypdf,'PdfReader',lambda _:Reader('social-\nevaluative result 42.01'))
    assert render_content_issues(expected,primary)==[]
    assert primary['text_crosscheck']['unknown_glyph_pages']==[2]
    monkeypatch.setattr(pypdf,'PdfReader',lambda _:Reader('socialevaluative result 42.01'))
    assert render_content_issues(expected,{**primary,'text_crosscheck':None})[0]['code']=='RENDER_TEXT_MISSING'
    assert render_content_issues({'text':'social-evaluative result 42.02'},primary)[0]['code']=='RENDER_TEXT_MISSING'

from docx import Document
from paper_formatter.docx_io import format_docx,inspect_docx
from paper_formatter.profiles import blank_profile


def test_caption_renumber_updates_exact_text_references_only(tmp_path):
    src=tmp_path/'source.docx';out=tmp_path/'output.docx'
    d=Document();d.add_paragraph('See Figure 7 for the measured value 7.42.')
    d.add_paragraph('Figure 7. Original scientific result',style='Caption');d.save(src)
    report=format_docx(src,out,blank_profile({'captions':{'figure':{'label':'Fig.','numbered':True}}}))
    assert report['passed'],report['issues']
    assert 'See Fig. 1 for the measured value 7.42.' in inspect_docx(out)['text']
    assert Document(out).paragraphs[1].text=='Fig. 1. Original scientific result'


def test_ambiguous_caption_number_blocks_renumber(tmp_path):
    src=tmp_path/'source.docx';out=tmp_path/'output.docx'
    d=Document();d.add_paragraph('Figure 1. First',style='Caption');d.add_paragraph('Figure 1. Second',style='Caption');d.save(src)
    report=format_docx(src,out,blank_profile({'captions':{'figure':{'numbered':True}}}))
    assert any(i['code']=='CAPTION_NUMBER_AMBIGUOUS' for i in report['issues'])

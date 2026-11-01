from pathlib import Path

from reportlab.pdfgen import canvas

from paper_formatter.pdf_io import inspect_pdf_template, extract_pdf, rebuild_docx
from paper_formatter.offline import offline_processing
from paper_formatter.docx_io import inspect_docx
from paper_formatter.contracts import fingerprint


def test_pdf_template_stays_local_and_reports_ambiguities(tmp_path):
    path = tmp_path / 'sample.pdf'
    c = canvas.Canvas(str(path), pagesize=(595.28, 841.89))
    c.drawString(70, 760, 'PRIVATE-TEMPLATE-CONTENT')
    c.save()
    profile = inspect_pdf_template(path)
    assert abs(profile['rules']['page']['width_mm'] - 210) < .1
    assert 'PRIVATE-TEMPLATE-CONTENT' not in str(profile)
    assert profile['unresolved']


def test_pdf_local_blocks_can_rebuild_without_losing_text(tmp_path):
    from paper_formatter.pdf_io import native_blocks
    path = tmp_path / 'paper.pdf'
    c = canvas.Canvas(str(path))
    c.drawString(72, 760, 'Evidence from sample 42.01 remains unchanged.')
    c.drawString(72, 735, 'The measured result is 17.5 percent.')
    c.save()
    blocks = native_blocks(path)
    assert len(blocks) == 2
    target = tmp_path / 'rebuilt.docx'
    rebuild_docx({'blocks': blocks}, target)
    assert '42.01' in inspect_docx(target)['text']
    assert '17.5 percent' in inspect_docx(target)['text']


def test_pdf_review_requires_each_uncertain_block(tmp_path):
    from paper_formatter.pdf_io import apply_review
    data = {'blocks': [{'id': 'b1', 'kind': 'text', 'text': 'unclear', 'review_required': True}], 'issues': []}
    result = apply_review(data, {})
    assert any(x['code'] == 'PDF_BLOCK_REVIEW_REQUIRED' for x in result['issues'])
    result = apply_review(data, {'b1': {'text': 'clear', 'confirmed': True}})
    assert result['blocks'][0]['text'] == 'clear'
    assert not result['issues']


def test_pdf_table_reconstruction_keeps_merges_and_cells(tmp_path):
    from docx import Document
    target=tmp_path/'table.docx'
    data={'blocks':[{'id':'t1','kind':'table','text':'Title A B','table':{'rows':2,'cols':2,'cells':[
        {'row':0,'col':0,'row_span':1,'col_span':2,'text':'Title'},
        {'row':1,'col':0,'row_span':1,'col_span':1,'text':'A'},
        {'row':1,'col':1,'row_span':1,'col_span':1,'text':'B'}]}}]}
    rebuild_docx(data,target)
    d=Document(target)
    assert len(d.tables)==1
    assert d.tables[0].cell(0,0).text=='Title'
    assert d.tables[0].cell(0,0)._tc is d.tables[0].cell(0,1)._tc
    assert d.tables[0].cell(1,1).text=='B'


def test_incomplete_detected_table_keeps_original_pixels_and_requires_review(tmp_path):
    from PIL import Image
    from docx import Document
    from paper_formatter.pdf_io import adopt_recognized_table, apply_review
    image=tmp_path/'source-table.png'
    Image.new('RGB',(360,150),'white').save(image)
    block={'id':'t1','kind':'image','label':'table','page':0,'image':str(image),
           'image_sha256':fingerprint(image),'width_pt':180,'height_pt':75,
           'review_required':True,'text':''}
    incomplete={'rows':2,'cols':2,'cells':[{'row':0,'col':0,'row_span':1,'col_span':2,'text':'Heading'},
                                         {'row':1,'col':0,'row_span':1,'col_span':1,'text':'42.01'}]}
    assert adopt_recognized_table(block,incomplete) is False
    assert block['kind']=='image' and block['table_recognition_error']=='TABLE_MISSING_CELLS'
    assert 'table' not in block
    extraction={'blocks':[block],'issues':[]}
    pending=apply_review(extraction,{})
    assert 'PDF_BLOCK_REVIEW_REQUIRED' in {issue['code'] for issue in pending['issues']}
    target=tmp_path/'checked.docx'
    rebuild_docx(pending,target)
    output=Document(target)
    assert len(output.tables)==0
    assert len(output.inline_shapes)==1


def test_local_review_can_correct_order_and_misclassified_table():
    from paper_formatter.pdf_io import apply_review
    data={'blocks':[{'id':'b1','kind':'table','text':'Right text','review_required':True},
                    {'id':'b2','kind':'text','text':'Left text','review_required':True}], 'issues':[]}
    reviewed=apply_review(data,{'b1':{'confirmed':True,'kind':'text','text':'Right text','order':2},
                              'b2':{'confirmed':True,'text':'Left text','order':1}})
    assert [b['id'] for b in reviewed['blocks']]==['b2','b1']
    assert reviewed['blocks'][1]['kind']=='text'


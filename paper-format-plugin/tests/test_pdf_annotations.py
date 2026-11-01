from reportlab.pdfgen import canvas
from pypdf import PdfReader,PdfWriter
from pypdf.annotations import Highlight,Link
from pypdf.generic import ArrayObject,FloatObject,DictionaryObject,NameObject,NumberObject
from paper_formatter.pdf_annotations import snapshot,embed,active_pdf_issue
from paper_formatter.annotations import migrate_annotations


def make_pdf(path,y):
    c=canvas.Canvas(str(path));c.setFont('Helvetica',12);c.drawString(72,y,'A unique highlighted sentence for migration.');c.save()


def test_embedded_highlight_keeps_identity_contents_and_coordinates(tmp_path):
    original=tmp_path/'original.pdf';new=tmp_path/'new.pdf';result=tmp_path/'result.pdf'
    make_pdf(original,700);make_pdf(new,600)
    writer=PdfWriter(clone_from=original)
    writer.add_annotation(0,Highlight(rect=(72,697,305,710),quad_points=ArrayObject([FloatObject(v) for v in (72,710,305,710,72,697,305,697)])))
    with original.open('wb') as f:writer.write(f)
    saved=snapshot(original)
    mapped=migrate_annotations(saved['annotations'],original,new)
    assert mapped['complete'],mapped
    embed(original,new,result,saved['annotations'],mapped['annotations'])
    restored=snapshot(result)
    assert len(restored['annotations'])==1
    assert restored['annotations'][0]['text']==saved['annotations'][0]['text']
    assert restored['annotations'][0]['position']['rects'][0][1]<650


def test_internal_open_destination_is_safe_but_external_action_is_blocked():
    assert not active_pdf_issue(DictionaryObject({NameObject('/OpenAction'):ArrayObject([NumberObject(0),NameObject('/Fit')])}))
    assert active_pdf_issue(DictionaryObject({NameObject('/OpenAction'):DictionaryObject({
        NameObject('/S'):NameObject('/URI'),NameObject('/URI'):'https://example.org'})}))


def test_multiple_pdf_links_remain_one_blocking_review_item(tmp_path):
    source=tmp_path/'links.pdf';make_pdf(source,700)
    writer=PdfWriter(clone_from=source)
    writer.add_annotation(0,Link(rect=(72,690,120,715),url='https://example.org/one'))
    writer.add_annotation(0,Link(rect=(130,690,180,715),url='https://example.org/two'))
    with source.open('wb') as output:writer.write(output)
    recorded=snapshot(source)
    issues=[item for item in recorded['issues'] if item['code']=='PDF_LINK_REVIEW']
    assert len(issues)==1 and issues[0]['count']==2 and issues[0]['blocking']

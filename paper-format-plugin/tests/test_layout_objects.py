from docx import Document
from docx.enum.section import WD_SECTION
from docx.oxml.ns import qn
from docx.shared import Inches, Mm
from PIL import Image

from paper_formatter.docx_io import format_docx, inspect_docx, read_package, xml, NS, q
from paper_formatter.profiles import blank_profile


def source_with_nested_objects(path):
    picture=path.with_suffix('.png');Image.new('RGB',(600,100),'navy').save(picture)
    doc=Document();doc.sections[0].page_width=Mm(210)
    outer=doc.add_table(rows=2,cols=2)
    outer.cell(0,0).text='Outer left';outer.cell(0,1).text='Outer right'
    nested=outer.cell(1,0).add_table(rows=2,cols=2)
    nested.cell(0,0).merge(nested.cell(0,1)).text='Merged nested heading'
    nested.cell(1,0).text='Value';nested.cell(1,1).text='42.01'
    outer.cell(1,1).paragraphs[0].add_run().add_picture(str(picture),width=Inches(6))
    doc.save(path)
    return path


def test_nested_table_and_picture_fit_their_resized_parent_cells(tmp_path):
    source=source_with_nested_objects(tmp_path/'nested.docx');target=tmp_path/'fitted.docx'
    report=format_docx(source,target,blank_profile({'page':{'columns':2,'column_gap_mm':8,'margins_mm':{'left':25,'right':25}},'tables':{'fit_width':True}}))
    assert report['passed'],report['issues']
    root=xml(read_package(target)['word/document.xml'])
    outer=root.find('.//w:tbl',NS)
    nested=outer.find('.//w:tc/w:tbl',NS)
    cell=nested.getparent()
    available=int(cell.find('w:tcPr/w:tcW',NS).get(q('w')))-216
    assert int(nested.find('w:tblPr/w:tblW',NS).get(q('w')))<=available
    image=root.find('.//wp:extent',NS)
    image_cell=next(n for n in image.iterancestors() if n.tag==q('tc'))
    image_available=int(image_cell.find('w:tcPr/w:tcW',NS).get(q('w')))-216
    assert int(image.get('cx'))<=image_available*635
    # A merged cell spans the full resized inner grid, without changing its content.
    assert int(nested.find('w:tr/w:tc/w:tcPr/w:tcW',NS).get(q('w')))==sum(int(c.get(q('w'))) for c in nested.find(q('tblGrid')))
    before,after=inspect_docx(source),inspect_docx(target)
    for key in ('content_hash','assets','math','tables','native_fields'):
        assert before[key]==after[key]


def test_switching_to_columns_caps_a_wide_table_without_extra_table_rule(tmp_path):
    source=tmp_path/'source.docx';target=tmp_path/'target.docx'
    doc=Document();doc.sections[0].page_width=Mm(210);table=doc.add_table(rows=2,cols=3)
    for i,c in enumerate(table.rows[0].cells):c.text=f'Column {i+1}'
    table.cell(1,0).text='All cells remain in the manuscript';doc.save(source)
    report=format_docx(source,target,blank_profile({'page':{'columns':2,'column_gap_mm':8,'margins_mm':{'left':25,'right':25}}}))
    assert report['passed']
    root=xml(read_package(target)['word/document.xml'])
    table=root.find('.//w:tbl',NS);width=sum(int(c.get(q('w'))) for c in table.find(q('tblGrid')))
    assert width<=round(76*1440/25.4)


def test_each_section_keeps_its_own_available_object_width(tmp_path):
    picture=tmp_path/'picture.png';Image.new('RGB',(600,100),'navy').save(picture)
    source=tmp_path/'source.docx';target=tmp_path/'target.docx';doc=Document()
    doc.sections[0].page_width=Mm(210);doc.sections[0].left_margin=doc.sections[0].right_margin=Mm(25)
    doc.add_picture(str(picture),width=Mm(140))
    section=doc.add_section(WD_SECTION.NEW_PAGE);cols=section._sectPr.find(qn('w:cols'));cols.set(qn('w:num'),'2');cols.set(qn('w:space'),str(round(8*1440/25.4)))
    doc.add_picture(str(picture),width=Mm(140));doc.save(source)
    assert format_docx(source,target,blank_profile({'body':{'size_pt':12}}))['passed']
    root=xml(read_package(target)['word/document.xml']);extents=root.findall('.//wp:extent',NS)
    assert int(extents[0].get('cx'))==Mm(140)
    # Page size, margins and gap round independently to OOXML twips.
    assert int(extents[1].get('cx'))<=round(76*36000)+635


def test_body_after_reference_section_does_not_inherit_reference_typography(tmp_path):
    source=tmp_path/'source.docx';target=tmp_path/'target.docx';doc=Document()
    doc.add_heading('References',1);doc.add_paragraph('Example author. Example reference.')
    doc.add_heading('Appendix',1);doc.add_paragraph('Additional measurements remain body text.')
    doc.save(source)
    assert format_docx(source,target,blank_profile({'body':{'size_pt':12},'references':{'size_pt':9}}))['passed']
    output=Document(target)
    assert output.paragraphs[1].runs[0].font.size.pt==9
    assert output.paragraphs[3].runs[0].font.size.pt==12

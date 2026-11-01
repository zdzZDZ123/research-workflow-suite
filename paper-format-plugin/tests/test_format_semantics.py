import pytest
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from paper_formatter.checks import applied_rule_issues
from paper_formatter.contracts import FormatterError
from paper_formatter.docx_io import format_docx, read_package, write_package, xml, serialize, q, NS
from paper_formatter.profiles import blank_profile, validate_profile, merge_profiles, distill_template, parse_simple_instructions


def formatted(tmp_path, rules):
    source=tmp_path/'source.docx';target=tmp_path/'formatted.docx'
    doc=Document();doc.add_paragraph('The measured result remains 42.01.');doc.save(source)
    profile=blank_profile(rules)
    assert format_docx(source,target,profile)['passed']
    assert not applied_rule_issues(target,profile)
    return target,profile


def mutate(path,fn,part='word/document.xml'):
    package=read_package(path);root=xml(package[part]);fn(root);package[part]=serialize(root);write_package(path,package)


def test_exclusive_line_spacing_merges_by_authority_and_records_replacement():
    with pytest.raises(FormatterError,match='LINE_SPACING_CONFLICT'):
        validate_profile(blank_profile({'body':{'line_spacing':1.5,'line_spacing_pt':18}}))
    result=merge_profiles(blank_profile({'body':{'line_spacing_pt':18}}),
                          blank_profile({'body':{'line_spacing':1.5}}),
                          blank_profile({'body':{'line_spacing_pt':24}}))
    assert result['rules']['body']=={'line_spacing_pt':24}
    assert [x['replaced_by'] for x in result['overrides']]==['body.line_spacing','body.line_spacing_pt']


@pytest.mark.parametrize('instruction,expected',[
    ('A4；正文 12pt；1.5 倍行距；页码居中',{'size_pt':12,'line_spacing':1.5}),
    ('正文：小四',{'size_pt':12}),
    ('12 磅',{'size_pt':12}),
])
def test_size_only_instruction_never_treats_body_role_as_a_font(instruction,expected):
    result=parse_simple_instructions(instruction)
    assert result['rules']['body']==expected
    assert not result['unresolved']


def test_size_only_heading_and_reference_roles_are_resolved():
    result=parse_simple_instructions('一级标题 16pt；参考文献：10.5 磅')
    assert result['rules']=={'headings':{'1':{'size_pt':16}},'references':{'size_pt':10.5}}
    assert not result['unresolved']


def test_impossible_explicit_columns_are_rejected_before_rendering():
    with pytest.raises(FormatterError,match='PAGE_NO_USABLE_COLUMN_WIDTH'):
        validate_profile(blank_profile({'page':{'width_mm':100,'columns':3,'column_gap_mm':40,
                                                'margins_mm':{'left':10,'right':10}}}))


def test_template_at_least_spacing_is_not_silently_changed_to_exact(tmp_path):
    doc=Document();ppr=doc.styles['Normal']._element.get_or_add_pPr()
    spacing=OxmlElement('w:spacing');spacing.set(qn('w:line'),'360');spacing.set(qn('w:lineRule'),'atLeast');ppr.append(spacing)
    path=tmp_path/'template.docx';doc.save(path)
    profile=distill_template(path)
    assert 'line_spacing_pt' not in profile['rules']['body']
    assert any('atLeast' in x for x in profile['unresolved'])


def test_same_numeric_line_spacing_with_wrong_unit_is_rejected(tmp_path):
    path,profile=formatted(tmp_path,{'body':{'line_spacing':1.5}})
    mutate(path,lambda root:root.find('.//w:p/w:pPr/w:spacing',NS).set(q('lineRule'),'exact'))
    assert {i['rule'] for i in applied_rule_issues(path,profile)}=={'body.line_spacing'}


@pytest.mark.parametrize('name,tag',[('bold','b'),('italic','i')])
def test_requested_false_character_property_cannot_be_replaced_by_true(tmp_path,name,tag):
    path,profile=formatted(tmp_path,{'body':{name:False}})
    mutate(path,lambda root:root.find('.//w:r/w:rPr/w:'+tag,NS).set(q('val'),'1'))
    assert {i['rule'] for i in applied_rule_issues(path,profile)}=={'body.'+name}


def test_western_font_cannot_differ_when_single_font_was_requested(tmp_path):
    path,profile=formatted(tmp_path,{'body':{'font':'Times New Roman'}})
    mutate(path,lambda root:root.find('.//w:r/w:rPr/w:rFonts',NS).set(q('ascii'),'Arial'))
    assert {i['rule'] for i in applied_rule_issues(path,profile)}=={'body.font'}


def test_requested_indent_and_keep_with_next_are_checked(tmp_path):
    path,profile=formatted(tmp_path,{'body':{'first_line_mm':8,'keep_with_next':True}})
    def change(root):
        root.find('.//w:p/w:pPr/w:ind',NS).set(q('firstLineChars'),'100')
        root.find('.//w:p/w:pPr/w:keepNext',NS).set(q('val'),'0')
    mutate(path,change)
    assert {i['rule'] for i in applied_rule_issues(path,profile)}=={'body.first_line_mm','body.keep_with_next'}


@pytest.mark.parametrize('change,rule',[('text','footer.text'),('page','footer.page_number'),('align','footer.align'),('size','footer.size_pt')])
def test_footer_content_page_field_and_typography_are_validated(tmp_path,change,rule):
    path,profile=formatted(tmp_path,{'footer':{'text':'Study draft','page_number':True,'align':'center','size_pt':9}})
    part=next(name for name in read_package(path) if name.startswith('word/footer-') and name.endswith('.xml'))
    def alter(root):
        if change=='text':root.find('.//w:t',NS).text='Wrong header'
        elif change=='page':
            field=root.find('.//w:fldSimple',NS);field.getparent().remove(field)
        elif change=='align':root.find('.//w:pPr/w:jc',NS).set(q('val'),'left')
        else:root.find('.//w:r/w:rPr/w:sz',NS).set(q('val'),'30')
    mutate(path,alter,part)
    assert rule in {i['rule'] for i in applied_rule_issues(path,profile)}


def test_missing_active_footer_reference_is_not_validated(tmp_path):
    path,profile=formatted(tmp_path,{'footer':{'page_number':True}})
    def remove(root):
        node=root.find('.//w:footerReference',NS);node.getparent().remove(node)
    mutate(path,remove)
    assert 'footer.reference' in {i['rule'] for i in applied_rule_issues(path,profile)}

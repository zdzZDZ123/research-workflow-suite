from docx import Document
from paper_formatter.citation_mapping import seed_citations, citation_context
from paper_formatter.docx_io import inspect_docx


def test_explicit_local_mapping_creates_fields_without_changing_visible_text(tmp_path):
    source=tmp_path/'plain.docx';target=tmp_path/'seeded.docx'
    d=Document();d.add_paragraph('The measured value is supported by [1].');d.add_paragraph('References');d.add_paragraph('[1] A. Author. Original reference. 2024.');d.save(source)
    mapping={'confirmed':True,'citations':[{'paragraph':0,'quote':'[1]','items':[{'id':'REF00001','itemData':{'id':'REF00001','type':'article-journal','title':'Original reference','author':[{'family':'Author'}],'issued':{'date-parts':[[2024]]}}}]}],'bibliography_paragraphs':[2]}
    result=seed_citations(source,target,mapping)
    assert len(result['citations'])==1
    assert [p.text for p in Document(source).paragraphs]==[p.text for p in Document(target).paragraphs]
    assert len(inspect_docx(target)['citations'])==1


def test_local_mapping_rejects_ambiguous_quotes(tmp_path):
    import pytest
    from paper_formatter.contracts import FormatterError
    source=tmp_path/'plain.docx';d=Document();d.add_paragraph('[1] then [1]');d.save(source)
    with pytest.raises(FormatterError,match='CITATION_QUOTE_AMBIGUOUS'):
        seed_citations(source,tmp_path/'out.docx',{'confirmed':True,'citations':[{'paragraph':0,'quote':'[1]','items':[{'id':'X','itemData':{'title':'Test','type':'book'}}]}],'bibliography_paragraphs':[]})

from pathlib import Path
from docx import Document
from paper_formatter.cli import dispatch
from paper_formatter.contracts import fingerprint


def test_word_end_to_end_and_restore(tmp_path):
    path=tmp_path/'paper.docx'
    d=Document()
    d.add_heading('A scientific manuscript',1)
    d.add_paragraph('The measured result is 42.01 for all 17 participants.')
    d.save(path)
    identity={'library_id':1,'key':'ABCDEFGH','parent_key':'BCDEFGHI','path':str(path)}
    profile={'schema_version':1,'rules':{'body':{'font':'Times New Roman','size_pt':12},'page':{'margins_mm':dict.fromkeys(('top','bottom','left','right'),25)}},'sources':[],'unresolved':[]}
    home=tmp_path/'home'
    original=fingerprint(path)
    job=dispatch({'command':'prepare','request':{'schema_version':1,'attachment':identity,'annotations':[],'source_sha256':original},'profile':profile},home)
    result=dispatch({'command':'convert','run_id':job['run_id']},home)
    assert result['status']=='validated', result.get('report')
    assert fingerprint(path)==original
    args={'run_id':job['run_id'],'attachment':identity,'annotations':[]}
    assert dispatch({'command':'begin_commit',**args},home)['status']=='awaiting_zotero'
    assert dispatch({'command':'finish_commit',**args},home)['status']=='committed'
    restored=dispatch({'command':'restore',**args},home)
    args['run_id']=restored['run_id']
    dispatch({'command':'begin_commit',**args},home)
    dispatch({'command':'finish_commit',**args},home)
    assert fingerprint(path)==original

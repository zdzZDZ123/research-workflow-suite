import copy
from pathlib import Path

import pytest
from docx import Document
from docx.shared import Pt

from paper_formatter.contracts import FormatterError, fingerprint
from paper_formatter.engine import Engine, invalidate_manual_layout, require_manual_relocation
from paper_formatter.profiles import blank_profile


def test_area_position_must_be_redrawn_after_corrected_docx_moves_content(tmp_path):
    source=tmp_path/'paper.docx'
    doc=Document();doc.add_paragraph('The measured result is 42.01 for all 17 participants.');doc.save(source)
    original=fingerprint(source)
    annotation={'key':'REGION01','type':'image','comment':'Keep this region', 'color':'#ffd400',
                'position':{'pageIndex':0,'rects':[[72,680,330,720]]}}
    engine=Engine(tmp_path/'home',tmp_path/'models')
    job=engine.prepare({'schema_version':1,'attachment':{'library_id':1,'key':'PAPER001','path':str(source)},
                        'source_sha256':original,'annotations':[annotation]},blank_profile({'body':{'size_pt':12}}))
    job=engine.run(job['run_id'])
    assert job['status']=='needs_review'
    token=job['preview_layout_sha256']
    manual={'REGION01':annotation['position']}
    job=engine.run(job['run_id'],manual=manual,layout_sha256=token)
    assert job['status']=='validated',job['report']['issues']
    assert job['manual_layout_sha256']==token
    corrected=tmp_path/'correction.docx'
    doc=Document(job['editable_docx']);doc.paragraphs[0].paragraph_format.space_before=Pt(60);doc.save(corrected)
    job=engine.run(job['run_id'],corrected_docx=str(corrected))
    assert job['status']=='needs_review'
    assert job['preview_layout_sha256']!=token
    assert not job['manual_annotations']
    assert job['manual_relocation_required']['manual_annotations']==['REGION01']
    assert 'MANUAL_LAYOUT_CHANGED' in {i['code'] for i in job['report']['issues']}
    assert job['manual_placement_history'][-1]['manual_annotations']==manual
    with pytest.raises(FormatterError,match='MANUAL_LAYOUT_MISMATCH'):
        engine.run(job['run_id'],manual=manual,layout_sha256=token)
    resumed=engine.run(job['run_id'])
    assert resumed['status']=='needs_review'
    moved={'REGION01':{'pageIndex':0,'rects':[[72,620,330,660]]}}
    fixed=engine.run(job['run_id'],manual=moved,layout_sha256=resumed['preview_layout_sha256'])
    assert fixed['status']=='validated',fixed['report']['issues']
    assert fixed['annotations_target'][0]['position']==moved['REGION01']
    assert fingerprint(source)==original


@pytest.mark.parametrize('map_name',['manual_annotations','embedded_manual'])
def test_unbound_legacy_manual_note_cannot_fall_back_to_guessed_auto_position(map_name):
    position={'pageIndex':0,'rects':[[10,10,40,40]]}
    job={map_name:{'NOTE0001':position}}
    invalidate_manual_layout(job,'current-page-digest')
    assert job[map_name]=={}
    annotation={'key':'NOTE0001','type':'note','text':'','position':position}
    auto={'complete':True,'annotations':[copy.deepcopy(annotation)],'unresolved':[]}
    guarded=require_manual_relocation(auto,[annotation],job['manual_relocation_required'][map_name])
    assert not guarded['complete'] and not guarded['annotations']
    assert guarded['unresolved'][0]['code']=='MANUAL_LAYOUT_CHANGED'

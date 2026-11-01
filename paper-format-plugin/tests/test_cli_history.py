from pathlib import Path

from paper_formatter.cli import dispatch
from paper_formatter.contracts import fingerprint
from paper_formatter.store import JobStore


def test_cli_lists_partial_history_and_pending_without_reading_document_previews(tmp_path):
    home = tmp_path/'home'
    store = JobStore(home/'jobs')
    source = tmp_path/'paper.pdf'; source.write_bytes(b'%PDF original fixture')
    identity = {'library_id':1,'key':'PAPER001','path':str(source)}
    job = store.create({'schema_version':1,'attachment':identity,'annotations':[],'source_sha256':fingerprint(source)})
    candidate = Path(job['run_dir'])/'candidate.pdf'; candidate.write_bytes(b'%PDF new fixture')
    store.stage(job['run_id'],candidate,{'passed':True,'issues':[]},[])
    job = store.begin_commit(job['run_id'],identity,[])
    extraction = Path(job['run_dir'])/'broken-extraction.json'; extraction.write_text('{')
    job['extraction_path'] = str(extraction)
    job['preview_new'] = {'pages':[{'chars':['large local payload']}]}
    store.save(job,'test_unreadable_preview')
    broken = store.run_dir('f'*32)/'manifest.json'; broken.parent.mkdir(); broken.write_text('{')
    for command in ('history','pending'):
        response = dispatch({'command':command},home)
        assert len(response['jobs']) == 1 and len(response['errors']) == 1
        assert response['jobs'][0]['run_id'] == job['run_id']
        assert 'preview_new' not in response['jobs'][0] and 'extraction_path' not in response['jobs'][0]
        assert response['errors'][0]['run_id'] == 'f'*32
    assert source.read_bytes()==b'%PDF new fixture'

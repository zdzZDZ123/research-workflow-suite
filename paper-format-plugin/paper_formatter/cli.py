"""One JSON request on stdin, progress/result JSON lines on stdout; no persistent server."""
from __future__ import annotations
import contextlib
import importlib.metadata
import json
import os
import sys
from pathlib import Path
from .contracts import FormatterError, default_home, fingerprint, read_json, write_json, digest, utc_now


def emit(event,**data):
    print(json.dumps({'event':event,**data},ensure_ascii=False,allow_nan=False),flush=True)


def configuration(home):
    path=home/'config.json'
    value=read_json(path) if path.is_file() else {}
    return {'models':str(Path(__file__).resolve().parents[1]/'.runtime/models'),**value}


def dispatch(request,home=None,progress=lambda *x:None):
    home=Path(home or default_home()).resolve()
    home.mkdir(parents=True,exist_ok=True)
    config=configuration(home)
    cmd=request.get('command')
    if cmd=='settings':
        from .credentials import read_secret
        return {**config,'has_model_key':bool(read_secret('model')),'has_search_key':bool(read_secret('search'))}
    if cmd=='configure':
        from .credentials import save_secret
        allowed={'base_url','model','api_style','models','soffice'}
        values=request.get('settings',{})
        if set(values)-allowed:raise FormatterError('UNKNOWN_SETTING')
        if any(not isinstance(v,str) for v in values.values()):raise FormatterError('SETTING_STRING_REQUIRED')
        # Keys arrive on stdin only; never add them to config, job files, or command arguments.
        for name in ('model','search'):
            secret=request.get(name+'_key')
            if secret:save_secret(name,secret)
        write_json(home/'config.json',{**config,**values})
        return {'saved':True}
    if config.get('soffice'):
        os.environ['PAPER_FORMATTER_SOFFICE']=config['soffice']
    if cmd=='doctor':
        from .render import locate_soffice
        versions={}
        for pkg in ('lxml','python-docx','pypdf','pypdfium2','docling','rapidocr','onnxruntime','fonttools'):
            try:versions[pkg]=importlib.metadata.version(pkg)
            except importlib.metadata.PackageNotFoundError:versions[pkg]=None
        try:office=locate_soffice()
        except FormatterError:office=None
        models=Path(config['models'])
        marker=models/'paper-formatter-models.json'
        return {'python':sys.executable,'version':sys.version,'packages':versions,'libreoffice':office,
            'models':str(models),'models_installed':marker.is_file(),
            'docx_ready':bool(office and all(versions[p] for p in ('lxml','python-docx','pypdfium2','fonttools'))),
            'pdf_ready':bool(office and all(versions.values()) and marker.is_file()),'home':str(home)}
    if cmd in ('resolve','search'):
        # A disjoint protocol rejects accidental attachment/template/job payloads before importing network code.
        if set(request)-{'command','input','refresh','use_model'}:raise FormatterError('RULE_PROCESS_BOUNDARY')
        from .rules import RuleInput, ModelClient, search_journal, fetch_guideline
        from .credentials import read_secret
        from .profiles import parse_simple_instructions,merge_profiles,blank_profile
        value=RuleInput.from_dict(request.get('input',{}))
        if cmd=='search':return {'candidates':search_journal(value,read_secret('search'))}
        # Version 2 discards rules cached before the size-only parser and spacing
        # precedence fixes. Previously confirmed job profiles remain in their jobs.
        cache_id=digest({'request':request['input'],'model':{k:config.get(k) for k in ('model','base_url','api_style')},'version':2})
        cache=home/'profiles'/(cache_id+'.json')
        if cache.is_file() and not request.get('refresh'):
            return {'profile':read_json(cache),'cached':True}
        explicit=parse_simple_instructions(value.instruction) if value.instruction else blank_profile()
        journal=blank_profile()
        client=None
        def model():
            nonlocal client
            if client is None:client=ModelClient(config,read_secret('model'))
            return client
        if value.guideline_url:
            public=fetch_guideline(value.guideline_url)
            journal=model().resolve(RuleInput(journal=value.journal,article_type=value.article_type),public)
        elif value.journal:
            raise FormatterError('OFFICIAL_GUIDELINE_SELECTION_REQUIRED','请先检索并选择对应期刊和文章类型的出版方指南')
        if explicit['unresolved'] and request.get('use_model',True):
            explicit=model().resolve(RuleInput(instruction=value.instruction))
        profile=merge_profiles(journal=journal,explicit=explicit)
        if not profile['unresolved']:write_json(cache,profile)
        return {'profile':profile,'journal_profile':journal,'explicit_profile':explicit,'cached':False}
    # All following document commands run with network calls denied.
    from .offline import offline_processing
    with offline_processing():
        from .store import JobStore
        from .engine import Engine,public_job
        from .profiles import distill_template,merge_profiles,validate_profile
        store=JobStore(home/'jobs')
        if cmd=='inspect':
            from .contracts import attachment_identity
            identity=attachment_identity(request['attachment'])
            result={'attachment':identity,'source_sha256':fingerprint(identity['path'])}
            if Path(identity['path']).suffix.lower()=='.docx':
                from .docx_io import inspect_docx
                result['document']=inspect_docx(identity['path'])
            return result
        if cmd=='template':return {'profile':distill_template(request['path'])}
        if cmd=='merge_profile':
            result=merge_profiles(request.get('journal'),request.get('template'),request.get('explicit'))
            return {'profile':result}
        if cmd=='cache_profile':
            profile=validate_profile(request['profile'])
            if profile['unresolved']:raise FormatterError('UNRESOLVED_PROFILE')
            profile['confirmed_at']=utc_now()
            name=digest(profile)
            write_json(home/'profiles'/(name+'.json'),profile)
            return {'profile':profile,'id':name}
        if cmd=='profiles':
            return {'profiles':[{'id':p.stem,'profile':read_json(p)} for p in (home/'profiles').glob('*.json')]}
        if cmd in ('history','pending'):
            from .store import PENDING
            scan=store.scan_history(request.get('attachment'))
            fields=('run_id','status','created_at','attachment','report_path','error') if cmd=='history' else (
                'run_id','status','attachment','annotations_original','annotations_target','restores_run')
            # Recovery needs transaction metadata only, never previews or parsed document files.
            return {'jobs':[{k:j[k] for k in fields if k in j} for j in scan['jobs']
                            if cmd=='history' or j['status'] in PENDING], 'errors':scan['errors']}
        if cmd=='load':return public_job(store.load(request['run_id']))
        if cmd in ('citation_context','seed_citations'):
            from .citation_mapping import citation_context,seed_citations
            job=store.load(request['run_id'])
            if job['status'] not in ('prepared','needs_review','validated','failed'):raise FormatterError('CITATION_MAPPING_STATE')
            source=job.get('citation_source_path') or job['original_path']
            if Path(source).suffix.lower()!='.docx':raise FormatterError('CONVERT_BEFORE_MAPPING')
            if cmd=='citation_context':return {'paragraphs':citation_context(source)}
            result=seed_citations(source,Path(job['run_dir'])/'citation-seeded.docx',request['mapping'])
            job['citation_mapping']=request['mapping'];job['status']='needs_review'
            job['report']['passed']=False
            store.save(job,'local_citation_mapping_saved')
            return result
        engine=Engine(home,config['models'],progress)
        if cmd=='prepare':return public_job(engine.prepare(request['request'],request['profile']))
        if cmd in ('convert','review'):
            return engine.run(request['run_id'],**{k:request[k] for k in ('reviews','manual','embedded_manual','citation_updates','corrected_docx','clear_correction','profile','layout_sha256') if k in request})
        actions={'begin_commit':store.begin_commit,'finish_commit':store.finish_commit,
            'begin_rollback':store.begin_rollback,'finish_rollback':store.finish_rollback,'restore':store.prepare_restore}
        if cmd in actions:
            return public_job(actions[cmd](request['run_id'],request['attachment'],request.get('annotations',[])))
        raise FormatterError('UNKNOWN_COMMAND',str(cmd))


def main():
    if hasattr(sys.stdin,'reconfigure'):sys.stdin.reconfigure(encoding='utf-8')
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    try:
        raw=sys.stdin.buffer.read(25_000_001)
        if len(raw)>25_000_000:raise FormatterError('REQUEST_TOO_LARGE')
        request=json.loads(raw.decode('utf-8-sig'))
        if not isinstance(request,dict):raise FormatterError('REQUEST_OBJECT')
        # Keep library diagnostics off the structured protocol channel.
        protocol=sys.stdout
        def progress(phase,message):
            protocol.write(json.dumps({'event':'progress','phase':phase,'message':message},ensure_ascii=False)+'\n')
            protocol.flush()
        with contextlib.redirect_stdout(sys.stderr):
            result=dispatch(request,progress=progress)
        emit('result',data=result)
    except Exception as exc:
        emit('error',code=getattr(exc,'code',type(exc).__name__),detail=getattr(exc,'detail',str(exc))[:2000])
        return 1
    return 0


if __name__=='__main__':
    raise SystemExit(main())

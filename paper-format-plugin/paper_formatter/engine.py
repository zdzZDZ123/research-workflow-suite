"""Local document work and non-bypassable validation before a staged commit."""
from __future__ import annotations
import copy
import html
import os
import shutil
import uuid
from pathlib import Path
from .contracts import FormatterError, digest, fingerprint, issue, normalized_annotations, read_json, utc_now, write_json
from .store import JobStore, file_lock
from .profiles import validate_profile
from .offline import offline_processing
from .docx_io import inspect_docx, format_docx, preservation_issues
from .render import render_docx, inspect_render
from .checks import font_issues, render_content_issues, requirement_issues, applied_rule_issues
from .annotations import migrate_annotations


def preview_layout_digest(view):
    """Bind locations to visible pixels and page geometry, not PDF metadata dates."""
    return digest([{'index':page['index'],'width':page['width'],'height':page['height'],
                    'image_sha256':fingerprint(page['image'])} for page in view['pages']])


MANUAL_MAPS = ('manual_annotations', 'embedded_manual')


def invalidate_manual_layout(job, layout):
    if not any(job.get(name) for name in MANUAL_MAPS):
        return
    if layout and job.get('manual_layout_sha256') == layout:
        return
    job.setdefault('manual_placement_history',[]).append({
        'recorded_at':utc_now(),'layout_sha256':job.get('manual_layout_sha256'),
        **{name:copy.deepcopy(job.get(name,{})) for name in MANUAL_MAPS}})
    required=job.setdefault('manual_relocation_required',{})
    for name in MANUAL_MAPS:
        required[name]=sorted(set(required.get(name,[])) | set(job.get(name,{})))
        job[name]={}
    job.pop('manual_layout_sha256',None)


def require_manual_relocation(result, annotations, required):
    """A stale user-selected area cannot be replaced by a guessed automatic anchor."""
    required=set(required)
    if not required:
        return result
    result['annotations']=[row for row in result['annotations'] if row['key'] not in required]
    result['unresolved']=[row for row in result['unresolved'] if row['key'] not in required]
    result['unresolved'] += [{'key':row['key'],'type':row.get('type'),'text':row.get('text',''),
                             'code':'MANUAL_LAYOUT_CHANGED','original_position':row.get('position')}
                            for row in normalized_annotations(annotations) if row['key'] in required]
    result['complete']=not result['unresolved']
    return result


class Engine:
    def __init__(self,home,models,progress=lambda *x:None):
        self.store=JobStore(Path(home)/'jobs')
        self.models=Path(models)
        self.progress=progress

    def prepare(self,request,profile):
        profile=validate_profile(profile)
        job=self.store.create(request)
        job['profile']=profile
        job['reviews']={}
        return self.store.save(job,'profile_saved')

    def run(self, run_id, **options):
        with file_lock(self.store.run_dir(run_id) / 'conversion.lock'):
            return self._run_locked(run_id, **options)

    def _run_locked(self,run_id,*,reviews=None,manual=None,embedded_manual=None,citation_updates=None,corrected_docx=None,clear_correction=False,profile=None,layout_sha256=None):
        job=self.store.load(run_id)
        if job['status'] not in ('prepared','needs_review','validated','failed'):
            raise FormatterError('INVALID_CONVERSION_STATE')
        if not isinstance(clear_correction, bool):
            raise FormatterError('CLEAR_CORRECTION_BOOLEAN_REQUIRED')
        if clear_correction and corrected_docx:
            raise FormatterError('CORRECTION_ACTION_CONFLICT', '不能同时选择校对稿和清除校对稿')
        directory=Path(job['run_dir'])
        if fingerprint(job['original_path'])!=job['source_sha256']:
            raise FormatterError('BACKUP_CHANGED')
        for value in (manual,embedded_manual):
            if value is not None and not isinstance(value,dict):
                raise FormatterError('MANUAL_MAP_REQUIRED')
        if (manual or embedded_manual) and (not layout_sha256 or layout_sha256!=job.get('preview_layout_sha256')):
            raise FormatterError('MANUAL_LAYOUT_MISMATCH','批注定位不属于当前检查稿，请重新打开任务并在最新页面上定位')
        # In particular, do not bless legacy unbound positions when a new map is submitted.
        invalidate_manual_layout(job,job.get('preview_layout_sha256'))
        if profile is not None:
            job['profile']=validate_profile(profile)
        if reviews is not None:
            job['reviews']=reviews
        for name,value in zip(MANUAL_MAPS,(manual,embedded_manual)):
            if value is not None:
                job[name]=value
                required=job.setdefault('manual_relocation_required',{})
                required[name]=sorted(set(required.get(name,[]))-set(value))
        if manual or embedded_manual:
            job['manual_layout_sha256']=layout_sha256
        if citation_updates is not None:
            job['citation_updates']=citation_updates
        job['status']='converting'
        job.pop('error',None)
        self.store.save(job,'conversion_started')
        try:
            with offline_processing():
                correction=self._select_correction(job,corrected_docx,clear_correction)
                return self._run(job,correction)
        except Exception as exc:
            job=self.store.load(run_id)
            job['status']='failed'
            job['error']={'code':getattr(exc,'code',type(exc).__name__),'detail':getattr(exc,'detail',str(exc))[:1000]}
            self.store.save(job,'conversion_failed')
            raise

    def _snapshot_correction(self,job,source,expected=None):
        """Copy once into a unique job file; never render from the user's mutable file."""
        source=Path(source)
        if source.suffix.lower()!='.docx':
            raise FormatterError('CORRECTION_DOCX_REQUIRED')
        if not source.is_file():
            raise FormatterError('CORRECTION_FILE_MISSING',str(source))
        before=fingerprint(source)
        if expected is not None and before!=expected:
            raise FormatterError('CORRECTION_SNAPSHOT_CHANGED', '旧版校对稿已被修改，请重新选择校对稿')
        directory=Path(job['run_dir'])/'corrections'
        directory.mkdir(exist_ok=True)
        target=directory/(uuid.uuid4().hex+'.docx')
        # A failed/partial copy remains unreferenced for diagnosis; it is never reused.
        with source.open('rb') as reader, target.open('xb') as writer:
            shutil.copyfileobj(reader,writer)
            writer.flush()
            os.fsync(writer.fileno())
        if fingerprint(target)!=before or fingerprint(source)!=before:
            raise FormatterError('CORRECTION_CHANGED_DURING_SNAPSHOT', '校对稿复制期间发生变化，请保存后重新选择')
        return {'path':str(target),'sha256':before,'recorded_at':utc_now()}

    def _select_correction(self,job,corrected_docx,clear_correction):
        previous=job.get('manual_document')
        # Earlier releases retained only a hash of formatted.docx. Recover it before
        # that working file is overwritten, and only when the recorded digest matches.
        if previous and not previous.get('path'):
            legacy=job.get('editable_docx')
            if not legacy:
                if not clear_correction and not corrected_docx:
                    raise FormatterError('CORRECTION_FILE_MISSING','旧版任务缺少校对稿路径')
            elif (not clear_correction and not corrected_docx) or (Path(legacy).is_file() and fingerprint(legacy)==previous.get('sha256')):
                previous={**self._snapshot_correction(job,legacy,previous['sha256']),
                          'recorded_at':previous.get('recorded_at',utc_now()),'migrated_at':utc_now()}
                job['manual_document']=previous
                self.store.save(job,'manual_document_migrated')
        if clear_correction or corrected_docx:
            # Snapshot the replacement before changing the active pointer. Bad input
            # must leave the prior correction available for the next review.
            selected=self._snapshot_correction(job,corrected_docx) if corrected_docx else None
            if previous:
                job.setdefault('manual_document_history',[]).append({**previous,
                    'archived_at':utc_now(),'reason':'cleared' if clear_correction else 'replaced'})
            if selected:
                job['manual_document']=selected
            else:
                job.pop('manual_document',None)
            self.store.save(job,'manual_document_selected' if selected else 'manual_document_cleared')
        selected=job.get('manual_document')
        if not selected:
            return None
        path=Path(selected['path'])
        if (not path.resolve().is_relative_to((Path(job['run_dir'])/'corrections').resolve())
                or path.suffix.lower()!='.docx' or not path.is_file()):
            raise FormatterError('CORRECTION_SNAPSHOT_INVALID','校对稿快照缺失或路径无效，请重新选择或清除校对稿')
        if fingerprint(path)!=selected['sha256']:
            raise FormatterError('CORRECTION_SNAPSHOT_CHANGED','校对稿快照已被修改，请重新选择或清除校对稿')
        return path

    def _run(self,job,corrected_docx):
        directory=Path(job['run_dir'])
        source=Path(job['original_path'])
        profile=job['profile']
        problems=[issue('UNRESOLVED_RULE',str(x)) for x in profile.get('unresolved',[])]
        problems+=font_issues(profile)
        is_pdf=source.suffix.lower()=='.pdf'
        self.progress('extracting','读取本地原稿与内容快照')
        extraction=None
        embedded={'annotations':[],'issues':[]}
        if is_pdf:
            from .pdf_io import extract_pdf, rebuild_docx, apply_review
            from .pdf_annotations import snapshot
            extraction_path=directory/'parsed'/'extraction.json'
            extraction=read_json(extraction_path) if extraction_path.exists() else extract_pdf(source,directory/'parsed',self.models,self.progress)
            extraction=apply_review(extraction,job.get('reviews',{}))
            write_json(directory/'extraction-reviewed.json',extraction)
            problems+=extraction['issues']
            original_docx=directory/'reconstructed.docx'
            rebuild_docx(extraction,original_docx)
            original_pdf=str(source)
            embedded=snapshot(source)
            problems+=embedded['issues']
        else:
            original_docx=source
            original_pdf=render_docx(source,directory/'original-render')['pdf']
        job['citation_source_path']=str(original_docx)
        if job.get('citation_mapping'):
            from .citation_mapping import seed_citations
            seeded=directory/'citation-seeded.docx'
            seed_citations(original_docx,seeded,job['citation_mapping'])
            original_docx=seeded
        self.progress('formatting','应用经过验证的格式规则')
        output_docx=directory/'formatted.docx'
        if corrected_docx:
            if Path(corrected_docx).suffix.lower()!='.docx':
                raise FormatterError('CORRECTION_DOCX_REQUIRED')
            # Derive the only permitted content/field changes through the same local pipeline.
            expected_path = directory / 'expected-correction.docx'
            expected_report = format_docx(original_docx, expected_path, profile, citation_updates=job.get('citation_updates'))
            if not expected_path.exists():
                raise FormatterError('UNSAFE_DOCUMENT', str(expected_report['issues']))
            before=expected_report['after']
            shutil.copyfile(corrected_docx,output_docx)
            expected=job['manual_document']['sha256']
            if fingerprint(output_docx)!=expected or fingerprint(corrected_docx)!=expected:
                raise FormatterError('CORRECTION_SNAPSHOT_CHANGED','校对稿快照复制期间发生变化')
            after=inspect_docx(output_docx)
            issues=preservation_issues(before, after)
            issues+=expected_report['issues']
            issues+=after['issues']
            document_report={'passed':not issues,'issues':issues,'before':before,'after':after}
        else:
            document_report=format_docx(original_docx,output_docx,profile,citation_updates=job.get('citation_updates'))
        problems+=document_report['issues']
        if not output_docx.exists():
            raise FormatterError('UNSAFE_DOCUMENT',str(document_report['issues']))
        problems+=requirement_issues(document_report['after']['text'],profile)
        problems+=applied_rule_issues(output_docx,profile)
        self.progress('rendering','用本机 LibreOffice 渲染并检查每一页')
        rendered=render_docx(output_docx,directory/'rendered')['pdf']
        output_view=inspect_render(rendered,directory/'preview-new')
        original_view=inspect_render(original_pdf,directory/'preview-original')
        layout=preview_layout_digest(output_view)
        invalidate_manual_layout(job,layout)
        job.update(preview_layout_sha256=layout,preview_original=original_view,preview_new=output_view)
        # Persist the new preview binding before migration, which may fail. A retry
        # must never display new pixels using the old preview's coordinate token.
        self.store.save(job,'preview_layout_bound')
        problems+=output_view['issues']
        problems+=render_content_issues(document_report['after'],output_view)
        # Scientific object invariants were checked before rendering; visual regions stay immutable.
        self.progress('annotations','迁移批注并验证每个批注的内容与位置')
        migration=migrate_annotations(job['annotations_original'],original_pdf,rendered,manual=job.get('manual_annotations'))
        required=job.get('manual_relocation_required',{})
        migration=require_manual_relocation(migration,job['annotations_original'],required.get('manual_annotations',[]))
        if not migration['complete']:
            problems.append(issue('ANNOTATION_REVIEW_REQUIRED','所有批注重新定位后才可覆盖',annotations=migration['unresolved']))
        embedded_map=migrate_annotations(embedded['annotations'],original_pdf,rendered,manual=job.get('embedded_manual'))
        embedded_map=require_manual_relocation(embedded_map,embedded['annotations'],required.get('embedded_manual',[]))
        if not embedded_map['complete']:
            problems.append(issue('EMBEDDED_ANNOTATION_REVIEW_REQUIRED','PDF 内嵌批注仍有未定位项',annotations=embedded_map['unresolved']))
        if any(row.get('code')=='MANUAL_LAYOUT_CHANGED' for row in migration['unresolved']+embedded_map['unresolved']):
            problems.append(issue('MANUAL_LAYOUT_CHANGED','检查稿版式已变化或旧定位缺少版式记录，请在最新页面上重新定位标记的批注'))
        candidate=directory/('candidate.pdf' if is_pdf else 'candidate.docx')
        if is_pdf and embedded_map['complete'] and embedded['annotations']:
            from .pdf_annotations import embed
            embed(source,rendered,candidate,embedded['annotations'],embedded_map['annotations'])
        else:
            shutil.copyfile(rendered if is_pdf else output_docx,candidate)
        job.update(extraction_path=str(directory/'extraction-reviewed.json') if extraction else None,
            editable_docx=str(output_docx),preview_original=original_view,preview_new=output_view,
            annotation_mapping=migration,embedded_mapping=embedded_map,embedded_original=embedded['annotations'],
            review_record={'recorded_at':utc_now(),'blocks':job.get('reviews',{}),'annotations':job.get('manual_annotations',{}),
                           'embedded_annotations':job.get('embedded_manual',{})})
        report={'passed':not any(i.get('blocking',True) for i in problems),'issues':problems,
            'automatic_checks':{'document':document_report,'render_engine':'LibreOffice','pages':len(output_view['pages']),
                'text_crosscheck':output_view.get('text_crosscheck',{'engine':'PDFium'}),
                'inspection':output_view['inspection'],'annotations_complete':migration['complete'] and embedded_map['complete']},
            'manual_review':job['review_record'],'format_sources':profile.get('sources',[]),'overrides':profile.get('overrides',[]),
            'limitations':['自动检查覆盖文字、对象完整性和几何异常；出版排版细节仍应查看检查稿。']}
        write_json(directory/'report.json',report)
        report_html(directory/'report.html',report)
        job['report_path']=str(directory/'report.html')
        self.store.save(job,'checks_completed')
        staged=self.store.stage(job['run_id'],candidate,report,migration['annotations'])
        self.progress(staged['status'],'检查通过' if report['passed'] else '需要本地校对')
        return public_job(staged)


def public_job(job):
    """Only local IPC; trim large glyph arrays to keep UI responsive."""
    result=copy.deepcopy(job)
    for key in ('preview_original','preview_new'):
        if key in result:
            for page in result[key]['pages']:
                page.pop('chars',None)
    if result.get('extraction_path') and Path(result['extraction_path']).is_file():
        result['extraction']=read_json(result['extraction_path'])
    return result


def report_html(path,report):
    issues=''.join('<li><strong>'+html.escape(i['code'])+'</strong> '+html.escape(i['detail'])+'</li>' for i in report['issues'])
    payload=html.escape(__import__('json').dumps(report,ensure_ascii=False,indent=2))
    Path(path).write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'"><title>论文格式检查报告</title><style>body{font:16px system-ui;max-width:1000px;margin:40px auto;padding:20px;color:#17302d}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f2f5f4;padding:20px}li{margin:12px 0}</style><h1>论文格式检查报告</h1><p>'+('检查通过，可覆盖' if report['passed'] else '存在待处理项，尚未覆盖')+'</p><ul>'+issues+'</ul><details><summary>完整检查与校对记录</summary><pre>'+payload+'</pre></details></html>',encoding='utf-8')

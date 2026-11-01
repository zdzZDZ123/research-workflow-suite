"""Preserve embedded PDF markup separately from Zotero database annotations."""
from __future__ import annotations
import hashlib
from pathlib import Path
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, FloatObject, NameObject, NumberObject
from .contracts import FormatterError, issue
from .annotations import selected_text
from .render import pdf_pages

KINDS={'/Highlight':'highlight','/Underline':'underline','/Text':'note','/FreeText':'text','/Square':'image','/Ink':'ink'}


def active_pdf_issue(root):
    """An internal page destination is navigation, not an active PDF action."""
    names=root.get('/Names') or {}
    names=names.get_object() if hasattr(names,'get_object') else names
    if '/AA' in root or '/JavaScript' in names:
        return True
    form=root.get('/AcroForm')
    if form:
        form=form.get_object() if hasattr(form,'get_object') else form
        if form.get('/Fields') or '/XFA' in form:
            return True
    action=root.get('/OpenAction')
    if action is None:
        return False
    action=action.get_object() if hasattr(action,'get_object') else action
    if isinstance(action,ArrayObject):
        return False
    return not (isinstance(action,dict) and str(action.get('/S'))=='/GoTo')


def snapshot(path):
    reader=PdfReader(path)
    if reader.is_encrypted:
        raise FormatterError('ENCRYPTED_PDF')
    problems=[]
    root=reader.trailer['/Root']
    if active_pdf_issue(root):
        problems.append(issue('ACTIVE_PDF','PDF 含脚本、表单或自动动作，请先导出静态 PDF'))
    records=[]
    link_count=0
    link_pages=set()
    pages=pdf_pages(path,chars=True)
    for pi,page in enumerate(reader.pages):
        for index,ref in enumerate(page.get('/Annots',[])):
            a=ref.get_object()
            typ=str(a.get('/Subtype',''))
            if typ=='/Link':
                # Hyperlinks need a new text anchor too; otherwise preserving a stale rectangle is misleading.
                link_count+=1
                link_pages.add(pi)
                continue
            if typ=='/Popup':
                continue
            if typ not in KINDS or '/AA' in a or '/A' in a:
                problems.append(issue('PDF_MARKUP_UNSUPPORTED','无法安全重建的 PDF 对象：'+typ,page=pi))
                continue
            key=hashlib.sha256(f'{pi}:{index}'.encode()).hexdigest()[:8].upper()
            rect=list(map(float,a.get('/Rect',[0,0,1,1])))
            rects=[rect]
            quad=a.get('/QuadPoints',[])
            if quad:
                rects=[]
                for i in range(0,len(quad),8):
                    pts=list(map(float,quad[i:i+8]))
                    rects.append([min(pts[::2]),min(pts[1::2]),max(pts[::2]),max(pts[1::2])])
            position={'pageIndex':pi,'rects':rects}
            if typ=='/Ink':
                position['paths']=[list(map(float,p)) for p in a.get('/InkList',[])]
                position['width']=float(a.get('/BS',{}).get('/W',1))
            records.append({'key':key,'type':KINDS[typ],
                'text':selected_text(pages[pi],rects) if typ in ('/Highlight','/Underline') else '',
                'comment':str(a.get('/Contents','')), 'authorName':str(a.get('/T','')),
                'color':str(list(a.get('/C',[]))), 'tags':[], 'position':position,
                'pageLabel':str(pi+1),'sortIndex':f'{pi:05}|{index:06}|00000',
                'source_page':pi,'source_index':index})
    if link_count:
        problems.append(issue('PDF_LINK_REVIEW',f'PDF 含 {link_count} 处链接区域；重排前须迁移或在源文件中移除',
                              count=link_count,pages=sorted(link_pages)))
    return {'annotations':records,'issues':problems}


def embed(original, rendered, output, snapshots, mapped):
    """Clone original metadata; geometry is rebuilt and stale appearance streams discarded."""
    src=PdfReader(original)
    writer=PdfWriter(clone_from=rendered)
    lookup={a['key']:a for a in snapshots}
    if set(lookup)!={a['key'] for a in mapped}:
        raise FormatterError('EMBEDDED_ANNOTATION_SET_CHANGED')
    for item in mapped:
        old=lookup[item['key']]
        original_annotation=src.pages[old['source_page']]['/Annots'][old['source_index']].get_object()
        cloned=original_annotation.clone(writer,ignore_fields=('/P','/AP','/Popup','/IRT'))
        for k in ('/P','/AP','/Popup','/IRT'):
            cloned.pop(k,None)
        pos=item['position']
        rects=pos.get('rects',[])
        if item['type']=='ink':
            paths=pos['paths']
            points=[v for p in paths for v in p]
            rects=[[min(points[::2]),min(points[1::2]),max(points[::2]),max(points[1::2])]]
            cloned[NameObject('/InkList')]=ArrayObject([ArrayObject([FloatObject(v) for v in p]) for p in paths])
        rect=[min(r[0] for r in rects),min(r[1] for r in rects),max(r[2] for r in rects),max(r[3] for r in rects)]
        cloned[NameObject('/Rect')]=ArrayObject([FloatObject(v) for v in rect])
        if item['type'] in ('highlight','underline'):
            quad=[v for x0,y0,x1,y1 in rects for v in (x0,y1,x1,y1,x0,y0,x1,y0)]
            cloned[NameObject('/QuadPoints')]=ArrayObject([FloatObject(v) for v in quad])
        writer.add_annotation(pos['pageIndex'],cloned)
    with Path(output).open('wb') as f:
        writer.write(f)
    # Read back all embedded markup and check count, comments and author metadata.
    readback=snapshot(output)
    expected=sorted((a['type'],a['comment'],a.get('authorName','')) for a in mapped)
    actual=sorted((a['type'],a['comment'],a.get('authorName','')) for a in readback['annotations'])
    if expected!=actual:
        raise FormatterError('EMBEDDED_ANNOTATION_READBACK')


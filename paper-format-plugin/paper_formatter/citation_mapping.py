"""Human-supplied local reference mapping; never infer or fabricate citation metadata."""
from __future__ import annotations
import copy
import json
import re
from pathlib import Path
from lxml import etree
from .contracts import FormatterError,digest
from .docx_io import read_package,write_package,xml,serialize,q,NS,fields,citation_data,inspect_docx


def citation_context(path):
    root=xml(read_package(path)['word/document.xml'])
    return [{'paragraph':i,'text':''.join(p.xpath('.//w:t/text()',namespaces=NS))} for i,p in enumerate(root.findall('.//w:p',NS))]


def marker(kind,text=None):
    run=etree.Element(q('r'))
    if kind=='instruction':
        node=etree.SubElement(run,q('instrText'));node.text=text
        node.set('{http://www.w3.org/XML/1998/namespace}space','preserve')
    else:etree.SubElement(run,q('fldChar')).set(q('fldCharType'),kind)
    return run


def seed_citations(source,target,mapping):
    if mapping.get('confirmed') is not True:raise FormatterError('CITATION_MAPPING_CONFIRMATION_REQUIRED')
    parts=read_package(source);root=xml(parts['word/document.xml'])
    paragraphs=root.findall('.//w:p',NS)
    before=[''.join(p.xpath('.//w:t/text()',namespaces=NS)) for p in paragraphs]
    if any(citation_data(f) for f in fields(root)):raise FormatterError('ALREADY_STRUCTURED_CITATIONS')
    clusters=[]
    bib_indices=mapping.get('bibliography_paragraphs',[])
    if len(set(bib_indices))!=len(bib_indices) or any(not isinstance(i,int) or not 0<=i<len(paragraphs) for i in bib_indices):
        raise FormatterError('BIBLIOGRAPHY_PARAGRAPHS_INVALID')
    for i,entry in enumerate(mapping.get('citations',[])):
        idx=entry.get('paragraph');quote=entry.get('quote')
        if not isinstance(idx,int) or not 0<=idx<len(paragraphs) or idx in bib_indices or not isinstance(quote,str) or not quote:
            raise FormatterError('CITATION_PARAGRAPH_INVALID')
        p=paragraphs[idx];text=''.join(p.xpath('.//w:t/text()',namespaces=NS))
        hits=[m.start() for m in re.finditer(re.escape(quote),text)]
        occurrence=entry.get('occurrence')
        if occurrence is None and len(hits)!=1:raise FormatterError('CITATION_QUOTE_AMBIGUOUS')
        at=(hits[occurrence-1] if isinstance(occurrence,int) and 1<=occurrence<=len(hits) else hits[0] if len(hits)==1 else None)
        if at is None:raise FormatterError('CITATION_QUOTE_AMBIGUOUS')
        items=entry.get('items',[])
        if not items or any(not x.get('id') or not x.get('itemData',{}).get('title') or not x.get('itemData',{}).get('type') for x in items):
            raise FormatterError('CITATION_METADATA_REQUIRED')
        offset=0;chosen=None
        already={n for f in fields(root) for n in f['results']}
        for node in p.findall('.//w:t',NS):
            length=len(node.text or '')
            if offset<=at and at+len(quote)<=offset+length and node not in already:
                chosen=node;start=at-offset;break
            offset+=length
        if chosen is None:raise FormatterError('CITATION_SPANS_RUNS','请先在校对稿中合并该引文的文字片段')
        run=chosen.getparent()
        if run.tag!=q('r') or run.getparent() is not p or any(n.tag not in (q('rPr'),q('t')) for n in run) or len(run.findall(q('t')))!=1:
            raise FormatterError('CITATION_RUN_STRUCTURE')
        old=chosen.text or '';before_run=copy.deepcopy(run);before_run.find(q('t')).text=old[:start]
        result_run=copy.deepcopy(run);result_run.find(q('t')).text=quote
        after_run=copy.deepcopy(run);after_run.find(q('t')).text=old[start+len(quote):]
        cid='paperformat-'+digest({'index':i,'paragraph':idx,'quote':quote})[:16]
        cluster={'citationID':cid,'citationItems':items,'properties':{'noteIndex':0,'plainCitation':quote}}
        nodes=[before_run,marker('begin'),marker('instruction',' ADDIN ZOTERO_ITEM CSL_CITATION '+json.dumps(cluster,ensure_ascii=False)),marker('separate'),result_run,marker('end'),after_run]
        index=list(p).index(run);p.remove(run)
        for node in reversed(nodes):p.insert(index,node)
        clusters.append(cluster)
    if not clusters:raise FormatterError('CITATION_MAPPING_EMPTY')
    if bib_indices:
        chosen=[paragraphs[i] for i in sorted(bib_indices)]
        parent=chosen[0].getparent()
        if any(p.getparent() is not parent for p in chosen):raise FormatterError('BIBLIOGRAPHY_STRUCTURE')
        indices=[list(parent).index(p) for p in chosen]
        if indices!=list(range(indices[0],indices[-1]+1)):raise FormatterError('BIBLIOGRAPHY_NOT_CONTIGUOUS')
        first=chosen[0];offset=1 if len(first) and first[0].tag==q('pPr') else 0
        for node in reversed([marker('begin'),marker('instruction',' ADDIN ZOTERO_BIBL {} '),marker('separate')]):first.insert(offset,node)
        chosen[-1].append(marker('end'))
    after=[''.join(p.xpath('.//w:t/text()',namespaces=NS)) for p in paragraphs]
    if before!=after:raise FormatterError('CITATION_SEED_TEXT_CHANGED')
    parts['word/document.xml']=serialize(root);write_package(target,parts)
    return {'citations':inspect_docx(target)['citations'],'seeded_path':str(Path(target).resolve())}

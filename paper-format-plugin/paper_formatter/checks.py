"""Checks that cannot be waived by a client supplied success flag."""
from __future__ import annotations
import os
import posixpath
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path
from .contracts import issue
from .annotations import normalized_text


@lru_cache(maxsize=1)
def installed_fonts():
    from fontTools.ttLib import TTFont, TTCollection
    names=set()
    dirs=[Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts',Path(os.environ.get('LOCALAPPDATA','.'))/'Microsoft/Windows/Fonts']
    for directory in dirs:
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            if path.suffix.lower() not in ('.ttf','.otf','.ttc'):
                continue
            fonts=[]
            try:
                if path.suffix.lower()=='.ttc':
                    collection=TTCollection(path,lazy=True)
                    fonts=collection.fonts
                else:
                    fonts=[TTFont(path,lazy=True)]
                for font in fonts:
                    for name in font['name'].names:
                        if name.nameID in (1,4,6,16):
                            try:names.add(name.toUnicode().strip().casefold())
                            except Exception:pass
            except Exception:pass
            finally:
                for font in fonts:font.close()
    return names


def font_issues(profile):
    requested=set()
    def walk(obj):
        if isinstance(obj,dict):
            for k,v in obj.items():
                if k in ('font','latin_font'):requested.add(v)
                elif isinstance(v,dict):walk(v)
    walk(profile['rules'])
    available=installed_fonts() if requested else set()
    return [issue('FONT_NOT_INSTALLED','指定字体未安装：'+font,font=font) for font in sorted(requested) if font.casefold() not in available]


def render_content_issues(docx_info, render_info):
    # Require all scientific text glyphs to survive rendering, allowing new page numbers and citation labels.
    expected=Counter(normalized_text(docx_info['text']))
    actual=Counter(normalized_text(render_info['text']))
    missing=expected-actual
    if missing:
        # PDFium can return U+FFFE for a visible glyph in some embedded fonts.
        # Permit an independent parser to verify the *entire* expected character
        # multiset only when the number of unknown glyphs can explain the gap.
        unknown=sum(page.get('text','').count('\ufffe') for page in render_info.get('pages',[]))
        if unknown>=sum(missing.values()) and render_info.get('pdf'):
            try:
                from pypdf import PdfReader
                second=''.join(page.extract_text() or '' for page in PdfReader(render_info['pdf']).pages)
                if not (expected-Counter(normalized_text(second))):
                    render_info['text_crosscheck']={'engine':'pypdf','reason':'pdfium_unknown_glyph',
                        'unknown_glyph_pages':[page.get('index',index) for index,page in enumerate(render_info['pages'])
                                               if '\ufffe' in page.get('text','')]}
                    return []
            except Exception:
                # A failed independent read is not evidence of visible content.
                pass
        return [issue('RENDER_TEXT_MISSING','渲染稿缺少正文字符，可能存在字体替换、遮挡或内容未显示',
                      missing_count=sum(missing.values()),unknown_glyph_count=unknown)]
    return []


def requirement_issues(text,profile):
    limits=profile['rules'].get('limits',{})
    result=[]
    words=len(re.findall(r'[\u4e00-\u9fff]|\b[\w-]+\b',text))
    if words>limits.get('max_words',float('inf')):
        result.append(issue('WORD_LIMIT',f'当前约 {words} 字/词，超过要求 {limits["max_words"]}；请在原稿中处理后重试'))
    for section in limits.get('required_sections',[]):
        if section.casefold() not in text.casefold():
            result.append(issue('MISSING_SECTION','缺少要求的内容：'+section))
    return result


def applied_rule_issues(path,profile):
    from .docx_io import read_package,xml,q,NS,R,fields,bibliography_paragraphs,reference_rule,usable_width
    parts=read_package(path);root=xml(parts['word/document.xml']);rules=profile['rules'];problems=[]
    def fail(path):
        if not any(i.get('rule')==path for i in problems):
            problems.append(issue('FORMAT_RULE_NOT_APPLIED','校对稿未满足格式要求：'+path,rule=path))
    def on_off(node):
        if node is None:return None
        return node.get(q('val'),'1') not in ('0','false','off')
    def paragraph_rule(p,rule,role):
        font_attrs={}
        if 'font' in rule:font_attrs.update(eastAsia=rule['font'],ascii=rule.get('latin_font',rule['font']),hAnsi=rule.get('latin_font',rule['font']))
        if 'latin_font' in rule:font_attrs.update(ascii=rule['latin_font'],hAnsi=rule['latin_font'])
        for run in p.findall('.//w:r',NS):
            if run.find(q('t')) is None:continue
            if 'size_pt' in rule:
                for tag in ('sz','szCs'):
                    prop=run.find('w:rPr/w:'+tag,NS)
                    if prop is None or prop.get(q('val'))!=str(round(rule['size_pt']*2)):fail(role+'.size_pt')
            prop=run.find('w:rPr/w:rFonts',NS)
            for attr,value in font_attrs.items():
                key='font' if attr=='eastAsia' or 'latin_font' not in rule else 'latin_font'
                if prop is None or prop.get(q(attr))!=value or prop.get(q(attr+'Theme')):fail(role+'.'+key)
            for key,tag in (('bold','b'),('italic','i')):
                if key in rule and on_off(run.find('w:rPr/w:'+tag,NS)) is not rule[key]:fail(role+'.'+key)
        if 'align' in rule:
            node=p.find('w:pPr/w:jc',NS);expected='both' if rule['align']=='justify' else rule['align']
            if node is None or node.get(q('val'))!=expected:fail(role+'.align')
        for key,attr,scale in [('line_spacing','line',240),('line_spacing_pt','line',20),('space_before_pt','before',20),('space_after_pt','after',20)]:
            if key in rule:
                node=p.find('w:pPr/w:spacing',NS)
                if node is None or node.get(q(attr))!=str(round(rule[key]*scale)):fail(role+'.'+key)
                if key in ('line_spacing','line_spacing_pt') and (node is None or node.get(q('lineRule'),'auto')!=('auto' if key=='line_spacing' else 'exact')):fail(role+'.'+key)
        if 'first_line_mm' in rule:
            node=p.find('w:pPr/w:ind',NS);positive=rule['first_line_mm']>=0
            attr='firstLine' if positive else 'hanging';other='hanging' if positive else 'firstLine'
            if (node is None or node.get(q(attr))!=str(round(abs(rule['first_line_mm'])*1440/25.4))
                    or any(node.get(q(k)) is not None for k in (other,'firstLineChars','hangingChars'))):fail(role+'.first_line_mm')
        if 'keep_with_next' in rule and on_off(p.find('w:pPr/w:keepNext',NS)) is not rule['keep_with_next']:fail(role+'.keep_with_next')
    geometry=rules.get('page',{})
    for section in root.findall('.//w:sectPr',NS):
        if usable_width(section)<=0:fail('page.usable_column_width')
        for field,tag,attr,scale in (('width_mm','pgSz','w',1440/25.4),('height_mm','pgSz','h',1440/25.4),('columns','cols','num',1),('column_gap_mm','cols','space',1440/25.4)):
            if field in geometry:
                node=section.find(q(tag))
                if node is None or node.get(q(attr))!=str(round(geometry[field]*scale)):fail('page.'+field)
        for side,value in geometry.get('margins_mm',{}).items():
            node=section.find(q('pgMar'))
            if node is None or node.get(q(side))!=str(round(value*1440/25.4)):fail('page.margins_mm.'+side)
        if 'line_numbers' in geometry and (section.find(q('lnNumType')) is not None)!=geometry['line_numbers']:fail('page.line_numbers')
    headings={}
    for style in xml(parts['word/styles.xml']).findall(q('style')):
        name=style.find(q('name'))
        match=re.fullmatch(r'(?:heading|标题)\s*([1-6])',name.get(q('val'),'') if name is not None else '',re.I)
        if match:headings[style.get(q('styleId'))]=match[1]
    references=False;reference_level=1
    bibliography=bibliography_paragraphs(root)
    for index,p in enumerate(root.findall('.//w:p',NS)):
        text=''.join(p.xpath('.//w:t/text()',namespaces=NS));sid=p.find('w:pPr/w:pStyle',NS)
        sid=sid.get(q('val'),'') if sid is not None else ''
        is_reference_heading=text.strip().casefold() in ('references','bibliography','参考文献')
        if is_reference_heading:
            references=True;reference_level=int(headings.get(sid,1))
        elif sid in headings and int(headings[sid])<=reference_level:
            references=False
        if sid in headings:rule=rules.get('headings',{}).get(headings[sid],{});role='headings.'+headings[sid]
        elif (match:=re.match(r'^\s*(Figure|Fig\.?|Table|图|表)\s*\d+',text,re.I)):
            kind='table' if match[1].lower() in ('table','表') else 'figure';rule=rules.get('captions',{}).get(kind,{});role='captions.'+kind
        elif references or p in bibliography:rule=reference_rule(rules);role='references'
        elif sid.lower() in ('title','subtitle'):continue
        else:rule=rules.get('body',{});role='body'
        paragraph_rule(p,rule,role)
    rels=xml(parts['word/_rels/document.xml.rels']) if 'word/_rels/document.xml.rels' in parts else []
    for kind in ('header','footer'):
        if kind not in rules:continue
        rule=rules[kind];seen=set();inherited_default=False
        for section in root.findall('.//w:sectPr',NS):
            refs=section.findall(q(kind+'Reference'))
            inherited_default|=any(n.get(q('type'))=='default' for n in refs)
            if not inherited_default:fail(kind+'.reference')
            for reference in refs:
                rel=next((n for n in rels if n.get('Id')==reference.get('{'+R+'}id')),None)
                target=rel.get('Target','') if rel is not None else ''
                name=posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'word/'+target)
                if not target or name not in parts or not name.startswith('word/'):
                    fail(kind+'.reference');continue
                if name in seen:continue
                seen.add(name);story=xml(parts[name]);complex_fields=fields(story)
                simple_fields=story.findall('.//w:fldSimple',NS)
                page_fields=[f for f in complex_fields if re.match(r'^\s*PAGE\b',f['instruction'])]
                page_fields += [f for f in simple_fields if re.match(r'^\s*PAGE\b',f.get(q('instr'),''))]
                if 'page_number' in rule and bool(page_fields)!=rule['page_number']:fail(kind+'.page_number')
                if 'text' in rule:
                    field_text={t for f in complex_fields for t in f['results']}
                    field_text.update(t for f in simple_fields for t in f.findall('.//w:t',NS))
                    prose=''.join(t.text or '' for t in story.findall('.//w:t',NS) if t not in field_text)
                    if prose!=rule['text']:fail(kind+'.text')
                for p in story.findall('.//w:p',NS):paragraph_rule(p,{k:v for k,v in rule.items() if k in ('size_pt','align')},kind)
    return problems

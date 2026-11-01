"""Mechanical caption labels/numbers and matching textual cross-references."""
import re
from collections import Counter
from .contracts import issue

PATTERN=re.compile(r'(?P<label>Figure|Fig\.?|Table|图|表)\s*(?P<number>\d+)(?!\d)',re.I)


def replace_span(paragraph,start,end,replacement,ns):
    offset=0;placed=False
    for node in paragraph.findall('.//w:t',ns):
        text=node.text or '';a,b=offset,offset+len(text);offset=b
        if b<=start or a>=end:continue
        left=max(0,start-a);right=min(len(text),end-a)
        node.text=text[:left]+(replacement if not placed else '')+text[right:]
        placed=True


def update_captions(root,rules,ns,q):
    paragraphs=root.findall('.//w:p',ns)
    caps=[];counts=Counter();problems=[];changes=[];mapping={}
    for p in paragraphs:
        text=''.join(p.xpath('.//w:t/text()',namespaces=ns))
        match=PATTERN.match(text.strip())
        if match:
            kind='table' if match['label'].lower() in ('table','表') else 'figure'
            counts[(kind,match['number'])]+=1
            caps.append((p,kind,match))
    sequence=Counter()
    for p,kind,match in caps:
        rule=rules.get(kind,{})
        if 'label' not in rule and 'numbered' not in rule:continue
        if counts[(kind,match['number'])]!=1:
            problems.append(issue('CAPTION_NUMBER_AMBIGUOUS','图表编号重复，无法唯一更新正文交叉引用',kind=kind,number=match['number']));continue
        sequence[kind]+=1
        number=str(sequence[kind]) if rule.get('numbered') else match['number']
        if rule.get('numbered') is False:
            problems.append(issue('CAPTION_UNNUMBERED_REFERENCES','去除编号会使交叉引用失去定位依据；请先在源文档中处理相关引用'));continue
        if number!=match['number'] and root.findall('.//w:instrText',ns):
            relevant=[n.text or '' for n in root.findall('.//w:instrText',ns)]
            if any(re.search(r'\b(?:SEQ|REF)\b',value,re.I) for value in relevant):
                problems.append(issue('CAPTION_NATIVE_FIELD_REVIEW','原生图表编号/交叉引用需先在文字处理软件中刷新到目标顺序'));continue
        mapping[(kind,match['number'])]=(rule.get('label',match['label']),number)
    for p in paragraphs:
        text=''.join(p.xpath('.//w:t/text()',namespaces=ns))
        for m in reversed(list(PATTERN.finditer(text))):
            kind='table' if m['label'].lower() in ('table','表') else 'figure'
            replacement=mapping.get((kind,m['number']))
            if replacement:
                label,number=replacement
                replace_span(p,m.start('number'),m.end('number'),number,ns)
                replace_span(p,m.start('label'),m.end('label'),label,ns)
                if (label,number)!=(m['label'],m['number']):
                    changes.append({'before':m.group(),'after':label+' '+number})
    return problems,changes

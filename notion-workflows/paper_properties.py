"""Populate existing paper properties from saved sources; preserve user overrides."""
import json
from pathlib import Path
from paper_reading import rich, SECTIONS, get_json
from visual_report import text, table, callout

# These analyses belong exclusively in the full report body.
BODY_ONLY_PROPERTIES = frozenset({'研究方法', '研究结果', '研究结论'})

def inspect_fulltext(zotero, attachments):
    uncertain=False
    for a in attachments:
        if a['data'].get('contentType')!='application/pdf': continue
        try:
            data=get_json(zotero.base+'/items/'+a['key']+'/fulltext')
            if data.get('content','').strip() and data.get('totalPages',0)>0 and data.get('indexedPages',0)>=data['totalPages']:
                return {'indexedPages':data['indexedPages'],'totalPages':data['totalPages']}
        except Exception: uncertain=True
    return {'unknown':True} if uncertain else None


def desired_properties(paper, payload, schema, folder, attachments):
    d=paper['data']; groups={s:[] for s in SECTIONS}; current=SECTIONS[-1]
    for b in payload['blocks']:
        if b['type']=='heading_1' and text(b) in groups: current=text(b)
        elif text(b) and not text(b).startswith('原报告没有'): groups[current].append(text(b))
    def excerpt(section):
        values=[v for v in groups[section] if len(v)>45]
        return ('已保存精读摘录（未重新核查）：'+ '\n'.join(values))[:1700] if values else '无（已保存笔记未提供）'
    f=Path(folder)/'renderer-profiles'/(payload['sha256']+'.json')
    profile=json.loads(f.read_text(encoding='utf-8')) if f.exists() else {}
    pdfs=[a['data'].get('url') for a in attachments if a['data'].get('contentType')=='application/pdf' and a['data'].get('url','').startswith('https://')]
    values={
        'Journal':d.get('publicationTitle') or '无（Zotero 未提供）',
        '期刊':d.get('publicationTitle') or '无（Zotero 未提供）',
        '中文标题':profile.get('chinese_title','无（已保存笔记未提供独立中文译名）').replace('中文名（沿用原精读译名）：',''),
        '翻译作者':'无（未提供可核实的中文姓名；保留原作者）',
        'Topics':profile.get('cards', [['',excerpt(SECTIONS[0])]])[0][1],
        '研究变量':'\n'.join('；'.join(r[:3]) for r in profile.get('variables',[])[1:]) or excerpt(SECTIONS[3]),
        'Full Text':('待核实' if paper.get('_fulltext',{} ) and paper['_fulltext'].get('unknown') else '已提取' if paper.get('_fulltext') else '未提取'), 'Knowledge Graph':'未开始', 'Open Access':'待确认',
        'JCR分区':'待核实', '中科院分区':'待核实',
        '论文类型':'实验研究' if profile.get('workflow') else '其他',
        'PDF URL':pdfs[0] if len(set(pdfs))==1 else None,
    }
    # A local attachment is not proof of a completed full-text extraction.
    props={}
    for name,value in values.items():
        if name=='Full Text' and value=='待核实': continue
        kind=schema.get(name,{}).get('type')
        if kind=='rich_text': props[name]={'rich_text':rich(value)}
        elif kind=='select': props[name]={'select':{'name':value}}
        elif kind=='url' and value: props[name]={'url':value}
    display=[['属性','来源或当前状态']]
    for name,prop in schema.items():
        if name in BODY_ONLY_PROPERTIES: continue
        if name in values: label=values[name] or '无（未提供有效网址）'
        elif name=='影响因子': label='待核实：缺少指定年份的可靠来源；数字字段保持为空，不填写 0'
        elif name=='Match Score': label='无：尚未定义匹配评分算法；数字字段保持为空，不填写 0'
        elif name=='DOI': label=d.get('DOI') or '无（Zotero 未提供）'
        elif name=='Paper Key': label=f"{paper['library']['type']}:{paper['library']['id']}:{paper['key']}"
        elif name=='原标题': label=d.get('title','无')
        elif name=='原作者': label='；'.join((a.get('firstName','')+' '+a.get('lastName','')).strip() or a.get('name','') for a in d.get('creators',[]) if a.get('creatorType')=='author') or '无'
        else: label='无（当前来源未提供）'
        display.append([name,str(label)])
    note='属性补全与来源状态\n“无”表示当前保存来源没有提供；“待核实”表示信息尚未确认，不能当成不存在。数字字段不使用 0 代替缺失。PDF URL 为 Zotero 保存的附件来源，可能是投稿版；不等同于出版社开放获取状态。Full Text 依据 Zotero 全文索引的实际完成页数判断。下表为来源可提供的值，用户手动维护的页面属性优先。'
    container=callout(note,'📋')
    container['callout']['children']=[{'object':'block','type':'toggle','toggle':{'rich_text':rich('展开全部属性与缺失原因'),'children':[table(display)]}}]
    return props,container


def fill_properties(api, db, identity, target, desired, folder):
    from notion_sync import plain, normalize_id, StopSync
    db.execute('CREATE TABLE IF NOT EXISTS paper_property_values(identity TEXT,name TEXT,value TEXT,PRIMARY KEY(identity,name))')
    page=api.request('GET','pages/'+target)
    updates={}
    for name,wanted in desired.items():
        actual=page['properties'][name]; kind=actual['type']
        def value(p):
            if kind=='rich_text': return ''.join(x.get('plain_text',x.get('text',{}).get('content','')) for x in p.get('rich_text',[]))
            if kind=='select': return (p.get('select') or {}).get('name')
            return p.get(kind)
        old=db.execute('SELECT value FROM paper_property_values WHERE identity=? AND name=?',(identity,name)).fetchone()
        now=value(actual); new=value(wanted)
        if now==new: continue
        if not now or (old and now==json.loads(old[0])): updates[name]=wanted
    if not updates: return
    backup=Path(folder)/'property-backups'; backup.mkdir(exist_ok=True)
    import uuid
    (backup/(str(uuid.uuid4())+'.json')).write_text(json.dumps({'page':page,'desired':updates},ensure_ascii=False,indent=2),encoding='utf-8')
    api.request('PATCH','pages/'+target,{'properties':updates})
    check=api.request('GET','pages/'+target)
    with db:
        for name,wanted in updates.items():
            kind=next(iter(wanted));actual=check['properties'][name]
            if kind=='rich_text': val=plain(actual); expected=''.join(x['text']['content'] for x in wanted[kind])
            elif kind=='select': val=(actual.get(kind) or {}).get('name');expected=wanted[kind]['name']
            else: val=actual.get(kind);expected=wanted[kind]
            if val!=expected: raise StopSync('PROPERTY_READBACK_MISMATCH: '+name)
            db.execute('INSERT OR REPLACE INTO paper_property_values VALUES(?,?,?)',(identity,name,json.dumps(val,ensure_ascii=False)))

"""Explicit Paper Chat selections: append-only, separate from full-report versions."""
import hashlib
import json
import re
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from paper_reading import block, rich
from selection_converter import convert_fragment
from notion_sync import (Store, StopSync, lock, preflight, paper_identity, match_original,
                         find_deep, validate_relation, normalize_id, plain,
                         reconcile_children, snapshot_tree, same_tree, canonical)


def read_selection(path, folder):
    root = (Path(folder) / 'selections').resolve()
    path = Path(path).resolve()
    if path.parent != root or path.suffix != '.json' or not path.is_file():
        raise StopSync('选区请求必须位于本项目的私有 selections 目录')
    if path.stat().st_size > 2_000_000:
        raise StopSync('选中内容过大，请分段同步')
    d = json.loads(path.read_text(encoding='utf-8'))
    if d.get('schema_version') != 1: raise StopSync('不支持的选区请求版本')
    try:
        uuid.UUID(d['request_id'])
        datetime.fromisoformat(d['captured_at'].replace('Z', '+00:00'))
    except (KeyError, ValueError, TypeError): raise StopSync('选区请求标识或时间无效') from None
    for name in ('item_key', 'library_type', 'library_id', 'message_id', 'text', 'html'):
        if not isinstance(d.get(name), str): raise StopSync('选区缺少字段：'+name)
    if not re.fullmatch('[A-Z0-9]{8}', d['item_key']) or d['library_type'] not in ('user','group') or not d['library_id'].isdigit():
        raise StopSync('选区论文身份无效')
    if not d['text'].strip() or not d['message_id'] or len(d['message_id']) > 200:
        raise StopSync('请在同一条 AI 回答中选择非空文字')
    if len(d['text']) > 200000 or len(d['html']) > 1500000:
        raise StopSync('选中内容过大，请分段同步')
    return d


def selection_digest(blocks):
    def normalized(b):
        value=canonical(b)
        if b[b['type']].get('children'):
            value[b['type']]['children']=[normalized(c) for c in b[b['type']]['children']]
        return value
    return hashlib.sha256(json.dumps([normalized(b) for b in blocks], ensure_ascii=False,
                                    sort_keys=True, separators=(',',':')).encode()).hexdigest()


def convert_selection(d):
    try:
        converted = convert_fragment(d['text'], d['html'])
    except ValueError as error:
        raise StopSync(str(error)) from None
    blocks, warnings = converted['blocks'], converted['warnings']
    # Bound the entire converted selection below Notion's 500 KB request limit,
    # including UTF-8 and formatting overhead. Reject in preview, never truncate.
    if len(json.dumps(converted, ensure_ascii=False).encode('utf-8')) > 350000:
        raise StopSync('所选内容的格式或长度超过本次同步容量，请分段选择；原始请求已保存在本地。')
    # Canonical output ignores markup-only wrappers, but preserves meaningful format.
    digest = selection_digest(blocks)
    return {'blocks': blocks, 'hash': digest, 'warnings': warnings}


def preview_selection(d):
    p = convert_selection(d)
    return {'status':'SELECTION_PREVIEW', 'hash':p['hash'], 'warnings':p['warnings'],
            'characters':len(d['text']), 'text':d['text']}


def sync_selection(api, config, paper, d, folder, confirmed_hash):
    # Record every invocation, including validation failures before any remote write.
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    with closing(Store(folder/'state.sqlite3').db) as audit:
        audit.execute('CREATE TABLE IF NOT EXISTS selection_runs(id INTEGER PRIMARY KEY AUTOINCREMENT,request_id TEXT,started TEXT,finished TEXT,status TEXT,error TEXT)')
        with audit:
            run=audit.execute('INSERT INTO selection_runs(request_id,started,status) VALUES(?,?,?)',
                (d.get('request_id'),datetime.now(timezone.utc).isoformat(),'running')).lastrowid
        try:
            result=_sync_selection(api,config,paper,d,folder,confirmed_hash)
        except Exception as error:
            with audit:audit.execute('UPDATE selection_runs SET finished=?,status=?,error=? WHERE id=?',
                (datetime.now(timezone.utc).isoformat(),'paused',str(error),run))
            raise
        with audit:audit.execute('UPDATE selection_runs SET finished=?,status=? WHERE id=?',
            (datetime.now(timezone.utc).isoformat(),result['status'],run))
        return result


def _sync_selection(api, config, paper, d, folder, confirmed_hash):
    payload = convert_selection(d)
    if payload['hash'] != confirmed_hash: raise StopSync('预览内容已变化，请重新预览后同步')
    if (paper['key'] != d['item_key'] or paper['library']['type'] != d['library_type']
            or str(paper['library']['id']) != d['library_id']):
        raise StopSync('选区论文身份与 Zotero 当前返回信息不一致')
    folder = Path(folder)
    with lock(folder), closing(Store(folder/'state.sqlite3').db) as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS selection_collections(identity TEXT PRIMARY KEY, page_id TEXT, data TEXT);
        CREATE TABLE IF NOT EXISTS selection_entries(request_id TEXT PRIMARY KEY, identity TEXT,
            hash TEXT, state TEXT, data TEXT, error TEXT, UNIQUE(identity,hash));
        CREATE TABLE IF NOT EXISTS selection_events(id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT, time TEXT, state TEXT, error TEXT);
        ''')
        identity = paper_identity(paper)
        schema = preflight(api, config)
        original = match_original(api, schema['original_source'], paper)
        mapping = db.execute('SELECT * FROM mappings WHERE identity=?',(identity,)).fetchone()
        index = find_deep(api, schema['source']['id'], paper)
        if not mapping or not index: raise StopSync('请先为该论文完成一次整篇同步，建立映射后再同步精选内容')
        if normalize_id(mapping['page_id']) != normalize_id(index['id']) or normalize_id(mapping['original_page']) != normalize_id(original):
            raise StopSync('精选同步映射冲突，未写入')
        validate_relation(api, api.request('GET','pages/'+index['id']), original, paper, schema['original_source']['id'])
        reused = db.execute('SELECT * FROM selection_entries WHERE request_id=?',(d['request_id'],)).fetchone()
        if reused and (reused['identity'] != identity or selection_digest(json.loads(reused['data'])['blocks']) != payload['hash']):
            raise StopSync('选区请求 ID 已对应其他内容，未写入')
        row = db.execute('SELECT * FROM selection_entries WHERE identity=? AND hash=?',(identity,payload['hash'])).fetchone()
        if not row:
            # Adopt earlier canonicalization without duplicating already written content.
            compatible=[r for r in db.execute('SELECT * FROM selection_entries WHERE identity=?',(identity,))
                        if selection_digest(json.loads(r['data'])['blocks'])==payload['hash']]
            if len(compatible)>1: raise StopSync('已有多个等价片段记录，请核对后恢复')
            row=compatible[0] if compatible else None
        a = json.loads(row['data']) if row else {'request_id':d['request_id'],'identity':identity,
            'hash':payload['hash'],'state':'prepared','page_id':original,'source':d,
            'blocks':payload['blocks'],'warnings':payload['warnings']}
        if a['page_id'] != original: raise StopSync('精选内容目标页面已变化')
        a['hash']=payload['hash']
        def save(error=None):
            a['updated_at']=datetime.now(timezone.utc).isoformat()
            with db:
                db.execute('INSERT OR REPLACE INTO selection_entries VALUES(?,?,?,?,?,?)',
                    (a['request_id'],identity,a['hash'],a['state'],json.dumps(a,ensure_ascii=False),error))
                db.execute('INSERT INTO selection_events(request_id,time,state,error) VALUES(?,?,?,?)',
                    (a['request_id'],a['updated_at'],a['state'],error))
        save()
        dest = folder/'selection-backups'/a['request_id']; dest.mkdir(parents=True,exist_ok=True)
        backup = dest/'source.json'
        if not backup.exists(): backup.write_text(json.dumps(a,ensure_ascii=False,indent=2),encoding='utf-8')
        try:
            parents = api.children(original)
            row = db.execute('SELECT data FROM selection_collections WHERE identity=?',(identity,)).fetchone()
            collection = json.loads(row[0]) if row else {
                'marker':'Paper Chat 精选内容 · 建立中 '+str(uuid.uuid4()),'state':'prepared'}
            def save_collection():
                with db: db.execute('INSERT OR REPLACE INTO selection_collections VALUES(?,?,?)',
                    (identity,original,json.dumps(collection,ensure_ascii=False)))
            if collection.get('id'):
                found = [b for b in parents if b['id']==collection['id']]
                if not found: raise StopSync('精选区域已被删除或移动；为保护人工修改，不自动重建')
            else:
                if collection['state']=='creating' and collection['marker']=='Paper Chat 精选内容':
                    raise StopSync('旧版精选区域创建结果不明确，缺少唯一标识；请人工核对恢复')
                if collection['state']=='prepared' and any(b['type']=='heading_1' and plain({'type':'rich_text','rich_text':b['heading_1']['rich_text']})=='Paper Chat 精选内容' for b in parents):
                    raise StopSync('已有同名用户区域，不能自动接管')
                found = [b for b in parents if b['type']=='heading_1' and plain({'type':'rich_text','rich_text':b['heading_1']['rich_text']})==collection['marker']]
                if len(found)>1: raise StopSync('存在多个精选区域，请人工核对')
                if found and collection['state'] != 'creating': raise StopSync('已有同名用户区域，不能自动接管')
                if not found:
                    if collection['state']=='creating': raise StopSync('精选区域创建结果不明确，请核对后恢复；未重复创建')
                    # Capture the whole page before the first mutation in this request.
                    (dest/'page-before.json').write_text(json.dumps(snapshot_tree(api,original),ensure_ascii=False,indent=2),encoding='utf-8')
                    personal = next((i for i,b in enumerate(parents) if b['type']=='heading_1' and plain({'type':'rich_text','rich_text':b['heading_1']['rich_text']})=='用户个人补充笔记'),None)
                    if personal == 0: raise StopSync('个人笔记位于页面首部，无法安全插入精选区')
                    heading=block('heading_1',rich(collection['marker']))
                    heading['heading_1']['is_toggleable']=True
                    body={'children':[heading]}
                    if personal is not None: body['after']=parents[personal-1]['id']
                    collection['state']='creating'; save_collection()
                    try: found=api.request('PATCH','blocks/'+original+'/children',body)['results']
                    except StopSync as error:
                        if re.match(r'NOTION_(NOT_SENT|HTTP_(400|401|403|404|409|413|429)):',str(error)):
                            collection['state']='prepared';save_collection()
                        raise
                collection.update(id=found[0]['id'],state='ready');save_collection()
            if found[0]['type']=='heading_1' and plain({'type':'rich_text','rich_text':found[0]['heading_1']['rich_text']})==collection['marker'] and collection['marker']!='Paper Chat 精选内容':
                api.request('PATCH','blocks/'+collection['id'],{'heading_1':{'rich_text':rich('Paper Chat 精选内容'),'is_toggleable':True}})
            roots = api.children(collection['id'])
            if a['state']=='committed':
                if not any(b['id']==a['container'] for b in roots): raise StopSync('该精选片段已被删除或移动；不自动重建')
                return {'status':'SELECTION_UNCHANGED','url':'https://www.notion.so/'+normalize_id(original)}
            marker='Paper Chat 片段 · '+a['source']['captured_at']+' · '+a['request_id']
            pending=marker+' · 同步中'; complete=marker+' · 已同步'
            found=[b for b in roots if b['type']=='paragraph' and plain({'type':'rich_text','rich_text':b['paragraph']['rich_text']}) in (pending,complete)]
            if len(found)>1: raise StopSync('重复片段容器冲突')
            if a.get('container') and not any(b['id']==a['container'] for b in found): raise StopSync('片段容器已被人工修改或移动')
            if not found:
                if a['state']=='creating': raise StopSync('片段创建结果不明确，请核对后恢复；未重复创建')
                if not (dest/'page-before.json').exists():
                    (dest/'page-before.json').write_text(json.dumps(snapshot_tree(api,original),ensure_ascii=False,indent=2),encoding='utf-8')
                a['state']='creating';save()
                try:
                    found=api.request('PATCH','blocks/'+collection['id']+'/children',{'children':[block('paragraph',rich(pending))]})['results']
                except StopSync as error:
                    if re.match(r'NOTION_(NOT_SENT|HTTP_(400|401|403|404|409|413|429)):',str(error)):
                        a['state']='prepared';save()
                    raise
            a['container']=found[0]['id'];a['state']='staging';save()
            expected=[block('paragraph',rich('来源：Paper Chat 选中内容；消息 '+a['source']['message_id']+'；仅保存选区，未经原文核查。'))]+a['blocks']
            if a['warnings']: expected.append(block('paragraph',rich('格式说明：'+'；'.join(a['warnings']))))
            reconcile_children(api,a['container'],expected)
            if not same_tree(snapshot_tree(api,a['container']),expected): raise StopSync('精选内容读回不一致')
            api.request('PATCH','blocks/'+a['container'],{'paragraph':{'rich_text':rich(complete)}})
            check=[b for b in api.children(collection['id']) if b['id']==a['container']]
            if len(check)!=1 or plain({'type':'rich_text','rich_text':check[0]['paragraph']['rich_text']}) != complete:
                raise StopSync('精选同步状态未确认')
            a['state']='committed';save()
            return {'status':'SELECTION_SYNCED','url':'https://www.notion.so/'+normalize_id(original),'warnings':a['warnings']}
        except Exception as e:
            save(str(e));raise

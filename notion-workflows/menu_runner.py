"""One process per explicit Zotero menu click. Never launches AI or background polling."""
import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from paper_reading import ROOT, Zotero, choose_note, note_summary
from notion_sync import Notion, Store, StopSync, sync, paper_identity
from paper_properties import inspect_fulltext


def load_environment():
    # A long-lived Zotero process may predate setting a user environment variable.
    if os.name == 'nt' and not (os.environ.get('NOTION_TOKEN') or os.environ.get('NOTION_API_KEY')):
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
            for name in ('NOTION_TOKEN', 'NOTION_API_KEY'):
                try: os.environ[name] = winreg.QueryValueEx(key, name)[0]
                except FileNotFoundError: pass


def run():
    p = argparse.ArgumentParser()
    p.add_argument('--item-key', required=True)
    p.add_argument('--library-type', choices=['user', 'group'], default='user')
    p.add_argument('--library-id', default='0')
    p.add_argument('--bind-note')
    p.add_argument('--selection-file')
    p.add_argument('--preview-selection', action='store_true')
    p.add_argument('--selection-hash')
    args = p.parse_args()
    folder = ROOT / '.runtime'; folder.mkdir(exist_ok=True)
    zotero = Zotero(args.library_type, args.library_id)
    paper = zotero.item(args.item_key)
    if paper['data']['itemType'] in ('note', 'attachment'): raise StopSync('请选择论文条目')
    if args.selection_file:
        from selection_sync import read_selection, preview_selection, sync_selection
        if args.bind_note: raise StopSync('选区同步不能绑定精读笔记')
        selection = read_selection(args.selection_file, folder)
        if (selection['item_key'] != paper['key'] or selection['library_type'] != paper['library']['type']
                or selection['library_id'] != str(paper['library']['id'])):
            raise StopSync('选区与目标论文身份不一致')
        if args.preview_selection: return preview_selection(selection)
        if not args.selection_hash: raise StopSync('请先预览选中内容')
        load_environment()
        config = json.loads((ROOT / 'deep-reading-database.json').read_text(encoding='utf-8'))
        return sync_selection(Notion(), config, paper, selection, folder, args.selection_hash)
    if args.preview_selection or args.selection_hash: raise StopSync('缺少选区请求文件')
    identity = paper_identity(paper)
    children = zotero.children(args.item_key)
    paper['_attachments'] = [n for n in children if n['data']['itemType']=='attachment']
    paper['_fulltext'] = inspect_fulltext(zotero, paper['_attachments'])
    store = Store(folder / 'state.sqlite3')
    with closing(store.db) as db:
        db.execute('CREATE TABLE IF NOT EXISTS note_bindings(identity TEXT PRIMARY KEY,note_key TEXT)')
        mapping = store.mapping(identity)
        bound = db.execute('SELECT note_key FROM note_bindings WHERE identity=?', (identity,)).fetchone()
        if args.bind_note:
            note = choose_note(paper, children, args.bind_note)
            if mapping and mapping['note_key'] != note['key']: raise StopSync('已有成功版本绑定其他笔记；需明确迁移，不能覆盖绑定')
            with db: db.execute('INSERT OR REPLACE INTO note_bindings VALUES(?,?)', (identity, note['key']))
            return {'status': 'BOUND', 'note_key': note['key']}
        note = choose_note(paper, children, mapping['note_key'] if mapping else bound[0] if bound else None)
    load_environment()
    config = json.loads((ROOT / 'deep-reading-database.json').read_text(encoding='utf-8'))
    return sync(Notion(), config, paper, note, folder)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    try: result = run()
    except ValueError as error:
        try: result = json.loads(str(error))
        except ValueError: result = {'status': 'PAUSED', 'error': str(error)}
    except Exception as error: result = {'status': 'PAUSED', 'error': str(error)}
    print(json.dumps(result, ensure_ascii=False))

from __future__ import annotations

import collections
import os
import platform
import plistlib
import subprocess
from pathlib import Path

from .local_api import LocalAPI
from .util import SyncError, now, write_json


def diagnose(api=None):
    os_name = platform.system()
    result = {'checked_at_utc': now(), 'os': os_name, 'os_version': platform.version(),
              'architecture': platform.machine(), 'python': platform.python_version(),
              'mac_tested': False, 'word_official_integration_tested': False,
              'bulk_write_enabled': False, 'apps': {}, 'limitations': []}
    if os_name == 'Darwin':
        for app in ('Zotero', 'Microsoft Word'):
            candidates = [Path('/Applications') / (app + '.app'), Path.home() / 'Applications' / (app + '.app')]
            found = next((p for p in candidates if p.exists()), None)
            version = None
            if found:
                with (found / 'Contents/Info.plist').open('rb') as handle:
                    version = plistlib.load(handle).get('CFBundleShortVersionString')
            result['apps'][app] = {'installed': bool(found), 'path': str(found) if found else None, 'version': version}
        office = Path.home() / 'Library/Group Containers/UBF8T346G9.Office'
        templates = [office / folder / start / 'Word/Zotero.dotm'
                     for folder in ('User Content', 'User Content.localized')
                     for start in ('Startup', 'Startup.localized')]
        result['word_template_found'] = any(p.is_file() for p in templates)
        result['limitations'].append('已检查安装文件；需在 Word 内完成官方插入、刷新和重新打开编辑测试')
    else:
        for app, relative in [('Zotero', 'Zotero/zotero.exe'), ('Microsoft Word', 'Microsoft Office/root/Office16/WINWORD.EXE')]:
            paths = [Path(os.environ.get(key, 'C:/Program Files')) / relative for key in ('ProgramFiles', 'ProgramFiles(x86)')]
            found = next((p for p in paths if p.is_file()), None)
            result['apps'][app] = {'installed': bool(found), 'path': str(found) if found else None}
        result['limitations'].append('当前不是 macOS；Mac Word 集成与启动器未实测')
    try:
        snapshot = (api or LocalAPI()).snapshot()
        counts = collections.Counter(item['data'].get('itemType') for item in snapshot['items'])
        result['zotero'] = {**snapshot['connection'], 'items': len(snapshot['items']),
                            'annotations': counts['annotation'], 'notes': counts['note'],
                            'pdf_attachments': sum(i['data'].get('contentType') == 'application/pdf' for i in snapshot['items']),
                            'snapshot_complete': True, 'version_cursor': snapshot['version']}
        result['snapshot'] = snapshot
    except SyncError as exc:
        result['zotero'] = {'available': False, 'error': exc.code, 'detail': exc.detail}
    return result


def write_report(output, result):
    output = Path(output)
    snapshot = result.pop('snapshot', None)
    if snapshot:
        write_json(output / 'private/diagnostic-snapshot.json', snapshot)
    write_json(output / 'private/environment.json', result)
    z = result['zotero']
    rows = [('# 环境诊断报告\n\n诊断时间 UTC：' + result['checked_at_utc'] + '\n'),
            '用户目标为 macOS；本次开发和实测环境为 ' + result['os'] + ' ' + result['os_version'] + '。\n',
            '| 检查项 | 实际结果 |\n| --- | --- |',
            '| Python | ' + result['python'] + ' |',
            '| Zotero | ' + str(z.get('version') or result['apps'].get('Zotero', {})) + ' |',
            '| Microsoft Word | ' + str(result['apps'].get('Microsoft Word', {})) + ' |',
            '| Local API | ' + ('可用，API v' + str(z.get('api_version')) if z['available'] else str(z)) + ' |',
            '| 官方 Word 动态引文插入、刷新、重开编辑 | 未实测 |',
            '| 批量自动写入 | 禁用 |']
    if z['available']:
        rows += [f"\n本地只读完整扫描：{z['items']} 个条目，{z['annotations']} 个 PDF 批注，{z['notes']} 个笔记，{z['pdf_attachments']} 个 PDF 附件。只记录数量；原始响应位于私有目录。"]
    rows += ['\n' + '\n'.join('- ' + text for text in result['limitations']),
             '\n诊断使用官方 Local API 的 GET 请求；未访问 Zotero SQLite、未创建或修改文献条目。文档字段检查仅能证明 OOXML 结构，动态可编辑性必须通过实际 Word 的 Zotero Add/Edit Citation 验收。',
             '\n此诊断不连接或操作 Word 编辑会话。是否能使用官方动态引文，须在目标 Mac 按实机验收单验证。']
    (output / 'environment_report.md').write_text('\n'.join(rows) + '\n', encoding='utf-8')


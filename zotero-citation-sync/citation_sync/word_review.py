"""Verify official-user-edited copies; this module never creates citation fields."""
from __future__ import annotations

import platform
import subprocess
from collections import Counter
from pathlib import Path

from .docx_read import citation_identity, locate, read_docx
from .util import SyncError

STYLES = {'preserve': None, 'apa': 'http://www.zotero.org/styles/apa',
          'ieee': 'http://www.zotero.org/styles/ieee', 'vancouver': 'http://www.zotero.org/styles/vancouver',
          'nature': 'http://www.zotero.org/styles/nature'}
ATTESTATIONS = ('saved_and_reopened', 'new_and_existing_editable', 'official_refresh_completed',
                'bibliography_checked', 'layout_reviewed')


def supported_mac():
    return platform.system() == 'Darwin' and any((p / 'Microsoft Word.app').is_dir()
                                                for p in (Path('/Applications'), Path.home() / 'Applications'))


def open_working_copy(path):
    if not supported_mac():
        raise SyncError('MAC_WORD_NOT_AVAILABLE', '此主机未具备 Mac Word；可在目标 Mac 打开工作副本')
    subprocess.run(['/usr/bin/open', '-a', 'Microsoft Word', str(Path(path).resolve())], check=True, timeout=15)


def official_steps(session):
    steps = [f"在 Microsoft Word 打开工作副本：{session['working_path']}",
             '在 Zotero → Document Preferences 中选择要求的 CSL 样式；存储方式使用 Fields。']
    for task in session['tasks']:
        steps.append(f"查找并核对正文原句「{task['target']['quote']}」，把光标放在原句末尾；点击 Zotero → Add/Edit Citation，选择 {task['selected']['title']}（{task['selected']['key']}）。")
    steps += ['如无文末参考文献，使用 Zotero → Add/Edit Bibliography 添加；然后点击 Zotero → Refresh。',
              '保存并关闭工作副本后重新打开，分别用 Add/Edit Citation 编辑新旧引用，核对文末参考文献和整篇版式。',
              '返回应用执行读回检查并填写官方操作确认。不能使用 Unlink Citations。']
    return steps


def item_core(item):
    # Formatting and itemData can refresh; identity and user citation options may not disappear.
    return {k: v for k, v in item.items() if k not in ('id', 'uris', 'itemData')}


def verify_copy(original, working, tasks, style='preserve'):
    before, after = read_docx(original), read_docx(working)
    issues = list(after['issues'])
    if before['protected_content'] != after['protected_content']:
        issues.append('BODY_OR_FORMAT_CHANGED')
    if before['protected_parts'] != after['protected_parts']:
        issues.append('IMAGE_NOTE_COMMENT_OR_EMBEDDED_PART_CHANGED')
    if before['objects_hash'] != after['objects_hash']:
        issues.append('EQUATION_TABLE_OR_DRAWING_CHANGED')
    for key, value in before['protected_definitions'].items():
        if after['protected_definitions'].get(key) != value:
            issues.append('STYLE_RELATIONSHIP_OR_DOCUMENT_SETTING_CHANGED')
    other_fields = lambda doc: [(f['part'], f['paragraph'], f['offset'], f['instruction'])
                                for f in doc['fields'] if f['kind'] == 'other']
    if other_fields(before) != other_fields(after):
        issues.append('OTHER_WORD_FIELD_CHANGED')
    old = {f.get('payload', {}).get('citationID'): f for f in before['citations']}
    new = {f.get('payload', {}).get('citationID'): f for f in after['citations']}
    for key, field in old.items():
        candidate = new.get(key)
        if not candidate:
            issues.append('EXISTING_CITATION_REMOVED')
            continue
        if (candidate['part'], candidate['paragraph'], candidate['offset']) != (field['part'], field['paragraph'], field['offset']):
            issues.append('EXISTING_CITATION_MOVED')
        for prior in field.get('payload', {}).get('citationItems', []):
            corresponding = [entry for entry in candidate.get('payload', {}).get('citationItems', [])
                             if set(citation_identity(prior)) & set(citation_identity(entry))]
            if len(corresponding) != 1 or item_core(prior) != item_core(corresponding[0]):
                issues.append('EXISTING_CITATION_CHANGED')
    permitted, observed = Counter(), Counter()
    for task in tasks:
        matches = [target for target in locate(after, task['target']['quote'])
                   if target['paragraph'] == task['target']['paragraph']]
        if len(matches) != 1:
            issues.append('TARGET_MOVED_OR_AMBIGUOUS')
            continue
        target = matches[0]
        permitted[(task['selected']['identity'], target['part'], target['paragraph'], target['offset'])] += 1
    added_ids = []
    for key, field in new.items():
        old_identities = set(old.get(key, {}).get('identities', []))
        additions = set(field.get('identities', [])) - old_identities
        for identity in additions:
            observed[(identity, field['part'], field['paragraph'], field['offset'])] += 1
        if additions:
            added_ids.append(key)
    if observed != permitted:
        issues.append('INSERTED_CITATIONS_DO_NOT_MATCH_APPROVED_TARGETS')
    if len(after['bibliographies']) != 1 or not after['bibliographies'][0]['display'].strip():
        issues.append('BIBLIOGRAPHY_NOT_PRESENT_OR_AMBIGUOUS')
    expected = STYLES.get(style) or before['style_id']
    if not after['style_id'] or (expected and after['style_id'] != expected):
        issues.append('CSL_STYLE_MISMATCH')
    return {'structural_checks_passed': not issues, 'issues': sorted(set(issues)),
            'working_sha256': after['sha256'], 'added_citation_ids': added_ids,
            'citation_count_before': len(before['citations']), 'citation_count_after': len(after['citations']),
            'bibliography_fields': len(after['bibliographies']), 'style_id': after['style_id'],
            'official_word_compatibility': 'not_proven_by_xml'}


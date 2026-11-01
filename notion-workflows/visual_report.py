"""Presentation of saved Paper Chat content with user-selected visible sections.

Optional local editorial profiles are bound to an exact source hash. They are
Source blocks and evidence remain in local version backups. No model calls.
"""
import copy
import json
from pathlib import Path
from paper_reading import SECTIONS, block, rich

RENDERER = 'relation-body-v3-visible-sections'


def text(b):
    return ''.join(x.get('text', {}).get('content', '') for x in b[b['type']].get('rich_text', []))


def callout(value, icon='📖', color='gray_background'):
    b = block('callout', rich(value))
    b['callout'].update(icon={'type': 'emoji', 'emoji': icon}, color=color)
    return b


def table(rows):
    return {'object': 'block', 'type': 'table', 'table': {
        'table_width': len(rows[0]), 'has_column_header': True, 'has_row_header': False,
        'children': [{'object': 'block', 'type': 'table_row', 'table_row': {'cells': [rich(c) for c in row]}} for row in rows]}}


def render(paper, payload, version, folder):
    source = copy.deepcopy(payload['blocks'])
    groups = {s: [] for s in SECTIONS}
    current = SECTIONS[-1]
    for b in source:
        if b['type'] == 'heading_1' and text(b) in groups:
            current = text(b)
        else:
            if text(b) != '原报告没有可直接归入本节的独立章节；未补写内容。':
                groups[current].append(b)
    profile_path = Path(folder) / 'renderer-profiles' / (payload['sha256'] + '.json')
    profile = json.loads(profile_path.read_text(encoding='utf-8')) if profile_path.exists() else {}
    def excerpt(section):
        return next((text(b) for b in groups[section] if b['type'] in ('paragraph', 'bulleted_list_item') and text(b)), '已保存笔记未提供独立摘录；详见下方原文。')[:1200]
    cards = profile.get('cards') or [
        ['核心研究问题', excerpt(SECTIONS[2])], ['研究设计', excerpt(SECTIONS[4])],
        ['主要研究发现', excerpt(SECTIONS[6])], ['学术贡献', excerpt(SECTIONS[8])]]
    columns = [{'object': 'block', 'type': 'column', 'column': {'children': [callout(k+'\n'+v, '🔎' if i == 0 else '📌') for k,v in cards[i::2]]}} for i in range(2)]
    result = [block('heading_1', rich('论文概览 · Dashboard')),
              block('paragraph', rich(paper['data']['title'], {'bold': True})),
              block('paragraph', rich(profile.get('chinese_title', '中文名称：待核对；未自动翻译'))),
              callout(f'精读版本 v{version} · 证据状态：沿用已保存记录，未重新核查 PDF\n'
                      f'Zotero {payload["identity"]} · Paper Chat {payload["note_key"]}\n'
                      '正文按已保存报告编排。作者论点、Paper Chat 解释与待核查项须结合证据阅读。', '🧾'),
              {'object':'block','type':'column_list','column_list':{'children':columns}}]
    if profile.get('caution'): result.append(callout(profile['caution'], '⚠️', 'yellow_background'))
    if payload.get('property_overview'): result.insert(4, payload['property_overview'])
    result.append(block('paragraph', rich('阅读顺序：一分钟概览 → 背景与问题 → 变量与方法 → 核心结果 → 图表与公式 → 贡献与批判 → 知识图谱', {'color':'gray'})))
    def section(title, items):
        result.extend([{'object':'block','type':'divider','divider':{}}, block('heading_1', rich(title))])
        result.extend(items or [callout('当前已保存报告未提供独立内容，待分析；未补写研究结论。', '🔍')])
    section('01 · 一分钟读懂论文', [block('paragraph',rich(profile['minute']))] if profile.get('minute') else [callout(excerpt(SECTIONS[0]))])
    section('02 · 研究背景与研究空白', groups[SECTIONS[0]] + groups[SECTIONS[1]])
    section('03 · 研究问题、假设与变量', groups[SECTIONS[2]] + ([table(profile['variables'])] if profile.get('variables') else []) + groups[SECTIONS[3]])
    methods = []
    if profile.get('workflow'):
        methods += [callout('研究流程（依据已保存方法章节整理；箭头表示处理顺序，不表示因果关系）', '🧭'),
                    {'object':'block','type':'code','code':{'language':'mermaid','rich_text':rich(profile['workflow'])}},
                    table(profile['workflow_table'])]
    section('04 · 研究方法与分析流程', methods + groups[SECTIONS[4]] + groups[SECTIONS[5]])
    findings = groups[SECTIONS[6]]
    for b in findings:
        if b['type'] == 'heading_3' and text(b).startswith('发现'):
            b['heading_3']['color'] = 'blue'
    section('05 · 核心研究结果', ([table(profile['results'])] if profile.get('results') else []) + findings)
    section('06 · 图表与数学公式深度解读', [table(profile['figures']), callout(profile['figure_note'], '🔍')] if profile.get('figures') else [callout('图表面板、坐标、公式变量与推导待逐项核实。原报告中已有图表、公式和解释在对应方法/结果章节完整保留。', '🔍')])
    section('07 · 研究结论与学术贡献', [callout('以下保留 Paper Chat 的原始分析。涉及“证明”“机制”“标志物”等表述不代表本次同步已核实；潜在应用与因果解释需结合批判性精读。', '📎')] + groups[SECTIONS[7]] + groups[SECTIONS[8]])
    section('08 · 批判性精读与后续研究', groups[SECTIONS[9]] + groups[SECTIONS[10]])
    section('09 · 单篇论文知识图谱', [callout('待生成：尚未完成逐条关系的证据核对。上方变量关系表区分了假设、相关及分组比较；不将其标记为已完成知识图谱。', '🔍')])
    # User requested sections 10 and 11 removed from the visible report.
    # Keep the complete source in the sync journal and local backups.
    return result

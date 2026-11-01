"""Conservative rich-text conversion for explicit Paper Chat selections only."""
import re

from paper_reading import Converter, Node, Tree, block, rich


_STRUCTURAL = {'p', 'div', 'section', 'article', 'blockquote', 'li', 'pre',
               'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}
_ACTIVE = {'script', 'style', 'iframe', 'object', 'embed', 'input', 'button', 'textarea'}
_FALLBACK = '选区格式无法完整保真，已改为仅同步预览中的原始文字；原始 HTML 片段保留在本地。'


def _span_text(spans):
    return ''.join(s['text']['content'] if s['type'] == 'text'
                   else s.get('equation', {}).get('expression', '') for s in spans)


def _visible_text(blocks):
    """Read the converted content in its actual display order, including children."""
    parts = []
    for b in blocks:
        data = b[b['type']]
        if 'rich_text' in data:
            parts.append(_span_text(data['rich_text']))
        if 'cells' in data:
            parts.append('\t'.join(_span_text(cell) for cell in data['cells']))
        if data.get('children'):
            parts.append(_visible_text(data['children']))
    return '\n'.join(parts)


class SelectionConverter(Converter):
    """Preserve selection whitespace without changing the full-report renderer."""

    def inline_sequence(self, nodes, styles=None, link=None):
        result, previous_block = [], False
        for node in nodes:
            structural = isinstance(node, Node) and node.tag in _STRUCTURAL
            # Indentation between block elements is layout, not another paragraph.
            if isinstance(node, str) and not node.strip() and previous_block:
                continue
            spans = self.inline(node, styles, link)
            if result and spans and (structural or previous_block):
                if not _span_text(result).endswith('\n'):
                    result.extend(rich('\n', styles, link))
            result.extend(spans)
            if spans:
                previous_block = structural
        return result

    def inline(self, node, styles=None, link=None):
        if isinstance(node, Node) and node.tag in _STRUCTURAL:
            return self.inline_sequence(node.children, styles, link)
        return super().inline(node, styles, link)

    def blocks(self, nodes, list_kind=None):
        result, pending = [], []

        def flush():
            nonlocal pending
            if _span_text(pending).strip():
                for offset in range(0, len(pending), 90):
                    result.append(block('paragraph', pending[offset:offset + 90]))
            pending = []

        for node in nodes:
            if isinstance(node, str):
                # Keep spaces between inline nodes; discard only empty block runs.
                pending.extend(rich(node))
            elif node.tag in ('div', 'section', 'article'):
                flush()
                result.extend(self.blocks(node.children))
            elif node.tag in ('ul', 'ol'):
                flush()
                result.extend(self.blocks(node.children, 'bulleted_list_item'
                                          if node.tag == 'ul' else 'numbered_list_item'))
            elif node.tag == 'table':
                flush()
                result.extend(self.table(node))
            elif node.tag in ('p', 'li', 'blockquote', 'pre') or re.fullmatch('h[1-6]', node.tag):
                flush()
                kind = {'p': 'paragraph', 'li': list_kind or 'paragraph',
                        'blockquote': 'quote', 'pre': 'paragraph'}.get(node.tag)
                if kind is None:
                    kind = 'heading_' + str(min(int(node.tag[1]), 3))
                first_list = next((i for i, child in enumerate(node.children)
                                   if isinstance(child, Node) and child.tag in ('ul', 'ol')),
                                  len(node.children))
                spans = self.inline_sequence(node.children[:first_list])
                if node.tag == 'pre':
                    self.warn('Code block retained as text; original formatting in HTML')
                for offset in range(0, max(1, len(spans)), 90):
                    result.append(block(kind, spans[offset:offset + 90]))
                if first_list < len(node.children):
                    # Keep later paragraphs AFTER the nested list, not before it.
                    children = self.blocks(node.children[first_list:])
                    if kind in ('paragraph', 'quote', 'bulleted_list_item', 'numbered_list_item'):
                        result[-1][kind]['children'] = children
                    else:
                        self.warn('Nested list outside a supported parent; hierarchy flattened')
                        result.extend(children)
            elif node.tag == 'hr':
                flush()
                result.append({'object': 'block', 'type': 'divider', 'divider': {}})
            else:
                pending.extend(self.inline(node))
        flush()
        return result


def convert_fragment(text, html):
    """Return {blocks, warnings}; never expand beyond the selected visible text.

    A fallback retains the literal text, including all line breaks. It deliberately
    avoids inferring missing formula glyphs, image captions, or hidden HTML text.
    """
    tree = Tree(html)

    def tags(node):
        if isinstance(node, str):
            return
        yield node.tag
        for child in node.children:
            yield from tags(child)

    names = set(tags(tree.root))
    if _ACTIVE.intersection(names):
        raise ValueError('选区包含界面控件或活动内容，请重新选择回答正文')

    def fallback(warnings):
        spans = rich(text)
        if 'img' in names:
            warnings = [*warnings, '图片未上传；原始图片引用保留在本地 HTML。']
        if 'math' in names:
            warnings = [*warnings, '公式无法可靠对应所选文字，按可见原文保留；原始公式结构保留在本地 HTML。']
        return {'blocks': [block('paragraph', spans[i:i + 90]) for i in range(0, len(spans), 90)],
                'warnings': list(dict.fromkeys([*warnings, _FALLBACK]))}

    # HTML may have render-only line breaks, but may not contribute extra words.
    compact = lambda value: re.sub(r'\s+', '', value)
    if compact(tree.root.text()) != compact(text):
        return fallback([])
    converter = SelectionConverter()
    converted = converter.blocks(tree.root.children)
    normalize = lambda value: re.sub(r'\s+', ' ', value).strip()
    if not converted or normalize(_visible_text(converted)) != normalize(text):
        return fallback(converter.warnings)
    return {'blocks': converted, 'warnings': converter.warnings}

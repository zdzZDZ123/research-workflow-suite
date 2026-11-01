import unittest

from selection_converter import convert_fragment, _visible_text


class SelectionConverterTests(unittest.TestCase):
    def test_inline_whitespace_is_not_lost(self):
        result = convert_fragment('A B', '<span>A</span> <span>B</span>')
        self.assertEqual(_visible_text(result['blocks']), 'A B')
        self.assertEqual(result['warnings'], [])

    def test_paragraphs_inside_quote_remain_separate(self):
        result = convert_fragment('First paragraph.\nSecond paragraph.',
                                  '<blockquote><p>First paragraph.</p><p>Second paragraph.</p></blockquote>')
        self.assertEqual(result['blocks'][0]['type'], 'quote')
        self.assertEqual(_visible_text(result['blocks']), 'First paragraph.\nSecond paragraph.')
        self.assertEqual(result['warnings'], [])

    def test_nested_list_keeps_later_paragraph_after_children(self):
        text = 'Outer one\nOuter two\nChild one\nChild two\nAfter children\nNext item'
        html = ('<ul><li><p>Outer one</p><p>Outer two</p><ul><li><p>Child one</p>'
                '<p>Child two</p></li></ul><p>After children</p></li><li>Next item</li></ul>')
        result = convert_fragment(text, html)
        outer = result['blocks'][0]
        self.assertEqual(outer['type'], 'bulleted_list_item')
        self.assertEqual([b['type'] for b in outer['bulleted_list_item']['children']],
                         ['bulleted_list_item', 'paragraph'])
        self.assertEqual(_visible_text(result['blocks']), text)
        self.assertEqual(result['warnings'], [])

    def test_inline_styles_links_and_supported_formula_preserved(self):
        result = convert_fragment('Bold link x', '<p><b>Bold</b> <a href="https://example.org">link</a> '
                                  '<math><annotation encoding="application/x-tex">x</annotation></math></p>')
        spans = result['blocks'][0]['paragraph']['rich_text']
        self.assertTrue(spans[0]['annotations']['bold'])
        self.assertEqual(spans[2]['text']['link']['url'], 'https://example.org')
        self.assertEqual(spans[-1]['equation']['expression'], 'x')
        self.assertEqual(result['warnings'], [])

    def test_html_cannot_add_unselected_words(self):
        result = convert_fragment('Selected only', '<p>Selected only UNSELECTED</p>')
        self.assertEqual(_visible_text(result['blocks']), 'Selected only')
        self.assertTrue(result['warnings'])

    def test_whitespace_mismatch_falls_back_to_literal_text(self):
        result = convert_fragment('A B\nC', '<p>AB C</p>')
        self.assertEqual(_visible_text(result['blocks']), 'A B\nC')
        self.assertTrue(result['warnings'])

    def test_unsupported_link_does_not_append_unselected_url(self):
        result = convert_fragment('Evidence', '<a href="zotero://select/items/PRIVATE">Evidence</a>')
        self.assertEqual(_visible_text(result['blocks']), 'Evidence')
        self.assertTrue(result['warnings'])

    def test_unsupported_images_warn_without_inventing_caption(self):
        result = convert_fragment('Selected', '<p>Selected<img src="file:///private.png" alt="UNSELECTED CAPTION"></p>')
        self.assertEqual(_visible_text(result['blocks']), 'Selected')
        self.assertTrue(result['warnings'])

    def test_active_content_rejected(self):
        with self.assertRaises(ValueError):
            convert_fragment('A bad B', '<p>A<button>bad</button>B</p>')

    def test_long_fallback_is_not_truncated(self):
        text = 'word ' * 1000 + '\nend'
        result = convert_fragment(text, '<p>different</p>')
        self.assertEqual(_visible_text(result['blocks']), text)

    def test_maximum_fallback_respects_rich_text_array_limit(self):
        text = 'x' * 200000
        result = convert_fragment(text, '<p>different</p>')
        self.assertEqual(''.join(_visible_text([b]) for b in result['blocks']), text)
        self.assertTrue(all(len(b['paragraph']['rich_text']) <= 90 for b in result['blocks']))

    def test_fallback_still_lists_image_and_formula_limits(self):
        result = convert_fragment('Selected', '<p>Different<img src="file:///private.png"></p><math>x</math>')
        self.assertTrue(any('图片' in w for w in result['warnings']))
        self.assertTrue(any('公式' in w for w in result['warnings']))
        self.assertEqual(_visible_text(result['blocks']), 'Selected')


if __name__ == '__main__':
    unittest.main()

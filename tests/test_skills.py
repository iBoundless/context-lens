import json
import unittest

from context_lens.analysis import analyze


def loaded(name='review', body='abcd'):
    return f'<skill_content name="{name}">{body}</skill_content>'


class SkillAnalysisTests(unittest.TestCase):
    def skills(self, payload):
        result = analyze(payload)
        self.assertIn('skills', result)
        return result, result.get('skills', [])

    def test_text_spans_are_disjoint_and_repeated_occurrences_are_counted(self):
        content = 'before ' + loaded() + '\n' + loaded() + ' after ' + loaded('other', 'я')
        result, skills = self.skills({'instructions': content})
        self.assertEqual([(s['name'], s['kind'], s['occurrences']) for s in skills],
                         [('other', 'loaded', 1), ('review', 'loaded', 2)])
        self.assertEqual(''.join(b['content'] for b in result['blocks']), content)
        self.assertEqual(result['estimated_tokens'], sum(b['estimated_tokens'] for b in result['blocks']))
        review = next(s for s in skills if s['name'] == 'review')
        self.assertEqual(review['estimated_tokens'], 26)
        self.assertEqual(review['attributions'], ['format-heuristic'])
        self.assertTrue(all('<skill_content' not in b['content'] for b in result['blocks'] if b['category'] != 'skills'))

    def test_explicit_metadata_owns_entire_span_and_wins_over_tags(self):
        content = loaded('other')
        result, skills = self.skills({'system': [{'type': 'text', 'text': content,
            'metadata': {'experimental': {'instructions': [{'key': 'skill:review', 'text': content}]}}}]})
        self.assertEqual(len(result['blocks']), 1)
        self.assertEqual([(s['name'], s['attributions']) for s in skills], [('review', ['source-metadata'])])

    def test_catalog_descriptions_are_separate_from_loaded_content(self):
        catalog = '<available_skills>\n<skill><id>slug</id><name>review</name><description>Read code</description></skill>\n</available_skills>'
        result, skills = self.skills({'instructions': catalog + loaded()})
        self.assertEqual([(s['name'], s['kind']) for s in skills], [('review', 'description'), ('review', 'loaded')])
        self.assertEqual(''.join(b['content'] for b in result['blocks']), catalog + loaded())

    def test_malformed_and_nested_tags_are_not_attributed(self):
        for content in [loaded(''), '<skill_content name=123>abcd</skill_content>',
                        '<skill_content name="x">open', loaded('outer', loaded('inner')),
                        '<available_skills><skill><name>x</name></skill></available_skills>',
                        '<available_skills><skill><name>x</name><name>y</name><description>d</description></skill></available_skills>']:
            with self.subTest(content=content):
                result, skills = self.skills({'instructions': content})
                self.assertEqual(skills, [])
                self.assertEqual(''.join(b['content'] for b in result['blocks']), content)

    def test_standard_message_text_and_tool_outputs_preserve_serialized_envelope(self):
        for message in [{'role': 'user', 'content': 'quote: ' + loaded()},
                        {'role': 'tool', 'content': loaded()},
                        {'type': 'function_call_output', 'call_id': 'c1', 'output': loaded()},
                        {'role': 'developer', 'content': [{'type': 'input_text', 'text': loaded()}]}]:
            with self.subTest(message=message):
                result, skills = self.skills({'input': [message]})
                self.assertEqual(len(skills), 1)
                self.assertEqual(''.join(b['content'] for b in result['blocks']),
                                 json.dumps(message, ensure_ascii=False, separators=(',', ':')))
                self.assertEqual(skills[0]['attributions'], ['format-heuristic'])

    def test_attachments_remain_unknown_but_text_skill_size_is_visible(self):
        result, skills = self.skills({'input': [{'role': 'user', 'content': [
            {'type': 'input_text', 'text': loaded()}, {'type': 'input_image', 'image_url': 'test'}]}]})
        self.assertIsNone(result['estimated_tokens'])
        self.assertEqual(len(skills), 1)
        self.assertGreater(skills[0]['estimated_tokens'], 0)
        self.assertIn('multimodal-tokens-unknown', result['warnings'])

    def test_tools_and_arbitrary_metadata_are_not_scanned_as_loaded_content(self):
        result, skills = self.skills({'tools': [{'name': 'read', 'description': loaded()}],
            'input': [{'role': 'assistant', 'content': 'ordinary', 'metadata': {'text': loaded()}}]})
        self.assertEqual(skills, [])

    def test_stale_provenance_falls_back_to_actual_tagged_text(self):
        result, skills = self.skills({'system': [{'type': 'text', 'text': loaded('actual'), 'metadata': {
            'experimental': {'instructions': [{'key': 'skill:stale', 'text': 'not actual'}]}}}]})
        self.assertEqual([s['name'] for s in skills], ['actual'])

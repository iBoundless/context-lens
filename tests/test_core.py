import json
import os
import tempfile
import time
import unittest
from pathlib import Path

try:
    from context_lens.analysis import analyze
    from context_lens.store import Store
except ImportError:
    analyze = Store = None


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(analyze, 'normalizer is not implemented')

    def test_messages_and_tools_have_disjoint_blocks(self):
        result = analyze({'model': 'test', 'instructions': 'System', 'tools': [
            {'type': 'function', 'name': 'mcp__gate__read', 'description': 'Read'}],
            'input': [{'role': 'user', 'content': 'Hello'},
                      {'type': 'function_call_output', 'call_id': 'c1', 'output': 'Done'}]})
        self.assertEqual([b['category'] for b in result['blocks']],
                         ['harness', 'mcp', 'user', 'tool'])
        self.assertEqual(result['estimated_tokens'], sum(b['estimated_tokens'] for b in result['blocks']))
        self.assertEqual(result['context_completeness'], 'client-payload')

    def test_server_reference_is_not_a_full_context(self):
        result = analyze({'previous_response_id': 'resp_missing', 'input': 'Next'})
        self.assertEqual(result['context_completeness'], 'server-state-unknown')
        self.assertIn('server-state-unknown', result['warnings'])

    def test_multimodal_and_unknown_protocol_are_not_zero(self):
        result = analyze({'messages': [{'role': 'user', 'content': [
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,abcd'}}]}]})
        self.assertIn('multimodal-tokens-unknown', result['warnings'])
        self.assertIsNone(result['blocks'][0]['estimated_tokens'])
        self.assertIsNone(result['estimated_tokens'])
        unknown = analyze({'strange_protocol': [1, 2]})
        self.assertEqual(unknown['context_completeness'], 'unknown-protocol')
        self.assertEqual(unknown['blocks'][0]['category'], 'unknown')

    def test_hosted_tools_are_not_mislabelled_builtin(self):
        result = analyze({'tools': [{'type': 'web_search'}, {'type': 'function', 'name': 'read'}]})
        self.assertEqual([b['category'] for b in result['blocks']], ['unknown', 'builtin'])

    def test_explicit_instruction_provenance_splits_without_double_count(self):
        result = analyze({'system': [{'type': 'text', 'text': 'core\nproject\nskill', 'metadata': {
            'experimental': {'instructions': [{'key': 'core/date', 'text': 'core'},
                {'key': 'file:/project/AGENTS.md', 'text': 'project'},
                {'key': 'skill:review', 'text': 'skill'}]}}}]})
        self.assertEqual([b['category'] for b in result['blocks']], ['harness', 'memory', 'skills'])
        self.assertEqual([b['content'] for b in result['blocks']], ['core', 'project', 'skill'])

    def test_stale_provenance_does_not_replace_actual_text(self):
        result = analyze({'system': [{'type': 'text', 'text': 'actual', 'metadata': {
            'experimental': {'instructions': [{'key': 'skill:old', 'text': 'stale'}]}}}]})
        self.assertEqual(len(result['blocks']), 1)
        self.assertIn('actual', result['blocks'][0]['content'])
        self.assertNotEqual(result['blocks'][0]['content'], 'stale')

    def test_malformed_nested_protocol_is_inspectable_not_an_exception(self):
        for payload in [{'tools': [{'function': None}]}, {'system': [{'metadata': None}]},
                        {'tools': [{'name': ['not-a-string']}]}, {'system': [{'metadata': {'experimental': None}}]}]:
            with self.subTest(payload=payload):
                self.assertTrue(analyze(payload)['blocks'])

    def test_document_and_unrecognized_content_blocks_have_unknown_token_size(self):
        for kind in ['document', 'input_video', 'new_provider_modality']:
            result = analyze({'messages': [{'role': 'user', 'content': [{'type': kind, 'source': {'data': 'abc'}}]}]})
            self.assertIsNone(result['estimated_tokens'])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(Store, 'store is not implemented')
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'private' / 'capture.db'
        self.store = Store(self.path)

    def tearDown(self):
        if hasattr(self, 'store'):
            self.store.close()
            self.tmp.cleanup()

    def event(self, **updates):
        event = {'event_id': 'evt1', 'client': 'opencode', 'session_id': 's1',
                 'request_id': 'r1', 'direction': 'request', 'transport': 'http',
                 'payload': {'model': 'test', 'messages': [{'role': 'user', 'content': 'hello'}]}}
        return event | updates

    def test_opt_in_dedup_and_private_permissions(self):
        self.assertFalse(self.store.ingest(self.event()))
        self.store.record('opencode', 's1', True)
        self.assertTrue(self.store.ingest(self.event()))
        self.assertFalse(self.store.ingest(self.event()))
        self.assertEqual(len(self.store.waves('opencode', 's1')), 1)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.path.parent).st_mode & 0o777, 0o700)
        self.store.record('opencode', 's1', False)
        self.assertFalse(self.store.ingest(self.event(event_id='evt2', request_id='r2')))

    def test_metadata_headers_discarded_payload_kept_and_usage_attached(self):
        self.store.record('opencode', 's1', True)
        self.store.ingest(self.event(headers={'authorization': 'SECRET', 'cookie': 'COOKIE'},
                                     unexpected_secret='SECRET'))
        self.store.ingest(self.event(event_id='evt2', direction='response', payload={
            'type': 'response.completed', 'response': {'id': 'resp1', 'usage': {
                'input_tokens': 123, 'output_tokens': 45}}}))
        wave = self.store.waves('opencode', 's1')[0]
        detail = self.store.detail(wave['id'])
        self.assertNotIn('SECRET', json.dumps(detail))
        self.assertNotIn('COOKIE', json.dumps(detail))
        self.assertEqual(wave['usage']['input_tokens'], 123)
        self.assertEqual(detail['request']['payload']['messages'][0]['content'], 'hello')

    def test_out_of_order_response_and_clients_do_not_mix(self):
        self.store.record('codex', 's1', True)
        self.store.record('opencode', 's1', True)
        self.store.ingest(self.event(event_id='e2', direction='response', payload={'usage': {'prompt_tokens': 9}}))
        self.store.ingest(self.event())
        self.store.ingest(self.event(event_id='e3', client='codex'))
        self.assertEqual(self.store.waves('opencode', 's1')[0]['usage']['prompt_tokens'], 9)
        self.assertIsNone(self.store.waves('codex', 's1')[0]['usage'])

    def test_retention_and_manual_deletion(self):
        self.store.record('opencode', 's1', True)
        self.store.ingest(self.event())
        self.store.prune(time.time() + 8 * 86400)
        self.assertEqual(self.store.waves('opencode', 's1'), [])
        self.store.ingest(self.event(event_id='new'))
        self.store.delete_session('opencode', 's1')
        self.assertEqual(self.store.sessions(), [])

    def test_nullable_response_does_not_poison_wave_list(self):
        self.store.record('opencode', 's1', True)
        self.store.ingest(self.event())
        self.store.ingest(self.event(event_id='e2', direction='response', payload={'response': None}))
        self.assertEqual(len(self.store.waves('opencode', 's1')), 1)

    def test_wave_window_is_bounded_and_content_is_loaded_on_demand(self):
        self.store.record('opencode', 's1', True)
        for i in range(105):
            self.store.ingest(self.event(event_id=f'e{i}', request_id=f'r{i}'))
        page = self.store.waves('opencode', 's1')
        self.assertEqual(len(page), 100)
        self.assertNotIn('content', page[0]['blocks'][0])
        older = self.store.waves('opencode', 's1', before=page[0]['id'])
        self.assertEqual(len(older), 5)
        full = self.store.wave(page[0]['id'])
        self.assertIn('hello', full['blocks'][0]['content'])

    def test_response_gap_does_not_double_provider_usage(self):
        self.store.record('opencode', 's1', True)
        self.store.ingest(self.event())
        self.store.ingest(self.event(event_id='gap', direction='gap', capture='gap', payload={'reason': 'lost'}))
        self.store.ingest(self.event(event_id='usage', direction='response', payload={'usage': {'input_tokens': 42}}))
        self.assertEqual(self.store.overview('opencode', 's1')['provider_input_sum'], 42)
        gap = next(w for w in self.store.waves('opencode', 's1') if w['capture'] == 'gap')
        self.assertIsNone(gap['estimated_tokens'])


if __name__ == '__main__':
    unittest.main()

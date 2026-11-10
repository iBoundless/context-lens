import asyncio
import json
import tempfile
import unittest
import sqlite3
from unittest.mock import patch
from pathlib import Path

from aiohttp import ClientPayloadError, web, WSMsgType
from aiohttp.test_utils import TestClient, TestServer

try:
    from context_lens.server import create_app
except ImportError:
    create_app = None

TOKEN = 'local-test-capability-not-a-real-secret'


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertIsNotNone(create_app, 'collector server is not implemented')
        self.tmp = tempfile.TemporaryDirectory()
        self.calls = []

        async def upstream(request):
            self.calls.append((request.path, request.headers.get('Authorization'), await request.read()))
            if request.headers.get('Upgrade', '').lower() == 'websocket':
                ws = web.WebSocketResponse()
                await ws.prepare(request)
                async for message in ws:
                    if message.type == WSMsgType.TEXT:
                        body = json.loads(message.data)
                        if body.get('input') == 'close':
                            await ws.close(code=1008, message=b'fixture reason')
                            break
                        if body.get('hold'):
                            await ws.send_json({'type': 'response.created', 'response': {'id': 'resp-held'}})
                            continue
                        await ws.send_json({'type': 'response.completed', 'response': {
                            'id': 'resp-ws', 'output': [], 'usage': {'input_tokens': 11, 'output_tokens': 3}}})
                return ws
            if request.path.endswith('/redirect'):
                raise web.HTTPFound('http://example.invalid/steal')
            body = json.loads(self.calls[-1][2] or '{}')
            if body.get('input') == 'redirect':
                raise web.HTTPFound('http://example.invalid/steal')
            if body.get('input') == 'abort-stream':
                result = web.StreamResponse(headers={'Content-Type': 'text/event-stream', 'Content-Length': '99999'})
                await result.prepare(request)
                await result.write(b'data: {"type":"partial"}\n\n')
                request.transport.close()
                return result
            result = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
            await result.prepare(request)
            await result.write(b'data: {"type":"response.completed","response":{"id":"resp-1","usage":{"input_tokens":12,"output_tokens":4}}}\n\n')
            await result.write(b'data: [DONE]\n\n')
            await result.write_eof()
            return result

        up = web.Application()
        up.router.add_route('*', '/{tail:.*}', upstream)
        self.up = TestServer(up)
        await self.up.start_server()
        self.app = create_app(Path(self.tmp.name) / 'state' / 'capture.db', TOKEN,
                              upstream=str(self.up.make_url('/v1')), allow_local_upstream=True)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.auth = {'X-Context-Lens-Token': TOKEN}

    async def asyncTearDown(self):
        if hasattr(self, 'client'):
            await self.client.close()
            await self.up.close()
            self.tmp.cleanup()

    async def enable(self, client='codex', session='s1'):
        response = await self.client.post('/api/record', json={
            'client': client, 'session_id': session, 'enabled': True}, headers=self.auth)
        self.assertEqual(response.status, 200)

    async def test_auth_host_origin_and_no_cache(self):
        self.assertEqual((await self.client.get('/api/sessions')).status, 401)
        self.assertEqual((await self.client.get('/api/sessions', headers=self.auth | {'Host': 'evil.test'})).status, 403)
        self.assertEqual((await self.client.post('/api/record', json={}, headers=self.auth | {'Origin': 'https://evil.test'})).status, 403)
        response = await self.client.get('/api/sessions', headers=self.auth)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(await response.json(), [])

    async def test_ingest_opt_in_and_discovery(self):
        event = {'event_id': 'e1', 'session_id': 's1', 'client': 'opencode',
                 'request_id': 'r1', 'direction': 'request', 'payload': {'input': 'hello'}}
        response = await self.client.post('/api/events', json=event, headers=self.auth)
        self.assertFalse((await response.json())['stored'])
        sessions = await (await self.client.get('/api/sessions', headers=self.auth)).json()
        self.assertEqual(sessions[0]['session_id'], 's1')
        await self.enable('opencode')
        await self.client.post('/api/events', json=event, headers=self.auth)
        waves = await (await self.client.get('/api/waves?client=opencode&session_id=s1', headers=self.auth)).json()
        self.assertEqual(len(waves), 1)
        self.assertEqual(waves[0]['blocks'][0]['category'], 'user')
        response = await self.client.post('/api/events', json={'bad': 'input'}, headers=self.auth)
        self.assertEqual(response.status, 400)

    async def test_sse_reports_updates_and_closes(self):
        response = await self.client.get('/api/live', headers=self.auth)
        self.assertEqual(response.status, 200)
        self.assertIn(b'event: ready', await response.content.readuntil(b'\n\n'))
        await self.enable()
        update = await asyncio.wait_for(response.content.readuntil(b'\n\n'), 2)
        self.assertIn(b'event: changed', update)
        response.close()

    async def test_skill_metadata_is_available_only_through_authenticated_wave_and_overview(self):
        await self.enable('opencode')
        event = {'event_id': 'skill', 'session_id': 's1', 'client': 'opencode',
                 'request_id': 'r1', 'direction': 'request', 'payload': {
                     'instructions': '<skill_content name="review">abcd</skill_content>'}}
        await self.client.post('/api/events', json=event, headers=self.auth)
        url = '/api/overview?client=opencode&session_id=s1'
        self.assertEqual((await self.client.get(url)).status, 401)
        info = await (await self.client.get(url, headers=self.auth)).json()
        self.assertEqual(info.get('skill_totals', [{}])[0].get('request_count'), 1)
        waves = await (await self.client.get('/api/waves?client=opencode&session_id=s1', headers=self.auth)).json()
        wave_url = '/api/wave/' + str(waves[0]['id'])
        self.assertEqual((await self.client.get(wave_url)).status, 401)
        full = await (await self.client.get(wave_url, headers=self.auth)).json()
        self.assertEqual(full['skills'][0]['name'], 'review')
        self.assertEqual(full['skills'], waves[0]['skills'])

    async def test_proxy_forwards_bytes_without_recording_by_default(self):
        body = b'{"model":"fixture","input":"hi","stream":true}'
        headers = self.auth | {'Authorization': 'Bearer UPSTREAM_SECRET',
                                'Content-Type': 'application/json', 'session_id': 's1'}
        response = await self.client.post('/proxy/codex/responses', data=body, headers=headers)
        self.assertEqual(response.status, 200)
        self.assertIn(b'[DONE]', await response.read())
        self.assertEqual(self.calls[0], ('/v1/responses', 'Bearer UPSTREAM_SECRET', body))
        waves = await (await self.client.get('/api/waves?client=codex&session_id=s1', headers=self.auth)).json()
        self.assertEqual(waves, [])
        await self.enable()
        response = await self.client.post('/proxy/codex/responses', data=body, headers=headers)
        await response.read()
        waves = await (await self.client.get('/api/waves?client=codex&session_id=s1', headers=self.auth)).json()
        self.assertEqual(waves[0]['usage']['input_tokens'], 12)
        detail = await (await self.client.get('/api/request/' + str(waves[0]['id']), headers=self.auth)).json()
        self.assertNotIn('UPSTREAM_SECRET', json.dumps(detail))

    async def test_websocket_capture_and_passthrough(self):
        await self.enable()
        headers = self.auth | {'session_id': 's1', 'Authorization': 'Bearer WS_SECRET'}
        ws = await self.client.ws_connect('/proxy/codex/responses', headers=headers)
        await ws.send_json({'type': 'response.create', 'model': 'fixture', 'input': 'hello'})
        reply = await ws.receive_json(timeout=2)
        self.assertEqual(reply['response']['id'], 'resp-ws')
        await ws.close()
        waves = await (await self.client.get('/api/waves?client=codex&session_id=s1', headers=self.auth)).json()
        self.assertEqual(len(waves), 1)
        self.assertEqual(waves[0]['transport'], 'ws')
        self.assertEqual(waves[0]['usage']['input_tokens'], 11)

    async def test_proxy_path_and_query_cannot_change_destination(self):
        response = await self.client.post('/proxy/codex/redirect', json={}, headers=self.auth)
        self.assertEqual(response.status, 404)
        response = await self.client.post('/proxy/codex/responses?api_key=secret', json={}, headers=self.auth)
        self.assertEqual(response.status, 400)
        self.assertEqual(self.calls, [])

    async def test_actual_codex_hyphenated_headers_prefer_thread_identity(self):
        await self.enable(session='thread-1')
        response = await self.client.post('/proxy/codex/responses', json={'input': 'hi'}, headers=self.auth | {
            'session-id': 'root-1', 'thread-id': 'thread-1'})
        self.assertEqual(response.status, 200)
        await response.read()
        waves = await (await self.client.get('/api/waves?client=codex&session_id=thread-1', headers=self.auth)).json()
        self.assertEqual(len(waves), 1)

    async def test_storage_failure_does_not_prevent_forwarding(self):
        from context_lens.store import Store
        with patch.object(Store, 'ingest', side_effect=sqlite3.OperationalError('disk full')):
            response = await self.client.post('/proxy/codex/responses', json={'input': 'hi'},
                                              headers=self.auth | {'session-id': 's1'})
            self.assertEqual(response.status, 200)
            self.assertIn(b'[DONE]', await response.read())

    async def test_websocket_control_frame_keeps_completion_on_create_wave(self):
        await self.enable()
        ws = await self.client.ws_connect('/proxy/codex/responses', headers=self.auth | {'session-id': 's1'})
        await ws.send_json({'type': 'response.create', 'input': 'hello'})
        await ws.receive_json(timeout=2)
        await ws.send_json({'type': 'response.interrupt', 'response_id': 'resp-ws'})
        await ws.receive_json(timeout=2)
        await ws.close()
        waves = await (await self.client.get('/api/waves?client=codex&session_id=s1', headers=self.auth)).json()
        self.assertEqual(len(waves), 1)

    async def test_broken_upstream_stream_leaves_explicit_gap(self):
        await self.enable()
        response = await self.client.post('/proxy/codex/responses', json={'input': 'abort-stream'},
                                          headers=self.auth | {'session-id': 's1'})
        try:
            await asyncio.wait_for(response.read(), 2)
        except ClientPayloadError:
            pass
        waves = await (await self.client.get('/api/waves?client=codex&session_id=s1', headers=self.auth)).json()
        self.assertTrue(any(w['capture'] != 'complete' for w in waves))

    async def test_upstream_redirect_is_not_given_to_the_client(self):
        response = await self.client.post('/proxy/codex/responses', json={'input': 'redirect'},
                                          headers=self.auth | {'session-id': 's1'}, allow_redirects=False)
        self.assertEqual(response.status, 502)
        self.assertNotIn('Location', response.headers)

    async def test_websocket_close_code_and_reason_are_preserved(self):
        ws = await self.client.ws_connect('/proxy/codex/responses', headers=self.auth | {'session-id': 's1'})
        await ws.send_json({'type': 'response.create', 'input': 'close'})
        message = await ws.receive(timeout=2)
        self.assertEqual(message.type, WSMsgType.CLOSE)
        self.assertEqual(message.data, 1008)
        self.assertEqual(message.extra, 'fixture reason')
        await ws.close()

    async def test_overlapping_ws_requests_are_marked_ambiguous_not_misattributed(self):
        await self.enable()
        ws = await self.client.ws_connect('/proxy/codex/responses', headers=self.auth | {'session-id': 's1'})
        await ws.send_json({'type': 'response.create', 'input': 'one', 'hold': True})
        await ws.receive_json(timeout=2)
        await ws.send_json({'type': 'response.create', 'input': 'two'})
        await ws.receive_json(timeout=2)
        await ws.close()
        waves = await (await self.client.get('/api/waves?client=codex&session_id=s1', headers=self.auth)).json()
        self.assertTrue(any(w['capture'] == 'association-unknown' for w in waves))
        self.assertTrue(all(w['usage'] is None for w in waves))

    async def test_delete_removes_session_and_turns_recording_off(self):
        await self.enable()
        response = await self.client.delete('/api/session?client=codex&session_id=s1', headers=self.auth)
        self.assertEqual(response.status, 200)
        self.assertEqual(await (await self.client.get('/api/sessions', headers=self.auth)).json(), [])


if __name__ == '__main__':
    unittest.main()

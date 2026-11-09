"""Fixed-destination inference proxy, never an open HTTP/TLS interception proxy."""

import asyncio
import json
import uuid
import sqlite3
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, WSMsgType, web

LIMIT = 8 * 1024 * 1024
PATHS = {'responses', 'responses/compact', 'chat/completions', 'messages'}
HOP = {'host', 'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
       'te', 'trailer', 'transfer-encoding', 'upgrade', 'content-length',
       'x-context-lens-token', 'cookie', 'sec-websocket-key', 'sec-websocket-version',
       'sec-websocket-extensions', 'sec-websocket-protocol', 'set-cookie'}


def forward_headers(headers):
    blocked = HOP | {h.strip().lower() for h in headers.get('Connection', '').split(',')}
    return {k: v for k, v in headers.items() if k.lower() not in blocked}


def decode_payload(body):
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return body.decode('utf-8', errors='replace') if isinstance(body, bytes) else body


class Proxy:
    def __init__(self, store, notify, upstream, allow_local=False):
        parsed = urlsplit(upstream)
        local = parsed.hostname in {'127.0.0.1', 'localhost', '::1'}
        if (parsed.scheme != 'https' and not (allow_local and local and parsed.scheme == 'http')) or not parsed.hostname:
            raise ValueError('upstream must be HTTPS (local HTTP is test-only)')
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('upstream must not contain credentials, query or fragment')
        self.store, self.notify = store, notify
        self.upstream = upstream.rstrip('/')
        self.session = None
        self.losses = {}
        self.storage_errors = 0

    async def start(self):
        self.session = ClientSession(timeout=ClientTimeout(total=None, sock_connect=15, sock_read=300),
                                     auto_decompress=False, trust_env=False)

    async def close(self):
        if self.session:
            await self.session.close()

    async def capture(self, session_id, request_id, direction, payload, transport, kind='primary', capture='complete'):
        event = {'event_id': str(uuid.uuid4()), 'client': 'codex', 'session_id': session_id,
                 'request_id': request_id, 'direction': direction, 'payload': payload,
                 'transport': transport, 'kind': kind, 'capture': capture}
        if isinstance(payload, dict) and isinstance(payload.get('model'), str):
            event['model'] = payload['model']
        try:
            await asyncio.to_thread(self.store.ingest, event)
            lost = self.losses.pop(session_id, None)
            if lost:
                lost['event_id'] = str(uuid.uuid4())
                await asyncio.to_thread(self.store.ingest, lost)
        except (sqlite3.Error, OSError):
            self.storage_errors += 1
            if len(self.losses) < 256 or session_id in self.losses:
                self.losses[session_id] = event | {'direction': 'gap', 'payload': {'reason': 'storage-error'}, 'capture': 'gap'}
        self.notify()

    async def handle(self, request):
        path = request.match_info['tail']
        if path not in PATHS:
            raise web.HTTPNotFound()
        if request.query_string:
            raise web.HTTPBadRequest(text='proxy query parameters are not supported')
        if request.headers.get('Content-Encoding', 'identity').lower() != 'identity':
            raise web.HTTPUnsupportedMediaType(text='disable request compression for capture')
        # A stable session header is required; never silently merge unrelated clients.
        session_id = (request.headers.get('thread-id') or request.headers.get('session-id') or
                      request.headers.get('session_id') or request.headers.get('x-context-lens-session'))
        if not session_id or len(session_id) > 256:
            raise web.HTTPBadRequest(text='thread-id, session-id or x-context-lens-session header required')
        headers = forward_headers(request.headers)
        # Capture response bytes in identity encoding, without changing model payloads.
        headers['Accept-Encoding'] = 'identity'
        target = self.upstream + '/' + path
        if request.headers.get('Upgrade', '').lower() == 'websocket':
            return await self.websocket(request, target, headers, session_id)
        if request.method != 'POST':
            raise web.HTTPMethodNotAllowed(request.method, ['POST'])
        body = await request.read()
        rid = str(uuid.uuid4())
        kind = 'compaction' if path.endswith('/compact') else 'primary'
        await self.capture(session_id, rid, 'request', decode_payload(body), 'http', kind)
        try:
            async with self.session.post(target, data=body, headers=headers, allow_redirects=False) as upstream:
                if 300 <= upstream.status < 400:
                    await self.capture(session_id, rid, 'gap', {'reason': 'upstream-redirect-refused'}, 'http', kind, 'gap')
                    raise web.HTTPBadGateway(text='upstream redirect refused')
                response_headers = forward_headers(upstream.headers)
                response_headers.pop('Set-Cookie', None)
                response = web.StreamResponse(status=upstream.status, headers=response_headers)
                await response.prepare(request)
                captured = bytearray()
                truncated = False
                try:
                    async for chunk in upstream.content.iter_any():
                        await response.write(chunk)
                        if len(captured) + len(chunk) <= LIMIT and not truncated:
                            captured.extend(chunk)
                        else:
                            truncated = True
                    await self.capture(session_id, rid, 'response', decode_payload(bytes(captured)), 'http', kind,
                                 'truncated' if truncated else 'complete')
                    await response.write_eof()
                except (ClientError, ConnectionError, asyncio.CancelledError) as error:
                    await self.capture(session_id, rid, 'gap', {'reason': 'response-interrupted'}, 'http', kind, 'gap')
                    if isinstance(error, asyncio.CancelledError):
                        raise
                    response.force_close()
                    # A successful chunk terminator would disguise upstream failure.
                    # Abort this response so downstream streaming clients see the break.
                    if request.transport:
                        request.transport.close()
                return response
        except (ClientError, OSError, asyncio.TimeoutError):
            await self.capture(session_id, rid, 'gap', {'reason': 'upstream-unavailable'}, 'http', kind, 'gap')
            raise web.HTTPBadGateway(text='upstream unavailable; credentials and response details omitted')

    async def websocket(self, request, target, headers, session_id):
        # This is a message-level bridge; ping/pong is managed on each leg.
        async with self.session.ws_connect(target, headers=headers, max_msg_size=64 * 1024 * 1024,
                                           autoping=True, autoclose=False) as upstream:
            downstream = web.WebSocketResponse(max_msg_size=64 * 1024 * 1024, autoclose=False)
            await downstream.prepare(request)
            current = None
            active = False
            ambiguous = False
            response_ids = {}

            async def pump(source, dest, outgoing):
                nonlocal current, active, ambiguous
                while True:
                    msg = await source.receive()
                    if msg.type == WSMsgType.TEXT:
                        payload = decode_payload(msg.data)
                        kind = payload.get('type') if isinstance(payload, dict) else None
                        control = outgoing and isinstance(kind, str) and kind != 'response.create'
                        if outgoing and not control:
                            if active:
                                ambiguous = True
                                await self.capture(session_id, current, 'gap', {'reason': 'overlapping-ws-requests'}, 'ws', capture='gap')
                            current = str(uuid.uuid4())
                            active = True
                        rid = current
                        if not outgoing and isinstance(payload, dict):
                            response = payload.get('response')
                            response_id = (response.get('id') if isinstance(response, dict) else None) or payload.get('response_id')
                            if kind == 'response.created' and response_id and not ambiguous:
                                response_ids[response_id] = current
                            if response_id and response_id in response_ids:
                                rid = response_ids[response_id]
                            elif ambiguous:
                                rid = None
                            if kind in {'response.completed', 'response.failed', 'response.cancelled', 'error'}:
                                active = False
                        if rid:
                            if len(msg.data.encode()) <= LIMIT:
                                await self.capture(session_id, rid, 'notice' if control else 'request' if outgoing else 'response',
                                                   payload, 'ws', capture='association-unknown' if ambiguous else 'complete')
                            else:
                                await self.capture(session_id, rid, 'gap', {'reason': 'frame-too-large'}, 'ws', capture='gap')
                        await dest.send_str(msg.data)
                    elif msg.type == WSMsgType.BINARY:
                        await self.capture(session_id, current or str(uuid.uuid4()), 'gap',
                                     {'reason': 'binary-frame-not-inspected'}, 'ws', capture='gap')
                        await dest.send_bytes(msg.data)
                    elif msg.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING}:
                        if active and current:
                            await self.capture(session_id, current, 'gap', {'reason': 'ws-closed-before-completion'}, 'ws', capture='gap')
                        code = msg.data if msg.type == WSMsgType.CLOSE and isinstance(msg.data, int) else source.close_code
                        await dest.close(code=code if code and code != 1006 else 1000,
                                         message=str(msg.extra or '').encode())
                        return
                    elif msg.type == WSMsgType.ERROR:
                        raise ConnectionError('websocket transport failed')

            tasks = [asyncio.create_task(pump(downstream, upstream, True)),
                     asyncio.create_task(pump(upstream, downstream, False))]
            try:
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                results = await asyncio.gather(*tasks, return_exceptions=True)
                if any(isinstance(result, Exception) for result in results) and current:
                    await self.capture(session_id, current, 'gap', {'reason': 'ws-transport-failed'}, 'ws', capture='gap')
            finally:
                for task in tasks:
                    task.cancel()
                await downstream.close()
            return downstream

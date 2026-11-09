"""Authenticated loopback-only collector and read UI."""

import asyncio
import hmac
import json
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

from .proxy import Proxy
from .store import Store

ROOT = Path(__file__).resolve().parent.parent
STATE = web.AppKey('state', dict)


def create_app(db_path, token, *, upstream='https://api.openai.com/v1', allow_local_upstream=False):
    if not isinstance(token, str) or len(token) < 24:
        raise ValueError('collector capability must have at least 24 characters')
    state = {'store': Store(db_path), 'token': token, 'listeners': set(), 'closing': False}

    @web.middleware
    async def security(request, handler):
        try:
            host = urlsplit('http://' + request.host).hostname
        except ValueError:
            host = None
        if host not in {'localhost', '127.0.0.1', '::1'}:
            raise web.HTTPForbidden(text='loopback host required')
        origin = request.headers.get('Origin')
        if origin and origin != f'{request.scheme}://{request.host}':
            raise web.HTTPForbidden(text='same-origin access required')
        protected = request.path.startswith(('/api/', '/proxy/')) and request.path != '/api/login'
        presented = request.headers.get('X-Context-Lens-Token') or request.cookies.get('context_lens', '')
        if protected and not hmac.compare_digest(presented.encode(), token.encode()):
            raise web.HTTPUnauthorized(text='collector access token required')
        try:
            response = await handler(request)
        except (ValueError, TypeError, json.JSONDecodeError):
            raise web.HTTPBadRequest(text='invalid request data')
        except sqlite3.Error:
            raise web.HTTPServiceUnavailable(text='capture storage unavailable')
        if not response.prepared:
            response.headers['Cache-Control'] = 'no-store'
            response.headers['X-Content-Type-Options'] = 'nosniff'
            response.headers['Referrer-Policy'] = 'no-referrer'
            response.headers['Content-Security-Policy'] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
        return response

    app = web.Application(middlewares=[security], client_max_size=16 * 1024 * 1024)
    app[STATE] = state

    def notify():
        for queue in tuple(state['listeners']):
            if queue.empty():
                queue.put_nowait(True)

    state['notify'] = notify
    state['proxy'] = Proxy(state['store'], notify, upstream, allow_local_upstream)

    async def login(request):
        data = await request.json()
        presented = data.get('token', '')
        if not isinstance(presented, str) or not hmac.compare_digest(presented.encode(), token.encode()):
            raise web.HTTPUnauthorized()
        response = web.json_response({'ok': True})
        response.set_cookie('context_lens', token, httponly=True, samesite='Strict', path='/')
        return response

    async def sessions(request):
        return web.json_response(await asyncio.to_thread(state['store'].sessions))

    async def waves(request):
        before = int(request.query['before']) if 'before' in request.query else None
        return web.json_response(await asyncio.to_thread(state['store'].waves,
            request.query.get('client'), request.query.get('session_id'), before=before))

    async def wave(request):
        data = await asyncio.to_thread(state['store'].wave, int(request.match_info['seq']))
        if data is None:
            raise web.HTTPNotFound()
        return web.json_response(data)

    async def overview(request):
        data = await asyncio.to_thread(state['store'].overview, request.query.get('client'), request.query.get('session_id'))
        data['storage_errors'] = state['proxy'].storage_errors
        data['pending_loss_sessions'] = len(state['proxy'].losses)
        return web.json_response(data)

    async def detail(request):
        data = await asyncio.to_thread(state['store'].detail, int(request.match_info['seq']))
        if data is None:
            raise web.HTTPNotFound()
        return web.json_response(data)

    async def record(request):
        data = await request.json()
        if not isinstance(data.get('enabled'), bool):
            raise ValueError('enabled must be boolean')
        await asyncio.to_thread(state['store'].record, data.get('client'), data.get('session_id'), data['enabled'])
        notify()
        return web.json_response({'ok': True})

    async def ingest(request):
        event = await request.json()
        stored = await asyncio.to_thread(state['store'].ingest, event)
        notify()
        return web.json_response({'stored': stored})

    async def recording(request):
        return web.json_response({'recording': await asyncio.to_thread(state['store'].is_recording,
            request.query.get('client'), request.query.get('session_id'))})

    async def delete(request):
        await asyncio.to_thread(state['store'].delete_session, request.query.get('client'), request.query.get('session_id'))
        notify()
        return web.json_response({'ok': True})

    async def live(request):
        queue = asyncio.Queue(maxsize=1)
        state['listeners'].add(queue)
        response = web.StreamResponse(headers={'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store',
                                              'X-Accel-Buffering': 'no'})
        await response.prepare(request)
        try:
            await response.write(b'event: ready\ndata: {}\n\n')
            while not state['closing']:
                try:
                    await asyncio.wait_for(queue.get(), 10)
                    await response.write(b'event: changed\ndata: {}\n\n')
                except asyncio.TimeoutError:
                    await response.write(b': heartbeat\n\n')
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            state['listeners'].discard(queue)
        return response

    async def index(request):
        return web.FileResponse(ROOT / 'web' / 'index.html')

    async def asset(request):
        name = request.match_info['name']
        if name not in {'app.js', 'style.css', 'view-model.mjs'}:
            raise web.HTTPNotFound()
        return web.FileResponse(ROOT / 'web' / name)

    async def lifetime(application):
        await state['proxy'].start()

        async def retention():
            while True:
                await asyncio.to_thread(state['store'].prune)
                await asyncio.sleep(3600)

        task = asyncio.create_task(retention())
        yield
        state['closing'] = True
        notify()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await state['proxy'].close()
        state['store'].close()

    async def shutdown(application):
        state['closing'] = True
        notify()

    app.cleanup_ctx.append(lifetime)
    app.on_shutdown.append(shutdown)
    app.router.add_post('/api/login', login)
    app.router.add_get('/api/sessions', sessions)
    app.router.add_get('/api/waves', waves)
    app.router.add_get('/api/wave/{seq}', wave)
    app.router.add_get('/api/overview', overview)
    app.router.add_get('/api/request/{seq}', detail)
    app.router.add_get('/api/recording', recording)
    app.router.add_post('/api/record', record)
    app.router.add_post('/api/events', ingest)
    app.router.add_delete('/api/session', delete)
    app.router.add_get('/api/live', live)
    app.router.add_route('*', '/proxy/codex/{tail:.*}', state['proxy'].handle)
    app.router.add_get('/', index)
    app.router.add_get('/assets/{name}', asset)
    return app

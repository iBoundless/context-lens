import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';

let plugin, createSender, readBody;
try {
  ({ default: plugin, createSender, readBody } = await import('../adapters/opencode/index.mjs'));
} catch (error) {
  if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error;
}

async function collector(t, { recording = true, status = 200 } = {}) {
  const events = [];
  const server = http.createServer(async (req, res) => {
    const parts = [];
    for await (const chunk of req) parts.push(chunk);
    res.setHeader('Content-Type', 'application/json');
    if (req.url.startsWith('/api/recording')) res.end(JSON.stringify({ recording }));
    else {
      events.push(JSON.parse(Buffer.concat(parts).toString()));
      res.writeHead(status); res.end(JSON.stringify({ stored: status === 200 }));
    }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => { server.closeAllConnections(); server.close(resolve); }));
  return { events, endpoint: `http://127.0.0.1:${server.address().port}` };
}

async function harness(t, options = {}) {
  assert.ok(plugin, 'OpenCode adapter is not implemented');
  const c = await collector(t, options);
  const hooks = new Map();
  const cleanup = await plugin.setup({
    options: { endpoint: c.endpoint, token: options.token ?? 'fixture-local-token-not-provider-key' },
    session: { hook: async (name, fn) => { hooks.set(name, fn); return { dispose: async () => {} }; } },
  });
  t.after(cleanup);
  return { ...c, hooks, cleanup };
}

const base = { sessionID: 's1', model: { providerID: 'fixture', id: 'test' }, kind: 'primary', agent: 'build' };

test('HTTP hooks preserve streams, correlate by Request and never serialize headers', async t => {
  const h = await harness(t);
  const request = new Request('https://fixture.invalid/responses', {
    method: 'POST', headers: { authorization: 'Bearer SECRET' }, body: JSON.stringify({ input: 'hello' }),
  });
  await h.hooks.get('http.request')({ ...base, request });
  assert.equal(await request.text(), '{"input":"hello"}');
  const response = new Response('data: {"usage":{"input_tokens":7}}\n\n');
  await h.hooks.get('http.response')({ ...base, request, response });
  assert.match(await response.text(), /input_tokens/);
  await h.cleanup();
  const recorded = h.events.filter(e => ['request', 'response'].includes(e.direction));
  assert.equal(recorded.length, 2);
  assert.equal(recorded[0].request_id, recorded[1].request_id);
  assert.equal(recorded[0].payload.input, 'hello');
  assert.doesNotMatch(JSON.stringify(h.events), /SECRET|authorization/);
});

test('unrecorded sessions emit discovery metadata but not payload', async t => {
  const h = await harness(t, { recording: false });
  await h.hooks.get('http.request')({ ...base, request: new Request('https://fixture.invalid', {
    method: 'POST', body: '{"input":"PRIVATE_BODY"}',
  }) });
  await h.cleanup();
  assert.ok(h.events.some(e => e.direction === 'notice'));
  assert.doesNotMatch(JSON.stringify(h.events), /PRIVATE_BODY/);
});

test('WebSocket hooks preserve frames and capture request and completion', async t => {
  const h = await harness(t);
  const event = { ...base, frame: '{"type":"response.create","input":"hi"}' };
  await h.hooks.get('experimental.ws.send')(event);
  await h.hooks.get('experimental.ws.receive')({ ...base, frame: '{"type":"response.completed","response":{"usage":{"input_tokens":5}}}' });
  assert.equal(event.frame, '{"type":"response.create","input":"hi"}');
  await h.cleanup();
  const events = h.events.filter(e => ['request', 'response'].includes(e.direction));
  assert.equal(events.length, 2);
  assert.equal(events[0].request_id, events[1].request_id);
  assert.equal(events[0].transport, 'ws');
});

test('bounded sender reports dropped work after recovery', async t => {
  assert.ok(createSender, 'bounded sender is not implemented');
  const c = await collector(t);
  const sender = createSender({ endpoint: c.endpoint, token: 'fixture', maxPending: 1 });
  let release;
  const wait = new Promise(resolve => { release = resolve; });
  const meta = { client: 'opencode', session_id: 's1', request_id: 'r1', transport: 'http' };
  sender.enqueue(async () => { await wait; return { ...meta, direction: 'request', payload: { input: 'one' } }; }, meta);
  sender.enqueue(async () => ({ ...meta, direction: 'request', payload: { input: 'two' } }), meta);
  release();
  await sender.close();
  assert.ok(c.events.some(e => e.direction === 'gap'));
});

test('collector failures never reject a model hook', async t => {
  const h = await harness(t, { status: 500 });
  await assert.doesNotReject(() => h.hooks.get('experimental.ws.send')({ ...base, frame: '{"input":"hi"}' }));
  await assert.doesNotReject(h.cleanup());
});

test('capture deadline marks partial body incomplete', async () => {
  assert.ok(readBody, 'bounded body reader is not exported');
  const body = new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode('partial')); } });
  const result = await readBody(body, { timeoutMs: 10 });
  assert.equal(result.capture, 'gap');
});

test('queue loss is reported for every affected session', async t => {
  const c = await collector(t);
  const sender = createSender({ endpoint: c.endpoint, token: 'fixture', maxPending: 1 });
  let release;
  const wait = new Promise(resolve => { release = resolve; });
  const meta = { client: 'opencode', session_id: 'a', request_id: 'r1', transport: 'http' };
  sender.enqueue(async () => { await wait; return { ...meta, direction: 'notice' }; }, meta);
  for (const session_id of ['b', 'c']) sender.enqueue(async () => null, { ...meta, session_id });
  release(); await sender.close();
  assert.deepEqual(c.events.filter(e => e.direction === 'gap').map(e => e.session_id).sort(), ['b', 'c']);
});

test('token from a newline-terminated private file works', async t => {
  const h = await harness(t, { token: 'fixture-local-token-not-provider-key\n' });
  await h.hooks.get('experimental.ws.send')({ ...base, frame: '{"type":"response.create","input":"hi"}' });
  await h.cleanup();
  assert.equal(h.events.filter(e => e.direction === 'request').length, 1);
});

test('OpenCode websocket control frame does not create an extra wave', async t => {
  const h = await harness(t);
  await h.hooks.get('experimental.ws.send')({ ...base, frame: '{"type":"response.create","input":"hi"}' });
  await h.hooks.get('experimental.ws.send')({ ...base, frame: '{"type":"response.interrupt","response_id":"resp1"}' });
  await h.hooks.get('experimental.ws.receive')({ ...base, frame: '{"type":"response.completed","response":{"id":"resp1"}}' });
  await h.cleanup();
  assert.equal(h.events.filter(e => e.direction === 'request').length, 1);
  const req = h.events.find(e => e.direction === 'request');
  const reply = h.events.find(e => e.direction === 'response' && e.payload.type === 'response.completed');
  assert.equal(req.request_id, reply.request_id);
});

test('normal WebSocket bursts are buffered rather than dropped as concurrent HTTP jobs', async t => {
  const c = await collector(t);
  const sender = createSender({ endpoint: c.endpoint, token: 'fixture' });
  const meta = { client: 'opencode', session_id: 'burst', request_id: 'r1', transport: 'ws' };
  for (let i = 0; i < 500; i++) {
    sender.enqueue(async () => ({ ...meta, direction: 'response', payload: {
      type: i === 499 ? 'response.completed' : 'response.output_text.delta', index: i,
    } }), meta);
  }
  await sender.close();
  assert.equal(c.events.filter(e => e.direction === 'response').length, 500);
  assert.equal(c.events.filter(e => e.direction === 'gap').length, 0);
  assert.ok(c.events.some(e => e.direction === 'response' && e.payload.type === 'response.completed'));
});

import { randomUUID } from 'node:crypto';

const LIMIT = 8 * 1024 * 1024;

function jsonOrText(value) {
  try { return JSON.parse(value); } catch { return value; }
}

export async function readBody(body, { timeoutMs = 300_000 } = {}) {
  if (!body) return { payload: null, capture: 'complete' };
  const reader = body.getReader();
  const parts = [];
  let size = 0;
  let timedOut = false;
  const deadline = setTimeout(() => { timedOut = true; void reader.cancel().catch(() => {}); }, timeoutMs);
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > LIMIT) {
        void reader.cancel().catch(() => {});
        return { payload: { reason: 'body-too-large' }, capture: 'gap' };
      }
      parts.push(value);
    }
    if (timedOut) return { payload: { reason: 'capture-deadline' }, capture: 'gap' };
    return { payload: jsonOrText(Buffer.concat(parts).toString('utf8')), capture: 'complete' };
  } finally { clearTimeout(deadline); }
}

export function createSender({ endpoint, token, maxPending = 1024, maxBytes = 32 * 1024 * 1024 }) {
  const jobs = new Set();
  const lanes = Array.from({ length: 4 }, () => Promise.resolve());
  let nextLane = 0, pendingBytes = 0;
  const gaps = new Map();
  const headers = { 'Content-Type': 'application/json', 'X-Context-Lens-Token': token };
  async function post(event) {
    const response = await fetch(endpoint + '/api/events', {
      method: 'POST', headers, body: JSON.stringify({ event_id: randomUUID(), ...event }),
      signal: AbortSignal.timeout(1000),
    });
    await response.arrayBuffer();
    if (!response.ok) throw new Error('collector unavailable');
  }
  function lost(meta, reason = 'collector-network-error') {
    const overflow = gaps.size >= 256 && !gaps.has(meta.session_id);
    const key = overflow ? '__global_capture_loss__' : meta.session_id;
    gaps.set(key, { ...meta, session_id: key, direction: 'gap',
      payload: { reason: overflow ? 'too-many-affected-sessions' : reason }, capture: 'gap' });
  }
  async function flushGap() {
    for (const [key, event] of [...gaps]) {
      gaps.delete(key);
      try { await post(event); } catch { if (!gaps.has(key)) gaps.set(key, event); }
    }
  }
  return {
    enqueue(factory, meta, cancel = () => {}, reservedBytes = 1024) {
      if (jobs.size >= maxPending || pendingBytes + reservedBytes > maxBytes) {
        lost(meta, jobs.size >= maxPending ? 'capture-queue-capacity' : 'capture-byte-budget');
        cancel(); return false;
      }
      pendingBytes += reservedBytes;
      const lane = nextLane++ % lanes.length;
      const job = lanes[lane].then(async () => {
        try {
          const event = await factory();
          if (event) await post(event);
        } catch { lost(meta); }
      }).finally(async () => {
        pendingBytes -= reservedBytes;
        jobs.delete(job);
        if (!jobs.size) await flushGap();
      });
      lanes[lane] = job;
      jobs.add(job);
      return true;
    },
    async close() { await Promise.allSettled([...jobs]); await flushGap(); },
  };
}

// Plugin.define is an identity function in @opencode/plugin@2.2.5.
// Export the structural plugin to avoid installing the whole SDK at runtime.
export default {
  id: 'context-lens',
  async setup(ctx) {
    const endpoint = ctx.options?.endpoint ?? 'http://127.0.0.1:18765';
    const token = (ctx.options?.token ?? process.env.CONTEXT_LENS_TOKEN ?? '').trim();
    const parsed = new URL(endpoint);
    if (!['127.0.0.1', 'localhost', '[::1]'].includes(parsed.hostname) || parsed.protocol !== 'http:' ||
        parsed.username || parsed.password || parsed.search || parsed.hash || parsed.pathname !== '/') {
      throw new Error('Context Lens requires a loopback HTTP collector URL');
    }
    if (!token) throw new Error('Set CONTEXT_LENS_TOKEN for the OpenCode service or plugin options');
    const sender = createSender({ endpoint: endpoint.replace(/\/$/, ''), token });
    const requests = new WeakMap();
    const sockets = new Map();
    const registrations = [];

    const meta = (event, id, transport) => ({ client: 'opencode', session_id: event.sessionID,
      request_id: id, transport, kind: event.kind ?? 'unknown', agent: event.agent,
      model: event.model?.id });

    async function enabled(event, id, transport) {
      const metadata = meta(event, id, transport);
      sender.enqueue(async () => ({ ...metadata, direction: 'notice' }), metadata);
      try {
        const query = new URLSearchParams({ client: 'opencode', session_id: event.sessionID });
        const res = await fetch(endpoint + '/api/recording?' + query, {
          headers: { 'X-Context-Lens-Token': token }, signal: AbortSignal.timeout(300),
        });
        return res.ok && (await res.json()).recording === true;
      } catch { return false; }
    }

    async function register(name, callback) {
      registrations.push(await ctx.session.hook(name, async event => {
        try { await callback(event); } catch { /* Capture must not reject the model operation. */ }
      }));
    }

    await register('http.request', async event => {
      const id = randomUUID();
      if (!await enabled(event, id, 'http')) return;
      requests.set(event.request, id);
      const copy = event.request.clone();
      const metadata = meta(event, id, 'http');
      sender.enqueue(async () => {
        const result = await readBody(copy.body);
        return { ...metadata, ...result, direction: result.capture === 'gap' ? 'gap' : 'request' };
      }, metadata, () => { void copy.body?.cancel().catch(() => {}); }, LIMIT);
    });

    await register('http.response', async event => {
      const id = requests.get(event.request);
      if (!id) return;
      const copy = event.response.clone();
      const metadata = meta(event, id, 'http');
      sender.enqueue(async () => {
        const result = await readBody(copy.body);
        return { ...metadata, ...result, direction: result.capture === 'gap' ? 'gap' : 'response' };
      }, metadata, () => { void copy.body?.cancel().catch(() => {}); }, LIMIT);
    });

    await register('experimental.ws.send', async event => {
      const payload = jsonOrText(event.frame);
      if (typeof payload?.type === 'string' && payload.type !== 'response.create') return;
      const id = randomUUID();
      const key = event.sessionID + '/' + event.kind;
      const existing = sockets.get(key);
      const ambiguous = Boolean(existing?.active);
      if (!await enabled(event, id, 'ws')) return;
      if (ambiguous) {
        const previous = meta(event, existing.id, 'ws');
        sender.enqueue(async () => ({ ...previous, direction: 'gap', capture: 'gap',
          payload: { reason: 'overlapping-ws-requests' } }), previous);
      }
      sockets.set(key, { id, active: true, ambiguous: ambiguous || existing?.ambiguous });
      const metadata = meta(event, id, 'ws');
      const frame = event.frame;
      sender.enqueue(async () => Buffer.byteLength(frame) <= LIMIT
        ? { ...metadata, direction: 'request', payload, capture: ambiguous ? 'association-unknown' : 'complete' }
        : { ...metadata, direction: 'gap', payload: { reason: 'frame-too-large' }, capture: 'gap' }, metadata,
        () => {}, Math.min(Buffer.byteLength(frame) * 2, LIMIT) + 1024);
    });

    await register('experimental.ws.receive', async event => {
      const state = sockets.get(event.sessionID + '/' + event.kind);
      if (!state) return;
      const payload = jsonOrText(event.frame);
      if (['response.completed', 'response.failed', 'response.cancelled', 'error'].includes(payload?.type)) state.active = false;
      if (state.ambiguous) return;
      const metadata = meta(event, state.id, 'ws');
      const frame = event.frame;
      sender.enqueue(async () => Buffer.byteLength(frame) <= LIMIT
        ? { ...metadata, direction: 'response', payload, capture: 'complete' }
        : { ...metadata, direction: 'gap', payload: { reason: 'frame-too-large' }, capture: 'gap' }, metadata,
        () => {}, Math.min(Buffer.byteLength(frame) * 2, LIMIT) + 1024);
    });

    let cleaned = false;
    return async () => {
      if (!cleaned) { cleaned = true; await Promise.allSettled(registrations.map(r => r.dispose())); }
      await sender.close();
      sockets.clear();
    };
  },
};

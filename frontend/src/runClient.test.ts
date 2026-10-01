import assert from 'node:assert/strict';
import test from 'node:test';
import { observeRun, submitRun, type LiveSocket, type ReplayPage, type SubscriptionTicket } from './runClient';
import { SubmissionStore } from './submissions';
import { attachStream, emptyStream, finalText, reduceStream, upsertMessage, type Message, type StreamEvent } from './streamState';

const flush = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const page = (events: ReplayPage['events'] = [], status = 'running', last = events.at(-1)?.seq ?? 0, more = false): ReplayPage =>
  ({ events, status, last_event_id: last, has_more: more });
const event = (seq: number, text = String(seq)) => ({ seq, data: { type: 'delta', text } });

class Clock {
  now = 0;
  jobs = new Set<{ at: number; callback: () => void }>();
  schedule = (callback: () => void, delay: number) => {
    const job = { at: this.now + delay, callback };
    this.jobs.add(job);
    return () => { this.jobs.delete(job); };
  };
  async advance(ms: number) {
    const target = this.now + ms;
    while (true) {
      const job = [...this.jobs].filter(job => job.at <= target).sort((a, b) => a.at - b.at)[0];
      if (!job) break;
      this.jobs.delete(job); this.now = job.at; job.callback();
      await flush();
    }
    this.now = target;
    await flush();
  }
}

class Socket implements LiveSocket {
  onopen: WebSocket['onopen'] = null;
  onmessage: WebSocket['onmessage'] = null;
  onerror: WebSocket['onerror'] = null;
  onclose: WebSocket['onclose'] = null;
  readyState = 0;
  sent: Record<string, any>[] = [];
  constructor(public url: string, public protocols: string[]) {}
  send = (data: string | ArrayBufferLike | Blob | ArrayBufferView) => { this.sent.push(JSON.parse(String(data))); };
  close = () => { this.readyState = 3; };
  open() { this.readyState = 1; this.onopen?.call(this as unknown as WebSocket, {} as Event); }
  frame(data: unknown) { this.onmessage?.call(this as unknown as WebSocket, { data: JSON.stringify(data) } as MessageEvent); }
  disconnect() { this.readyState = 3; this.onclose?.call(this as unknown as WebSocket, {} as CloseEvent); }
  subscribe(timeout = 300000) {
    this.open();
    this.frame({ type: 'connection_ack', connectionTimeoutMs: timeout });
    this.frame({ type: 'subscribe_success', id: 'run-events' });
  }
  hint(...seqs: number[]) {
    this.frame({ type: 'data', id: 'run-events', event: seqs.map(seq => JSON.stringify({ run_id: 'run', seq })) });
  }
}

function harness(replay: (after: number, signal: AbortSignal) => Promise<Response> | Response = () => json(page()),
  ticketFailure = false) {
  const clock = new Clock();
  const controller = new AbortController();
  const sockets: Socket[] = [];
  const cursors: number[] = [];
  const received: { seq: number; data: StreamEvent }[] = [];
  const terminals: string[] = [];
  const connections: string[] = [];
  const ticketSignals: AbortSignal[] = [];
  let ticketCount = 0;
  const ticket: SubscriptionTicket = { url: 'wss://test/event/realtime', host: 'http.test',
    channel: '/runs/run', token: 'token', expires: 120 };
  const done = observeRun({
    base: '/api/agents/agent/conversations/conversation', runId: 'run', signal: controller.signal,
    onEvent: (data, seq) => received.push({ data, seq }), onTerminal: status => terminals.push(status),
    onConnection: state => connections.push(state),
  }, {
    schedule: clock.schedule, random: () => 1,
    socket: (url, protocols) => { const socket = new Socket(url, protocols); sockets.push(socket); return socket; },
    fetch: async (input, init) => {
      const url = String(input);
      assert.match(url, /^\/api\/agents\/agent\/conversations\/conversation\/runs\/run\//);
      if (url.endsWith('/subscription')) {
        assert.equal(init?.method, 'POST');
        ticketSignals.push(init!.signal!);
        ticketCount++;
        if (ticketFailure) throw new Error('Socket service unavailable');
        return json({ ...ticket, token: `ticket-${ticketCount}` });
      }
      const after = Number(new URL(url, 'https://portal').searchParams.get('after'));
      cursors.push(after);
      return replay(after, init!.signal!);
    },
  }).then(() => undefined, error => error as Error);
  return { clock, controller, sockets, cursors, received, terminals, connections, ticketSignals, done,
    get ticketCount() { return ticketCount; } };
}

test('native AppSync handshake subscribes before replay and validates subscription IDs', async () => {
  const h = harness();
  try {
    await flush();
    const ws = h.sockets[0];
    assert.equal(ws.url, 'wss://test/event/realtime');
    assert.equal(ws.protocols[0], 'aws-appsync-event-ws');
    assert.match(ws.protocols[1], /^header-[A-Za-z0-9_-]+$/);
    assert.deepEqual(JSON.parse(Buffer.from(ws.protocols[1].slice(7), 'base64url').toString()),
      { authorization: 'ticket-1', host: 'http.test' });
    ws.open();
    assert.deepEqual(ws.sent, [{ type: 'connection_init' }]);
    ws.frame({ type: 'subscribe_success', id: 'run-events' });
    assert.deepEqual(h.cursors, []);
    ws.frame({ type: 'connection_ack', connectionTimeoutMs: 300000 });
    assert.deepEqual(ws.sent[1], { id: 'run-events', type: 'subscribe', channel: '/runs/run',
      authorization: { authorization: 'ticket-1', host: 'http.test' } });
    ws.frame({ type: 'subscribe_success', id: 'wrong' });
    assert.deepEqual(h.cursors, []);
    ws.frame({ type: 'subscribe_success', id: 'run-events' });
    await flush();
    assert.deepEqual(h.cursors, [0]);
  } finally { h.controller.abort(); await h.done; }
  assert.equal(h.sockets[0].readyState, 3);
  assert.deepEqual(h.sockets[0].sent.at(-1), { type: 'unsubscribe', id: 'run-events' });
  assert.equal(h.clock.jobs.size, 0);
});

test('duplicate/out-of-order notification hints coalesce into one authoritative replay consumer', async () => {
  let release: (value: Response) => void = () => {};
  let calls = 0;
  const h = harness(() => ++calls === 1 ? new Promise(resolve => { release = resolve; })
    : json(page([event(1, 'duplicate'), event(2, 'B')], 'running', 2)));
  try {
    await flush(); h.sockets[0].subscribe(); await flush();
    h.sockets[0].hint(2, 1, 2, 999);
    await h.clock.advance(5000);
    assert.deepEqual(h.cursors, [0]); // polling and hints cannot start a second in-flight read
    release(json(page([event(1, 'A')]))); await flush();
    assert.deepEqual(h.cursors, [0, 1]);
    assert.deepEqual(h.received.map(e => e.data.text), ['A', 'B']);
    h.sockets[0].hint(1, 2); await flush();
    h.sockets[0].frame({ type: 'data', id: 'run-events', event: [JSON.stringify({ run_id: 'other', seq: 100 })] });
    h.sockets[0].frame({ type: 'data', id: 'wrong', event: [JSON.stringify({ run_id: 'run', seq: 100 })] });
    await flush();
    assert.deepEqual(h.cursors, [0, 1]);
  } finally { h.controller.abort(); await h.done; }
});

test('replay pages advance contiguously, recover gaps on poll, and drain before terminal', async () => {
  let calls = 0;
  const h = harness(after => {
    calls++;
    if (calls === 1) return json(page([event(1), event(3)], 'complete', 4, true));
    if (calls === 2) return json(page([event(3)], 'complete', 4, true));
    if (after === 1) return json(page([event(3), event(2), event(1)], 'complete', 4, true));
    return json(page([{ seq: 4, data: { type: 'complete', text: 'Done' } }], 'complete', 4));
  });
  await flush(); h.sockets[0].subscribe(); await flush();
  assert.deepEqual(h.cursors, [0, 1]);
  assert.deepEqual(h.received.map(e => e.seq), [1]);
  assert.deepEqual(h.terminals, []);
  await h.clock.advance(5000);
  assert.equal(await h.done, undefined);
  assert.deepEqual(h.cursors, [0, 1, 1, 3]);
  assert.deepEqual(h.received.map(e => e.seq), [1, 2, 3, 4]);
  assert.deepEqual(h.terminals, ['complete']);
  assert.equal(h.clock.jobs.size, 0);
});

test('reconnect gets fresh tickets with backoff and retains the one in-flight replay and cursor', async () => {
  let release: (value: Response) => void = () => {};
  let first = true;
  const h = harness(() => {
    if (first) { first = false; return new Promise(resolve => { release = resolve; }); }
    return json(page([event(1, 'duplicate'), event(2)]));
  });
  try {
    await flush(); const old = h.sockets[0]; old.subscribe(); await flush();
    const lateFrame = old.onmessage!;
    old.disconnect();
    await h.clock.advance(999); assert.equal(h.ticketCount, 1);
    await h.clock.advance(1); assert.equal(h.ticketCount, 2);
    const next = h.sockets[1]; next.subscribe(); next.hint(2); await flush();
    assert.deepEqual(h.cursors, [0]);
    assert.equal(old.onmessage, null);
    lateFrame.call(old as unknown as WebSocket, { data: JSON.stringify({ type: 'data', id: 'run-events',
      event: [JSON.stringify({ run_id: 'run', seq: 999 })] }) } as MessageEvent);
    release(json(page([event(1)]))); await flush();
    assert.deepEqual(h.cursors, [0, 1]);
    assert.deepEqual(h.received.map(e => e.seq), [1, 2]);
    assert.deepEqual(next.sent[1].authorization, { authorization: 'ticket-2', host: 'http.test' });
    next.disconnect();
    await h.clock.advance(1999); assert.equal(h.ticketCount, 2);
    await h.clock.advance(1); assert.equal(h.ticketCount, 3);
    assert.equal(h.sockets.filter(ws => ws.readyState !== 3).length, 1);
  } finally { h.controller.abort(); await h.done; }
});

test('five-second polling recovers missing notifications and enforces revocation', async () => {
  let allowed = true;
  let ready = false;
  const h = harness(() => !allowed ? json({ detail: 'Membership revoked' }, 403)
    : json(page(ready ? [event(1)] : [])));
  await flush(); h.sockets[0].subscribe(); await flush();
  ready = true;
  await h.clock.advance(4999); assert.equal(h.received.length, 0);
  await h.clock.advance(1); assert.equal(h.received.length, 1);
  allowed = false;
  await h.clock.advance(5000);
  assert.match((await h.done)!.message, /Membership revoked/);
  assert.equal(h.sockets[0].readyState, 3);
  assert.equal(h.clock.jobs.size, 0);
  const calls = h.cursors.length;
  await h.clock.advance(60000);
  assert.equal(h.cursors.length, calls);
});

test('poll fallback works even when ticket service never connects', async () => {
  const h = harness(() => json(page([{ seq: 1, data: { type: 'interrupted' } }], 'interrupted')), true);
  await flush();
  assert.deepEqual(h.cursors, []);
  await h.clock.advance(5000);
  assert.equal(await h.done, undefined);
  assert.deepEqual(h.terminals, ['interrupted']);
  assert.equal(h.clock.jobs.size, 0);
});

test('abort during replay ignores a late response and cancels sockets, retries and polling', async () => {
  let release: (value: Response) => void = () => {};
  let requestSignal: AbortSignal | undefined;
  const h = harness((_after, signal) => { requestSignal = signal; return new Promise(resolve => { release = resolve; }); });
  await flush(); h.sockets[0].subscribe(); await flush();
  h.sockets[0].disconnect();
  h.controller.abort();
  assert.equal(await h.done, undefined);
  assert.equal(requestSignal!.aborted, true);
  release(json(page([event(1)], 'complete'))); await flush();
  await h.clock.advance(60000);
  assert.deepEqual(h.received, []);
  assert.deepEqual(h.terminals, []);
  assert.equal(h.ticketCount, 1);
  assert.equal(h.clock.jobs.size, 0);
});

test('keep-alive timeout closes the old socket and obtains a new ticket', async () => {
  const h = harness();
  try {
    await flush(); h.sockets[0].subscribe(2000); await flush();
    await h.clock.advance(1500); h.sockets[0].frame({ type: 'ka' });
    await h.clock.advance(1500); assert.equal(h.sockets[0].readyState, 1);
    await h.clock.advance(500); assert.equal(h.sockets[0].readyState, 3);
    await h.clock.advance(1000); assert.equal(h.ticketCount, 2);
  } finally { h.controller.abort(); await h.done; }
});

test('an oversized keep-alive timeout from the server is capped at ten minutes', async () => {
  const h = harness();
  try {
    await flush(); h.sockets[0].subscribe(Number.MAX_SAFE_INTEGER); await flush();
    await h.clock.advance(599000); assert.equal(h.sockets[0].readyState, 1);
    await h.clock.advance(1000); assert.equal(h.sockets[0].readyState, 3);
  } finally { h.controller.abort(); await h.done; }
});

test('terminal replay after reload replaces history with accumulated final chunks exactly once', async () => {
  const h = harness(() => json(page([
    event(1, 'draft'), { seq: 2, data: { type: 'final_chunk', text: 'Final ' } },
    { seq: 3, data: { type: 'final_chunk', text: 'answer' } },
    { seq: 4, data: { type: 'complete', text: '' } },
  ], 'complete')));
  await flush(); h.sockets[0].subscribe(); await h.done;
  let view = attachStream(emptyStream(), 'run', 'connecting');
  let messages: Message[] = [{ role: 'user', run_id: 'run', text: 'Question' },
    { role: 'assistant', run_id: 'run', text: 'Final answer' }];
  for (const event of h.received) {
    view = reduceStream(view, event.data, event.seq);
    if (event.data.type === 'complete') messages = upsertMessage(messages,
      { role: 'assistant', run_id: 'run', text: finalText(view, event.data) });
  }
  assert.equal(messages.length, 2);
  assert.equal(messages[1].text, 'Final answer');
});

test('abort while obtaining a subscription ticket cannot create a late socket', async () => {
  const controller = new AbortController();
  const clock = new Clock();
  let release: (response: Response) => void = () => {};
  let signal: AbortSignal | undefined;
  let socketCount = 0;
  const done = observeRun({ base: '/api/agents/a/conversations/c', runId: 'run', signal: controller.signal,
    onEvent: () => assert.fail('Late event'), onTerminal: () => assert.fail('Late terminal'), onConnection: () => {},
  }, {
    schedule: clock.schedule,
    fetch: async (_input, init) => { signal = init!.signal!; return new Promise(resolve => { release = resolve; }); },
    socket: (url, protocols) => { socketCount++; return new Socket(url, protocols); },
  });
  controller.abort();
  await done;
  assert.equal(signal!.aborted, true);
  release(json({ url: 'wss://late', host: 'late', channel: '/runs/run', token: 'late', expires: 120 }));
  await flush(); await clock.advance(60000);
  assert.equal(socketCount, 0);
  assert.equal(clock.jobs.size, 0);
});

test('subscription failure retries with a new ticket while transient replay failure recovers by poll', async () => {
  let calls = 0;
  const h = harness(() => ++calls === 1 ? json({ detail: 'Unavailable' }, 503)
    : json(page([{ seq: 1, data: { type: 'complete', text: 'Done' } }], 'complete')));
  await flush();
  h.sockets[0].open();
  h.sockets[0].frame({ type: 'connection_ack', connectionTimeoutMs: 300000 });
  h.sockets[0].frame({ type: 'subscribe_error', id: 'run-events', errors: [{ message: 'Expired ticket' }] });
  await h.clock.advance(1000);
  assert.equal(h.ticketCount, 2);
  h.sockets[1].subscribe(); await flush();
  assert.deepEqual(h.cursors, [0]);
  assert.deepEqual(h.terminals, []);
  await h.clock.advance(4000);
  assert.equal(await h.done, undefined);
  assert.deepEqual(h.cursors, [0, 0]);
  assert.deepEqual(h.terminals, ['complete']);
});

test('command retries send the identical idempotency header and body after ambiguous responses', async () => {
  const store = new SubmissionStore();
  const attempts: RequestInit[] = [];
  const fetcher: typeof fetch = async (url, init) => {
    assert.equal(url, '/api/agents/a/conversations/c/runs');
    attempts.push(init!);
    if (attempts.length === 1) throw new TypeError('Network response lost after acceptance');
    if (attempts.length === 2) return json({}); // Invalid success cannot discard the retry identity.
    return json({ id: 'same-run', status: 'complete', last_event_id: 42 });
  };
  const send = () => submitRun('/api/agents/a/conversations/c', store.prepare('user', 'a', 'c', 'Question'),
    new AbortController().signal, fetcher);
  await assert.rejects(send, /Network response lost/);
  await assert.rejects(send, /submission response was incomplete/);
  assert.equal((await send()).id, 'same-run');
  assert.equal(new Set(attempts.map(attempt => new Headers(attempt.headers).get('Idempotency-Key'))).size, 1);
  assert.deepEqual(attempts.map(attempt => JSON.parse(String(attempt.body))),
    [{ message: 'Question' }, { message: 'Question' }, { message: 'Question' }]);
});

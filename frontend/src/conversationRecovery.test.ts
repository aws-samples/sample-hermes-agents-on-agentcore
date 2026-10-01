import assert from 'node:assert/strict';
import test from 'node:test';
import { loadHistory, recoverConversation, recoverNextRun } from './conversationRecovery';
import { attachStream, emptyStream, finalText, reduceStream, upsertMessage } from './streamState';
import type { Run } from './runClient';

const base = '/api/agents/agent/conversations/session';
const first: Run = { id: 'first', status: 'complete', last_event_id: 3 };
const second: Run = { id: 'second', status: 'running', last_event_id: 1 };
const question = (id: string) => ({ role: 'user', run_id: id, text: `Question ${id}` });
const answer = (id: string) => ({ role: 'assistant', run_id: id, text: `Answer ${id}` });

function requests(steps: { path: string; body: unknown; before?: () => void }[]) {
  const paths: string[] = [];
  const fetcher: typeof fetch = async (input, init) => {
    const next = steps[paths.length];
    assert.ok(next, 'No unexpected recovery requests');
    paths.push(String(input));
    assert.equal(input, base + next.path);
    assert.ok(init?.signal);
    next.before?.();
    const body = next.path.startsWith('/messages') && Array.isArray(next.body)
      ? { messages: next.body, next_cursor: null } : next.body;
    return new Response(JSON.stringify(body));
  };
  return { paths, fetcher };
}

for (const changeBeforeHistory of [false, true]) {
  test(`retries a changed run pointer when new turn starts ${changeBeforeHistory ? 'before' : 'after'} history is read`, async () => {
    const previous = [question('first'), answer('first')];
    const current = [...previous, question('second')];
    const h = requests([
      { path: '/runs/active', body: first },
      { path: '/messages', body: changeBeforeHistory ? current : previous },
      { path: '/runs/active', body: second },
      { path: '/runs/active', body: second },
      { path: '/messages', body: current },
      { path: '/runs/active', body: second },
    ]);
    const snapshot = await recoverConversation(base, new AbortController().signal, h.fetcher);
    assert.deepEqual(snapshot, { history: current, run: second });
    assert.equal(h.paths.length, 6);
  });
}

test('empty conversation becoming active retries and includes the first user message', async () => {
  const h = requests([
    { path: '/runs/active', body: null },
    { path: '/messages', body: [] },
    { path: '/runs/active', body: second },
    { path: '/runs/active', body: second },
    { path: '/messages', body: [question('second')] },
    { path: '/runs/active', body: second },
  ]);
  const snapshot = await recoverConversation(base, new AbortController().signal, h.fetcher);
  assert.deepEqual(snapshot.history, [question('second')]);
  assert.equal(snapshot.run?.id, second.id);
});

test('same-run completion during history read merges final replay without duplicate messages', async () => {
  const completed = { ...second, status: 'complete', last_event_id: 2 };
  for (const terminalInHistory of [false, true]) {
    const h = requests([
      { path: '/runs/active', body: second },
      { path: '/messages', body: terminalInHistory ? [question('second'), answer('second')] : [question('second')] },
      { path: '/runs/active', body: completed },
    ]);
    const snapshot = await recoverConversation(base, new AbortController().signal, h.fetcher);
    assert.deepEqual(snapshot.run, completed);
    let view = attachStream(emptyStream(), second.id, 'connecting');
    view = reduceStream(view, { type: 'final_chunk', text: 'Answer second' }, 1);
    view = reduceStream(view, { type: 'complete', text: '' }, 2);
    assert.deepEqual(upsertMessage(snapshot.history, {
      role: 'assistant', run_id: second.id, text: finalText(view),
    }), [question('second'), answer('second')]);
  }
});

test('missing selected-run input never yields an attachable recovery snapshot', async () => {
  const h = requests(Array.from({ length: 3 }, () => [
    { path: '/runs/active', body: second },
    { path: '/messages', body: [question('first')] },
    { path: '/runs/active', body: second },
  ]).flat());
  await assert.rejects(recoverConversation(base, new AbortController().signal, h.fetcher), /Reopen/);
  assert.equal(h.paths.length, 9);
});

test('continuously changing pointer has bounded retries instead of accepting mismatched history', async () => {
  const h = requests(Array.from({ length: 3 }, (_, index) => [
    { path: '/runs/active', body: { ...second, id: String(index) } },
    { path: '/messages', body: [question(String(index))] },
    { path: '/runs/active', body: { ...second, id: String(index + 1) } },
  ]).flat());
  await assert.rejects(recoverConversation(base, new AbortController().signal, h.fetcher), /changed while loading/);
  assert.equal(h.paths.length, 9);
});

test('abort during history recovery cannot return a stale snapshot or start more reads', async () => {
  const controller = new AbortController();
  const h = requests([
    { path: '/runs/active', body: second },
    { path: '/messages', body: [question('second')], before: () => controller.abort() },
  ]);
  await assert.rejects(recoverConversation(base, controller.signal, h.fetcher), { name: 'AbortError' });
  assert.equal(h.paths.length, 2);
});

test('a newer turn after replay is recovered before the composer can be enabled', async () => {
  const h = requests([
    { path: '/runs/active', body: second },
    { path: '/runs/active', body: second },
    { path: '/messages', body: [question('first'), answer('first'), question('second')] },
    { path: '/runs/active', body: second },
  ]);
  const next = await recoverNextRun(base, first.id, new AbortController().signal, h.fetcher);
  assert.equal(next?.run?.id, second.id);
  assert.equal(next?.history.at(-1)?.text, 'Question second');
});

test('finished run still latest does not loop its terminal replay', async () => {
  const h = requests([{ path: '/runs/active', body: first }]);
  assert.equal(await recoverNextRun(base, first.id, new AbortController().signal, h.fetcher), null);
  assert.equal(h.paths.length, 1);
});

test('history paging loads every message in order and only then validates the latest run', async () => {
  const h = requests([
    { path: '/runs/active', body: second },
    { path: '/messages', body: { messages: [question('first')], next_cursor: 'page-2' } },
    { path: '/messages?cursor=page-2', body: { messages: [answer('first')], next_cursor: 'page-3' } },
    { path: '/messages?cursor=page-3', body: { messages: [question('second')], next_cursor: null } },
    { path: '/runs/active', body: second },
  ]);
  const snapshot = await recoverConversation(base, new AbortController().signal, h.fetcher);
  assert.deepEqual(snapshot.history, [question('first'), answer('first'), question('second')]);
  assert.equal(h.paths.length, 5);
});

test('a new run during paginated recovery discards the partial snapshot and restarts at page one', async () => {
  const h = requests([
    { path: '/runs/active', body: first },
    { path: '/messages', body: { messages: [question('first')], next_cursor: 'page-2' } },
    { path: '/messages?cursor=page-2', body: { messages: [answer('first'), question('second')], next_cursor: null } },
    { path: '/runs/active', body: second },
    { path: '/runs/active', body: second },
    { path: '/messages', body: { messages: [question('first'), answer('first')], next_cursor: 'retry-page-2' } },
    { path: '/messages?cursor=retry-page-2', body: { messages: [question('second')], next_cursor: null } },
    { path: '/runs/active', body: second },
  ]);
  const snapshot = await recoverConversation(base, new AbortController().signal, h.fetcher);
  assert.deepEqual(snapshot, { run: second, history: [question('first'), answer('first'), question('second')] });
  assert.equal(h.paths.length, 8);
});

test('pagination rejects repeated cursors without returning incomplete history', async () => {
  const h = requests([
    { path: '/messages', body: { messages: [question('first')], next_cursor: 'same' } },
    { path: '/messages?cursor=same', body: { messages: [answer('first')], next_cursor: 'same' } },
  ]);
  await assert.rejects(loadHistory(base, new AbortController().signal, h.fetcher), /Invalid history page/);
  assert.equal(h.paths.length, 2);
});

test('abort in a later history page does not return earlier pages as complete history', async () => {
  const controller = new AbortController();
  const h = requests([
    { path: '/messages', body: { messages: [question('first')], next_cursor: 'next' } },
    { path: '/messages?cursor=next', body: { messages: [answer('first')], next_cursor: null }, before: () => controller.abort() },
  ]);
  await assert.rejects(loadHistory(base, controller.signal, h.fetcher), { name: 'AbortError' });
  assert.equal(h.paths.length, 2);
});

import assert from 'node:assert/strict';
import test from 'node:test';
import { attachStream, emptyStream, finalText, reduceStream, upsertMessage, type Message } from './streamState';

test('keeps only the latest completed interim segment', () => {
  let state = reduceStream(emptyStream(), { type: 'delta', text: 'Planning v1' });
  state = reduceStream(state, { type: 'segment_end' });
  assert.equal(state.interim, 'Planning v1');
  state = reduceStream(state, { type: 'delta', text: 'Planning v2' });
  state = reduceStream(state, { type: 'segment_end' });
  assert.equal(state.interim, 'Planning v2');
});

test('tracks replay cursors and ignores duplicate events', () => {
  let state = attachStream(emptyStream(), 'run-1', 'connecting');
  state = reduceStream(state, { type: 'delta', text: 'First' }, 1);
  state = reduceStream(state, { type: 'delta', text: ' duplicate' }, 1);

  assert.equal(state.runId, 'run-1');
  assert.equal(state.lastEventId, 1);
  assert.equal(state.current, 'First');
});

test('resets the replay cursor when attaching a different run', () => {
  let state = attachStream(emptyStream(), 'run-1', 'connecting');
  state = reduceStream(state, { type: 'delta', text: 'First' }, 1);
  state = attachStream(state, 'run-2', 'reconnecting');

  assert.deepEqual(state, {
    runId: 'run-2', lastEventId: 0, connection: 'reconnecting', current: '', interim: '', finalChunks: '',
  });
});

test('old duplicates beyond 256 events and future gaps cannot change text or regress the cursor', () => {
  let state = emptyStream();
  for (let seq = 1; seq <= 600; seq++) state = reduceStream(state, { type: 'delta', text: '.' }, seq);
  const previous = state;
  state = reduceStream(state, { type: 'delta', text: 'OLD' }, 1);
  state = reduceStream(state, { type: 'delta', text: 'GAP' }, 602);
  assert.equal(state, previous);
  assert.equal(state.current.length, 600);
  state = reduceStream(state, { type: 'delta', text: 'next' }, 601);
  assert.equal(state.lastEventId, 601);
  assert.equal(state.current, '.'.repeat(600) + 'next');
});

test('long final chunks replace streamed draft and survive an empty complete event', () => {
  let state = reduceStream(emptyStream(), { type: 'delta', text: 'Draft' }, 1);
  state = reduceStream(state, { type: 'segment_end' }, 2);
  const first = 'Long final response. '.repeat(20000);
  state = reduceStream(state, { type: 'final_chunk', text: first }, 3);
  state = reduceStream(state, { type: 'final_chunk', text: 'The end.' }, 4);
  state = reduceStream(state, { type: 'final_chunk', text: first }, 3);
  state = reduceStream(state, { type: 'complete', text: '' }, 5);
  assert.equal(finalText(state, { type: 'complete', text: '' }), first + 'The end.');
  assert.equal(finalText(state, { type: 'complete', text: 'Authoritative' }), 'Authoritative');
});

test('terminal replay replaces the keyed history assistant in place and retries replace the user', () => {
  const history: Message[] = [
    { role: 'user', text: 'Earlier legacy message' },
    { role: 'user', text: 'Question', run_id: 'run-1' },
    { role: 'assistant', text: 'History answer', run_id: 'run-1' },
  ];
  const final = { role: 'assistant', text: 'Final answer', run_id: 'run-1' };
  let messages = upsertMessage(history, final);
  messages = upsertMessage(messages, final);
  messages = upsertMessage(messages, { role: 'user', text: 'Question', run_id: 'run-1' });
  assert.equal(messages.length, 3);
  assert.deepEqual(messages[2], final);
  // The opposite race (history snapshot before terminal commit) also yields one assistant.
  assert.deepEqual(upsertMessage(history.slice(0, 2), final), messages);
});

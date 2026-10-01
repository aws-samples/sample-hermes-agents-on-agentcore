import assert from 'node:assert/strict';
import test from 'node:test';
import { selectionKey, SubmissionStore } from './submissions';

function storage() {
  const values = new Map<string, string>();
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value); },
    removeItem: (key: string) => { values.delete(key); },
  };
}

test('ambiguous submissions reuse their key on retry and reload; later identical turns get a new key', () => {
  const persisted = storage();
  const store = new SubmissionStore(persisted);
  const submitted = store.prepare('user', 'agent', 'conversation', 'Hello');
  assert.deepEqual(store.prepare('user', 'agent', 'conversation', 'Hello'), submitted);
  const reloaded = new SubmissionStore(persisted);
  assert.deepEqual(reloaded.prepare('user', 'agent', 'conversation', 'Hello'), submitted);
  assert.throws(() => reloaded.prepare('user', 'agent', 'conversation', 'Edited'), /Retry the previous message/);
  reloaded.clear('user', 'agent', 'conversation');
  assert.notEqual(reloaded.prepare('user', 'agent', 'conversation', 'Hello').key, submitted.key);
});

test('pending submissions and remembered selections are isolated by user and conversation', () => {
  const store = new SubmissionStore(storage());
  const first = store.prepare('one', 'agent', 'conversation', 'Hello');
  assert.notEqual(store.prepare('two', 'agent', 'conversation', 'Hello').key, first.key);
  assert.notEqual(store.prepare('one', 'agent', 'other', 'Hello').key, first.key);
  assert.notEqual(selectionKey('one', 'agent'), selectionKey('two', 'agent'));
  assert.notEqual(selectionKey('one'), selectionKey('one', 'agent'));
});

test('disabled storage retains retry identity in memory', () => {
  const denied = () => { throw new Error('Storage disabled'); };
  const store = new SubmissionStore({ getItem: denied, setItem: denied, removeItem: denied });
  const first = store.prepare('user', 'agent', 'conversation', 'Hello');
  assert.deepEqual(store.prepare('user', 'agent', 'conversation', 'Hello'), first);
  store.clear('user', 'agent', 'conversation');
  assert.notEqual(store.prepare('user', 'agent', 'conversation', 'Hello').key, first.key);
});

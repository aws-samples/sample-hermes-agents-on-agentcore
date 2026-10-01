import assert from 'node:assert/strict';
import test from 'node:test';
import { DOWNLOAD_CHUNK_BYTES, MAX_DOWNLOAD_BYTES, fetchDownload, parseContentRange } from './download';

const url = '/api/agents/agent/download?path=folder%2Freport.bin';
const bytes = (length: number, value = 0) => new Uint8Array(length).fill(value).buffer;
const partial = (body: string | ArrayBuffer, start: number, total: number, overrides: Record<string, string> = {}) => {
  const size = typeof body === 'string' ? new TextEncoder().encode(body).length : body.byteLength;
  return new Response(body, { status: 206, headers: {
    'Content-Range': `bytes ${start}-${start + size - 1}/${total}`, ETag: '"version-1"',
    'Content-Type': 'application/octet-stream', 'Content-Length': String(size), ...overrides,
  } });
};

test('Content-Range parses inclusive byte offsets and rejects malformed, unknown or unsafe lengths', () => {
  assert.deepEqual(parseContentRange('bytes 0-0/1'), { start: 0, end: 0, total: 1 });
  assert.deepEqual(parseContentRange('bytes 2097152-4194303/33554432'),
    { start: 2097152, end: 4194303, total: 33554432 });
  for (const header of [null, '', 'bytes */0', 'bytes 0-1/*', 'items 0-1/2', 'bytes -1-1/2',
    'bytes 2-1/3', 'bytes 0-2/2', 'bytes 0-0/0', 'bytes 0-1/9007199254740992',
    'bytes 0-1/2, 2-3/4', 'bytes 0-1/2 trailing', 'bytes 0.5-1/2']) {
    assert.throws(() => parseContentRange(header), /invalid/i, String(header));
  }
});

test('sequential range assembly preserves binary content, honors clamped ends, and pins If-Range', async () => {
  const requests: string[] = [];
  let firstBodyFinished = false;
  const total = DOWNLOAD_CHUNK_BYTES + 4;
  const blob = await fetchDownload(url, { fetcher: async (input, init) => {
    assert.equal(input, url);
    assert.equal(init?.cache, 'no-store');
    const headers = new Headers(init?.headers);
    requests.push(headers.get('Range')!);
    if (requests.length === 1) {
      assert.equal(headers.get('If-Range'), null);
      const response = partial(bytes(DOWNLOAD_CHUNK_BYTES, 255), 0, total);
      // The next request must wait for the entire first response body.
      return new Response(new ReadableStream({
        async start(controller) {
          await Promise.resolve();
          controller.enqueue(new Uint8Array(await response.arrayBuffer()));
          firstBodyFinished = true;
          controller.close();
        },
      }), { status: response.status, headers: response.headers });
    }
    assert.equal(firstBodyFinished, true);
    assert.equal(headers.get('If-Range'), '"version-1"');
    if (requests.length === 2) return partial(new Uint8Array([0, 128]).buffer, DOWNLOAD_CHUNK_BYTES, total);
    assert.equal(requests.length, 3);
    return partial(new Uint8Array([254, 1]).buffer, DOWNLOAD_CHUNK_BYTES + 2, total);
  } });
  assert.deepEqual(requests, [`bytes=0-${DOWNLOAD_CHUNK_BYTES - 1}`,
    `bytes=${DOWNLOAD_CHUNK_BYTES}-${total - 1}`, `bytes=${DOWNLOAD_CHUNK_BYTES + 2}-${total - 1}`]);
  assert.equal(blob.size, total);
  assert.equal(blob.type, 'application/octet-stream');
  const contents = new Uint8Array(await blob.arrayBuffer());
  assert.ok(contents.subarray(0, DOWNLOAD_CHUNK_BYTES).every(byte => byte === 255));
  assert.deepEqual([...contents.subarray(DOWNLOAD_CHUNK_BYTES)], [0, 128, 254, 1]);
});

test('accepts a complete small 200 response and retries an initial 416 without Range for an empty file', async () => {
  const small = await fetchDownload(url, { fetcher: async () => new Response('hello', {
    headers: { 'Content-Type': 'text/plain', 'Content-Length': '5' },
  }) });
  assert.equal(await small.text(), 'hello');
  assert.equal(small.type, 'text/plain');
  let calls = 0;
  const empty = await fetchDownload(url, { fetcher: async (_input, init) => {
    calls++;
    const headers = new Headers(init?.headers);
    if (calls === 1) {
      assert.equal(headers.get('Range'), `bytes=0-${DOWNLOAD_CHUNK_BYTES - 1}`);
      return new Response('', { status: 416 });
    }
    assert.equal(calls, 2);
    assert.equal(headers.get('Range'), null);
    assert.equal(headers.get('If-Range'), null);
    return new Response('', { headers: { 'Content-Length': '0', ETag: '"empty"' } });
  } });
  assert.equal(empty.size, 0);
  assert.equal(calls, 2);
});

test('empty-file fallback is attempted only once and never after accepting chunks', async () => {
  let initialCalls = 0;
  await assert.rejects(fetchDownload(url, { fetcher: async () => {
    initialCalls++;
    return new Response('', { status: 416 });
  } }), /byte range is unavailable/);
  assert.equal(initialCalls, 2);
  let partialCalls = 0;
  await assert.rejects(fetchDownload(url, { fetcher: async () => ++partialCalls === 1
    ? partial('ab', 0, 4) : new Response('', { status: 416 }) }), /byte range is unavailable/);
  assert.equal(partialCalls, 2);
});

test('rejects version changes, ignored ranges, gaps, overlaps, changed totals and truncated chunks', async t => {
  const cases: [string, () => Response][] = [
    ['precondition failure', () => new Response('', { status: 412 })],
    ['changed ETag', () => partial('cd', 2, 4, { ETag: '"version-2"' })],
    ['missing ETag', () => { const response = partial('cd', 2, 4); response.headers.delete('ETag'); return response; }],
    ['ignored range', () => new Response('abcd', { headers: { ETag: '"version-1"' } })],
    ['gap', () => partial('d', 3, 4)],
    ['overlap', () => partial('bc', 1, 4)],
    ['changed total', () => partial('cd', 2, 5)],
    ['end beyond request', () => partial('cde', 2, 4)],
    ['short body', () => partial('c', 2, 4, { 'Content-Range': 'bytes 2-3/4', 'Content-Length': '2' })],
    ['long body', () => partial('cde', 2, 4, { 'Content-Range': 'bytes 2-3/4', 'Content-Length': '2' })],
  ];
  for (const [name, response] of cases) await t.test(name, async () => {
    let calls = 0;
    await assert.rejects(fetchDownload(url, { fetcher: async (_input, init) => {
      if (++calls === 1) return partial('ab', 0, 4);
      assert.equal(new Headers(init?.headers).get('If-Range'), '"version-1"');
      return response();
    } }));
    assert.equal(calls, 2);
  });
});

test('partial downloads require strong ETags and a valid first range', async () => {
  for (const etag of ['', 'W/"version-1"', 'unquoted']) {
    await assert.rejects(fetchDownload(url, { fetcher: async () => partial('a', 0, 2, { ETag: etag }) }), /strong ETag/);
  }
  await assert.rejects(fetchDownload(url, { fetcher: async () => partial('a', 1, 2) }), /unexpected byte range/);
  await assert.rejects(fetchDownload(url, { fetcher: async () => partial('a', 0, 2, { 'Content-Range': 'bytes 0-0/*' }) }), /Content-Range/);
});

test('enforces the 32 MiB total before reading and rejects chunks larger than 2 MiB', async () => {
  let cancelled = false;
  await assert.rejects(fetchDownload(url, { fetcher: async () => new Response(new ReadableStream({
    cancel() { cancelled = true; },
  }), { status: 206, headers: { 'Content-Range': `bytes 0-1/${MAX_DOWNLOAD_BYTES + 1}`, ETag: '"version-1"' } }) }), /32 MiB/);
  assert.equal(cancelled, true);
  await assert.rejects(fetchDownload(url, { fetcher: async () => new Response(bytes(DOWNLOAD_CHUNK_BYTES + 1)) }), /more bytes than requested/);
  await assert.rejects(fetchDownload(url, { fetcher: async () => new Response('', {
    headers: { 'Content-Length': String(DOWNLOAD_CHUNK_BYTES + 1) },
  }) }), /invalid chunk length/);
});

test('accepts exactly 32 MiB through sixteen bounded sequential requests', async () => {
  let calls = 0;
  const blob = await fetchDownload(url, { fetcher: async (_input, init) => {
    const start = calls++ * DOWNLOAD_CHUNK_BYTES;
    const headers = new Headers(init?.headers);
    assert.equal(headers.get('Range'), `bytes=${start}-${start + DOWNLOAD_CHUNK_BYTES - 1}`);
    return partial(bytes(DOWNLOAD_CHUNK_BYTES, calls), start, MAX_DOWNLOAD_BYTES);
  } });
  assert.equal(calls, 16);
  assert.equal(blob.size, MAX_DOWNLOAD_BYTES);
  assert.deepEqual([...new Uint8Array(await blob.slice(-2).arrayBuffer())], [16, 16]);
});

test('aborting a download discards partial bytes and prevents further requests', async () => {
  const controller = new AbortController();
  let calls = 0;
  await assert.rejects(fetchDownload(url, { signal: controller.signal, fetcher: async (_input, init) => {
    calls++;
    assert.equal(init?.signal, controller.signal);
    controller.abort();
    return partial('ab', 0, 4);
  } }), { name: 'AbortError' });
  assert.equal(calls, 1);
});

test('HTTP errors and network failures reject instead of returning partial files', async () => {
  await assert.rejects(fetchDownload(url, { fetcher: async () => new Response(JSON.stringify({ detail: 'Membership revoked' }),
    { status: 403, headers: { 'Content-Type': 'application/json' } }) }), /Membership revoked/);
  let calls = 0;
  await assert.rejects(fetchDownload(url, { fetcher: async () => {
    if (++calls === 1) return partial('ab', 0, 4);
    throw new TypeError('Network unavailable');
  } }), /Network unavailable/);
  assert.equal(calls, 2);
});

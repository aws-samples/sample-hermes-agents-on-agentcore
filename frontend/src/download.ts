export const DOWNLOAD_CHUNK_BYTES = 2 * 1024 * 1024;
export const MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024;

export type ContentRange = { start: number; end: number; total: number };

export function parseContentRange(header: string | null): ContentRange {
  const match = /^bytes (\d+)-(\d+)\/(\d+)$/i.exec(header ?? '');
  if (!match) throw new Error('Download returned an invalid Content-Range header.');
  const [start, end, total] = match.slice(1).map(Number);
  if (![start, end, total].every(Number.isSafeInteger) || start < 0 || end < start || end >= total) {
    throw new Error('Download returned an invalid byte range.');
  }
  return { start, end, total };
}

async function downloadError(response: Response): Promise<Error> {
  if (response.status === 412) return new Error('File changed during download. Please download it again.');
  if (response.status === 416) return new Error('File changed or its byte range is unavailable. Please download it again.');
  const detail = await response.json().catch(() => ({}));
  return new Error(typeof detail?.detail === 'string' ? detail.detail : `Download failed (HTTP ${response.status}).`);
}

async function readChunk(response: Response, expected?: number): Promise<Blob> {
  const lengthHeader = response.headers.get('Content-Length');
  if (lengthHeader !== null && !response.headers.has('Content-Encoding')) {
    const length = Number(lengthHeader);
    if (!/^\d+$/.test(lengthHeader) || !Number.isSafeInteger(length) || length > DOWNLOAD_CHUNK_BYTES
      || (expected !== undefined && length !== expected)) {
      throw new Error('Download returned an invalid chunk length.');
    }
    expected ??= length;
  }
  const parts: BlobPart[] = [];
  let size = 0;
  const reader = response.body?.getReader();
  if (reader) {
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > DOWNLOAD_CHUNK_BYTES || (expected !== undefined && size > expected)) {
          throw new Error('Download returned more bytes than requested.');
        }
        parts.push(value);
      }
    } finally { reader.releaseLock(); }
  }
  if (expected !== undefined && size !== expected) throw new Error('Download was truncated. Please try again.');
  return new Blob(parts);
}

/** Fetch one version, sequentially, without ever assembling unvalidated or overlapping chunks. */
export async function fetchDownload(url: string, options: { signal?: AbortSignal; fetcher?: typeof fetch } = {}): Promise<Blob> {
  const { signal, fetcher = fetch } = options;
  const chunks: Blob[] = [];
  let offset = 0;
  let total: number | undefined;
  let etag: string | undefined;
  let contentType = 'application/octet-stream';
  while (true) {
    signal?.throwIfAborted();
    const end = Math.min(offset + DOWNLOAD_CHUNK_BYTES, total ?? MAX_DOWNLOAD_BYTES) - 1;
    const headers: Record<string, string> = { Range: `bytes=${offset}-${end}` };
    if (etag) headers['If-Range'] = etag;
    let response = await fetcher(url, { headers, signal, cache: 'no-store' });
    try {
      // Empty files reject all ranges. Retry once without Range before any bytes are accepted.
      if (response.status === 416 && offset === 0) {
        await response.body?.cancel();
        response = await fetcher(url, { signal, cache: 'no-store' });
        if (response.status !== 200) throw await downloadError(response);
      }
      if (response.status === 200) {
        if (offset !== 0) throw new Error('File changed or the server ignored a byte range. Please download it again.');
        const body = await readChunk(response);
        signal?.throwIfAborted();
        return new Blob([body], { type: response.headers.get('Content-Type') || contentType });
      }
      if (response.status !== 206) throw await downloadError(response);
      const range = parseContentRange(response.headers.get('Content-Range'));
      if (range.total > MAX_DOWNLOAD_BYTES) throw new Error('Files larger than 32 MiB cannot be downloaded.');
      if (range.start !== offset || range.end > end || (total !== undefined && range.total !== total)) {
        throw new Error('File changed or the server returned an unexpected byte range. Please download it again.');
      }
      const nextEtag = response.headers.get('ETag');
      // If-Range requires a strong validator. A missing/weak validator cannot pin the file version.
      if (!nextEtag || !/^"[\x21\x23-\x7e\x80-\xff]*"$/.test(nextEtag)) {
        throw new Error('Download is missing a strong ETag; the file version cannot be verified.');
      }
      if (etag !== undefined && nextEtag !== etag) throw new Error('File changed during download. Please download it again.');
      if (offset === 0) contentType = response.headers.get('Content-Type') || contentType;
      etag = nextEtag;
      total = range.total;
      const chunk = await readChunk(response, range.end - range.start + 1);
      signal?.throwIfAborted();
      chunks.push(chunk);
      offset = range.end + 1;
      if (offset === total) return new Blob(chunks, { type: contentType });
    } catch (error) {
      await response.body?.cancel().catch(() => {});
      throw error;
    }
  }
}

export function saveDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  try { link.click(); }
  finally {
    link.remove();
    // Let the browser begin consuming the Blob before releasing its object URL.
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}

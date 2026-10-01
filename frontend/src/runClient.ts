import type { ConnectionState, StreamEvent } from './streamState';

export type Run = { id: string; status: string; last_event_id: number };
export type ReplayPage = {
  events: { seq: number; data: StreamEvent }[];
  last_event_id: number;
  status: string;
  has_more: boolean;
};
export type SubscriptionTicket = { url: string; host: string; channel: string; token: string; expires: number };
export const isTerminal = (status: string) => ['complete', 'partial', 'failed', 'interrupted'].includes(status);

export class HttpError extends Error {
  constructor(public status: number, message: string) { super(message); }
  get permanent() { return this.status >= 400 && this.status < 500 && ![408, 429].includes(this.status); }
}

export async function responseJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new HttpError(response.status, typeof detail.detail === 'string' ? detail.detail : 'Request failed. Please retry.');
  }
  return response.json();
}

export async function submitRun(base: string, submission: { message: string; key: string }, signal: AbortSignal,
  fetcher: typeof fetch = fetch): Promise<Run> {
  const run = await responseJson<Run>(await fetcher(`${base}/runs`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': submission.key },
    body: JSON.stringify({ message: submission.message }), signal,
  }));
  // A malformed success is ambiguous too: callers must retain the submission key.
  if (!run || typeof run.id !== 'string' || !run.id || typeof run.status !== 'string'
    || !Number.isSafeInteger(run.last_event_id) || run.last_event_id < 0) {
    throw new Error('The submission response was incomplete. Retry the same message to recover it.');
  }
  return run;
}

// https://docs.aws.amazon.com/appsync/latest/eventapi/event-api-websocket-protocol.html
export function appsyncProtocols(ticket: SubscriptionTicket): string[] {
  const bytes = new TextEncoder().encode(JSON.stringify({ authorization: ticket.token, host: ticket.host }));
  const encoded = btoa(Array.from(bytes, byte => String.fromCharCode(byte)).join(''))
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  return ['aws-appsync-event-ws', `header-${encoded}`];
}

export type LiveSocket = Pick<WebSocket, 'send' | 'close' | 'onopen' | 'onmessage' | 'onerror' | 'onclose' | 'readyState'>;
type Schedule = (callback: () => void, delay: number) => () => void;
const MIN_KEEP_ALIVE_MS = 1000;
const MAX_KEEP_ALIVE_MS = 600000;
type Dependencies = {
  fetch?: typeof fetch;
  socket?: (url: string, protocols: string[]) => LiveSocket;
  schedule?: Schedule;
  random?: () => number;
};
type ObserverOptions = {
  base: string;
  runId: string;
  signal: AbortSignal;
  after?: number;
  onEvent: (data: StreamEvent, seq: number) => void;
  onConnection: (state: ConnectionState) => void;
  onTerminal: (status: string) => void;
};

/** One replay pump for the entire attachment; reconnects only replace the notification socket. */
export function observeRun(options: ObserverOptions, dependencies: Dependencies = {}): Promise<void> {
  const fetcher = dependencies.fetch ?? fetch;
  const makeSocket = dependencies.socket ?? ((url, protocols) => new WebSocket(url, protocols));
  const schedule: Schedule = dependencies.schedule ?? ((callback, delay) => {
    const timer = setTimeout(callback, delay);
    return () => clearTimeout(timer);
  });
  const random = dependencies.random ?? Math.random;
  return new Promise((resolve, reject) => {
    const controller = new AbortController();
    let stopped = false;
    let cursor = options.after ?? 0;
    let pumping = false;
    let dirty = false;
    let attempt = 0;
    let cancelPoll = () => {};
    let cancelRetry = () => {};
    let closeSocket = () => {};

    function finish(error?: unknown) {
      if (stopped) return;
      stopped = true;
      controller.abort();
      cancelPoll(); cancelRetry(); closeSocket();
      options.signal.removeEventListener('abort', aborted);
      if (error) reject(error); else resolve();
    }
    const aborted = () => finish();
    options.signal.addEventListener('abort', aborted, { once: true });
    if (options.signal.aborted) { finish(); return; }

    async function request<T>(suffix: string, method = 'GET'): Promise<T> {
      const requestController = new AbortController();
      const abort = () => requestController.abort();
      controller.signal.addEventListener('abort', abort, { once: true });
      const cancelTimeout = schedule(abort, 15000);
      try {
        return await responseJson<T>(await fetcher(`${options.base}/runs/${options.runId}${suffix}`, {
          method, signal: requestController.signal,
        }));
      } finally {
        cancelTimeout();
        controller.signal.removeEventListener('abort', abort);
      }
    }

    async function replay() {
      if (stopped) return;
      dirty = true;
      if (pumping) return;
      pumping = true;
      try {
        while (dirty && !stopped) {
          dirty = false;
          const page = await request<ReplayPage>(`/events?after=${cursor}`);
          if (stopped) return;
          const before = cursor;
          // Ignore duplicates and tolerate reordered pages without ever skipping a gap.
          for (const event of [...page.events].sort((a, b) => a.seq - b.seq)) {
            if (stopped) return;
            if (event.seq <= cursor) continue;
            if (!Number.isSafeInteger(event.seq) || event.seq !== cursor + 1) break;
            options.onEvent(event.data, event.seq);
            cursor = event.seq;
          }
          if (stopped) return;
          if (!page.has_more && cursor >= page.last_event_id && isTerminal(page.status)) {
            options.onTerminal(page.status);
            finish();
            return;
          }
          // A non-progressing page is retried on the next poll, never in a tight loop.
          if (cursor > before && (page.has_more || cursor < page.last_event_id)) dirty = true;
          else if (cursor < page.last_event_id || page.has_more) dirty = false;
        }
      } catch (error) {
        if (!stopped) {
          if (error instanceof HttpError && error.permanent) finish(error);
          else options.onConnection('reconnecting');
        }
      } finally { pumping = false; }
    }

    function poll() {
      cancelPoll = schedule(() => {
        if (stopped) return;
        void replay();
        poll();
      }, 5000);
    }

    async function connect() {
      if (stopped) return;
      options.onConnection(attempt ? 'reconnecting' : 'connecting');
      let socket: LiveSocket | undefined;
      let closed = false;
      let subscribed = false;
      let acknowledged = false;
      let cancelHandshake = () => {};
      let cancelKeepAlive = () => {};
      let cancelStable = () => {};
      const id = 'run-events'; // Unique within this single-subscription connection.
      function dispose() {
        closed = true;
        cancelHandshake(); cancelKeepAlive(); cancelStable();
        if (socket) {
          socket.onopen = socket.onmessage = socket.onerror = socket.onclose = null;
          try {
            if (subscribed && socket.readyState === 1) socket.send(JSON.stringify({ id, type: 'unsubscribe' }));
          } catch { /* The transport may have closed before cleanup. */ }
          try { socket.close(); } catch { /* Cleanup still releases timers and requests. */ }
        }
      }
      closeSocket = dispose;
      function retry() {
        if (closed || stopped) return;
        dispose();
        options.onConnection('reconnecting');
        const delay = Math.min(30000, 1000 * 2 ** Math.min(attempt++, 5)) * (0.5 + random() * 0.5);
        cancelRetry = schedule(() => { void connect(); }, delay);
      }
      try {
        // Every connection obtains a new short-lived, run-scoped ticket.
        const ticket = await request<SubscriptionTicket>('/subscription', 'POST');
        if (stopped || closed) return;
        socket = makeSocket(ticket.url, appsyncProtocols(ticket));
        cancelHandshake = schedule(retry, 10000);
        let timeoutMs = 300000;
        function keepAlive() {
          cancelKeepAlive();
          cancelKeepAlive = schedule(retry, timeoutMs);
        }
        socket.onopen = () => {
          if (!closed && !stopped) socket!.send(JSON.stringify({ type: 'connection_init' }));
        };
        socket.onmessage = message => {
          if (closed || stopped) return;
          try {
            const frame = JSON.parse(String(message.data));
            if (frame.type === 'connection_ack' && !acknowledged) {
              acknowledged = true;
              // Bounded so a malformed value cannot overflow setTimeout (which then fires at once).
              const requested = frame.connectionTimeoutMs;
              if (typeof requested === 'number' && requested > 0) {
                if (requested > MAX_KEEP_ALIVE_MS) timeoutMs = MAX_KEEP_ALIVE_MS;
                else if (requested < MIN_KEEP_ALIVE_MS) timeoutMs = MIN_KEEP_ALIVE_MS;
                else timeoutMs = requested;
              }
              keepAlive();
              socket!.send(JSON.stringify({ id, type: 'subscribe', channel: ticket.channel,
                authorization: { authorization: ticket.token, host: ticket.host } }));
            } else if (frame.type === 'ka' && acknowledged) {
              keepAlive();
            } else if (frame.type === 'subscribe_success' && frame.id === id && acknowledged && !subscribed) {
              subscribed = true;
              cancelHandshake();
              cancelStable = schedule(() => { attempt = 0; }, 30000);
              options.onConnection('idle');
              void replay(); // Subscribe BEFORE catch-up so there is no live/replay handoff gap.
            } else if (frame.type === 'data' && frame.id === id && subscribed && Array.isArray(frame.event)) {
              for (const encoded of frame.event) {
                try {
                  const hint = JSON.parse(encoded);
                  if (hint.run_id === options.runId && Number.isSafeInteger(hint.seq) && hint.seq > cursor) void replay();
                } catch { /* Malformed hints never supply content or move the cursor. */ }
              }
            } else if (['connection_error', 'subscribe_error', 'broadcast_error'].includes(frame.type)) retry();
          } catch { retry(); }
        };
        socket.onerror = retry;
        socket.onclose = retry;
      } catch (error) {
        if (stopped || closed) return;
        if (error instanceof HttpError && error.permanent) finish(error);
        else retry();
      }
    }
    poll(); // Also checks authorization when notifications are missing or the socket is unavailable.
    void connect();
  });
}

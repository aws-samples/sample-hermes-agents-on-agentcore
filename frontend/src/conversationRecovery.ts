import { responseJson, type Run } from './runClient';
import { upsertMessage, type Message } from './streamState';

export type ConversationSnapshot = { history: Message[]; run: Run | null };
export type HistoryPage = { messages: Message[]; next_cursor: string | null };

export async function loadHistory(base: string, signal: AbortSignal,
  fetcher: typeof fetch = fetch): Promise<Message[]> {
  const history: Message[] = [];
  const seen = new Set<string>();
  let cursor: string | null = null;
  do {
    signal.throwIfAborted();
    const suffix: string = cursor === null ? '' : `?cursor=${encodeURIComponent(cursor)}`;
    const page: HistoryPage = await responseJson<HistoryPage>(await fetcher(`${base}/messages${suffix}`, { signal }));
    signal.throwIfAborted();
    if (!Array.isArray(page.messages) || (page.next_cursor !== null &&
      (typeof page.next_cursor !== 'string' || !page.next_cursor || seen.has(page.next_cursor)))) {
      throw new Error('Invalid history page. Reopen the conversation to retry recovery.');
    }
    history.push(...page.messages);
    cursor = page.next_cursor;
    if (cursor !== null) seen.add(cursor);
  } while (cursor !== null);
  return history;
}

async function latestRun(base: string, signal: AbortSignal, fetcher: typeof fetch): Promise<Run | null> {
  signal.throwIfAborted();
  const run = await responseJson<Run | null>(await fetcher(`${base}/runs/active`, { signal }));
  signal.throwIfAborted();
  return run;
}

/** The latest-run pointer and user message are written atomically. Bracket history reads
 * with strong pointer reads, rejecting history from a different turn before showing it.
 * Completion within the same run is reconciled by replay/upsert, not a duplicate message.
 */
export async function recoverConversation(base: string, signal: AbortSignal,
  fetcher: typeof fetch = fetch): Promise<ConversationSnapshot> {
  for (let attempt = 0; attempt < 3; attempt++) {
    const before = await latestRun(base, signal, fetcher);
    const history = await loadHistory(base, signal, fetcher);
    const after = await latestRun(base, signal, fetcher);
    if (before?.id !== after?.id) continue;
    // Never attach output whose input is missing from the recovered history.
    if (after && !history.some(message => message.run_id === after.id && message.role === 'user')) continue;
    return { history: history.reduce(upsertMessage, []), run: after };
  }
  throw new Error('Conversation changed while loading. Reopen it to retry recovery.');
}

/** Before enabling sending after replay completes, catch a newer turn started by another client. */
export async function recoverNextRun(base: string, finishedRunId: string, signal: AbortSignal,
  fetcher: typeof fetch = fetch): Promise<ConversationSnapshot | null> {
  const latest = await latestRun(base, signal, fetcher);
  if (!latest || latest.id === finishedRunId) return null;
  return recoverConversation(base, signal, fetcher);
}

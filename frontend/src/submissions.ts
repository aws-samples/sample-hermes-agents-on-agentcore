export type PendingSubmission = { message: string; key: string };
type StorageLike = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

export function selectionKey(user: string, agent?: string): string {
  return `agent-sandbox-selection:${JSON.stringify([user, agent ?? null])}`;
}

export function readStored(key: string): string | null {
  try { return localStorage.getItem(key); } catch { return null; }
}
export function writeStored(key: string, value: string | null) {
  try {
    if (value === null) localStorage.removeItem(key); else localStorage.setItem(key, value);
  } catch { /* Storage can be disabled; the current view still works. */ }
}

/** A logical submission gets one key, retained until an unambiguous response. */
export class SubmissionStore {
  private pending = new Map<string, PendingSubmission>();
  constructor(private storage?: StorageLike, private newKey = () => crypto.randomUUID()) {}
  private scope(user: string, agent: string, conversation: string) {
    return `agent-sandbox-pending:${JSON.stringify([user, agent, conversation])}`;
  }
  get(user: string, agent: string, conversation: string): PendingSubmission | null {
    const scope = this.scope(user, agent, conversation);
    if (this.pending.has(scope)) return this.pending.get(scope)!;
    try {
      const value = JSON.parse(this.storage?.getItem(scope) ?? 'null');
      if (typeof value?.message === 'string' && typeof value?.key === 'string') {
        this.pending.set(scope, value);
        return value;
      }
    } catch { /* Fall back to in-memory retry identity. */ }
    return null;
  }
  prepare(user: string, agent: string, conversation: string, message: string): PendingSubmission {
    const previous = this.get(user, agent, conversation);
    if (previous) {
      if (previous.message !== message) throw new Error('Retry the previous message first; its submission has not been confirmed.');
      return previous;
    }
    const submission = { message, key: this.newKey() };
    const scope = this.scope(user, agent, conversation);
    this.pending.set(scope, submission);
    try { this.storage?.setItem(scope, JSON.stringify(submission)); } catch { /* In-memory retry remains safe. */ }
    return submission;
  }
  clear(user: string, agent: string, conversation: string) {
    const scope = this.scope(user, agent, conversation);
    this.pending.delete(scope);
    try { this.storage?.removeItem(scope); } catch { /* Storage is optional. */ }
  }
}

export type ConnectionState = 'idle' | 'connecting' | 'reconnecting' | 'interrupted';
export type StreamView = {
  runId: string | null;
  lastEventId: number;
  connection: ConnectionState;
  current: string;
  interim: string;
  finalChunks: string;
};
export type StreamEvent = { type: string; text?: string; message?: string; reason?: string };

export const emptyStream = (): StreamView => ({
  runId: null, lastEventId: 0, connection: 'idle', current: '', interim: '', finalChunks: '',
});

export function attachStream(state: StreamView, runId: string, connection: ConnectionState): StreamView {
  return state.runId === runId
    ? { ...state, connection }
    : { ...emptyStream(), runId, connection };
}

export function reduceStream(state: StreamView, event: StreamEvent, eventId?: number): StreamView {
  // Replay is contiguous. Never advance across a gap or reapply an old delta.
  if (eventId !== undefined && eventId !== state.lastEventId + 1) return state;
  const tracking = eventId === undefined ? {} : { lastEventId: eventId };
  if (event.type === 'final_chunk' && typeof event.text === 'string') {
    const finalChunks = state.finalChunks + event.text;
    return { ...state, ...tracking, finalChunks, current: finalChunks };
  }
  if (event.type === 'delta' && typeof event.text === 'string') {
    return { ...state, ...tracking, current: state.current + event.text };
  }
  if (event.type === 'segment_end') {
    return state.current.trim()
      ? { ...state, ...tracking, current: '', interim: state.current }
      : { ...state, ...tracking };
  }
  return { ...state, ...tracking };
}

export function finalText(state: StreamView, event?: StreamEvent): string {
  return event?.text || state.finalChunks || state.current || state.interim;
}

export type Message = { role: string; text: string; run_id?: string; partial?: boolean; exit_reason?: string };

// History can already contain the atomically committed terminal assistant message.
export function upsertMessage(messages: Message[], message: Message): Message[] {
  const index = message.run_id
    ? messages.findIndex(item => item.run_id === message.run_id && item.role === message.role) : -1;
  if (index < 0) return [...messages, message];
  return messages.flatMap((item, i) => i === index ? [message]
    : item.run_id === message.run_id && item.role === message.role ? [] : [item]);
}

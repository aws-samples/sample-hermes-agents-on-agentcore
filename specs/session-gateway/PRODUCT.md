# Session Gateway

## Summary

Web and mobile clients use a shared authenticated session gateway to create, send to, and
reconnect to agent conversations without holding a direct connection to an AgentCore invocation.
An in-progress agent turn continues when a client reloads, loses connectivity, backgrounds the
app, or reconnects from another authorized client.

## Goals / Non-goals

- Provide one HTTP API and event protocol for browser and native mobile clients.
- Keep AgentCore runtime session IDs and agent credentials
  private to backend services.
- Require conversation-owner authorization in addition to current team access. Workspace
  artifacts, skills, MEMORY.md, USER.md and SOUL.md are shared across the agent's conversations.
  Sequential agents have one active turn per agent;
  concurrent agents permit separate conversations to execute simultaneously.
- Defer Discord and all other third-party chat adapters.
- Do not promise continuation after an AgentCore runtime session or active run has terminated.

## Figma

Figma: none provided. The portal uses its existing chat visual language.

## Behavior

1. An authenticated web or mobile client can explicitly create a new conversation session for an
   agent it is authorized to use. The response contains an opaque client session ID; it never
   exposes an AgentCore runtime session ID, agent credential, or filesystem identifier.

2. A client can resume a session owned by its authenticated human identity. Another device signed
   in as the same human can reconnect; another human, including an administrator, cannot. Resuming
   returns the session's conversation history and current state: idle, running, completed,
   partial, failed, interrupted, or pending dispatch.
   History is paginated so the total conversation may exceed a single response's size limit.
   Clients follow `next_cursor` until null and must not present a failed partial page traversal
   as complete history. Access is rechecked on every page.

3. Starting a new session always creates an empty conversation state. Existing agent workspace,
   skills, and artifacts remain available, but no message or SQLite conversation state from a
   different session is visible in the new session.

4. A client sends a message with an idempotency key. Retrying the same request must return the
   already-created run and must not submit a second user message or create a second agent turn.

5. Agent creation offers Sequential (lock) and Concurrent (no agent-wide lock), defaulting to
   sequential. This choice is fixed at creation. Sending starts a run if that agent is idle
   (sequential mode), or that conversation is idle (concurrent mode). A competing key for the
   reserved scope gets HTTP 409 without inserting another user message. Retrying the original
   key returns its run. Concurrent mode permits shared workspace/skill edits from different
   conversations without an agent-wide lock, but does not weaken privacy or run fencing.

6. A client observes an active run through AppSync WebSocket notifications and authenticated HTTP
   event replay. Ordered replay events include status, output deltas, commentary boundaries,
   final-text chunks, completion, partial completion and terminal failure. Live notifications
   contain only run ID and cursor, with no conversation text.

7. A client may reconnect and request replay after the last event ID it processed. It receives
   only later events in order. Notifications can duplicate, reorder or be missed; periodic
   replay catches up even without a socket. Replay/idempotency retention is seven days, after
   which history remains available and expired replay requests fail explicitly.

8. Closing a browser tab, refreshing a page, losing network connectivity, or backgrounding a
   mobile app disconnects only that client observation. It must not cancel the active agent run.
   On reconnect, the client discovers and resumes the active run for its selected session.

9. A completed or partial run materializes exactly one assistant message in the conversation.
   A partial message visibly identifies that the turn stopped early and includes its available
   exit reason. A failed or interrupted run retains all safely available streamed output but does
   not claim a completed assistant response. For Hermes, its SQLite checkpoint becomes authoritative only
   with accepted completion; an interrupted or stale run cannot advance the session's recovery
   database. Workspace files and external tool side effects are not rolled back.

10. A client that reconnects after an active AgentCore runtime session has terminated sees the
    recorded terminal or interrupted state and all retained output. It may send a new message,
    which for Hermes resumes from the latest completed session checkpoint; the service does not imply that
    the interrupted generation itself continued.

    Snapshot recovery is Hermes-specific. Other framework implementations may use another
    persistence strategy or none; their events/history remain durable and completion never
    requires a snapshot in the generic gateway.

11. Web and mobile clients authenticate with existing human identities and can access a session
    only if they own it and still share a team with its agent. Unowned legacy sessions are hidden
    until ownership can be established through a separate verified migration. Revoked team membership blocks new sends and
    event attachment immediately. A client cannot substitute another client session ID, agent,
    run ID, or event cursor to access another team's content.

12. The portal restores the selected session's latest run after page load. It shows connecting or
    reconnecting state while attaching, preserves streamed output through reconnection, and allows
    a new message only after the recovered run reaches a terminal state. Final responses may be
    subject to a 300,000 UTF-8 byte application cap and a 256 KiB encoded worker-message limit.
    Responses can fail below the application cap due to JSON encoding expansion. This limitation
    is accepted; saving large results as artifacts is agent guidance, not an automatic fallback.
    History and run recovery must refer to the same turn and include its user message. A concurrent
    turn started by another client causes recovery to retry rather than displaying mismatched
    history. If consistent recovery fails, sending stays disabled with instructions to reopen
    the conversation; navigation and starting a new conversation remain available.

13. The mobile protocol has the same resource shapes, authorization, idempotency, event IDs, and
    terminal semantics as the web protocol. Native clients may reconnect after application resume
    without relying on browser-specific storage or behavior.

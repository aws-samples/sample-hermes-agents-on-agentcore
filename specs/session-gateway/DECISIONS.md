# Serverless gateway review decisions

The user requested one-by-one resolution of the implementation review. Local implementation
validation is followed by a deployment/diff review and live acceptance, as requested separately.

## 1. Checkpoint ownership — fixed

Approved: write an immutable run-specific checkpoint and commit its authoritative pointer in the
fenced completion transaction. Discard the worker on unsuccessful finalization. The previous
committed checkpoint remains authoritative if ownership was lost. Shared workspace files and
external tool side effects are not rolled back. Targeted checkpoint/durability tests passed.

Scope clarification from the harness separation: this snapshot policy belongs to Hermes.
Its sandboxed adapter creates backups and its trusted lifecycle module restores/publishes them.
The generic completion transaction carries optional opaque `adapter_state`; Hermes uses that
field for its checkpoint pointer and handles legacy `checkpoint_name` recovery itself. Other
agents can complete without snapshots. The fencing and failure guarantees above still apply to Hermes.

## 2. Existing runtime sessions during cutover — accepted risk, no change

Decision: preserve private runtime session IDs and do not implement automatic ID rotation or
require explicit session termination as the resolution to this finding.

Rationale supplied by the user: after a runtime VM times out and shuts down, a subsequent
invocation using the same session ID starts a new VM with updated code.

Accepted consequence: while an old VM is still alive, it may reject the new `start`/`run_id`
protocol. Dispatch can fail and its pending run can become interrupted. Subsequent new runs can
use the updated code once that VM shuts down. This remains a deployment validation consideration;
draining active turns does not itself terminate idle old VMs.

## 3. Ambiguous ownership-claim response — accepted risk, no change

Decision: do not implement owner-token read-back recovery for ambiguous claim results.
The user explicitly acknowledged and accepted this risk after discussing synchronous calls and
the distinction between a committed write and receipt of its response.

Accepted failure sequence: DynamoDB commits `pending -> running` with an owner token, but the
runtime does not receive the success response. A retry can fail the pending-only condition and
skip producer startup. The durable run is then marked running without an executing producer.

Existing mitigation: the reconciler detects a running run with no heartbeat for three minutes
and conditionally marks it interrupted, releasing the durable reservation. Detection also depends
on the minute-based schedule and service availability; it is not an exact three-minute guarantee.
The user may submit a new turn afterward. No automatic re-execution of a claimed run is added.
The affected code remains `RunStore.claim` in `common/runs.py` and the `start` handler in
`runtime/server.py`.

## 4A. Ambiguous event-write response — accepted risk, no change

Decision: leave event-write recovery unchanged. The user explicitly accepted the risk of an
event transaction committing while its success response is lost. The local cursor can remain
behind the durable cursor, causing the producer to stop unnecessarily and leave reconciliation
to terminalize the run. Already-committed events remain durable; no automatic replay of tool
execution is added. Existing recognition of a committed event after TransactionCanceledException
remains, but recovery is not extended to exhausted transport timeouts.

## 4B. Transient transaction conflicts — fixed

Approved: distinguish explicit DynamoDB TransactionConflict cancellation reasons from lost
ownership and other service errors. `RunStore.append` strongly reads the run's owner, status,
cursor and update timestamp after cancellation. If the expected fencing state is unchanged and
the only failure reason is TransactionConflict, retry the identical conditional transaction up to
three times with exponential full-jitter backoff (100, 200 and 400 ms upper bounds).

All original transaction conditions remain on every attempt. Changed fencing state or a lost
terminal reservation stops the stale writer. Missing/mixed cancellation reasons, validation
failures and retry exhaustion preserve the actual service error rather than being misreported
as ownership loss. No agent/tool execution is repeated by this database retry loop.

Tests cover successful conflict retries (including atomic completion/checkpoint promotion),
reconciliation winning between the read and retry, changed ownership/status, bounded exhaustion,
and non-retryable errors. A separate regression confirms that the accepted exhausted transport
timeout behavior in 4A remains unchanged.

## 5. Conversation recovery race — fixed

Approved: recover a coherent history/latest-run pair instead of issuing independent parallel
reads. The frontend reads latest run, loads history, and reads latest run again. It retries up to
three times when the pointer changes or history is missing that run's user message. Only a
validated pair is displayed/attached, and keyed history/terminal replay merging prevents duplicate
messages when completion occurs during recovery.

Before enabling the composer after a terminal replay, the client checks for a newer run and
recovers/attaches it if present. If recovery fails or keeps changing, sending stays disabled and
the UI asks the user to reopen the conversation; navigation and new-conversation actions remain
available. Aborted view changes ignore late recovery results.

Tests exercise both new-turn/history interleavings, same-run completion, empty-to-active sessions,
missing input, bounded retries, view cancellation and the post-terminal newer-run check.
Other review findings remain under individual review.

## 6. Response size and worker framing — accepted risk, no change

Decision: leave the application response-size limit and worker framing limit unchanged. The user
chose this after clarification that the 300,000 UTF-8 byte final-message limit was introduced
during implementation, and after discussing the behavior of an oversized response.

Accepted consequences: the application rejects final messages above 300,000 UTF-8 bytes. The
worker-to-supervisor reader also limits a JSON line to 256 KiB, so JSON encoding/escaping can
reject responses below the application limit. These limits do not guarantee support for every
response below 300,000 bytes. Such failures can end the turn without a completed assistant
message. Committed replay events remain available and the previous committed checkpoint remains
authoritative; workspace files and external tool side effects are not rolled back.

There is no automatic oversized-response artifact fallback. Saving large deliverables as files
is guidance for the agent, not automatic recovery implemented by the gateway. Neither chunked
final-message storage nor a larger worker framing limit is added as part of this finding.

## 7. Large conversation history — fixed

Approved: paginate history rather than buffering the entire conversation in one Lambda response.
The messages endpoint now returns `{messages, next_cursor}`. Each page uses one strongly
consistent DynamoDB query, at most 100 messages, and a 2 MiB escaped-JSON budget including
headroom for the cursor and response envelope. Cursors resume after the last returned message
and are scoped to the authorized agent/conversation; authorization is repeated on every page.

The frontend loads all pages between the latest-run consistency checks from finding 5. A new
run during paging causes recovery to start over; malformed/repeating cursors and cancellation
do not present partial history as a complete result. Tests cover history larger than Lambda's
6 MiB response limit, encoding expansion, count/byte cutoffs without skipped messages, cursor
scope, per-page authorization, and frontend recovery across multiple pages.

The separate test-order validation gap in the Mangum adapter test is fixed by giving that test
an explicit event loop. This does not change production invocation behavior.

## Creation-time execution mode

The pre-option serverless/privacy implementation was committed as `b5cdacb`. The user then
requested a choice between concurrent (no lock) and sequential (lock) execution at agent creation.
Sequential remains the default for existing agents and missing fields. Concurrent mode removes
agent-wide DynamoDB and EFS serialization, allowing different private conversations to run
together. It retains conversation-scoped ordering and per-run idempotency/ownership fencing.
The immutable application setting is persisted, signed by Cognito and checked against the run
and invocation. Shared workspace/skill edits can race in concurrent mode; privacy is unchanged.

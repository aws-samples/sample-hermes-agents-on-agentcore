# Serverless session gateway

## Context and branch boundary

The ECS implementation is preserved at `dd28883` on `ecs-session-gateway`. This branch replaces
process-local run ownership and SSE observers. The existing metadata table, Cognito pool, EFS
access point and conversation IDs remain authoritative. Hermes retains its per-session SQLite
recovery through its own integration; snapshots are not required by the generic gateway.

## Architecture

* CloudFront `/api/*` -> API Gateway HTTP API -> short-lived Python Lambda (FastAPI/Mangum).
  Reuse every existing account, administration, provisioning, settings and file API. Lambda mounts
  the existing EFS access point for workspace operations. Credentials remain in Secrets Manager.
* DynamoDB run table: run metadata/owner/cursor, scoped execution reservation, subscription
  tickets, and ordered immutable events. Create-run transactions also write the user message and
  conversation's latest run pointer in the existing metadata table.
* DynamoDB Streams INSERT run -> dispatcher Lambda -> AgentCore `start` request. No streaming
  invocation is held open in Lambda. The supervisor claims a pending run and returns promptly,
  retaining an independent asyncio producer and reporting `HealthyBusy` until it finishes.
* Supervisor commits each event and cursor in one fenced transaction. Terminal events, final
  assistant message, run status, optional opaque `adapter_state` and reservation release commit atomically. Background work owns
  the EFS lock for sequential agents and retains the one-hour deadline. Concurrent agents skip
  the agent-wide lock. The sandbox gets no database credentials.
* DynamoDB Streams INSERT event -> publisher Lambda -> IAM-authenticated AppSync Events publish.
  AppSync delivers only `{run_id, seq}` notifications. Clients fetch ordered text via authorized
  replay reads. This also prevents existing sockets leaking content after membership revocation.
* AppSync is live fanout, **not durable storage**. DynamoDB is the durable replay source. Delivery
  may duplicate, reorder or lag; clients always advance a contiguous cursor from replay responses.

## Command and client contracts

All routes retain the prefix `/api/agents/{agent}/conversations/{conversation}`:

| Route | Contract |
| --- | --- |
| POST `/runs` | JSON `{message}`, required Idempotency-Key; returns `{id,status,last_event_id}` |
| GET `/runs/active` | latest run or null, including terminal state for recovery |
| GET `/runs/{run}` | authorized status |
| GET `/runs/{run}/events?after=N` | `{events:[{seq,data}],last_event_id,status,has_more}`; pages of 100 |
| POST `/runs/{run}/subscription` | `{url,host,channel,token,expires}` for native AppSync WebSocket |
| GET `/messages?cursor=...` | `{messages,next_cursor}`; ordered history pages, null cursor at end |

Explicit new conversations retain the existing create/list/messages endpoints. A conversation is
the client session. Its DynamoDB record holds private runtime ID, creator and latest run; web and
mobile access requires immutable conversation `owner_sub` equality plus current agent-team access.
Listings filter by that owner; all history/run/replay/ticket routes enforce it, with no admin bypass.
Run creation and terminal completion transactions also check the persisted owner. Ticket authorization
and dispatch recheck conversation ownership. No task affinity or sticky session is needed.

Legacy conversations without an owner are retained but inaccessible: ownership must not be inferred
from the agent creator, the latest participant or whichever user first requests a conversation.
Runtime HOME and HERMES_HOME are private `/state/home` and `/state`. Only the agent's `workspace`
and `hermes/skills` children are mounted, not its whole root. Private config directs terminal CWD
to the shared workspace and new/viewed skills to `/shared/skills`. Agent knowledge/persona in
MEMORY.md, USER.md and SOUL.md use the agent's `hermes/agent` directory mounted at `/shared/agent`
through a pinned Hermes path adapter. All three files belong to the agent. Logs, dumps and skill
usage telemetry and trajectories stay private. Skill ledger generation is disabled and historical
ledger/usage sidecars are masked. Only SQLite is currently checkpointed; private logs are
ephemeral across worker replacement. Agent memory files persist directly on EFS, as do explicitly
saved artifacts and skill content.

Frontend recovery brackets history with latest-run reads and retries up to three times if the
pointer changes or that run's user message is absent. Completion within the same run is merged by
run ID and role through replay. Before enabling sending after replay, check again for a newer
run and recover it if necessary. Failed/inconsistent recovery keeps sending disabled while
allowing the user to reopen the conversation or start another one. This provides a consistent
recovery point; a later concurrent send is still arbitrated by the durable reservation.

History pages use one strongly consistent DynamoDB query with a 100-message maximum. Escaped
JSON is budgeted to 2 MiB with cursor/envelope headroom, so encoding expansion cannot overflow
Lambda's buffered response. The cursor is bound to the agent/conversation and the last returned
sort key; a byte cutoff resumes after that returned key, not after unread query results. Every
page is authorized independently. Recovery brackets the full page traversal with latest-run
reads and does not accept incomplete/repeating-cursor results.

The idempotency record key is deterministic from agent, conversation, human and idempotency key;
it maps to a random run ID so physical TTL deletion cannot cause collisions with old events.
Reuse with a different message returns 409. Idempotent retries return the same run even after completion. Concurrent
different keys compete on the configured reservation scope; losers receive 409 without recording a user
message. Retention is 7 days for runs, events and idempotency; conversation messages have no TTL.

`execution_mode` is selected at agent creation and immutable through application APIs. Missing
values mean `sequential`. It is stored in metadata and Cognito, signed into tokens, copied into
run records, and compared at runtime admission. Sequential mode uses `AGENT#<id>/LOCK` and the
EFS execution lock. Concurrent mode uses `CONVERSATION#<agent-id>#<conversation-id>/LOCK` only,
so separate conversations can work on shared files simultaneously. Completion/reconciliation
derive the release key from the frozen run policy. Run claims, per-conversation ordering, private
history, cursor fencing and checkpoint transactions remain enforced in both modes.

Run state: pending -> running -> complete/partial/failed/interrupted. Owner UUID is a fencing token;
only the claim winner can append events. Ambiguous dispatcher failures retry the same run, never a
fresh message. A claimed run is never restarted automatically (tools may already have side effects).
Supervisor heartbeats every 15 seconds. A scheduled reconciler terminalizes pending runs older than
five minutes or running runs without heartbeat for three minutes. It uses conditional status,
owner and timestamp checks; a live writer's successful renewal defeats a stale sweep. It never
steals an EFS lock. A stale owner fails its next write and stops its worker.

Accepted exception: an ownership claim can commit while its success response is lost, leaving
the run marked running without a producer. Owner-token read-back recovery is intentionally not
implemented; stale-run reconciliation handles this case. Existing runtime IDs also remain
unchanged during cutover, accepting temporary incompatibility while an old VM is alive.
See [review decisions](DECISIONS.md) for the user's rationale and accepted consequences.

The user also accepts interruption after an ambiguous event-write response: an exhausted
transport timeout can leave the producer's local cursor behind a committed event. No additional
read-back recovery is implemented for that case; committed events remain available for replay.
Explicit TransactionConflict cancellations receive up to three retries with exponential full
jitter only after a strong read confirms unchanged owner/status/cursor/update timestamp.
Every retry retains the original transaction conditions, so reconciliation can still win after
that read. Other cancellation reasons and exhaustion propagate the actual service error; changed
fencing state stops the stale producer. This does not add transport-timeout recovery.

## Authentication

Browser cookie authentication, Origin checks, PKCE login and current Cognito membership checks stay
in FastAPI. Native clients use verified human bearer ID tokens; cookie-authenticated requests must
never gain a CSRF bypass by merely adding an invalid Authorization header. Dedicated mobile OAuth
callback URLs are deployment configuration. Both ID-token and cookie requests use live membership.

AppSync uses Lambda authorization for connect/subscribe and IAM for publish. Short-lived opaque
run-scoped subscription tickets are issued through the authenticated API, hashed in DynamoDB, and
validated with current membership at subscription time. No wildcard subscriptions and no client
publishing. Tickets expire after two minutes; active sockets receive only non-sensitive hints.
Reconnect obtains a fresh ticket. API auth is rechecked on every replay/status fetch.

## Runtime durability and limits

AgentCore process death interrupts the in-flight computation; events already committed survive.
The reconciler releases durable reservations and writes an interrupted terminal event. Restarting
the same runtime ID does not replay a claimed run. Agent-specific persistence may restore the
conversation on a new turn. Model/tool iteration limits may complete a turn before one hour.

The generic harness invokes optional trusted lifecycle hooks and commits their bounded opaque
`adapter_state` output with completion. A complete/partial turn does not require a snapshot,
filename, or persistence method. Echo completes without any state metadata or snapshot files.

For Hermes, `agents/hermes/agent_adapter.py` creates its SQLite backup before returning a turn
result. Its trusted `agents/hermes/lifecycle.py` writes and fsyncs an immutable
`.control/<agent-sub>/<conversation-id>.<run-id>.db` candidate before the completion transaction.
The transaction stores its pointer inside opaque `adapter_state` on the conversation record
(no TTL), together with completion and its assistant message. Hermes' lifecycle consumes the
strongly read context and restores only that
file. A missing pointed-to file fails closed. When no pointer exists, only that conversation's
legacy `checkpoint_name` metadata or `<conversation-id>.db` may be restored; run-specific orphan
files are ignored. Snapshot paths, limits and legacy recovery belong entirely to Hermes.

HealthyBusy and, in sequential mode, the EFS lock remain held through finalization. A worker is reusable only after
the transaction succeeds; finalization failure or loss of ownership discards it before releasing
the lock. For Hermes, a crash before commit leaves an unreferenced immutable file, never a promoted checkpoint.
A lost response after a successful commit may discard the worker conservatively, but its next
start restores the committed pointer. These guarantees cover SQLite recovery, not rollback of
workspace files or external tool side effects. Automatic orphan checkpoint cleanup is deferred;
retaining an unreferenced file is safer than deleting a possibly committed one.

Coalesce token deltas (2,048 characters or 250 ms of arriving deltas) before writes. Bound each
event to 200,000 UTF-8 bytes and the final message to 300,000; larger responses fail explicitly
instead of silently truncating. Split final text into 8,192-character replay chunks before the
final completion event. Large deliverables belong in artifacts. AppSync publication failure never
rolls back persisted execution events.

Accepted framing limitation: the worker protocol still bounds each JSON line to 256 KiB before
the supervisor can chunk final text. Encoding/escaping can therefore reject a response below
the 300,000-byte application cap. The user elected to retain both limits. No automatic artifact
fallback is implemented for oversized responses; see finding 6 in [review decisions](DECISIONS.md).
Stream consumers retry failures and use failure destinations/alarms; periodic client replay also
recovers from notification loss. Expired replay returns an explicit error, not an empty success.

## Full ECS migration

Lambda retains `/api/health`, `/api/me`, OAuth login/callback/logout, agent create/list/settings,
conversation creation/history, teams/users/admin account actions, EFS file listing/download,
Secrets Manager agent-password issuance, and current membership checks. Existing resource logical
IDs for persisted resources must stay stable. File downloads need chunked range reads because
buffered Lambda/API Gateway responses cannot carry the old 32 MiB maximum in one response.
The legacy `/chat` streaming route returns a migration response rather than holding Lambda open.
No container service is needed in this branch; ARM64 AgentCore and Lambda images remain.

## Parallelization

After this contract is written, implementation can use local subagents with disjoint ownership:
frontend (frontend sources/tests), infrastructure (CDK and Lambda container packaging), and core
(orchestrator: Python run store, runtime, HTTP routes, stream consumers). Work is integrated on
`serverless-session-gateway`, with no extra commits/PRs until requested. Validation is owned by the
orchestrator. Each worker must report changed files and verification results.

## Validation

* Transaction tests: concurrent send arbitration, same-key retries, payload collisions, claim
  race, owner fencing, stale heartbeat race, sequence atomicity, terminal/message atomicity.
* Runtime tests: command response returns while producer lives; disconnect does not cancel;
  duplicated start does not invoke tools twice; lost owner stops work.
* Auth/API tests: current teams, forged tickets, wildcard/publish denial, origin bypass, cross-run
  paths, ordered pages and expiry.
* Frontend tests: duplicate/out-of-order notifications, subscribe-before-replay, gap recovery,
  reconnect/abort, terminal history de-duplication.
* Infrastructure synthesis assertions: no ECS, short Lambda timeouts, API routes, AppSync auth,
  stream filtering/failure destinations, EFS grants and durable resource identity preservation.
* Live acceptance after deployment: start delayed turn, close browser, connect on second device,
  observe progress and exactly one final message; retry command; revoke team access; interrupt
  runtime and verify durable interrupted outcome. These checks require AWS deployment and are
  reported separately from local tests.

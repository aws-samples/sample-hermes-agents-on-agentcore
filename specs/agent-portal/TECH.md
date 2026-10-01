# Agent portal technical design

## Components

### Harness and sandboxed agent boundary

`runtime/` is the framework-independent platform harness. `server.py` owns admission,
bubblewrap, worker reuse, verified mounts, optional lifecycle hooks and fenced durable events;
`broker.py` owns credentialed model inference and public-HTTPS egress; `telemetry.py` owns
export credentials and bounded telemetry interpretation. The sandbox-side `worker.py` owns
descriptor closure, seccomp, loopback proxy bridges and the correlated JSONL protocol.

`agents/hermes/agent_adapter.py` is the sandboxed Hermes integration. It owns the upstream pin and
dependencies, HERMES_HOME/configuration, AIAgent/SessionDB, memory hooks, tool callbacks,
token estimation and SQLite backup. The harness never imports this adapter. `runtime/contract.py`
defines the stdlib-only adapter API: configuration, event callback, `run`, `estimate_tokens`,
and `close`. There is no snapshot method or required snapshot file. `agents/echo/` implements the
same interface with process-local memory, no persistence and no framework dependencies.
See the [adapter contract and extension guide](../../docs/HARNESS.md).

The image build selects `AGENT_SOURCE` (default `agents/hermes`); CDK exposes this as
`-c agentImplementation=hermes` or `echo`. Each selected directory supplies `install.sh`,
`manifest.json` and `agent_adapter.py`. The agent installs into `/opt/venv`; the harness uses
its own `/harness-venv`, which is not mounted into the sandbox. `AGENT_ADAPTER_MANIFEST` points
to immutable image configuration, not a caller-supplied field. Only the selected adapter is
mounted; its code is imported after seccomp and stdout redirection. The manifest declares
protocol version, model protocol, storage namespace and a bounded list of skill sidecar masks.

Snapshots are entirely Hermes-specific. Its optional trusted `agents/hermes/lifecycle.py`
implements restoration/publication through generic `before_start`/`after_turn` hooks; it never
imports the framework or parses SQLite. `host_lifecycle: true` opts into this separately trusted
host integration. The default is a no-op lifecycle and snapshot-free completion. The generic run
store accepts optional, bounded `adapter_state` metadata and atomically commits it with completion,
without interpreting persistence semantics. Hermes returns its checkpoint pointer in that metadata.
There is no automatic cross-framework migration or metadata-based framework routing. Keep
existing conversations on compatible adapter deployments. Hermes retains its legacy `hermes`
storage namespace and checkpoint encoding, while new namespaces are created by the harness
with descriptor-relative no-follow opens. The backend only provisions the shared workspace.

Sandbox events flow through the supervisor into fenced DynamoDB records; the independent
`backend/events.py` stream publisher sends AppSync notification hints and authenticated HTTP
reads replay the durable events. Neither Hermes nor any other adapter receives publishing or
AWS credentials. The current model broker supports Anthropic Messages only; another framework
must use that interface and honor the HTTPS proxy, or receive a separately implemented broker
protocol extension.

### Portal services

The serverless architecture uses CloudFront/S3 for React, a FastAPI/Mangum backend on Lambda behind
API Gateway HTTP API, AppSync Events for notifications, Cognito,
DynamoDB ownership/session records, Secrets Manager account passwords, EFS, and a shared
AgentCore Runtime. HTTP runtime contract: GET /ping and POST /invocations on port 8080.

See the [serverless gateway](../session-gateway/TECH.md) for transactional DynamoDB run ownership,
durable replay, the dispatch/publish stream consumers, and failure reconciliation.

Agent provisioning is an idempotent state machine. The backend authenticates agent users via
AdminInitiateAuth and calls AgentCore with the access token. A Cognito pre-token V2 trigger emits
`team_ids` arrays for humans and exactly one `team_id` for agents. Separate app clients plus group
enforcement prevent humans authenticating as agents and agents entering the human portal.
The supervisor validates the singular agent team claim and permanently binds a microVM session
to an agent sub. A multi-team agent token is rejected before any workspace is selected.
Agent creation also persists a boolean Security Test Mode setting as a Cognito custom attribute.
The pre-token trigger adds a typed `security_test_mode` claim to agent access tokens. The runtime
requires exact equality between that claim and the backend payload before conditionally adding
`SECURITY_TEST_MODE=true` to bubblewrap's otherwise cleared environment. False mode omits the
variable entirely; no caller-controlled environment map is accepted.

The mutable input and output limits follow the same signed-claim path. For token input limits the
worker uses Hermes `estimate_tokens_rough` against the new user message; MB limits use UTF-8 bytes.
Zero skips the application input check. The independent output value constructs AIAgent with that
value as `max_tokens`, while zero passes `None`; the trusted model broker rejects output requests
above a positive cap. Updating either limit globally signs out the agent identity; the next
invocation obtains a fresh token and restarts an existing persistent worker from its last
checkpoint if configuration differs. Legacy combined `token_budget` values map to both new limits
in token units until edited. Infrastructure/provider request-size limits still apply at zero.

Agents are globally discoverable from non-sensitive DynamoDB metadata. The persistent agent record
is keyed by agent UUID; conversations and messages follow that agent. All sensitive endpoints load
current Cognito memberships and require `agent.team_id in human.team_ids`. Humans can belong to up
to 20 teams. Agents have exactly one team and may be moved by an administrator, which immediately
changes the users able to access the agent's shared workspace and skills. Conversations and their
history additionally require the immutable human `owner_sub`; administrators cannot read another
human's conversation. Legacy ownerless conversations are hidden pending verified ownership migration.

## Sandbox boundary

Trusted supervisor code runs outside bubblewrap. The selected adapter, its framework and every tool run inside. An empty
mount namespace exposes readonly code/libraries, private /proc, /dev and /tmp, and exactly one
EFS directory. Directory selection uses a canonical UUID and descriptor-relative O_NOFOLLOW
opens; no untrusted path is accepted. No outer directory descriptor may reach the final process.
User, mount, PID, IPC, UTS and network namespaces, no_new_privs, dropped capabilities, and a
restricted environment are mandatory. Privileged host Docker execution is not a deployment
requirement and does not count as evidence of AgentCore support.

The descriptor guarantee has a known startup gap: installed Python hooks can run before the
worker closes inherited outer descriptors and installs seccomp. Remediation is deferred; see
[finding 4 and its validation limits](../../docs/ISOLATION.md#finding-4--python-startup-hooks-precede-descriptor-cleanup).

The runtime broker exposes constrained Bedrock inference and an internet egress proxy,
not generic filesystem operations. The sandbox has no direct network interface. Egress must
reject private/link-local/metadata addresses, NFS, DNS rebinding and redirects to blocked targets.
The live probe has no network broker: it proves default-deny before access is added. The full
worker installs a seccomp policy denying namespace manipulation, mount, ptrace, BPF and other
privileged operations; clone3 returns ENOSYS for a safe fallback to constrained clone calls.

## Persistence

Run history/event persistence is generic. Database snapshots and the SQLite recovery details
below apply specifically to the Hermes implementation.

Execution policy is immutable through the application after agent creation. `execution_mode`
is `sequential` (default for absent values) or `concurrent`, persisted in agent metadata and
`custom:execution_mode` in Cognito. The token trigger emits the mode, runtime admission compares
it with the payload and frozen run mode, and chat/settings payloads cannot change it.

Sequential runs use `AGENT#<id>/LOCK` plus the EFS execution lock. Concurrent runs use
`CONVERSATION#<agent-id>#<conversation-id>/LOCK` only: there is no agent-wide reservation or EFS
lock. This retains per-conversation ordering and duplicate-run protection while permitting
cross-session shared-file races explicitly requested by the agent creator. Completion and
reconciliation release the key derived from the run's stored mode, not mutable current metadata.

EFS access point root /agents is mounted at /mnt/agents. The supervisor binds only a verified
agent's workspace child at /workspace/workspace, skills child at /shared/skills, and
hermes/agent child at /shared/agent for MEMORY.md, USER.md and SOUL.md. Its whole
root and old shared Hermes logs/memory are not mounted. HOME=/state/home and HERMES_HOME=/state
are session-private; the sandboxed Hermes adapter pins terminal.cwd=/workspace/workspace and
TERMINAL_HOME_MODE=profile. Skill content is shared via external_dirs/create_dir while usage,
logs and request dumps stay private. A pinned Hermes path adapter preserves memory file
locking and atomic replacement while routing both MEMORY.md and USER.md to agent-scoped shared
storage; the SOUL loader uses the same directory. Prompt views refresh before each turn. Shared
memory/persona writes are not rolled back with SQLite checkpoints. Private auxiliary files are currently ephemeral;
SQLite recovery remains durable. Trajectory exports and shared skill-ledger writes are disabled.
Each conversation/session has its own active SQLite database at /state/state.db in a private
local temporary directory. The durable EFS checkpoint is
/mnt/agents/.control/<agent-sub>/<conversation-id>.<run-id>.db, using the stable conversation ID as
the session-specific prefix. The completion transaction atomically commits its pointer in
`adapter_state` on the conversation record alongside the final message. The Hermes host lifecycle
restores that pointer (or legacy `checkpoint_name` metadata); a
missing committed file fails closed. Before a pointer exists, the legacy session-specific
<conversation-id>.db is restored if available, otherwise Hermes creates a fresh database.
Uncommitted run files are ignored. The legacy agent-wide state.db is never used
to seed a session. Hermes currently selects DELETE journaling for the base image's SQLite version.
After a successful turn the Hermes adapter creates a consistent SQLite backup; its trusted host
lifecycle copies bounded opaque bytes to an immutable EFS candidate. Failed finalization discards the worker and
leaves the previous committed pointer authoritative.
The supervisor never opens agent-controlled SQLite data with its own SQLite library.

Sequential execution serialization uses an EFS advisory lock in .control/<sub>, outside the agent mount,
rather than an expiring DynamoDB lease. It is held through worker termination and checkpoint
publication. Locks are never forcibly stolen on a timeout. A crash can lose the incomplete turn's
conversation state; completed-turn checkpoints and already-written EFS artifacts persist.
Each conversation stores one AgentCore runtime session ID. Every turn in that conversation reuses
the ID, allowing AgentCore to retain its supervisor microVM. Legacy conversation records receive
an ID using a conditional DynamoDB update so concurrent requests cannot split a conversation
between sessions. AgentCore suspends an idle session after 15 minutes and caps its lifetime at
8 hours. One bubblewrap worker, SessionDB and AIAgent remain alive for that AgentCore session and
process sequential turns through a correlated line protocol. Each turn has a one-hour runtime and
Hermes execution budget. Each successful turn writes a
conversation-specific EFS checkpoint. A fatal error terminates the worker; its next invocation
starts a clean sandbox from the last completed checkpoint. Different conversations never publish
the same SQLite snapshot, preventing an idle persistent worker from overwriting another
conversation's newer state.

Hermes uses `stream_delta_callback(None)` as a boundary between pre-tool commentary segments.
The worker translates it to `segment_end`; the UI keeps only the latest interim segment in a
separate work-update surface and clears it when final output arrives. A non-failed
`completed=False` result is terminal `partial`, not a worker failure: state is checkpointed,
available text is persisted, and the exit reason is shown. Broker servers suppress only expected
BrokenPipe/ConnectionReset exceptions caused by clients closing error responses; unexpected
broker exceptions retain normal traceback logging.

## Agent tracing

The supervisor initializes ADOT providers explicitly; the sandbox emits bounded telemetry
callbacks over its existing request-correlated pipe. Agent spans cover the full background turn,
broker-thread model spans inherit its context, and tool spans represent lifecycle callback
observations. No prompt/response/tool payloads are captured. See [observability](../../docs/OBSERVABILITY.md)
for export settings, CloudWatch queries and interpretation of usage/iteration counts.

## Probe scope

The probe uses IAM inbound authentication and fixed synthetic agent UUIDs, not Cognito accounts.
It accepts only diagnostic operations, no user code or shell commands. This deliberately tests
the platform boundary before introducing credentials, models or the portal. Its report must
distinguish process startup failure, failed assertions, and successful assertions.

Infrastructure creates an isolated VPC (or imports supplied VPC/subnets), EFS, a scoped execution
role, ARM64 Docker asset and runtime. The installed CDK and regional CloudFormation schema both
support native FilesystemConfigurations; no custom provisioning resource is required. A Python
command invokes diagnostics, stops the sessions and saves structured evidence locally.

## Sources

- https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-filesystem-configurations.html
- https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-security-best-practices.html
- https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-header-allowlist.html
- https://github.com/containers/bubblewrap
- https://github.com/NousResearch/hermes-agent (inspected HEAD 045eb44363464072637a4661d02bf1d6ce9b9c38)

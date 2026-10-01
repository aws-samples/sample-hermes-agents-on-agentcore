# Hermes Agent Portal

## Approved scope

- A framework-independent sandbox harness, with NousResearch Hermes as its default agent adapter
  and Amazon Bedrock inference through a harness-owned model broker.
- One shared AgentCore Runtime in VPC mode.
- Existing VPC if supplied; otherwise create a VPC.
- Cognito human login and a separate Cognito account for every agent.
- Humans may belong to multiple teams; agents belong to exactly one team.
- Every human can discover every agent. Workspace artifacts and skills require a shared team;
  conversations and messages additionally require the authenticated conversation owner.
- Backend-mediated agent access tokens and current Cognito membership enforcement.
- Per-agent EFS persistence for shared artifacts, skills, MEMORY.md, USER.md and SOUL.md.
  Conversation state and logs remain private to each conversation.
- Hermes-specific per-session SQLite snapshots with local working copies and session-prefixed
  EFS publication. Other frameworks choose their own persistence policy, including none.
- All agent framework code and tools confined to a sandbox exposing only verified shared children.
- Controlled internet access, with creation-time sequential or concurrent agent execution.
- React/TypeScript portal, Python backend and TypeScript CDK.

## User experience

### Framework independence

The platform harness owns sandbox creation, model/egress proxies, worker lifecycle and durable
event publishing. The sandboxed agent owns its framework, tool callbacks,
conversation-state encoding and reasoning loop. A deployment selects one adapter at image build
time; implementing another Python agent framework must not require changes to the harness,
backend event publishers or browser event protocol. Hermes remains the default implementation.
Snapshots belong to the Hermes implementation, not the generic harness or adapter contract.
Hermes preserves its recovery behavior through its own sandboxed backup code and trusted host
lifecycle integration. The included Echo adapter demonstrates durable completion without installing
Hermes, calling a model or implementing persistence. Its memory lasts only for its worker process.
Per-agent framework selection in the portal and cross-framework checkpoint migration are outside
this change. Existing conversations remain pinned operationally to compatible adapter deployments.

### Portal behavior

An invited user signs in and sees the global agent directory. Agents sharing any of the human's
teams can be used as teammates: users open conversations, receive streamed responses and browse
artifacts. Agents without a shared team remain visible but unavailable. When creating an agent,
the user chooses exactly one of their teams. A new conversation retains skills and artifacts,
but starts with a fresh SQLite database. Resuming a conversation restores only its own database.
Agent creation optionally enables Security Test Mode. When selected, and only when the signed
agent identity carries the matching claim, its sandbox receives `SECURITY_TEST_MODE=true`.
The mode is fixed when the agent is created and does not weaken sandbox controls.
Agent creation also chooses **Sequential (lock)** or **Concurrent (no agent-wide lock)** execution.
Sequential is the default, including for existing agents. It permits one active conversation per
agent. Concurrent permits different private conversations to execute simultaneously and write the
shared workspace/skills without agent-wide serialization. Each conversation still has one active
turn, and run idempotency/ownership protection remains. Execution mode is fixed at creation.
Agent creation also accepts independent limits that teammates can edit later. The input limit has
a value and either token or MB units; zero disables the application input check. The output limit
is measured in tokens and is passed to Hermes as `max_tokens`; zero passes `max_tokens=None`.

Administrators use a dedicated portal to create/rename teams, invite/disable/delete human users,
grant administrative access, assign humans to one or more teams, and move an agent between teams.
The UI and API prohibit assigning an agent to multiple teams because that would bridge security
domains through the agent's memory and workspace. Moving an agent requires explicit transfer
confirmation because its shared workspace and skills become available to the destination team.
Private conversations never transfer to other users; owners must still share the agent's current team.

No UI mock has been supplied. The portal will use a compact agent sidebar and conversation view.

## Invariants

1. Agent identity comes from a validated access token, never a caller-selected filesystem path.
2. Current human/agent team intersection is checked before every conversation or artifact operation.
   Conversation listing, history, sends, replay and subscriptions additionally require owner equality;
   administrators do not bypass this rule. Unowned legacy conversations are hidden, not claimed by a caller.
3. Agent credentials and runtime credentials are never exposed to the browser or Hermes sandbox.
4. Cross-agent filesystem, process, session and network access fails closed.
5. An unavailable sandbox must never result in unsandboxed execution.
6. Sequential agents serialize turns with DynamoDB and EFS locks. Concurrent agents intentionally
   allow shared workspace/skill writes from separate conversations; private SQLite state remains
   session-specific and each conversation's turns remain serialized.
7. Agent tokens contain one `team_id`; human tokens contain a non-empty `team_ids` array.
8. Invocation payloads cannot enable Security Test Mode without the matching agent token claim.
9. Invocation payloads cannot alter input or output limits without matching agent token claims.

## Delivery gate

Before portal implementation, a real AgentCore microVM must demonstrate namespace isolation,
single-directory EFS access, blocked direct network/metadata access, no ambient credentials,
and EFS persistence across different runtime sessions. Failure blocks the shared-runtime design;
the topology will not silently change.

## Acceptance

The harness must complete multiple turns with the non-Hermes adapter, reuse and replace its worker,
and persist/replay its events without requiring any snapshot API or file through the same
durable-run pipeline. Security restrictions must be applied before importing adapter code.
Hermes memory scoping, callbacks, input/output limits and SQLite recovery must retain their behavior.

Multiple human users and agents must demonstrate denied cross-team requests, global discovery,
same-team access, rejected multi-team agent identities, rejected
expired/forged/wrong-client tokens, rejected session substitution, retained artifacts and skills,
bounded execution, and durable recovery. State-checkpoint recovery must not use a live SQLite
WAL database on NFS. The isolation probe is necessary but not sufficient for production approval.

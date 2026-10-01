# Hermes lifecycle benchmark

Measured locally on Apple Silicon using the production ARM64 runtime image. The container ran
with `--network none`; inference was served by a deterministic Anthropic-compatible loopback
server. No AWS, Bedrock, or external API key was used. A minimal fresh models.dev cache and
disabled title generation prevent public metadata timeouts from contaminating the result.

Command:

```sh
docker run --rm --network none --platform linux/arm64 \
  -v "$PWD/benchmarks/hermes_lifecycle.py:/benchmark.py:ro" \
  -v "$PWD/.deployment:/results" \
  hermes-agentcore-runtime python /benchmark.py --turns 10 \
  --output /results/hermes-lifecycle-benchmark.json
```

## Results

| Lifecycle | First turn | Steady median | Peak RSS |
| --- | ---: | ---: | ---: |
| Fresh Python/Hermes process per turn | 10,922 ms | 10,834 ms | 129.5 MiB per process |
| Persistent process, fresh AIAgent per turn | 9,481 ms | 76.8 ms | 130.5 MiB process |
| Persistent process, reused AIAgent | 36.3 ms | 65.6 ms | Included above |

Fresh-process median composition:

- Process startup and Hermes imports: approximately 1,369 ms.
- SessionDB and AIAgent initialization: 320 ms.
- First `run_conversation` in each process: 9,156 ms.

Keeping the process alive removes almost all repeated cost. Reusing the AIAgent object saves only
another 11.2 ms over creating a fresh AIAgent in an already-warm process. Therefore the important
architecture change is a persistent sandbox worker, not necessarily a long-lived AIAgent object.

## Hermes HTTP support

Hermes exposes an authenticated OpenAI-compatible HTTP API through `hermes gateway` when
`API_SERVER_KEY` is configured. Stable `X-Hermes-Session-Id` headers preserve transcript
continuity. However, the stock API server creates a new AIAgent for every HTTP request; it keeps
the Python process, module imports, registries and SessionDB infrastructure warm.

The benchmark's second mode models that lifecycle without the unrelated gateway adapters. The
third mode proves that sequential reuse of one AIAgent works locally, but this is not the stock
HTTP server's behavior.

## Implemented architecture

The AgentCore runtime now uses the third lifecycle: one persistent bubblewrap worker and AIAgent
per runtime session/conversation. Requests and streamed events use correlated JSON lines. A random
worker instance ID is emitted on completed turns for reuse verification. Checkpoints are isolated
per conversation and published only after successful turns; fatal failures discard the process
and restart from the prior checkpoint.

Live browser validation in eu-west-1 confirmed that two turns in one conversation retained both
the same AgentCore `runtimeSessionId` and the same random Hermes worker instance ID. A new
conversation receives a separate runtime session and worker.

## Caveats

- The mock model has near-zero latency. Real Bedrock latency reduces the relative speedup but not
  the roughly 10.7-second local lifecycle cost avoided per turn.
- The benchmark excludes bubblewrap startup, EFS checkpoint copy and network/model latency. It
  therefore does not exaggerate those production costs.
- Results measure this pinned Hermes build and runtime image on this machine, not a service SLA.
- A persistent worker must remain one-conversation-per-AgentCore-session and preserve the current
  EFS lock, token binding, seccomp, timeout, cancellation and checkpoint boundaries.

<!-- ---
!-- Timestamp: 2026-09-09 09:58:12
!-- Author: ywatanabe
!-- File: /ssh:ywatanabe@scitex-compute-04:/home/ywatanabe/proj/scitex-agent-container/docs/SETUP_LOCAL_LLM.md
!-- --- -->

# Using the local Qwen engine from agents

This package consumes the local LLM; it does not lease GPUs or launch model
servers. Those responsibilities belong to `scitex-hpc` and `scitex-genai`.

## Stable endpoint

Agents use one gateway address:

```text
http://scitex-compute-04:18772
```

The tracked provider name is `qwen-gateway`. Its host-aware definition lives
in `src/scitex_agent_container/config/_qwen_gateway.py`; override it on a host
with `SAC_QWEN_GATEWAY_URL`. The token value is read from
`SCITEX_GENAI_GATEWAY_API_KEY` by default and must not be written into a spec.

The gateway supports Anthropic Messages, OpenAI chat completions, and OpenAI
Responses. It hides one or more two-GPU inference replicas and keeps each
conversation sticky to one replica so SGLang's radix prefix cache is reused
across turns. Replicas can appear or disappear without changing the endpoint.

SAC's Codex pilot adapter sets the inference provider's
`stream_idle_timeout_ms` to 3,660,000 for long autonomous local-model canaries.
Codex otherwise disconnects after 300,000 ms of parsed-SSE silence. This was
measured directly: a valid SGLang response arrived after 301.3 seconds and was
lost with the default, while a 301.7-second response was consumed with the SAC
override. The pilot process still enforces its explicit outer task timeout, so
the longer stream allowance is bounded. SSE comment heartbeats do not reset
Codex's parsed-event timer.

## Select Qwen for an agent

The engine definition and its selection are explicit in each agent spec:

```yaml
spec:
  runtime: tui
  harness: codex
  engine: qwen38-27b
  available_harnesses:
    codex:
      session: {mode: continue, max_age_minutes: null}
      channels: [server:claude-code-telegrammer, server:sac]
      approval_policy: never
      sandbox_mode: danger-full-access
  available_engines:
    qwen38-27b:
      model: qwen38-27b
      provider:
        base_url: http://scitex-compute-04:18772
        auth_token_env: SCITEX_GENAI_GATEWAY_API_KEY
      max_context_tokens: 1048576
      reasoning_effort: low
```

The Claude Code prompt watchdog is not a global lifecycle policy. A spec that
offers that harness declares it under
`available_harnesses.claude-code.watchdog`; a Codex-only spec omits it.
Newly authored harness labels use product names (`claude-code`, `codex`), not
the ambiguous compatibility labels `claude` or `anthropic`.

Or select it for one start/restart:

```bash
sac agents start AGENT --engine qwen38-27b --probe-engine
sac agents restart AGENT --engine qwen38-27b --probe-engine
```

Do not couple `harness` and `engine`: the harness chooses the agent program;
the engine chooses the inference backend. The DeepSeek harness experiment is
retired and is not part of this setup.

## Hermes migration

Hermes is the staged Qwen application-agent harness. SAC keeps the spec,
Apptainer boundary, placement, secrets, stable routing, and neutral
agent/spec/session/incarnation identities. Hermes keeps the agent loop,
sessions, context management, tools, queue/steer/interrupt behavior, messaging
gateways, goals, subagents, Kanban, cron, and native memory.

The integration boundary is the authenticated Hermes gateway API. A SAC
message id maps to `Idempotency-Key`; a SAC session id maps to the Hermes
session key/id. The selected Qwen endpoint and absolute container workdir are
compiled into a credential-free derived Hermes profile. Operators do not edit
that artifact, and secrets remain environment references.

On `scitex-compute-03`, Hermes 0.21.1 at upstream commit
`a74e76632cce62ad6948cf7e4e6b27629d66d147` completed the Qwen task/resume,
busy-steer, stop, unclean-restart, and Apptainer absolute-workdir probes. The
image-installed production Scholar agent then passed name-only SAC launch,
Cards and SAC MCP discovery, a representative repository task, exact session
recall after SAC restart, and durable SAC-bus admission with explicit
post-acceptance acknowledgement. The Codex/TUI path remains a rollback while
Hermes is staged beyond Scholar.

Hermes inbox consumers use
`GET /agents/<name>/inbox/stream?ack=explicit`. SAC leaves each PostgreSQL row
undelivered until the bridge has received HTTP 202 from Hermes, after which the
bridge calls `POST /agents/<name>/inbox/ack` with the SSE event id. A crash
before admission causes replay; a busy Hermes gateway causes bounded retry of
the same event and idempotency key.

Hermes launches do not materialize `.claude` commands, hooks, settings, or the
developer host deep-merge. The neutral `to_home` cascade still supplies MCP,
environment, and git configuration. This distinction is selected from
`spec.harness`; no second operator switch is required.

SAC disables Hermes tool-approval prompts by default. The agent therefore has
full authority within its declared container view; Apptainer binds, identity,
network, and injected credentials remain the security boundary. This is
independent of `autonomous.enabled`: that setting controls whether the agent
continues taking turns, not whether an individual tool call asks a human for
permission.

See [ADR-0027](adr/0027-hermes-owned-agent-control-plane.md) and the
[pilot record](../examples/pilots/hermes/README.md). Do not expand SAC's
transitional tmux queue with Hermes features that Hermes already provides.

## Cache affinity

Preserving request identity matters more than naive per-request round-robin.
When an integration can set headers directly, reuse one stable `session_id`
for the life of the conversation. Otherwise the gateway derives its key from
the prompt preamble and first real turn. Do not generate a new ID for every
turn and do not bypass port 18772 to choose replicas manually.

## Verify before restarting an agent

```bash
curl -sS -o /dev/null -w '%{http_code}\n' \
  http://scitex-compute-04:18772/v1/models
sac agents engine-check /path/to/spec.yaml --engine qwen38-27b --count 5
```

An unauthenticated `401` from `/v1/models` proves the inference API is present
and auth-gated. DNS failure, timeout, connection refusal, authentication
failure, and an upstream HTTP error are different conditions; keep the
response body when diagnosing them.

## Current server contract

- model ID: `qwen38-27b`
- context ceiling: 1,000,000 tokens
- engine: SGLang
- model profile: Qwen3.8-27B FP8, FP8 KV, YaRN, DeepGEMM, MTP/EAGLE, TP=2
- performance target: at least 100 generated tok/s once scheduled; queue delay
  under concurrent long prefills requires additional TP=2 replicas

The engine mechanics and acceptance tests are documented in
`scitex-genai/docs/SETUP_LOCAL_LLM.md`. Allocation recovery is documented in
`scitex-hpc/docs/SETUP_LOCAL_LLM.md`.

<!-- EOF -->

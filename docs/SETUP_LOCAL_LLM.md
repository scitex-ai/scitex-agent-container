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
      max_context_tokens: 1000000
      reasoning_effort: low
      timeouts:
        upstream_deadline_seconds: 1800
        client_abandonment_seconds: 1860
```

The client deadline is intentionally later than the inference gateway's
upstream deadline. Long-context Qwen requests have taken more than 18 minutes
before producing a response; a shorter harness stale detector abandons useful
GPU work and can create duplicate queued requests. SAC validates the ordering
and writes the effective client deadline into Hermes' supported per-model
request and stale-timeout fields. A dead provider remains bounded by the finite
client deadline.

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

For `runtime: tui`, the official Hermes Ink TUI and SAC's inbound adapter are
two clients of one loopback Hermes gateway. A2A, Cards, and CCT messages enter
through SAC's neutral inbox event, then Hermes `prompt.submit`; an active turn
returns `steered` under the generated `display.busy_input_mode: steer`.
Accepted input and later events remain visible in the attached TUI. Prompt
delivery does not expose terminal-control key injection; interactive modal
control belongs to the attached native TUI.

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

### Subagent delegation and parallel work

Hermes 0.21.1 can spawn subagents. Its `delegate_task` accepts a `tasks` array
and executes those independent entries concurrently; `display.busy_input_mode`
set to `queue` controls messages arriving while the parent is busy and is unrelated to
subagent scheduling. Sending ordinary turns one after another therefore still
looks sequential. The parent must make one batch delegation (or background
delegations) to use parallel capacity.

SAC makes that capability and its bound explicit in the agent spec:

```yaml
spec:
  lineage:
    may_spawn: true
  delegation:
    max_concurrent_children: 2
    worktree_isolation: true
```

`may_spawn: false` removes Hermes' `delegation` toolset, so `delegate_task` is
not merely discouraged in the prompt; it is absent from the model's callable
tools. If `lineage` is omitted, the backward-compatible value is `true`.
`max_concurrent_children` defaults to 2 and is limited to 1–8, rather than
silently inheriting Hermes' upstream default of 10. It is a per-parent cap and
SAC also fixes `max_spawn_depth: 1`, so child agents cannot multiply it through
another fan-out level.

Keep external, metered engines such as DeepSeek at 1–2 children unless a task
justifies additional spend. A measured local Qwen deployment may author 3–4
when its inference replicas have capacity. These are authored workload limits,
not assumptions inferred from model or provider names, so switching engines
does not silently change concurrency.

Hermes' `worktree_isolation` applies only to Git workspaces using its local
terminal backend. It requests a separate Git worktree per child; it is not a
general container or filesystem namespace. Parallel editing tasks must still
own disjoint Cards and branches/worktrees. Do not give two children the same
Card or working tree, and do not treat this flag as isolation for a non-Git
directory or shared services such as a database.

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

The measured two-H100 production profile, cache tiers, admission limits,
throughput window, reproducible image digest, and unresolved risks are recorded
in [QWEN_AGENT_FLEET.md](QWEN_AGENT_FLEET.md). Treat that file as a dated
observation rather than a permanent capacity promise.

The engine mechanics and acceptance tests are documented in
`scitex-genai/docs/SETUP_LOCAL_LLM.md`. Allocation recovery is documented in
`scitex-hpc/docs/SETUP_LOCAL_LLM.md`.

<!-- EOF -->

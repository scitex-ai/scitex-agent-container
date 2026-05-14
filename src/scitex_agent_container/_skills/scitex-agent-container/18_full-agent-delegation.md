---
description: |
  [TOPIC] Full-agent delegation via sac (vs subagent)
  [DETAILS] How to delegate a multi-step task to *another full Claude Code agent* using sac, instead of spawning a Task-tool subagent. Covers the trade-off (full filesystem + bash + MCP vs. limited surface), the minimal YAML, lifecycle (validate → start → status → inspect → stop), prompt design, and when to choose this over Task. Use when a task needs autonomous read-write capability over many turns, parallel execution alongside the parent agent, or isolation from the parent's context budget.
tags: [scitex-agent-container-full-agent-delegation, full-agent, claude-session, sac, vs-subagent]
---

# Full-agent delegation via sac

When the parent agent needs to delegate a multi-step task to another agent
that has the **same capabilities as itself** — full filesystem access,
bash, all MCP tools, multi-turn iteration, independent context window —
launch a fresh Claude Code agent through sac instead of spawning a
Task-tool subagent.

> Operational deep-dives (peer-stuck recovery, reaper fleet pattern,
> hard/soft skill internals, observing peers via Monitor) live in the
> sibling leaf [19_full-agent-troubleshooting.md](19_full-agent-troubleshooting.md).

## Full-agent vs subagent — pick the right one

| Concern | `Task` subagent | sac peer agent |
|---|---|---|
| Filesystem | Read/Edit/Write only | Full (Bash, NotebookEdit, …) |
| Tools | Curated subset per agent type | Same as parent — all MCP, all built-ins |
| Turns | One-shot reply (~minutes) | Hours to days |
| Parent's context | Counts against parent | Independent — parent gets a small report |
| Permission model | Inherits parent's | `bypassPermissions` available |
| Parallel parent work | Parent blocks until reply | Parent stays free; check status async |
| Per-agent state | Discarded after reply | Persisted under `~/.scitex/agent-container/runtime/<name>/` |
| Cost | Same flat-rate Claude Code billing | Same flat-rate Claude Code billing |

**Use a sac peer agent when**: the task is *substantial* (≥ a working
day's worth of edits), needs *autonomous bash*, or you want the parent
free to do other work in parallel. **Use Task** for short, well-scoped
research or single-file edits.

## Minimal YAML

```yaml
# ~/.scitex/agent-container/agents/<name>/<name>.yaml
apiVersion: scitex-agent-container/v3
kind: Agent

metadata:
  labels: { project: <my-project>, purpose: <one-line> }

spec:
  runtime: claude-session         # SDK-native, no tmux pane scraping
  model: sonnet                   # alias for the latest Sonnet (4.6+)
  workdir: /absolute/path/to/repo
  # WARNING (F-CS8): NEVER point workdir at an umbrella directory
  # that contains a heavy ``.claude/`` tree (e.g. ``~/proj/`` when
  # ``~/proj/.claude/`` weighs tens of MB of hooks/skills).
  # claude-agent-sdk auto-discovers ``<workdir>/.claude/`` and silently
  # swallows errors when the tree is too large or contains a failing
  # hook — every turn returns 0 tokens, no log line, heartbeat fresh.
  # Use a project-specific subdir (e.g. ``~/proj/<this-project>/``) or
  # ``/tmp/<scratch>/`` and reference other repos via absolute paths.
  # ``sac agent start`` emits a stderr precheck warning when the
  # workdir's ``.claude/`` exceeds 10 MB.

  startup_commands:
    - command: |
        You are a SourceDeveloperAgent working on <package>.

        TASK: <one paragraph>.

        SCOPE: <bullets — what to do, what NOT to do>.

        CONSTRAINTS:
          - Read ~/.claude/skills/<relevant>/ first.
          - Do not push to remote without explicit confirmation.
          - Tests must pass before claiming DONE.
          - If blocked by a design question, write to GITIGNORED/QUESTIONS.md
            and stop — do NOT guess.

        DELIVERABLES:
          1. <output>
          2. <output>
          ...
        Print on stdout: "DONE <name> <one-line summary>".

        Begin by reading <pointer files>.

  health: { enabled: true, interval: 60 }
```

The startup `command` *is* the delegation contract. Be specific:
inputs, scope, constraints, deliverables, exit signal.

## Why "peer" (not "subagent")

A sac-launched agent is a **peer**, not a subordinate. With
`spec.a2a.port` set, the agent serves `POST /v1/turn`; any other peer —
including but not limited to the launching agent — can `sac peer
post-turn <name> "..."` to drive a new turn into its live SDK session.
Peers form a mesh: the launcher talks to peer A, peer A talks to peer
B, peer B reports back to the launcher, etc. There is no hierarchy
imposed by the runtime itself; structure is something *you* design on
top.

Contrast with the Task tool's `subagent`: a subagent runs once, replies
to the parent, and disappears. Peers persist, accept multiple turns
from multiple senders, and can themselves spawn further peers.

## Lifecycle

```bash
# 1. Validate (fast — schema check only)
sac agent validate ~/.scitex/agent-container/agents/<name>/<name>.yaml

# 2. Start
sac agent start ~/.scitex/agent-container/agents/<name>/<name>.yaml

# 3. Verify it's running
sac agent status <name>      # full table
sac agent inspect <name>     # last-N tool calls + current task

# 4. (optional) Talk to it
sac peer call <name> "How is the F2 implementation going?"
sac a2a doctor <name>        # health probe

# 5. Stop / restart
sac agent stop <name>
sac agent restart <name>
```

## Why `runtime: claude-session` (default)

| Runtime | Drives | How |
|---|---|---|
| `claude-session` (default) | `claude-agent-sdk` Python library | direct, no multiplexer, structured tool events |
| `claude-cli-tui` (formerly `claude-code`) | the `claude` CLI binary | tmux/screen pane, send-keys + screen-scrape (TUI mode) |

`claude-session` is the default — lower latency, no auto-accept hacks,
no pane scraping, programmatic tool events. Most new agents should use
it.

`claude-cli-tui` is **intentionally retained** as a stability fallback
while `claude-session` matures: when the SDK runtime is unavailable,
broken, or feature-incomplete on a given host, `claude-cli-tui` provides
a known-good path through the same Anthropic CLI a human would run
interactively. The orthogonal `spec.multiplexer: tmux|screen` field
chooses *which* multiplexer to use under it.

The name `claude-code` (legacy alias) will continue to be accepted but
new agents should write `claude-cli-tui` to make the implementation
mechanism explicit.

See [15_claude-session.md](15_claude-session.md) for the runtime details.

## Model selection — `sonnet` vs `opus`

Spell the alias, not a pinned version. `sonnet` and `opus` resolve to
the latest production model at the time the agent connects to the SDK.
Pin only when you need reproducibility (`claude-sonnet-4-6`,
`claude-opus-4-7[1m]` for 1M-context Opus, etc.).

| Task complexity | Default model |
|---|---|
| Mechanical (git commit, file rename, report formatting) | `haiku` |
| Routine development (most peer scopes) | `sonnet` |
| Hard reasoning, architecture, dep audits, foundation phase | `opus` (or `claude-opus-4-7[1m]` for very long-context tasks) |

**Empirical lesson (2026-05-05)**: `sonnet` peers occasionally went
idle mid-task on substantial scopes (the SDK turn returned with no
assistant text and no tool calls). Re-spawning the same yaml on
`claude-opus-4-7[1m]` shipped the work quickly. During foundation
phase, prefer `opus` for non-trivial scopes — eliminates "is the model
smart enough?" as a confounder. The cost of a failed-then-respawned
peer is higher than launching with the larger model once.

## A2A asymmetry — parent vs peers

The relationship between the launcher and a peer depends on how the
launcher is running:

| Launcher kind | Outbound (drive a peer) | Inbound (receive from peer) |
|---|---|---|
| Another sac peer (claude-session) with `a2a.port` | `sac peer post-turn <other>` | peers can post to its `/v1/turn` |
| **Claude Code CLI** (the human-facing supervisor) | `sac peer post-turn <peer>` (works) | **no native push** — CLI doesn't expose `/v1/turn` |
| Plain shell script | `sac peer post-turn <peer>` (works) | no |

Consequence: when the orchestrator is Claude Code CLI, peers cannot
push state back to it. The orchestrator must **observe peers via the
filesystem** — see [19_full-agent-troubleshooting.md](19_full-agent-troubleshooting.md)
for the `Monitor`-based push-notification recipe.

## Related

- [15_claude-session.md](15_claude-session.md) — runtime details
- [19_full-agent-troubleshooting.md](19_full-agent-troubleshooting.md) — stuck-peer recovery, reaper pattern, hard/soft skills, Monitor over polling
- [07_a2a-protocol.md](07_a2a-protocol.md) — agent-to-agent message bus
- [10_cli.md](10_cli.md) — full sac CLI reference
- [20_env-vars.md](20_env-vars.md) — environment variables consumed by runners
- `~/.claude/skills/ywatanabe/07_agent-rules/07_orchestrator_02_sac-peer-mesh.md` — the orchestrator-side counterpart of this leaf
- `~/proj/scitex-agent-container/GITIGNORED/FEATURE_REQUESTS.md` — F-CS1..F-CS6 pending sac improvements (parameterised fleet, autonomous loop, verifier kind, A2A ACL, runtime rename)

---
description: |
  [TOPIC] Twin spawning — fork a context-inheriting twin of a running agent
  [DETAILS] `sac agents twin <parent>` (+ the `agent_twin` MCP tool) spawns a NEW agent that inherits the parent's LIVE conversation transcript at birth, then diverges — the parent never stops. Covers what a twin is, the three use cases (inherit-context-without-sharing-future-context / split parallel work / don't-block-the-parent), ephemeral vs persistent lifetime, the safety-critical identity split (writes authored as the twin, cards OWNED by the parent — and WHY), the transcript-inheritance mechanism, and when a plain Task subagent is the cheaper choice instead. Use when an agent needs a second self that carries its context.
tags: [scitex-agent-container-twin-spawning, twin, fork-session, claude-session, sac, identity-split, ephemeral, persistent]
---

# Twin spawning

A **twin** is a NEW agent forked from a running **parent**: it inherits the
parent's conversation transcript *at birth* (a fork of the parent's live
session), then diverges on its own. **The parent is never touched** — twin
spawning is how an agent splits off context-carrying work without pausing
its own main loop.

```bash
# ephemeral triage twin — inherits context, auto-stops after 30m
sac agents twin neurovista --task "audit the failing figures" --ttl 30m

# persistent writer companion sitting beside the parent
sac agents twin neurovista --name neurovista-writer --persist \
    --task "draft the results section"
```

An agent can spawn **its own** twin from inside its container via the MCP
tool `agent_twin(parent="<self>", task="...", persist=False)` — it brokers
to the host exactly like `agent_spawn`.

## What you get

The twin inherits the parent's spec **verbatim** — same repo, workdir,
image, apptainer binds, model, skills/hooks (`to_home`) — with only these
overridden:

| Field | Twin value | Why |
|---|---|---|
| name | `<parent>-twin` (or `--name`, bumped `-2`/`-3` if taken) | its own identity |
| `claude.session` | `continue` (marker seeded host-side) | inherit at first boot; continue own session on restart |
| `restart.policy` | `never` (ephemeral) / `always` (`--persist`) | lifetime, below |
| `a2a.port` | `auto` | a fresh sidecar port — never the parent's |
| `env.SCITEX_TODO_AGENT_ID` | the twin | writes authored as the twin |
| `env.SAC_TWIN_PARENT` | the parent | the owner-convention value (below) |
| channels | telegrammer dropped, `server:sac` kept | two agents must not fight one bot's getUpdates slot |

## When to use a twin — the three cases

1. **Inherit context, don't share future context.** You want a second self
   that knows everything you know *right now*, but whose subsequent turns are
   its own — not folded back into your conversation. The twin forks at birth
   and diverges.
2. **Split work across parallel twins.** Two (or more) twins each carry the
   parent's context and work different sub-tasks at the same time.
3. **Don't block the parent.** Long-running or heavy work runs in the twin
   while the parent's main loop stays free (the operator's rule:
   "24時間動かすこと" — keep the parent running).

## Ephemeral vs persistent — lifetime is independent of role

Twins are **general**, not just ephemeral cleanup workers. Lifetime and
role are **independent** parameters: an ephemeral triage twin and a
persistent companion are the SAME primitive with different lifetime
settings — nothing about twinning implies short-lived.

- **Ephemeral (default)** — `restart.policy: never`. A stopped twin does not
  come back. Add `--ttl <90s|30m|2h|1d>` for a best-effort auto-stop (a
  detached host-side timer; it does not survive a host reboot). Fully remove
  a finished twin with `sac agents delete <twin>`.
- **Persistent (`--persist`)** — `restart.policy: always`. A **first-class**
  use, per the operator: a long-lived companion standing by beside its
  parent, restarted on exit — e.g. a paper-writing twin always waiting next
  to its data agent ("論文書きのエージェントはいつでも…そばで待機していて
  欲しい"). `--persist` and `--ttl` are mutually exclusive (fail loud).

## Identity split — author = twin, owner = parent (READ THIS)

The operator's ask: a twin's writes should be attributed to the **twin's**
name ("分身の名前で書いて欲しい"). So `SCITEX_TODO_AGENT_ID` is set to the
twin — its scitex-todo `created_by` / comment author / actor are the twin.
That part is automatic.

**But scitex-todo card OWNERSHIP must stay with the PARENT — and this
CANNOT be enforced from env.** Verified against `scitex_todo._store`:
`add_task` **fails loud** without an explicit `assignee`, and
`SCITEX_TODO_AGENT_ID` feeds ONLY the author path — owner (`agent` /
`assignee` / `scope`) has no env default at all. So:

> **HARD RULE — the twin passes `assignee=<parent>` (== `$SAC_TWIN_PARENT`)
> on EVERY `add_task` / `reassign`.** Never leave a card owned by the twin.

`SAC_TWIN_PARENT` is injected into the twin's container precisely so this
value is always available, and the twin's boot-kick states the rule. **Why
it matters:** an ephemeral twin that owns cards and then exits strands them
in an inbox nobody drains — this is exactly the ownership-drift incident
that orphaned **75 cards** in one night. Author = twin; owner = parent;
coordinate results back to the parent via a2a or a parent-owned card.

## How context inheritance works (mechanism)

`sac agents twin` derives the twin's inline spec and POSTs it to the host
`sac listen` (the same broker `agent_spawn` uses). On the host, at twin
start, `_lifecycle._twin.seed_twin_from_parent` runs BEFORE the runtime
launches:

1. resolves the parent's **current** (possibly forked) session uuid from
   `<parent-state>/session_id`;
2. copies the parent's transcript
   (`runtime/<parent>/home/.claude/projects/<enc>/<uuid>.jsonl`) into the
   twin's container-home projects store, mirroring the project subdir;
3. seeds the twin's session marker to that uuid so `session: continue`
   resumes the copied transcript (TUI `-c` / the SDK marker).

This is **first-boot only** (keyed on the twin having no session marker yet):
on later restarts the twin `continue`s its OWN diverged session — a pinned
`resume` would instead re-fork from the parent each restart and discard the
twin's history — and a persistent twin keeps starting even after its parent
stops. Because the parent's uuid is resolved on the host at first-boot time,
the twin inherits the **freshest** transcript, and all paths resolve on the
bare host regardless of whether you ran `twin` on the host or brokered it
from inside a container. Fail-loud on first boot: if the parent has no live
session or its transcript is missing, the twin start aborts (a twin with no
inherited context is pointless).

## When NOT to use a twin

If you do **not** need the parent's conversation context, a twin is the
wrong (heavier) tool:

- **A `Task` subagent** is cheaper for short, well-scoped research or a
  single-file edit that needs no inherited context.
- **A fresh full agent** (`sac agents start <new>`, see
  [18_full-agent-delegation.md](18_full-agent-delegation.md)) or a
  worktree agent is the right call for a self-contained multi-step job
  that starts from a clean slate.

A twin's entire value is the inherited conversation. Reach for one only
when carrying the parent's context is the point.

## Related

- [18_full-agent-delegation.md](18_full-agent-delegation.md) — delegate to a fresh full agent (no inherited context)
- [15_claude-session.md](15_claude-session.md) — the SDK/TUI runner + session resume
- [14_claude-session-state.md](14_claude-session-state.md) — state-dir layout (`runtime/<name>/`, session_id marker, projects transcript)
- [07_a2a-protocol.md](07_a2a-protocol.md) — how a twin coordinates results back to its parent
- docs/adr/0019-twin-spawning-context-inheritance-and-identity-split.md — the design record

---
description: |
  [TOPIC] Fork spawning — fork context from a running agent
  [DETAILS] `sac agents fork <parent>` creates a new isolated agent from the parent's selected harness/spec. Claude context inheritance is supported; Hermes forks fail closed until a Hermes-native state.db fork exists. Remote agent-authenticated forks also fail closed while listen has only a host-wide bearer. Use from the bare-host admin path.
tags: [scitex-agent-container-twin-spawning, fork, fork-session, claude-session, sac, identity-split, ephemeral, persistent]
---

# Fork spawning

A **fork** is a new isolated agent derived from a running **parent**. For the
Claude harness it inherits the conversation at birth and then diverges. The
parent is never touched.

> **Current safety boundary:** Hermes stores context in
> `~/.hermes/state.db`; copying Claude JSONL/session IDs is not valid for it.
> Hermes therefore fails closed. In-container fork requests also fail closed
> because the current host-wide listen bearer cannot bind a JSON `caller` to an
> agent identity cryptographically. Use the explicit bare-host admin path.

```bash
# ephemeral Claude fork
sac agents fork neurovista --task "audit the failing figures" --ttl 30m

# persistent Claude companion
sac agents fork neurovista --name neurovista-writer --persist \
    --task "draft the results section"
```

The old `sac agents twin` spelling remains a hidden compatibility alias.

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
| `env.SAC_FORK_PARENT` | the parent | the owner-convention value (below) |
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

> **HARD RULE — the twin passes `assignee=<parent>` (== `$SAC_FORK_PARENT`)
> on EVERY `add_task` / `reassign`.** Never leave a card owned by the twin.

`SAC_FORK_PARENT` is injected into the twin's container precisely so this
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

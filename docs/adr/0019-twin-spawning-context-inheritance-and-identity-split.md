# 0019 — Fork spawning: context inheritance + identity split

- Status: Accepted (operator-designed + approved 2026-07-10, Telegram)
- Deciders: ywatanabe (operator), sac-fcs1-impl
- Builds on ADR-0010 (agent-spawn family tree + ACL) — twins reuse the
  server-mediated spawn substrate, they do not add a new spawn mechanism.

> **Terminology and safety amendment:** the user-facing operation is **fork**
> (``sac agents fork``). ``twin`` remains only as an internal compatibility
> name and in the existing ``SAC_TWIN_PARENT`` wire key. Hermes context is
> branched through its authenticated native gateway, never by copying
> ``state.db``. A fork is accepted only from a bare-host client presenting the
> separate owner credential; the shared listener bearer injected into agent
> containers confers no fork authority. Agent-authenticated remote forks remain
> fail closed until caller identity is cryptographically bound.

## Context

The operator asked for **twin spawning**: "twin を生み出す。~/proj/XXX から
XXX エージェントが動いているとして、XXX からコンテキストを持った
XXX-twin を spawn する。" A twin is a NEW agent that INHERITS the parent's
conversation context at birth, then diverges. **The parent never stops**
("24時間動かすこと、これに尽きる") — a twin is how an agent splits off
context-carrying work without pausing its own main loop.

Two constraints shaped the design:

1. **Lifetime and role are independent.** A twin can be an ephemeral triage
   worker (short-lived, auto-pruned) OR a long-lived companion sitting beside
   its parent ("neurovista 論文書きのエージェントはいつでも neurovista agent
   のそばで待機していて欲しい"). Same primitive, different lifetime settings —
   ephemerality must NOT be hardcoded.
2. **Attribution vs ownership.** Writes must be attributed to the TWIN's name
   ("分身の名前で書いて欲しい"), BUT scitex-todo card OWNERSHIP must stay with
   the PARENT. If a twin owns cards and then exits, those cards land in an
   inbox nobody drains — the ownership-drift incident that orphaned 75 cards.

An investigation of `scitex_todo._store` established the hard boundary:
`add_task` **fails loud** without an explicit `assignee`, and
`SCITEX_TODO_AGENT_ID` feeds ONLY the author path (`created_by` / comment
author / actor). Card owner (`agent` / `assignee` / `scope`) has **no env
default of any kind**. So author=twin is env-enforceable, but owner=parent is
**not** achievable from env — it can only be set by passing an explicit
`assignee` on every write.

## Decision

### Fork = server-derived spec + host-side session branch

`sac agents fork <parent>` (with the legacy hidden `twin` alias) sends only
fork parameters: parent, child name, task, role and lifetime. It never sends an
execution spec. Under a per-child lock, `sac listen` reads the authoritative
parent once and derives the complete child document. Image, selected harness
configuration, engine/model/provider references, session policy, `to_home`,
startup policy and Cards lineage therefore come from the host authority rather
than attacker-controlled JSON. A body claim such as `authority=admin` has no
effect.

The parent repo remains read-only. The fork receives one writable bind: its
fresh detached worktree at the parent's container workdir. Other writable binds
(including broad `/scratch`, host-home and parent-repo binds) are dropped;
explicit read-only binds may be retained. Its canonical overlay must be absent,
non-symlinked and distinct from the parent's real path/inode.

Context inheritance is a **host-side** pre-start step,
`_lifecycle._twin.seed_twin_from_parent`, called from `agent_start`
immediately after `seed_pinned_session_id` (its file-level companion). At
twin start it (1) resolves the parent's CURRENT session uuid from
`<parent-state>/session_id`, (2) copies the parent's transcript
(`runtime/<parent>/home/.claude/projects/<enc>/<uuid>.jsonl`) into the twin's
container-home projects store — mirroring the on-disk project subdir so no
cwd-encoding is recomputed — and (3) seeds the twin's session marker to that
uuid so its `session: continue` resumes the copied transcript. This is
**first-boot only**: a persistent twin's later restarts `continue` its OWN
diverged session (a pinned `resume` would re-fork from the parent each
restart, discarding the twin's history), and a persistent twin keeps
starting even after its parent stops. Doing this host-side means all
runtime paths resolve on the bare host regardless of whether `twin` ran on
the host or was brokered from inside a container, and the twin inherits the
FRESHEST transcript rather than one captured at command time. Fail-loud: a
parent with no live session / no transcript aborts the twin start.

For Hermes, the listener selects only the exact engine-scoped parent title or
session key, invokes native `session.branch`, and records a 0600 seed bound to
parent name, engine, stored session id, child title/cwd and a visible-history
digest. Descriptor-safe bounded reads/writes and atomic replace protect the
handoff; retries must match every binding.

Spec, snapshot, seed, runtime, overlay and worktree ownership are recorded in a
single handoff ledger. Synchronous failures and failures after a `202 Accepted`
remove only artifacts created by that request, verify each removal and report
cleanup failures. Reused winner artifacts are never deleted. A `202` always
means accepted with an unknown outcome and names the status route to poll.

### Identity split (safety-critical)

| Axis | Value | Mechanism |
|---|---|---|
| sac lifecycle self-name (`SAC_NAME`) | twin | `listen_env_flags` injects it from the twin's own spec name |
| scitex-todo AUTHOR (`SCITEX_TODO_AGENT_ID`) | twin | set in the twin's `spec.env` (overrides the inherited parent value) |
| scitex-todo card OWNER | parent | **convention only** — see below |
| `SAC_TWIN_PARENT` | parent | injected so the owner value is deterministic + is the twin trigger |

Because owner=parent is **not** enforceable from env, the twin's **boot-kick
prompt** and the `33_twin-spawning` skill state it as a HARD RULE: the twin
passes `assignee=$SAC_TWIN_PARENT` on every `add_task` / `reassign`. We inject
`SAC_TWIN_PARENT` so the value is always available and so a single env var
also serves as the twin-detection trigger for `seed_twin_from_parent`.

### Lifetime + safe defaults

- Ephemeral (default): `restart.policy: never`; optional `--ttl` schedules a
  best-effort detached host-side `sac agents stop` (a timer, not a durable
  scheduler). Persistent (`--persist`): `restart.policy: always`. `--persist`
  and `--ttl` are mutually exclusive.
- `a2a.port` reset to `auto` (never reuse the parent's, which may be pinned).
- The `server:claude-code-telegrammer` channel is dropped from the twin so two
  agents don't fight one bot's getUpdates slot (409); `server:sac` is kept for
  bus reachability.

## Consequences

- Bare-host operators can create context-carrying forks without pausing the
  parent. In-container agent/MCP requests fail closed until per-agent transport
  identity is cryptographically authenticated.
- The identity split is the safety-critical surface: author=twin is guaranteed
  by env; owner=parent is a documented convention (env cannot enforce it), so
  it is stated in the boot-kick AND the skill, backed by `SAC_TWIN_PARENT`.
  A twin that forgets `assignee` gets a fail-loud `add_task` error rather than
  a silent orphan — but a twin that defaults `assignee` to its own identity
  would orphan cards, which is why the rule is stated redundantly.
- `--ttl` is a soft cap (detached timer); it does not survive a host reboot.
  Full cleanup of a finished ephemeral twin is `sac agents delete <twin>`.
- Twin logic is isolated in `_lifecycle/_twin.py` (derivation + host seed) and
  `cli_pkg/lifecycle/_twin.py` (CLI); `_start.py` gains one guarded call.
- Live canary mutation remains blocked pending PR #1501; only isolated tests and
  dry validation are permitted meanwhile.

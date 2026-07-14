# 0019 — Twin spawning: context inheritance + identity split

- Status: Accepted (operator-designed + approved 2026-07-10, Telegram)
- Deciders: ywatanabe (operator), sac-fcs1-impl
- Builds on ADR-0010 (agent-spawn family tree + ACL) — twins reuse the
  server-mediated spawn substrate, they do not add a new spawn mechanism.

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

### Twin = derived spec + host-side session-fork

`sac agents twin <parent>` (and the `agent_twin` MCP tool) derive the twin's
inline spec from the parent's on-disk spec — inheriting repo / workdir /
image / binds / model / `to_home` verbatim — and POST it to the host
`sac listen` via the **existing** ADR-0010 spawn substrate
(`_spawn_client.request_spawn`). No new spawn mechanism; the same
`check_spawn` ACL + lineage recording apply.

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

- An agent can spawn its own context-carrying second self without pausing —
  enabling parallel twins on sub-tasks and heavy work off the main loop.
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

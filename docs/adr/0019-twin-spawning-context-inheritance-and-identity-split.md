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
- Twin logic is isolated in `_lifecycle/_twin.py` (derivation + host seed),
  `_lifecycle/_twin_identity.py` (naming + boot gate — see the amendment) and
  `cli_pkg/lifecycle/_twin.py` (CLI); `_start.py` gains one guarded call.

## Amendment — 2026-07-17 (operator-directed)

Four changes; the decisions above otherwise stand. Operator, same day:
「claude code にオフィシャルで fork オプションがありますね」 and
「agent id は fork と descriptive name で決定的に付けると良さそう」.

### 1. Context inheritance uses the SUPPORTED fork mechanism

`seed_twin_from_parent` no longer seeds the twin's LIVE session to the
parent's uuid. On first boot it now points the launch at
`claude --resume <parent-uuid> --fork-session --session-id <derived>` (SDK:
the same three via `ClaudeAgentOptions`), so the twin inherits the
conversation but writes to a session of its **own** from turn one.
`--fork-session` ("When resuming, create a new session ID instead of reusing
the original") is the documented mechanism the original design hand-rolled.
No live collision from the old uuid reuse was ever demonstrated; the
justification is using the supported mechanism, which is sufficient on its own.

**The transcript copy stays, and is not what the fork replaces.** Parent and
twin have separate container homes and `--resume <uuid>` resolves the
transcript from the LOCAL `~/.claude/projects`, so the copy is the cross-home
transport; the fork only handles the id. The `session_id` marker is still
seeded to the parent's uuid — it is the in-container SDK runner's only
"resume FROM" channel — and the fork advances it to the twin's own id after
turn one, so that value is transient, never the twin's identity.

New `spec.claude.fork_session` / `spec.claude.session_id` (validated:
session_id must be a UUID; fork_session requires continue/resume) carry this
to BOTH runtimes — `runtime: tui` (88 of 97 fleet specs) and
`runtime: claude-agent-sdk` (9). A twin inherits its parent's runtime, so both
had to move together.

### 2. `--tag` → deterministic ids

`sac agents twin <parent> --tag <slug>` ⇒ `<parent>-forked-<tag>`, on all four
surfaces (Python API, CLI, the `agent_twin` MCP tool, the `33_twin-spawning`
skill). The session uuid is `uuid5(TWIN_SESSION_NAMESPACE, <twin-id>)` — a
valid UUID for `--session-id`, derivable from the name alone. The legacy
`<parent>-twin` default (bumped `-2`/`-3`) is retained for back-compat but is
no longer the advice: it mints a NEW agent per re-run, which is the sprawl
`--tag` exists to stop.

### 3. The identity split gains a BOOT GATE

`assert_twin_identity` refuses to boot a twin whose `SCITEX_TODO_AGENT_ID` is
missing, is the parent's, or is not its own name. WHY a gate and not a prompt:
the inherited transcript says "I am \<parent\>" hundreds of turns deep and
outweighs any prompt line — the 2026-07-03 two-agents-one-identity bug. Env is
the only channel that outranks the transcript.

**The scope line in "Identity split" above still holds and is now enforced by
construction:** the gate covers AGENT IDENTITY (env-carried). Card OWNERSHIP
is NOT gated — `add_task` has no env default for the owner, so there is
nothing to assert with, and the prompt-level `assignee=$SAC_TWIN_PARENT` rule
remains load-bearing. Gate what an env can carry; prompt what it cannot.

### 4. Two defects the original shipped (found while implementing the above)

Both in `derive_twin_spec`, both demonstrated against the real on-disk
`scitex-tex` spec — **`sac agents twin` could not have worked end-to-end**:

- **Identity env was written to the top-level `spec.env`**, which v3
  validation REJECTS ("no longer accepted at the top level; move it to
  `spec.apptainer.env`"). Every twin derived from a v3-valid parent produced
  an UNLOADABLE spec — so the twin never started, never received
  `SAC_TWIN_PARENT`, and `seed_twin_from_parent` never triggered. Now written
  to `spec.apptainer.env`, which reaches both `config.env` (via the loader's
  `merged_env`) and the container (`--env KEY=VAL`).
- **The parent's identity was re-injected via inherited `raw_args`.** Real
  specs pin identity as `raw_args: [--env, SCITEX_TODO_AGENT_ID=<parent>]`,
  and `build_run_argv` appends `raw_args` verbatim AFTER every curated
  `--env`. Deep-copied into a twin, that re-asserts the parent's identity
  downstream of the twin's own — and through a channel the boot gate cannot
  see (the gate reads the host-side `config.env`, which correctly says
  "twin", while the container's env would say "parent"). `derive_twin_spec`
  now scrubs identity `--env` pairs from inherited `raw_args`, leaving every
  other raw_arg verbatim. We delete rather than reason about which duplicate
  `--env` the engine honours: an ambiguity removed beats an ambiguity bet on.

These went unnoticed because the test fixture used a top-level `spec.env` —
a shape no real spec has and the validator rejects — and never ran the
validator, so the suite could not have disagreed with the implementation.
`test_derived_twin_spec_passes_v3_validation` is the test that can.

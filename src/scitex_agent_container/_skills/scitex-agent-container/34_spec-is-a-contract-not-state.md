---
description: |
  [TOPIC] A spec is the CONTRACT for an agent that has not started yet — never the state of a running one.
  [DETAILS] The operator's universal ruling (2026-08-11): `spec.yaml` is a design document about the FUTURE; what is actually true of a RUNNING agent lives in the database (per-host PostgreSQL `:55432`); the spec as it stood AT LAUNCH is burned into the agent as a file so it can answer "how was I born". Covers the tense test, the sentinel trap (`a2a.port: auto` — 0 of 104 fleet specs carry a concrete port), where to read each fact instead, the `session_id` case, and the `STX-SAC004` lint rule that catches the mechanical half.
tags: [scitex-agent-container-spec-is-a-contract-not-state, spec, state, postgres, a2a-port, session-id, adr-0022]
---

# A spec is a contract, not a state

**Operator ruling, 2026-08-11, called universal**
(「これはどんな時も従うべき話だと思います」):

> 「スペックというのは今動いてるエージェントの状態を表すのではなくて、未来に
> 動くエージェントの規約」
> 「スペックは設計書、実際に動いてるエージェントの状況はデータベース」

Three homes, decided by **tense**:

| Question | Home |
|---|---|
| What *shall* an agent be? | `spec.yaml`, under git — the design document |
| What *is* true of a running agent? | the database (per-host PostgreSQL `:55432`) |
| What did the contract say *at launch*? | a file burned into the agent |

The third is not a copy of the first. The git-side spec is **live**: an
operator edit does not retroactively change how a running agent started,
so a spec read at 23:00 does not describe an agent launched at 19:00.

## The tense test — the one thing to remember

> **If answering the question requires knowing that a start has already
> happened, the spec cannot answer it. Ask the database.**

Ports bound, session resumed, host actually running on, pid, uptime,
group membership *as enforced* — all of these exist only because a start
happened. None of them is in the spec, no matter how much the spec looks
like it is talking about them.

## The trap: a spec field may be a PROMISE, not a value

A field can legitimately declare *"resolve this at start"*. Read it as a
value and you get a **sentinel**, which then silently fails every numeric
or identity test you apply to it — no exception, no log line, just a
branch that never fires.

`spec.a2a.port` is the live example. Measured on scitex-compute-04,
2026-08-11, over every `agents/*/spec.yaml` (107 files, 3 of them
`_template_*` scaffolds → **104 real agent specs**):

| declared | agents |
|---|---|
| `port: auto` | 93 |
| `port: null` (sidecar deliberately off) | 11 |
| **a concrete int** | **0** |

The near-miss that produced this skill: the tui turn-bridge supervisor
(PR #973) has to know which port an agent's bridge should serve. Had it
read `config.a2a.port`, it would have received the string `"auto"`, found
no number, concluded there was nothing to check, and supervised
**nothing — on every agent in the fleet**, while reporting healthy.

It reads the port allocator's **claim** instead — `a2a_ports`, which is
state, written when a start actually happened:

```python
# WRONG — the spec declares a promise; you get the string "auto"
port = config.a2a.port

# RIGHT — the claim is a fact, and exists only because a start happened
from scitex_agent_container._state import port_allocator
port = port_allocator.get_port(name)          # int, or None if never started
```

`sac agents list scitex-agent-container` answers `a2a_port: 19016` for
exactly this reason: it reports the claim, not the declaration.

## Where to read each fact instead

| You want | Do NOT read | Read |
|---|---|---|
| the port an agent actually serves | `spec.a2a.port` | `port_allocator.get_port(name)` (`a2a_ports`) |
| which conversation resumed | `spec.claude.session` | the instance row / session state — **not yet pinned**, see below |
| whether an agent is running | any spec field | `instances` / heartbeats |
| the host it runs on now | `spec.host` | `agent_residency` (relocation moves residency, not the spec) |
| the groups an ACL enforces | the persisted policy cache | `config/_group_authority.py` — spec IS authority here (ADR-0022 §6) |

The last row is the deliberate exception and shows the rule is about
tense, not about file format: group membership is **configuration** —
authored by a human, changed by nobody at runtime — so the spec is the
right answer and the cached row was the bug.

## `session_id` — the same split, still open

What a spec declares (`claude.session: continue` / `fresh`) is a
*promise about how to resume*. **Which conversation actually resumed** is
state. Today `sac agents list <name>` can report `session_id: null` for
an agent with a live session — the promise is recorded, the fact is not.
Tracked by card `sac-pin-session-id-at-start-removes-f34-20260812`.

## What is mechanically enforced (and what is not)

**`STX-SAC004`** (severity: *warning*, this package's linter plugin)
fires when a sentinel-bearing spec field — `<x>.a2a.port` today — is
`return`ed from a function or passed as a call argument **without the
enclosing function narrowing the sentinel** (`is_auto`, `is_disabled`,
`== "auto"`, `isinstance(..., int)`, or a resolver call). That is exactly
the shape the near-miss would have had. Suppress a deliberate case with
`# stx-allow: STX-SAC004`.

It is deliberately narrow. **Nothing checks the general rule** — "this
code is reading a spec to learn a running agent's state" is not
mechanically decidable, and a check that fired on every `config.` access
would be noise and would be switched off within a week. Comparisons
(`assert cfg.a2a.port == 7901`) are *not* flagged: asserting what the
contract says is the correct way to read a contract.

New spec fields that resolve at runtime must be added to
`_SENTINEL_FIELDS` in `_linter_plugin.py`, or the rule cannot see them.

## When you write a spec

Every scaffolded spec now opens with two lines saying so:

```yaml
# THIS IS A DESIGN DOCUMENT — the contract for an agent not yet started.
# The state of a RUNNING agent lives in the database, never in this file.
```

Emitted by `sac agents create` (minimal + full templates) and by the
contributor-spec renderer. Nothing enforces its presence — a YAML comment
is not schema — and that is accepted: its audience is a human with the
file open, not a validator.

Full reasoning, the sync design and the deferred work: **ADR-0022**
(`docs/adr/0022-state-in-postgres-configuration-in-git.md`), §3.

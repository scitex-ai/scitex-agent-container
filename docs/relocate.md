# Relocating an agent between hosts

**Scope of this document.** Everything you must do BEFORE a relocation, the
procedure ITSELF, and what must be true AFTER. Written so that it is sufficient
on its own: if the relocated agent arrives with no memory of the conversation
that moved it, handing it this file is enough to continue.

That fallback is a **workaround, not the design**. An agent that needs a
document to remember what it was doing has not been relocated, it has been
replaced. Carrying the session is relocate's own job — see
[§5 Session continuity](#5-session-continuity) for what is built and what is not.

---

## 0. What relocate is, and what it is not

| | axis that changes | identity | count |
|---|---|---|---|
| **relocate** | WHERE it runs | unchanged | 1 → 1 |
| **fork / twin** | WHAT it does | new | 1 → 2 |

The agent relocates. The host does not move. Relocate is *not* a kind of twin,
and neither verb should be described in terms of the other; they share exactly
one implementation detail (seeding a session from an existing transcript) and
nothing else.

**Why the command exists at all.** `cardinality: singleton` is declared in the
spec and enforced by nothing. Identity lives in the spec and `host:` is just a
field, so copying a spec to another machine and starting it produces TWO live
agents under one identity, with no error. There is no "relocate" without this
command — there is only "copy and start", and copy-and-start makes two. That is
the root of the 2026-08-07 card-store split-brain.

---

## 1. Before you begin — the failure this is all guarding against

On 2026-08-07 this move was done by hand. Rewriting `host:` alone produced an
agent that **started, reported healthy, and did nothing**. That is the worst
failure shape available, because it looks exactly like success and nobody goes
looking.

Every check in §2 exists because of a specific thing that went wrong that day.
None of them is hypothetical.

---

## 2. BEFORE — preflight

Run the dry run first. It touches nothing and returns **every** problem, not the
first one, so you do not have to run it N times to find N problems.

```bash
sac agents relocate <name> --to <host> --dry-run
```

### The nine checks

| check | what it catches | the 2026-08-07 instance |
|---|---|---|
| `target_reachable` | host does not answer | — nothing else in the report means anything until this passes |
| `image_present` | SIF absent on the target | a missing image fails at boot, **after** the lease has moved |
| `binds_exist_on_target` | bind source paths absent there | the spec bound `/mnt/c`, a Windows drive that does not exist on the nas |
| `card_store_reachable` | agent cannot reach its board | `SCITEX_CARDS_DB` is port 5432 here, 5442 there |
| `credentials_valid` | **validity, not presence** | the nas had a file expired 2026-05-23 with an empty `refreshToken`, and sac loaded it *in preference to* the good one — every turn 401'd while `sac agents health` still said healthy |
| `runtime_supported` | target's sac rejects the runtime | nas's sac 0.21.9 rejects `tui` |
| `spec_schema_accepted` | target's validator rejects a key | a top-level `provider:` key, same older validator |
| `ports_free` | port already bound there | reassign in the spec before moving |
| `hub_reachable_from_target` | hub unreachable **from there** | the nas's services bind `127.0.0.1`, so reaching them from HERE proves nothing about THERE |

**Read the credentials row twice.** A presence check passes on an expired file.
The failure it prevents is silent.

### Three answers, not two

Every check is **three-valued**: pass / fail / **could not determine**. Unknown
never counts as a pass, and it is reported separately from failure, because
"this is wrong" and "I could not tell" call for different actions from you.

A probe that fails produces `None` (not observed), never a falsy value. This
matters more than it looks:

```
probe raises -> False   a missing image reads as present, a busy port as free.
                        The relocation proceeds on fiction.
probe raises -> None    preflight reports UNKNOWN, refuses, names the check.
                        Nothing proceeds on fiction.
```

An unobserved fact folded into "pass" is precisely how the 08-07 move reported
healthy.

### Do not skip the unknowns

If the dry run says UNKNOWN, the answer is to run the missing probe — not to
proceed because nothing said no.

---

## 3. THE PROCEDURE — an ordered handover of one write lease

### Why a lease and not a handshake

The obvious design is a mutual handshake: the source confirms the target is up,
the target confirms the source is gone. **Do not do this.** Under a partition,
mutual confirmation either deadlocks (both wait) or double-commits (both
proceed) — which is the same failure it was meant to prevent.

Instead there is exactly ONE write-lease token. Whoever holds it may write;
nobody else can. **Two holders is unrepresentable.** Relocation is the ordered
handoff of that token, driven by the relocate command as coordinator, never by
agreement between peers.

### TTL decides reclaim; the fence decides who may write

The lease carries a TTL, and a TTL assumes clocks agree. A stopped container, a
suspended laptop, or an NTP step hands a source a lease it *honestly believes*
is valid.

So every lease also carries an integer that only increases, bumped on each
handoff and each reclaim. A writer presents its fence, and anything below the
current value is refused. The stale holder is locked out by arithmetic rather
than by trusting its clock.

### The six phases

```
PREFLIGHT  ->  TARGET_STANDBY  ->  SOURCE_DRAIN  ->  HANDOVER  ->  SOURCE_STOP  ->  DONE
                                                     ^^^^^^^^
                                          the single atomic point
```

| phase | what happens |
|---|---|
| `PREFLIGHT` | validate the target; touch nothing |
| `TARGET_STANDBY` | start the target **without** the lease — it runs read-only; verify health |
| `SOURCE_DRAIN` | source finishes in-flight work, stops accepting new |
| `HANDOVER` | the lease moves source → target |
| `SOURCE_STOP` | stop the source, and **verify** it stopped |
| `DONE` | append the residency record |

Every step is idempotent and journalled, so a crash **resumes** rather than
restarts. Advancing to the phase you are already in succeeds and appends
nothing: a coordinator that finished the work and died before journalling
re-runs harmlessly. Skipping is refused, and the refusal names the next legal
step. Going backward is refused.

### If it dies mid-way

| when | state | what to do |
|---|---|---|
| before `HANDOVER` | lease still with the source; target is standby and harmless | re-run: it resumes, or abort cleanly |
| after `HANDOVER`, before `SOURCE_STOP` | target holds the lease; the source is alive but **cannot write** | re-run: it completes the stop. Fails safe. |
| target never healthy | abort; the lease never moves; the source is untouched | fix the target, re-run |
| network partition | the holder that cannot renew **stops writing**; nobody else may claim until the TTL expires | wait, or reclaim deliberately |

**`abort` is refused at or past `HANDOVER`.** Undoing there would mean taking
write authority back from a live holder — that is itself a relocation and should
be run as one, not smuggled in under a name that promises the opposite. The
refusal names the host that now owns the lease.

### What the lease does NOT govern

Only writes to the **shared** hub store. Each host's LOCAL store keeps accepting
local work while partitioned — every host must stay usable with no network.
Reconciliation settles it afterwards. A network outage must never become a fleet
outage.

---

## 4. AFTER — what must be true

1. **The source is stopped, verified.** Not "stop was requested" — confirm it.
2. **The target holds the lease** and its fence is above the source's.
3. **The residency record is appended**: `{host, from_ts, to_ts}`.
   - Boundaries are half-open `[from_ts, to_ts)`, so the handover instant
     belongs to the **target**. That is the one moment two answers would
     otherwise be possible.
   - `to_ts is None` means *still living there* — an open interval, not a
     missing value.
   - `host_at(history, ts)` returning `None` means the history genuinely does
     not know (before the first record, or a gap while stopped). **Never read
     that as "the current host"** — that guess is what makes a split-brain look
     explained when it is not.
4. **The agent answers.** Send it something and confirm the reply. Confirm
   arrival, not dispatch.
5. **Its board is reachable from the new host** — re-check, since the card store
   port differs per host.

### Why residency is worth keeping

The audit trail is the smaller half. The larger half is **attribution**: the
cards `host` column is NULL on 3247 of 3424 rows, so when two instances of one
identity disagree, nothing can say which host wrote which row. With residency
that becomes a lookup instead of an investigation.

---

## 5. Session continuity

### The measurement

The nas instance booted with **no memory** of the originating conversation. The
transcript lives inside the container overlay, so carrying it means copying it
explicitly. Nothing about a host change carries it for free.

### The decision

**Relocate implies continuity.** `--no-carry-session` opts out.

Reasoning, from the definition rather than preference: relocate changes WHERE an
agent runs; identity is unchanged and the count stays 1. It is the SAME agent
continuing. Making continuity opt-IN would mean the default produces the outcome
we filed as the defect. The opt-out exists for the deliberate case — a wedged
session you want to leave behind — and should be loud about what it discards.

### The mechanism

1. Resolve the source's current session uuid from `<state>/session_id`.
2. Write that uuid as the target's `session_id` marker, so `session: continue`
   resumes it.
3. Copy the source's `<uuid>.jsonl` into the target's projects store,
   **mirroring the source's project subdir name** so claude's cwd encoding
   matches without recomputing it.
4. **First boot only.** If the target already has its own marker it has booted
   and diverged, and re-seeding would discard that history.

### Verify readability ON THE TARGET

Do not trust the copy's exit code. A path inside a container can resolve
somewhere entirely unexpected — a moved-aside tree, a git worktree — while every
operation on it succeeds. On 2026-08-08 `~/.scitex` inside a container turned
out to be a symlink into a dotfiles worktree, created mid-session, with every
write "succeeding" into a tree a `git clean -xdf` can erase. Read the transcript
back from the target before declaring the carry done.

### Honest status

The **decision** logic is built and merged (`_session_carry.plan_session_carry`).
The cross-host **transport** that executes a `carry=True` plan is not. Until it
lands, a relocation carries no memory, and this document is the stopgap the
operator asked for.

---

## 6. Current implementation status

| piece | state | what it guarantees |
|---|---|---|
| write lease + fence | **merged** (#888) | two writers unrepresentable |
| phase journal | **merged** (#889) | a crash resumes; a handover is never undone |
| preflight checks | **merged** (#890) | the target is checked before anything is touched |
| session-carry decision | **merged** (#891) | whether the transcript follows the agent |
| residency history | **merged** (#892) | where it lived, and who wrote a row |
| probe adapter | **merged** (#894) | a failed probe stays UNKNOWN, never a false negative |
| cross-host transcript transport | **not built** | — |
| the `relocate` CLI verb | **in progress** | dry-run first |

Six pure pieces, 138+ tests, none of which touches a host — that was deliberate,
so each is testable without a second machine. The two remaining items are the
ones that act.

**Until the CLI lands, a relocation is a manual procedure**: follow §2 by hand,
then §3, then §4. The checks in §2 are the ones a hand-move actually needs; they
are written down here precisely because they were learned the hard way.

---

## 7. Known gaps

- **Relocate does not yet carry memory.** §5 — decision merged, transport not.
  This document exists because of that gap and should shrink when it closes.
- **"Defined" and "running" are not distinguished.** The fleet listing collapses
  a spec that exists with a process that is alive, so from inside a container
  `sac agents list` reports the whole fleet — including the agent running the
  command — as `defined`. Before relocating, confirm what is actually running by
  another route.
  Card: `sac-agents-list-blind-inside-container-reports-whole-fleet-defined-20260808`.
- **Four sources disagree about which agents exist** (18 / 32 / 159 / 111 as
  measured 2026-08-08). Which is canonical is an open decision.
- **Host naming is not yet canonical.** Relocate must write the canonical
  `scitex-<category>-0N` name.
  Card: `sac-standardize-host-naming-and-fail-loud-during-rename-20260807`.

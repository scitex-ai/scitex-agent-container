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

### The seventeen checks

| check | what it catches | the instance |
|---|---|---|
| `target_reachable` | host does not answer | — nothing else in the report means anything until this passes |
| `image_present` | SIF absent on the target | a missing image fails at boot, **after** the lease has moved |
| `binds_exist_on_target` | bind source paths absent there | the spec bound `/mnt/c`, a Windows drive that does not exist on the nas |
| `workdir_exists_on_target` | `spec.workdir` absent there | it becomes apptainer's `--pwd`; there is no `spec.repo`, the workdir **is** the checkout |
| `card_store_dsn_correct` | the DSN itself is wrong | `5432` is wrong on every host with no exceptions, and a port-less DSN means the same thing to libpq |
| `card_store_reachable` | agent cannot reach its board | the fleet's endpoint is `55432`; an agent that cannot reach its board runs and records nothing |
| `credentials_valid` | **validity, not presence** | the nas had a file expired 2026-05-23 with an empty `refreshToken`, and sac loaded it *in preference to* the good one — every turn 401'd while `sac agents health` still said healthy |
| `runtime_supported` | target's sac rejects the runtime | nas's sac 0.21.9 rejects `tui` |
| `spec_schema_accepted` | target's validator rejects a key | a top-level `provider:` key, same older validator |
| `ports_free` | port already bound there | reassign in the spec before moving |
| `groups_resolvable_on_target` | the target's sac cannot read this spec's group labels | three hosts resolve `[]` for every agent regardless of `spec.yaml`; nine relocation probes were refused 403 by exactly that on 2026-08-11 |
| `hub_reachable_from_target` | hub unreachable **from there** | the nas's services bind `127.0.0.1`, so reaching them from HERE proves nothing about THERE |
| `sac_present_on_target` | remote `sac` calls will fail | on scitex-compute-04 sac lives at `/home/ywatanabe/.env-sac/bin/sac` and is **not** on the non-interactive ssh PATH, so `ssh host sac …` answers "No such file or directory" while sac is installed and working (2026-08-11) |
| `source_work_committed` | uncommitted/unpushed work on the host being **left** | a relocation carries the spec and the transcript and nothing else; a half-finished branch stays on a machine nobody is watching |
| `session_resolvable` | which conversation would travel cannot be named | asked HERE because the phase that needs the answer runs *after* the agent has been stopped; ten agents passed every other check on 2026-08-12 and could not complete |
| `target_start_accepts` | the target's own `sac agents start` would refuse the spec source | asked of the target's sac rather than re-implemented here; it cost the canary its first leg (2026-08-11) |
| `lease_holdable` | the stored write lease is held by another host | HANDOVER refuses this, and HANDOVER runs after the source has been stopped, transported and booted (2026-08-11, exit 5 with nothing running anywhere) |

**Read the credentials row twice.** A presence check passes on an expired file.
The failure it prevents is silent.

**`sac_present_on_target` is one check answering two questions.** "Not installed"
and "installed but invisible to ssh" produce the *identical* error and need
*opposite* fixes — install it, versus put it on the PATH (or set the peer's
`env_preamble`). The check reports them separately, resolving sac by login shell
and by absolute path, so nobody is sent to install a second copy of something
that already works. When it can see that sac is off the PATH but cannot
establish whether it exists at all, the answer is UNKNOWN, not a guess.

**A missing bind is never one problem.** Fifteen fleet specs bind paths that do
not exist on scitex-compute-04, and every one of those specs is *correct for the
machine it currently pins*. Nine are Spartan agents binding shared cluster storage
a workstation cannot provide at all; six are laptop agents binding a dataset and a
local checkout that exist on exactly one machine because that machine made them.
Printed as "path not found" they look identical, so the check classifies each path
and splits the hint by what you have to do about it:

- **provision on the target** — host infrastructure (`/data`, `/mnt`, `/gpfs`, …).
  If the filesystem does not exist there at all, this spec belongs on a host that
  has it; re-pointing the bind produces a *different agent*, not a relocated one.
- **must travel with the agent** — anything under or beside the agent's own
  workdir, and any `dataset` directory. A relocation carries the spec and the
  transcript and nothing else, so this data does not follow by itself.
- **provision there, never copy** — credential paths (`.ssh`, `.config/gh`,
  `accounts/*/.credentials.json`, `.pgpass`). Key material must not travel
  between hosts, so the hint says so instead of "move it with the agent".

Where the path's shape cannot settle it, the answer is `unclassified` and the
hint states both possibilities. A confident wrong category sends you to provision
a directory that should have travelled.

**`groups_resolvable_on_target` distinguishes "refused" from "no".** A target that
resolves a non-empty group set and omits one this spec declares has *answered*,
and that is a FAIL. A target that resolves nothing has not — that is the shape of
a daemon too old to read spec labels, and reporting it as a failure sends you to
edit a spec that is already correct. Likewise a 403 from the **local** listen
daemon brokering the probe is about this container's authorization, not about the
target; the report says so in those words and leaves every target fact UNKNOWN.

**`source_work_committed` is the only check about the source.** Its facts are
gathered locally rather than over ssh, and an unscanned repo is UNKNOWN — never
"clean". A failed `git status --porcelain` prints nothing and so does a clean
tree; counting lines conflates them, and the conflation clears exactly the repo
the check exists to protect. A branch with no upstream is likewise unmeasured
rather than zero-unpushed.

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

Whether an unknown *refuses* is relocation's policy, not a property of the
three-valued answer — a dashboard may paint the same unknown amber and carry on,
and be right to. It lives as one named constant
(`UNKNOWN_BLOCKS_RELOCATION`) at the aggregation site rather than as an `if`
inside seventeen checks, so it can be read, and by a different consumer replaced,
in one place.

### Every problem in one pass, ordered by what to do about it

No check is skipped because an earlier one failed. All fifteen run, always, and
the refusal is one block rather than a sentence about the first thing that broke.

Two things make a complete list readable:

- **Root causes are stated once.** An unreachable target turns eleven checks
  UNKNOWN, and every one of them carries the *same* probe-error text — so
  identical reasons are recognised as one cause, printed once, with the blocked
  checks named under it. They still block; they are just not eleven tasks.
- **The rest are ordered by action**, not by check index: what the target must be
  provisioned with, what must travel with the agent, what is a spec correction,
  what still has to be measured. Each entry names *what* is wrong concretely,
  *where* it was checked, and *what would fix it*.

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

### The seven phases

```
PREFLIGHT -> TARGET_STANDBY -> HANDSHAKE -> SOURCE_DRAIN -> HANDOVER -> SOURCE_STOP -> DONE
                                                            ^^^^^^^^
                                                 the single atomic point
```

| phase | what happens |
|---|---|
| `PREFLIGHT` | validate the target and the source; touch nothing |
| `TARGET_STANDBY` | start the target **without** the lease — it runs read-only |
| `HANDSHAKE` | target → source round trip; the source must **observe** the reply |
| `SOURCE_DRAIN` | source finishes in-flight work, stops accepting new |
| `HANDOVER` | the lease moves source → target |
| `SOURCE_STOP` | stop the source, and **verify** it stopped |
| `DONE` | append the residency record — **which is the host write** |

### Where the host is written: the db, never the spec

Operator, 2026-08-11, after asking whether the spec is a file or a db:

> 設定ファイル、人が書くものはファイル、状態は db

`host` was in the spec for years and it was **never intent**. Where an agent
actually runs is an *observation*; a human typing `host: nas-03` is recording a
fact, and a fact hand-written into a git-tracked file that exists in one copy
per machine will eventually be wrong in at least one of them.

So **a relocation writes nothing to any spec file.** The residency record
appended at `DONE` *is* the host write. There is deliberately no spec-editing
phase — an earlier draft had one, with an undo, and removing it also removed the
only pre-handover step that changed anything durable. That is why `abort` has no
compensation to perform.

**Migration — seed once, then ignore.** Every spec on disk still carries `host:`
(106 of 106, measured 2026-08-11).

| the db… | what happens to the spec's `host:` |
|---|---|
| has an open residency | **ignored.** Not compared, not merged, not warned about on every read |
| knows nothing | read **once** to seed the db, and the seeding is recorded (`host_seeded_from_spec`) so the value's provenance survives |
| knows nothing, no spec host | **UNKNOWN.** Not "local", not the current hostname |

The dry run prints a one-line notice whenever a spec still carries the field,
saying plainly that it is ignored and which value is authoritative. A field that
is authoritative on Tuesday and ignored on Wednesday is worse than either, so
the seeding branch is a migration and not a fallback that keeps running.

### The handshake: A → B is not a handshake

Measured 2026-08-11: **a2a between two live agents delivered nothing**, and
nobody noticed until a human asked. Every one-way signal was green throughout —
both processes ran, both sidecars listened, both dispatch calls returned
accepted. A relocation gated on "the target started" would have handed the lease
straight into that, and `abort` is refused past the handover.

So the handshake requires four things, and each rules out a way that "green" was
wrong:

1. the target **accepted** the challenge — otherwise its agent was never asked
   anything;
2. a reply was **observed**, on the source side — arrival, not dispatch;
3. the reply carries **this challenge's nonce** — otherwise a reply left over
   from an earlier turn passes, and a relocation retried three times will
   eventually find one;
4. the reply proves **work** — an answer the loop had to compute, because an
   echo proves the transport and not the agent.

Anything not observed is UNKNOWN, which refuses. A timeout waiting for a reply
is "I did not see one in the time I waited", not "the target is broken", and the
two call for different actions.

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

The **decision** logic is built and merged (`_session_carry.plan_session_carry`),
and so is the **verification** contract (`_relocate_transcript.carry_transcript`):
it reads the transcript back from the target and compares digests, and an
unverifiable copy is `carried=None` rather than a soft yes. What is missing is
the pair of callables that actually move bytes between two hosts. Until those
land, a relocation carries no memory, and this document is the stopgap the
operator asked for.

### If the move goes wrong: the origin record

`DONE` also writes an **origin record** — where the source was, in enough detail
to go back by hand: the workdir, the state dir, the session uuid, the transcript
path (with whether it was verified on the target, or UNKNOWN), and every repo
with its uncommitted and unpushed counts. `recovery_lines()` renders it as
instructions rather than a field dump, because its reader is by definition
dealing with a relocation that already went wrong.

Two rules make it worth having. A record naming **no path at all** is refused at
construction — "it came from ywata-note-win" sends someone to a machine with no
idea where to look. And a repo that was never scanned is listed under **NOT
MEASURED**, separately from the clean ones, because a recovery aid that cannot
tell "clean" from "not looked at" is worse than none.

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
| transcript carry + read-back | **merged** | the copy is verified ON the target, by digest |
| agentic handshake | **in review** | the target proved it can do agent work |
| host-in-db + one-time seed | **in review** | the spec never holds an observation |
| origin record | **in review** | a bad move is recoverable by hand |
| the phase driver | **in review** | the order, the journal, and abort-vs-report |
| cross-host transcript **transport adapters** | **not built** | — |
| the executing CLI adapters | **not built** | — |

**What "in review" buys and what it does not.** The pure machinery is complete
and tested: given effects, the driver runs the phases in order, journals each,
refuses on any non-yes, and aborts only where an abort is legal. What is *not*
built is the set of adapters that perform those effects against two real hosts —
starting the standby, running the challenge, moving the lease, stopping the
source. Until those land, `--no-dry-run` has nothing to call.

**The lease is recorded, not yet enforced.** `check_write` exists and no writer
in sac calls it. So a handover moves an authoritative *record* of who may write;
it does not currently exclude a second writer. Two live instances remain
possible by copy-and-start, which is what the lease was designed to make
unrepresentable. That gap is in the enforcement, not the model.

Six pure pieces, 138+ tests, none of which touches a host — that was deliberate,
so each is testable without a second machine. The two remaining items are the
ones that act.

**Until the executing adapters land, a relocation is a manual procedure**:
follow §2 by hand, then §3, then §4. The checks in §2 are the ones a hand-move
actually needs; they are written down here precisely because they were learned
the hard way.

One of them is worth doing by hand before anything else, because it is cheap and
it invalidates the rest when it fails:

```bash
ssh <target> 'command -v sac'                 # the PATH remote sac calls get
ssh <target> 'bash -lc "command -v sac"'      # the login shell's answer
```

Two different answers mean sac is installed and unreachable the way sac calls
it — a PATH fix, not an install.

---

## 7. Known gaps

- **Relocate does not yet carry memory.** §5 — decision merged, transport not.
  This document exists because of that gap and should shrink when it closes.
- **"Defined" and "running" are not distinguished.** The fleet listing collapses
  a spec that exists with a process that is alive, so from inside a container
  `sac agents list` reports the whole fleet — including the agent running the
  command — as `defined`. That is a SPEC fact ("a file exists") sitting in a
  STATE column called `status`, and it is why the registry reported 0 agents
  running while 24 were live. The same listing prints `host` as the literal
  string `'local'` on every row — a placeholder where an observation belongs.
  Relocate does not build on either: it reads the host from the state db and
  returns `None` when nothing knows. Before relocating, confirm what is actually
  running by another route.
  Card: `sac-agents-list-blind-inside-container-reports-whole-fleet-defined-20260808`.
- **`host` still lives in the spec schema everywhere else.** Relocate no longer
  reads it, but `validate_placement` still *requires* `spec.host` (or `hosts`),
  and eleven runtime sites resolve placement from `config.hosts_spec.host` —
  cross-host dispatch, stop/restart routing, `attach`, cold-start reuse, the
  priority report, and remote liveness. Moving those to the db is a separate
  migration; until it lands, `host` is db-authoritative *for relocate* and
  spec-authoritative for dispatch. That split is deliberate and temporary, and
  it is the next thing to close.
- **There is no residency table.** The host is read from `instances.host`, which
  gives the current stay and no history — so "where does it live now" is
  answerable and "which host wrote this row in March" is not. Attribution, the
  larger half of why residency was built, needs the table.
- **Four sources disagree about which agents exist** (18 / 32 / 159 / 111 as
  measured 2026-08-08). Which is canonical is an open decision.
- **Host naming is not yet canonical.** Relocate must write the canonical
  `scitex-<category>-0N` name.
  Card: `sac-standardize-host-naming-and-fail-loud-during-rename-20260807`.

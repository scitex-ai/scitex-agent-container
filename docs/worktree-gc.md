# `sac worktree gc` — worktree-sprawl GC + cap alarm

Agent-tool isolation worktrees auto-clean **only when nothing edited
them**. Anything an agent actually TOUCHED persisted forever, and until
now no periodic GC, no cap, and no alarm existed anywhere: one repo
reached **105 worktrees** and helped trigger a host load-spike
(`incident-worktree-sprawl-permanent-gc-20260710`, operator-declared P1).

Sprawl is not a tidiness problem — it is a standing liability. This is
the permanent countermeasure.

> Documented as a standalone page rather than a section in
> `scripts/systemd/README.md` (where `sac.accounts-refresh` and
> `sac.host-sync-check` are written up): that file already sits exactly at
> the repo's 512-line cap, so adding to it demands an unrelated refactor.

## The safety predicate — the whole point

A worktree is removed **iff ALL FOUR** legs pass:

| leg | passes when | on doubt |
|---|---|---|
| **CLEAN** | `git status --porcelain` is empty — **untracked counts as DIRTY** | KEEP |
| **MERGED** | ancestor of `develop`/`main`, **OR** a merged PR exists for the branch | KEEP |
| **OLD** | the HEAD commit is older than `--min-age-hours` (default 24) | KEEP |
| **IDLE** | no running process has its cwd inside it (best-effort `/proc` scan) | KEEP |

Every leg is **three-state**, never boolean: a check that could not RUN
returns UNKNOWN and the worktree is KEPT with a reason naming the
unknown. "I could not look" must never collapse into "I looked and it was
fine" — that collapse is exactly how a predicate destroys work.

The asymmetry is deliberate and permanent:

- a false **KEEP** leaves a stale directory on disk — annoying, and the
  cap alarm shouts about it;
- a false **REMOVE** destroys work that exists nowhere else.

We pick annoying, every time.

Two details that are load-bearing rather than incidental:

- **Untracked files count as DIRTY.** An untracked file is work saved
  nowhere else, which makes it the most expensive thing in the tree, not
  the cheapest.
- **The MERGED leg needs both styles.** A squash-merged branch is *not*
  an ancestor of its base, so the ancestor check alone would call every
  squash-merged branch "unmerged" forever and a squash-merging repo would
  never be GC'd at all. The merged-PR lookup (`gh pr list --head <branch>
  --state merged`) covers that case, and it answers `None` (→ KEEP) on
  any doubt: gh missing, unauthenticated, offline, or rate-limited.

## What it never touches

- **`--force` is never passed.** Removal is plain `git worktree remove`,
  so git's own refusal to remove a dirty worktree is a second,
  independent implementation of the CLEAN leg — the only check in this
  system we did not write ourselves. Passing `--force` would disable it.
  A test pins the absence.
- **The main worktree and bare repos.** They are not sprawl; they are the
  repo.
- **Locked worktrees.** A lock is a human saying "leave this alone".
- **Repos no agent declares.** See `--all`'s source below.

`git worktree prune` runs alongside as the separate, always-safe half: it
only drops administrative refs whose directory is **already gone**, so it
destroys no files by construction and needs no predicate. On a dry run it
is invoked as `prune --dry-run` — a report, so `--dry-run` is a pure read
across the *whole* pass, not just the remove half.

## Usage

`--dry-run` is the **default**. A GC whose default is destructive gets run
destructively by accident exactly once.

```bash
# Report (read-only — removes nothing)
sac worktree gc --repo ~/proj/scitex-todo
sac worktree gc --all
sac worktree gc --all --json          # machine-readable, for cron

# Act
sac worktree gc --apply --all
```

| flag | default | meaning |
|---|---|---|
| `--dry-run` | **on** | read-only report (explicit form, for scripts) |
| `--apply` | off | remove what the predicate proved safe |
| `--repo PATH` | — | sweep this repo (repeatable) |
| `--all` | — | sweep every declared repo (see below) |
| `--min-age-hours N` | 24 | keep any worktree whose HEAD commit is younger |
| `--cap N` | 20 | alarm when a repo *still* has more than N after the pass |
| `--alarm/--no-alarm` | on with `--apply` | record cap verdicts in sac's event log |
| `--json` | off | structured output |

**Exit codes**: `0` every repo under cap · `1` a repo is still over cap
after the pass · `2` a repo could not be READ (unknown outranks
known-bad, because it is a known-bad you cannot see).

### What `--all` sweeps

Every local **git-repo toplevel** declared as some agent's
`spec.workdir`. sac's own spec tree is the source, so `--all` cannot
drift from a second registry that would need its own maintenance. Two
filters keep that source clean:

- **Must exist locally** — specs describe agents on Spartan and inside
  containers too; this GC only ever touches the machine it runs on.
- **Must be a repo toplevel** — a workdir merely *inside* a repo does not
  drag the enclosing repo in, and the default per-agent runtime workspace
  is not a repo at all.

A repo that no agent spec declares is **never** swept by `--all` — name
it with `--repo`. That gap is honest and narrow: worktree sprawl comes
from agent tools, and agent tools run in agent workdirs.

## The cap alarm

The reaping is the easy half. The half that prevents the incident is
**shouting about what the predicate refused to touch**, because those are
the worktrees that accumulate forever.

After a pass, a repo still over `--cap` is recorded in sac's own
append-only event log (`sac-events.jsonl`):

- **event** `subject-degraded`, **subsystem** `worktree-gc`,
  **subject** the repo basename
- **fields** carry the count, the cap, how many were removed, and the
  **kept-reasons breakdown** (`9 dirty, 6 unmerged, 2 in-use`) as
  structured data. That breakdown is the record's whole value: "17 kept"
  is a number, "9 dirty" is an instruction.

Three-state, like the predicate: **over cap** → `subject-degraded`;
**unreadable repo** → `subject-unknown` (never rendered clean); **back
under cap** → `subject-recovered`, recorded on the transition, so a
fixed repo stops shouting. A repo that sprawls, gets cleaned, then
sprawls again is recorded as degraded again.

Recording is a **side rail**: a failed write prints loudly to stderr and
never crashes the GC that feeds it.

## The daily timer

`sac.worktree-gc` is a federated `scitex_dev.jobs` JobSpec
(`_jobs_plugin.py`), `kind="timer"`, running `sac worktree gc --apply
--all` daily. This is what makes the countermeasure *periodic* — a GC
nobody schedules is a script, not a countermeasure, which is precisely
how the sprawl accumulated.

Sprawl accumulates over days and the age gate is 24h, so a faster cadence
could not remove anything a daily pass would miss.

Install (operator-side). sac's own wrapper works again — it used to query
a dead kind (`jobs_of_kind("systemd")`) and report "No sac systemd-kind
jobs to install." forever, so this page told you to route around it. Both
forms below are equivalent; the wrapper just filters `sac.*` for you:

```bash
sac dev systemd install --yes                 # or, equivalently:
scitex-dev ecosystem systemd install --name sac.worktree-gc --yes
systemctl --user daemon-reload
systemctl --user enable --now sac.worktree-gc.timer

# Verify
systemctl --user list-timers sac.worktree-gc.timer
journalctl --user -u sac.worktree-gc.service -n 50
```

Before enabling it, look at what it *would* do — the default is read-only:

```bash
sac worktree gc --all
```

## Where the code lives

| file | concern |
|---|---|
| `_maintenance/_worktree_gc_model.py` | dataclasses, keep-reason vocabulary, exit codes |
| `_maintenance/_worktree_gc_probe.py` | observation: git, `/proc`, `gh` (the injectable seams) |
| `_maintenance/_worktree_gc_predicate.py` | **the four legs** |
| `_maintenance/_worktree_gc.py` | the engine (dry-run/apply, remove, prune) |
| `_maintenance/_worktree_gc_alarm.py` | the cap verdict record |
| `_maintenance/_worktree_gc_repos.py` | `--all`'s repo discovery |
| `cli_pkg/_worktree_gc.py` | the `sac worktree gc` verb |

Tests drive **real** temp git repos with **real** `git worktree add` (and
a real child process for the in-use leg) — no mocks. Only the merged-PR
lookup and the `/proc` scan are injected, so the suite needs no network.

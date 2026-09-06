---
description: |
  [TOPIC] The convenient read answers confidently and can be wrong; only the authoritative read counts.
  [DETAILS] On 2026-08-12 three agents independently misreported fleet state in one night from four instruments that each returned a cached, partial or overridden answer WITHOUT saying so: `git fetch` leaving a stale `origin/<branch>`, `gh api .../actions/runners` returning `status: offline` on a row whose own `busy: true` contradicted it, `gh pr checks` making a not-yet-spawned check identical to an absent one, and `nproc` returning 1 on a host whose affinity mask held 48 CPUs. Gives the general rule, the authoritative counterpart for each, and the tell that catches this class: an internally inconsistent row is evidence about the INSTRUMENT, not about the thing it describes.
tags: [scitex-agent-container-authoritative-vs-convenient-reads, ci, git, github-api, runners, nproc, verification, false-alarm]
---

# The authoritative read, and the convenient one that disagrees

**The rule**

> Prefer the authoritative read. When the cheap read is the only one you took,
> **say so** — report it as "the cached view says X", not as "X".

Four instruments, three agents, one night (2026-08-12). Each returned an answer
that was cached, partial, or overridden — and none of them said so. Every
resulting report was confident and wrong.

| Instrument | What it said | Why it was wrong | Authoritative read |
|---|---|---|---|
| `git fetch origin <branch>` | exit 0, "success" | did not advance `refs/remotes/origin/<branch>`; a later `git log origin/<branch>` read a **stale local ref** | `git ls-remote origin refs/heads/<branch>`, or `gh api repos/O/R/contents/<path>?ref=<branch>` |
| `gh api /orgs/O/actions/runners` | `status: offline` | a **cached liveness field**; the runner served jobs minutes either side | job-serving history: `gh api .../actions/runs/<id>/jobs --jq '.jobs[].runner_name'` |
| `gh pr checks <n>` | check absent | a check not yet **spawned** is indistinguishable from one that does not exist | the workflow's own matrix + the run's jobs list |
| `nproc` | `1` | honours `OMP_NUM_THREADS` **ahead of** the affinity mask | `python3 -c 'import os;print(len(os.sched_getaffinity(0)))'`, or `taskset -pc $$` |

The first three are caching/partiality. The fourth is a different sub-family —
an **overridable derived readout** — but it belongs here because it fails the
same way: a one-word answer, no indication that it was not measured directly.

## The tell

**An internally inconsistent row is evidence about the instrument, not about the
thing being described.**

The runners endpoint returned, on the *same row*:

```json
{"name": "scitex-04-org-cpu-01", "status": "offline", "busy": true}
```

A runner cannot be busy and offline. That contradiction was the signal to stop
trusting the endpoint — instead it was read as a fact about the runner and
propagated to two agents and the operator before anyone checked job history.

Same shape in the `nproc` case: `nproc` said 1 while `nproc --all` said 128 and
`sched_getaffinity` said 48. Three numbers from one machine that cannot all
describe the same thing. The disagreement *was* the finding.

When two fields of one response contradict each other, you have learned
something about your instrument. Go get the other read.

## What this cost

- A branch reported red that was green.
- A runner reported down that was serving jobs, told to two agents and the
  operator, then retracted.
- A merged PR nearly reported as unmerged — caught only because the reporter
  checked `ls-remote` before escalating.
- A CI suite run on **4 xdist workers instead of 48**, inside a 48-CPU
  allocation the fleet was already paying for, because `run-in-sif.sh` derives
  its worker count from `nproc`. Every timing conclusion drawn from those runs
  described the wrong machine.

## Practical rules

1. **Verifying a merge, a branch tip, or file content on a remote branch?**
   Ask the server. `git ls-remote` and the contents API at `?ref=` cannot be
   stale. A local `origin/<branch>` can be, even immediately after a `fetch`
   that exited 0.
2. **Verifying that something is alive?** Liveness fields are cached. Use
   evidence of *work done*: the last job served, the last heartbeat row, the
   last commit. Absence of recent work is weak evidence; presence is strong.
3. **Verifying that a check is missing?** Distinguish *not spawned yet* from
   *does not exist* before calling it missing. Read the workflow that would
   spawn it. Two agents called a deliberately-absent `py3.12` leg a missing
   required check on the same night; the gate is min+max by design.
4. **Verifying a resource count** (CPUs, memory, file descriptors)? Prefer the
   kernel's own answer for *this process* over a summary utility. `nproc`,
   `free`, and `ulimit` all report through layers that can be overridden.
5. **When you only took the cheap read**, say which one you took. "The runners
   API reports it offline" is a true and useful sentence. "The runner is down"
   is a claim you have not earned.

## The generalisation

A read is *convenient* when it is fast, one call, and returns a scalar. Those
three properties are exactly what makes it likely to be serving you something
derived, cached, or defaulted. Convenience and authority are in tension, and the
instrument almost never volunteers which one it is giving you.

So the question to ask before reporting is not "what did the tool say" but
**"could this tool have said this without the underlying fact being true?"** If
yes, take the second read before you escalate.

## See also

- `34_spec-is-a-contract-not-state.md` — the same distinction in a different
  costume: the spec is a design document, the database is the state.
- `46_agents-list-auth-cache.md` — a cached auth view with the same failure mode.

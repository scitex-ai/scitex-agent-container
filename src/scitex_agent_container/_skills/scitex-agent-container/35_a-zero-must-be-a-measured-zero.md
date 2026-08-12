---
description: |
  [TOPIC] An instrument's `0` must be shown to be a MEASURED zero before it is believed — "did not check" and "checked, and it is clean" print the same character.
  [DETAILS] Three independent instances measured in one night (2026-08-12), in three different instruments: greps against a zero-byte CI log ruled out the exact bug being hunted; `gh`'s empty-string `conclusion` made QUEUED checks read as green and let the auto-merge sweep treat un-run CI as passing; and `git log` invoked directly through the Bash tool returned another ref's history while silently discarding an explicit 40-char sha, which reads as "your commits are gone". Covers the one question that catches all three, the size-guard pattern, three-states-not-two, and the rule that a destructive recovery action needs a second instrument.
tags: [scitex-agent-container-a-zero-must-be-a-measured-zero, evidence, ci, gh, git, grep, false-negative, auto-merge]
---

# A zero must be a measured zero

**The failure shape**, seen three times in one night in three unrelated
instruments:

> **A value meaning "I did not check" renders identically to a value
> meaning "I checked, and it is fine."**

Nothing errors. Nothing is red. You get `0`, or `[]`, or an empty string,
and it looks exactly like the good news you were hoping for — so the
inquiry stops there. This is the same family as
`reference-evidence-that-could-not-have-disagreed`: the check could not
have told you the bad news even if the bad news were true.

## The one question

Before believing any negative result, ask:

> **What would this output be if the check had never run at all?**

If the answer is "the same thing I am looking at now", you have not
measured anything yet. Go and prove the instrument produced a
measurement *before* you read its verdict.

## Instance 1 — greps against an empty log

Hunting a specific CI failure, PR #985, py3.13 leg:

```bash
gh run view "$RUN" --log-failed > failed.log       #  81 bytes
grep -c "BrokerSelfError"        failed.log        # -> 0
grep -c "did not become healthy" failed.log        # -> 0
grep -c "failed to write commit object" failed.log # -> 0
```

Three zeros. Read naively that is *"not the known runner fault, not the
free-port race"* — a confident ruling-out of the exact hypotheses under
test. In fact the log was **81 bytes** containing
`run ... is still in progress; logs will be available when it is complete`.
Run-level `--log-failed` yields nothing while any job in the run is still
going.

The second attempt returned **0 bytes**, for a different reason: `gh api`
withholds a response containing terminal escape sequences unless you pass
`--allow-escape-sequences`. The error went to stderr; the greps went on
returning `0`.

With the real 6.1 MB log the same greps still returned `0` — the race
genuinely was not the cause — but that agreement is a coincidence, not a
vindication. Two reports earlier and the conclusion would have been
identical and unfounded.

**The guard** — refuse to conclude from an instrument that produced no
measurement:

```bash
gh api "repos/$REPO/actions/jobs/$JOB/logs" --allow-escape-sequences > "$LOG"
BYTES=$(wc -c < "$LOG")
if [ "$BYTES" -lt 1000 ]; then
  echo "!!! LOG TOO SMALL ($BYTES bytes) — drawing NO conclusion from the greps below."
  exit 3
fi
```

Cheap, and it is the only reason the ruling-out above was not published as
fact.

## Instance 2 — `gh` says a queued check is green

`.github/workflows/auto-merge-to-develop.yaml`, the per-PR greenness
filter (pre-existing; carded
`auto-merge-queued-checks-read-as-green`, fixed in #990):

```jq
[ .statusCheckRollup[]
  | (.conclusion // .state // "")
  | select(. != "SUCCESS" and . != "NEUTRAL" and . != "SKIPPED" and . != "") ] | length
```

`gh` returns `conclusion` as an **empty string, not null**, for a
CheckRun that is `QUEUED` or `IN_PROGRESS`. In jq `""` is **truthy**, so
`//` never falls through — and the trailing `. != ""` then discards it. A
check that has not started counts as neither failing nor pending.

Measured on PR #985: nine CheckRuns, every one
`"status":"QUEUED","conclusion":""`, and the filter returned **0 pending**
— i.e. *green*. The sweep duly named it a merge candidate before a single
test had run, and `gh pr merge --admin` bypasses the branch protection
that would otherwise have stopped it.

The develop-health gate a few lines above in the same file gets this
right, which is what makes the contrast legible:

```bash
# THREE STATES, NOT TWO.
dev_busy=$(... | select(.status != "completed") ... )   # pending is its own answer
```

**The guard**: never collapse an unknown into a pole. Completed-and-good
is green, completed-and-bad is red, **not-completed is neither** and must
be its own branch.

## Instance 3 — `git log` reports another ref's history

Isolated on 2026-08-12 (`/usr/bin/git` 2.43.0, no aliases, no `log.*`
config). After merging `origin/develop` into a topic branch:

| how it was invoked | command | result |
|---|---|---|
| **directly, as a Bash command** | `git -C <wt> log -1 --format=%H` | `0c2ae7a` — **develop's head, not this branch's** |
| **directly** | `git -C <wt> log --oneline -1 1c398d7…` (full 40-char sha) | `0c2ae7a` — **the explicit rev was ignored** |
| directly | `git -C <wt> rev-parse HEAD` | `1c398d7` correct |
| **from inside a `bash script.sh`** | the *same* `git log` spellings | `1c398d7 Merge …` correct, graph correct |

`rev-parse`, `show`, `cat-file`, `merge-base` and `reflog` were correct
even on the direct path. It is `log`, and only on the direct path — which
fits the argv-inspecting git wrapper this environment runs (the layer that
prints `ok fetched` summaries and refuses "too complex" compound
commands). A script file hands it only `bash script.sh`, so nothing is
intercepted.

**Why this one is dangerous rather than merely wrong.** The output read
exactly like a topic branch had been fast-forwarded away and two commits
destroyed. The natural next action is `reset --hard`, a re-push, or a
force-push — a *destructive* response to a loss that never happened.

**The guard**:

> Never trust `git log` run directly as a Bash command here, and **never
> take a recovery action on its say-so.** Confirm with `rev-parse`,
> `show` or `reflog`, or run `git log` from a script file. The reflog in
> particular cannot be talked out of the truth: it recorded
> `merge origin/develop: Merge made by the 'ort' strategy` while `log`
> was insisting the merge did not exist.

## The rule, generalised

1. **Ask what the output would be if the check never ran.** Same as
   success? Then it is not evidence.
2. **Prove the instrument measured something** before reading its
   verdict — file size, row count, exit status, a sentinel the real path
   must emit. A grep over an empty file is not a negative result.
3. **Three states, not two.** "Unknown" is an answer; folding it into
   "fine" is how un-run CI merges.
4. **A destructive action needs a second instrument.** One tool saying
   your work is gone is a hypothesis. `reset --hard` on a hypothesis is
   how the hypothesis becomes true.

An earlier draft of this note claimed instance 3 was a `--graph`
rendering artifact. That was wrong, and the correction matters more than
the note: running the 2×2 (`--oneline` vs `--format`, direct vs
in-script) showed the real behaviour was *worse* than the tidy
explanation — not cosmetic rendering but a wrong ref and a discarded
argument. **A plausible story that explains the symptom is not the same
as the mechanism**, and the difference is one experiment wide.

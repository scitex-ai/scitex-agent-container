# CI runner routing

Where this repo's CI jobs run, who decides it, and why the decision is a
repository **variable** rather than a line of YAML.

## The one rule

> A job's runner **pool** is named by a repository variable. It is never
> frozen into the workflow file.

A pool frozen in YAML can only be re-pointed by a pull request — through the
CI gate that the dead pool just jammed. A pool named in a variable is
re-pointed in one click, by someone who does not need a review to do it.

Enforced by `src/scitex_agent_container/_runner_pool_guard.py` (`SAC-CI005`),
which runs as a step of the `no-hosted-runners` job, as a `pre-commit` hook,
and as a pytest test against this repo.

## The variables

| Variable | Read by | Set? |
|---|---|---|
| `CI_RUNS_ON` | the heavy gate, the release gate, docs/quality/auto-merge — nine workflows | yes: `["self-hosted","Linux","X64","scitex-org-cpu"]` |
| `LIGHT_RUNS_ON` | the light lane only (four jobs) | **no — deliberately unset** |

The canonical spellings:

```yaml
# everything else
runs-on: ${{ fromJSON(vars.CI_RUNS_ON || '["self-hosted","Linux","X64","scitex-ci"]') }}

# the light lane
runs-on: ${{ fromJSON(vars.LIGHT_RUNS_ON || vars.CI_RUNS_ON || '["self-hosted","Linux","X64","scitex-ci"]') }}
```

The **literal JSON fallback is required**, not decorative. A bare
`${{ vars.X }}` is refused twice: by `_hosted_runner_guard` (`SAC-CI002` — a
destination it cannot resolve is indistinguishable from a bypass) and by
scitex-dev's `PS-224`. Naming the destination in a variable and naming it
readably are both requirements; neither substitutes for the other.

## Why `LIGHT_RUNS_ON` is unset

`vars.LIGHT_RUNS_ON` evaluates to the empty string when unset, so `||` falls
through and the light lane rides `CI_RUNS_ON` — the correct default. The
variable exists so the lane **can** be split off again the moment there is a
pool worth splitting it onto, without touching four files.

Setting it is therefore an act with a cost: it becomes a second place the
routing lives, and a stale override is exactly the failure below. Set it when
the split buys something measurable; unset it the moment it does not.

## The light lane, and what it is for

This repo's CI is bimodal. Over 276 self-hosted jobs (2.1 h of machine time),
`tests` is 31% of jobs at 368 s and saturates 32 cores (`xdist -n $(nproc)`).
The other 69% run 12–51 s on about one core, never enter the CI SIF, and need
nothing but `uv` and a Python:

| job | workflow | median |
|---|---|---|
| `ruff` | `lint.yml` | 12 s |
| `no-hosted-runners` | `no-hosted-runners-guard-on-self-hosted.yml` | 13 s |
| `import-smoke` | `import-smoke-on-ubuntu-py3-12.yml` | 47 s |
| `rtd-sphinx-build` | `rtd-sphinx-build-on-ubuntu-latest.yml` | 51 s |

This table had a fifth row, `scitex-dev-quality-audit` /
`quality-audit-on-ubuntu-latest.yml` (44 s), until that workflow was deleted.
It is named here because the incident below counts five checks and the
arithmetic should not look wrong: the job's five audit steps invoked pre-0.11
`scitex-dev quality audit-*` verb spellings, every one of which exits 2 on the
installed scitex-dev, under `continue-on-error: true` — 44 s of a light-lane
slot per push and per PR to produce a green that measured nothing. The audit
it appeared to perform is really performed by `tests/develop/test_audit.py`
(`scitex-dev ecosystem audit-all`) inside the `tests` matrix leg.

A 12-second single-core job holding one of four 32-core machines, on a pool at
93–94% utilisation, after queueing 289 s median (p90 902 s), is a real waste
and PR #1006 was right to route it away. **That intent stands.** Only the seam
changed.

## The incident (2026-08-12)

PR #1006 wrote the light lane's destination as a literal:

```yaml
runs-on: ["self-hosted", "Linux", "X64", "spartan-cpu"]
```

Its own comment argued the choice: *"PINNED PER WORKFLOW rather than through
vars.CI_RUNS_ON so the routing is visible in a diff."* Visibility in a diff is
a real benefit. It is not worth what it cost.

Six days later the three `spartan-cpu` runners went offline (post-inode stop).
All five checks queued **forever** on every pull request while four
`scitex-org-cpu` runners sat online and idle. 34 runs were queued against zero
busy runners; `mergeStateStatus` read `UNSTABLE`; 15 open PRs could not merge.

The failure mode is worth naming, because it is the reason this was not caught
sooner: **nothing failed.** The checks never *started*, and a check that never
started is visually identical to one that has not run yet. There is no red.
The only signal is a queue depth nobody was watching, and the fix required a
PR through the gate that was jammed.

## When a literal pin is actually right

When a job's purpose is **to reach one named place**, rather than to run
somewhere appropriate. A variable expresses "wherever this class of work
belongs"; it cannot express "that specific machine, and no substitute".

Two such jobs exist here, both in `.github/runner-pin-allowlist.yaml` with a
machine-enforced `reason:`:

- **`spartan-capacity-canary` → `spartan-cpu`.** Naming the pool *is* the
  measurement. Reading the variable would make it interrogate whichever pool
  is already configured — the question we already know the answer to.
- **`pytest-matrix … / verdict` → `sac-control-plane`.** It writes the CI
  verdict over loopback to the one host running `sac listen` and the card
  store. Three of the four `scitex-org-cpu` machines would write it somewhere
  nobody reads, with nothing raising an error.

Note what they share: each would be **wrong if it silently ran somewhere
else**. That is the test. "This pool is faster for this job" is not — that is
a routing preference, and routing preferences go in variables.

Both are also jobs that gate nothing: the canary is `workflow_dispatch`-only,
and `verdict` is not a required check. A pin that gates a merge is a pin that
can hold the repo hostage.

## Runbook: a pool has gone offline

1. Confirm it is not saturation — `gh api orgs/scitex-ai/actions/runners`
   shows `status` and `busy` per runner. Idle runners plus a deep queue is a
   routing fault, not a capacity fault.
2. Re-point the variable, not the code:
   `gh variable set CI_RUNS_ON --repo scitex-ai/scitex-agent-container --body '["self-hosted","Linux","X64","<live-pool>"]'`
   (and `LIGHT_RUNS_ON` only if it is set — prefer unsetting it).
3. Cancel the runs whose jobs *requested the dead pool*. Enumerate first:
   `gh api repos/<repo>/actions/runs/<id>/jobs --jq '.jobs[] | [.name, .status, (.labels|join(","))] | @tsv'`.
   Never cancel an `in_progress` run, and never blanket-cancel — a queued run
   waiting on a *live* pool will start on its own.
4. A variable change does **not** retroactively re-route already-queued runs.
   They keep the labels they were created with; re-run them.

## The rule this does not enforce

Which pool the variable points at. That is a settings value no static reader
can see, and it is exactly the thing that should change without a commit. The
guard enforces only the weaker, statically-decidable property that makes
redirection possible at all.

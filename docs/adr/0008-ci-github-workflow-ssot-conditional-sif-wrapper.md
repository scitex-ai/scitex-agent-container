# ADR: GitHub workflow is the CI SSOT; `.github/ci/` conditionally wraps it in the SIF (2026-07-15)

**Status:** Accepted (operator design, 2026-07-15).

**Context:** We do not pay for GitHub-hosted runners; CI runs on the
self-hosted Spartan compute nodes (constitution §4). The migration off
hosted runners was done per-workflow by changing only `runs-on:`. That
left the ecosystem in two inconsistent shapes, and produced a live
failure on `main`.

## Problem

Two ways of running a job co-existed, and they diverged:

- **Bare-node jobs** (`lint`, `quality-audit`): the workflow keeps the
  GitHub-style steps (`actions/setup-uv`, `uv pip install`, `ruff
  check`) and runs them **directly on the Spartan node**. Measured:
  their run logs contain **zero** `apptainer`/`exec-in-sif` references.
- **SIF jobs** (`pytest-matrix`, `import-smoke`, `pypi-publish`,
  `newb-docs`): the workflow calls `bash .github/ci/exec-in-sif.sh
  <inner>`, which `apptainer exec`s the CI SIF and runs the command
  **inside** it (`exec-in-sif.sh:95`).

The bare-node path fails on Spartan. `actions/setup-uv` extracts uv to
GPFS scratch, then `copyfile`s it into `RUNNER_TOOL_CACHE`
(`/home/ywatanabe/.runner-toolcache/`, which is on GPFS). That copy
dies with `errno -116` (ESTALE, stale file handle). This is today's red
on `main` (`lint`, `quality-audit`) — not a code failure and not a
flake, but a filesystem failure caused by an unfinished migration.

Chasing "make the bare node resemble a GitHub VM" is a losing battle:
it means reproducing GitHub's entire pre-baked image on a node with no
root and network-filesystem `$HOME`.

## Decision

The **GitHub workflow YAML is the single source of truth** for what CI
does. It is written to run **as-is on GitHub-hosted runners** — plain
`actions/setup-*` + the real commands, no Spartan specifics.

Execution on Spartan is a **separate, conditional layer** in
`.github/ci/`, not a fork of the workflow:

- `.github/ci/exec-in-sif.sh` (and its `run/build/publish` siblings) is
  the adapter. Given the CI SIF (resolved from the repo Actions
  Variable `SCITEX_CI_SIF`, e.g. `~/.scitex/dev/containers/ci-cpu.sif`)
  and the apptainer shim (`SCITEX_CI_APPTAINER`), it `apptainer exec`s
  the SIF and runs the workflow's command **inside** it.
- The switch is **configuration, not code**: if the SIF variable is
  set, the command runs inside the SIF; if not, it runs bare (i.e.
  exactly as GitHub-hosted would). `exec-in-sif.sh` already references
  these variables and its header already anticipates a bare-runner
  path.
- **The SIF is our Docker-preparation equivalent.** GitHub runs each
  job inside a Docker image full of tools; we run each job inside the
  SIF full of tools. One-to-one.

Consequently:

- `lint` and `quality-audit` must be routed through the same SIF
  adapter that `pytest-matrix` already uses. This is the missing second
  half of the runner migration; today all six jobs must go through the
  adapter.
- Inside the SIF, `RUNNER_TOOL_CACHE` must be pinned to a **SIF-local**
  path (`/opt/hostedtoolcache`, exactly GitHub's path, on the container
  overlay — not GPFS). Running inside the SIF is necessary but **not
  sufficient**: `exec-in-sif.sh` binds `/data/gpfs/...punim0264` and
  `$HOME/.scitex` is a symlink into it, so if the toolcache still
  resolves onto GPFS the `copyfile` lands on GPFS again and ESTALE
  returns *inside* the container. Measured: `/opt` is writable inside
  our apptainer overlay as uid 1000 (no root), so `/opt/hostedtoolcache`
  is viable and matches GitHub 1:1. (This must still be verified on the
  CI SIF specifically, not only on the agent container.)

## Rationale

- **One behaviour, two places to run it.** The operator's requirement,
  stated repeatedly: "just use Spartan's compute; do not split
  GitHub-does-this / Spartan-does-that; run the exact same thing on
  Spartan; waste is fine, uniformity is worth far more." A single
  GitHub-portable workflow + a conditional wrapper satisfies that; a
  Spartan-specific workflow does not.
- **SSOT.** The workflow file is the one authoritative description of
  each job. The Spartan-specific mechanism lives once, in
  `.github/ci/`, and is referenced — not copied into each workflow.
- **SoC.** "What the job is" (workflow) is separated from "where it
  runs" (the `ci/` adapter). Changing the execution environment does
  not touch the job definitions.
- **Fail loud, no silent fallback (constitution §2).** The bare-vs-SIF
  branch must **log which path it took** every run ("SIF set → running
  inside"; "SIF unset → running bare = GitHub-equivalent"). The current
  `${VAR:?...}` hard-errors when the variable is missing; the conditional
  form must announce the branch, never silently pick one.

## Consequences

- The workflow YAML stays diffable against a hypothetical GitHub-hosted
  copy; ideally the diff is empty (the wrapper is applied by the runner
  layer, not per-step). Where a per-step `bash .github/ci/exec-in-sif.sh`
  prefix is used instead, that prefix is the only allowed divergence and
  must be uniform.
- This is the reference implementation for unifying `.github` across the
  scitex ecosystem (constitution §4, no hosted runners). Once proven on
  scitex-agent-container, the `ci/` adapter is copyable and the workflow
  YAML remains the GitHub original.
- Verification is by watching it fail then pass: reproduce the ESTALE on
  the bare node (run 29370729709), then confirm the same `lint` job runs
  inside the SIF (apptainer present in its log) and `setup-uv` writes to
  `/opt/hostedtoolcache` and succeeds.

## Related

- ADR-0005 (SIF-mode migration), ADR-0007 (Spartan apptainer canonical
  args) — this ADR extends the SIF execution model to *all* CI jobs.
- Cards: `sac-unify-all-ci-jobs-inside-sif-github-workflow-as-ssot-20260715`
  (implementation), `fleet-unify-all-github-spartan-only-no-hosted-runners-20260714`
  (the `runs-on` migration this completes).

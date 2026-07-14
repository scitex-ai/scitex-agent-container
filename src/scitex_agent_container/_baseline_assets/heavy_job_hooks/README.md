# Heavy-job demotion guard hook

Canonical, version-controlled, tested source for the PreToolUse Bash
hook that keeps agents on a shared interactive host from launching
known-HEAVY jobs at normal priority. Sibling in spirit to
`../hpc_login_hooks/`: the authoritative copy lives here in the
package and is **propagated** into the fleet baseline that
`runtimes/_to_home.py` materializes into every agent's
`$HOME/.claude/`.

## Why

P1 incident 2026-07-10 (`incident-local-heavy-build-saturated-
interactive-host-20260710`): a full SIF rebake (`sac image build`,
apt + pip + mksquashfs, ~40 min of sustained CPU+IO) ran at NORMAL
priority on the operator's already-loaded interactive host
(ywata-note-win, ~8 claude sessions, load ~27). Load spiked past 50
and the operator's interactive session starved — he was the monitor.

Closure is a three-part system, not a memory:

1. `sac image build` **self-demotes** by default (PR #605,
   `_build_priority.py`) — fixes sac's own build path.
2. **This hook** guards the general PATTERN: ANY heavy job an agent
   launches by hand (raw `apptainer build`, `mksquashfs`, mass
   compression, `make -j16`, …) without a `nice`/`ionice` prefix is
   blocked with the corrected command. The guard, not the memory —
   same shape as the env-dump and reload-watch guards.
3. A **remote-first advisory** in the build path (loadavg above 1.5x
   cores — calibrated to the incident's pre-build load, ~27 on 16
   cores — → loud scitex-logging warning advising Spartan / a
   dedicated build host). Actual remote build routing is a separate
   card (`sac-spartan-sif-parity-rebuild-distribute-drift-check-20260702`).

## What

- `enforce_heavy_job_demotion.sh` — the PreToolUse Bash hook
  (wrapper: `--self-test`, enablement switch, cheap keyword fast-path,
  bypasses, delegation).
- `heavy_job_demotion_core.py` — the decision engine (quote-aware
  command parsing incl. heredoc bodies / wrapper chains / `bash -c`
  recursion; demotion detection; judging). **Fails open** on any
  unparseable payload or missing core file: a broken hook must never
  brick the agent. The segment splitter is copied verbatim from the
  battle-tested `hpc_login_whitelist_core.py`.
- `heavy_job_demotion_policy.py` — the policy data: heavy classes, the
  taught demotion prefix, env-knob names, per-class educational texts,
  block-message builder. "Add a command / reword an error" never
  touches parsing logic.
- `settings.local.json.fragment.json` — the PreToolUse wiring snippet
  to merge into the baseline `settings.json` (see below).

### The taught prefix (and why NOT ionice idle)

```
nice -n 19 ionice -c 2 -n 7 <cmd>
```

Field-tested the night of the incident: a host SIF build at
`ionice -c 3` (idle class) starved and **died silently at the
mksquashfs stage** under sustained load — idle-class IO is only
serviced when the disk is otherwise idle, so it can starve
indefinitely. The retry at `-c 2 -n 7` (best-effort lowest) + `nice
19` completed fine: it still yields to all interactive IO but is
guaranteed forward progress. Constants + rationale live in
`heavy_job_demotion_policy.py::DEMOTE_PREFIX` and
`scitex_agent_container/_build_priority.py` — keep them in lockstep.

### Heavy classes (deny-list; everything else passes)

| class | trigger | notes |
| ----- | ------- | ----- |
| image_build | `apptainer\|singularity build`, `docker build`, `docker buildx build`, `docker\|podman compose build`, `podman build`, `buildah build\|bud`, `nerdctl build`, `docker-compose build` | other subcommands (`ps`, `exec`, `compose up`, …) pass |
| squashfs | `mksquashfs`, `unsquashfs` | the exact incident stage |
| compress | `xz`/`pixz`/`pigz`/`pbzip2`/`zstd`/`lrzip`/`lzma`/`plzip`/`lz4` (+un\* variants) | `--version`/`--help` pass; plain `gzip`/`bzip2` deliberately ungated (single-file, brief, low-impact) |
| archive | `tar` CREATE (`czf`/`-cJf`/`--create`), `zip -r`, `7z a` | extraction / listing pass |
| parallel_build | `make`/`gmake`/`ninja`/`cargo`/`cmake`/`ctest`/`bazel`/`mvn`/`gradle`/`gcc`/`g++`/`clang(++)`/`rustc`/`nvcc` with `-j`>threshold, bare `-j`, or dynamic `-j$(nproc)` | serial / `-j<=$SAC_HEAVY_JOB_JOBS_MAX` (default 4) pass |
| sac_no_nice | `sac image build --no-nice` (or `SAC_BUILD_NO_NICE=1` prefix) | plain `sac image build` self-demotes and passes |
| extra | names in `$SAC_HEAVY_JOB_EXTRA_DENY` | per-host extension (e.g. `rsync` on a host where bulk rsync hurt) |

A `nice` or `ionice` anywhere in the segment's wrapper chain marks the
whole segment demoted (priority is inherited by every descendant) and
allows it. Scripts (`bash foo.sh`) are opaque to a deny-list and pass;
`bash -c '...'` / `eval` payloads are recursed into.

Known precision limits (guardrail for cooperative agents, NOT a
security boundary): command/process substitution payloads are not
descended into; a value-taking global flag before a subcommand (e.g.
`docker --context foo build`) can evade subcommand detection — both
fail toward ALLOW, and the bypasses below are the sanctioned escape.

### Knobs and bypasses

- `SAC_HEAVY_JOB_GUARD_DISABLE=1` — standing opt-out for DEDICATED
  build hosts (set once in that host's profile; any value other than
  empty/`0`).
- `SAC_HEAVY_JOB_JOBS_MAX=8` — raise the `-j` threshold.
- `SAC_HEAVY_JOB_EXTRA_DENY='rsync,ffmpeg'` — extend the deny set.
- `SAC_HEAVY_JOB_ALLOW=1` — env escape (rare, operator-supervised).
- `# hook-bypass: heavy-job` — inline per-command marker.

### Performance

The hook runs on EVERY Bash call, so the wrapper pre-filters with a
single `grep -qE` for candidate heavy keywords and only spawns the
python core on a hit (the pre-filter is skipped when
`SAC_HEAVY_JOB_EXTRA_DENY` is set, since extra names are not in the
built-in regex). The self-test's block cases cover every class, so a
pre-filter/policy drift fails the self-test.

## How to deploy fleet-wide

The hook only FIRES once it is in the materialized baseline. The
package copy here is the source of truth; propagate it exactly like
`hpc_login_hooks`:

1. Copy **all three** script files to the shared baseline pre-tool-use
   dir:

       <agents_dir>/_shared/to_home/.claude/hooks/pre-tool-use/enforce_heavy_job_demotion.sh
       <agents_dir>/_shared/to_home/.claude/hooks/pre-tool-use/heavy_job_demotion_core.py
       <agents_dir>/_shared/to_home/.claude/hooks/pre-tool-use/heavy_job_demotion_policy.py

   (In this fleet that is
   `~/.dotfiles/src/.scitex/agent-container/agents/_shared/to_home/.claude/hooks/pre-tool-use/`,
   which `~/.scitex/agent-container/agents/_shared/to_home` symlinks
   to.) `runtimes/_to_home.py` materializes that tree into every
   agent's `$HOME/.claude/hooks/pre-tool-use/` on each start — no SIF
   rebuild.

2. Merge the `PreToolUse` `Bash`-matcher entry from
   `settings.local.json.fragment.json` into the baseline
   `<agents_dir>/_shared/to_home/.claude/settings.json` `hooks` block
   (append it to the existing `Bash` matcher's `hooks` list).

3. Set `SAC_HEAVY_JOB_GUARD_DISABLE=1` in the profile of any DEDICATED
   build host, then restart agents (or wait for the next natural
   restart).

## Verification

- `bash enforce_heavy_job_demotion.sh --self-test` — 50+ cases: every
  allow group (light usage, demoted invocations, sac's self-demoting
  build, heredoc bodies), every blocked class, the educational message
  content (corrected prefix, not-idle rationale, remote-first, both
  bypasses), the knobs, and the fail-open paths. Exit 0 iff all pass.
- Mirror suite: `tests/scitex_agent_container/_baseline_assets/heavy_job_hooks/`
  drives the engine's parsing/judging helpers directly (PS-202).
- Regression suite: `tests/integration/heavy_job_hooks/` drives the
  real shell hook via subprocess (no mocks).

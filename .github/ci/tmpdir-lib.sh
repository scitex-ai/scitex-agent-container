#!/usr/bin/env bash
# Per-run scratch LIFECYCLE for the in-SIF CI scripts.
#
# SOURCE this file. It only defines functions — it creates nothing, removes
# nothing and exports nothing on its own.
#
# ---------------------------------------------------------------------------
# THE DEFECT THIS EXISTS FOR (measured on scitex-04-cpu-01, 2026-08-09)
# ---------------------------------------------------------------------------
# run-in-sif.sh, build-in-sif.sh and publish-in-sif.sh each export a per-run
# TMPDIR under /tmp, and NOTHING ever removed it. 116 surviving directories at
# 1.8-2.2 GB each put /tmp at 270 GB of a 393 GB root: root 100% FULL (39 MB
# free), inodes at 92%. Twelve fleet agents live on that box, so a full root is
# a fleet outage, not a nuisance. The pytest matrix runs 3.11/3.12/3.13, so ONE
# CI run leaked THREE directories.
#
# The bug is a HOSTED-RUNNER ASSUMPTION applied to a PERSISTENT runner. On a
# GitHub-hosted runner the whole VM is discarded after the job, so leaking
# scratch is invisible and free. Every one of this repo's in-SIF call sites is
# self-hosted today, where the same code is a slow outage — but `runs-on` is
# `fromJSON(vars.CI_RUNS_ON || …)`, so re-pointing ONE repo Actions Variable
# moves them all to hosted images with no code change. Nothing here may
# therefore REQUIRE a persistent box: the prune tolerates an empty or absent
# root, and no function in this file can fail a job.
#
# ---------------------------------------------------------------------------
# THREE MECHANISMS, because no single one covers every way a job can end
# ---------------------------------------------------------------------------
#   1. ci_tmpdir_path()   ONE definition of the directory name, used by the
#                         script that CREATES it, the step that REMOVES it and
#                         the prune that SKIPS it. Two independent spellings of
#                         this name is how a cleanup starts quietly missing the
#                         directory it exists to remove.
#
#   2. clean-tmpdir.sh    An `if: always()` job step. Covers SUCCESS, FAILURE
#                         and CANCELLATION. It is a SEPARATE step process the
#                         runner starts after the work step has been torn down,
#                         so it does not race the signals that killed that step.
#
#   3. ci_tmpdir_prune()  Startup sweep, called host-side from exec-in-sif.sh.
#                         The ONLY cover for SIGKILL and reboot, where no
#                         in-process cleanup can run by construction. That path
#                         is real here: the runner's systemd unit is
#                         KillMode=process / TimeoutStopSec=5min, so a service
#                         stop signals only the supervisor and then SIGKILLs the
#                         job's descendants.
#
# WHY NOT A bash TRAP — the obvious answer, which does not work here. Both
# layers end in `exec`: exec-in-sif.sh hands off to apptainer, run-in-sif.sh
# hands off to pytest. `exec` replaces the shell image and takes every trap with
# it, so a `trap … EXIT` added above either line SILENTLY NEVER FIRES (verified
# by probe, not assumed). Making one fire means deleting an `exec` that is
# load-bearing for signal and exit-code propagation through a 45-minute pytest
# run — autobump-release-sweep.yaml depends on that propagation by name. An
# `if: always()` step buys the same coverage for free and cannot race a SIGTERM.
#
# ---------------------------------------------------------------------------
# WHY /tmp AND NOT /scratch, even with 3.0 TB of it at 1% used on that host
# ---------------------------------------------------------------------------
# Three facts, checked on scitex-04-cpu-01 rather than assumed:
#   * /scratch is `drwxr-xr-x root root`; the runner is User=ywatanabe. A
#     `mkdir -p /scratch/…` under `set -euo pipefail` would abort EVERY CI job
#     before a single test ran — the exact failure exec-in-sif.sh already
#     documents for the Spartan GPFS path.
#   * /scratch is NOT VISIBLE INSIDE THE SIF. apptainer.conf binds only
#     /etc/localtime and /etc/hosts (plus `mount tmp = yes`, which is the whole
#     reason /tmp works), and exec-in-sif.sh passes no --bind for it. A
#     `[ -d /scratch ]` test evaluated by an INNER script is FALSE even on the
#     host where /scratch really exists.
#   * Hosted runners have no /scratch at all.
#
# And the decisive one: RELOCATING A LEAK DOES NOT FIX IT. 3 TB buys roughly
# 430 runs instead of 40 — the same outage, months later, with nobody watching.
# The lifecycle is the bug. A move would also put ~480 `tmp_path` test files and
# the SQLite state-DB tests on a volume that is probably network-backed, where
# SQLite locking is unreliable and a `--target` install's tens of thousands of
# small files are the worst possible workload; this suite has already been
# burned by that genre of flake. If disk pressure later needs structural relief,
# gate it on FILESYSTEM TYPE (node-local only, not mere existence), decide it
# host-side in exec-in-sif.sh with a conditional --bind, and land it separately
# with before/after wall-time measured. Not in a lifecycle fix.

# --- knobs (TEST-ONLY; unset in CI, where the defaults are the contract) -----
# SAC_CI_TMPDIR_ROOT     scratch root                  (default /tmp)
# SAC_CI_TMPDIR_MAX_AGE_H  prune age floor, hours      (default 24)

_ci_tmpdir_root() {
    printf '%s' "${SAC_CI_TMPDIR_ROOT:-/tmp}"
}

# The inner scripts that create a per-run scratch, and the prefix each uses.
#
# THIS TABLE IS TESTED AGAINST THE SCRIPTS THEMSELVES
# (tests/integration/test_ci_tmpdir_lifecycle.py): a new *-in-sif.sh that
# exports a per-run TMPDIR and is not listed here fails CI, rather than quietly
# leaking 2 GB per run for four months like the three above it did.
ci_tmpdir_prefix_for_inner() {
    case "${1:-}" in
    run-in-sif.sh) printf 'ci' ;;
    build-in-sif.sh) printf 'build' ;;
    publish-in-sif.sh) printf 'publish' ;;
    *) printf '' ;; # creates no per-run scratch
    esac
}

# The canonical per-run scratch path. Kept byte-identical to the names the three
# scripts already used, so this fix also reclaims the directories ALREADY on
# disk instead of starting a second, differently-named leak beside the first.
ci_tmpdir_path() {
    local prefix="${1:?prefix required (ci|build|publish)}"
    local version="${2:?python version required}"
    printf '%s/%s-scitex_agent_container-%s-%s-%s' \
        "$(_ci_tmpdir_root)" "$prefix" \
        "${GITHUB_RUN_ID:-0}" "${GITHUB_RUN_ATTEMPT:-0}" "$version"
}

# Is this path one WE created, directly under the scratch root?
#
# The guard exists because ci_tmpdir_cleanup's argument is built from a
# workflow-interpolated matrix value and then handed to `rm -rf`. Rejects the
# root itself, anything outside it, anything nested deeper, and any traversal.
_ci_tmpdir_is_managed() {
    local d="${1:-}" root base
    root="$(_ci_tmpdir_root)"
    [ -n "$d" ] || return 1
    case "$d" in
    "$root"/*) ;;
    *) return 1 ;;
    esac
    base="${d#"$root"/}"
    case "$base" in
    '' | */* | *..*) return 1 ;;
    esac
    case "$base" in
    ci-scitex_agent_container-?* | build-scitex_agent_container-?* | publish-scitex_agent_container-?*)
        return 0
        ;;
    esac
    return 1
}

# Remove ONE scratch directory.
#
# IDEMPOTENT (a path already gone is success) and CONCURRENCY-SAFE (two callers
# removing the same path both succeed). Both properties are load-bearing, not
# defensive padding: the `always()` step and the NEXT run's prune can and do
# fire on the same directory, and a re-run of a cancelled job re-enters here.
ci_tmpdir_cleanup() {
    local d="${1:-}"
    if ! _ci_tmpdir_is_managed "$d"; then
        echo "::error::refusing to remove '$d' — not a managed CI scratch path" >&2
        return 1
    fi
    rm -rf -- "$d" 2>/dev/null || true
    return 0
}

# Sweep scratch left by runs that COULD NOT clean up after themselves — SIGKILL,
# runner service stop, reboot. Called once, host-side, from exec-in-sif.sh
# before the SIF starts. This is the disk-side sibling of that script's process
# reap, and rests on the same reasoning its comment already spells out: AGE is
# what separates a leftover from a live concurrent sibling.
#
# TWO INDEPENDENT GUARDS, because this is the one part of the fix that can cause
# the very outage it was written to prevent:
#
#  (a) SELF-EXCLUSION BY RUN IDENTITY. The three matrix legs start at the SAME
#      INSTANT on the same box and share GITHUB_RUN_ID/GITHUB_RUN_ATTEMPT, so
#      the 3.11 leg's prune SEES 3.12's and 3.13's live scratch. Any name
#      carrying this run+attempt is skipped BY NAME — an exact test, not a
#      heuristic, and it holds no matter how long a leg has been running.
#
#  (b) AN AGE FLOOR for every OTHER run id, because a different workflow run can
#      legitimately be in flight on the same runner. Note what the floor
#      actually measures: after the shims are written nothing creates or removes
#      entries directly in $TMPDIR, so its top-level mtime is frozen at job
#      start and `-mmin` prunes by JOB AGE, not idle time. The floor must
#      therefore exceed the longest a job can legitimately LIVE. pytest-matrix
#      caps at `timeout-minutes: 45`, but the release workflow's test job sets
#      NO timeout-minutes and inherits GitHub's 6-HOUR platform default. 24 h is
#      4x that ceiling. The asymmetry justifies erring large: too small deletes
#      a LIVE job's scratch and produces a baffling red release, while too large
#      only delays reclaiming disk that mechanisms 1-2 already reclaim on every
#      normal ending. This prune is the backstop, not the workhorse.
#
# Never fails, never returns non-zero: a multi-user /tmp can hand us a directory
# we may not remove, and that must not take down CI.
ci_tmpdir_prune() {
    local root age_h age_min run_id attempt victims d
    root="$(_ci_tmpdir_root)"
    [ -d "$root" ] || return 0
    age_h="${SAC_CI_TMPDIR_MAX_AGE_H:-24}"
    age_min=$((age_h * 60))
    run_id="${GITHUB_RUN_ID:-0}"
    attempt="${GITHUB_RUN_ATTEMPT:-0}"

    victims="$(
        find "$root" -mindepth 1 -maxdepth 1 -type d \
            \( -name 'ci-scitex_agent_container-*' \
            -o -name 'build-scitex_agent_container-*' \
            -o -name 'publish-scitex_agent_container-*' \) \
            ! -name "*-${run_id}-${attempt}-*" \
            -mmin "+${age_min}" \
            -print 2>/dev/null || true
    )"
    [ -n "$victims" ] || return 0

    # Report every removal. A silent destructive sweep on a shared node is how
    # the next incident gets misdiagnosed.
    while IFS= read -r d; do
        [ -n "$d" ] || continue
        echo "ci-tmpdir: pruning leftover scratch (job started >${age_h}h ago): $d"
        ci_tmpdir_cleanup "$d" || true
    done <<EOF
$victims
EOF
    return 0
}

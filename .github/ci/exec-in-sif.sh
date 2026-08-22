#!/usr/bin/env bash
# Outer apptainer-exec wrapper for scitex-agent-container's self-hosted CI.
#
# Runs ON THE RUNNER (outside the SIF). Resolves apptainer + the SIF image, then
# `apptainer exec`s the SIF and hands off to an INNER script (run inside the
# container). Keeps every workflow job's YAML down to one line —
# `bash .github/ci/exec-in-sif.sh <inner-script> [args...]` — and concentrates
# all the SIF plumbing (apptainer resolution, ~-expansion, scratch, binds) in
# one version-controlled place.
#
# HOST-AGNOSTIC. This script is NOT Spartan-only: it auto-adapts to whichever
# self-hosted runner picks the job up, keyed off what actually exists on the box.
#
#   * Spartan HPC — the GPFS project dir /data/gpfs/projects/punim0264 EXISTS:
#     apptainer comes from the ~/.env-3.11 shim named by SCITEX_CI_APPTAINER,
#     apptainer scratch lives on the GPFS project, and punim0264 is bound into
#     the container ($HOME/.scitex there is a symlink into it, so without the
#     bind the symlink dangles inside the SIF).
#
#   * Local compute nodes (scitex-compute-01..04) — NO /data/gpfs at all:
#     apptainer is the distro package on PATH (/usr/bin/apptainer), scratch is
#     host-local under $HOME/.cache/scitex-ci, and the GPFS bind is OMITTED
#     (apptainer refuses a bind whose source does not exist, and `mkdir -p` on a
#     GPFS scratch path would hard-fail here under `set -e`).
#
# Each of those three decisions (interpreter, scratch, bind) is made
# INDEPENDENTLY from a probe of this host, so a runner that matches neither
# profile exactly still gets a coherent command line. Nothing here is keyed off
# a variable someone has to remember to set correctly for the host: a value that
# is right on one machine by construction is the defect being removed.
#
# Env (set by the workflow from repo Actions Variables):
#   SCITEX_CI_APPTAINER   OPTIONAL path to an apptainer shim
#                         (e.g. ~/.env-3.11/bin/apptainer). Honoured when it
#                         points at an executable; otherwise apptainer is taken
#                         from PATH.
#   SCITEX_CI_SIF         REQUIRED path to the CI SIF image
#                         (e.g. ~/.scitex/dev/containers/ci-cpu.sif)
#
# Usage:
#   bash .github/ci/exec-in-sif.sh run-in-sif.sh 3.12
#
# Fail-loud (operator directive): if NEITHER the shim nor PATH yields an
# apptainer, or the SIF is missing, that is a HARD error naming what was tried
# — never a silent fallback to a bare-runner install.
#
# FLEET LINEAGE: this is the fleet-standard shim (scitex-writer 0e7d6ad,
# "ci(sif-shim): make exec-in-sif.sh adapt to the host instead of assuming
# Spartan"), verbatim except for the repo name in this header and the two
# scitex-agent-container-specific blocks below (the leaked-process reap and the
# scratch-dir prune), which exist because THIS repo's suite spawns detached
# `python -m scitex_agent_container` processes and per-run SIF scratch dirs that
# no other repo in the fleet creates. Keep the rest byte-aligned with the fleet
# copy: a second dialect of this file is the failure mode, not the deliverable.
set -euo pipefail

INNER="${1:?inner script name required (relative to .github/ci/)}"
shift || true

# Spartan's job shell is --noprofile --norc (no Lmod), so its apptainer shim dir
# must be put on PATH explicitly; the shim execs the real Apptainer binary.
# Harmless where that directory is absent — a non-existent PATH entry is simply
# never matched — which is the case on the local compute nodes.
export PATH="$HOME/.env-3.11/bin:$PATH"

# ~-expand the Actions-Variable paths: a quoted "~/…" is NOT tilde-expanded by
# the shell, so substitute a leading ~ with $HOME ourselves.
APPTAINER_VAR="${SCITEX_CI_APPTAINER:-}"
APPTAINER_VAR="${APPTAINER_VAR/#\~/$HOME}"
SIF="${SCITEX_CI_SIF:?SCITEX_CI_SIF not set (repo Actions Variable)}"
SIF="${SIF/#\~/$HOME}"

# Apptainer resolution, in order:
#   1. SCITEX_CI_APPTAINER when it names an executable  (Spartan's shim)
#   2. `apptainer` on PATH                              (local compute nodes)
# Only when NEITHER resolves is this an error — and the message names BOTH
# attempts, because "which one did you even try" is the whole diagnosis.
if [ -n "$APPTAINER_VAR" ] && [ -x "$APPTAINER_VAR" ]; then
    APPTAINER="$APPTAINER_VAR"
    APPTAINER_FROM="SCITEX_CI_APPTAINER"
elif APPTAINER="$(command -v apptainer 2>/dev/null)"; then
    APPTAINER_FROM="PATH"
else
    echo "::error::no apptainer on this runner. Tried (1) SCITEX_CI_APPTAINER=${SCITEX_CI_APPTAINER:-<unset>} (expanded to '${APPTAINER_VAR:-<empty>}') — not an executable; (2) 'apptainer' on PATH ($PATH) — not found. Install apptainer on this runner, or point SCITEX_CI_APPTAINER at a working shim. Running the job outside the SIF on a bare-runner install is NOT an acceptable fallback."
    exit 1
fi

[ -f "$SIF" ] || {
    echo "::error::CI SIF missing at $SIF — rebuild it: scitex-container apptainer build ci-cpu"
    exit 1
}

# Apptainer scratch. On Spartan the GPFS project scratch (shared FS) keeps HOME
# clean; everywhere else that path does not exist, and `mkdir -p` under it would
# be a hard failure, so fall back to host-local scratch under $HOME.
GPFS_PROJECT="/data/gpfs/projects/punim0264"
if [ -d "$GPFS_PROJECT" ]; then
    export APPTAINER_TMPDIR="$GPFS_PROJECT/ywatanabe/ci/apptainer-tmp"
else
    export APPTAINER_TMPDIR="$HOME/.cache/scitex-ci/apptainer-tmp"
fi
mkdir -p "$APPTAINER_TMPDIR"

# --- scitex-agent-container-specific (1/2): reap leaked CI processes ----------
# Reap leaked CI processes from PRIOR runs on this persistent self-hosted node.
# Several tests spawn DETACHED `sac agents`/`sac listen` background processes
# (`python -m scitex_agent_container ...`); a failed/killed run leaves them
# running and holding a2a ports [19000-19999], so claims accumulate until the
# allocator can't find a free port (test__start_force_clears_session went red on
# the release node after repeated retries). So the reap itself is legitimate.
#
# *** BUT AN UNSCOPED pkill HERE IS A LOADED GUN AIMED AT OUR OWN SIBLING JOBS. ***
#
# This block used to justify itself with: "runs here are serialised (one job at a
# time)". That WAS true when there was ONE runner. Then -02 and -03 were
# registered — and nobody re-checked the invariant this destructive action rests
# on. It is now FALSE: all three matrix legs start at the SAME INSTANT, they run
# the SAME ci-cpu SIF, `pkill -f` is MACHINE-scoped, and the runners share one
# node (spartan-bm155). Nothing but timing has been stopping a leg from SIGTERMing
# its siblings mid-run.
#
# HONESTY, because the wrong story is worse than no story: this pkill is NOT known
# to have caused the v0.21.14 / v0.21.15 release ghost-tags. I believed it had, and
# said so. The real cause was found by scitex-hpc, by inspecting live processes:
# DUPLICATE Runner.Listener processes under one registered runner identity, with
# GitHub reconciling the conflict by killing the other session's in-flight job
# (SIGTERM → 143). That explains a MID-RUN kill; this pkill only fires at startup
# and could not have. Fixed on their side.
#
# So this change is HAZARD REMOVAL, not a root-cause fix. It stays because the
# hazard is real and the justifying assumption is provably dead.
#
# THE AGE GUARD WAS NOT ENOUGH, AND THE REASON IS INSTRUCTIVE (2026-08-15).
# The argument above — "a leftover is minutes-to-hours old, a concurrent sibling is
# seconds old, so `--older` separates them" — is CORRECT about the population it
# considered: CI jobs. It is silent about the population it did not: LIVE FLEET
# AGENTS on the same machine. A `_tui_turn_bridge` is long-lived by construction,
# so it is ALWAYS older than any age floor. The filter keys on exactly the property
# agents always have, which makes it structurally unable to exclude them.
#
# MEASURED on scitex-compute-04, from the HOST (an in-container pgrep sees a
# different PID namespace and reports 0 — that near-miss nearly refuted this):
#     pgrep --older 600 -f 'python.* -m scitex_agent_container'  ->  11 PIDs,
#         every one a live agent turn bridge, two peers mid-turn among them
#     Runner.Listener processes on the same host              ->  2
# This script is shared by SEVEN workflows (pytest-matrix, lint, import-smoke,
# newb-docs, spartan-canary, autobump-sweep, publish), so it fired on ordinary PR
# CI, not just releases.
#
# THE FIX IS SCOPE, NOT TIMING. An age floor is a PROXY for "is this process
# mine"; a runner workspace path is the thing itself. We now kill only processes
# whose CWD lives under a runner `_work` tree — which a fleet agent's never does,
# at any age. The age floor is KEPT as a second, independent condition so a
# genuinely concurrent sibling stays protected even inside the workspace.
#
# ACCEPTED TRADE-OFF, stated rather than hidden: a leftover whose cwd is gone or
# has moved outside `_work` will no longer be reaped. That is UNDER-reaping, and
# it is the right direction — a leaked a2a port costs a retry, a SIGTERMed agent
# costs an operator's morning. If port exhaustion resurfaces, fix it with a
# port-scoped cleanup, never by widening this kill.
_REAP_MIN_AGE_S=600
_reap_scoped() {
    # Kill matching processes ONLY when their cwd is inside a runner workspace.
    # Reads /proc/<pid>/cwd, so it cannot be fooled by argv.
    local pat="$1" pid cwd
    while read -r pid; do
        [ -n "$pid" ] || continue
        cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null)" || continue
        case "$cwd" in
            */actions-runner*/_work/*) kill "$pid" 2>/dev/null || true ;;
        esac
    done < <(pgrep --older "$_REAP_MIN_AGE_S" -f "$pat" 2>/dev/null || true)
}
if [ -z "${GITHUB_ACTIONS:-}" ]; then
    # Never reap outside CI. Running this script by hand on a dev box must not be
    # able to kill anything, whatever else is true.
    :
elif pkill --help 2>&1 | grep -q -- '--older'; then
    _reap_scoped 'python.* -m scitex_agent_container'
    _reap_scoped 'apptainer.*ci-cpu'
else
    # procps too old for --older. Reaping leftovers is a nice-to-have; killing the
    # sibling matrix legs is not. Skip LOUDLY rather than shoot blind — a silently
    # skipped cleanup costs a stale port, an unscoped pkill costs a release.
    echo "::warning::pkill --older unsupported here; skipping the leftover reap." \
         "An unscoped pkill would SIGTERM the sibling matrix legs (see run 29284554656)."
fi

# --- scitex-agent-container-specific (2/2): prune leaked in-SIF scratch -------
# THE DISK-SIDE SIBLING OF THE REAP ABOVE, and the same reasoning: age is what
# separates a leftover from a live concurrent sibling.
#
# MEASURED 2026-08-09 on scitex-04-cpu-01: the three in-SIF scripts each created
# a per-run scratch under /tmp and NOTHING ever removed it. 116 survivors at
# 1.8-2.2 GB each put /tmp at 270 GB of a 393 GB root — root 100% FULL (39 MB
# free), inodes 92% — on a box hosting twelve fleet agents. A leaked temp dir is
# free on a hosted runner, where the VM is discarded; on a persistent one it is
# a slow outage.
#
# Here, host-side, because this is the ONE place all five in-SIF call sites pass
# through, it still runs when the SIF never starts, and /tmp is shared with the
# container anyway (`mount tmp = yes`, no --contain below). This sweep is only
# the backstop for SIGKILL/reboot; the normal ending is the `if: always()`
# clean-tmpdir.sh step in each job. Guards (self-exclusion by run identity, 24 h
# age floor) and the /scratch decision are argued in tmpdir-lib.sh.
# shellcheck source=/dev/null
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/tmpdir-lib.sh"
ci_tmpdir_prune
# --- end scitex-agent-container-specific -------------------------------------

# Build the argv as an ARRAY so the GPFS bind can be dropped cleanly rather than
# passed as an empty string. --pwd "$PWD" keeps the checkout as cwd.
APPTAINER_ARGV=(exec --pwd "$PWD")
if [ -d "$GPFS_PROJECT" ]; then
    APPTAINER_ARGV+=(--bind "$GPFS_PROJECT")
    GPFS_STATE="present (scratch on GPFS, punim0264 bound)"
else
    GPFS_STATE="absent (scratch under \$HOME, no GPFS bind)"
fi

# Echo the resolved plan: when a run fails on an unfamiliar node, the FIRST
# question is which of the two profiles it took.
echo "exec-in-sif: apptainer=$APPTAINER (via $APPTAINER_FROM)"
echo "exec-in-sif: sif=$SIF"
echo "exec-in-sif: $GPFS_PROJECT $GPFS_STATE"
echo "exec-in-sif: APPTAINER_TMPDIR=$APPTAINER_TMPDIR"
echo "exec-in-sif: + $APPTAINER ${APPTAINER_ARGV[*]} $SIF bash .github/ci/$INNER $*"

exec "$APPTAINER" "${APPTAINER_ARGV[@]}" "$SIF" bash ".github/ci/$INNER" "$@"

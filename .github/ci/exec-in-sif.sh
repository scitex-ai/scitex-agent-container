#!/usr/bin/env bash
# Outer apptainer-exec wrapper for scitex-tex's self-hosted (Spartan) CI.
#
# Runs ON THE RUNNER (outside the SIF). Resolves the apptainer shim + SIF image
# from the repo Actions Variables, then `apptainer exec`s the SIF and hands off
# to an INNER script (run inside the container). Keeps every workflow job's YAML
# down to one line — `bash .github/ci/exec-in-sif.sh <inner-script> [args...]` —
# and concentrates all the HPC/SIF plumbing (shim PATH, ~-expansion, scratch,
# binds) in one version-controlled place.
#
# Required env (set by the workflow from repo Actions Variables):
#   SCITEX_CI_APPTAINER   path to the apptainer shim   (e.g. ~/.env-3.11/bin/apptainer)
#   SCITEX_CI_SIF         path to the CI SIF image     (e.g. ~/.scitex/dev/containers/ci-cpu.sif)
#
# Usage:
#   bash .github/ci/exec-in-sif.sh run-in-sif.sh 3.12
#
# Fail-loud (operator directive): a missing shim or SIF is a HARD error — never
# a silent fallback to a bare-runner install.
set -euo pipefail

INNER="${1:?inner script name required (relative to .github/ci/)}"
shift || true

# The runner's job shell is --noprofile --norc (no Lmod), so the apptainer shim
# must be put on PATH explicitly; it execs the real Apptainer binary directly.
# ~-expand the Actions-Variable paths: a quoted "~/…" is NOT tilde-expanded by
# the shell, so substitute a leading ~ with $HOME ourselves.
APPTAINER="${SCITEX_CI_APPTAINER:?SCITEX_CI_APPTAINER not set (repo Actions Variable)}"
SIF="${SCITEX_CI_SIF:?SCITEX_CI_SIF not set (repo Actions Variable)}"
APPTAINER="${APPTAINER/#\~/$HOME}"
SIF="${SIF/#\~/$HOME}"
export PATH="$HOME/.env-3.11/bin:$PATH"

# The Actions Variable names ONE host's apptainer (~/.env-3.11/bin/apptainer is
# the Spartan shim); on a runner that ships apptainer in /usr/bin the same
# variable points at nothing. Fall back to whatever is on PATH.
#
# This does NOT weaken the fail-loud rule in the header. That rule forbids
# falling back to a BARE-RUNNER install — running the suite outside the SIF, in
# a different environment from the release gate, which is the drift the whole
# script exists to prevent. Locating the apptainer BINARY elsewhere still execs
# the same SIF. No apptainer at all is still a hard error.
if [ ! -x "$APPTAINER" ]; then
    _found="$(command -v apptainer 2>/dev/null || true)"
    [ -n "$_found" ] && APPTAINER="$_found"
fi
[ -x "$APPTAINER" ] || {
    echo "::error::apptainer not executable at '$APPTAINER' and not on PATH." \
         "Set the SCITEX_CI_APPTAINER Actions Variable to this runner's path."
    exit 1
}
[ -f "$SIF" ] || {
    echo "::error::CI SIF missing at $SIF — rebuild it: scitex-container apptainer build ci-cpu"
    exit 1
}

# apptainer scratch. The GPFS path is Spartan's project filesystem and does not
# exist on our own machines, where `mkdir -p` under `set -e` would abort the job
# before a single test ran. Use it when it is there, a node-local scratch when
# it is not.
_SPARTAN_PROJ="/data/gpfs/projects/punim0264"
if [ -d "$_SPARTAN_PROJ" ]; then
    export APPTAINER_TMPDIR="$_SPARTAN_PROJ/ywatanabe/ci/apptainer-tmp"
else
    export APPTAINER_TMPDIR="${TMPDIR:-/tmp}/apptainer-tmp-${USER:-ci}"
fi
mkdir -p "$APPTAINER_TMPDIR"

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
# THE AGE GUARD IS THE FIX: it puts the word this comment always used — LEFTOVER —
# into the MECHANISM instead of into an assumption the world can quietly
# invalidate underneath it. A leftover from a prior run is minutes-to-hours old; a
# concurrent sibling is seconds old. `--older` separates them no matter how many
# runners share the node, which is exactly the property "serialised" did not have.
_REAP_MIN_AGE_S=600
if pkill --help 2>&1 | grep -q -- '--older'; then
    pkill --older "$_REAP_MIN_AGE_S" -f 'python.* -m scitex_agent_container' 2>/dev/null || true
    pkill --older "$_REAP_MIN_AGE_S" -f 'apptainer.*ci-cpu'                   2>/dev/null || true
else
    # procps too old for --older. Reaping leftovers is a nice-to-have; killing the
    # sibling matrix legs is not. Skip LOUDLY rather than shoot blind — a silently
    # skipped cleanup costs a stale port, an unscoped pkill costs a release.
    echo "::warning::pkill --older unsupported here; skipping the leftover reap." \
         "An unscoped pkill would SIGTERM the sibling matrix legs (see run 29284554656)."
fi

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

# --bind punim0264: on Spartan $HOME/.scitex is a symlink into punim0264, so the
# bind is what makes that symlink resolve inside the container. Elsewhere the
# path does not exist and apptainer refuses to start with it ("bind path does
# not exist"), so bind it only where it is real. --pwd "$PWD" keeps the checkout
# as cwd.
_BINDS=()
[ -d "$_SPARTAN_PROJ" ] && _BINDS=(--bind "$_SPARTAN_PROJ")
exec "$APPTAINER" exec --pwd "$PWD" "${_BINDS[@]+"${_BINDS[@]}"}" \
    "$SIF" bash ".github/ci/$INNER" "$@"

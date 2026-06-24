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

[ -x "$APPTAINER" ] || {
    echo "::error::apptainer shim not executable at $APPTAINER"
    exit 1
}
[ -f "$SIF" ] || {
    echo "::error::CI SIF missing at $SIF — rebuild it: scitex-container apptainer build ci-cpu"
    exit 1
}

# apptainer scratch on the shared FS — keeps HOME clean.
export APPTAINER_TMPDIR="/data/gpfs/projects/punim0264/ywatanabe/ci/apptainer-tmp"
mkdir -p "$APPTAINER_TMPDIR"

# Reap leaked CI processes from PRIOR runs on this persistent self-hosted node.
# Several tests spawn DETACHED `sac agents`/`sac listen` background processes
# (`python -m scitex_agent_container ...`); a failed/killed run leaves them
# running and holding a2a ports [19000-19999], so claims accumulate until the
# allocator can't find a free port (test__start_force_clears_session went red on
# the release node after repeated retries). Runs here are serialised (one job at
# a time) and the operator's LIVE agents run on a DIFFERENT host, so killing
# leftover CI sac/ci-cpu processes before this run starts is safe + makes the
# node self-healing instead of needing a manual clean.
pkill -f 'python.* -m scitex_agent_container' 2>/dev/null || true
pkill -f 'apptainer.*ci-cpu' 2>/dev/null || true

# --bind punim0264: $HOME/.scitex is a symlink into punim0264; bind it so the
# symlink resolves inside the container. --pwd "$PWD" keeps the checkout as cwd.
exec "$APPTAINER" exec --pwd "$PWD" --bind /data/gpfs/projects/punim0264 \
    "$SIF" bash ".github/ci/$INNER" "$@"

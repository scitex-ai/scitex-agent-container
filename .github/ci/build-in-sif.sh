#!/usr/bin/env bash
# Runs INSIDE the reused scitex-ci SIF (apptainer exec — invoked via
# exec-in-sif.sh). Builds scitex-agent-container's wheel + sdist into ./dist/.
#
# WHY build in the SIF: the self-hosted Spartan runner has no Python on the
# bare node (the whole reason the old `actions/setup-python@v5` step failed:
# "version 3.x not found for this OS"). The SIF bakes python 3.11/3.12/3.13 +
# pip + uv at /opt/venv-<ver>, exactly like the working pytest-matrix CI.
#
# `python -m build` needs the `build` frontend, which is NOT baked in the SIF
# (only scitex-dev[all,dev] deps are). Mirror run-in-sif.sh: install `build`
# into a writable --target on node-local /tmp and put it on PYTHONPATH. The
# SIF's /opt/venv-* are root-owned + RO and the compute-node HOME is RO inside
# the container, so a normal install fails Permission denied — a --target on
# writable scratch sidesteps both.
#
# Fail-loud (operator directive): a missing interpreter or a failed build is a
# HARD error, never a silent fallback.
set -euo pipefail

V="${1:-3.12}"
VENV="/opt/venv-$V"
PY="$VENV/bin/python"
test -x "$PY" || {
    echo "::error::baked python missing in $VENV — rebuild the SIF: scitex-container apptainer build ci-cpu"
    exit 1
}

export LC_ALL=C.UTF-8 LANG=C.UTF-8

# Writable scratch (the runner's TMPDIR=~/.cache/tmp is a host path that does
# NOT resolve inside the container). Node-local /tmp is writable.
#
# It was NOT "ephemeral", whatever this comment used to say: nothing removed
# this directory, so every release leaked one. Smaller and rarer than
# run-in-sif.sh's, therefore slower to notice — not less of a leak. Lifecycle
# (naming, end-of-job removal, startup prune) now lives in tmpdir-lib.sh.
# shellcheck source=/dev/null
. "$(dirname "${BASH_SOURCE[0]}")/tmpdir-lib.sh"
TMPDIR="$(ci_tmpdir_path build "$V")"
export TMPDIR
# `${TMPDIR:?}` — see run-in-sif.sh for the measurement. Short version: `rm -rf ""`
# exits 0 SILENTLY on GNU coreutils (`-f` swallows the empty operand), so an empty
# name here would delete nothing, fail nothing, and leave the rest of the script
# addressing paths off the filesystem root. `:?` aborts instead.
rm -rf "${TMPDIR:?build scratch path came back empty — refusing to rm -rf it}"
mkdir -p "$TMPDIR/site" "$TMPDIR/uv-cache"

# The compute-node $HOME is RO inside the container — point every cache the
# installer might touch at the writable scratch (else uv/pip die creating
# ~/.cache).
export UV_CACHE_DIR="$TMPDIR/uv-cache"
export XDG_CACHE_HOME="$TMPDIR"
export PIP_CACHE_DIR="$TMPDIR/pip-cache"

# A VIRTUAL_ENV leaked from the runner profile (~/.env-3.11) is a broken
# symlink in here; unset it so no tool follows it.
unset VIRTUAL_ENV || true

export PATH="$VENV/bin:$PATH"
echo "build: py=$("$PY" -V) target=$TMPDIR/site"

# Install the PEP 517 build frontend into the writable target (uv fast path,
# pip safety net), then build with it. Clean dist/ first so only the freshly
# built artifacts are uploaded.
uv pip install --python "$PY" --target="$TMPDIR/site" build ||
    "$PY" -m pip install --target="$TMPDIR/site" build

export PYTHONPATH="$TMPDIR/site${PYTHONPATH:+:$PYTHONPATH}"

rm -rf dist
"$PY" -m build --outdir dist

echo "=== built artifacts ==="
ls -l dist
# fail-loud: refuse to continue the pipeline with an empty dist/.
test -n "$(ls -A dist 2>/dev/null)" || {
    echo "::error::python -m build produced no artifacts in dist/"
    exit 1
}

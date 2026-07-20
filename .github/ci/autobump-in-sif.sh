#!/usr/bin/env bash
# Inner script for autobump-release-sweep.yaml — runs INSIDE the ci-cpu SIF.
#
# The bare Spartan node has NO python (that is the whole reason the SIF exists);
# autobump.py is pure stdlib, so ANY python3 in the SIF runs it. Invoked as:
#
#   bash .github/ci/exec-in-sif.sh autobump-in-sif.sh <autobump-subcommand> [args...]
#
# e.g.  ... autobump-in-sif.sh bump --date 2026-07-21
#       ... autobump-in-sif.sh verify --version 0.24.2   (exit 3 == inconsistent)
#
# It only reads the subcommand's EXIT CODE and, for `bump`, the file mutations
# in the checkout ($PWD is bound into the SIF by exec-in-sif.sh). autobump.py's
# stdout is not captured through the SIF — the caller recomputes the version in
# plain shell and cross-checks it with `verify`.
set -euo pipefail

PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
    for cand in /opt/venv-3.12/bin/python /opt/venv-3.11/bin/python /opt/venv-3.13/bin/python; do
        [ -x "$cand" ] && PY="$cand" && break
    done
fi
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
    echo "::error::no python3 available inside the CI SIF for autobump" >&2
    exit 1
fi

exec "$PY" .github/ci/autobump.py "$@"

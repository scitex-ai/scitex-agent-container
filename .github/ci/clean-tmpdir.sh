#!/usr/bin/env bash
# Remove THIS job's per-run scratch. Runs ON THE RUNNER (outside the SIF) as an
# `if: always()` step that MIRRORS THE WORK STEP'S ARGUMENTS EXACTLY:
#
#   - run: bash .github/ci/exec-in-sif.sh  run-in-sif.sh ${{ matrix.python-version }}
#   - if: always()
#     run: bash .github/ci/clean-tmpdir.sh run-in-sif.sh ${{ matrix.python-version }}
#
# The same two args on purpose: the pairing is then mechanically checkable, and
# tests/integration/test_ci_tmpdir_lifecycle.py checks it — so a new in-SIF job
# that forgets its cleanup step fails CI instead of leaking ~2 GB per run.
#
# WHY A JOB STEP AND NOT A TRAP: both script layers end in `exec`, which
# discards traps, and the `exec`s are load-bearing for signal/exit-code
# propagation. An `always()` step is a SEPARATE process the runner starts after
# the work step is torn down, so it covers SUCCESS, FAILURE and CANCELLATION
# without racing the signals that killed the work step. See tmpdir-lib.sh.
#
# SCOPED TO THIS MATRIX LEG, and that is not cosmetic. All three legs share
# GITHUB_RUN_ID/GITHUB_RUN_ATTEMPT, so a cleanup keyed on the run alone would
# delete the LIVE scratch of the two siblings still running. The python-version
# arg is what makes the target this leg's own directory and nothing else.
#
# Host /tmp IS container /tmp here (apptainer.conf `mount tmp = yes`, and
# exec-in-sif.sh passes no --contain), so removing the path from the runner
# removes the directory the in-SIF script created.
#
# NEVER FAILS THE JOB — no `set -e`, and it always exits 0. A cleanup step that
# turns a green run red, or that replaces the real failure of a job it was
# `always()`-attached to, is worse than the leak it fixes.
set -uo pipefail

_CI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
. "$_CI_DIR/tmpdir-lib.sh"

# A partial checkout leaves the library unsourced, and every helper below then
# resolves to "command not found" — which fell through to the "creates no
# per-run scratch" message, i.e. told a future reader the OPPOSITE of the truth
# while a directory leaked. Say what actually happened; still never fail the job.
if ! command -v ci_tmpdir_prefix_for_inner >/dev/null 2>&1; then
    echo "::warning::clean-tmpdir: could not load ${_CI_DIR}/tmpdir-lib.sh —" \
        "no scratch was removed. The next run's startup prune will reclaim it."
    exit 0
fi

INNER="${1:-}"
VERSION="${2:-}"

PREFIX="$(ci_tmpdir_prefix_for_inner "$INNER")"
if [ -z "$PREFIX" ]; then
    echo "clean-tmpdir: '${INNER:-<none>}' creates no per-run scratch — nothing to remove."
    exit 0
fi

# Without a version we cannot scope the removal to this leg, and an unscoped
# removal would take out the sibling legs still running. Refuse LOUDLY but
# harmlessly: the next run's prune reclaims it, whereas a wrong deletion here
# reds a release.
if [ -z "$VERSION" ]; then
    echo "::warning::clean-tmpdir: no version arg for '$INNER' — cannot scope the removal" \
        "to this matrix leg, so skipping. The startup prune will reclaim it."
    exit 0
fi

TARGET="$(ci_tmpdir_path "$PREFIX" "$VERSION")"

# Decide MANAGED-NESS BEFORE announcing or measuring anything. A malformed
# version arg can compose a path that ci_tmpdir_cleanup will (correctly) refuse,
# and announcing first printed `clean-tmpdir: removing /tmp (270G)` on the
# incident host — a line that reads like an imminent disaster and spends the
# 30 s `du` bound to say nothing.
if ! _ci_tmpdir_is_managed "$TARGET"; then
    echo "::error::clean-tmpdir: refusing to remove '$TARGET' — not a managed CI scratch path" >&2
    exit 0
fi

if [ -d "$TARGET" ]; then
    # Size is the evidence that this fix reclaims what the incident measured.
    # Bounded: a cold 100k-file tree walk must not stretch the job's teardown.
    SIZE=""
    if command -v timeout >/dev/null 2>&1; then
        SIZE="$(timeout 30 du -sh -- "$TARGET" 2>/dev/null | cut -f1)"
    fi
    echo "clean-tmpdir: removing ${TARGET}${SIZE:+ (${SIZE})}"
    if ! ci_tmpdir_cleanup "$TARGET"; then
        echo "::warning::clean-tmpdir: could not remove ${TARGET}"
    fi
else
    echo "clean-tmpdir: ${TARGET} already gone — nothing to do."
fi

exit 0

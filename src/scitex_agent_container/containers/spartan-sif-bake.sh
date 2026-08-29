#!/usr/bin/env bash
# spartan-sif-bake.sh — the SPARTAN-SIDE leg of the periodic SIF bake.
#
# OPERATOR DIRECTIVE (2026-07-17, verbatim): 「sif は最新版を定期焼きにしましょう。
# spartan 側で。それでこちらには定期的に rsync する形で。」 — bake fresh SIFs on
# Spartan, rsync them to the master; the master's CPU is never spent baking.
#
# This script is SHIPPED IN THE WHEEL (next to the .def recipes) and is
# executed by `sac image bake-remote` by PIPING it over ssh:
#
#     ssh <host> 'bash -l -s -- --layer base ...' < spartan-sif-bake.sh
#
# so the version that runs is ALWAYS the one the installed sac ships —
# nothing is deployed to, or trusted from, the remote host. No tokens, no
# agent specs, no credentials ever land on Spartan (operator constraint
# 「スパルタンに定義は一切置かない」): the build source is a dedicated clone of
# the PUBLIC repo over https.
#
# HARD RULES this script encodes (fleet doctrine, measured 2026-07-17):
#   * The standing CPU lease is resolved BY NAME (--lease-name), NEVER by
#     job id — the id changes at every ~7d auto-resubmit boundary.
#   * Work is launched as STEPS into that lease (`srun --jobid=<id>
#     --overlap`). This script NEVER calls sbatch (queue tax: a fresh job
#     sat PENDING for 32h) and NEVER runs the build on the login node
#     (login-node guard kills it; account sanctions).
#   * The dedicated clone lives under --workdir and is used ONLY for
#     baking. Spartan's existing sac checkout is the CI runners' audit
#     workspace — this script must never touch it.
#   * Every step is THREE-STATE and loud: quota-unknown is a FAILURE, not
#     quota-ok; a gate that did not run is a FAILURE, not gate-passed.
#     The final line of output is a single machine-readable verdict:
#
#         SAC_BAKE_RESULT={"verdict":"BAKED|SKIPPED|FAILED", ...}
#
#     A missing SAC_BAKE_RESULT line means the script DIED — the caller
#     must treat that as its own state (FAILED-NO-RESULT), never as ok.
#   * The build-time freshness gate lives in the .def itself (%post
#     symbol gate — a stale bake DIES at build time). This script adds an
#     ARTIFACT probe on top: `apptainer exec <sif> python <symbol probe>`
#     against the produced file, because the artifact, not the build rc,
#     is what ships (2026-07-17 01:23Z: a bake returned SUCCESS with the
#     wrong contents).
#   * keep-N rotation prunes ONLY older timestamped SIFs, never the live
#     symlink target, and logs every pruned name (no silent deletion).
#
# STDIN RULE — EVERY srun IN THIS FILE MUST CARRY `--input=none` AND
# `< /dev/null`. This script is DELIVERED ON STDIN (`bash -l -s --` over
# ssh, above), so bash is reading the script text from fd 0 — a
# non-seekable pipe holding the UNREAD REMAINDER OF THIS FILE. srun
# forwards its own stdin to the launched task by default, so an unguarded
# srun READS THE REST OF THIS SCRIPT and hands it to the compute node.
# Bash then finds EOF where the next line should be and exits **0**: no
# error, no signal, no FATAL, no SAC_BAKE_RESULT. Five consecutive bakes
# (2026-07-17..19) built a SIF and died exactly this way, leaving five
# .partial files and zero published images; an earlier reading blamed an
# idle-connection drop and added ssh keepalives, which cannot help — the
# connection was healthy and ssh exited 0 every time. Measured A/B on the
# lease, single variable: without --input=none every line after the srun
# vanished; with it, all of them came back. Do not remove either guard,
# and never redirect fd 0 for the SHELL (`exec 0</dev/null` would discard
# the rest of this file) — guard each srun instead.
set -uo pipefail

# ---------------------------------------------------------------------------
# args
# ---------------------------------------------------------------------------
LAYER=""
WORKDIR="/data/gpfs/projects/punim2354/ywatanabe/sac-sif-bake"
LEASE_NAME="spartan-cpu-32-cores-64-ram"
REPO_URL="https://github.com/ywatanabe1989/scitex-agent-container.git"
BRANCH="develop"
RETAIN=3
MIN_FREE_GB=40
MIN_FREE_INODES=100000
CPUS=8
FORCE=0
MODULES="slurm/default GCCcore/11.3.0 Apptainer/1.3.3"

while [ $# -gt 0 ]; do
    case "$1" in
        --layer) LAYER="$2"; shift 2 ;;
        --workdir) WORKDIR="$2"; shift 2 ;;
        --lease-name) LEASE_NAME="$2"; shift 2 ;;
        --repo-url) REPO_URL="$2"; shift 2 ;;
        --branch) BRANCH="$2"; shift 2 ;;
        --retain) RETAIN="$2"; shift 2 ;;
        --min-free-gb) MIN_FREE_GB="$2"; shift 2 ;;
        --cpus) CPUS="$2"; shift 2 ;;
        --force) FORCE=1; shift ;;
        --modules) MODULES="$2"; shift 2 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

STEP="args"
START_EPOCH=$(date +%s)

fail() {
    # fail <reason> [detail...] — emit the FAILED verdict and die non-zero.
    local reason="$1"; shift || true
    echo "FATAL[$STEP]: $reason $*" >&2
    printf 'SAC_BAKE_RESULT={"verdict":"FAILED","layer":"%s","step":"%s","reason":"%s"}\n' \
        "${LAYER:-unset}" "$STEP" "$reason"
    exit 1
}

case "$LAYER" in
    base|scitex) : ;;
    *) fail "bad-layer" "--layer must be base|scitex, got '${LAYER}'" ;;
esac

# ---------------------------------------------------------------------------
# toolchain: modules, absolute binary paths (a chpwd hook — direnv — can
# rewrite PATH on cd, so every binary is captured absolute immediately)
# ---------------------------------------------------------------------------
STEP="modules"
for m in $MODULES; do
    module load "$m" 2>/dev/null || fail "module-load" "$m"
done
SQUEUE="$(command -v squeue)" || fail "squeue-missing"
SRUN="$(command -v srun)" || fail "srun-missing"
APPTAINER="$(command -v apptainer)" || fail "apptainer-missing"
GIT="$(command -v git)" || fail "git-missing"

# ---------------------------------------------------------------------------
# workdir + single-flight lock
# ---------------------------------------------------------------------------
STEP="workdir"
mkdir -p "$WORKDIR"/{store,state,build-context,apptainer-cache,logs} \
    || fail "workdir-create" "$WORKDIR"
STORE="$WORKDIR/store"
exec 9>"$WORKDIR/state/bake-$LAYER.lock"
flock -n 9 || fail "already-running" "another bake of layer=$LAYER holds the lock"

# ---------------------------------------------------------------------------
# lease: resolve BY NAME, require RUNNING. Never sbatch.
# ---------------------------------------------------------------------------
STEP="lease"
JID="$("$SQUEUE" --me --name="$LEASE_NAME" --states=RUNNING -h -o %i | head -1)"
[ -n "$JID" ] || fail "lease-not-running" "no RUNNING job named $LEASE_NAME"
echo "lease: $LEASE_NAME -> job $JID"

# ---------------------------------------------------------------------------
# quota gate: unknown headroom is a FAILURE, not a pass.
# ---------------------------------------------------------------------------
STEP="quota"
AVAIL_KB="$(df -P "$WORKDIR" | awk 'NR==2{print $4}')"
[ -n "$AVAIL_KB" ] || fail "quota-unknown" "df gave no availability for $WORKDIR"
AVAIL_GB=$((AVAIL_KB / 1024 / 1024))
[ "$AVAIL_GB" -ge "$MIN_FREE_GB" ] \
    || fail "quota-low" "${AVAIL_GB}GB free < ${MIN_FREE_GB}GB required"
AVAIL_INODES="$(df -iP "$WORKDIR" | awk 'NR==2{print $4}')"
[ -n "$AVAIL_INODES" ] || fail "quota-unknown" "df -i gave no inode count"
[ "$AVAIL_INODES" -ge "$MIN_FREE_INODES" ] \
    || fail "quota-low-inodes" "${AVAIL_INODES} inodes free < ${MIN_FREE_INODES}"
echo "quota: ${AVAIL_GB}GB / ${AVAIL_INODES} inodes free (ok)"

# ---------------------------------------------------------------------------
# dedicated bake clone (never the CI runners' checkout)
# ---------------------------------------------------------------------------
STEP="clone"
REPO="$WORKDIR/repo"
if [ ! -d "$REPO/.git" ]; then
    "$GIT" clone --branch "$BRANCH" "$REPO_URL" "$REPO" || fail "git-clone"
fi
"$GIT" -C "$REPO" fetch origin "$BRANCH" || fail "git-fetch"
"$GIT" -C "$REPO" checkout -B "$BRANCH" "origin/$BRANCH" -- || fail "git-checkout"
"$GIT" -C "$REPO" reset --hard "origin/$BRANCH" || fail "git-reset"
"$GIT" -C "$REPO" clean -fdx >/dev/null || fail "git-clean"
HEAD_SHA="$("$GIT" -C "$REPO" rev-parse HEAD)" || fail "git-rev-parse"
echo "clone: $REPO at $HEAD_SHA (origin/$BRANCH)"

# ---------------------------------------------------------------------------
# skip-if-unchanged: same source (and, for scitex, same base image) as the
# last SUCCESSFUL bake, and that artifact still exists => SKIPPED (loudly).
# ---------------------------------------------------------------------------
STEP="skip-check"
STATE_FILE="$WORKDIR/state/$LAYER.last"
BASE_LIVE=""
if [ "$LAYER" = "scitex" ]; then
    BASE_LIVE="$(readlink -f "$STORE/sac-base.sif" 2>/dev/null || true)"
    [ -n "$BASE_LIVE" ] && [ -f "$BASE_LIVE" ] \
        || fail "missing-base" "no live sac-base.sif in $STORE — bake base first"
fi
STATE_KEY="$HEAD_SHA:$(basename "${BASE_LIVE:-none}")"
if [ "$FORCE" -eq 0 ] && [ -f "$STATE_FILE" ]; then
    read -r LAST_KEY LAST_SIF < "$STATE_FILE" || true
    if [ "${LAST_KEY:-}" = "$STATE_KEY" ] && [ -f "${LAST_SIF:-/nonexistent}" ]; then
        echo "skip: source unchanged since last successful bake ($STATE_KEY)"
        printf 'SAC_BAKE_RESULT={"verdict":"SKIPPED","layer":"%s","head":"%s","sif":"%s","sha256":"%s","reason":"source-unchanged"}\n' \
            "$LAYER" "$HEAD_SHA" "$LAST_SIF" \
            "$(cat "${LAST_SIF}.sha256" 2>/dev/null | awk '{print $1}')"
        exit 0
    fi
fi

# ---------------------------------------------------------------------------
# stage the build context — the same shape sac's own CLI stages
# (cli_pkg/_image_source_build.py): the .def next to a pip-installable
# scitex-agent-container-src/ tree, plus the base SIF for layered defs.
# ---------------------------------------------------------------------------
STEP="stage"
CTX="$WORKDIR/build-context/$LAYER"
rm -rf "$CTX" && mkdir -p "$CTX/scitex-agent-container-src/src" || fail "stage-mkdir"
DEF_SRC="$REPO/src/scitex_agent_container/containers/apptainer-$LAYER.def"
[ -f "$DEF_SRC" ] || fail "def-missing" "$DEF_SRC"
cp -f "$DEF_SRC" "$CTX/" || fail "stage-def"
cp -f "$REPO/pyproject.toml" "$REPO/README.md" "$CTX/scitex-agent-container-src/" \
    || fail "stage-pyproject"
cp -f "$REPO/src/hatch_build.py" "$CTX/scitex-agent-container-src/src/" \
    || fail "stage-hatch-build"
cp -rf "$REPO/src/scitex_agent_container" "$CTX/scitex-agent-container-src/src/" \
    || fail "stage-package"
if [ "$LAYER" = "scitex" ]; then
    ln -s "$BASE_LIVE" "$CTX/sac-base.sif" || fail "stage-base-sif"
fi
echo "stage: $CTX"

# ---------------------------------------------------------------------------
# build — an srun STEP inside the lease, on the compute node.
# APPTAINER_TMPDIR on node-local /tmp (fast, big); the docker-layer cache
# persists on gpfs so repeat bakes don't re-pull ubuntu.
# ---------------------------------------------------------------------------
STEP="build"
TS="$(date +%Y-%m%d-%H%M%S)"
LAYER_DIR="$STORE/sac-$LAYER"
mkdir -p "$LAYER_DIR" || fail "store-mkdir"
FINAL_SIF="$LAYER_DIR/sac-$LAYER-$TS.sif"
PARTIAL_SIF="$FINAL_SIF.partial"
BUILD_LOG="$LAYER_DIR/sac-$LAYER.build-$TS.log"
echo "build: layer=$LAYER ts=$TS cpus=$CPUS (log: $BUILD_LOG)"
# --input=none IS LOAD-BEARING — see the STDIN RULE at the top of this file.
# Without it srun ate the rest of this script and five consecutive bakes
# (2026-07-17..19) built a SIF and then died right here, silently, at rc 0.
#
# The build output is TEE'd (remote store log AND the ssh channel) rather
# than redirected: a redirect leaves the channel silent for ~20 minutes, and
# the master's journal gets the full bake log as a bonus. pipefail makes the
# captured rc srun's, not tee's.
#
# ONE FILESYSTEM RULE — TMPDIR *AND* CACHEDIR ARE BOTH NODE-LOCAL.
# CACHEDIR used to point at "$WORKDIR/apptainer-cache", i.e. GPFS, while
# TMPDIR was node-local: ONE build straddling TWO filesystems with different
# consistency semantics. GPFS on this cluster has a documented read-after-
# write consistency race — the same root cause as the CI-runner _work/_temp
# incident, which produced "Missing file at path ..." and "Unknown system
# error -116" (ESTALE) with no clean error and no exit signal.
#
# 2026-07-19: two bakes died at DIFFERENT points (one after "Build complete",
# one mid-apt), each with no error, no signal and no SAC_BAKE_RESULT line.
# A deterministic control-flow bug dies in the SAME place every run; a
# filesystem race dies wherever it happens to be. Disk space was NOT the
# cause and is refuted with numbers: GPFS had 2.0T / 2.8M inodes free, node
# /tmp 3.3T of 3.5T free, /dev/shm 1008G free.
#
# THE TRADE, stated so nobody "optimises" it back: a node-local cache does
# NOT persist across nodes or lease re-allocations, so some bakes re-pull
# base layers. A cache miss costs minutes. A consistency race costs a
# silently unpublished image and a whole debugging session. Take the miss.
#
# BOTH PATHS SPELLED OUT IN FULL, sharing one parent — deliberately NOT a
# shell variable. A variable assigned on the line above would live OUTSIDE
# this block, and the stdin-guard harness extracts each srun invocation IN
# ISOLATION to prove it does not swallow the script tail. An unset variable
# there makes the block fail early for the wrong reason, so the control test
# can no longer tell a guarded block from an unguarded one.
# Keep every path this block needs INSIDE the block.
#
# The invariant a test pins: both paths share the SAME parent, and that
# parent is under node-local /tmp. Editing one without the other breaks it.
"$SRUN" --input=none --jobid="$JID" --overlap --ntasks=1 --cpus-per-task="$CPUS" \
    --job-name="sac-sif-bake-$LAYER" \
    --chdir="$CTX" \
    --export=ALL,APPTAINER_TMPDIR="/tmp/sac-sif-bake-$USER/tmp",APPTAINER_CACHEDIR="/tmp/sac-sif-bake-$USER/cache" \
    bash -c "mkdir -p /tmp/sac-sif-bake-$USER/tmp /tmp/sac-sif-bake-$USER/cache && exec \"$APPTAINER\" build --force \"$PARTIAL_SIF\" \"$CTX/apptainer-$LAYER.def\"" \
    < /dev/null 2>&1 | tee "$BUILD_LOG"
BUILD_RC=$?
[ "$BUILD_RC" -eq 0 ] || fail "apptainer-build" "rc=$BUILD_RC (log: $BUILD_LOG)"
[ -f "$PARTIAL_SIF" ] || fail "no-artifact" "build rc=0 but $PARTIAL_SIF missing"

# ---------------------------------------------------------------------------
# artifact symbol probe — the gate that runs against the FILE we ship.
# Same doctrine as the .def's %post gate: symbols, never version strings.
# ---------------------------------------------------------------------------
STEP="gate"
# The probe body below is a VERBATIM copy of containers/sif_symbol_probe.py
# (the master-side verify runs that file against the pulled SIF). A unit
# test asserts the two stay in lockstep — edit both together.
PROBE="$WORKDIR/state/probe.py"
cat > "$PROBE" <<'PYEOF'
"""Artifact gate: assert BY SYMBOL that this SIF is fresh and whole."""

import sys

# noqa placement is deliberate: this import LOOKS unused and is not. The
# probe is an artifact gate that asserts BY SYMBOL that the SIF shipped a
# whole scitex_cards, so the bare import IS the assertion — ruff F401 reads
# it as dead because nothing references the name, and removing it on that
# advice blinded the gate and reddened test_probe_imports_scitex_cards.
import scitex_cards  # noqa: F401  (the import itself is the check)
from scitex_cards._throughput import WIP_STATUSES

# scitex-cards 0.49.1: the comment-preserving mirror write, CORRECTED.
# Through 0.48.0, comment_task / update_task rebuilt a card from the doc the
# caller happened to hold and DROPPED every comment row that doc had not
# seen — a peer's comment written between your read and your write was
# destroyed silently, with a success report. 0.49.0 added this symbol to fix
# that and indexed its rows POSITIONALLY (row[0]), which is KeyError(0) on
# psycopg's dict_row, so on PostgreSQL every card holding comments became
# READ-ONLY: uncommentable, unupdatable, uncompletable, undeletable. 0.49.1
# reads row["author"]. THE FLOOR IS >=0.49.1 AND MUST NEVER BE >=0.49.0.
#
# THIS IMPORT PROVES PRESENCE, NOT BEHAVIOUR, and that distinction is the
# whole lesson of 2026-08-23: it passed on the broken 0.49.0, because the
# function was there and wrong. Measured that day, five independent gates
# went green on that artifact within one hour — this probe, the master-side
# SYMBOL_PROBE, the Spartan bake's content check, upstream's hasattr check,
# and a 7537-test suite that runs on SQLite where the defect cannot exist.
# So the FLOOR is what excludes the broken release; this import only catches
# a version string that lies; and only a post-deploy write to a card that
# ALREADY HAS a comment proves the path actually runs.
from scitex_cards._mirror_rows import _merge_unseen_comment_rows  # noqa: F401

# scitex-dev 0.56.6: the bounded (origin, seq) oplog-allocation retry.
# Through 0.56.5, Store._append read MAX(seq) ONCE and then inserted, so a
# burst of writers on a SINGLE node collided on the oplog (origin, seq)
# primary key with no bounded retry -- 7/8 and 5/8 failures on the two
# concurrency tests, reproduced three times. 0.56.6 adds this constant and
# the retry loop that uses it, plus an advisory lock around table creation.
#
# THIS IMPORT PROVES PRESENCE, NOT BEHAVIOUR -- the same narrow job as the
# scitex-cards import above, and the same 2026-08-23 lesson. The FLOOR is
# what excludes the releases without the retry; this import only catches a
# version string that lies; and only a concurrent multi-writer append after
# deploy proves the loop actually settles under contention.
#
# The name is PRIVATE -- underscore-prefixed and absent from __all__ -- so
# upstream may rename or inline it with no deprecation, and that would land
# here as a dead bake far from scitex-dev's repo. If this line is what broke
# the build, read scitex_dev/store/_store.py before suspecting the image.
from scitex_dev.store._store import _SEQ_ALLOCATION_ATTEMPTS  # noqa: F401

if "in_progress" not in WIP_STATUSES:
    print(f"FATAL: 'in_progress' missing from WIP_STATUSES: {sorted(WIP_STATUSES)}")
    sys.exit(1)

# Newer than any published sac release => proves the %files-staged source
# tree won the install (no transitive PyPI sac wheel overwrote it).
from scitex_agent_container.runtimes._apptainer_overlay import (
    ensure_overlay_dirs,  # noqa: F401,E402
)

print("OK: artifact symbol probe passed")
PYEOF
# --input=none: see the STDIN RULE at the top of this file. The probe is a
# FILE argument; this task has no use for stdin.
"$SRUN" --input=none --jobid="$JID" --overlap --ntasks=1 \
    --job-name="sac-sif-gate-$LAYER" \
    "$APPTAINER" exec --bind "$WORKDIR" "$PARTIAL_SIF" \
    /opt/venv-sac/bin/python "$PROBE" < /dev/null
GATE_RC=$?
[ "$GATE_RC" -eq 0 ] || fail "gate-failed" "rc=$GATE_RC — artifact is stale/wrong; NOT published"

# ---------------------------------------------------------------------------
# publish: checksum, atomic rename into the store, atomic latest-symlink
# flip, state update. The store never holds an ungated file under a final
# name, and the symlink never points at a partial.
# ---------------------------------------------------------------------------
STEP="publish"
# --input=none: see the STDIN RULE at the top of this file. sha256sum reads
# the FILE argument, never stdin.
SHA256="$("$SRUN" --input=none --jobid="$JID" --overlap --ntasks=1 \
    sha256sum "$PARTIAL_SIF" < /dev/null | awk '{print $1}')"
[ -n "$SHA256" ] || fail "sha256-unknown"
mv -f "$PARTIAL_SIF" "$FINAL_SIF" || fail "publish-rename"
echo "$SHA256  $(basename "$FINAL_SIF")" > "$FINAL_SIF.sha256" || fail "publish-sha-sidecar"
TMP_LINK="$STORE/.sac-$LAYER.sif.tmp.$$"
ln -s "sac-$LAYER/$(basename "$FINAL_SIF")" "$TMP_LINK" || fail "publish-symlink"
mv -Tf "$TMP_LINK" "$STORE/sac-$LAYER.sif" || fail "publish-symlink-swap"
echo "$STATE_KEY $FINAL_SIF" > "$STATE_FILE" || fail "publish-state"
echo "publish: $FINAL_SIF (sha256=$SHA256)"

# ---------------------------------------------------------------------------
# keep-N rotation — newest RETAIN stay; the live symlink target always
# stays; every pruned artifact is NAMED in the log.
# ---------------------------------------------------------------------------
STEP="rotate"
LIVE_TARGET="$(readlink -f "$STORE/sac-$LAYER.sif")"
PRUNED=""
KEPT=0
# `ls -1` sorts lexicographically and the YYYY-MMDD-HHMMSS stamp is
# lexicographically chronological, so `sort -r` = newest first.
for sif in $(ls -1 "$LAYER_DIR"/sac-"$LAYER"-*.sif 2>/dev/null | sort -r); do
    if [ "$(readlink -f "$sif")" = "$LIVE_TARGET" ] || [ "$KEPT" -lt "$RETAIN" ]; then
        KEPT=$((KEPT + 1))
        continue
    fi
    echo "rotate: pruning $(basename "$sif") (+ .sha256, build log)"
    rm -f "$sif" "$sif.sha256" \
        "$LAYER_DIR/sac-$LAYER.build-$(basename "$sif" .sif | sed "s/^sac-$LAYER-//").log"
    PRUNED="$PRUNED $(basename "$sif")"
done

DURATION=$(( $(date +%s) - START_EPOCH ))
printf 'SAC_BAKE_RESULT={"verdict":"BAKED","layer":"%s","ts":"%s","head":"%s","sif":"%s","sha256":"%s","pruned":"%s","duration_sec":%s}\n' \
    "$LAYER" "$TS" "$HEAD_SHA" "$FINAL_SIF" "$SHA256" "${PRUNED# }" "$DURATION"

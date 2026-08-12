#!/usr/bin/env bash
# Runs INSIDE the reused scitex-ci SIF (apptainer exec). $1 = python version.
#
# WHY a layered install (not the bare PYTHONPATH=src trick scitex-dev uses):
# the shared ci-cpu.sif bakes scitex-dev[all,dev] DEPS, NOT scitex-agent-container's —
# matplotlib / graphviz / seaborn / django / Pillow / networkx / playwright /
# pytesseract / scitex-app / scitex-ui are absent from the SIF. So we install
# THIS checkout + its [all,dev] extras (WITH dependency resolution) into a
# writable --target dir and prepend that on PYTHONPATH. The SIF still supplies
# the heavy shared base (pip/uv, the python interpreters, scitex-dev's deps),
# so only scitex-agent-container's own thin dep set is fetched per run.
#
# --target (not a plain `-e .`): the SIF's /opt/venv-* are root-owned + RO and
# the HPC compute-node HOME is RO inside the container, so a normal site install
# fails Permission denied. A writable target on node-local /tmp sidesteps both.
#
# Fail-loud: a missing interpreter or a failed install is a hard error.
set -euo pipefail

V="${1:?python version arg required (3.11/3.12/3.13)}"
VENV="/opt/venv-$V"
test -x "$VENV/bin/python" || {
    echo "::error::baked python missing in $VENV — rebuild the SIF: scitex-container apptainer build ci-cpu"
    exit 1
}

export LC_ALL=C.UTF-8 LANG=C.UTF-8

# NEVER SIGN COMMITS MADE BY THE TEST SUITE. Dozens of tests build throwaway
# git repos under tmp_path and commit into them (worktree GC, prune, drift,
# doctor, the git-identity hooks). They inherit the AMBIENT global config, and
# the operator's dotfiles set:
#
#     ~/.dotfiles/src/.gitconfig
#       commit.gpgsign  = true
#       tag.gpgsign     = true
#       gpg.format      = ssh
#       user.signingkey = ~/.ssh/id_ed25519_scitex.pub
#
# When that key is absent — as it is on scitex-compute-04 — every one of those
# commits dies, and git reports it like this:
#
#     error: Couldn't load public key .../id_ed25519_scitex.pub: No such file
#     fatal: failed to write commit object
#     exit 128
#
# THAT SECOND LINE IS THE TRAP. "failed to write commit object" reads as disk
# I/O, so the obvious diagnosis is a full filesystem or a broken runner. It
# cost most of an afternoon and FOUR wrong root causes (safe.directory, ENOSPC,
# a /tmp/pytest-of-<uid> collision, .gitconfig.lock contention) before anyone
# read the line above it. Reproduced exactly, and verified fixed, before this
# landed — see tests/integration/test_ci_commit_signing_disabled.py.
#
# WHY IT LOOKS INTERMITTENT AND IS NOT: the outcome is decided by WHICH RUNNER
# the job lands on. Only compute-04's ~/.gitconfig includes the dotfiles config;
# 01/02/03 carry that file on disk without including it. Measured 2026-08-12
# across both runner pools: 13/13 jobs on a compute-04 runner emit the error,
# 0/11 on 01/02/03. Two runs seconds apart on identical code therefore differ.
#
# This mirrors the same fix already on develop (#939); it is repeated here
# because the release path runs from main, which does not carry that commit.
#
# A test's scratch repo has no business carrying a signature. Signing stays ON
# for real commits; this scopes it off for CI only, and additively — two
# overrides on top of the ambient config rather than replacing it, so
# safe.directory and everything else the runner relies on survives.
export GIT_CONFIG_COUNT=2
export GIT_CONFIG_KEY_0=commit.gpgsign
export GIT_CONFIG_VALUE_0=false
export GIT_CONFIG_KEY_1=tag.gpgsign
export GIT_CONFIG_VALUE_1=false

# Real writable scratch. The runner profile exports TMPDIR=~/.cache/tmp, a host
# path that does NOT resolve inside the container; tests (tmp_path) and the
# install target both need a working, writable tmp. Node-local /tmp is writable
# + per-version-isolated so concurrent matrix legs don't collide.
#
# "+ ephemeral" USED TO BE CLAIMED HERE AND WAS NEVER TRUE. Nothing removed this
# directory — on the persistent self-hosted node 116 of them at 1.8-2.2 GB each
# filled the root filesystem (2026-08-09). tmpdir-lib.sh now owns the whole
# lifecycle: it names the directory (once, here), an `if: always()` job step
# removes it at the end of the job, and exec-in-sif.sh prunes what a SIGKILL or
# a reboot left behind. The name is unchanged, so this also reclaims the
# directories already on disk.
# shellcheck source=/dev/null
. "$(dirname "${BASH_SOURCE[0]}")/tmpdir-lib.sh"
TMPDIR="$(ci_tmpdir_path ci "$V")"
export TMPDIR
rm -rf "$TMPDIR"
mkdir -p "$TMPDIR/site" "$TMPDIR/uv-cache"

# REMOVE OUR OWN SCRATCH ON THE WAY OUT. The `rm -rf` above only ever deletes a
# RE-RUN of this exact (run_id, attempt, version) triple, because the name it
# cleans is the name it is about to use. Every new run gets a new GITHUB_RUN_ID
# and therefore a new directory, so nothing has ever removed the previous one.
#
# MEASURED 2026-08-09 on scitex-compute-04: 153 orphaned ci-* directories,
# 1.8-2.2G each, ~290G total — the root filesystem hit 393G/393G, 0 bytes free.
# Every writing test then failed with `fatal: failed to write commit object`, on
# EVERY pull request regardless of its diff, which reads as a runner fault and
# is not one. `sac listen` also began returning HTTP 500 (it could not write its
# audit log). One PR costs ~6G across the three matrix legs.
#
# The trap preserves the script's exit status (bash re-raises it after the
# handler), so a failing test suite still fails.
trap 'rm -rf "$TMPDIR"' EXIT

# ...and sweep SIBLINGS left behind by jobs that never reached the trap — a
# cancelled workflow, a SIGKILL, an OOM, or any run that predates this change.
# Without this the 153-directory backlog needs a human with sudo, which is how
# it reached 290G in the first place: the only cleanup path was one nobody ran.
#
# Age-gated rather than name-gated: a concurrent matrix leg on this same runner
# owns a sibling directory that is minutes old and MUST NOT be removed, while
# anything untouched for hours belongs to a job that is long gone. Mirrors the
# `_REAP_MIN_AGE_S` process reap in exec-in-sif.sh — same hazard, same guard.
_TMPDIR_REAP_MIN_AGE_MIN="${SCITEX_CI_TMPDIR_REAP_MIN_AGE_MIN:-360}"
find /tmp -maxdepth 1 -type d -name 'ci-scitex_agent_container-*' \
    -mmin "+$_TMPDIR_REAP_MIN_AGE_MIN" ! -path "$TMPDIR" \
    -exec rm -rf {} + 2>/dev/null || true

# The HPC compute-node $HOME is READ-ONLY inside the container, so uv/pip cannot
# create their default caches under ~/.cache — point them at the writable
# scratch instead (else `uv pip install` dies: "failed to create directory
# ~/.cache/uv: File exists / read-only").
export UV_CACHE_DIR="$TMPDIR/uv-cache"
export XDG_CACHE_HOME="$TMPDIR"
export PIP_CACHE_DIR="$TMPDIR/pip-cache"

# Headless matplotlib — no DISPLAY on the compute node; force the Agg backend so
# pyplot imports + figure rendering in the test suite never try to open a GUI.
export MPLBACKEND=Agg

# Dedicated, stable matplotlib config/cache dir for this matrix leg. Without
# pinning it, MPLCONFIGDIR defaults to $XDG_CACHE_HOME/matplotlib which is COLD
# every CI run; the xdist workers (one per core, see below) then each cold-start
# matplotlib and RACE to build fontList.json in that shared dir.
# A partial/contended cache makes some renders fall back to a different font, so
# scitex-agent-container's reproducibility tests (validate_recipe renders the SAME recipe
# twice and compares) see render1 != render2 → spurious MSE-over-threshold
# failures (e.g. TestValidateRecipe, max channel diff 255). One stable dir +
# a single warm-up below (build the cache ONCE, pre-fork) removes the race.
export MPLCONFIGDIR="$TMPDIR/mpl"
mkdir -p "$MPLCONFIGDIR"

# A VIRTUAL_ENV leaked from the runner profile (~/.env-3.11) is a broken symlink
# in here; unset it so no tool (uv, pip) tries to follow it.
unset VIRTUAL_ENV || true

# Pin the timezone to the operator's TZ (Asia/Tokyo, +09:00). The suite carries
# ~290 clock-format assertions (account quota-reset times, ISO offsets) that
# assume +09:00. The dedicated matrix nodes happen to carry that
# /etc/localtime, but the release node's apptainer-exec run inherits a
# different host TZ (+10:00) -> spurious "→22:05 != →21:05" / "+10:00 vs +09:00"
# mismatches that blocked the v0.21.12 publish. Forcing TZ here is a no-op on
# the Tokyo nodes and makes the in-SIF run deterministic everywhere.
export TZ="Asia/Tokyo"

# venv bin on PATH (this matrix leg's python3 + pip); PYTHONPATH points at the
# writable target so imports + coverage use the freshly-installed checkout.
export PATH="$VENV/bin:$PATH"

echo "py=$("$VENV/bin/python" -V) target=$TMPDIR/site"

# Install scitex-agent-container + its [all,dev] extras WITH deps into the writable target.
# Fallback chain mirrors scitex-agent-container's historical bare-uv/pip workflow so a
# packaging hiccup in an optional extra doesn't strand CI: [all,dev] → [dev] →
# bare. uv first (fast resolver), pip as a final safety net.
uv pip install --python "$VENV/bin/python" --target="$TMPDIR/site" -e ".[all,dev]" ||
    uv pip install --python "$VENV/bin/python" --target="$TMPDIR/site" -e ".[dev]" ||
    uv pip install --python "$VENV/bin/python" --target="$TMPDIR/site" -e "." ||
    pip install --target="$TMPDIR/site" -e ".[dev]"

# The CI SIF ships no SYSTEM tzdata, so zoneinfo.ZoneInfo("Asia/Tokyo") (the
# account-list renderer's TZ resolver; ~290 clock assertions assume +09:00)
# raises ZoneInfoNotFoundError and silently falls back to UTC on the release
# node — TZ above is then inert. Provide the data via the pip ``tzdata`` package
# (zoneinfo discovers it through importlib, no /usr/share/zoneinfo needed).
# No-op where system tzdata already exists (the dedicated matrix nodes).
uv pip install --python "$VENV/bin/python" --target="$TMPDIR/site" tzdata ||
    pip install --target="$TMPDIR/site" tzdata || true

export PYTHONPATH="$TMPDIR/site:$PWD/src${PYTHONPATH:+:$PYTHONPATH}"

# Several tests invoke the `sac` / `scitex-agent-container` CONSOLE SCRIPTS as a
# subprocess (e.g. the shell-completion + install tests). `pip install --target`
# does NOT reliably expose entry-point scripts on PATH, so the release node hit
# `FileNotFoundError: 'sac'`. Provide self-contained shims: each sets its own
# PYTHONPATH (so it works even from a test's cleaned subprocess env) and execs
# `python -m scitex_agent_container`, which is the package's CLI entry point.
mkdir -p "$TMPDIR/bin"
for _prog in sac scitex-agent-container; do
    {
        echo "#!/bin/sh"
        echo "export PYTHONPATH=\"$TMPDIR/site:$PWD/src\${PYTHONPATH:+:\$PYTHONPATH}\""
        echo "exec \"$VENV/bin/python\" -m scitex_agent_container \"\$@\""
    } >"$TMPDIR/bin/$_prog"
    chmod +x "$TMPDIR/bin/$_prog"
done
export PATH="$TMPDIR/bin:$PATH"

# ASSERT THE PLUGIN SET BEFORE TRUSTING A SINGLE PASS/FAIL COUNT.
#
# MEASURED 2026-08-11 in the fleet's shared /opt/venv-sac: a wide run produced
# 93 failures that were ALL `async def` tests, under a header reading
# `plugins: anyio, scitex-dev` — pytest-asyncio ABSENT. The same files pass
# standalone with asyncio-1.4.0 loaded. Plugin autoload had silently dropped it.
#
# THAT FAILURE IS INDISTINGUISHABLE FROM A REAL REGRESSION by inspection, and
# that is the whole problem: 93 red async tests look exactly like someone
# breaking the event loop. It also poisons the opposite direction — a run that
# quietly loses a plugin can go GREEN by not collecting what it should have.
# Any tuning decision (worker counts, selection strategy, the version matrix)
# taken against such a run is measuring the plugin lottery, not the code.
#
# So: fail LOUDLY and IMMEDIATELY when the set is incomplete, instead of handing
# back a number nobody can interpret. One line of diagnosis beats an afternoon.
#
# The check asks PYTEST what it actually loaded rather than asking Python what
# is importable — an installed-but-not-registered plugin is precisely the case
# that bit us, and `import pytest_asyncio` would have said everything was fine.
# An empty directory keeps it to a fraction of a second and collects nothing.
#
# NOT `-q`. THE FIRST VERSION OF THIS CHECK SHIPPED WITH `-q` AND FAILED EVERY
# JOB IT WAS ADDED TO: `-q` SUPPRESSES THE `plugins:` HEADER, so the grep found
# nothing, concluded the plugin set was empty, and refused to run the suite —
# all three matrix legs red in ~35s, on the very PR that introduced it. A guard
# whose sensor is switched off reports "broken" about a healthy environment,
# which is worse than no guard. Default verbosity prints the header; keep it.
_PLUGCHECK="$TMPDIR/plugcheck"
mkdir -p "$_PLUGCHECK"
_PLUGINS="$(python -m pytest --collect-only -p no:cacheprovider "$_PLUGCHECK" 2>&1 |
    grep -m1 '^plugins:' || true)"
echo "preflight ${_PLUGINS:-plugins: <no header emitted>}"
for _need in asyncio xdist cov timeout; do
    case "$_PLUGINS" in
    *"$_need"*) ;;
    *)
        echo "::error::pytest plugin '$_need' did NOT load in this environment."
        echo "::error::header was: ${_PLUGINS:-<none>}"
        echo "::error::Every pass/fail count from this run would be untrustworthy" \
            "— a missing pytest-asyncio turns every async test red, and a" \
            "missing plugin can also turn a run green by not collecting." \
            "Refusing to run the suite. Rebuild the CI SIF or fix the" \
            "[all,dev] install rather than re-running and hoping."
        exit 1
        ;;
    esac
done

# Parallelise with pytest-xdist (baked in [dev]/[all,dev] as pytest-xdist>=3).
# scitex-agent-container's suite is ~2460 tests; single-process it overran the job's old
# 30-min cap (2300 passed in ~28 min, cancelled at 96%). Each xdist worker is
# a SEPARATE PROCESS, so matplotlib's global rcParams / pyplot state and the
# scitex-agent-container style-stack are naturally isolated per worker — the safe way to
# parallelise a matplotlib-heavy suite.
#
# Worker count: use ALL cores. Each matrix leg now runs on its own dedicated
# self-hosted node (one runner per node: scitex-agent-container-01/02/03), so there is no
# co-tenant to yield half the box to — the old nproc//2 cap left 2x the cores
# idle. nice/ionice (below) handles the "yield to higher-priority work if the
# node is ever shared" concern instead of statically reserving half the CPUs.
# Floor 4. pyproject addopts carries `-v`; override to `-q` here — 2460 verbose
# lines x workers bloats the CI log and adds measurable overhead.
NPROC="$(nproc 2>/dev/null || echo 4)"
WORKERS=$NPROC
[ "$WORKERS" -lt 4 ] && WORKERS=4
echo "xdist workers=$WORKERS (nproc=$NPROC)"

# Warm the matplotlib font cache ONCE, single-process, before xdist forks the
# workers. This builds $MPLCONFIGDIR/fontlist-*.json a single time so every
# worker reads a complete, consistent cache instead of racing to build it
# concurrently (the source of the render1!=render2 reproducibility flakes).
# Fail-loud: if matplotlib can't even build its font cache, CI must surface it.
# matplotlib may not be a dependency of this package; only warm the
# font cache when it's importable (no-op otherwise — never fail the run
# on an optional warm-up).
if python -c "import matplotlib" 2>/dev/null; then
    python -c "import matplotlib; matplotlib.use('Agg'); from matplotlib import font_manager; font_manager.fontManager; import matplotlib.pyplot as plt; f=plt.figure(); f.canvas.draw(); print('mpl font cache warmed at', matplotlib.get_cachedir())"
else
    echo "matplotlib not importable — skipping font-cache warm-up (not a dep)"
fi

# Distribution: `--dist load` (per-TEST round-robin), NOT `--dist loadscope`.
# loadscope pins an entire MODULE's tests to ONE worker — and scitex-agent-container's heavy
# suites are big SINGLE modules (e.g. tests/integration/test_all_plotters_*.py
# parametrize one test over all 47 plotters, ~28 s each). loadscope therefore
# ran all ~50+ cases of such a module SERIALLY on one worker (~25 min) while the
# rest idled. There are NO module/session/class-scoped fixtures in those heavy
# modules and the root conftest's autouse `_close_figures` resets pyplot state
# after EVERY test, so loadscope's "same worker per module" buys nothing here —
# it only serialized. `load` spreads the parametrized cases across ALL workers.
#
# nice -n 19 ionice -c 3: run at the lowest CPU + idle I/O priority so that if
# this node is ever shared with interactive/dev work, CI grabs otherwise-idle
# cores but YIELDS the CPU and disk to any higher-priority process — "all
# available CPUs, with priority handling". exec replaces the shell with nice,
# which execs ionice, which execs python (still PID-traceable, signals/exit
# code propagate to the runner step).
exec nice -n 19 ionice -c 3 \
    python -m pytest tests/ -n "$WORKERS" --dist load -q \
    --cov=src/scitex_agent_container --cov-report=xml --cov-report=term \
    -p no:cacheprovider

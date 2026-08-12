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
# When that key is absent — as it was on 2026-08-09 — every one of those
# commits dies, and git reports it like this:
#
#     error: Couldn't load public key .../id_ed25519_scitex.pub: No such file
#     fatal: failed to write commit object
#     exit 128
#
# THAT SECOND LINE IS THE TRAP. "failed to write commit object" reads as disk
# I/O, so the obvious diagnosis is a full filesystem or a broken runner. It
# cost most of an afternoon and two WRONG root causes broadcast to other
# agents (a shared-runner git-identity fault, then ENOSPC) before anyone read
# the line above it. Reproduced exactly, and verified fixed, before this
# landed — see tests/integration/test_ci_commit_signing_disabled.py.
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
# `${TMPDIR:?}` AND NOT `$TMPDIR`, at every `rm -rf` of this path.
#
# The name is produced by a function in ANOTHER file, so "it is always non-empty"
# is a property of tmpdir-lib.sh, not of this line — one refactor away from being
# false, and nothing here would notice.
#
# `rm -rf ""` IS NOT THE SAFE NO-OP IT LOOKS LIKE. Measured on GNU coreutils 9.4:
# `-f` treats the empty operand as a nonexistent file, so it exits 0 SILENTLY —
# `set -euo pipefail` does not catch it, and the script CONTINUES with TMPDIR="".
# Every later use is then a path off the filesystem root: `"$TMPDIR/site"` is
# `/site`, and the sibling sweep's `! -path "$TMPDIR"` self-exclusion stops
# matching anything. The empty value is dangerous because it is silent.
#
# `:?` makes the shell abort right here, naming the variable, before the deletion.
rm -rf "${TMPDIR:?ci scratch path came back empty — refusing to rm -rf it}"
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
#
# Same `:?` guard as above, and it matters MORE here: a trap body is evaluated at
# EXIT, so it reads whatever TMPDIR holds then — not what it held at line 80.
trap 'rm -rf "${TMPDIR:?exiting with an empty scratch path — refusing to rm -rf it}"' EXIT

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
# Worker count: ALL cores by default, because the justification below USED to
# hold — "each matrix leg runs on its own dedicated self-hosted node (one runner
# per node: scitex-agent-container-01/02/03), so there is no co-tenant to yield
# half the box to". nice/ionice handles an occasional higher-priority neighbour.
#
# THAT ASSUMPTION DIES THE MOMENT TWO RUNNERS SHARE A BOX, and that is exactly
# where CI is going: Spartan CI was retired 2026-08-05 and the suite now runs on
# scitex-04, one 32-core machine. Three matrix legs on three runners there means
# 3 x 32 = 96 xdist processes fighting over 32 cores — an uncapped default
# resting on a premise the world quietly invalidated, the same shape as the
# "runs here are serialised" claim that once justified an unscoped pkill in
# exec-in-sif.sh.
#
# So the count is now an INPUT, not an inference. Set CI_XDIST_WORKERS (repo
# Actions Variable -> workflow env) to nproc / legs-per-box when runners are
# co-tenant. Unset keeps the historical behaviour exactly, so nothing changes
# for a one-leg-per-node deployment.
#
# Measured on scitex-04 (32 cores, full suite, one leg at a time):
#   WORKERS=32 -> 170s     WORKERS=8 -> 233s
# A single leg is faster with all cores; three CONCURRENT legs at ~10 each
# finish the whole matrix in about one leg's time instead of three.
#
# Floor 4. pyproject addopts carries `-v`; override to `-q` here — 2460 verbose
# lines x workers bloats the CI log and adds measurable overhead.
#
# ASK THE KERNEL WHAT THIS PROCESS MAY RUN ON — DO NOT ASK `nproc`.
#
# `nproc` was the obvious source and it is wrong on the Spartan runners.
# Measured on spartan-bm153 inside SLURM job 29015324, which holds 48 CPUs:
#
#     nproc                      1     <- what this line used to read
#     nproc --all              128
#     sched_getaffinity         48
#     taskset -pc self          48     (listed explicitly)
#     cpuset.cpus.effective  0-127     (no cgroup confinement)
#     SLURM_CPUS_PER_TASK       48
#
# The kernel will schedule this process on 48 CPUs. Only `nproc` disagreed, so
# the suite ran on the FLOOR of 4 workers inside a 48-CPU allocation the fleet
# was already paying for — 15282 passed in 532s where it had 12x the cores
# available.
#
# CAUSE, MEASURED RATHER THAN MATCHED. coreutils `nproc` honours
# OMP_NUM_THREADS / OMP_THREAD_LIMIT AHEAD of the affinity mask. On that runner:
#
#     OMP_NUM_THREADS        1
#     nproc (as-is)          1
#     nproc (OMP_* cleared)  48
#     sched_getaffinity      48
#
# Clearing the variable moves nproc from 1 to 48, which establishes the cause
# instead of merely fitting it.
#
# DO NOT "FIX" THIS BY UNSETTING OMP_NUM_THREADS. It is set to 1 on purpose and
# it is CORRECT: each xdist worker is a separate process, and BLAS/OpenMP inside
# numpy will happily start one thread per core in EVERY one of them. 48 workers
# x 48 OMP threads is 2304 threads on 48 CPUs. OMP_NUM_THREADS=1 with 48
# PROCESSES is exactly the right shape — one thread per core, no oversubscription.
# The bug was never the variable; it was reading a THREAD-BUDGET knob as a
# CPU-COUNT fact.
#
# `Cpus_allowed_list` in /proc/self/status is exactly "the CPUs the kernel will
# schedule THIS process on". It needs no python and no taskset (neither is
# guaranteed on a bare HPC node — that is why the SIF exists), and no OpenMP
# variable can override it. Fall back to SLURM_CPUS_PER_TASK, then to `nproc`,
# so a host without /proc/self/status behaves exactly as before.
#
# The echo prints EVERY source, not just the winner. When these disagree again
# — and they will, on some host nobody has met yet — the disagreement is the
# finding, and it should be in the log rather than requiring a special trip.
_cpus_from_affinity() {
    local list part lo hi n=0
    list=$(awk '/^Cpus_allowed_list:/ {print $2; exit}' /proc/self/status 2>/dev/null)
    [ -n "$list" ] || return 1
    local IFS=','
    for part in $list; do
        case "$part" in
            *-*)
                lo=${part%%-*}; hi=${part##*-}
                case "$lo$hi" in *[!0-9]*) return 1 ;; esac
                n=$(( n + hi - lo + 1 ))
                ;;
            *)
                case "$part" in *[!0-9]*) return 1 ;; esac
                n=$(( n + 1 ))
                ;;
        esac
    done
    [ "$n" -gt 0 ] || return 1
    printf '%s\n' "$n"
}

NPROC="$(nproc 2>/dev/null || echo 4)"
# THE TWO CHANGES COMPOSE; THEY ARE NOT ALTERNATIVES.
#
#   CI_XDIST_WORKERS (#881)                     the OVERRIDE — outermost
#   affinity -> SLURM_CPUS_PER_TASK -> nproc    the DEFAULT — what it is
#                                               when nobody sets the override
#   floor of 4                                  last
#
# #881 fixed who *can* set the number. This fixes what it *is* when nobody
# does. An override with a broken default is still broken for everyone who
# does not set it; a good default with no override is inflexible.

# DEFAULT: what this process may actually run on.
AFFINITY="$(_cpus_from_affinity || true)"
DETECTED="${AFFINITY:-${SLURM_CPUS_PER_TASK:-}}"
case "$DETECTED" in
    ''|*[!0-9]*) DETECTED="$NPROC" ;;
esac

# OVERRIDE: an explicit CI_XDIST_WORKERS beats any detection, including a
# correct one — that is the point of #881 (co-tenant legs on one box).
WORKERS="${CI_XDIST_WORKERS:-$DETECTED}"
case "$WORKERS" in
    ''|*[!0-9]*)
        # Falls back to DETECTED rather than raw nproc: a malformed override
        # should degrade to the best number we can measure, not to the one
        # that reported 1 inside a 48-CPU allocation.
        echo "::warning::CI_XDIST_WORKERS='$WORKERS' is not a positive integer; using detected=$DETECTED"
        WORKERS=$DETECTED
        ;;
esac

[ "$WORKERS" -lt 4 ] && WORKERS=4

# EVERY source, not the winner. When these disagree on a host nobody has met
# yet, the disagreement is already in the log instead of costing a probe run.
echo "xdist workers=$WORKERS (affinity=${AFFINITY:-<unreadable>} nproc=$NPROC SLURM_CPUS_PER_TASK=${SLURM_CPUS_PER_TASK:-<unset>} CI_XDIST_WORKERS=${CI_XDIST_WORKERS:-unset})"

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

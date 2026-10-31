#!/usr/bin/env bash
# Runs the suite on a GitHub-HOSTED runner. $1 = python version.
#
# The hosted sibling of run-in-sif.sh, for nightly and unadmitted matrix requests. Same suite, different hardware and
# therefore a different install path: there is no ci-cpu.sif here and no baked
# /opt/venv-<ver>, so uv fetches its OWN managed CPython and builds a venv.
#
# WHY A SEPARATE SCRIPT AND NOT A FLAG ON run-in-sif.sh: that script's entire
# body is SIF-specific (read-only /opt venvs, a read-only $HOME, --target
# installs, node-local scratch that must be reaped). None of it applies here,
# and threading an `if hosted` through it would put the release gate's script
# one typo away from behaving differently on the path that ships. Two scripts,
# each honest about its environment.
#
# WHAT IS DELIBERATELY COPIED FROM run-in-sif.sh, because it is not
# environment-specific but SUITE-specific — get any of it wrong and the run
# reports something other than "does the code work on this Python":
#
#   * commit signing OFF (dozens of tests commit into throwaway repos),
#   * TZ=Asia/Tokyo (~290 clock assertions assume +09:00),
#   * MPLBACKEND=Agg + a warmed font cache before xdist forks,
#   * the pytest PLUGIN PREFLIGHT — a run that quietly lost pytest-asyncio
#     invents ~93 failures that look exactly like a real regression, and can
#     also go green by not collecting. Nightly output nobody can interpret is
#     worse than no nightly.
set -euo pipefail

V="${1:?python version arg required (e.g. 3.12)}"
SUITE="${2:-nightly}"
case "$V" in 3.11|3.12|3.13) ;; *) echo "unsupported CI Python: $V" >&2; exit 2 ;; esac
case "$SUITE" in matrix|nightly) ;; *) echo "unsupported CI suite: $SUITE" >&2; exit 2 ;; esac

# An exclusive hosted job directory owns its interpreter/cache; it never writes
# a repository-local agent environment or uses company HOME credentials.
HOSTED_ROOT="$(mktemp -d "${RUNNER_TEMP:?hosted RUNNER_TEMP required}/sac-hosted-$V.XXXXXXXX")"
# The hosted VM owns final scratch reclamation; this shell never removes a
# directory while a cancelled foreground test process might still be using it.
export UV_CACHE_DIR="$HOSTED_ROOT/uvcache"
export XDG_CACHE_HOME="$HOSTED_ROOT/cache"
unset CODEX_HOME CLAUDE_CONFIG_DIR HERMES_HOME SCITEX_STORE_DSN PGUSER PGPASSWORD PGPASSFILE

export LC_ALL=C.UTF-8 LANG=C.UTF-8

# Never sign commits made by the test suite (see run-in-sif.sh for the full
# story: the error surfaces as "fatal: failed to write commit object", which
# reads like a full disk and cost most of an afternoon once).
export GIT_CONFIG_COUNT=2
export GIT_CONFIG_KEY_0=commit.gpgsign
export GIT_CONFIG_VALUE_0=false
export GIT_CONFIG_KEY_1=tag.gpgsign
export GIT_CONFIG_VALUE_1=false

# ~290 clock-format assertions in this suite assume +09:00. The hosted image is
# UTC, so without this the nightly would be red on the clock, not on the code.
export TZ="Asia/Tokyo"

export MPLBACKEND=Agg
export MPLCONFIGDIR="$HOSTED_ROOT/mpl"
mkdir -p "$MPLCONFIGDIR"

# uv's own managed CPython — actions/setup-python is not used anywhere in this
# repo (it fails on the self-hosted nodes) and there is no reason to diverge.
uv venv --python "$V" "$HOSTED_ROOT/venv"
PY="$HOSTED_ROOT/venv/bin/python"

# The full declared all/dev dependency install is the test contract. An install
# error fails this leg before pytest; a reduced extra set cannot give green CI.
uv pip install --python "$PY" -e ".[all,dev]" tzdata

# THE VENV'S bin/ MUST BE ON PATH, because several tests exec the `sac` CONSOLE
# SCRIPT as a subprocess (the shell-completion install tests, the SDK channel
# sidecar resolver). Without this the first hosted run reported SIX failures out
# of 14991 — all of them this one cause, and all of them reading like real bugs:
#
#   SacBinaryNotFoundError: Cannot resolve the `sac` console script: not on PATH
#   ...test_install_writes_bash_cache_file - assert False where False = is_file()
#
# run-in-sif.sh has the same requirement and solves it by hand-writing shims,
# because `pip install --target` does not materialise entry points at all. A
# venv install DOES create them — they just have to be reachable.
export PATH="$HOSTED_ROOT/venv/bin:$PATH"

# ASSERT THE PLUGIN SET BEFORE TRUSTING A SINGLE PASS/FAIL COUNT.
# NOT `-q`: quiet suppresses the `plugins:` header this reads, and the first
# version of this check shipped that way and failed every job it was added to.
_PLUGCHECK="$HOSTED_ROOT/plugcheck"
mkdir -p "$_PLUGCHECK"
_PLUGINS="$("$PY" -m pytest --collect-only -p no:cacheprovider "$_PLUGCHECK" 2>&1 |
    grep -m1 '^plugins:' || true)"
echo "preflight ${_PLUGINS:-plugins: <no header emitted>}"
for _need in asyncio xdist cov timeout; do
    case "$_PLUGINS" in
    *"$_need"*) ;;
    *)
        echo "::error::pytest plugin '$_need' did NOT load in this environment."
        echo "::error::header was: ${_PLUGINS:-<none>}"
        echo "::error::Refusing to run the suite — every pass/fail count from" \
            "this run would be untrustworthy."
        exit 1
        ;;
    esac
done

# Warm the matplotlib font cache once, pre-fork, so xdist workers do not race to
# build it (that race produces render1 != render2 reproducibility flakes).
if "$PY" -c "import matplotlib" 2>/dev/null; then
    "$PY" -c "import matplotlib; matplotlib.use('Agg'); from matplotlib import font_manager; font_manager.fontManager; import matplotlib.pyplot as plt; f=plt.figure(); f.canvas.draw(); print('mpl font cache warmed')"
else
    echo "matplotlib not importable — skipping font-cache warm-up (not a dep)"
fi

NPROC="$(nproc 2>/dev/null || echo 2)"
WORKERS="${CI_XDIST_WORKERS:-$NPROC}"
case "$WORKERS" in ''|*[!0-9]*|0) echo "invalid CI worker count: $WORKERS" >&2; exit 2 ;; esac

echo "python=$("$PY" -V) xdist workers=$WORKERS"

# Both paths run every test with xdist. Only the matrix publishes coverage;
# the scheduled nightly keeps its existing no-coverage behavior.
PYTEST_ARGS=(tests/ -n "$WORKERS" --dist load -q -rfEs -p no:cacheprovider)
if [ "$SUITE" = matrix ]; then
    PYTEST_ARGS+=(--cov=src/scitex_agent_container --cov-report=xml --cov-report=term)
fi
"$PY" -m pytest "${PYTEST_ARGS[@]}"

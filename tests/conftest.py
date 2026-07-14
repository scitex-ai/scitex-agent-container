"""Pytest fixtures and rootdir marker for this package.

An empty conftest.py at tests/ is the canonical SciTeX
convention (audit-project PS208) — it pins the pytest
rootdir and gives downstream fixtures a home.

Also wires subprocess + parallel coverage at module-import time so
child Python interpreters spawned via `subprocess.run(...)` are
included in the coverage report. `os.environ.setdefault` would be a
no-op here because pytest-cov has already set `COVERAGE_FILE` to a tmp
dir by the time this conftest is loaded — force-set is required. See
~/proj/scitex-dev/src/scitex_dev/_skills/general/05_development_06_subprocess-coverage.md.
"""

from __future__ import annotations

import itertools
import os
import sysconfig
from pathlib import Path
from typing import Iterator

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Pin coverage's data file under tests/results/coverage/ (keeps the repo
# root clean) and point process_startup at our pyproject so child
# interpreters configure themselves correctly. COVERAGE_FILE is absolute
# so every child writes here regardless of its working directory.
_COVERAGE_DIR = _PROJECT_ROOT / "tests" / "results" / "coverage"
_COVERAGE_DIR.mkdir(parents=True, exist_ok=True)
os.environ["COVERAGE_PROCESS_START"] = str(_PROJECT_ROOT / "pyproject.toml")
os.environ["COVERAGE_FILE"] = str(_COVERAGE_DIR / ".coverage")

# incident-local-heavy-build: `sac image build` self-demotes its own
# process (CPU nice 19 + IO best-effort lowest) by default, and demotion
# is ONE-WAY for unprivileged processes. Tests drive the real CLI
# in-process via CliRunner, so without this opt-out the first build test
# would demote the entire remaining pytest run. The real demotion
# behavior is exercised in CHILD interpreters with a curated env — see
# tests/scitex_agent_container/test__build_priority.py.
os.environ.setdefault("SAC_BUILD_NO_NICE", "1")

# --- NEVER let a test drive the LIVE listen watchdog ----------------------
# `scripts/systemd/sac-listen-health-probe.sh` persists a FAILURE LEDGER at
#   $HOME/.scitex/agent-container/runtime/listen-health.state
# because systemd invokes it FRESH every ~30s, so counting CONSECUTIVE
# failures is only possible across a file.
#
# A test that shells the probe without redirecting that path writes to the
# REAL ledger. On the host that is not a dirty-state annoyance, it is an
# OUTAGE: a test leaving `failures=2` behind means the next real timer tick
# can reach the threshold and RESTART the real `sac listen` — which tears
# down the in-memory a2a Broker and DEAFENS EVERY AGENT'S INBOX AT ONCE. The
# test suite would cause the exact incident the watchdog exists to prevent
# (2026-07-14). CI cannot see this — there is no fleet on a runner — which
# is precisely why the floor belongs here and not in a single test file.
#
# Force-set (not setdefault): a hard floor that holds even for code paths
# that bypass fixtures. Individual probe suites additionally give each test
# its own ledger so they cannot bleed a failure count into one another.
_LISTEN_HEALTH_DIR = _PROJECT_ROOT / "tests" / "results" / "listen-health"
_LISTEN_HEALTH_DIR.mkdir(parents=True, exist_ok=True)
os.environ["SAC_LISTEN_HEALTH_STATE"] = str(_LISTEN_HEALTH_DIR / "session.state")
# And a probe run by a test must never page the operator.
os.environ["SAC_LISTEN_NOTIFY"] = "0"

# --- NEVER let a test touch the REAL sac state root -----------------------
# Same class as the watchdog ledger above, same reason it belongs HERE.
#
# sac resolves its state root under $HOME. Three of those paths are
# module-level constants computed at IMPORT time from an env var:
#
#   SCITEX_AGENT_CONTAINER_STATE_DB      -> _state.state_db.DEFAULT_DB_PATH
#   SCITEX_AGENT_CONTAINER_REGISTRY_DIR  -> _state.registry.REGISTRY_DIR
#   SCITEX_AGENT_CONTAINER_RUNTIME_DIR   -> _runners._session_state.DEFAULT_STATE_ROOT
#
# Because they are computed at import, a *fixture* that only sets the env var
# is too late — the value is already baked. Setting the env HERE, in the
# conftest module body, is early enough: pytest imports this before it imports
# any test module (and therefore before `scitex_agent_container` is first
# imported), so the constants are born pointing at the sandbox.
#
# Force-set, not setdefault: a hard floor that holds even for code paths that
# bypass fixtures (module-level imports, session fixtures, subprocesses a test
# spawns with an inherited env).
#
# The bug this closes — ghost tag v0.21.18 (2026-07-14). Tests opted into
# state.db isolation one file at a time and several that reach `agent_start`
# never did, so their writes landed in whatever the process called "default":
#
#   grant_send -> comms_grants rows     (fixed at the seam in #675)
#   claim_port -> a2a_ports rows        <-- the release killer
#
# The killer mechanism is INTRA-RUN EXHAUSTION, and it is worth being precise
# because the obvious story is wrong. `claim_port` (port_allocator.py:196)
# consults ONLY the database — it never checks whether a port is actually
# bound — and `a2a_ports.name` is the PRIMARY KEY. So every DISTINCT agent
# name that reaches `agent_start` inserts ANOTHER row and burns ANOTHER port
# out of the fixed range [19000, 19999]. Nothing gives them back inside a
# run: `release_port` is only ever called from `agent_stop`, which an
# `agent_start`-only test never reaches.
#
# So the rows pile up WITHIN a single pytest session, against the one
# unisolated database all of those tests share. Once ~1000 distinct names
# have been through, the range is gone and every subsequent `agent_start`
# dies with:
#
#   RuntimeError: no free a2a port in range [19000, 19999] (all claimed)
#
# It is NOT an accumulation across runs. (The release runner's on-disk db was
# checked afterwards and held FOUR rows, not a thousand — the exhausted db is
# the job's own, and it dies with the job.) It is also not a flake: it is a
# deterministic function of how many distinct agent names one session drives.
#
# CI's PR gate cannot see it and never will: `pytest-matrix` shards nothing
# and each `ubuntu-latest` job is ephemeral, but more importantly the count
# only has to cross 1000 — which it does on the release job, where three
# python legs run concurrently on the SELF-HOSTED `scitex-ci` node. When
# `test` fails there, build/publish/release (all `needs: test`) are SKIPPED:
# the tag is pushed and nothing reaches PyPI. That is a ghost tag, and eight
# historical ones went unnoticed for months.
_SAC_STATE_FLOOR = _PROJECT_ROOT / "tests" / "results" / "sac-state" / "floor"
os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(_SAC_STATE_FLOOR / "state.db")
os.environ["SCITEX_AGENT_CONTAINER_REGISTRY_DIR"] = str(_SAC_STATE_FLOOR / "registry")
os.environ["SCITEX_AGENT_CONTAINER_RUNTIME_DIR"] = str(_SAC_STATE_FLOOR / "runtime")


def _ensure_subprocess_coverage_shim() -> None:
    """Drop an idempotent ``.pth`` shim in site-packages so every child
    Python interpreter auto-starts coverage via
    ``coverage.process_startup()``.
    """
    purelib = Path(sysconfig.get_paths()["purelib"])
    pth = purelib / "_scitex_agent_container_subprocess_coverage.pth"
    # ``import coverage`` MUST stay inside the guard: a ``.pth`` runs at
    # every interpreter startup, so an unconditional import taxes ~120ms
    # onto each non-coverage `python`/`sac` invocation. Mirror the shape
    # of the sibling ``a1_coverage.pth``.
    shim = (
        "import os\n"
        "if os.environ.get('COVERAGE_PROCESS_START'):\n"
        "    import coverage\n"
        "    coverage.process_startup()\n"
    )
    try:
        if not pth.exists() or pth.read_text() != shim:
            pth.write_text(shim)
    except OSError:
        # site-packages may be read-only (e.g. system Python). Local
        # dev venvs are writable and that's where this matters.
        pass


_ensure_subprocess_coverage_shim()

# Expose shared no-mocks helpers (subprocess_shim, env_save_restore,
# ssh_http_shim) as session-wide fixtures so any test under tests/
# can use them by name.
from tests.scitex_agent_container._helpers.ssh_http_shim import (  # noqa: E402,F401
    ssh_http_shim,
)
from tests.scitex_agent_container._helpers.subprocess_shim import (  # noqa: E402,F401
    env_save_restore,
    subprocess_shim,
)

# ---------------------------------------------------------------------------
# Per-test state.db isolation, layered ON TOP of the floor above.
#
# The floor keeps every write off the operator's real state.db. This fixture
# additionally gives each test its OWN database, and that second layer is not
# cosmetic — it is what stops the port ratchet from re-forming INSIDE a single
# run. `claim_port` never releases, so ~4900 tests sharing one floor database
# would march through [19000, 19999] and exhaust it from the inside, failing
# late tests for the same reason the release was failing. A fresh DB per test
# means the allocator always starts from an empty `a2a_ports` table and always
# finds 19000 free, so isolation alone cures the exhaustion and no test has to
# learn to call `release_port`.
#
# BOTH handles are redirected. The env var alone is not enough once
# `state_db` has been imported (DEFAULT_DB_PATH is already baked), and the
# constant alone is not enough for a subprocess (which reads the env). Tests
# that additionally `importlib.reload(state_db)` re-derive the constant from
# the env we set here, so they keep working; tests with their own isolation
# fixture just override both again — this only moves the DEFAULT.
# ---------------------------------------------------------------------------

_STATE_DB_KEY = "SCITEX_AGENT_CONTAINER_STATE_DB"
_state_db_seq = itertools.count()


# scope="function" is SPELLED OUT (it is also pytest's default) because the
# whole fix depends on it, and a future "let's not rebuild the DB 4900 times"
# optimisation to scope="session"/"module" would silently REINTRODUCE the bug:
# `claim_port` never releases, so any db shared across tests re-accumulates
# rows until [19000, 19999] is exhausted mid-run. Per-TEST or it does not work.
@pytest.fixture(autouse=True, scope="function")
def _isolate_state_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Point this test's state.db at a private tmp file. Restores on teardown."""
    from scitex_agent_container._state import state_db

    # Computed but deliberately NOT created: `state_db._connect` mkdirs the
    # parent on first real use, so a test that never opens the DB pays no
    # mkdir and leaves no empty dir behind (this runs ~4900 times).
    db = (
        tmp_path_factory.getbasetemp()
        / "state-db"
        / f"t{next(_state_db_seq)}"
        / "state.db"
    )
    saved_env = os.environ.get(_STATE_DB_KEY)
    saved_const = state_db.DEFAULT_DB_PATH
    os.environ[_STATE_DB_KEY] = str(db)
    state_db.DEFAULT_DB_PATH = db
    try:
        yield db
    finally:
        state_db.DEFAULT_DB_PATH = saved_const
        if saved_env is None:
            os.environ.pop(_STATE_DB_KEY, None)
        else:
            os.environ[_STATE_DB_KEY] = saved_env

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

# --- the suite must not inherit the CONTAINER's spec-env manifest ----------
# `SAC_SPEC_ENV_KEYS` is injected at agent launch and names the spec-env keys
# the launch PROMISED to provide. `resolve_spec_env` (runtimes/_mcp_spec_env.py)
# reads the REAL os.environ and RAISES SpecEnvUnresolvedError when a promised
# key is missing — deliberately, because a silently-degraded MCP config
# rebuilds the mid-session identity/store loss of
# card sac-env-injection-lost-on-mcp-reconnect-20260721.
#
# That guard is correct in production and WRONG to inherit in tests. Every
# agent in this fleet runs pytest INSIDE its own container, where the var is
# set (10 keys), while tests legitimately construct a controlled env without
# them. The guard then fires during `build_sdk_options` and kills tests that
# have nothing to do with spec-env.
#
# Measured 2026-08-18 on develop @4a03f69c, same commit, same worktree, only
# the environment differing:
#
#   SAC_SPEC_ENV_KEYS set (inside a container)
#       runtimes/test__sdk_common.py      2 failed, 26 passed, 19 errors
#   SAC_SPEC_ENV_KEYS unset (CI, or any plain shell)
#       runtimes/test__sdk_common.py      47 passed
#
# plus 4 more failures elsewhere that also pass once it is unset — 25 false
# failures from one leaked variable.
#
# WHY THIS IS WORSE THAN AN ORDINARY RED: it is invisible and it is uniform.
# CI never sets the var, so the gate is green and cannot see it; every agent
# runs in a container, so every agent sees the same false red and concludes
# trunk is broken. An A/B against a clean baseline does NOT catch it either,
# because both arms carry the leak — which is how it survived being carded as
# "develop is RED before any change" (2026-08-17) for a day.
#
# Deleted, not set to "": an EMPTY manifest is a meaningful value to
# `resolve_spec_env` (it means "nothing was promised"), and writing one would
# assert a promise this process never made. Absence is the honest state.
#
# Module body, not a fixture, and NOT restored afterwards: the leak also
# travels into subprocesses a test spawns with an inherited env, which a
# function-scoped fixture would re-open the moment it restored. Tests that
# exercise the guard are unaffected — they build a FAKE environ dict with
# their own setenv/delenv helpers (tests/.../runtimes/test__mcp_spec_env.py)
# and never consult the ambient one, so the guard keeps its own coverage.
os.environ.pop("SAC_SPEC_ENV_KEYS", None)

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
#
# THE FLOOR IS PER-XDIST-WORKER, and that suffix is not cosmetic. The release
# gate runs `pytest -n $(nproc)`, so N worker PROCESSES share one checkout. A
# single project-relative floor is therefore a HOST-GLOBAL NAMESPACE shared by
# every worker in the leg — the same shape of bug as the tmux-socket collision
# and the persistent-$HOME collision. Anything that lands on the floor (rather
# than in a per-test tmp db) is then contended ACROSS workers, which is
# precisely the condition the a2a_ports race needs. `PYTEST_XDIST_WORKER` is set
# in each worker's env before conftest is imported, so it is available here;
# it is absent (-> "main") for a plain single-process run.
_XDIST_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")
_SAC_STATE_FLOOR = (
    _PROJECT_ROOT / "tests" / "results" / "sac-state" / f"floor-{_XDIST_WORKER}"
)
os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(_SAC_STATE_FLOOR / "state.db")
os.environ["SCITEX_AGENT_CONTAINER_REGISTRY_DIR"] = str(_SAC_STATE_FLOOR / "registry")
# Also reached by `_listen._single_instance.default_lock_dir()`, which used to
# hard-code Path.home() and so was NOT redirected by this floor at all — on the
# self-hosted release runner ($HOME = the operator's real home) that let a test
# touch the LIVE `sac listen` PIDFILE. It now honours this same variable.
os.environ["SCITEX_AGENT_CONTAINER_RUNTIME_DIR"] = str(_SAC_STATE_FLOOR / "runtime")

# --- NEVER let a test touch the REAL card board ---------------------------
# INCIDENT 2026-07-20: the fleet's live board went from ~2777 cards to SIX.
# Five of the six survivors were OUR fixtures — `other-agent-card-0/1` and
# `scitex-todo-card-0/1/2`, i.e. exactly the `seed_cards()` calls in
# tests/scitex_agent_container/_lifecycle/test__rename_cards.py.
#
# This was NOT a floor that broke. It is a floor that was never laid, and the
# reason it was missed is that the tests LOOKED isolated. `_helpers/fleet_root.
# isolated_board()` carefully redirects `$SCITEX_TODO_TASKS_YAML_SHARED` at a
# tmp YAML and passes an explicit `store=` to every scitex-todo call. All of
# that is correct — and all of it protects only the YAML.
#
# The board is no longer just YAML. scitex-cards mirrors every write into a
# SQLite shadow, and the mirror resolves its OWN path independently of the
# store you wrote to:
#
#   _dual_write.mirror_after_save(doc, store_path)   # store_path = our tmp yaml
#     -> mirror_doc_incremental(doc, resolve_db_path(), store_path=store_path)
#                                    ^^^^^^^^^^^^^^^^^ no explicit arg
#
# `resolve_db_path()` (scitex_cards/_db.py:95) with no argument falls through
# `$SCITEX_CARDS_DB` -> `$SCITEX_TODO_DB` -> `~/.scitex/cards/cards.db`. None of
# those were set, so the doc went to tmp and the MIRROR went to the live board.
#
# And the mirror is a RECONCILE, not an append (scitex_cards/_db_mirror.py:208):
#
#   removed = [i for i in prior if i not in now_hashes]
#   for tid in removed: _delete_task(conn, tid)
#
# So mirroring a 5-card tmp doc onto the real DB DELETED the other 2,772 cards.
# The test never touched the real board's YAML and still destroyed the board.
#
# Force-set, same rationale as the sac paths above: a hard floor that does not
# depend on any fixture remembering to opt in. Redirecting the PATH (rather
# than switching the mirror off) is deliberate — the production dual-write path
# keeps running and stays under test; it just runs into the sandbox.
#
# BOTH names are set. `$SCITEX_CARDS_DB` is the current one and wins;
# `$SCITEX_TODO_DB` is the pre-rename name (package renamed 2026-07-16) that
# `resolve_db_path` still honours for direct callers in a process that never
# imported the scitex_cards root, and this is the variable whose absence cost
# 2,777 cards — it is not the place to bet on a transition window.
_SAC_CARDS_DB = _SAC_STATE_FLOOR / "cards" / "cards.db"
os.environ["SCITEX_CARDS_DB"] = str(_SAC_CARDS_DB)
os.environ["SCITEX_TODO_DB"] = str(_SAC_CARDS_DB)

# --- NEVER let a test ssh into the operator's REAL fleet -------------------
# `sac agents list` is FLEET-WIDE by default: with no flags it fans out over
# every peer in `~/.scitex/agent-container/config.yaml` UNION the scitex-dev
# host registry. On the operator's own machines that is ~12 live hosts, and
# `scitex_dev.hosts.list_hosts()` SEEDS a default registry from its built-ins
# on any box where the file is absent — so even a fresh CI runner resolves
# mba / spartan / the NAS row set and would try to reach them.
#
# Six existing tests invoke the fleet view (`runner.invoke(status, ["--json"])`
# and friends). Without this floor each of them would open real ssh
# connections, take a per-host timeout to fail, and make its assertions a
# function of the operator's network. Same reasoning, same shape, as the state
# / event-log / card floors above: force-set, so it does not depend on any
# fixture remembering to opt in.
#
# Tests that exercise the fan-out ITSELF clear this via `env_save_restore` (or
# pass explicit `targets=` / `peer_probe=` seams), which is the intended way to
# reach the peer leg deliberately rather than by accident.
os.environ["SAC_AGENTS_LIST_NO_FANOUT"] = "1"

# --- NEVER let a test run a BACKGROUND LOOP it did not ask for ------------
# THE ASSERTION-CORRUPTION PATH, measured. `sac listen`'s lifespan launches
# six background loops. `SAC_LISTEN_STARTUP_SYNC_DISABLED`,
# `SAC_PERIODIC_DRIVE_DISABLED`, `SAC_LIVENESS_TICK_DISABLED` and friends
# are honoured AT THE LAUNCH SITE, so setting them means the task is never
# created. The CI poller, the TUI heartbeat writer and the SDK heartbeat
# writer were the exceptions: they launched unconditionally and let the
# coroutine "self-disable" — which still cost a task and STILL LOGGED A
# LINE. They now honour their switch at the launch site too, which is what
# makes this floor work at all.
#
# Why that line is not cosmetic. sac logs through scitex-logging, whose
# `LazyStderrStreamHandler` deliberately re-resolves `sys.stderr` AT EVERY
# EMIT so it follows click's isolated streams and pytest's capture. In a
# test process that means a loop's line is written into whatever stream is
# installed at that instant — including the buffer of a `CliRunner.invoke`
# running elsewhere in the same worker. Measured: a background thread's log
# line landed inside `result.output` on 30 invokes out of 30.
#
# Every test that boots the real listen app therefore paid for a CI poller
# and a 30-second heartbeat writer it has no interest in. Measured across
# tests/scitex_agent_container/{_lifecycle,_listen,a2a} alone: 1561 loop log
# lines from 134 tests, none of which is about CI polling or tmux
# heartbeats. An ACL test does not need a GitHub poller.
#
# WHY THE GROUP SWITCH AND NOT THE THREE PUBLISHED ONES. The obvious floor
# is `SAC_GITHUB_CI_POLLER_DISABLED=1` + the two heartbeat twins. It is
# wrong, and the by-name before/after diff on this very change caught it:
# those three variables are read by the COROUTINES too, so setting them
# suite-wide silently changed the behaviour of every test that calls a loop
# function DIRECTLY — the loops' own unit tests. It failed
#   _lifecycle/test__sdk_heartbeat_loop_unknown_is_not_dead.py and
#   _lifecycle/test__tui_heartbeat_loop_unknown_is_not_dead.py,
# two files a per-file opt-in list had missed, and any file added later
# would have been missed the same way. A floor whose correctness depends on
# maintaining a list of exceptions is not a floor.
#
# `SAC_LISTEN_POLLER_LOOPS_DISABLED` is read at the LIFESPAN LAUNCH SITE and
# nowhere else, so it says exactly one thing — "this app boots without its
# pollers" — and cannot reach a test that drives a loop itself. Force-set,
# like every floor above.
os.environ["SAC_LISTEN_POLLER_LOOPS_DISABLED"] = "1"

# --- A `--json` ASSERTION READS result.stdout, NEVER result.output --------
# Convention for every CliRunner test in this tree. It is one line to get
# right and it has cost a red trunk.
#
# In click >= 8.2 `Result.output` is a THIRD buffer: stdout and stderr
# mixed in write order, "as the user would see it in a terminal".
# `Result.stdout` is stdout alone. So
#
#     json.loads(result.output[result.output.index("{"):])
#
# does not assert "the --json surface emitted valid JSON". It asserts
# "and also nothing anywhere in this PROCESS wrote to stderr during the
# invoke" — a condition no test controls, because under pytest-xdist the
# whole worker's background threads share the process. develop 312975ec
# died on exactly that: a request thread in
# tests/integration/test_sac_listen_health_watchdog_decision.py appended a
# BrokenPipeError traceback after the JSON, and two fleet tests in a
# different directory failed with `JSONDecodeError: Extra data`
# (run 31867365078, py3.11, gw7).
#
# The prefix-skip is the other half. It exists to tolerate LEADING noise,
# and tolerating leading noise is precisely what kept TRAILING noise
# invisible until it landed mid-document. It also hid a real product bug:
# `sac image list --json` printed a human "scan root: ..." banner to
# STDOUT before the payload, so `sac image list --json | jq` failed on the
# first byte, and three tests asserted happily past it for as long as it
# existed.
#
# So: `json.loads(result.stdout)`, whole and unsliced. It is simpler than
# what it replaces, and it says the true thing — a `--json` surface
# promises stdout is EXACTLY one JSON document.


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
# ssh_exec_shim, ssh_http_shim, dead_port) as session-wide fixtures so any test
# under tests/ can use them by name.
from tests.scitex_agent_container._helpers.ports import (  # noqa: E402,F401
    dead_port,
)
from tests.scitex_agent_container._helpers.ssh_exec_shim import (  # noqa: E402,F401
    ssh_exec_shim,
)
from tests.scitex_agent_container._helpers.ssh_http_shim import (  # noqa: E402,F401
    ssh_http_shim,
)
from tests.scitex_agent_container._helpers.subprocess_shim import (  # noqa: E402,F401
    env_save_restore,
    subprocess_shim,
)

# Autouse gate: fail the test that leaves a background thread running. Imported
# here rather than defined here for the same reason the shims above are -- and
# it belongs beside `_assert_state_floor_intact` in spirit, because it is the
# same failure class: state a test leaves behind that damages a LATER,
# unrelated test while the guilty one passes. See the module docstring.
from tests.scitex_agent_container._helpers.thread_leak import (  # noqa: E402,F401
    _assert_no_leaked_threads,
)


# ---------------------------------------------------------------------------
# THE PACKAGE UNDER TEST MUST BE THIS CHECKOUT. Fail the SESSION otherwise.
#
# Without `pythonpath = ["src"]` (pyproject, [tool.pytest.ini_options]) a bare
# `pytest` here imported scitex_agent_container from wherever the ambient
# interpreter found it — in the agent container, a REAL copy in
# /opt/venv-sac/lib/python3.12/site-packages. A worker got 50/50 PASSED on a
# package that did not contain their PR. They only caught it because their
# branch ADDED a file, so collection raised ImportError; a change that merely
# EDITS existing files has no such tell and simply goes green.
#
# CI never saw this and never could: .github/ci/run-in-sif.sh installs THIS
# checkout into an ephemeral target and puts $PWD/src on PYTHONPATH, so CI
# always resolved the checkout. The one gate that would have caught it is the
# one gate that is structurally blind to it — which is exactly why it survived.
# So the guard runs where the bug actually lives: the developer's box.
#
# It compares the MODULE PATH, never the version string. __version__ is a
# fossil: the site-packages copy on this host reported 0.21.13 while pyproject
# said 0.21.20, and a stale .dist-info will happily report a number matching
# nothing on disk. Paths cannot lie about where the code came from.
# ---------------------------------------------------------------------------
def pytest_sessionstart(session: pytest.Session) -> None:
    """Abort the run if `import scitex_agent_container` is not this worktree."""
    try:
        from scitex_agent_container._provenance import origin_mismatch
    except ImportError as exc:
        # `origin_mismatch` EXISTS in this checkout. If it cannot be imported,
        # the package that got imported is not this checkout — which is the
        # very thing we are here to detect. Do not let it surface as a bare
        # ImportError; that is loud but unclear, and an older installed copy
        # (site-packages here was FOUR releases stale) is exactly the case.
        import scitex_agent_container

        raise RuntimeError(
            "\n=================== WRONG PACKAGE UNDER TEST ===================\n"
            "`import scitex_agent_container` resolved to a package that does\n"
            "not even contain `_provenance.origin_mismatch` — so it cannot be\n"
            "this checkout, and this run would test code you did not write.\n"
            f"\n  imported from : {getattr(scitex_agent_container, '__file__', '?')}\n"
            f"  expected under: {Path(_PROJECT_ROOT).resolve() / 'src'}\n"
            f"\n  underlying    : {exc}\n"
            '\nFix: `pythonpath = ["src"]` under [tool.pytest.ini_options].\n'
            "===============================================================\n"
        ) from exc

    error = origin_mismatch(_PROJECT_ROOT)
    if error is not None:
        raise RuntimeError(error)


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


# ---------------------------------------------------------------------------
# THE FLOOR MUST BE ABLE TO DETECT ITS OWN BREACH.
#
# Everything above sets the env EARLY so the three import-time constants are
# born inside the sandbox. Nothing above notices when a test MOVES one back
# out — and a test can, trivially: `importlib.reload()` re-derives the constant
# from whatever the env says AT THAT MOMENT, so any teardown that drops the env
# var and only THEN reloads re-pins the constant at the operator's real
# ``$HOME/.scitex/agent-container/runtime`` for the REST of that xdist worker's
# session. Every later test in that worker keeps passing while writing outside
# the floor, because passing was never conditional on where the bytes landed.
#
# That is not hypothetical. Measured on PR #784, all three matrix legs died on
# a Spartan runner with
#
#   OSError: [Errno 122] Disk quota exceeded:
#     '/home/ywatanabe/.scitex/agent-container/runtime/alpha/instance_id.tmp'
#
# and the same escape had produced a FileNotFoundError on that path earlier.
# Worse than CI: a live fleet agent really is named `alpha`, so the suite was
# contending with a running agent for its own state files.
#
# So the floor gets an alarm. After EVERY test, re-read all three constants and
# fail loudly — naming the test — if any of them no longer resolves inside
# ``_SAC_STATE_FLOOR``. The modules are imported LAZILY here, inside the
# finalizer, on purpose: binding them at collection time would capture a stale
# reference and we would be asserting about a copy of the value rather than the
# value the next test is about to use.
#
# ``sys.modules.get`` (not ``import_module``) so this fixture never IMPORTS a
# module the run had not already loaded — a check that changes what it measures
# is not a check.
#
# ORDERING IS LOAD-BEARING: ``_isolate_state_db`` below legitimately points
# ``DEFAULT_DB_PATH`` at a per-test tmp db and restores it on teardown, so this
# assertion has to run AFTER that restore. Fixture finalization is LIFO, so
# this fixture must be SET UP FIRST — which is why ``_isolate_state_db``
# requests it by name rather than relying on declaration order.
# ---------------------------------------------------------------------------

_STATE_FLOOR_CONSTANTS = (
    ("scitex_agent_container._state.state_db", "DEFAULT_DB_PATH"),
    ("scitex_agent_container._state.registry", "REGISTRY_DIR"),
    ("scitex_agent_container._runners._session_state", "DEFAULT_STATE_ROOT"),
)


@pytest.fixture(autouse=True, scope="function")
def _assert_state_floor_intact(request: pytest.FixtureRequest) -> Iterator[None]:
    """Fail the test that moves an import-time state constant off the floor."""
    yield

    import sys

    floor = _SAC_STATE_FLOOR.resolve()
    breaches: list[str] = []
    for module_path, attr in _STATE_FLOOR_CONSTANTS:
        module = sys.modules.get(module_path)
        if module is None:
            continue
        value = getattr(module, attr, None)
        if value is None:
            continue
        resolved = Path(value).resolve()
        if resolved != floor and floor not in resolved.parents:
            breaches.append(f"  {module_path}.{attr}\n      -> {resolved}")

    # The card board is checked by ASKING THE RESOLVER, not by reading a
    # constant: `scitex_cards._db.resolve_db_path()` is a function evaluated at
    # every write, so the only honest question is the one the dual-write mirror
    # itself asks -- "where would a card write land RIGHT NOW". Reading an env
    # var here would re-implement its precedence chain and could agree with
    # itself while disagreeing with production.
    #
    # scitex-cards is an OPTIONAL peer (see test__rename_cards.py's
    # importorskip), so its absence is not a breach -- but it must be a real
    # ImportError, never a silently swallowed one.
    try:
        from scitex_cards._db import resolve_db_path
    except ImportError:
        resolve_db_path = None
    if resolve_db_path is not None:
        card_db = Path(resolve_db_path()).resolve()
        if card_db != floor and floor not in card_db.parents:
            breaches.append(f"  scitex_cards._db.resolve_db_path()\n      -> {card_db}")

    if breaches:
        raise AssertionError(
            "\n=============== SAC STATE FLOOR BREACHED ===============\n"
            f"After `{request.node.nodeid}` these state locations no longer\n"
            "resolve under the per-worker sandbox floor:\n\n"
            + "\n".join(breaches)
            + f"\n\n  floor: {floor}\n\n"
            "Everything from here on in this xdist worker will keep PASSING\n"
            "while writing to the operator's REAL state -- that is precisely\n"
            "how this went unseen: nothing was asserting on where the bytes\n"
            "landed. It has cost three CI legs (Errno 122 on a live agent's\n"
            "runtime dir) and ~2,772 cards off the live board.\n\n"
            "If a MODULE CONSTANT moved: the teardown re-derived it from an\n"
            "env var it had ALREADY dropped. RESTORE the env var BEFORE the\n"
            "final `importlib.reload(...)`, not after. Working examples:\n"
            "  tests/scitex_agent_container/_listen/test__agent_delete.py\n"
            "  tests/scitex_agent_container/_runners/test_claude_session.py\n"
            "Or register the module with the `env_save_restore` fixture:\n"
            "  env_save_restore.reload_after_restore(module)\n\n"
            "If the CARD BOARD moved: something cleared or overrode\n"
            "$SCITEX_CARDS_DB. Note the dual-write mirror resolves that path\n"
            "ITSELF at every write and RECONCILES (deletes cards absent from\n"
            "the doc), so a tmp store plus a real DB path does not merely\n"
            "pollute the board -- it DESTROYS it.\n"
            "=======================================================\n"
        )


# scope="function" is SPELLED OUT (it is also pytest's default) because the
# whole fix depends on it, and a future "let's not rebuild the DB 4900 times"
# optimisation to scope="session"/"module" would silently REINTRODUCE the bug:
# `claim_port` never releases, so any db shared across tests re-accumulates
# rows until [19000, 19999] is exhausted mid-run. Per-TEST or it does not work.
@pytest.fixture(autouse=True, scope="function")
def _isolate_state_db(
    tmp_path_factory: pytest.TempPathFactory,
    _assert_state_floor_intact: None,
) -> Iterator[Path]:
    """Point this test's state.db at a private tmp file. Restores on teardown.

    Requests ``_assert_state_floor_intact`` purely for ORDERING: that makes the
    floor assertion set up FIRST and therefore (LIFO) finalize LAST, so it
    observes ``DEFAULT_DB_PATH`` after the restore below rather than while this
    fixture still has it pointed at a tmp db.
    """
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


# ---------------------------------------------------------------------------
# sac event log isolation
# ---------------------------------------------------------------------------

_EVENT_LOG_KEY = "SAC_EVENT_LOG"
_event_log_seq = itertools.count()


# Same shape and the same reason as `_isolate_state_db` above. sac's alarm
# rails record to an append-only event log whose path is resolved PER CALL
# from this env var, and several of them default ON (the worktree GC alarms
# under `--apply`; the reconcile and auth-heal passes record every pass). Any
# test that exercises one of those paths without this fixture appends to the
# OPERATOR'S REAL LOG — quietly, because the rail is deliberately fail-open.
#
# Per-TEST, not per-session: the rails also keep small "currently degraded"
# state files BESIDE the log, so a shared log would let one test's remembered
# degraded subject suppress the next test's degraded record — a cross-test
# dependency that would only ever show up as a mystifying order-dependent
# failure under `-p randomly`.
@pytest.fixture(autouse=True, scope="function")
def _isolate_event_log(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Point this test's sac event log at a private tmp file."""
    # Computed but deliberately NOT created: the rail mkdirs its parent on
    # first real write, so a test that records nothing leaves no dir behind.
    log = (
        tmp_path_factory.getbasetemp()
        / "sac-events"
        / f"t{next(_event_log_seq)}"
        / "sac-events.jsonl"
    )
    saved = os.environ.get(_EVENT_LOG_KEY)
    os.environ[_EVENT_LOG_KEY] = str(log)
    try:
        yield log
    finally:
        if saved is None:
            os.environ.pop(_EVENT_LOG_KEY, None)
        else:
            os.environ[_EVENT_LOG_KEY] = saved

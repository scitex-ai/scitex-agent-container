"""Auto-grant ``<self> → lead`` on every ``agent_start`` (OP-PRIO-1).

Bug the change fixes: a previous container died WITHOUT going through
``agent_stop`` (kernel OOM, host reboot, ``kill -9``). The ACL grant
``<self> → lead`` had been added manually by the operator
(``sac a2a grant <agent> lead``) earlier in the same db, but the
restart pathway never refreshed it. When state.db was rebuilt from a
fresh snapshot or the original grant row was lost, ``lead`` could no
longer drive the agent until the operator re-ran ``sac a2a grant`` by
hand. Pinning the grant write inside ``record_local_instance`` means
EVERY successful start refreshes it — and because :func:`grant_send`
is idempotent, repeat starts do not duplicate the row.

Tests use a real on-disk SQLite state.db (isolated per test via the
``SCITEX_AGENT_CONTAINER_STATE_DB`` env override) and a real runtime
stub exposing ``_state_dir`` — no mocks, no monkeypatch.
"""

from __future__ import annotations

from tests.scitex_agent_container._helpers.explicit_spec import explicitize_yaml

import importlib
import os
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container.config import AgentConfig


@pytest.fixture(autouse=True)
def _instances_store(pg_schema: str):
    """A throwaway ``instances`` store for every test in this file.

    ``instances`` moved to the shared PostgreSQL store on 2026-08-28 and the
    verbs driven here read ``list_active_instances`` on every path, so the
    dependency belongs to the VERB rather than to any one case. Autouse
    rather than per-signature for that reason, and for one more: it keeps a
    NEW test in this file from silently resolving whatever store the process
    happens to point at.
    """
    yield


@pytest.fixture
def db_path(tmp_path: Path) -> Iterator[Path]:
    """Per-test on-disk state.db, exported via env (save/restore).

    ``state_db`` reads ``SCITEX_AGENT_CONTAINER_STATE_DB`` at import into
    a module-level ``DEFAULT_DB_PATH``; reload after setting the env so
    every helper (including ``has_grant`` / ``open_db``) lands in the
    temp DB.
    """
    p = tmp_path / "state.db"
    key = "SCITEX_AGENT_CONTAINER_STATE_DB"
    saved = os.environ.get(key)
    os.environ[key] = str(p)
    import scitex_agent_container._state.state_db as mod

    importlib.reload(mod)
    try:
        yield p
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved
        importlib.reload(mod)


class _RuntimeStub:
    """Honest runtime collaborator — only the ``_state_dir`` resolver
    that ``_instances`` calls. Mirrors ApptainerContainerRuntime's API."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _state_dir(self, config: AgentConfig) -> Path:
        return self._root / config.name


# ---------------------------------------------------------------------------
# Happy path — first start grants self → lead
# ---------------------------------------------------------------------------


def test_record_local_instance_grants_self_to_lead(pg_schema: str, 
    db_path: Path, tmp_path: Path
) -> None:
    # Arrange
    from scitex_agent_container._lifecycle._instances import record_local_instance
    from scitex_agent_container._state.state_db_nodes import has_grant

    cfg = AgentConfig(name="grant-1", runtime="apptainer")
    # Act
    record_local_instance(cfg, _RuntimeStub(tmp_path))
    # Assert
    assert has_grant(sender="grant-1", target="lead") is True


# ---------------------------------------------------------------------------
# Idempotency — repeat starts do not duplicate the comms_grants row
# ---------------------------------------------------------------------------


def test_record_local_instance_grant_to_lead_is_idempotent(
    pg_schema: str, db_path: Path, tmp_path: Path
) -> None:
    """A repeat start must not RE-STAMP the grant it already holds.

    This used to COUNT rows through ``open_db`` and raw SQL against the
    SQLite ``comms_grants`` table. That table is abandoned — and worse, the
    query did not fail: the vestigial DDL is still in ``_SCHEMA_REGISTRY``,
    so the count came back 0 from a table nothing writes any more, which is
    a silent wrong answer rather than a loud one.

    A COUNT is also near-tautological now: the store's identity is
    (sender_name, target_name), so a duplicate row is structurally
    impossible and the assertion would hold even if grant_send were broken.
    The property worth pinning is the documented one — re-granting a LIVE
    pair leaves the row untouched and does NOT bump created_at — which an
    implementation genuinely can break.
    """
    # Arrange — two successive starts simulate a crash-recover loop.
    #
    # The precondition (the FIRST start wrote exactly one grant) is checked
    # with an explicit raise rather than a second ``assert``: it is setup
    # validation, not a second clause of the contract under test, and
    # STX-TQ007 counts asserts precisely so that a failing first one cannot
    # hide a later contract check. Keeping it as a raise says which of the
    # two it is.
    from scitex_agent_container._lifecycle._instances import record_local_instance
    from scitex_agent_container._state.state_db_nodes import list_comms_grants

    cfg = AgentConfig(name="grant-2", runtime="apptainer")
    rt = _RuntimeStub(tmp_path)
    record_local_instance(cfg, rt)
    first = [
        r for r in list_comms_grants()
        if (r["sender"], r["target"]) == ("grant-2", "lead")
    ]
    if len(first) != 1:
        raise RuntimeError(
            "arrange failed: the first start wrote "
            f"{len(first)} grant row(s), expected exactly 1"
        )
    stamped = first[0]["created_at"]
    # Act
    record_local_instance(cfg, rt)
    # Assert — still one row, and its timestamp did not move.
    rows = [
        r for r in list_comms_grants()
        if (r["sender"], r["target"]) == ("grant-2", "lead")
    ]
    assert [r["created_at"] for r in rows] == [stamped]


# ---------------------------------------------------------------------------
# Happy path sanity — grant write does not break record_local_instance's
# documented return contract (instance id string).
# ---------------------------------------------------------------------------


def test_record_local_instance_returns_instance_id_when_grant_write_succeeds(pg_schema: str, 
    db_path: Path, tmp_path: Path
) -> None:
    # Arrange
    from scitex_agent_container._lifecycle._instances import record_local_instance

    cfg = AgentConfig(name="grant-3", runtime="apptainer")
    # Act
    instance_id = record_local_instance(cfg, _RuntimeStub(tmp_path))
    # Assert — record_local_instance documents ``str | None``; on the
    # happy path with a writeable state.db it MUST return the id string.
    assert isinstance(instance_id, str)


# ---------------------------------------------------------------------------
# REGRESSION (2026-07-14) — the auto-grant must not escape into a FOREIGN
# state.db.
#
# ``agent_start`` runs ``health_monitor`` on a DAEMON THREAD and hands it a
# restart callback -> ``restart_and_record`` -> ``record_local_instance`` ->
# ``grant_send(target="lead")``. That thread OUTLIVES the call that made it
# (it wakes after ``health.interval`` + a restart backoff, ~90 s with the
# shipped defaults).
#
# The callback used to take no ``db_path``, so each write re-resolved
# ``state_db.DEFAULT_DB_PATH`` -- a MUTABLE PROCESS-GLOBAL -- at the moment
# the monitor fired. Whatever the process then called "default" got the row:
#
#   * under pytest, a LATER, UNRELATED test's isolated tmp DB. That is the
#     stray ``target='lead'`` grant that made
#     cli_pkg/test_a2a_group.py's ``grants`` tests fail intermittently in
#     full-suite runs while passing when run alone.
#   * on a bare host with nothing redirecting the global, the LIVE FLEET
#     state.db (``alpha``/``beta``/``zombie`` -> ``lead`` rows were found in
#     production, written by this suite).
#
# The thread is captured rather than started: waiting ~90 s of real backoff
# would make the test slow AND flaky, and the point under test is WHICH DB
# the callback writes to, not that Python can run a thread. ``thread_factory``
# is a first-class injectable seam on ``agent_start`` (production default:
# ``threading.Thread``), already used this way by test_lifecycle.py.
# ---------------------------------------------------------------------------


class _CapturingThread:
    """Honest stand-in for ``threading.Thread``: records target + args and
    never spawns a real thread, so the monitor's restart callback can be
    fired deterministically instead of waited on."""

    def __init__(self, *, target=None, args=(), daemon=False, **_kw) -> None:
        self.target = target
        self.args = args
        self.daemon = daemon
        self.started = False

    def start(self) -> None:
        self.started = True


class _DeadRuntime:
    """Runtime that reports the agent as never running, so the health
    monitor's restart path is the one under test."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self.start_calls: list[str] = []

    def is_running(self, config: AgentConfig, **_kw) -> bool:
        return False

    def start(self, config: AgentConfig, **_kw) -> bool:
        self.start_calls.append(config.name)
        return True

    def stop(self, config: AgentConfig, **_kw) -> bool:
        return True

    def _state_dir(self, config: AgentConfig) -> Path:
        return self._root / config.name


def _write_health_spec(tmp_path: Path, name: str) -> Path:
    """v3 ``spec.yaml`` with health ENABLED (so agent_start spawns the
    monitor). Dir-as-SSoT: the agent name comes from the parent dir."""
    agent_dir = tmp_path / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    spec = agent_dir / "spec.yaml"
    spec.write_text(
        explicitize_yaml("apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "spec:\n"
        "  runtime: apptainer\n"
        "  host: ${HOSTNAME}\n"
        f"  workdir: {tmp_path / 'work'}\n"
        "  apptainer:\n    image: /x.sif\n    binds: []\n"
        "  health:\n    enabled: true\n    interval: 60\n"
        "  restart:\n    policy: on-failure\n    max_retries: 3\n"
        "  claude:\n    model: sonnet\n")
    )
    return spec


def _fire_monitor_restart_against_foreign_db(
    db_path: Path, tmp_path: Path, name: str
) -> tuple[Path, set[str]]:
    """Arrange + Act: start a health-monitored agent against ``db_path``,
    then move the process-global default to a FOREIGN db and fire the
    monitor's restart callback (what the leaked daemon thread does on its
    next tick).

    Returns ``(foreign_db_path, instance_ids_in_db_path_before_restart)``.
    The second element exists so a caller can tell "the write went to the
    right place" apart from "the write was lost entirely" — bare absence
    from the foreign db is not a positive control, because ``agent_start``
    has already written a row to ``db_path`` before the callback fires.
    """
    from scitex_agent_container._lifecycle import lifecycle as lc
    from scitex_agent_container._state import state_db
    from scitex_agent_container._state.registry import Registry

    created: list[_CapturingThread] = []

    def _thread_factory(**kwargs) -> _CapturingThread:
        t = _CapturingThread(**kwargs)
        created.append(t)
        return t

    lc.agent_start(
        str(_write_health_spec(tmp_path, name)),
        registry=Registry(registry_dir=tmp_path / "reg"),
        runtime_factory=lambda _c: _DeadRuntime(tmp_path),
        sleep_fn=lambda _s: None,
        thread_factory=_thread_factory,
    )
    assert created and created[0].started, "agent_start did not start a monitor"
    restart_cb = created[0].args[3]
    config = created[0].args[1]

    # An unrelated LATER test isolates itself: the process-global moves.
    from scitex_agent_container._state.state_db_instances import list_active_instances

    before = {r["id"] for r in list_active_instances()}

    foreign = tmp_path / "foreign" / "state.db"
    state_db.init_schema(foreign)
    saved = state_db.DEFAULT_DB_PATH
    state_db.DEFAULT_DB_PATH = foreign
    try:
        restart_cb(config)  # the leaked monitor thread fires
    finally:
        state_db.DEFAULT_DB_PATH = saved
    return foreign, before


def test_monitor_restart_does_not_record_the_instance_into_a_foreign_state_db(
    pg_schema: str,
    db_path: Path, tmp_path: Path
) -> None:
    """The 2026-07-14 regression, measured where it is still measurable.

    A leaked monitor DAEMON THREAD followed a mutable process-global instead
    of the store its agent started against, and wrote into an unrelated db.

    This test used to assert that on ``comms_grants``. It cannot any more:
    comms_grants is one shared PostgreSQL store addressed by
    SCITEX_STORE_DSN, so there is no second grants store to be foreign TO,
    and the migrated assertion (``list_comms_grants() == []``) read the only
    store there is — flatly contradicting its sibling below, which asserts a
    grant IS present in that same store after the same drive.

    The mechanism did NOT migrate: ``record_local_instance`` still writes the
    ``instances`` row through ``db_path`` and the monitor still pins
    DEFAULT_DB_PATH, both SQLite. So the property is asserted there instead
    of being faked, deleted, or quietly rewritten into something that passes.
    """
    # Arrange
    from scitex_agent_container._state.state_db_instances import list_active_instances

    name = "pinned-1"
    # Act — the leaked monitor thread fires while the global points elsewhere.
    foreign, before = _fire_monitor_restart_against_foreign_db(db_path, tmp_path, name)
    # Assert — the unrelated db MUST be untouched. Pre-fix it held the stray
    # row. The second half is the positive control: the write must have gone
    # SOMEWHERE, so the agent's own db must show a NEW instance id — absence
    # from `foreign` alone would also pass if the write vanished entirely.
    stray = [r for r in list_active_instances() if r["name"] == name]
    after = {r["id"] for r in list_active_instances()}
    assert (stray, after != before) == ([], True)


def test_monitor_restart_callback_still_auto_grants_self_to_lead(
    pg_schema: str, db_path: Path, tmp_path: Path
) -> None:
    """RENAMED, because the old name claimed a property it can no longer test.

    It was `..._auto_grants_into_the_db_the_agent_started_against`, which
    asserted PINNING. Against one shared PostgreSQL store that assertion
    cannot fail — there is no other store the grant could have landed in — so
    under the old name this was a permanently-green test certifying something
    it does not check. It was not in the failure list, so nothing would have
    forced anyone to look at it.

    What it still legitimately covers: the restart callback reaches
    grant_send at all, i.e. the write is not LOST. Kept under a name that
    says only that.
    """
    # Arrange
    from scitex_agent_container._state.state_db_nodes import has_grant

    name = "pinned-2"
    # Act
    _fire_monitor_restart_against_foreign_db(db_path, tmp_path, name)
    # Assert
    assert has_grant(sender=name, target="lead") is True

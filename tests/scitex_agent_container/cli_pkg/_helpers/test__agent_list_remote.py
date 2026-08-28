"""Tests for the REMOTE-instance read side of ``sac agents list``.

The master ssh-dispatches an agent (spec ``host: spartan``) and already records
an ``instances`` row (``host=<peer>``, ``remote=1``, ``pid=NULL``) on dispatch,
tombstoning it on stop. This suite pins the newly-added READ side:
``get_agent_list_data`` / ``remote_instance_rows`` surface that row as
**running on host <peer>** — with a LIVE ssh probe deciding the status — and the
gc reaper's ``remote=0`` guard never tombstones it locally.

No-mocks, per the repo's STX-TQ rules:

* real on-disk ``state.db`` via the ``isolated_state_db`` fixture (env override +
  module reload — the exact pattern the ``_stale_lease`` / lifecycle suites use),
* all ``instances`` state through the real ``record_instance_start`` /
  ``record_instance_stop`` / ``gc_dead_instances``,
* a real empty ``Registry(tmp_path)``,
* the cross-host probe driven by an injected ``run_ssh`` / ``status_probe``
  (real rc-returning / status-returning callables — never a shell-out, never a
  ``MagicMock``),
* real ``spec.yaml`` files exercised by the real ``load_config`` for labels.

One logical assertion per test (STX-TQ007); AAA markers throughout.
"""

from __future__ import annotations

import importlib
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

import scitex_agent_container.cli_pkg._helpers._agent_list as _al
from scitex_agent_container._state.registry import Registry
from scitex_agent_container.cli_pkg._helpers import is_live_status
from scitex_agent_container.cli_pkg._helpers._agent_list import (
    LocalProbe,
    get_agent_list_data,
    remote_instance_rows,
)
from scitex_agent_container.cli_pkg._helpers._agent_list_discover import (
    _default_remote_status_probe,
    _probe_remote_statuses,
)

# ---------------------------------------------------------------------------
# Fixtures + seams (copied from the sibling suites — real callables, no mocks).
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_state_db(tmp_path: Path, pg_schema: str) -> Iterator[Path]:
    """Per-test on-disk state.db, exported via env (explicit save/restore).

    ``state_db`` reads ``SCITEX_AGENT_CONTAINER_STATE_DB`` at import into a
    module-level ``DEFAULT_DB_PATH``; reload it after setting the env so the
    ``instances`` writes land in the temp DB, not the developer's real
    ``~/.scitex`` tree. Mirrors ``_lifecycle/test__stale_lease.py``.
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


@pytest.fixture(autouse=True)
def _pin_spartan_peer(tmp_path: Path) -> Iterator[None]:
    """Pin a REAL temp sac config.yaml with a ``spartan`` login-node peer.

    Part 2 routes the remote probe through ``build_ssh_argv``, which raises for
    an UNKNOWN peer (→ UNKNOWN, never a false DEAD). Every remote row in this
    suite lives on host ``spartan``; make it a real, resolvable peer via the
    documented ``SCITEX_AGENT_CONTAINER_CONFIG`` override so the probe's
    rc→status mapping is actually exercised (not short-circuited to UNKNOWN by
    an unresolvable peer). Real config file + real loader — no mock; the env is
    saved/restored explicitly (no ``monkeypatch``). ``SAC_SSH_CONTROL_MASTER=0``
    keeps the rendered argv deterministic.
    """
    cfg = tmp_path / "sac-config.yaml"
    cfg.write_text("peers:\n  spartan: { ssh: ywatanabe@spartan-login }\n")
    env = {
        "SCITEX_AGENT_CONTAINER_CONFIG": str(cfg),
        "SAC_SSH_CONTROL_MASTER": "0",
    }
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextmanager
def _swap_discover(
    impl: Callable[[], list[tuple[str, Path]]],
) -> Iterator[None]:
    """Swap ``_al._discover_defined_agents`` for a real callable."""
    saved = _al._discover_defined_agents
    _al._discover_defined_agents = impl  # type: ignore[assignment]
    try:
        yield
    finally:
        _al._discover_defined_agents = saved  # type: ignore[assignment]


@contextmanager
def _swap_probe(impl: Callable[[Any], Any]) -> Iterator[None]:
    """Swap the LOCAL liveness probe (both module + parent-package re-export).

    Keyed on ``probe_local_detail``, which is what the pool resolves since
    the probe began recording which adapter answered. Left on the old
    ``_probe_local`` name this swap would simply stop being consulted, and
    the tests below would keep passing without exercising anything.
    """
    import scitex_agent_container.cli_pkg._helpers as _pkg

    saved_al = _al.probe_local_detail
    saved_pkg = getattr(_pkg, "probe_local_detail", None)
    _al.probe_local_detail = impl  # type: ignore[assignment]
    _pkg.probe_local_detail = impl  # type: ignore[assignment]
    try:
        yield
    finally:
        _al.probe_local_detail = saved_al  # type: ignore[assignment]
        if saved_pkg is None:
            delattr(_pkg, "probe_local_detail")
        else:
            _pkg.probe_local_detail = saved_pkg  # type: ignore[assignment]


def _running(value: bool | None, runtime: str = "TestRuntime"):
    """A probe callable answering ``value`` — the shape the pool consumes."""

    def impl(cfg):
        return LocalProbe(running=value, runtime=runtime, error=None)

    return impl


def _no_discover() -> list[tuple[str, Path]]:
    """Return [] — no on-disk agents interfere with the instances-row merge."""
    return []


def _write_valid_spec(
    dir_: Path, *, machine: str | None = None, account: str | None = None
) -> Path:
    """Write a minimal real v3 spec.yaml.

    ``machine`` adds a ``metadata.labels.machine`` label; ``account`` adds a
    ``spec.claude.account`` pin (so ``_safe_account_for`` resolves a
    deterministic, non-empty spec-derived label — Part 3).
    """
    dir_.mkdir(parents=True, exist_ok=True)
    spec = dir_ / "spec.yaml"
    lines = ["apiVersion: scitex-agent-container/v3", "kind: Agent"]
    if machine is not None:
        lines += ["metadata:", "  labels:", f'    machine: "{machine}"']
    else:
        lines.append("metadata: {}")
    lines += [
        "spec:",
        "  runtime: apptainer",
        "  host: ${HOSTNAME}",
        "  workdir: /home/agent/work",
        "  apptainer:",
        "    image: /x.sif",
        "    binds: []",
        "  claude:",
        "    model: sonnet",
    ]
    if account is not None:
        lines.append(f"    account: {account}")
    lines += [
        "  health:",
        "    enabled: true",
        "    interval: 60",
        "  restart:",
        "    policy: on-failure",
        "    max_retries: 3",
    ]
    from tests.scitex_agent_container._helpers.explicit_spec import (
        explicitize_yaml,
    )

    # Red-start ruling 2026-07-21: every field explicit (body wins).
    spec.write_text(explicitize_yaml("\n".join(lines) + "\n"))
    return spec


# ---------------------------------------------------------------------------
# Drive helpers — real record_instance_start + the two entry points.
# ---------------------------------------------------------------------------


def _record_remote(
    name: str = "spartan-dev", host: str = "spartan", a2a: int = 8123
) -> str:
    """Record a cross-host (remote=1) active instances row; return its id."""
    from scitex_agent_container._state.state_db import record_instance_start

    return record_instance_start(name=name, host=host, a2a_port=a2a, remote=True)


def _list_rows(
    tmp_path: Path,
    *,
    run_ssh: Callable[[list[str]], int] = lambda argv: 0,
    discover: Callable[[], list[tuple[str, Path]]] = _no_discover,
    registry: Registry | None = None,
    **kw: Any,
) -> list[dict]:
    """Drive ``get_agent_list_data`` with an injected (never-shell-out) ssh
    probe. ``run_ssh`` default returns rc 0 (ALIVE -> running)."""
    reg = registry or Registry(registry_dir=tmp_path / "reg")
    with _swap_discover(discover):
        return get_agent_list_data(reg, remote_run_ssh=run_ssh, **kw)


def _remote_rows_direct(
    *,
    run_ssh: Callable[[list[str]], int],
    discover: Callable[[], list[tuple[str, Path]]] = _no_discover,
) -> list[dict]:
    """Drive ``remote_instance_rows`` straight (real instances oracle)."""
    with _swap_discover(discover):
        return remote_instance_rows(
            registered=set(),
            display_host="master-host",
            port_claims={},
            run_ssh=run_ssh,
        )


def _spartan_row(rows: list[dict]) -> dict:
    return next(r for r in rows if r["name"] == "spartan-dev")


# ---------------------------------------------------------------------------
# 1. A remote-dispatched agent surfaces as running-on-peer (probe ALIVE).
# ---------------------------------------------------------------------------


def test_remote_dispatched_agent_surfaces_exactly_once(isolated_state_db, tmp_path):
    # Arrange
    _record_remote()
    # Act
    rows = _list_rows(tmp_path)
    # Assert — surfaces AND is not duplicated as a defined/local row.
    assert [r["name"] for r in rows].count("spartan-dev") == 1


def test_remote_row_status_is_running_when_probe_alive(isolated_state_db, tmp_path):
    # Arrange
    _record_remote()
    # Act — rc 0 == tmux session up on the peer == ALIVE.
    rows = _list_rows(tmp_path, run_ssh=lambda argv: 0)
    # Assert
    assert _spartan_row(rows)["status"] == "running"


def test_remote_row_reports_the_peer_host(isolated_state_db, tmp_path):
    # Arrange
    _record_remote()
    # Act
    rows = _list_rows(tmp_path)
    # Assert
    assert _spartan_row(rows)["host"] == "spartan"


def test_remote_row_host_display_is_the_peer(isolated_state_db, tmp_path):
    # Arrange
    _record_remote()
    # Act
    rows = _list_rows(tmp_path)
    # Assert — a concrete peer host passes through the display mapping unchanged.
    assert _spartan_row(rows)["host_display"] == "spartan"


def test_remote_row_carries_the_bound_a2a_port(isolated_state_db, tmp_path):
    # Arrange
    _record_remote(a2a=8123)
    # Act
    rows = _list_rows(tmp_path)
    # Assert
    assert _spartan_row(rows)["a2a_port"] == 8123


def test_remote_row_is_flagged_remote(isolated_state_db, tmp_path):
    # Arrange
    _record_remote()
    # Act
    rows = _list_rows(tmp_path)
    # Assert
    assert _spartan_row(rows)["remote"] is True


def test_remote_row_is_not_emitted_as_defined_local(isolated_state_db, tmp_path):
    # Arrange
    _record_remote()
    # Act
    rows = _list_rows(tmp_path)
    # Assert — the only row for the name is the remote one (host != "local").
    matching = [r for r in rows if r["name"] == "spartan-dev"]
    assert len(matching) == 1 and matching[0]["host"] == "spartan"


# ---------------------------------------------------------------------------
# 2. LIVE probe verdict -> status (via remote_process_signal + injected run_ssh).
# ---------------------------------------------------------------------------


def test_remote_probe_dead_maps_to_stopped(isolated_state_db):
    # Arrange — rc 1 == ssh connected, peer tmux has NO session == DEAD.
    _record_remote()
    # Act
    rows = _remote_rows_direct(run_ssh=lambda argv: 1)
    # Assert
    assert _spartan_row(rows)["status"] == "stopped"


def test_remote_probe_unknown_maps_to_unknown(isolated_state_db):
    # Arrange — rc 255 == wedged/auth/bare-PATH/broken-ProxyJump ssh == UNKNOWN.
    _record_remote()
    # Act
    rows = _remote_rows_direct(run_ssh=lambda argv: 255)
    # Assert — Part 1: an un-probed peer reads "unknown" (hidden from the default
    # view but counted in the footer), NOT a comforting false "running".
    assert _spartan_row(rows)["status"] == "unknown"


# ---------------------------------------------------------------------------
# 3. Precedence: a LOCAL registry row wins over the remote instances row.
# ---------------------------------------------------------------------------


def test_local_registry_row_wins_over_remote_instance(isolated_state_db, tmp_path):
    # Arrange — same name registered locally AND active as a remote instance.
    spec = _write_valid_spec(tmp_path / "spartan-dev")
    registry = Registry(registry_dir=tmp_path / "reg")
    registry.add("spartan-dev", str(spec), "tui-spartan-dev")
    _record_remote()
    # Act
    with _swap_discover(_no_discover), _swap_probe(_running(True)):
        rows = get_agent_list_data(registry, remote_run_ssh=lambda argv: 0)
    # Assert — one row, and it is the local one.
    matching = [r for r in rows if r["name"] == "spartan-dev"]
    assert len(matching) == 1 and matching[0]["host"] == "local"


# ---------------------------------------------------------------------------
# 4. A tombstoned (stopped) remote row disappears from the list.
# ---------------------------------------------------------------------------


def test_tombstoned_remote_row_disappears(isolated_state_db, tmp_path):
    # Arrange
    from scitex_agent_container._state.state_db import record_instance_stop

    instance_id = _record_remote()
    record_instance_stop(instance_id)
    # Act
    rows = _list_rows(tmp_path)
    # Assert
    assert [r for r in rows if r["name"] == "spartan-dev"] == []


# ---------------------------------------------------------------------------
# 5. The gc reaper never tombstones a remote row (the remote=0 guard).
# ---------------------------------------------------------------------------


def test_reaper_leaves_remote_row_active(isolated_state_db):
    # Arrange
    from scitex_agent_container._state.state_db import (
        gc_dead_instances,
        list_active_instances,
    )

    _record_remote()
    # Act — a full sweep must not touch the cross-host row.
    gc_dead_instances(dry_run=False)
    # Assert
    active = [r for r in list_active_instances(host=None) if r["name"] == "spartan-dev"]
    assert len(active) == 1


def test_reaper_remote_guard_keeps_row_with_forced_stale_heartbeat(isolated_state_db):
    # Arrange — force a long-stale last_heartbeat_at onto the remote row; only
    # the ``AND remote=0`` guard keeps the heartbeat sweep from reaping it.
    from scitex_agent_container._state.state_db import (
        gc_dead_instances,
        list_active_instances,
        open_db,
    )

    instance_id = _record_remote()
    with open_db() as conn:
        conn.execute(
            "UPDATE instances SET last_heartbeat_at=? WHERE id=?",
            ("2000-01-01T00:00:00Z", instance_id),
        )
    # Act — a 1s staleness cutoff would trip the sweep but for the guard.
    gc_dead_instances(dry_run=False, heartbeat_stale_seconds=1)
    # Assert
    active = [r for r in list_active_instances(host=None) if r["name"] == "spartan-dev"]
    assert len(active) == 1


# ---------------------------------------------------------------------------
# 6. The default RUNNING-ONLY view retains a running remote row.
# ---------------------------------------------------------------------------


def test_running_only_view_retains_running_remote_row(isolated_state_db, tmp_path):
    # Arrange
    _record_remote()
    # Act
    rows = _list_rows(tmp_path, running_only=True, run_ssh=lambda argv: 0)
    # Assert
    matching = [r for r in rows if r["name"] == "spartan-dev"]
    assert len(matching) == 1 and matching[0]["status"] == "running"


# ---------------------------------------------------------------------------
# 7. Label filters (machine) include / exclude the remote row correctly.
# ---------------------------------------------------------------------------


def test_machine_label_includes_matching_remote_row(isolated_state_db, tmp_path):
    # Arrange — spec on disk carries machine="spartan"; the instances row is remote.
    spec = _write_valid_spec(tmp_path / "spartan-dev", machine="spartan")
    _record_remote()

    def _disc() -> list[tuple[str, Path]]:
        return [("spartan-dev", spec)]

    # Act
    rows = _list_rows(tmp_path, machine="spartan", discover=_disc)
    # Assert
    assert any(r["name"] == "spartan-dev" and r.get("remote") for r in rows)


def test_machine_label_excludes_non_matching_remote_row(isolated_state_db, tmp_path):
    # Arrange
    spec = _write_valid_spec(tmp_path / "spartan-dev", machine="spartan")
    _record_remote()

    def _disc() -> list[tuple[str, Path]]:
        return [("spartan-dev", spec)]

    # Act
    rows = _list_rows(tmp_path, machine="nuc", discover=_disc)
    # Assert
    assert [r for r in rows if r["name"] == "spartan-dev"] == []


# ---------------------------------------------------------------------------
# 8. Part 1 — the default ssh probe maps the ternary verdict to a status word,
#    with UNKNOWN → "unknown" (NOT a false "running"). Driven straight through
#    the real ``remote_process_signal`` via an injected rc-returning ``run_ssh``
#    (the ``_pin_spartan_peer`` autouse fixture makes ``spartan`` resolvable).
# ---------------------------------------------------------------------------


def test_default_remote_status_probe_rc0_is_running():
    # Arrange — rc 0 == the peer's tmux HAS the session == ALIVE.
    probe = _default_remote_status_probe({}, run_ssh=lambda argv: 0)
    # Act
    status = probe("spartan-dev", "spartan")
    # Assert
    assert status == "running"


def test_default_remote_status_probe_rc1_is_stopped():
    # Arrange — rc 1 == ssh connected, peer tmux has NO session == DEAD.
    probe = _default_remote_status_probe({}, run_ssh=lambda argv: 1)
    # Act
    status = probe("spartan-dev", "spartan")
    # Assert
    assert status == "stopped"


def test_default_remote_status_probe_rc255_is_unknown():
    # Arrange — rc 255 == wedged/auth ssh == UNKNOWN, NOT a false "running".
    probe = _default_remote_status_probe({}, run_ssh=lambda argv: 255)
    # Act
    status = probe("spartan-dev", "spartan")
    # Assert
    assert status == "unknown"


# ---------------------------------------------------------------------------
# 9. Part 1 — get_agent_list_data surfaces an UNKNOWN remote row as status
#    "unknown": present in the full (-v/--json) data, but excluded by the
#    render layer's own default-view filter (``is_live_status``).
# ---------------------------------------------------------------------------


def test_get_agent_list_data_remote_unknown_probe_status_is_unknown(
    isolated_state_db, tmp_path
):
    # Arrange — an active remote row whose live probe cannot observe the peer.
    _record_remote()
    # Act — full data keeps every row (running_only defers enrichment, not rows).
    rows = _list_rows(tmp_path, run_ssh=lambda argv: 255)
    # Assert
    assert _spartan_row(rows)["status"] == "unknown"


def test_get_agent_list_data_remote_unknown_row_hidden_from_default_view(
    isolated_state_db, tmp_path
):
    # Arrange — an active remote row whose live probe cannot observe the peer.
    _record_remote()
    # Act — the exact predicate print_agent_list applies for the default view.
    rows = _list_rows(tmp_path, run_ssh=lambda argv: 255)
    # Assert — "unknown" is not a live status, so the default view omits it.
    visible = [r["name"] for r in rows if is_live_status(r.get("status"))]
    assert "spartan-dev" not in visible


# ---------------------------------------------------------------------------
# 10. Part 3 — the remote row's Account is derived from the on-disk spec (the
#     SAME spec-derived label ``defined_agent_rows`` uses), killing the bare
#     "—". The remote agent's spec lives on the master's disk (that is how it
#     was ssh-dispatched), so the label is available without a DB migration.
# ---------------------------------------------------------------------------


def test_remote_row_account_is_spec_derived(isolated_state_db, tmp_path):
    # Arrange — a real spec carrying a claude.account pin; discover feeds it.
    from scitex_agent_container.cli_pkg._helpers._agent_list import _safe_account_for
    from scitex_agent_container.config import load_config

    spec = _write_valid_spec(tmp_path / "spartan-dev", account="pool-acct-xyz")
    _record_remote()

    def _disc() -> list[tuple[str, Path]]:
        return [("spartan-dev", spec)]

    expected = _safe_account_for(load_config(str(spec)))
    # Act
    rows = _remote_rows_direct(run_ssh=lambda argv: 0, discover=_disc)
    # Assert — same spec-derived label, and demonstrably non-empty (not "").
    assert _spartan_row(rows)["account"] == expected != ""


# ---------------------------------------------------------------------------
# 11. The remote probe pass spends ONE budget for the whole batch, not one per
#     probe. `future.result(timeout=T)` called once per future in submission
#     order hands each future the FULL budget, so n wedged peers cost about
#     ceil(n/workers)*T -- exactly the serialization _probe_remote_statuses'
#     own docstring promises cannot happen.
#
#     ASSERTED AS A RATIO, DELIBERATELY. The absolute-bound form of this
#     assertion already exists (test_cli.py: `elapsed < 3.0` against a 1s
#     budget) and it is the assertion that goes red under 32-way xdist load --
#     load inflates the measurement while the bound stays put, so the test
#     reports a defect that is not there. A ratio moves both arms together: a
#     loaded machine makes the baseline slow too, and the comparison survives.
#     The two implementations stay far apart either way -- a shared budget puts
#     the batch at ~1x the single-probe cost, a per-future budget at ~16x.
# ---------------------------------------------------------------------------


@contextmanager
def _wedged_probe() -> Iterator[Callable[[str, str], str]]:
    """A real probe callable that blocks until the context exits.

    No mock: this is an ordinary function that waits on a real Event, which is
    what a peer whose ssh never answers looks like from the pool's side.
    """
    release = threading.Event()

    def probe(name: str, host: str) -> str:
        release.wait(30)
        return "running"

    try:
        yield probe
    finally:
        release.set()


def _batch_seconds(
    probe: Callable[[str, str], str], n_peers: int, budget_s: float
) -> float:
    """Wall-clock cost of probing ``n_peers`` wedged peers under one budget."""
    peers = [{"name": f"peer-{i}", "host": "unreachable"} for i in range(n_peers)]
    started = time.monotonic()
    # max_parallel_probes=1 on purpose: a pool wide enough to run the whole
    # batch at once would hide the defect, because the WAITING is what
    # serializes, not the work.
    _probe_remote_statuses(peers, probe, 1, budget_s)
    return time.monotonic() - started


def test_remote_probe_batch_spends_one_budget_not_one_per_peer():
    # Arrange — every probe wedges, so every future must be cut off by the
    # budget rather than by finishing.
    budget_s = 0.3
    n_peers = 16
    with _wedged_probe() as probe:
        # Act — one peer establishes what a single budget costs ON THIS
        # MACHINE, RIGHT NOW; sixteen peers must not cost sixteen of them.
        one_peer = _batch_seconds(probe, 1, budget_s)
        many_peers = _batch_seconds(probe, n_peers, budget_s)
    # Assert
    ratio = many_peers / max(one_peer, 1e-9)
    assert ratio < 5.0, (
        f"{n_peers} wedged peers cost {many_peers:.2f}s against a "
        f"{one_peer:.2f}s single-peer baseline ({ratio:.1f}x). One shared "
        f"batch budget is ~1x; ~{n_peers}x means future.result(timeout=...) "
        "restarts the deadline per future, so the pool's parallelism never "
        "reaches the waiting -- todo#254, remote half."
    )

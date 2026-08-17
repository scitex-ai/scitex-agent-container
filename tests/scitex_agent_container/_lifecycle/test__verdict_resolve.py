"""Liveness signal resolvers, driven against REAL seams.

NO MOCKS (repo doctrine). These drive real files, a real live OS process, a real
REAPED pid, and the real ``TuiSessionRuntime`` — the actual things the resolvers
inspect in production.

The contract under test is one sentence: **a probe that could not run returns
UNKNOWN, never DEAD.** ``False`` and "I could not look" are different facts, and
only one of them may be acted on — because the remedy for DEAD (``--force
--fresh``) destroys the thing it misdiagnosed.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

from scitex_agent_container._lifecycle._verdict import (
    ALIVE,
    DEAD,
    SOURCE_HEARTBEAT,
    SOURCE_PROCESS,
    SOURCE_REGISTRY,
    SOURCE_SCREEN,
    UNKNOWN,
    WEDGED,
)
from scitex_agent_container._lifecycle._verdict_resolve import (
    _tmux_probe_ran,
    heartbeat_signal,
    process_signal,
    registry_signal,
    remote_process_signal,
    screen_signal,
)


class _Cfg:
    """A real minimal config object — the two attributes the resolvers read."""

    def __init__(self, name: str, runtime: str) -> None:
        self.name = name
        self.runtime = runtime


class _RuntimeSaysUp:
    """A real runtime whose probe SUCCEEDS and finds the agent up."""

    def is_running(self, config) -> bool:
        return True


class _RuntimeSaysDown:
    """A real runtime whose probe SUCCEEDS and finds nothing there."""

    def is_running(self, config) -> bool:
        return False


class _RuntimeProbeExplodes:
    """A real runtime whose probe CANNOT RUN (raises) — the UNKNOWN case."""

    def is_running(self, config):
        raise OSError("tmux server is wedged; cannot probe")


def _on_the_host() -> bool:
    """We are NOT in a container, so a pid check is a real sensor.

    Pinned explicitly wherever a resolver bottoms out in ``os.kill(pid, 0)``. A
    pid only means anything in the namespace that minted it, so these tests would
    otherwise return DEAD on a CI runner and UNKNOWN inside a container —
    the same code, two answers, depending on where pytest happened to run. The
    instrument-independence suite covers the in-container half explicitly.
    """
    return False


@pytest.fixture
def live_pid():
    """A REAL live OS process. Reaped at teardown."""
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    yield proc.pid
    proc.kill()
    proc.wait(timeout=10)


@pytest.fixture
def reaped_pid():
    """A REAL pid that has genuinely exited and been reaped."""
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", ""],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proc.wait(timeout=30)
    return proc.pid


# --------------------------------------------------------------------------
# process — the ternary that a bare bool could not express.
# --------------------------------------------------------------------------


def test_process_probe_that_succeeds_and_finds_the_agent_up_is_alive():
    # Arrange
    config = _Cfg("agent-a", "tui")
    # Act
    signal = process_signal(config, _RuntimeSaysUp(), tmux_probe_ran=lambda: True)
    # Assert
    assert signal.verdict == ALIVE


def test_process_probe_that_succeeds_and_finds_nothing_is_dead():
    """Positive evidence of absence — the tmux probe RAN and there is no session."""
    # Arrange
    config = _Cfg("agent-a", "tui")
    # Act
    signal = process_signal(config, _RuntimeSaysDown(), tmux_probe_ran=lambda: True)
    # Assert
    assert signal.verdict == DEAD


def test_a_wedged_tmux_makes_a_missing_session_unknown_not_dead():
    """THE false-RED regression.

    ``TmuxManager.exists`` returns ``False`` both for "no such session" and for
    "I cannot talk to tmux at all". TUI is this fleet's DEFAULT runtime, so
    collapsing those two marks EVERY agent dead the moment tmux hiccups — or the
    moment the prober sits in a mount namespace that cannot see the tmux socket,
    which is exactly what happens inside a container.
    """
    # Arrange
    config = _Cfg("agent-a", "tui")
    # Act
    signal = process_signal(config, _RuntimeSaysDown(), tmux_probe_ran=lambda: False)
    # Assert
    assert signal.verdict == UNKNOWN


def test_a_wedged_tmux_says_so_in_the_evidence():
    # Arrange
    config = _Cfg("agent-a", "tui")
    # Act
    signal = process_signal(config, _RuntimeSaysDown(), tmux_probe_ran=lambda: False)
    # Assert
    assert "could not look" in signal.detail


def test_a_probe_that_raises_is_unknown_not_dead():
    # Arrange
    config = _Cfg("agent-a", "tui")
    # Act
    signal = process_signal(config, _RuntimeProbeExplodes())
    # Assert
    assert signal.verdict == UNKNOWN


def test_a_non_tui_runtime_that_reports_down_is_dead():
    """The apptainer pidfile read is a real probe — ON THE HOST.

    ``in_sif_fn`` is pinned rather than left to the ambient environment, and that
    is not a formality: ``ApptainerRuntime.is_running`` is ``os.kill(pid, 0)``,
    which is only a sensor in the pid namespace that MINTED the pid. Run from
    inside a container it reads "reaped" for every healthy agent on the host. So
    "is this a probe at all" depends on where the test runs — and a test whose
    verdict flips between CI and a container is testing the environment, not the
    code.
    """
    # Arrange
    config = _Cfg("agent-a", "apptainer")
    # Act
    signal = process_signal(config, _RuntimeSaysDown(), in_sif_fn=_on_the_host)
    # Assert
    assert signal.verdict == DEAD


def test_process_signal_is_sourced_as_process():
    # Arrange
    config = _Cfg("agent-a", "tui")
    # Act
    signal = process_signal(config, _RuntimeSaysUp(), tmux_probe_ran=lambda: True)
    # Assert
    assert signal.source == SOURCE_PROCESS


# --------------------------------------------------------------------------
# The false-DEAD this module produced on itself, caught in development.
# --------------------------------------------------------------------------


def test_an_empty_tmux_snapshot_from_inside_a_container_is_not_an_observation():
    """MEASURED 2026-07-14 — and it convicted a live agent.

    From inside a SIF, ``tmux ls`` prints "no server running on
    /tmp/tmux-1000/default": TRUE of the CONTAINER's own /tmp, and one of
    ``_tmux_probe``'s "no server ⇒ confirmed-empty" markers. So
    ``list_sessions_activity()`` does not FAIL — it SUCCEEDS and returns ``{}``,
    i.e. "the fleet is genuinely empty". The host's tmux is merely in another
    mount namespace.

    Run from in there, that made ``process_signal`` return DEAD for ``grant`` —
    an agent holding a live tmux session, a fresh heartbeat and a live inbox
    subscriber on the host. A confident, well-evidenced, entirely false death
    verdict. Only the corroboration gate stopped it authorising anything.
    """
    # Arrange: the real "empty snapshot" + the real "we are in a container".
    empty_snapshot = lambda **_kw: {}  # noqa: E731  — what tmux really returns
    in_a_container = lambda: True  # noqa: E731
    # Act
    ran = _tmux_probe_ran(snapshot_fn=empty_snapshot, in_sif_fn=in_a_container)
    # Assert — a non-observation must not be read as an observation.
    assert ran is None


def test_an_empty_tmux_snapshot_on_the_bare_host_IS_an_observation():
    """The probe must keep its teeth where it CAN see: on the host, empty is empty."""
    # Arrange
    empty_snapshot = lambda **_kw: {}  # noqa: E731
    on_the_host = lambda: False  # noqa: E731
    # Act
    ran = _tmux_probe_ran(snapshot_fn=empty_snapshot, in_sif_fn=on_the_host)
    # Assert
    assert ran is True


def test_a_failed_tmux_probe_is_never_an_observation():
    # Arrange — list_sessions_activity's own contract: None = the probe FAILED.
    failed_probe = lambda **_kw: None  # noqa: E731
    on_the_host = lambda: False  # noqa: E731
    # Act
    ran = _tmux_probe_ran(snapshot_fn=failed_probe, in_sif_fn=on_the_host)
    # Assert
    assert ran is None


def test_a_tui_agent_is_unknown_not_dead_when_the_probe_cannot_see_the_fleet():
    """The fix, at the signal level: cannot see ⇒ UNKNOWN, never DEAD."""
    # Arrange
    config = _Cfg("grant", "tui")
    # Act
    signal = process_signal(config, _RuntimeSaysDown(), tmux_probe_ran=lambda: None)
    # Assert
    assert signal.verdict == UNKNOWN


# --------------------------------------------------------------------------
# heartbeat — a real file with a real mtime.
# --------------------------------------------------------------------------


def test_a_fresh_heartbeat_is_alive(tmp_path):
    # Arrange
    hb = tmp_path / "heartbeat.json"
    hb.write_text('{"ts": 1.0, "pid": 0, "state": "running"}')
    # Act
    signal = heartbeat_signal("grant", path=hb)
    # Assert
    assert signal.verdict == ALIVE


def test_a_stale_heartbeat_is_unknown_never_dead(tmp_path):
    """The shared writer lives in ``sac listen``, not in the agent.

    When it stops, EVERY agent's beat freezes at once — a fact about the writer,
    not about any agent. Convicting on it would swap one fleet-wide false-death
    flood for another.
    """
    # Arrange
    hb = tmp_path / "heartbeat.json"
    hb.write_text('{"ts": 1.0, "pid": 0, "state": "running"}')
    old = time.time() - 5086  # grant's measured staleness, 2026-07-14
    os.utime(hb, (old, old))
    # Act
    signal = heartbeat_signal("grant", path=hb)
    # Assert
    assert signal.verdict == UNKNOWN


def test_a_missing_heartbeat_is_unknown_never_dead(tmp_path):
    # Arrange
    hb = tmp_path / "does-not-exist.json"
    # Act
    signal = heartbeat_signal("ghost", path=hb)
    # Assert
    assert signal.verdict == UNKNOWN


def test_pid_zero_in_the_heartbeat_is_reported_as_deciding_nothing(tmp_path):
    """``pid: 0`` is a HARDCODED literal from the central listen-side writer.

    ``_tui_heartbeat_loop._beat_one`` calls ``write_fn(state_dir, pid=0, ...)``.
    It was never a fact about the agent, and the evidence line must say so — the
    whole "unfalsifiable row" panic was built on reading it as one.
    """
    # Arrange
    hb = tmp_path / "heartbeat.json"
    hb.write_text('{"ts": 1.0, "pid": 0, "state": "running"}')
    # Act
    signal = heartbeat_signal("grant", path=hb)
    # Assert
    assert "decides nothing" in signal.detail


def test_heartbeat_signal_is_sourced_as_heartbeat(tmp_path):
    # Arrange
    hb = tmp_path / "heartbeat.json"
    hb.write_text('{"ts": 1.0, "pid": 0}')
    # Act
    signal = heartbeat_signal("grant", path=hb)
    # Assert
    assert signal.source == SOURCE_HEARTBEAT


# --------------------------------------------------------------------------
# registry — a DECLARATION, graded asymmetrically against a REAL pid.
# --------------------------------------------------------------------------


def test_a_reaped_recorded_pid_is_positive_evidence_of_death(reaped_pid):
    """``os.kill(pid, 0)`` raising ESRCH means THAT process does not exist.

    True only in the namespace that minted the pid — hence the pinned
    ``in_sif_fn``; see :func:`_on_the_host`.
    """
    # Arrange
    rows = [{"name": "scitex-dev", "pid": reaped_pid}]
    # Act
    signal = registry_signal("scitex-dev", rows=rows, in_sif_fn=_on_the_host)
    # Assert
    assert signal.verdict == DEAD


def test_a_live_recorded_pid_is_only_unknown_because_pids_are_recycled(live_pid):
    """Asymmetric on purpose: a reaped pid is proof, a live one may be a stranger."""
    # Arrange
    rows = [{"name": "grant", "pid": live_pid}]
    # Act
    signal = registry_signal("grant", rows=rows, in_sif_fn=_on_the_host)
    # Assert
    assert signal.verdict == UNKNOWN


def test_no_active_row_is_unknown_never_dead():
    """Absence of a declaration is not evidence of death.

    Reading it as one alarmed ~100 false criticals per sweep against agents that
    were serving HTTP in the same log.
    """
    # Arrange
    rows: list[dict] = []
    # Act
    signal = registry_signal("grant", rows=rows)
    # Assert
    assert signal.verdict == UNKNOWN


def test_a_row_recording_pid_zero_is_unknown_never_dead():
    """``grant``'s exact shape: a row that declares 'running' and records pid 0."""
    # Arrange
    rows = [{"name": "grant", "pid": 0}]
    # Act
    signal = registry_signal("grant", rows=rows)
    # Assert
    assert signal.verdict == UNKNOWN


def test_a_row_recording_a_null_pid_is_unknown_never_dead():
    """On this fleet, active rows routinely carry ``pid = NULL`` while healthy."""
    # Arrange
    rows = [{"name": "grant", "pid": None}]
    # Act
    signal = registry_signal("grant", rows=rows)
    # Assert
    assert signal.verdict == UNKNOWN


def test_registry_signal_is_sourced_as_registry():
    # Arrange
    rows: list[dict] = []
    # Act
    signal = registry_signal("grant", rows=rows)
    # Assert
    assert signal.source == SOURCE_REGISTRY


# --------------------------------------------------------------------------
# remote_process_signal — control-plane cross-host liveness. ssh is INJECTED
# (a real callable returning rc). Same doctrine: a probe that could not run
# is UNKNOWN, never DEAD — so a wedged ssh cannot slander a live remote agent.
#
# The argv is rendered by the REAL ``build_ssh_argv`` off a REAL ``PeersMap``
# parsed from a REAL temp config.yaml (``spartan_config`` — no mock), pinned via
# the documented ``SCITEX_AGENT_CONTAINER_CONFIG`` override. That is what lets a
# two-tier HPC target (a ``spartan-bmNNN`` compute node reachable ONLY via the
# ``spartan`` login node) probe at all — the follow-on to #710.
# --------------------------------------------------------------------------


@pytest.fixture
def spartan_config(tmp_path):
    """A REAL temp sac config.yaml the remote probe loads through the real
    ``host_config.load()`` (glob matching + ``build_ssh_argv`` all real):

      * ``spartan``  — the login-node peer (DIRECT, no ProxyJump).
      * ``spartan*`` — the compute-node GLOB peer whose ssh target is the queried
        node name, reachable only ``via: [spartan]`` and gated behind an
        ``env_preamble`` (Lmod) — the exact two-tier HPC shape.

    Exported via env (explicit save/restore, no ``monkeypatch``): the documented
    ``SCITEX_AGENT_CONTAINER_CONFIG`` override points ``load()`` at this file,
    and ``SAC_SSH_CONTROL_MASTER=0`` opts out of ControlMaster so the rendered
    argv is deterministic (no scratch-dir-dependent ``-o ControlPath`` triple).
    """
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "peers:\n"
        "  spartan: { ssh: ywatanabe@spartan-login }\n"
        '  "spartan*": { via: [spartan], env_preamble: "source /lmod/init'
        ' && module load apptainer" }\n'
    )
    env = {
        "SCITEX_AGENT_CONTAINER_CONFIG": str(cfg),
        "SAC_SSH_CONTROL_MASTER": "0",
    }
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        yield cfg
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_remote_process_signal_rc0_session_present_is_alive(spartan_config):
    # Arrange — spartan-dev lives on the login node (a DIRECT peer).
    cfg = _Cfg("spartan-dev", "tui")
    # Act
    signal = remote_process_signal(cfg, "spartan", run_ssh=lambda _argv: 0)
    # Assert
    assert signal.verdict == ALIVE


def test_remote_process_signal_rc1_no_remote_session_is_dead(spartan_config):
    # Arrange
    cfg = _Cfg("spartan-dev", "tui")
    # Act
    signal = remote_process_signal(cfg, "spartan", run_ssh=lambda _argv: 1)
    # Assert
    assert signal.verdict == DEAD


def test_remote_process_signal_ssh_connect_failure_is_unknown_never_dead(
    spartan_config,
):
    # Arrange — rc 255 is ssh's own connection-failed code.
    cfg = _Cfg("spartan-dev", "tui")
    # Act
    signal = remote_process_signal(cfg, "spartan", run_ssh=lambda _argv: 255)
    # Assert
    assert signal.verdict == UNKNOWN


def test_remote_process_signal_run_ssh_raising_is_unknown_never_dead(spartan_config):
    # Arrange
    cfg = _Cfg("spartan-dev", "tui")

    def _boom(_argv):
        raise OSError("ssh shell-out exploded")

    # Act
    signal = remote_process_signal(cfg, "spartan", run_ssh=_boom)
    # Assert
    assert signal.verdict == UNKNOWN


def test_remote_process_signal_unknown_peer_is_unknown_never_dead(spartan_config):
    """A peer that is neither a config entry nor a ``spartan*`` glob match cannot
    be rendered into an argv (``build_ssh_argv`` raises ``KeyError``); that is
    "I could not even look" -> UNKNOWN, never a false DEAD. ``run_ssh`` returning
    0 is irrelevant — it is never reached."""
    # Arrange
    cfg = _Cfg("mystery", "tui")
    # Act — rc 0 would be ALIVE if the argv had built; it does not.
    signal = remote_process_signal(cfg, "nuc-not-in-config", run_ssh=lambda _argv: 0)
    # Assert
    assert signal.verdict == UNKNOWN


# --- Part 2: the ProxyJump multihop path (compute node via login node) --------


def test_remote_process_signal_multihop_rc0_session_present_is_alive(spartan_config):
    # Arrange — spartan-bm043 glob-matches spartan*, reached via the login node.
    cfg = _Cfg("proj-x", "tui")
    # Act
    signal = remote_process_signal(cfg, "spartan-bm043", run_ssh=lambda _argv: 0)
    # Assert
    assert signal.verdict == ALIVE


def test_remote_process_signal_multihop_rc1_no_session_is_dead(spartan_config):
    """The ``preamble && tmux has-session`` chain preserves tmux's rc 1 through
    ``bash -c`` for a genuinely-absent session -> DEAD."""
    # Arrange
    cfg = _Cfg("proj-x", "tui")
    # Act
    signal = remote_process_signal(cfg, "spartan-bm043", run_ssh=lambda _argv: 1)
    # Assert
    assert signal.verdict == DEAD


def test_remote_process_signal_multihop_rc255_is_unknown_never_dead(spartan_config):
    # Arrange — a wedged hop / failed ProxyJump is a non-0/1 rc.
    cfg = _Cfg("proj-x", "tui")
    # Act
    signal = remote_process_signal(cfg, "spartan-bm043", run_ssh=lambda _argv: 255)
    # Assert
    assert signal.verdict == UNKNOWN


# --------------------------------------------------------------------------
# The argv the remote probe hands to ssh — captured via the injected run_ssh
# (a real recording callable, no mock), rendered by the REAL ``build_ssh_argv``
# off the ``spartan_config`` PeersMap. A login shell (`bash -lc`) is the bug: on
# Spartan it triggers the profile's interactive-tmux, which prints "open
# terminal failed: not a terminal" and exits rc 1 — a LIVE remote agent misread
# as DEAD. A DIRECT peer runs tmux without any wrapper; a preamble peer wraps it
# in ``bash -c`` (deliberately ``-c``, NOT ``-lc``).
# --------------------------------------------------------------------------


def _captured_remote_probe_argv(
    peer: str = "spartan", name: str = "spartan-dev"
) -> list[str]:
    """Return the argv ``remote_process_signal`` hands to ssh.

    The ``run_ssh`` seam is a real callable; here it merely records the argv it
    is given and reports rc 0. No mock — this is the injection point the signal
    is designed around. The caller must hold the ``spartan_config`` fixture so
    the peer resolves through the real loader.
    """
    captured: list[list[str]] = []

    def _run_ssh(argv: list[str]) -> int:
        captured.append(argv)
        return 0

    remote_process_signal(_Cfg(name, "tui"), peer, run_ssh=_run_ssh)
    return captured[0]


def test_remote_process_signal_argv_uses_no_login_shell(spartan_config):
    """Regression (2026-07-16): the probe must NOT wrap tmux in a login shell.

    ``ssh <peer> bash -lc 'tmux has-session ...'`` runs the peer's login profile,
    whose interactive-tmux stanza fails with "open terminal failed: not a
    terminal" (rc 1) — mapping a live remote agent to DEAD in ``sac agents list``.
    """
    # Arrange
    login_shell_tokens = {"bash", "-lc"}
    # Act — a DIRECT peer (spartan): no wrapper at all.
    argv = _captured_remote_probe_argv()
    # Assert — no login shell: neither `bash` nor `-lc` in the built argv.
    assert not (login_shell_tokens & set(argv))


def test_remote_process_signal_argv_probes_tmux_has_session_directly(spartan_config):
    # Arrange — the target is the EXACT-match form (=name:): a bare -t
    # prefix-matches on the peer's tmux, so a sibling session would vouch
    # this agent ALIVE (incident 2026-08-14).
    expected_tail = ["tmux", "has-session", "-t", "=tui-spartan-dev:"]
    # Act
    argv = _captured_remote_probe_argv()
    # Assert — a DIRECT peer runs `tmux has-session -t <session>` directly.
    assert argv[-4:] == expected_tail


def test_remote_process_signal_argv_passes_ssh_dash_n(spartan_config):
    """``-n`` so ssh never consumes our stdin during a bulk `sac agents list`."""
    # Arrange
    required_flag = "-n"
    # Act
    argv = _captured_remote_probe_argv()
    # Assert
    assert required_flag in argv


# --- Part 2: multihop argv (compute node via login node) + regression guard ---


def test_remote_process_signal_direct_peer_argv_has_no_proxyjump(spartan_config):
    """#710 regression guard: spartan-dev lives on the login node (host=spartan),
    a DIRECT peer — its probe argv must carry NO ProxyJump (`-J`). Reusing
    ``build_ssh_argv`` must not start hopping a login-node agent."""
    # Arrange
    direct_peer = "spartan"
    # Act
    argv = _captured_remote_probe_argv(peer=direct_peer, name="spartan-dev")
    # Assert
    assert "-J" not in argv


def test_remote_process_signal_multihop_argv_uses_proxyjump_via_login(spartan_config):
    """A ``spartan-bmNNN`` compute node is reachable ONLY through its login node,
    so ``build_ssh_argv`` must emit ``-J <login-node ssh target>`` (from the
    ``spartan*`` glob peer's ``via: [spartan]``)."""
    # Arrange
    compute_node = "spartan-bm043"
    # Act
    argv = _captured_remote_probe_argv(peer=compute_node, name="proj-x")
    # Assert — `-J` is immediately followed by the login node's ssh target.
    assert argv[argv.index("-J") + 1] == "ywatanabe@spartan-login"


def test_remote_process_signal_multihop_argv_wraps_cmd_in_bash_c(spartan_config):
    """The glob peer's ``env_preamble`` forces a single ``bash -c '<preamble> &&
    tmux has-session ...'`` argv element (``-c``, NOT ``-lc``)."""
    # Arrange
    compute_node = "spartan-bm043"
    # Act
    argv = _captured_remote_probe_argv(peer=compute_node, name="proj-x")
    # Assert — one collapsed element carrying the preamble + the tmux probe
    # (exact-match =name: target; see the direct-peer test above).
    assert any(
        el.startswith("bash -c ") and "tmux has-session -t =tui-proj-x:" in el
        for el in argv
    )


# --------------------------------------------------------------------------
# screen_signal — the WORKING sensor, driven through the REAL
# ``auth_state.verdict_for`` with REAL cached-row dicts and a REAL clock (no
# mocks: ``read_state`` is an injection seam returning a real dict). All the
# freshness / SUPERSEDED honesty lives in ``verdict_for``, so these pin that a
# WEDGED reaches decide() ONLY when the cache is fresh AND this-incarnation —
# every other read (stale, superseded, clean, never-checked) degrades to
# UNKNOWN, never a false WEDGE.
# --------------------------------------------------------------------------


@pytest.fixture
def screen_now() -> datetime:
    """A fixed reference instant, so freshness/scope assertions are deterministic."""
    return datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)


def _screen_stamp(moment: datetime) -> str:
    """``moment`` in the exact ISO-8601 UTC 'Z' shape the auth store writes."""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_a_fresh_auth_failed_row_reads_wedged(screen_now):
    """The P0 at the resolver level: a fresh frozen banner ⇒ WEDGED."""
    # Arrange — checked 60s ago, this incarnation started an hour ago.
    row = {
        "auth_failed": True,
        "checked_at": _screen_stamp(screen_now - timedelta(seconds=60)),
        "banner": "Login expired",
        "reason": "revoked",
    }
    # Act
    signal = screen_signal(
        "clew",
        started_at=_screen_stamp(screen_now - timedelta(hours=1)),
        read_state=lambda _n: row,
        now=screen_now,
    )
    # Assert
    assert signal.verdict == WEDGED


def test_a_wedged_screen_signal_carries_the_banner_and_remedy(screen_now):
    """The evidence must be specific — the operator learns WHAT and WHAT TO DO."""
    # Arrange
    row = {
        "auth_failed": True,
        "checked_at": _screen_stamp(screen_now - timedelta(seconds=60)),
        "banner": "Login expired",
        "reason": "revoked",
    }
    # Act
    signal = screen_signal(
        "clew",
        started_at=_screen_stamp(screen_now - timedelta(hours=1)),
        read_state=lambda _n: row,
        now=screen_now,
    )
    # Assert — a revoked token's remedy is a restart, and it says so.
    assert "restart" in signal.detail


def test_a_fresh_clean_row_reads_unknown_not_alive(screen_now):
    """A clean pane is NOT proof of life — only the absence of a known wedge."""
    # Arrange — fresh, auth_failed False.
    row = {
        "auth_failed": False,
        "checked_at": _screen_stamp(screen_now - timedelta(seconds=60)),
    }
    # Act
    signal = screen_signal(
        "worker",
        started_at=_screen_stamp(screen_now - timedelta(hours=1)),
        read_state=lambda _n: row,
        now=screen_now,
    )
    # Assert
    assert signal.verdict == UNKNOWN


def test_a_stale_auth_failed_row_reads_unknown_never_a_stale_wedge(screen_now):
    """A verdict older than 900s is weak evidence — the banner may be cleared.

    A stale cache must never be rendered as a current WEDGE, so it degrades to
    UNKNOWN rather than asserting a wedge that may no longer be true.
    """
    # Arrange — checked 6 hours ago (>> 900s STALE_AFTER_S).
    row = {
        "auth_failed": True,
        "checked_at": _screen_stamp(screen_now - timedelta(hours=6)),
        "banner": "Login expired",
    }
    # Act
    signal = screen_signal(
        "clew",
        started_at=_screen_stamp(screen_now - timedelta(hours=12)),
        read_state=lambda _n: row,
        now=screen_now,
    )
    # Assert
    assert signal.verdict == UNKNOWN


def test_a_superseded_auth_failed_row_reads_unknown(screen_now):
    """A verdict stamped BEFORE this incarnation's start describes a PREVIOUS
    life — a restarted agent must never still read wedged.

    verdict_for discards a ``checked_at`` older than ``started_at`` (reports it as
    never-checked), so screen_signal sees no ``auth_checked_at`` and reports
    UNKNOWN.
    """
    # Arrange — checked 2h ago, but this incarnation started only 1h ago.
    row = {
        "auth_failed": True,
        "checked_at": _screen_stamp(screen_now - timedelta(hours=2)),
        "banner": "Login expired",
    }
    # Act
    signal = screen_signal(
        "clew",
        started_at=_screen_stamp(screen_now - timedelta(hours=1)),
        read_state=lambda _n: row,
        now=screen_now,
    )
    # Assert
    assert signal.verdict == UNKNOWN


def test_a_never_checked_agent_reads_unknown(screen_now):
    """No cached row at all ⇒ the screen was not read ⇒ UNKNOWN, never a wedge."""
    # Arrange — the auth cache has no row for this agent.
    # Act
    signal = screen_signal(
        "ghost",
        started_at=_screen_stamp(screen_now),
        read_state=lambda _n: None,
        now=screen_now,
    )
    # Assert
    assert signal.verdict == UNKNOWN


def test_screen_signal_is_sourced_as_screen(screen_now):
    # Arrange
    row = {
        "auth_failed": True,
        "checked_at": _screen_stamp(screen_now - timedelta(seconds=60)),
        "banner": "Login expired",
    }
    # Act
    signal = screen_signal(
        "clew",
        started_at=_screen_stamp(screen_now - timedelta(hours=1)),
        read_state=lambda _n: row,
        now=screen_now,
    )
    # Assert
    assert signal.source == SOURCE_SCREEN

"""A destruction may be authorised only by TWO SENSORS — not by one, asked twice.

WHY THIS FILE EXISTS (2026-07-14). ``may_destroy`` required "2 independent
sources" and counted SOURCE STRINGS. But ``process`` and ``registry`` are not two
sensors — for BOTH runtimes they are the SAME ``os.kill(pid, 0)`` on the SAME
pid, and the codebase engineered that identity ON PURPOSE:

    runtimes/_tui_liveness.pane_pid_of:
      "This is the value ``instances.pid`` records for a TUI agent, and it is the
       SAME signal ``pane_process_alive`` (hence ``is_running``) already keys
       liveness on — so the registry and ``is_running`` can never disagree about
       which process represents this agent."

    runtimes/_apptainer_runtime.agent_pid:
      "This is EXACTLY the pid ``is_running`` above probes with
       ``os.kill(pid, 0)`` ... reusing ``_read_pid`` here means the registry and
       ``is_running`` can never disagree."

So the two witnesses the gate demanded were GUARANTEED to agree. One syscall,
two hats, and ``may_destroy`` came back True — whose remedy is ``--force
--fresh``, which KILLS THE AGENT. From inside a container, where a host pid is
not even in our pid namespace, that syscall reads "reaped" for every healthy
agent on the box.

MEASURED against the code as shipped in v0.21.20, on a real reaped pid:
``dead_sources = ('process', 'registry')`` → ``may_destroy = True``.

NO MOCKS (repo doctrine): these drive a REAL reaped pid through the REAL
resolvers. The taxonomy's own guards live in ``test__verdict_instruments.py``.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from scitex_agent_container._lifecycle._verdict import (
    DEAD,
    INSTRUMENT_HOST_TMUX,
    INSTRUMENT_PID_NAMESPACE,
    SOURCE_PROCESS,
    SOURCE_REGISTRY,
    UNKNOWN,
    Signal,
    decide,
)
from scitex_agent_container._lifecycle._verdict_resolve import (
    process_signal,
    registry_signal,
)


class _Cfg:
    """A real minimal config object — the two attributes the resolvers read."""

    def __init__(self, name: str, runtime: str) -> None:
        self.name = name
        self.runtime = runtime


class _RuntimeSaysDown:
    """A real runtime whose probe SUCCEEDS and finds nothing there."""

    def is_running(self, config) -> bool:
        return False


def _on_the_host() -> bool:
    """The destruction gate runs on the HOST, where a pid check IS a sensor."""
    return False


def _in_a_container() -> bool:
    return True


def _session_absent(_session: str) -> tuple[bool | None, bool | None]:
    """The tmux probe RAN, and tmux has no session for this agent."""
    return True, False


def _session_present(_session: str) -> tuple[bool | None, bool | None]:
    """The tmux probe RAN, and the session is THERE (so the pane pid is what died)."""
    return True, True


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


@pytest.fixture
def pid_collapse_verdict(reaped_pid):
    """THE P0 SHAPE: an apptainer agent, and a registry row for THE SAME pid.

    ``runtime.is_running`` is ``os.kill(pid, 0)``; ``agent_pid`` (which feeds
    ``instances.pid``) returns the very pid it probes. So both resolvers run the
    same syscall on the same integer, and both come back DEAD.
    """
    config = _Cfg("agent-a", "apptainer")
    rows = [{"name": "agent-a", "pid": reaped_pid, "host": None}]
    return decide(
        "agent-a",
        [
            process_signal(config, _RuntimeSaysDown(), in_sif_fn=_on_the_host),
            registry_signal("agent-a", rows=rows, in_sif_fn=_on_the_host),
        ],
    )


# --------------------------------------------------------------------------
# THE BUG — these fail on the code as it shipped in v0.21.20.
# --------------------------------------------------------------------------


def test_one_syscall_reported_twice_does_not_authorise_a_destruction(
    pid_collapse_verdict,
):
    """BEFORE: dead_sources == ("process", "registry") → 2 → may_destroy True.

    A healthy agent destroyed on the strength of ONE reading. AFTER: both
    resolvers declare ``pid_namespace``, so there is one witness — and one
    witness never authorises a kill.
    """
    # Arrange
    verdict = pid_collapse_verdict
    # Act
    authorised = verdict.may_destroy
    # Assert
    assert authorised is False


def test_the_two_dead_reports_are_still_counted_as_two_reporters(
    pid_collapse_verdict,
):
    """The inputs are unchanged — it is the COUNTING that was wrong."""
    # Arrange
    verdict = pid_collapse_verdict
    # Act
    reporters = verdict.dead_sources
    # Assert
    assert reporters == ("process", "registry")


def test_the_two_dead_reports_collapse_to_one_instrument(pid_collapse_verdict):
    """Two reporters, one sensor. This is the whole fix, in one assertion."""
    # Arrange
    verdict = pid_collapse_verdict
    # Act
    sensors = verdict.dead_instruments
    # Assert
    assert sensors == (INSTRUMENT_PID_NAMESPACE,)


def test_the_collapse_still_reports_the_agent_as_dead(pid_collapse_verdict):
    """We do not pretend the pid is alive — we refuse to ACT on one sensor.

    Refusing to destroy is not the same as claiming life. The verdict stays
    honest; only the authorisation is withheld.
    """
    # Arrange
    verdict = pid_collapse_verdict
    # Act
    reported = verdict.verdict
    # Assert
    assert reported == DEAD


def test_the_veto_tells_the_operator_the_two_deads_were_one_sensor(
    pid_collapse_verdict,
):
    """A refusal must teach, not merely refuse."""
    # Arrange
    verdict = pid_collapse_verdict
    # Act
    reason = verdict.destroy_veto_reason
    # Assert
    assert "one sensor reported twice" in reason


def test_two_reporters_on_one_instrument_are_one_witness():
    """The rule, stated purely: DIFFERENT sources, SAME sensor ⇒ no destruction."""
    # Arrange — the exact shape the old gate accepted as "2 independent sources".
    signals = [
        Signal(SOURCE_PROCESS, DEAD, "pid reaped", INSTRUMENT_PID_NAMESPACE),
        Signal(SOURCE_REGISTRY, DEAD, "same pid, reaped", INSTRUMENT_PID_NAMESPACE),
    ]
    # Act
    verdict = decide("x", signals)
    # Assert
    assert verdict.may_destroy is False


# --------------------------------------------------------------------------
# The RESOLVERS must agree with the taxonomy — a declared independence the code
# does not honour is just a comment.
# --------------------------------------------------------------------------


def test_a_pid_based_runtime_declares_the_pid_namespace_instrument():
    """``ApptainerRuntime.is_running`` IS ``os.kill(pid, 0)``. Nothing else."""
    # Arrange
    config = _Cfg("agent-a", "apptainer")
    # Act
    signal = process_signal(config, _RuntimeSaysDown(), in_sif_fn=_on_the_host)
    # Assert
    assert signal.instrument == INSTRUMENT_PID_NAMESPACE


def test_the_registry_declares_the_same_instrument_as_a_pid_based_runtime(reaped_pid):
    """Pin the collapse. If these ever diverge, the gate is armed on one syscall."""
    # Arrange
    rows = [{"name": "agent-a", "pid": reaped_pid, "host": None}]
    # Act
    signal = registry_signal("agent-a", rows=rows, in_sif_fn=_on_the_host)
    # Assert
    assert signal.instrument == INSTRUMENT_PID_NAMESPACE


def test_a_tui_agent_whose_session_is_gone_is_convicted_by_tmux():
    """The one case where ``process`` IS independent of the registry.

    tmux's own bookkeeping positively has no session. That is a different
    bookkeeper from the kernel's pid table, so it MAY corroborate the registry —
    which is why the fleet's default runtime keeps a real destruction path.
    """
    # Arrange
    config = _Cfg("grant", "tui")
    # Act
    signal = process_signal(
        config, _RuntimeSaysDown(), session_observation=_session_absent
    )
    # Assert
    assert signal.instrument == INSTRUMENT_HOST_TMUX


def test_a_tui_agent_whose_session_is_gone_is_dead():
    """Positive evidence of absence, from a probe that actually ran."""
    # Arrange
    config = _Cfg("grant", "tui")
    # Act
    signal = process_signal(
        config, _RuntimeSaysDown(), session_observation=_session_absent
    )
    # Assert
    assert signal.verdict == DEAD


def test_a_tui_agent_whose_session_survives_is_convicted_only_by_the_pid_check():
    """The trap: the session is THERE, so ``is_running``'s False came from
    ``os.kill(pane_pid, 0)`` — the registry's instrument, not tmux's.

    ``pane_pid_of`` feeds ``instances.pid``, so this is literally the same syscall
    on the same integer the registry runs. It must not corroborate the registry.
    """
    # Arrange
    config = _Cfg("grant", "tui")
    # Act
    signal = process_signal(
        config, _RuntimeSaysDown(), session_observation=_session_present
    )
    # Assert
    assert signal.instrument == INSTRUMENT_PID_NAMESPACE


def test_a_surviving_tui_session_cannot_corroborate_the_registry(reaped_pid):
    """The two halves, folded: same sensor ⇒ still no destruction."""
    # Arrange
    config = _Cfg("grant", "tui")
    rows = [{"name": "grant", "pid": reaped_pid, "host": None}]
    # Act
    verdict = decide(
        "grant",
        [
            process_signal(
                config, _RuntimeSaysDown(), session_observation=_session_present
            ),
            registry_signal("grant", rows=rows, in_sif_fn=_on_the_host),
        ],
    )
    # Assert
    assert verdict.may_destroy is False


# --------------------------------------------------------------------------
# A pid read across a namespace boundary is NOT A SENSOR.
# --------------------------------------------------------------------------


def test_a_reaped_pid_seen_from_inside_a_container_convicts_nobody(reaped_pid):
    """From inside a SIF, a HOST pid is not in our namespace.

    ``os.kill`` there answers about a different process — or none — so it reads
    "reaped" for agents that are perfectly healthy on the host. That is not a
    weak sensor. It is not a sensor.
    """
    # Arrange
    rows = [{"name": "grant", "pid": reaped_pid, "host": None}]
    # Act
    signal = registry_signal("grant", rows=rows, in_sif_fn=_in_a_container)
    # Assert
    assert signal.verdict == UNKNOWN


def test_the_container_pid_read_says_in_words_that_it_is_not_a_sensor(reaped_pid):
    """The evidence line has to teach the next reader why it abstained."""
    # Arrange
    rows = [{"name": "grant", "pid": reaped_pid, "host": None}]
    # Act
    signal = registry_signal("grant", rows=rows, in_sif_fn=_in_a_container)
    # Assert
    assert "NOT A SENSOR" in signal.detail


def test_a_pid_runtime_probed_from_inside_a_container_convicts_nobody():
    """Same blindness, reached through ``runtime.is_running`` — the same syscall."""
    # Arrange
    config = _Cfg("agent-a", "apptainer")
    # Act
    signal = process_signal(config, _RuntimeSaysDown(), in_sif_fn=_in_a_container)
    # Assert
    assert signal.verdict == UNKNOWN


def test_a_row_written_on_another_host_convicts_nobody(reaped_pid):
    """That pid was minted in another machine's namespace. The integer is not ours."""
    # Arrange
    rows = [{"name": "grant", "pid": reaped_pid, "host": "some-other-box"}]
    # Act
    signal = registry_signal("grant", rows=rows, in_sif_fn=_on_the_host)
    # Assert
    assert signal.verdict == UNKNOWN


def test_a_cross_host_row_explains_the_namespace_mismatch(reaped_pid):
    """Name the host we are on and the host the row came from."""
    # Arrange
    rows = [{"name": "grant", "pid": reaped_pid, "host": "some-other-box"}]
    # Act
    signal = registry_signal("grant", rows=rows, in_sif_fn=_on_the_host)
    # Assert
    assert "another machine's namespace" in signal.detail

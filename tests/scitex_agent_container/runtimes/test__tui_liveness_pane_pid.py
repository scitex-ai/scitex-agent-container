"""``_tui_liveness.pane_pid_of`` — the pid the TUI runtime records.

This is the value ``TuiSessionRuntime.agent_pid`` hands to
``instances.pid`` (``_lifecycle._instances.record_local_instance``), and it
is the SAME signal ``pane_process_alive`` (hence
``TuiSessionRuntime.is_running``) already keys liveness on — so the registry
and ``is_running`` can never disagree about which process IS the agent.

Why the PANE pid and NOT the launcher: the launcher spawns the tmux session
and exits within seconds, so recording it would store a pid that is dead
almost immediately. The pane's ``bash -c`` ``exec``s apptainer, and ``exec``
REPLACES the image while KEEPING the pid — so ``#{pane_pid}`` is stable from
the moment the pane exists and IS the long-lived ``apptainer exec ... claude``
process.

A REAL detached tmux session (skipped when tmux is unavailable). No mocks.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid

import pytest

from scitex_agent_container.runtimes._tui_liveness import pane_pid_of


@pytest.fixture
def tmux_session():
    """A REAL detached tmux session running a long-lived process."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux not available")
    # UNIQUE PER PROCESS, NOT PER MILLISECOND. A session name is a key on a
    # HOST-GLOBAL tmux server, and `new-session` on a name that already
    # exists exits 1 with `duplicate session: <name>` — measured, tmux 3.4.
    #
    # The old name was `int(time.time() * 1000)`, which is not a unique
    # source at that resolution at all: 20000 draws in a tight loop collapse
    # into FOUR distinct milliseconds. Everything that can draw it at the
    # same instant then collides, and plenty can — this fixture serves TWO
    # tests that xdist's `--dist load` may hand to two worker PROCESSES at
    # once, and the three matrix legs are co-tenant on one node sharing
    # /tmp/tmux-<uid>/default (apptainer mounts the host /tmp; `sac`'s CI
    # exec wrapper takes no --contain).
    #
    # That is the shape develop hit on 2026-08-12 (run 31593095548): ONE of
    # the two setups errored, its sibling using this same fixture passed in
    # the same run, and 15552 other tests were green — a momentary clash,
    # not a broken environment. pid + uuid4 is what every sibling real-tmux
    # test here already uses, and it cannot clash across processes or hosts.
    name = f"sac-test-panepid-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    proc = subprocess.run(
        ["tmux", "new-session", "-d", "-s", name, "sleep 300"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        # NOT `check=True`. CalledProcessError's message is only "Command
        # ... returned non-zero exit status 1." — tmux's actual reason lives
        # on the unprinted `.stderr` attribute, so `check=True` here fed CI
        # an exit status and destroyed the one line that says WHY. The
        # develop failure above could not be diagnosed from its own log for
        # exactly this reason. Fail loudly, quoting tmux.
        pytest.fail(
            f"tmux could not create the test session (rc={proc.returncode}). "
            f"tmux stderr: {proc.stderr.strip() or '<empty>'} | "
            f"stdout: {proc.stdout.strip() or '<empty>'}"
        )
    try:
        yield name
    finally:
        subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)


def test_pane_pid_of_returns_the_live_pane_process(tmux_session) -> None:
    # Arrange — the pane's process is a real, live `sleep`.
    from scitex_agent_container._runners._tmux.tmux import TmuxManager

    # Act
    pid = pane_pid_of(tmux_session, pane_pid_fn=TmuxManager.pane_pid)
    # Assert — a concrete OS pid (not the launcher, which already exited).
    assert isinstance(pid, int) and pid > 0


def test_pane_pid_of_agrees_with_the_is_running_signal(tmux_session) -> None:
    # Arrange — the pid landing in instances.pid must be provably alive, or
    # the registry would vouch for a corpse.
    from scitex_agent_container._runners._tmux.tmux import TmuxManager
    from scitex_agent_container.runtimes._tui_liveness import pid_alive

    pid = pane_pid_of(tmux_session, pane_pid_fn=TmuxManager.pane_pid)
    # Act
    alive = pid_alive(pid)
    # Assert
    assert alive is True


def test_pane_pid_of_is_none_for_an_absent_session() -> None:
    # Arrange — no session => nothing to record.
    from scitex_agent_container._runners._tmux.tmux import TmuxManager

    # Act
    pid = pane_pid_of("sac-test-no-such-session", pane_pid_fn=TmuxManager.pane_pid)
    # Assert
    assert pid is None


def test_pane_pid_of_is_none_without_a_probe() -> None:
    # Arrange — a multiplexer fake predating the probe must yield "unknown",
    # never a fabricated pid (a wrong pid can be REUSED and vouch for a dead
    # agent; None is honestly unknown).
    # Act
    pid = pane_pid_of("whatever", pane_pid_fn=None)
    # Assert
    assert pid is None

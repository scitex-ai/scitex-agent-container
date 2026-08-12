#!/usr/bin/env python3
"""A stop that tmux confirmed is not a file that has stopped being written.

SOURCE_STOP used to report success on the liveness answer alone. On 2026-08-12 it
did exactly that — tmux answered that session ``tui-…`` was gone, which was TRUE —
and the transcript then grew 11,717 bytes as the dying process finished flushing
its final line. The relocation aborted three phases later, blaming the copy.

These tests drive the real phase against a real filesystem. The ``exec_fn`` seam
hands each rendered script to ``sh -c`` through ``subprocess``, so ``tmux``,
``wc``, ``stat`` and ``readlink`` actually run and the answers are the machine's.
Nothing is mocked. The liveness probe answers honestly for an agent name no tmux
session exists under, which is the state a stopped agent is in.

The clock and the sleep are injected, which is not a mock: they are the two things
a test must not spend. The fake sleep advances the fake clock by exactly the
interval it was asked for, so the deadline arithmetic runs at full fidelity in no
wall-clock time.
"""

from __future__ import annotations

import subprocess

from scitex_agent_container._lifecycle._relocate_effects import RelocateAdapters
from scitex_agent_container._lifecycle._relocate_shell import Shell


def _real_exec(argv, timeout_s=None):
    done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
    return {
        "exit_code": done.returncode,
        "stdout": done.stdout,
        "stderr": done.stderr,
        "timed_out": False,
    }


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class _FlushingClock(_Clock):
    """A clock whose every sleep appends to the transcript — a flush in flight."""

    def __init__(self, path) -> None:
        super().__init__()
        self.path = path

    def sleep(self, seconds: float) -> None:
        super().sleep(seconds)
        with open(self.path, "ab") as handle:
            handle.write(b'{"type":"assistant","text":"still flushing"}\n')


def _adapters(tmp_path, clock, agent="relocate-test-agent-no-such-session"):
    """A real source whose transcript home and workdir are real directories.

    The spec is the real shape: an apptainer bind supplies the container home,
    and the workdir is what Claude Code encodes into the project directory name.
    Both are under ``tmp_path``, so the derivation runs for real rather than
    being handed a path.
    """
    home = tmp_path / "container-home"
    workdir = tmp_path / "work"
    workdir.mkdir(parents=True, exist_ok=True)
    spec = {
        "spec": {
            "workdir": str(workdir),
            "apptainer": {"binds": [f"{home}:/home/agent"]},
        }
    }
    source = Shell(host="here", is_local=True)
    return RelocateAdapters(
        agent=agent,
        spec=spec,
        from_host="here",
        to_host="there",
        source=source,
        target=Shell(host="there"),
        stamp="20260812T000000Z",
        exec_fn=_real_exec,
        now=clock.now,
        sleep=clock.sleep,
    )


def _transcript_dir(adapters):
    directory, _, _ = adapters.source_transcript_dir()
    return directory


def _seed(adapters, lines=3):
    from pathlib import Path

    directory = Path(_transcript_dir(adapters))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "sess.jsonl"
    path.write_text(
        "".join(f'{{"i":{i}}}\n' for i in range(lines)), encoding="utf-8"
    )
    return path


# --------------------------------------------------------------------------
# The stop now waits for the file
# --------------------------------------------------------------------------


def test_a_stopped_agent_with_a_settled_transcript_passes(tmp_path) -> None:
    # Arrange: the ordinary case, and the positive control. Without it every
    # refusal below could pass because the fixture is broken.
    clock = _Clock()
    adapters = _adapters(tmp_path, clock)
    _seed(adapters)
    # Act
    result = adapters.stop_source()
    # Assert
    assert result.ok is True


def test_the_pass_says_the_file_was_read_twice(tmp_path) -> None:
    # Arrange: the whole change is that "stopped" is no longer the last word, so
    # the journal line has to record what else was established.
    clock = _Clock()
    adapters = _adapters(tmp_path, clock)
    _seed(adapters)
    # Act
    result = adapters.stop_source()
    # Assert
    assert "identically" in result.detail


def test_a_transcript_still_being_written_is_unknown_not_a_pass(tmp_path) -> None:
    # Arrange: THE 2026-08-12 failure. tmux has already reported the session
    # gone — positive evidence of absence, and true — while the process is still
    # flushing. The old code returned ok=True here and the transport read a file
    # that had moved since it was measured.
    clock = _Clock()
    adapters = _adapters(tmp_path, clock)
    path = _seed(adapters)
    flushing = _FlushingClock(path)
    adapters.now, adapters.sleep = flushing.now, flushing.sleep
    # Act
    result = adapters.stop_source()
    # Assert
    assert result.ok is None


def test_that_refusal_is_never_reported_as_a_failure_of_the_stop(tmp_path) -> None:
    # Arrange: the agent IS stopped. Calling this False would send someone to
    # stop a process that is already down; it is an unfinished measurement, and
    # UNKNOWN is the honest three-valued answer.
    clock = _Clock()
    adapters = _adapters(tmp_path, clock)
    path = _seed(adapters)
    flushing = _FlushingClock(path)
    adapters.now, adapters.sleep = flushing.now, flushing.sleep
    # Act
    result = adapters.stop_source()
    # Assert
    assert result.ok is not False


def test_the_refusal_names_what_was_still_changing(tmp_path) -> None:
    # Arrange: "not quiescent" without the file and the numbers is not something
    # anyone can act on — which was the complaint about the 422 it replaces.
    clock = _Clock()
    adapters = _adapters(tmp_path, clock)
    path = _seed(adapters)
    flushing = _FlushingClock(path)
    adapters.now, adapters.sleep = flushing.now, flushing.sleep
    # Act
    result = adapters.stop_source()
    # Assert
    assert "sess.jsonl" in result.detail


def test_the_refusal_still_reports_that_the_agent_is_stopped(tmp_path) -> None:
    # Arrange: the driver's recovery advice depends on knowing the source is
    # down. A refusal that omitted it would leave the operator guessing about the
    # one thing that changed.
    clock = _Clock()
    adapters = _adapters(tmp_path, clock)
    path = _seed(adapters)
    flushing = _FlushingClock(path)
    adapters.now, adapters.sleep = flushing.now, flushing.sleep
    # Act
    result = adapters.stop_source()
    # Assert
    assert "stopped" in result.detail


def test_an_already_stopped_agent_still_waits_for_the_file(tmp_path) -> None:
    # Arrange: THE exit the failing run actually took. tmux held no session at
    # all, so `stop_source` returned "already stopped" without issuing anything —
    # and that path has to go through the quiescence wait too, or the fix misses
    # the case it was written for.
    clock = _Clock()
    adapters = _adapters(tmp_path, clock)
    path = _seed(adapters)
    flushing = _FlushingClock(path)
    adapters.now, adapters.sleep = flushing.now, flushing.sleep
    # Act
    result = adapters.stop_source()
    # Assert
    assert result.ok is None and "already stopped" in result.detail


def test_an_underivable_transcript_directory_refuses(tmp_path) -> None:
    # Arrange: a stop cannot be called verified without watching what the stopped
    # process was writing. With no workdir there is nothing to watch, and that is
    # an unfinished measurement rather than a pass.
    clock = _Clock()
    adapters = _adapters(tmp_path, clock)
    adapters.spec = {"spec": {"apptainer": {"binds": ["/x:/home/agent"]}}}
    # Act
    result = adapters.stop_source()
    # Assert
    assert result.ok is None


# --------------------------------------------------------------------------
# The drain, unchanged — a stop is not a drain
# --------------------------------------------------------------------------


def test_a_stopped_agent_has_nothing_to_drain(tmp_path) -> None:
    # Arrange: vacuously true, and MEASURED rather than assumed — an agent with
    # no session cannot accept work.
    clock = _Clock()
    adapters = _adapters(tmp_path, clock)
    # Act
    result = adapters.drain_source()
    # Assert
    assert result.ok is True

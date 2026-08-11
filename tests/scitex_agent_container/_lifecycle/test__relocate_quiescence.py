#!/usr/bin/env python3
"""A stop that tmux confirmed, over a file that was still being written.

Every test here runs the REAL sampling script against REAL files under
``tmp_path``: the ``exec_fn`` seam hands the argv to ``subprocess.run``, so
``wc -c`` and ``stat`` actually execute and the byte counts are the ones the
filesystem reports. Nothing is mocked. That matters because the thing being
pinned is not arithmetic — it is whether a file that grows between two readings
can be mistaken for one that has settled.

The clock and the sleep ARE injected, and that is not a mock: they are the two
things a test must not spend. The fake sleep advances the fake clock by exactly
the interval it was asked to wait, so the deadline arithmetic is exercised at
full fidelity in no wall-clock time at all.
"""

from __future__ import annotations

import subprocess

import pytest

from scitex_agent_container._lifecycle._relocate_quiescence import (
    FileState,
    Quiescence,
    await_quiescence,
    describe_change,
    sample_transcripts,
)
from scitex_agent_container._lifecycle._relocate_shell import Shell

LOCAL = Shell(host="here", is_local=True)


def _real_exec(argv, timeout_s=None):
    """Actually run the argv. The seam the production code already takes."""
    done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
    return {
        "exit_code": done.returncode,
        "stdout": done.stdout,
        "stderr": done.stderr,
        "timed_out": False,
    }


class _Clock:
    """A clock that only moves when something sleeps. Real arithmetic, no waiting."""

    def __init__(self) -> None:
        self.t = 1000.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class _GrowingClock(_Clock):
    """A clock whose sleep also appends to a file — a flush still in flight."""

    def __init__(self, path, chunk: bytes) -> None:
        super().__init__()
        self.path = path
        self.chunk = chunk

    def sleep(self, seconds: float) -> None:
        super().sleep(seconds)
        with open(self.path, "ab") as handle:
            handle.write(self.chunk)


def _transcript(tmp_path, name="sess.jsonl", lines=3):
    directory = tmp_path / "projects" / "-home-agent-proj"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(
        "".join(f'{{"i":{i}}}\n' for i in range(lines)), encoding="utf-8"
    )
    return directory, path


# --------------------------------------------------------------------------
# Sampling: real sizes off a real filesystem
# --------------------------------------------------------------------------


def test_a_real_file_is_sampled_with_its_real_byte_count(tmp_path) -> None:
    # Arrange: the positive control for the sampler. Without it every "it
    # changed" below could pass because the script is broken rather than
    # because the detection works.
    directory, path = _transcript(tmp_path)
    # Act
    sample = sample_transcripts(LOCAL, str(directory), exec_fn=_real_exec)
    # Assert
    assert sample[0].byte_count == path.stat().st_size


def test_a_directory_with_no_transcript_samples_empty_rather_than_unknown(
    tmp_path,
) -> None:
    # Arrange: an observed "nothing here" is an answer. Collapsing it into
    # "could not tell" would refuse a relocation of an agent that has not
    # spoken yet.
    directory = tmp_path / "empty"
    directory.mkdir()
    # Act
    sample = sample_transcripts(LOCAL, str(directory), exec_fn=_real_exec)
    # Assert
    assert sample == ()


def test_a_missing_directory_is_an_observed_empty_sample(tmp_path) -> None:
    # Arrange: there is no transcript there to still be moving. Distinct from
    # a script that never answered, which is the case below.
    # Act
    sample = sample_transcripts(LOCAL, str(tmp_path / "nope"), exec_fn=_real_exec)
    # Assert
    assert sample == ()


def test_a_sample_ignores_files_that_are_not_transcripts(tmp_path) -> None:
    # Arrange: a log file beside the transcript churns constantly and is not
    # what the transport reads. Watching it would never settle.
    directory, _ = _transcript(tmp_path)
    (directory / "notes.md").write_text("x", encoding="utf-8")
    # Act
    sample = sample_transcripts(LOCAL, str(directory), exec_fn=_real_exec)
    # Assert
    assert [f.name for f in sample] == ["sess.jsonl"]


def test_a_sample_is_ordered_by_name_so_two_readings_can_be_compared(
    tmp_path,
) -> None:
    # Arrange: equality is the whole mechanism. Two readings of an unchanged
    # directory that differ only in emission order would restart the wait
    # forever.
    directory, _ = _transcript(tmp_path)
    _transcript(tmp_path, name="aaa.jsonl")
    # Act
    first = sample_transcripts(LOCAL, str(directory), exec_fn=_real_exec)
    second = sample_transcripts(LOCAL, str(directory), exec_fn=_real_exec)
    # Assert
    assert first == second


# --------------------------------------------------------------------------
# The wait: settled, still moving, not measurable
# --------------------------------------------------------------------------


def test_a_file_that_is_not_changing_settles(tmp_path) -> None:
    # Arrange: the ordinary case. A stopped agent's transcript reads the same
    # twice and the phase may proceed.
    directory, _ = _transcript(tmp_path)
    clock = _Clock()
    # Act
    verdict = await_quiescence(
        LOCAL,
        str(directory),
        exec_fn=_real_exec,
        now=clock.now,
        sleep=clock.sleep,
    )
    # Assert
    assert verdict.settled is True


def test_a_file_still_being_written_is_unknown_not_a_pass(tmp_path) -> None:
    # Arrange: THE 2026-08-12 case, reproduced. tmux has already said the
    # session is gone; the dying process is still flushing. Every sleep appends,
    # so no two readings ever agree and the deadline arrives.
    directory, path = _transcript(tmp_path)
    clock = _GrowingClock(path, b'{"flush":1}\n')
    # Act
    verdict = await_quiescence(
        LOCAL,
        str(directory),
        exec_fn=_real_exec,
        now=clock.now,
        sleep=clock.sleep,
    )
    # Assert
    assert verdict.settled is None


def test_a_file_still_being_written_is_never_reported_as_settled(tmp_path) -> None:
    # Arrange: the same run, stated as the property that must hold. A timeout
    # folded into success is what let the failing relocation reach the copy.
    directory, path = _transcript(tmp_path)
    clock = _GrowingClock(path, b'{"flush":1}\n')
    # Act
    verdict = await_quiescence(
        LOCAL,
        str(directory),
        exec_fn=_real_exec,
        now=clock.now,
        sleep=clock.sleep,
    )
    # Assert
    assert verdict.settled is not True


def test_the_timeout_names_the_file_that_was_still_changing(tmp_path) -> None:
    # Arrange: "not quiescent" without the evidence is not something anyone can
    # act on — which was the complaint about the 422 that prompted this module.
    directory, path = _transcript(tmp_path)
    clock = _GrowingClock(path, b'{"flush":1}\n')
    # Act
    verdict = await_quiescence(
        LOCAL,
        str(directory),
        exec_fn=_real_exec,
        now=clock.now,
        sleep=clock.sleep,
    )
    # Assert
    assert "sess.jsonl" in verdict.detail


def test_the_timeout_reports_the_size_it_grew_by(tmp_path) -> None:
    # Arrange: the numbers are the point. A reader must be able to see that the
    # file is gaining bytes rather than merely "changing".
    directory, path = _transcript(tmp_path)
    clock = _GrowingClock(path, b"0123456789\n")
    # Act
    verdict = await_quiescence(
        LOCAL,
        str(directory),
        exec_fn=_real_exec,
        now=clock.now,
        sleep=clock.sleep,
    )
    # Assert
    assert "+11" in verdict.detail


def test_a_file_that_settles_after_a_late_flush_is_confirmed(tmp_path) -> None:
    # Arrange: the realistic shutdown. One flush lands, then the process lets go
    # and the next two readings agree — which must be a PASS, or the fix would
    # simply move the failure from a bad 422 to a spurious timeout.
    directory, path = _transcript(tmp_path)

    class _OneFlush(_Clock):
        def __init__(self) -> None:
            super().__init__()
            self.flushed = False

        def sleep(self, seconds: float) -> None:
            super().sleep(seconds)
            if not self.flushed:
                self.flushed = True
                with open(path, "ab") as handle:
                    handle.write(b'{"last":true}\n')

    clock = _OneFlush()
    # Act
    verdict = await_quiescence(
        LOCAL,
        str(directory),
        exec_fn=_real_exec,
        now=clock.now,
        sleep=clock.sleep,
    )
    # Assert
    assert verdict.settled is True


def test_a_new_transcript_appearing_counts_as_movement(tmp_path) -> None:
    # Arrange: the SET is part of the reading. A session file created after the
    # stop means something is still running, and comparing only the files we
    # happened to see first would miss it.
    directory, _ = _transcript(tmp_path)

    class _Spawner(_Clock):
        def __init__(self) -> None:
            super().__init__()
            self.n = 0

        def sleep(self, seconds: float) -> None:
            super().sleep(seconds)
            self.n += 1
            (directory / f"new-{self.n}.jsonl").write_text("{}\n", encoding="utf-8")

    clock = _Spawner()
    # Act
    verdict = await_quiescence(
        LOCAL,
        str(directory),
        exec_fn=_real_exec,
        now=clock.now,
        sleep=clock.sleep,
    )
    # Assert
    assert verdict.settled is None


def test_a_reading_that_could_not_be_taken_refuses(tmp_path) -> None:
    # Arrange: a shell that ran and printed nothing measured nothing. Treating
    # silence as "no files, therefore settled" is how an unreadable directory
    # becomes a confident green light.
    def _silent(argv, timeout_s=None):
        return {"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False}

    clock = _Clock()
    # Act
    verdict = await_quiescence(
        LOCAL,
        str(tmp_path),
        exec_fn=_silent,
        now=clock.now,
        sleep=clock.sleep,
    )
    # Assert
    assert verdict.settled is None


def test_an_unmeasured_byte_count_refuses_rather_than_comparing_two_blanks(
    tmp_path,
) -> None:
    # Arrange: two unmeasured readings compare EQUAL, which would manufacture
    # exactly the false "it has settled" this module exists to prevent.
    def _blank_counts(argv, timeout_s=None):
        return {
            "exit_code": 0,
            "stdout": "TX-QDIR=yes\nTX-QUIET=sess.jsonl\t\t\n",
            "stderr": "",
            "timed_out": False,
        }

    clock = _Clock()
    # Act
    verdict = await_quiescence(
        LOCAL,
        str(tmp_path),
        exec_fn=_blank_counts,
        now=clock.now,
        sleep=clock.sleep,
    )
    # Assert
    assert verdict.settled is None


def test_the_wait_is_bounded(tmp_path) -> None:
    # Arrange: a host whose transcript never settles must fail the phase, not
    # sit in front of the copy's much larger budget forever.
    directory, path = _transcript(tmp_path)
    clock = _GrowingClock(path, b"x\n")
    # Act
    await_quiescence(
        LOCAL,
        str(directory),
        exec_fn=_real_exec,
        now=clock.now,
        sleep=clock.sleep,
        timeout_s=6.0,
        interval_s=2.0,
    )
    # Assert
    assert clock.t <= 1000.0 + 6.0 + 2.0


# --------------------------------------------------------------------------
# The pure parts
# --------------------------------------------------------------------------


def test_growth_is_described_with_both_sizes_and_the_delta() -> None:
    # Arrange: the exact shape of the 2026-08-12 abort, as this module would
    # have reported it BEFORE the copy rather than after.
    before = [FileState("s.jsonl", byte_count=108412278, mtime="1")]
    after = [FileState("s.jsonl", byte_count=108423995, mtime="2")]
    # Act
    described = describe_change(before, after)
    # Assert
    assert "+11717" in described


def test_a_same_size_rewrite_is_still_movement() -> None:
    # Arrange: why mtime is read at all. A file rewritten in place at the same
    # length is not a file at rest, and size alone would call it settled.
    before = [FileState("s.jsonl", byte_count=10, mtime="100")]
    after = [FileState("s.jsonl", byte_count=10, mtime="200")]
    # Act
    described = describe_change(before, after)
    # Assert
    assert "rewritten in place" in described


def test_an_unsettled_verdict_must_say_what_to_do_next() -> None:
    # Arrange: the invariant lives in the type. A refusal with no next action
    # turns a one-command fix into an investigation.
    build = lambda: Quiescence(settled=None, detail="still moving")  # noqa: E731
    # Act
    attempt = build
    # Assert
    with pytest.raises(ValueError):
        attempt()

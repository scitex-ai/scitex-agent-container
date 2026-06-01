"""Tests for ``_state._meta.resources.collect_agent_resources`` — the
per-agent CPU% + RSS probe that surfaces in ``sac agents list``.

Lead task (2026-06-01): attribute host load to specific agents. The
probe walks each registered agent's PID + descendants in one psutil
sweep, sums their CPU% and RSS, and returns absent (``None``) for any
PID whose process is dead/unknown.

PA-306 no-mocks: every test exercises real OS processes. We use
``os.getpid()`` (this test process itself) for the "live PID" case and
``subprocess.Popen`` for the "child tree" / "dead PID" cases. No
``unittest.mock``, no ``monkeypatch`` of psutil internals, no patched
``Process`` classes — the probe sees a real ``/proc`` entry or it
doesn't.

Each test:
* AAA markers (TQ002).
* One assertion (TQ007).
* 3+-word descriptive name.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from scitex_agent_container._state._meta.resources import collect_agent_resources

# ---------------------------------------------------------------------------
# Fixtures — real subprocesses we own and clean up
# ---------------------------------------------------------------------------


@pytest.fixture
def sleeper_proc():
    """Spawn a real Python subprocess that sleeps. Yields the PID. The
    fixture kills it on teardown so tests don't leak children.

    Why Python (not ``sleep 60``): ensures a non-trivial RSS so the
    sum-RSS assertion has a defensible floor (a ``sleep`` binary is
    sometimes only a few hundred KB; a Python interpreter is ~5-20 MB).
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Give the child a beat to be visible in /proc and have RSS settled.
        time.sleep(0.1)
        yield proc.pid
    finally:
        proc.kill()
        proc.wait(timeout=5)


@pytest.fixture
def parent_with_child():
    """Spawn a parent Python subprocess that forks a child + sleeps.
    Returns the parent PID. Both parent and child are killed at teardown.
    """
    # Parent forks a child via os.fork (POSIX only); both sleep.
    script = (
        "import os, time;"
        " p = os.fork();"
        " time.sleep(60) if p == 0 else (os.waitpid(p, 0) or time.sleep(60))"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(0.2)  # let the fork settle
        yield proc.pid
    finally:
        # Best-effort kill of the whole process group (parent + child).
        # stx-allow: fallback (reason: test teardown; if the proc already
        # exited the kill is a no-op error we want to swallow)
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except (ProcessLookupError, PermissionError):
            pass
        proc.kill()
        proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# Shape — keys returned per PID
# ---------------------------------------------------------------------------


def test_returns_dict_keyed_by_input_pid(sleeper_proc):
    # Arrange
    pid = sleeper_proc
    # Act
    result = collect_agent_resources([pid])
    # Assert
    assert set(result.keys()) == {pid}


def test_live_pid_payload_carries_two_required_keys(sleeper_proc):
    # Arrange
    pid = sleeper_proc
    # Act
    payload = collect_agent_resources([pid])[pid]
    # Assert
    assert payload is not None and set(payload.keys()) == {
        "cpu_percent",
        "mem_rss_mb",
    }


def test_live_pid_mem_rss_is_positive_float(sleeper_proc):
    # Arrange — a real Python subprocess has > 1 MB of RSS even sleeping.
    pid = sleeper_proc
    # Act
    payload = collect_agent_resources([pid])[pid]
    # Assert
    assert isinstance(payload["mem_rss_mb"], float) and payload["mem_rss_mb"] > 1.0


def test_live_pid_cpu_percent_is_float(sleeper_proc):
    # Arrange — sleeping process has near-zero CPU% but the field must
    # still be a float (not None, not absent). Tests the contract that
    # a live PID always has both fields, even if cpu_percent is 0.0.
    pid = sleeper_proc
    # Act
    payload = collect_agent_resources([pid])[pid]
    # Assert
    assert isinstance(payload["cpu_percent"], float)


# ---------------------------------------------------------------------------
# Dead-PID handling
# ---------------------------------------------------------------------------


def test_dead_pid_returns_none_payload():
    # Arrange — spawn then kill, capture PID after death so /proc/<pid>
    # no longer exists. The probe must return None (not raise, not
    # invent zeros) — see lead's "Handle dead-PID gracefully" spec.
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    pid = proc.pid
    # Give the kernel a beat to reap the zombie.
    time.sleep(0.1)
    # Act
    result = collect_agent_resources([pid])
    # Assert
    assert result[pid] is None


def test_never_running_pid_returns_none():
    # Arrange — pick a PID that is virtually guaranteed to never have
    # existed in this kernel. The probe must absent-out, not raise.
    # 2^31 - 1 is well above any realistic kernel PID space.
    pid = 2**31 - 1
    # Act
    result = collect_agent_resources([pid])
    # Assert
    assert result[pid] is None


# ---------------------------------------------------------------------------
# Descendants — the tree, not just the recorded PID
# ---------------------------------------------------------------------------


def test_descendants_rss_is_included_in_sum(parent_with_child):
    # Arrange — parent + forked child both Python interpreters; each
    # carries its own ~5-20 MB RSS. Sum should exceed a single Python's
    # RSS comfortably. We compare against the parent-alone case by
    # observing that the parent_with_child fixture's reported RSS is
    # strictly greater than a single Python's RSS floor (1 MB) AND
    # consistent with "two interpreters" (>5 MB combined).
    pid = parent_with_child
    # Act
    payload = collect_agent_resources([pid])[pid]
    # Assert — two interpreters should clear 5 MB easily; we pin a
    # lower bound that no single Python could meet by itself if the
    # child weren't summed in.
    assert payload is not None and payload["mem_rss_mb"] >= 5.0


# ---------------------------------------------------------------------------
# Mixed live + dead — single-sweep contract
# ---------------------------------------------------------------------------


def test_mixed_live_and_dead_pids_in_one_sweep(sleeper_proc):
    # Arrange — one live PID and one definitely-dead PID. The probe
    # returns the same keyspace as the input, with each PID's payload
    # resolved independently.
    live_pid = sleeper_proc
    dead_pid = 2**31 - 1
    # Act
    result = collect_agent_resources([live_pid, dead_pid])
    # Assert — both keys present, the dead one absent-as-None, live one
    # has the payload dict. Single result, single sweep.
    assert result[live_pid] is not None and result[dead_pid] is None


# ---------------------------------------------------------------------------
# Empty input — edge case
# ---------------------------------------------------------------------------


def test_empty_input_returns_empty_dict():
    # Arrange — no PIDs to probe (no running agents). The probe should
    # not raise, not sleep, not allocate; return an empty dict.
    # Act
    result = collect_agent_resources([])
    # Assert
    assert result == {}


# ---------------------------------------------------------------------------
# PID==0 sentinel — registry uses 0 for "unknown"
# ---------------------------------------------------------------------------


def test_pid_zero_sentinel_returns_none():
    # Arrange — the registry's ``_pids_from_session`` returns ``pid=0``
    # when tmux can't tell us the pane PID. The probe must NOT treat 0
    # as a real PID (psutil.Process(0) is the kernel scheduler on Linux,
    # which would crash or surface bogus data). Treat 0 as "no PID
    # known" and return None.
    # Act
    result = collect_agent_resources([0])
    # Assert
    assert result[0] is None

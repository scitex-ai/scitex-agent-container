"""The poller-singleton detector must FIRE on a duplicate it can actually see.

THE POSITIVE CONTROL IS THE POINT. A detector that has never returned a known
answer is not a measurement, so the load-bearing tests here CONSTRUCT the
fault: two REAL live processes, spawned by the test, whose argv names
``telegram-server.ts`` and whose environment carries the SAME
``CCT_BOT_TOKEN``. That is the 409 condition, and the check must call it a
VIOLATION. "Returns ok on a healthy host" would pass for a function that
returns the string "ok" unconditionally.

NO MOCKS ANYWHERE. The processes are real, ``/proc`` is the real ``/proc``,
and the reader is the real reader. The only thing the test controls is the
POPULATION: it builds a scan root of symlinks pointing at exactly the pids it
spawned, so a live poller belonging to some other agent on this host can
neither hide a duplicate nor invent one. Scoping the population is not faking
the instrument.

THE THIRD VALUE IS TESTED TOO. A live poller whose token cannot be read must
come back UNKNOWN, never OK — an unread process could be the twin of a read
one, and collapsing that into an all-clear is the bug this fleet ships most.

PA-307 / STX-TQ002 / STX-TQ007 — one assert per test, full AAA markers.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from scitex_agent_container.runtimes._cct_poller_scan import (
    UNRESOLVED_ENVIRON,
    LivePoller,
    is_poller_argv,
)
from scitex_agent_container.runtimes._cct_poller_singleton import (
    POLLER_OK,
    POLLER_UNKNOWN,
    POLLER_VIOLATION,
    check_poller_singleton,
    verdict_for,
)

#: A token-shaped string that must never appear in any output.
_TOKEN_A = "8123456789:AAHduplicate-bot-token-value-A"
_TOKEN_B = "9987654321:AAHdistinct-bot-token-value-B"


class _PollerFarm:
    """Spawns REAL poller-shaped processes and scopes a /proc view onto them.

    The child is a shell script named ``telegram-server.ts`` invoked through a
    stand-in ``bun``, i.e. exactly the argv shape the real launcher produces.
    It blocks on ``read`` from its stdin pipe, so it holds its cmdline and its
    environment for as long as the test needs and dies on its own the moment
    the pipe closes — no forked ``sleep`` to leak.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.proc_root = tmp_path / "proc"
        self.proc_root.mkdir()
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        self.bun = bin_dir / "bun"
        self.bun.write_text("#!/bin/sh\nread line\n")
        self.bun.chmod(0o755)
        ts_dir = tmp_path / "ts"
        ts_dir.mkdir()
        self.script = ts_dir / "telegram-server.ts"
        self.script.write_text("// stand-in for the real poller\n")
        self._procs: list[subprocess.Popen] = []

    def spawn(self, *, token: str | None, agent: str = "") -> int:
        """Start one live poller; return its pid once /proc shows it."""
        env = {"PATH": "/usr/bin:/bin"}
        if token is not None:
            env["CCT_BOT_TOKEN"] = token
        if agent:
            env["CCT_AGENT_ID"] = agent
        proc = subprocess.Popen(
            [str(self.bun), "run", str(self.script)],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._procs.append(proc)
        self._await_exec(proc.pid)
        (self.proc_root / str(proc.pid)).symlink_to(f"/proc/{proc.pid}")
        return proc.pid

    @staticmethod
    def _await_exec(pid: int, timeout: float = 10.0) -> None:
        """Block until the child's real cmdline names the poller script."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
            argv = [t for t in raw.decode("utf-8", "replace").split("\0") if t]
            if is_poller_argv(argv):
                return
            time.sleep(0.02)
        raise AssertionError(f"pid {pid} never exec'd into a poller-shaped argv")

    def close(self) -> None:
        for proc in self._procs:
            proc.kill()
            proc.wait(timeout=10)


@pytest.fixture
def farm(tmp_path):
    """A live poller farm, torn down after the test."""
    made = _PollerFarm(tmp_path)
    try:
        yield made
    finally:
        made.close()


# ---------------------------------------------------------------------------
# POSITIVE CONTROL — the fault, constructed with real processes
# ---------------------------------------------------------------------------


def test_two_real_pollers_on_one_token_is_a_violation(farm):
    # Arrange — THE FAULT: a restart left the old poller alive and the new one
    # started anyway, both on the same bot token.
    farm.spawn(token=_TOKEN_A, agent="scitex-agent-container")
    farm.spawn(token=_TOKEN_A, agent="scitex-agent-container")
    # Act
    verdict = check_poller_singleton(proc_root=farm.proc_root)
    # Assert
    assert verdict.state == POLLER_VIOLATION


def test_the_violation_names_both_offending_pids(farm):
    # Arrange
    first = farm.spawn(token=_TOKEN_A, agent="scitex-agent-container")
    second = farm.spawn(token=_TOKEN_A, agent="scitex-agent-container")
    # Act
    verdict = check_poller_singleton(proc_root=farm.proc_root)
    # Assert
    assert sorted(verdict.duplicates[0].pids) == sorted([first, second])


def test_the_violation_names_the_shared_fingerprint(farm):
    # Arrange
    farm.spawn(token=_TOKEN_A, agent="scitex-agent-container")
    farm.spawn(token=_TOKEN_A, agent="scitex-agent-container")
    # Act
    verdict = check_poller_singleton(proc_root=farm.proc_root)
    # Assert
    assert verdict.duplicates[0].token_fp.startswith("sha256:")


def test_the_violation_names_the_owning_agent(farm):
    # Arrange
    farm.spawn(token=_TOKEN_A, agent="scitex-agent-container")
    farm.spawn(token=_TOKEN_A, agent="scitex-agent-container")
    # Act
    verdict = check_poller_singleton(proc_root=farm.proc_root)
    # Assert
    assert verdict.duplicates[0].agents == ("scitex-agent-container",)


def test_the_violation_carries_an_actionable_hint(farm):
    # Arrange
    farm.spawn(token=_TOKEN_A, agent="scitex-agent-container")
    farm.spawn(token=_TOKEN_A, agent="scitex-agent-container")
    # Act
    verdict = check_poller_singleton(proc_root=farm.proc_root)
    # Assert
    assert "SIGKILL" in verdict.hint()


def test_no_token_value_appears_in_the_violation_report(farm):
    # Arrange — the whole report is serialised and searched for the secret.
    farm.spawn(token=_TOKEN_A, agent="scitex-agent-container")
    farm.spawn(token=_TOKEN_A, agent="scitex-agent-container")
    # Act
    blob = json.dumps(check_poller_singleton(proc_root=farm.proc_root).to_dict())
    # Assert
    assert _TOKEN_A not in blob and "AAHduplicate" not in blob


# ---------------------------------------------------------------------------
# The other three answers
# ---------------------------------------------------------------------------


def test_one_live_poller_is_ok(farm):
    # Arrange
    farm.spawn(token=_TOKEN_A, agent="scitex-agent-container")
    # Act
    verdict = check_poller_singleton(proc_root=farm.proc_root)
    # Assert
    assert verdict.state == POLLER_OK


def test_two_pollers_on_distinct_tokens_is_ok(farm):
    # Arrange — two agents, two bots. The invariant is per TOKEN, not per host.
    farm.spawn(token=_TOKEN_A, agent="scitex-agent-container")
    farm.spawn(token=_TOKEN_B, agent="scitex-cards")
    # Act
    verdict = check_poller_singleton(proc_root=farm.proc_root)
    # Assert
    assert verdict.state == POLLER_OK


def test_zero_live_pollers_is_ok(farm):
    # Arrange — nothing spawned; the scan RAN and found none.
    # Act
    verdict = check_poller_singleton(proc_root=farm.proc_root)
    # Assert
    assert verdict.state == POLLER_OK


def test_a_poller_with_no_readable_token_is_unknown(farm):
    # Arrange — THE LOAD-BEARING NEGATIVE: a live poller sac cannot read a
    # token for. Reporting OK here would be an all-clear from a blind check.
    farm.spawn(token=None, agent="scitex-agent-container")
    # Act
    verdict = check_poller_singleton(proc_root=farm.proc_root)
    # Assert
    assert verdict.state == POLLER_UNKNOWN


def test_an_unresolved_poller_is_not_folded_into_ok(farm):
    # Arrange — one readable poller alongside one unreadable one. The readable
    # one alone would satisfy the invariant; the unreadable one could be its
    # twin, so the honest answer is still unknown.
    farm.spawn(token=_TOKEN_A, agent="scitex-agent-container")
    farm.spawn(token=None, agent="")
    # Act
    verdict = check_poller_singleton(proc_root=farm.proc_root)
    # Assert
    assert verdict.state == POLLER_UNKNOWN


def test_the_unknown_verdict_names_the_unresolved_pid(farm):
    # Arrange
    blind = farm.spawn(token=None, agent="scitex-agent-container")
    # Act
    verdict = check_poller_singleton(proc_root=farm.proc_root)
    # Assert
    assert verdict.unresolved_pids == (blind,)


def test_the_unknown_verdict_carries_an_actionable_hint(farm):
    # Arrange — this poller's environ IS readable and simply has no token, so
    # the remedy is "find the process", NOT "re-run as another user".
    farm.spawn(token=None, agent="scitex-agent-container")
    # Act
    verdict = check_poller_singleton(proc_root=farm.proc_root)
    # Assert
    assert "outside sac's env" in verdict.hint()


def test_an_unreadable_environ_hint_names_the_vantage():
    # Arrange — the OTHER unresolved reason. Sending a reader who hit a
    # cross-uid /proc to "restart it through sac" wastes the one action they
    # could have taken, so the two hints must differ.
    pollers = [LivePoller(pid=7, token_fp=None, reason=UNRESOLVED_ENVIRON)]
    # Act
    verdict = verdict_for(pollers)
    # Assert
    assert "OWNER-ONLY" in verdict.hint()


def test_an_unscannable_proc_root_is_unknown(tmp_path):
    # Arrange — nobody looked. Zero pollers found this way is not zero pollers.
    missing = tmp_path / "no-such-proc"
    # Act
    verdict = check_poller_singleton(proc_root=missing)
    # Assert
    assert verdict.state == POLLER_UNKNOWN


def test_an_unscannable_proc_root_records_that_it_did_not_scan(tmp_path):
    # Arrange
    missing = tmp_path / "no-such-proc"
    # Act
    verdict = check_poller_singleton(proc_root=missing)
    # Assert
    assert verdict.scanned is False


# ---------------------------------------------------------------------------
# The decision, without a process
# ---------------------------------------------------------------------------


def test_a_violation_outranks_an_unresolved_poller():
    # Arrange — an unread third process must not mute an observed duplicate.
    pollers = [
        LivePoller(pid=1, token_fp="sha256:aaaaaaaaaaaa"),
        LivePoller(pid=2, token_fp="sha256:aaaaaaaaaaaa"),
        LivePoller(pid=3, token_fp=None, detail="environ unreadable"),
    ]
    # Act
    verdict = verdict_for(pollers)
    # Assert
    assert verdict.state == POLLER_VIOLATION


def test_the_ok_verdict_carries_no_hint():
    # Arrange
    pollers = [LivePoller(pid=1, token_fp="sha256:aaaaaaaaaaaa")]
    # Act
    verdict = verdict_for(pollers)
    # Assert
    assert verdict.hint() == ""


def test_the_invariant_is_stated_in_the_ok_detail():
    # Arrange
    pollers = [
        LivePoller(pid=1, token_fp="sha256:aaaaaaaaaaaa"),
        LivePoller(pid=2, token_fp="sha256:bbbbbbbbbbbb"),
    ]
    # Act
    verdict = verdict_for(pollers)
    # Assert
    assert verdict.distinct_fingerprints == len(verdict.pollers)

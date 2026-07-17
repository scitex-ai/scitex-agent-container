"""Tests for the push-config remote I/O (read/write transport).

PA-306: no ``unittest.mock``. The runner seam takes a REAL callable that
returns real :class:`subprocess.CompletedProcess` objects (or raises a
real ``subprocess.TimeoutExpired``) — the same injection style
``check_peer`` uses. Marker strings are hard-coded on purpose: they are
the wire format, and a test that imported them would follow a drift it
exists to catch. Each test: AAA (TQ002), one assertion (TQ007),
behaviour-shaped name (TQ003).
"""

from __future__ import annotations

import base64
import subprocess

from scitex_agent_container._hostsync._push_config_io import (
    read_peer_config,
    render_read_snippet,
    render_write_snippet,
    write_peer_config,
)
from scitex_agent_container._state.host_config import PeerSpec

_PEERS = {"spartan": PeerSpec(name="spartan", ssh="spartan")}


class _ScriptedRunner:
    """Injectable runner: pops real ``CompletedProcess`` results in order.

    An instance is a plain callable (no mock library); appending every
    call to ``.calls`` lets tests assert argv/kwargs shape, and a
    scripted BaseException instance is raised instead of returned.
    """

    def __init__(self, *results):
        self._results = list(results)
        self.calls: list[tuple[list, dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))
        result = self._results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def _proc(stdout: str = "", rc: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["ssh"], returncode=rc, stdout=stdout, stderr=stderr
    )


def _b64_block(text: str) -> str:
    b64 = base64.b64encode(text.encode()).decode()
    return f"SAC_PUSHCFG b64={b64}\nSAC_PUSHCFG end\n"


# ---------------------------------------------------------------------------
# snippets
# ---------------------------------------------------------------------------


def test_read_snippet_expands_home_on_the_peer():
    # Arrange
    # (the path must be expanded REMOTELY — a local ~ is the footgun)
    # Act
    snippet = render_read_snippet()
    # Assert
    assert '"$HOME/.scitex/agent-container/config.yaml"' in snippet


def test_write_snippet_lands_via_atomic_mv():
    # Arrange
    # (a dropped connection must never leave a half-written config)
    # Act
    snippet = render_write_snippet()
    # Assert
    assert 'mv "$p.tmp" "$p"' in snippet


def test_write_snippet_without_stamp_takes_no_backup():
    # Arrange
    # (plain pushes overwrite only our own generated output)
    # Act
    snippet = render_write_snippet()
    # Assert
    assert "pre-adopt" not in snippet


def test_write_snippet_with_stamp_backs_up_first():
    # Arrange
    stamp = "20260716T080000Z"
    # Act
    snippet = render_write_snippet(backup_stamp=stamp)
    # Assert
    assert 'cp -p "$p" "$p.pre-adopt-20260716T080000Z"' in snippet


# ---------------------------------------------------------------------------
# read_peer_config
# ---------------------------------------------------------------------------


def test_read_returns_decoded_text_for_present_file():
    # Arrange
    runner = _ScriptedRunner(_proc(stdout=_b64_block("peers: {}\n")))
    # Act
    remote = read_peer_config("spartan", _PEERS, runner=runner)
    # Assert
    assert remote.text == "peers: {}\n"


def test_read_reports_absent_on_the_absent_marker():
    # Arrange — __ABSENT__ is POSITIVE evidence from the peer's shell.
    runner = _ScriptedRunner(_proc(stdout="SAC_PUSHCFG __ABSENT__\nSAC_PUSHCFG end\n"))
    # Act
    remote = read_peer_config("spartan", _PEERS, runner=runner)
    # Assert
    assert remote.absent


def test_read_without_end_marker_is_not_ok():
    # Arrange — a truncated probe told us NOTHING; it must never read as
    # absent (push mode would then CREATE over an unseen peer).
    runner = _ScriptedRunner(_proc(stdout="", rc=255, stderr="connect refused\n"))
    # Act
    remote = read_peer_config("spartan", _PEERS, runner=runner)
    # Assert
    assert not remote.ok


def test_read_names_the_ssh_exit_code_when_undetermined():
    # Arrange
    runner = _ScriptedRunner(_proc(stdout="", rc=255, stderr="connect refused\n"))
    # Act
    remote = read_peer_config("spartan", _PEERS, runner=runner)
    # Assert
    assert "ssh exit 255" in remote.detail


def test_read_reports_unreadable_file_as_not_ok():
    # Arrange — exists-but-unreadable is UNKNOWN, not absent, not clean.
    runner = _ScriptedRunner(_proc(stdout="SAC_PUSHCFG unreadable\nSAC_PUSHCFG end\n"))
    # Act
    remote = read_peer_config("spartan", _PEERS, runner=runner)
    # Assert
    assert not remote.ok


def test_read_timeout_degrades_to_not_ok():
    # Arrange — a real TimeoutExpired, raised by the scripted callable.
    runner = _ScriptedRunner(subprocess.TimeoutExpired(cmd=["ssh"], timeout=30))
    # Act
    remote = read_peer_config("spartan", _PEERS, runner=runner)
    # Assert
    assert not remote.ok


def test_read_unknown_peer_is_not_ok():
    # Arrange
    runner = _ScriptedRunner()
    # Act
    remote = read_peer_config("ghost", {}, runner=runner)
    # Assert
    assert not remote.ok


def test_read_garbage_base64_is_not_ok():
    # Arrange — undecodable content is UNKNOWN, never an empty config.
    runner = _ScriptedRunner(
        _proc(stdout="SAC_PUSHCFG b64=!!not-base64!!\nSAC_PUSHCFG end\n")
    )
    # Act
    remote = read_peer_config("spartan", _PEERS, runner=runner)
    # Assert
    assert not remote.ok


def test_read_dispatches_through_ssh_argv():
    # Arrange
    runner = _ScriptedRunner(_proc(stdout=_b64_block("x: 1\n")))
    # Act
    read_peer_config("spartan", _PEERS, runner=runner)
    # Assert — the choke point: argv is an ssh invocation of the peer.
    assert runner.calls[0][0][0] == "ssh"


# ---------------------------------------------------------------------------
# write_peer_config
# ---------------------------------------------------------------------------


def test_write_pipes_content_via_stdin():
    # Arrange — the file body must ride stdin, never the argv.
    runner = _ScriptedRunner(_proc())
    # Act
    write_peer_config("spartan", _PEERS, "host:\n  canonical: spartan\n", runner=runner)
    # Assert
    assert runner.calls[0][1]["input"] == "host:\n  canonical: spartan\n"


def test_write_reports_ok_on_zero_exit():
    # Arrange
    runner = _ScriptedRunner(_proc(rc=0))
    # Act
    ok, _detail = write_peer_config("spartan", _PEERS, "x: 1\n", runner=runner)
    # Assert
    assert ok


def test_write_reports_failure_on_nonzero_exit():
    # Arrange
    runner = _ScriptedRunner(_proc(rc=1, stderr="disk full\n"))
    # Act
    ok, _detail = write_peer_config("spartan", _PEERS, "x: 1\n", runner=runner)
    # Assert
    assert not ok


def test_write_failure_detail_carries_the_remote_stderr():
    # Arrange
    runner = _ScriptedRunner(_proc(rc=1, stderr="disk full\n"))
    # Act
    _ok, detail = write_peer_config("spartan", _PEERS, "x: 1\n", runner=runner)
    # Assert
    assert "disk full" in detail


def test_write_backup_stamp_reaches_the_dispatched_argv():
    # Arrange
    runner = _ScriptedRunner(_proc())
    # Act
    write_peer_config(
        "spartan", _PEERS, "x: 1\n", backup_stamp="20260716T080000Z", runner=runner
    )
    # Assert — the adopt backup rides the same single dispatch.
    assert "pre-adopt-20260716T080000Z" in " ".join(runner.calls[0][0])


def test_write_unknown_peer_fails_without_dispatch():
    # Arrange
    runner = _ScriptedRunner()
    # Act
    ok, _detail = write_peer_config("ghost", {}, "x: 1\n", runner=runner)
    # Assert
    assert not ok

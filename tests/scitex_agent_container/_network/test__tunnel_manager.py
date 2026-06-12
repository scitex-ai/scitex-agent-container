"""Tests for ``_network._tunnel_manager.TunnelManager``.

Real subprocesses for the supervisor — no mocks. Tests pass a
``supervisor_cmd`` that points at a tiny ``python -c '...'`` one-liner
which opens a listening socket on the requested local_port and
selects forever; that lets the manager exercise the real spawn + poll
+ kill loop without needing actual ssh or a real bastion.

Each test pins one observable fact (TQ007), AAA markers (TQ002),
descriptive name with >=3 tokens (TQ003).
"""

from __future__ import annotations

import socket
import sys
import time

import pytest

from scitex_agent_container._network._tunnel_manager import (
    TunnelManager,
    TunnelUpError,
)
from scitex_agent_container.config._tunnel_types import TunnelSpec

# A fake supervisor that ignores its ssh-specific argv but parses the
# manager's --local-port flag, binds a TCP listening socket on that
# port, and blocks until SIGTERM. Implemented as a python one-liner so
# the test never depends on a separate file.
_FAKE_SUPERVISOR_LISTEN = (
    "import argparse,socket,select,signal,sys;"
    "p=argparse.ArgumentParser();"
    "p.add_argument('--jump');"
    "p.add_argument('--target');"
    "p.add_argument('--remote-port', type=int);"
    "p.add_argument('--local-port', type=int);"
    "p.add_argument('--backoff', type=float, default=0.1);"
    "p.add_argument('--ssh-opt', action='append', default=[]);"
    "a=p.parse_args();"
    "s=socket.socket(socket.AF_INET, socket.SOCK_STREAM);"
    "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1);"
    "s.bind(('127.0.0.1', a.local_port));"
    "s.listen(8);"
    "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0));"
    "select.select([s],[],[])"
)


# A fake supervisor that DOESN'T bind any socket — just sleeps. Lets the
# manager's wait_timeout_s path trigger.
_FAKE_SUPERVISOR_HANG = (
    "import argparse,signal,sys,time;"
    "p=argparse.ArgumentParser();"
    "p.add_argument('--jump');"
    "p.add_argument('--target');"
    "p.add_argument('--remote-port', type=int);"
    "p.add_argument('--local-port', type=int);"
    "p.add_argument('--backoff', type=float, default=0.1);"
    "p.add_argument('--ssh-opt', action='append', default=[]);"
    "p.parse_args();"
    "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0));"
    "time.sleep(60)"
)


def _fake_cmd_listen() -> list[str]:
    return [sys.executable, "-c", _FAKE_SUPERVISOR_LISTEN]


def _fake_cmd_hang() -> list[str]:
    return [sys.executable, "-c", _FAKE_SUPERVISOR_HANG]


def _spec(local_port: int = 0, wait_timeout_s: int = 5) -> TunnelSpec:
    return TunnelSpec(
        jump_host="fake-jump",
        target_host="fake-target",
        remote_port=4000,
        local_port=local_port,
        wait_timeout_s=wait_timeout_s,
        respawn_backoff_s=0,
    )


def _port_is_free(port: int) -> bool:
    """Best-effort: True iff no listener responds on 127.0.0.1:port."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return False
    except OSError:
        return True


# ---------------------------------------------------------------------------
# up() — happy path
# ---------------------------------------------------------------------------


def test_up_returns_bound_local_port_for_pinned_port(tmp_path):
    # Arrange — use a port the OS picks, then pin it so the manager
    # honours the operator's choice (rather than re-picking ephemeral).
    pinned = _pick_free_port()
    mgr = TunnelManager(
        spec=_spec(local_port=pinned),
        agent_name="t1",
        state_dir=tmp_path,
        supervisor_cmd=_fake_cmd_listen(),
    )
    try:
        # Act
        port = mgr.up()
        # Assert
        assert port == pinned
    finally:
        mgr.down()


def test_up_picks_ephemeral_port_when_spec_local_port_is_zero(tmp_path):
    # Arrange — spec.local_port=0 → manager allocates an ephemeral
    # port and the supervisor binds on it.
    mgr = TunnelManager(
        spec=_spec(local_port=0),
        agent_name="t-eph",
        state_dir=tmp_path,
        supervisor_cmd=_fake_cmd_listen(),
    )
    try:
        # Act
        port = mgr.up()
        # Assert
        assert 1024 < port < 65536
    finally:
        mgr.down()


def test_up_writes_pidfile_with_supervisor_pid(tmp_path):
    # Arrange
    mgr = TunnelManager(
        spec=_spec(),
        agent_name="t-pid",
        state_dir=tmp_path,
        supervisor_cmd=_fake_cmd_listen(),
    )
    try:
        # Act
        mgr.up()
        # Assert — pidfile exists and contains an int that is alive.
        assert mgr.pidfile.is_file()
        pid = int(mgr.pidfile.read_text())
        assert pid > 0
    finally:
        mgr.down()


def test_is_alive_returns_true_after_up(tmp_path):
    # Arrange
    mgr = TunnelManager(
        spec=_spec(),
        agent_name="t-alive",
        state_dir=tmp_path,
        supervisor_cmd=_fake_cmd_listen(),
    )
    try:
        # Act
        mgr.up()
        result = mgr.is_alive()
        # Assert
        assert result is True
    finally:
        mgr.down()


# ---------------------------------------------------------------------------
# down() — teardown
# ---------------------------------------------------------------------------


def test_down_removes_pidfile(tmp_path):
    # Arrange
    mgr = TunnelManager(
        spec=_spec(),
        agent_name="t-down",
        state_dir=tmp_path,
        supervisor_cmd=_fake_cmd_listen(),
    )
    mgr.up()
    # Act
    mgr.down()
    # Assert
    assert not mgr.pidfile.is_file()


def test_down_releases_local_port(tmp_path):
    # Arrange
    mgr = TunnelManager(
        spec=_spec(),
        agent_name="t-down-port",
        state_dir=tmp_path,
        supervisor_cmd=_fake_cmd_listen(),
    )
    port = mgr.up()
    # Act
    mgr.down()
    # Give the OS a moment to actually release the TCP listen socket.
    for _ in range(20):
        if _port_is_free(port):
            break
        time.sleep(0.05)
    # Assert
    assert _port_is_free(port)


def test_down_is_idempotent(tmp_path):
    # Arrange
    mgr = TunnelManager(
        spec=_spec(),
        agent_name="t-down-idem",
        state_dir=tmp_path,
        supervisor_cmd=_fake_cmd_listen(),
    )
    mgr.up()
    mgr.down()
    # Act — second down() must not raise.
    mgr.down()
    # Assert
    assert mgr.is_alive() is False


# ---------------------------------------------------------------------------
# Fail-loud — supervisor never binds in time
# ---------------------------------------------------------------------------


def test_up_raises_tunnel_up_error_when_supervisor_never_binds(tmp_path):
    # Arrange — supervisor that sleeps and never binds.
    mgr = TunnelManager(
        spec=_spec(wait_timeout_s=1),
        agent_name="t-timeout",
        state_dir=tmp_path,
        supervisor_cmd=_fake_cmd_hang(),
    )
    try:
        # Act
        ctx = pytest.raises(TunnelUpError, match="did not bind")
        # Assert
        with ctx:
            mgr.up()
    finally:
        # Manager already cleaned the pidfile, but cover the case where
        # the supervisor is still alive.
        mgr.down()


def test_up_error_message_includes_reproducer_recipe(tmp_path):
    # Arrange
    mgr = TunnelManager(
        spec=_spec(wait_timeout_s=1),
        agent_name="t-recipe",
        state_dir=tmp_path,
        supervisor_cmd=_fake_cmd_hang(),
    )
    message = ""
    try:
        # Act
        try:
            mgr.up()
        except TunnelUpError as exc:
            message = str(exc)
        # Assert
        assert "ssh -J fake-jump" in message
        assert "fake-target" in message
    finally:
        mgr.down()


# ---------------------------------------------------------------------------
# Fail-loud — spec validation
# ---------------------------------------------------------------------------


def test_up_raises_when_jump_host_is_empty(tmp_path):
    # Arrange
    spec = TunnelSpec(jump_host="", target_host="t", remote_port=4000)
    mgr = TunnelManager(
        spec=spec,
        agent_name="t-bad",
        state_dir=tmp_path,
        supervisor_cmd=_fake_cmd_listen(),
    )
    # Act
    ctx = pytest.raises(TunnelUpError, match="jump_host")
    # Assert
    with ctx:
        mgr.up()


def test_up_raises_when_remote_port_out_of_range(tmp_path):
    # Arrange
    spec = TunnelSpec(jump_host="j", target_host="t", remote_port=70000)
    mgr = TunnelManager(
        spec=spec,
        agent_name="t-bad",
        state_dir=tmp_path,
        supervisor_cmd=_fake_cmd_listen(),
    )
    # Act
    ctx = pytest.raises(TunnelUpError, match="remote_port")
    # Assert
    with ctx:
        mgr.up()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pick_free_port() -> int:
    """Get a free TCP port from the OS (binds + immediately releases)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

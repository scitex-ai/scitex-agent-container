"""Tests for :mod:`scitex_agent_container.runtimes.ssh_remote`.

Scope: pure argv-shape / string-rendering tests only. The previous
file (645 lines, ~30 tests) stacked ``patch("...subprocess.run")`` chains
to verify argv composition — those tests verified the mock, not ssh.

The remaining tests here exercise the pure-function surface
(``_ssh_target``, ``_ssh_base``, ``_scp_base``, ``_wrap_login_shell``).
Integration coverage for the actual ``_run``/``_preflight``/``_copy_config``
codepaths is out of scope: those need a real ssh server (loopback ssh
or container) and belong in ``tests/integration/`` once we wire it up.
"""

from __future__ import annotations

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.runtimes.ssh_remote import SSHRemote


def _make_config(
    name: str = "alpha",
    host: str = "remote.example.com",
    user: str = "u",
    hops: list[str] | None = None,
    key: str = "",
    port: int = 22,
) -> AgentConfig:
    c = AgentConfig(name=name)
    c.remote.host = host
    c.remote.user = user
    c.remote.hops = hops or []
    c.remote.key = key
    c.remote.port = port
    return c


# ---------------------------------------------------------------------------
# _ssh_target / _ssh_base / _scp_base — pure argv-shape tests.
# ---------------------------------------------------------------------------


def test_ssh_target_renders_user_at_host():
    # Arrange
    c = _make_config(user="alice", host="bastion")
    # Act
    target = SSHRemote._ssh_target(c)
    # Assert
    assert target == "alice@bastion"


def test_ssh_base_uses_ssh_as_argv0():
    # Arrange
    c = _make_config()
    # Act
    cmd = SSHRemote._ssh_base(c)
    # Assert
    assert cmd[0] == "ssh"


def test_ssh_base_sets_batch_mode_yes():
    # Arrange
    c = _make_config()
    # Act
    cmd = SSHRemote._ssh_base(c)
    # Assert
    assert "BatchMode=yes" in cmd


def test_ssh_base_sets_strict_host_key_accept_new():
    # Arrange
    c = _make_config()
    # Act
    cmd = SSHRemote._ssh_base(c)
    # Assert
    assert "StrictHostKeyChecking=accept-new" in cmd


def test_ssh_base_target_is_last_arg_when_no_hops():
    # Arrange
    c = _make_config()
    # Act
    cmd = SSHRemote._ssh_base(c)
    # Assert
    assert cmd[-1] == "u@remote.example.com"


def test_ssh_base_omits_key_flag_when_key_empty():
    # Arrange
    c = _make_config()
    # Act
    cmd = SSHRemote._ssh_base(c)
    # Assert
    assert "-i" not in cmd


def test_ssh_base_omits_port_flag_when_default():
    # Arrange
    c = _make_config()
    # Act
    cmd = SSHRemote._ssh_base(c)
    # Assert
    assert "-p" not in cmd


def test_ssh_base_includes_key_when_set():
    # Arrange
    c = _make_config(key="/path/to/key")
    # Act
    cmd = SSHRemote._ssh_base(c)
    # Assert
    assert cmd[cmd.index("-i") + 1] == "/path/to/key"


def test_ssh_base_includes_port_when_set():
    # Arrange
    c = _make_config(port=2222)
    # Act
    cmd = SSHRemote._ssh_base(c)
    # Assert
    assert cmd[cmd.index("-p") + 1] == "2222"


def test_scp_base_uses_scp_as_argv0():
    # Arrange
    c = _make_config(key="/k", port=2200)
    # Act
    cmd = SSHRemote._scp_base(c)
    # Assert
    assert cmd[0] == "scp"


def test_scp_base_uses_capital_p_for_port():
    # Arrange — scp uses -P (capital) for port, unlike ssh's -p.
    c = _make_config(port=2200)
    # Act
    cmd = SSHRemote._scp_base(c)
    # Assert
    assert cmd[cmd.index("-P") + 1] == "2200"


def test_scp_base_includes_key_when_set():
    # Arrange
    c = _make_config(key="/k")
    # Act
    cmd = SSHRemote._scp_base(c)
    # Assert
    assert "-i" in cmd


# ---------------------------------------------------------------------------
# _wrap_login_shell — pure string transformation.
# ---------------------------------------------------------------------------


def test_wrap_login_shell_starts_with_bash_dash_l():
    # Arrange
    cmd = "uptime"
    # Act
    wrapped = SSHRemote._wrap_login_shell(cmd)
    # Assert
    assert wrapped.startswith("bash -l -c '")


def test_wrap_login_shell_ends_with_closing_quote():
    # Arrange
    cmd = "uptime"
    # Act
    wrapped = SSHRemote._wrap_login_shell(cmd)
    # Assert
    assert wrapped.endswith("'")


def test_wrap_login_shell_plain_command_round_trips():
    # Arrange
    cmd = "uptime"
    # Act
    wrapped = SSHRemote._wrap_login_shell(cmd)
    # Assert
    assert wrapped == "bash -l -c 'uptime'"


def test_wrap_login_shell_escapes_single_quote_via_backslash_pattern():
    # Arrange — bash's standard single-quote escape: '\''
    cmd = "echo 'hi'"
    # Act
    wrapped = SSHRemote._wrap_login_shell(cmd)
    # Assert
    assert "'\\''" in wrapped

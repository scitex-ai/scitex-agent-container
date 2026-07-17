"""Tests for ``sac agents attach`` (cli_pkg/lifecycle/_attach.py).

``attach`` resolves an agent's tmux session and hands the terminal to
``tmux attach``. When no session exists it must fail loud (non-zero exit +
a red notice pointing at ``sac agents start``) rather than dropping into an
empty tmux. We exercise the no-session path (real ``tmux has-session`` on a
name that cannot be running, or tmux absent — both resolve to "no session").

STX-NM002: no mocks/monkeypatch. STX-TQ002/TQ007: AAA markers, one fact/test.
"""

from __future__ import annotations

from click.testing import CliRunner

from scitex_agent_container.cli_pkg.lifecycle._attach import attach


def test_attach_no_session_exits_nonzero() -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(attach, ["zzz-not-a-running-agent-zzz"])
    # Assert
    assert result.exit_code != 0


def test_session_for_falls_back_to_tui_prefix_for_unknown_name() -> None:
    # Arrange
    from scitex_agent_container.cli_pkg.lifecycle._attach import _session_for

    # Act
    _, session = _session_for("zzz-not-a-real-spec-zzz")
    # Assert
    assert session == "tui-zzz-not-a-real-spec-zzz"


# --------------------------------------------------------------------------
# Cross-host attach: a remote agent is attached over ssh, not the local tmux.
# --------------------------------------------------------------------------


def test_remote_attach_argv_forces_a_pty_with_ssh_dash_t() -> None:
    # Arrange
    from scitex_agent_container.cli_pkg.lifecycle._attach import _remote_attach_argv

    # Act
    argv = _remote_attach_argv("tui-spartan-dev", "zzz-unknown-peer-zzz")
    # Assert
    assert argv[:2] == ["ssh", "-t"]


def test_remote_attach_argv_runs_tmux_attach_on_the_named_session() -> None:
    # Arrange
    from scitex_agent_container.cli_pkg.lifecycle._attach import _remote_attach_argv

    # Act
    argv = _remote_attach_argv("tui-spartan-dev", "zzz-unknown-peer-zzz")
    # Assert — tmux attach runs DIRECTLY on the peer (no `bash -lc` wrapper).
    assert argv[-4:] == ["tmux", "attach", "-t", "tui-spartan-dev"]


def test_remote_attach_argv_uses_no_login_shell() -> None:
    """Regression (2026-07-16): attach must not wrap tmux in a login shell.

    A login shell (``bash -lc``) runs the peer's profile, whose interactive-tmux
    stanza fails with "open terminal failed: not a terminal"; tmux is on the
    peer's non-login PATH, so attach invokes it directly.
    """
    # Arrange
    from scitex_agent_container.cli_pkg.lifecycle._attach import _remote_attach_argv

    login_shell_tokens = {"bash", "-lc"}
    # Act
    argv = _remote_attach_argv("tui-spartan-dev", "zzz-unknown-peer-zzz")
    # Assert — no login shell: neither `bash` nor `-lc` in the built argv.
    assert not (login_shell_tokens & set(argv))


def test_remote_attach_argv_falls_back_to_peer_name_when_no_ssh_alias() -> None:
    # Arrange — a peer absent from host_config has no ssh alias.
    from scitex_agent_container.cli_pkg.lifecycle._attach import _remote_attach_argv

    # Act
    argv = _remote_attach_argv("tui-x", "zzz-unknown-peer-zzz")
    # Assert
    assert "zzz-unknown-peer-zzz" in argv


def test_classify_agent_host_unresolvable_spec_is_local() -> None:
    # Arrange
    from scitex_agent_container.cli_pkg.lifecycle._attach import _classify_agent_host

    # Act
    kind, _peer = _classify_agent_host("zzz-not-a-real-spec-zzz")
    # Assert
    assert kind == "local"

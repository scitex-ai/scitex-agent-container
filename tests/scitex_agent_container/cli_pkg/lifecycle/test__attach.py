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

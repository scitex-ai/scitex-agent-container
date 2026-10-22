"""Restart text-mode errors reach stderr even when stdout is not a TTY.

``sac agents restart`` piped through ssh exited 1 with EMPTY output while
``--json`` showed the abort: the levelled log record never reached a
non-TTY console. The text branch now also writes the error straight to
stderr, which this test pins by driving the real single-restart path
against a name that resolves nowhere.
"""

from __future__ import annotations

from scitex_agent_container.cli_pkg.lifecycle._restart import _restart_one


def test_restart_text_error_reaches_stderr_when_piped(capsys) -> None:
    # Arrange — a name on no registry row and no discovery chain.
    # Act
    _out, _ok = _restart_one("definitely-not-an-agent-xyz", as_json=False, fresh=False)
    # Assert
    assert "Error:" in capsys.readouterr().err

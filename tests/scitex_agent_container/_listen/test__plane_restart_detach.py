"""The detached form of a plane-restarting command.

Mirrors ``src/scitex_agent_container/_listen/_plane_restart_detach.py``
(PS-204 §2).

Asserts the CONSTRUCTED command rather than spawning one — the module exposes
``build_detached_command`` as a pure seam precisely so these properties are
checkable without forking a process that would restart the daemon running the
tests. That is the same save/restore-seam idiom ``_agent_restart`` uses for its
own detached bounce.

Each property here is load-bearing, and each maps to a way the inline form
failed on 2026-08-09 (``exit_code -15``, empty stdout, outcome unknowable):

  * ``setsid``      — the child must survive the daemon it is about to restart;
  * ``sleep <n>``   — the 202 must reach the caller BEFORE the daemon goes down;
  * append to a LOG — the command outlives whatever could have reported it, so
                      the log is the only post-hoc evidence it ran;
  * ``shlex`` quoting — argv is a list by contract and must not word-split into
                      shell text.
"""

from __future__ import annotations

from scitex_agent_container._listen._plane_restart_detach import (
    PLANE_RESTART_DELAY_S,
    build_detached_command,
    plane_restart_log_path,
)

_ARGV = ["systemctl", "--user", "restart", "sac-listen.service"]
_LOG = "/tmp/plane-restart-test.log"


def test_the_detached_command_starts_a_new_session() -> None:
    """Without setsid the child dies with the daemon it is restarting."""
    # Arrange
    argv = _ARGV
    # Act
    built = build_detached_command(argv, delay_s=3, log_path=_LOG)
    # Assert
    assert built[0] == "setsid"


def test_the_detached_command_runs_through_a_shell() -> None:
    # Arrange
    argv = _ARGV
    # Act
    built = build_detached_command(argv, delay_s=3, log_path=_LOG)
    # Assert
    assert built[1:3] == ["sh", "-c"]


def test_the_detached_command_defers_by_the_requested_delay() -> None:
    """The delay is what lets the 202 reach the caller before the bounce."""
    # Arrange
    argv = _ARGV
    # Act
    built = build_detached_command(argv, delay_s=7, log_path=_LOG)
    # Assert
    assert built[3].startswith("sleep 7;")


def test_the_detached_command_carries_the_original_argv() -> None:
    # Arrange
    argv = _ARGV
    # Act
    built = build_detached_command(argv, delay_s=3, log_path=_LOG)
    # Assert
    assert "systemctl --user restart sac-listen.service" in built[3]


def test_the_detached_command_appends_to_the_log() -> None:
    """Never /dev/null: this output is the only evidence the command ran."""
    # Arrange
    argv = _ARGV
    # Act
    built = build_detached_command(argv, delay_s=3, log_path=_LOG)
    # Assert
    assert f">> {_LOG} 2>&1" in built[3]


def test_the_detached_command_quotes_hostile_tokens() -> None:
    """argv is a LIST by contract; it must not become splittable shell text."""
    # Arrange
    argv = ["echo", "a b; rm -rf /"]
    # Act
    built = build_detached_command(argv, delay_s=3, log_path=_LOG)
    # Assert
    assert "'a b; rm -rf /'" in built[3]


def test_the_log_path_is_under_the_runtime_logs_dir() -> None:
    # Arrange
    # (no setup — the path is derived from HOME)
    # Act
    path = plane_restart_log_path()
    # Assert
    assert path.endswith(
        "/.scitex/agent-container/runtime/logs/host_exec-plane-restart.log"
    )


def test_the_default_delay_matches_the_agent_self_restart_delay() -> None:
    """Same hazard, same number — so the two cannot drift into different answers.

    ``_agent_restart._SELF_RESTART_DELAY_S`` is the original; this pins the
    host_exec copy to it so a future change to one is visible against the other.
    """
    # Arrange
    from scitex_agent_container._listen._agent_restart import _SELF_RESTART_DELAY_S

    # Act
    here = PLANE_RESTART_DELAY_S
    # Assert
    assert here == _SELF_RESTART_DELAY_S

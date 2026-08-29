"""Which host_exec commands would kill the daemon serving the request.

Mirrors ``src/scitex_agent_container/_listen/_plane_targeting_argv.py``
(PS-204 §2).

WHAT THIS GUARDS. ``POST /v1/host_exec`` is served BY the sac listen daemon, so
a command that restarts that daemon kills its own reporter. Measured 2026-08-09:

    systemctl --user restart sac-listen.service   ->  exit_code -15, stdout ""

The restart had SUCCEEDED; the caller could not be told. This predicate lets the
handler recognise that class up front and schedule it detached instead.

THE NEGATIVES MATTER AS MUCH AS THE POSITIVES, and are the reason this is a
predicate rather than a substring search:

  * ``systemctl status sac-listen`` is a READ — deferring it would answer 202
    to a question that wanted data;
  * a DIFFERENT unit that merely runs on the same host must not be caught;
  * ``sac listen start`` kills nobody, and on a DOWN daemon it is the recovery
    we must not make harder.

No mocks (STX-NM002): the function is pure, so these are plain calls.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._listen._plane_targeting_argv import targets_listen_plane

# ---------------------------------------------------------------------------
# Commands that WOULD end the daemon serving the request
# ---------------------------------------------------------------------------

_DISRUPTIVE = [
    ["systemctl", "--user", "restart", "sac-listen.service"],
    ["systemctl", "--user", "stop", "sac-listen.service"],
    ["systemctl", "restart", "sac-listen"],
    ["sac", "listen", "restart"],
    ["sac", "listen", "stop"],
    ["/home/ywatanabe/.scitex/agent-container/venv/bin/sac", "listen", "restart"],
    ["pkill", "-f", "sac listen"],
    ["killall", "sac-listen"],
]


@pytest.mark.parametrize("argv", _DISRUPTIVE, ids=lambda a: " ".join(a)[:48])
def test_a_plane_ending_command_is_detected(argv) -> None:
    # Arrange
    # (argv supplied by parametrize)
    # Act
    verdict = targets_listen_plane(argv)
    # Assert
    assert verdict.targets_plane is True


@pytest.mark.parametrize("argv", _DISRUPTIVE, ids=lambda a: " ".join(a)[:48])
def test_a_detected_command_carries_a_reason(argv) -> None:
    """A 202 must be able to say WHY it was scheduled instead of run."""
    # Arrange
    # (argv supplied by parametrize)
    # Act
    verdict = targets_listen_plane(argv)
    # Assert
    assert verdict.reason


# ---------------------------------------------------------------------------
# Commands that must still run INLINE — over-matching breaks ordinary work
# ---------------------------------------------------------------------------

_SAFE = [
    # A read of the same unit: the caller wants the output, not a 202.
    ["systemctl", "--user", "status", "sac-listen.service"],
    ["systemctl", "--user", "is-active", "sac-listen.service"],
    # A DIFFERENT unit on the same host.
    ["systemctl", "--user", "restart", "sac-creds-watch.service"],
    # start kills nobody, and on a down daemon it is the recovery path.
    ["sac", "listen", "start"],
    # Ordinary sac work.
    ["sac", "agents", "list"],
    ["sac", "agents", "restart", "some-agent"],
    # Ordinary shell work.
    ["echo", "hello"],
    ["git", "-C", "/repo", "status"],
]


@pytest.mark.parametrize("argv", _SAFE, ids=lambda a: " ".join(a)[:48])
def test_an_unrelated_command_runs_inline(argv) -> None:
    # Arrange
    # (argv supplied by parametrize)
    # Act
    verdict = targets_listen_plane(argv)
    # Assert
    assert verdict.targets_plane is False


# ---------------------------------------------------------------------------
# Degenerate input must not raise — this runs before every host_exec
# ---------------------------------------------------------------------------


def test_an_empty_argv_is_not_plane_targeting() -> None:
    # Arrange
    argv: list[str] = []
    # Act
    verdict = targets_listen_plane(argv)
    # Assert
    assert verdict.targets_plane is False


def test_a_non_string_argv_entry_does_not_raise() -> None:
    """host_exec validates argv first, but this must never be the thing that
    500s the endpoint."""
    # Arrange
    argv = [None, 123]  # type: ignore[list-item]
    # Act
    verdict = targets_listen_plane(argv)  # type: ignore[arg-type]
    # Assert
    assert verdict.targets_plane is False


def test_an_undetected_command_carries_no_reason() -> None:
    """The shape is fixed either way; only the reason is conditional."""
    # Arrange
    argv = ["echo", "hello"]
    # Act
    verdict = targets_listen_plane(argv)
    # Assert
    assert verdict.reason is None

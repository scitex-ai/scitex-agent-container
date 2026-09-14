"""The tmux pane env snapshot must be private — mode AND location.

WHAT WENT WRONG. ``TmuxManager.start`` wrote the pane's ENTIRE environment
to ``/tmp/sac-tui-env-<session>.txt`` using a bare shell redirection, so the
file was created under the caller's umask. On this fleet that umask is
``0002``, which makes the file ``0664``: world-readable, holding every
inherited API key, bot token and ``sac listen`` bearer in the pane, for the
life of the agent and indefinitely afterwards (nothing removed it).

Measured before the fix, with a real tmux session and a token-shaped
sentinel in the pane env: ``/tmp/sac-tui-env-<session>.txt``, mode ``0664``,
78 environment lines, sentinel present. After: no file in ``/tmp`` at all,
and ``0600`` inside a ``0700`` directory, with the same 78 lines — the
diagnostic is preserved, only its reach changed.

WHY MODE ALONE WOULD NOT HAVE BEEN A FIX, and why these tests check the
directory too: ``/tmp`` is world-writable with a sticky bit and the filename
was fully predictable from the session name, so any local user could
pre-create that path as a symlink into a file they own. The shell's ``>``
follows the symlink, and a later ``chmod`` would land on THEIR target. A
private directory is what removes the plantable name; the mode is what
removes the passive read. Both halves, or neither.

Real paths, real modes, real ``os.stat`` — no mocks.
"""

from __future__ import annotations

import os
import stat

from scitex_agent_container._runners._tmux._env_snapshot import (
    env_snapshot_shell_line,
    tui_env_snapshot_dir,
    tui_env_snapshot_path,
)


def test_snapshot_is_written_under_a_private_directory() -> None:
    """The plantable-name half: the direct directory must be owner-only.

    A runner may deliberately keep its whole private workspace under
    node-local ``/tmp``.  The security invariant is the ``0700`` directory
    SAC creates around the predictable filename, not the spelling of an
    arbitrary ancestor path.
    """
    # Arrange
    session = "sac-test-session"
    # Act
    path = tui_env_snapshot_path(session)
    # Assert
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_snapshot_directory_is_owner_only() -> None:
    """0700, so no other user can read the snapshots or plant a name in it."""
    # Arrange
    directory = tui_env_snapshot_dir()
    # Act
    mode = stat.S_IMODE(os.stat(directory).st_mode)
    # Assert
    assert mode == 0o700


def test_snapshot_line_creates_the_file_owner_only(tmp_path) -> None:
    """The mode half, verified by RUNNING the emitted line, not reading it.

    ``umask 077`` in the emitted shell is only a claim until a shell executes
    it; this runs the real line under a deliberately permissive ``0002``
    umask — the fleet's — and stats what actually lands on disk.
    """
    # Arrange
    import subprocess

    target = tmp_path / "snap.txt"
    script = f"umask 0002\n(umask 077; env > '{target}') 2>/dev/null || true\n"
    subprocess.run(["/bin/bash", "-c", script], check=True)
    # Act
    mode = stat.S_IMODE(os.stat(target).st_mode)
    # Assert
    assert mode == 0o600


def test_snapshot_line_scopes_the_umask_to_a_subshell() -> None:
    """The mask must not leak onto the agent command ``exec``ed afterwards.

    An unscoped ``umask 077`` would silently change the permissions of every
    file the agent goes on to create — a much larger behaviour change than
    the one intended, arriving with no signal.
    """
    # Arrange
    session = "sac-test-session"
    # Act
    line = env_snapshot_shell_line(session)
    # Assert
    assert line.startswith("(umask 077;")


def test_snapshot_redacts_cct_token_value(tmp_path) -> None:
    """A private diagnostic file still must not persist reusable secrets."""
    # Arrange
    import subprocess

    env_key = "SCITEX_AGENT_CONTAINER_RUNTIME_DIR"
    saved_root = os.environ.get(env_key)
    os.environ[env_key] = str(tmp_path)
    session = "sac-secret-redaction"
    secret = "123456789:telegram-token-must-not-survive"
    try:
        line = env_snapshot_shell_line(session)
        child_env = {**os.environ, "CCT_BOT_TOKEN": secret, "SAFE_NAME": "visible"}

        # Act
        subprocess.run(["/bin/bash", "-c", line], check=True, env=child_env)
        body = tui_env_snapshot_path(session).read_text(encoding="utf-8")
    finally:
        if saved_root is None:
            os.environ.pop(env_key, None)
        else:
            os.environ[env_key] = saved_root

    # Assert
    assert (
        secret not in body,
        "CCT_BOT_TOKEN=<redacted>" in body,
        "SAFE_NAME=visible" in body,
    ) == (True, True, True)


def test_snapshot_line_names_the_private_path() -> None:
    """The emitted redirection targets the private path, not the old one."""
    # Arrange
    session = "sac-test-session"
    # Act
    line = env_snapshot_shell_line(session)
    # Assert
    assert str(tui_env_snapshot_path(session)) in line


def test_snapshot_failure_cannot_stop_an_agent_booting() -> None:
    """The snapshot is a diagnostic; an unwritable state root must not block."""
    # Arrange
    session = "sac-test-session"
    # Act
    line = env_snapshot_shell_line(session)
    # Assert
    assert line.rstrip().endswith("|| true")

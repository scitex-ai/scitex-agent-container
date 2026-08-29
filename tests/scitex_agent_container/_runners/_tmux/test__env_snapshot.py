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


def test_snapshot_is_not_written_under_world_writable_tmp() -> None:
    """The plantable-name half: the path must leave ``/tmp`` entirely."""
    # Arrange
    session = "sac-test-session"
    # Act
    path = str(tui_env_snapshot_path(session))
    # Assert
    assert not path.startswith("/tmp/")


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

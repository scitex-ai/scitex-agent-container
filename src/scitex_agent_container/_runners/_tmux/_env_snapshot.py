"""The tmux pane's env-snapshot file — a whole environment, kept private.

WHAT IT IS
==========
``TmuxManager.start`` writes the pane's environment to a per-session file
immediately before ``exec``ing the agent command (lead a2a ``4303f855``,
2026-06-14). The ``/proc/<pid>/environ`` + ps-walk verify in
``TuiSessionRuntime`` could mis-attribute to another ``claude`` running under
a different tmux session; a known per-session file gives sac a structural
source of truth that needs no PID hunting.

WHY IT LIVES IN ITS OWN MODULE
==============================
Because WHERE the file lands and WHO may read it are the load-bearing parts,
and they were previously two incidental characters in an f-string inside a
120-line launcher method. The content is not a status line — it is ``env``,
the pane's ENTIRE environment: every inherited API key, every
``CCT_BOT_TOKEN_<SLOT>``, the ``sac listen`` bearer that authorises
``host_exec`` (RCE-equivalent). A dump of that set deserves a module that
says so.

THE TWO DEFECTS THIS MODULE CLOSES
==================================
The snapshot used to be written as::

    env > '/tmp/sac-tui-env-<session>.txt'

which is wrong twice over, and fixing only one half fixes nothing.

1. **Mode.** The redirection creates the file under the caller's umask. On
   this fleet that is ``0002``, so the file was created ``0664`` — readable
   by every local user, for the life of the agent and well past it (nothing
   ever removed it). This is precisely the exposure
   :mod:`...runtimes._apptainer_secret_env` exists to close for the launcher
   argv — ``/proc/<pid>/environ`` is owner-only exactly so that an
   environment stays private — re-opened one line later by the process that
   consumes that hardened argv.

2. **Location.** ``/tmp`` is world-writable (sticky), and the filename was
   fully predictable from the session name. Any local user could pre-create
   that path as a symlink pointing into a file they own; the shell's ``>``
   follows it, and the whole environment lands somewhere they can read. A
   ``chmod 0600`` after the fact would have been applied to THEIR target,
   which is why tightening the mode alone would have left the hole open.
   The directory has to be one no other user can create a name in.

So both halves are fixed together: the file lands inside a ``0700`` per-user
directory under sac's own state root (created here, by us, before tmux is
launched), and the redirection runs inside a ``umask 077`` subshell so the
file is ``0600`` from its first byte rather than tightened afterwards.

The subshell is scoped deliberately — ``(umask 077; env > …)`` — so the mask
applies to this one redirection and NOT to the agent command ``exec``ed two
lines later, which must keep the inherited umask it has always had.

NOTHING IN-REPO READS THIS FILE TODAY
=====================================
Verified across the tree: ``tmux.py`` is the only writer and there is no
reader. The file is kept anyway rather than deleted — it is a deliberate
operator-facing affordance from the directive above, and an operator or an
out-of-tree tool may well read it by path — but its being write-only is
exactly why the leak survived unnoticed, and exactly why "make it private"
is the right change rather than "make it correct".
"""

from __future__ import annotations

from pathlib import Path

#: Per-user directory holding the snapshots, under sac's own state root
#: (``$SCITEX_DIR/agent-container/runtime``) rather than world-writable
#: ``/tmp``. Created ``0700``: other users cannot read the snapshots AND
#: cannot plant a symlink under a name we are about to write.
TUI_ENV_SNAPSHOT_DIRNAME = "tui-env"

#: Mode for the snapshot directory. 0700, so the *names* stay private too.
TUI_ENV_SNAPSHOT_DIR_MODE = 0o700


def tui_env_snapshot_dir() -> Path:
    """The ``0700`` directory holding pane env snapshots. Created on demand.

    Anchored on :func:`..._state.state_paths.runtime_root` so the snapshots
    follow ``$SCITEX_DIR`` like every other piece of sac's user state — a
    hardcoded ``~`` here would split state across two roots on Spartan, the
    exact failure that module was written to end.

    The directory is created and tightened HERE, on the host, before tmux is
    launched — the guarantee the pane depends on has to exist before the pane
    does.
    """
    from ..._state.state_paths import runtime_root

    directory = runtime_root() / TUI_ENV_SNAPSHOT_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(TUI_ENV_SNAPSHOT_DIR_MODE)
    return directory


def tui_env_snapshot_path(session_name: str) -> Path:
    """Absolute path of ``session_name``'s env snapshot (dir created 0700)."""
    return tui_env_snapshot_dir() / f"sac-tui-env-{session_name}.txt"


def env_snapshot_shell_line(session_name: str) -> str:
    """The one shell line that writes the snapshot, ``0600``, never failing.

    ``umask 077`` runs inside a subshell so it governs this redirection only
    and never leaks onto the agent command ``exec``ed afterwards. Errors are
    swallowed (``2>/dev/null || true``) because the snapshot is a diagnostic
    affordance: an unwritable state root must not stop an agent from booting.
    """
    return (
        f"(umask 077; env > '{tui_env_snapshot_path(session_name)}') "
        "2>/dev/null || true\n"
    )


__all__ = [
    "TUI_ENV_SNAPSHOT_DIRNAME",
    "TUI_ENV_SNAPSHOT_DIR_MODE",
    "env_snapshot_shell_line",
    "tui_env_snapshot_dir",
    "tui_env_snapshot_path",
]

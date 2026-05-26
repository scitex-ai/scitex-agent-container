#!/usr/bin/env python3
# File: src/scitex_agent_container/_ssh.py

"""SSH ControlMaster multiplexing helper for sac.

The central concern this module addresses: when sac agents (running inside
an Apptainer SIF) issue parallel SSH connections to the same remote host
(e.g. Spartan), OpenSSH's default behaviour is to open a new TCP
connection per invocation. This runs into two problems:

1. **Read-only control-socket directory**: ~/.ssh/controlmaster/ (or
   wherever the default ControlPath lands) may live inside the SIF's
   read-only squashfs overlay, causing "control socket dir is read-only"
   errors when OpenSSH tries to create the multiplex socket.

2. **Spartan's MaxSessions / per-user concurrent-session limit**:
   Parallel SSH without multiplexing can exceed the remote SSHd's
   ``MaxSessions`` ceiling (commonly 10 per user on HPC login nodes).
   Beyond that limit, connections are silently rejected.

The fix is to reuse ONE multiplexed master connection per host via
OpenSSH's ``ControlMaster=auto``:

* ``ControlMaster=auto`` — act as a master if no socket exists,
  otherwise slave onto the existing master connection.
* ``ControlPersist=60s`` — keep the master connection alive for 60
  seconds after the last slave disconnects, so short-lived follow-up
  SSH invocations reuse it.
* ``ControlPath`` — pointed at a **writable** directory inside the
  container (``${TMPDIR:-/tmp}/.sac-ssh-cm/%C``), bypassing the
  read-only home / SIF overlay.

Usage::

    from scitex_agent_container._ssh import ensure_control_path_dir, ssh_control_opts

    # Once per process (or before the first ssh call):
    ensure_control_path_dir()

    # In every ssh argv:
    argv = ["ssh", *ssh_control_opts(), ...]
"""

from __future__ import annotations

import os
import pathlib

from ._env import getenv as _sac_env

# ── Control path directory ─────────────────────────────────────────────
# The token ``%C`` expands to a hash of ``%l%h%p%r`` (local-host,
# remote-host, remote-port, remote-user), producing one socket per
# unique ``(user, host, port)`` tuple.  This avoids collisions when
# sac connects to multiple remote hosts or to the same host with
# different users/ports.
#
# The default is `${TMPDIR:-/tmp}/.sac-ssh-cm` but can be overridden
# via the env var ``SAC_SSH_CONTROL_DIR`` (useful when /tmp itself is
# not writable inside a particular container / overlay setup).
_SAC_SSH_CONTROL_DIR_DEFAULT = os.path.join(
    os.environ.get("TMPDIR") or "/tmp", ".sac-ssh-cm"
)


def _control_path_dir() -> str:
    """Return the writable directory for SSH ControlMaster sockets.

    Honour ``$SAC_SSH_CONTROL_DIR`` when set (operator override).
    Fall back to ``${TMPDIR:-/tmp}/.sac-ssh-cm``.
    """
    return _sac_env("SSH_CONTROL_DIR") or _SAC_SSH_CONTROL_DIR_DEFAULT


def _control_path() -> str:
    """Return the full ControlPath string (with the ``%C`` expansion token).

    OpenSSH replaces ``%C`` with a hash of ``%l%h%p%r``, giving one
    socket per unique (local-host, remote-host, remote-port, remote-user)
    tuple.
    """
    return os.path.join(_control_path_dir(), "%C")


def ensure_control_path_dir() -> str:
    """Create the SSH ControlMaster socket directory (no-op if it exists).

    Returns the directory path so callers can log or inspect it.

    Safe to call multiple times — idempotent via ``exist_ok=True``.
    """
    d = _control_path_dir()
    pathlib.Path(d).mkdir(parents=True, exist_ok=True)
    return d


def ssh_control_opts() -> list[str]:
    """Return the ``-o`` flags for ControlMaster multiplexing.

    Returns a flat list suitable for splatting into an SSH argv::

        ["-o", "ControlMaster=auto",
         "-o", "ControlPersist=60s",
         "-o", "ControlPath=/tmp/.sac-ssh-cm/%C"]

    Callers should also invoke :func:`ensure_control_path_dir()` once
    before the first ``subprocess.run(ssh_argv, ...)``.
    """
    return [
        "-o",
        "ControlMaster=auto",
        "-o",
        "ControlPersist=60s",
        "-o",
        f"ControlPath={_control_path()}",
    ]


def sac_ssh_args(extra_opts: list[str] | None = None) -> list[str]:
    """Convenience: ``ensure_control_path_dir()`` + ``ssh_control_opts()``.

    Returns the control opts; callers splat this into their SSH argv.

    One-liner for callers that don't need to customise anything beyond
    the control-master options::

        from scitex_agent_container._ssh import sac_ssh_args
        argv = ["ssh", *sac_ssh_args(), ...]
    """
    ensure_control_path_dir()
    opts = ssh_control_opts()
    if extra_opts:
        opts += list(extra_opts)
    return opts


__all__ = [
    "ensure_control_path_dir",
    "ssh_control_opts",
    "sac_ssh_args",
]

"""SSH ControlMaster option rendering for sac→peer multiplexing.

Without multiplexing, every concurrent sac invocation (parallel
``sac host exec`` calls, dispatch fan-out, drift probes, OAuth
preflight, ssh+curl turn delivery) opens its own TCP/SSH session.
On hosts that cap concurrent sessions per user (Spartan's
``MaxSessions``, sshd ``MaxStartups``) the surplus connections are
dropped and the calling agent sees empty stdout / sporadic failures.
Inside an apptainer SIF the default OpenSSH ``ControlPath``
(``~/.ssh/sockets``) also lives on a read-only overlay, surfacing as
``control socket dir is read-only`` and silently disabling
multiplexing even when the user has it configured in ``~/.ssh/config``.

This module is the single source of truth for the rendered
``-o ControlMaster/Persist/Path`` triple. ``_state.host_config``,
``cli_pkg.priority_cmds``, ``cli_pkg._send_preflight``, and
``_network.peer`` all import :func:`ssh_control_options` (or its
shell-quoted twin) and prepend the result to their ssh argv.

The module is intentionally tiny + self-contained so the per-package
cli-startup budget (`_skills/.../21_cli-startup-budget.md`) isn't
affected — ``host_config`` re-exports the symbols, but the heavy
imports (``tempfile``, ``shlex``) are deferred to call time.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["ssh_control_options", "ssh_control_options_str"]


def ssh_control_options(*, control_dir: str | os.PathLike | None = None) -> list[str]:
    """Return ssh ControlMaster options for connection multiplexing.

    Strategy:

      * ``ControlMaster=auto`` — first ssh becomes master; siblings reuse.
      * ``ControlPersist=60s`` — master lingers 60s after the last client
        exits so a sub-second burst of sac calls shares one TCP handshake.
      * ``ControlPath=<dir>/%C`` — ``%C`` is the SHA256 hash of
        ``(user,host,port)``; collision-free across targets and short
        enough to stay inside the Unix-domain-socket 108-byte name limit
        even when ``<dir>`` is long.

    The control_dir is created on call (``mkdir -p`` semantics).
    Resolution order:

      1. ``control_dir`` argument — explicit pin (tests, callers that
         already have a writable scratch dir).
      2. ``$SAC_SSH_CONTROL_DIR`` env override.
      3. ``${TMPDIR:-/tmp}/.sac-ssh-cm`` via :func:`tempfile.gettempdir`
         (writable inside apptainer SIFs by default).

    Set ``SAC_SSH_CONTROL_MASTER=0`` (or ``no``/``false``/``off``) to opt
    out entirely; the function returns ``[]`` so each sac-emitted ssh
    argv falls back to one-connection-per-invocation. Useful when ssh
    is itself proxied through a wrapper that breaks ``ControlPath``
    (rare, but the escape hatch is required).

    Failure mode is fall-through: if the control_dir cannot be created
    (read-only mount, ENOSPC) the function returns ``[]`` rather than
    raising. The next sac ssh invocation just omits the ``-o`` flags
    and behaves exactly like pre-patch. This is intentional — connection
    multiplexing is an optimization; making it required would break
    callers that already work on hosts where the optimization can't
    apply.
    """
    opt_out = (os.environ.get("SAC_SSH_CONTROL_MASTER", "") or "").strip().lower()
    if opt_out in ("0", "no", "false", "off"):
        return []
    if control_dir is None:
        env_dir = os.environ.get("SAC_SSH_CONTROL_DIR")
        if env_dir:
            control_dir = env_dir
        else:
            # Lazy import — keeps `sac --help` startup cost off the
            # `_state` package import (see
            # `_skills/.../21_cli-startup-budget.md`).
            import tempfile

            control_dir = os.path.join(tempfile.gettempdir(), ".sac-ssh-cm")
    try:
        Path(control_dir).mkdir(parents=True, exist_ok=True)
    except OSError:
        # Read-only mount or ENOSPC — degrade to one-conn-per-call. The
        # caller's ssh argv ends up byte-identical to pre-patch.
        return []
    # %C => hashed (user,host,port); keeps the socket name short and
    # collision-free across simultaneously-active peers.
    path = os.path.join(str(control_dir), "%C")
    return [
        "-o",
        "ControlMaster=auto",
        "-o",
        "ControlPersist=60s",
        "-o",
        f"ControlPath={path}",
    ]


def ssh_control_options_str(*, control_dir: str | os.PathLike | None = None) -> str:
    """Return :func:`ssh_control_options` flags as a shell-quoted string.

    Convenience for agent prompts and shell wrappers that want to splat
    the options into a literal ``ssh`` command::

        ssh $(sac host ssh-opts) myhost cmd

    Returns the empty string when multiplexing is opted out so the splat
    has no effect.
    """
    import shlex

    opts = ssh_control_options(control_dir=control_dir)
    return " ".join(shlex.quote(o) for o in opts)

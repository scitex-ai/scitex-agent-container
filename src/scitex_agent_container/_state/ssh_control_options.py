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
imports (runtime path resolution, ``shlex``) are deferred to call time.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["ssh_control_options", "ssh_control_options_str"]


#: Usable bytes in a Linux ``sockaddr_un.sun_path`` (107 + NUL).
_SOCKADDR_UN_BUDGET = 107

#: Characters OpenSSH ``%C`` expands to (40 hex of user/host/port).
_HASH_CHARS = 40

#: Bytes reserved past ``<dir>/%C`` for the master listener socket.
#: Measured 2026-10-26 (``ssh -v`` on compute-03): OpenSSH appends a
#: random ``.<16 alnum>`` suffix when creating the master listener
#: (collision avoidance for concurrent masters), so the on-disk name
#: is ``<dir>/<40hex>.<16>`` — 17 bytes more than ``%C`` alone. The
#: previous budget counted only the hash and green-lit directories
#: whose sockets OpenSSH then refused, breaking every cross-host lane
#: (card sac-ssh-crosshost-unix-socket-path-20261026). 24 reserves the
#: observed 17 plus headroom for implementation variance.
_MASTER_SUFFIX_RESERVE = 24


def _fits_socket_budget(control_dir: str | os.PathLike) -> bool:
    """True iff ``<dir>/%C`` plus the master suffix fits ``sockaddr_un``.

    Pure (no mkdir): the byte budget is ``dir + 1 + 40 + 24 <= 107``.
    """
    used = len(os.fsencode(os.fspath(control_dir))) + 1 + _HASH_CHARS + _MASTER_SUFFIX_RESERVE
    return used <= _SOCKADDR_UN_BUDGET


def ssh_control_options(*, control_dir: str | os.PathLike | None = None) -> list[str]:
    """Return ssh ControlMaster options for connection multiplexing.

    Strategy:

      * ``ControlMaster=auto`` — first ssh becomes master; siblings reuse.
      * ``ControlPersist=60s`` — master lingers 60s after the last client
        exits so a sub-second burst of sac calls shares one TCP handshake.
      * ``ControlPath=<dir>/%C`` — ``%C`` is OpenSSH's 40-character hash of
        ``(user,host,port)``; collision-free across targets. The FINAL
        socket is longer: OpenSSH appends a random ``.<16>`` suffix to
        the master listener, so the budget counts hash + suffix
        (see ``_MASTER_SUFFIX_RESERVE``), not the hash alone.

    The control_dir is created on call (``mkdir -p`` semantics).
    Resolution order:

      1. ``control_dir`` argument — explicit pin (tests, callers that
         already have a writable scratch dir). Unguarded by design:
         an explicit pin remains the caller's responsibility.
      2. ``$SAC_SSH_CONTROL_DIR`` env override — an explicit operator
         pin, used verbatim (caller's responsibility, same as the
         argument pin; see below). An over-long pin fails loudly in
         ssh rather than silently losing multiplexing.
      3. ``<SAC runtime>/ssh-cm`` inside the canonical SAC state
         namespace — budget-gated. Unchanged from before, so hosts
         whose runtime dir already fits behave byte-identically.
      4. ``$XDG_RUNTIME_DIR/sac-ssh-cm`` — the per-user tmpfs, which is
         short by construction and writable where the apptainer overlay
         is not. Only used when the runtime dir does NOT fit.
      5. Otherwise ``[]`` (multiplexing off, one-conn-per-call).

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
            # Explicit operator pin: used verbatim, unguarded. An
            # over-long pin is the operator's stated choice and fails
            # loudly inside ssh; silently second-guessing it would
            # turn an explicit configuration into a mystery.
            # Only GENERATED candidates (runtime, XDG below) are
            # budget-gated — those the operator never chose.
            control_dir = env_dir
        else:
            # Lazy import — keeps `sac --help` startup cost off the
            # `_state` package import (see
            # `_skills/.../21_cli-startup-budget.md`).
            from .._runtime_paths import runtime_base_dir

            runtime_dir = runtime_base_dir() / "ssh-cm"
            if _fits_socket_budget(runtime_dir):
                control_dir = runtime_dir
            else:
                # Long runtime roots (HPC scratch, deep home anchors)
                # cannot host the master socket even though `%C`
                # alone would fit — fall to the per-user tmpfs, whose
                # short path is short by construction. Only a
                # fallback: hosts whose runtime dir fits keep the
                # exact pre-change behavior.
                xdg_base = os.environ.get("XDG_RUNTIME_DIR", "")
                xdg_dir = os.path.join(xdg_base, "sac-ssh-cm") if xdg_base else ""
                if xdg_dir and _fits_socket_budget(xdg_dir):
                    control_dir = xdg_dir
    if control_dir is None:
        # Every generated candidate blew the socket budget (or the env
        # pin was unusable): multiplexing off, one-conn-per-call, rather
        # than handing ssh a socket path it must refuse.
        return []
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

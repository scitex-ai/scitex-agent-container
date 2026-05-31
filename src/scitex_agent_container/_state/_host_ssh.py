"""ssh-argv rendering for ``sac fleet``/``sac host`` dispatch (extracted).

Lifted out of :mod:`scitex_agent_container._state.host_config` so the
config schema file stays close to the per-file line cap headroom needed
to land the ADR-0013 Phase 1 ``LeadConfig`` block. No behaviour change —
this is a pure extraction. The public import path
``from scitex_agent_container._state.host_config import build_ssh_argv``
is preserved by a one-line re-export in ``host_config.py``.

Lives next to :mod:`._host_interfaces` (the matching interface-inventory
extraction) and :mod:`.ssh_control_options` (the ControlMaster option
renderer this function calls).
"""

from __future__ import annotations

from .host_config import PeerSpec
from .ssh_control_options import ssh_control_options


def build_ssh_argv(
    peer_name: str,
    command: list[str],
    peers: dict[str, PeerSpec],
    *,
    ssh_binary: str = "ssh",
    extra_opts: list[str] | None = None,
) -> list[str]:
    """Render the ssh argv that runs ``command`` on ``peer_name``.

    Multi-hop is handled via OpenSSH's ``-J`` (ProxyJump) flag, which
    chains intermediate hosts without sac needing its own ssh tunnel
    code. ``via: [mba, spartan]`` becomes ``-J <mba.ssh>,<spartan.ssh>``.

    Conservative defaults pick: ``-o BatchMode=yes`` (no interactive
    password / known-hosts prompts), ``-o ConnectTimeout=10``
    (probe-friendly), and ``-o ServerAliveInterval=15`` (keepalive
    so a wedged middle-hop is detectable).

    When the peer carries an ``env_preamble`` (e.g. Spartan, where
    ``apptainer`` is only on $PATH after two ``module load`` calls),
    the dispatched command is wrapped in ``bash -c '<preamble> &&
    <quoted-cmd>'`` so the preamble runs before the real command. The
    wrapper deliberately uses ``-c`` (NOT ``-lc``) to skip the full
    login profile — sourcing ``.bashrc`` on some HPC compute nodes
    (verified 2026-05-17 on spartan-bm152) triggers cgroup/PAM
    process kills during user-init scripts (e.g. ``gh config`` from
    ``~/.bash.d/``), aborting the login before the real command runs.
    The cost: ``module`` is no longer auto-defined; the peer's
    ``env_preamble`` must source the Lmod init script explicitly
    (e.g. ``source /usr/share/lmod/lmod/init/bash`` as its first
    line on Spartan).  The wrapper collapses into a single argv
    element so ssh's post-host word-join preserves the inner quoting.
    Peers without an ``env_preamble`` keep the byte-identical
    pre-existing argv shape — mba / nas invocations are unchanged.

    Returns the argv list ready for ``subprocess.run``. Raises
    ``KeyError`` when ``peer_name`` isn't in ``peers``.
    """
    import shlex

    peer = peers[peer_name]
    argv: list[str] = [ssh_binary]
    if peer.via:
        chain = peer.jump_chain(peers)
        if chain:
            argv += ["-J", ",".join(chain)]
    argv += [
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=15",
        # TOFU policy for first-touch peers: accept the host key on
        # initial connect (record it in known_hosts), but reject on any
        # subsequent mismatch. Without this, the very first dispatch to
        # a freshly-registered peer hangs on the interactive
        # ``Are you sure you want to continue connecting`` prompt, which
        # under BatchMode=yes degrades to an immediate close — the
        # operator sees a bare non-zero exit with no actionable error.
        # ``accept-new`` is the safer ``no``: it does NOT silently
        # accept changed keys, so a MITM still surfaces.
        "-o",
        "StrictHostKeyChecking=accept-new",
    ]
    # Connection multiplexing — must come before extra_opts so caller
    # overrides win. See :func:`ssh_control_options` for the rationale
    # (Spartan MaxSessions cap + apptainer overlay ControlPath issue).
    argv += ssh_control_options()
    if extra_opts:
        argv += list(extra_opts)
    argv += [peer.ssh, "--"]
    preamble = peer.joined_preamble()
    if preamble:
        # OpenSSH joins every token after the host with spaces and feeds
        # the result to the remote user's login shell, which re-parses
        # it. To get the remote shell to launch `bash -c 'CMD'` we
        # therefore must collapse the wrapping into a single argv
        # element whose contents are pre-quoted at *both* layers: the
        # inner CMD (preamble && user-cmd) is shlex-quoted so the
        # `bash -c` parse sees one token, and the resulting string is
        # appended whole so ssh's word-join preserves it. Note the
        # *lack* of `-l` — bypassing the login profile avoids HPC
        # compute-node bashrc kills (see docstring). The preamble is
        # responsible for sourcing Lmod (or any other env layer) on
        # its own.
        inner = f"{preamble} && {shlex.join(list(command))}"
        argv.append(f"bash -c {shlex.quote(inner)}")
    else:
        argv += list(command)
    return argv


__all__ = ["build_ssh_argv"]

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

import posixpath
from typing import TYPE_CHECKING

from .host_registry import remote_state_root
from .ssh_control_options import ssh_control_options

if TYPE_CHECKING:
    # TYPE_CHECKING-only: ``host_config`` re-exports this module's
    # ``build_ssh_argv`` at ITS bottom, so a runtime import here forms a
    # cycle that explodes whenever ``_host_ssh`` is imported FIRST
    # ("partially initialized module"). PeerSpec is used purely in
    # annotations, and ``from __future__ import annotations`` makes those
    # lazy strings — so the runtime import buys nothing and costs a cycle.
    from .host_config import PeerSpec


def _is_sac_invocation(command: list[str]) -> bool:
    """True when ``command`` runs the ``sac`` CLI on the remote.

    Matches a bare ``sac`` and any absolute/relative path to it (Spartan's
    peer entries invoke ``/home/ywatanabe/.env-3.11/bin/sac``). Anything
    else — a user's ``sac host exec <peer> -- <arbitrary cmd>`` — is NOT a
    sac invocation and must be dispatched byte-identically to before.
    """
    if not command:
        return False
    return posixpath.basename(command[0]) == "sac"


def _resolve_peer_root(peer_name: str, peers: dict[str, PeerSpec]) -> str | None:
    """Registry root for ``peer_name``, inheriting through its ``via:`` chain.

    Direct hit first (``spartan`` IS a registry row). Otherwise fall back
    to the peer's ProxyJump chain, walking it from the hop CLOSEST to the
    target outwards — that hop is the cluster the node belongs to, and a
    compute node necessarily shares its login node's filesystem.

    This is the sac/registry split working as intended. The registry owns
    "where is host X and what is its scitex root" and rightly does NOT
    enumerate ephemeral HPC compute nodes. sac owns the ``via:`` chains.
    Composing them here is what lets ``spartan-bm043`` — a glob peer that
    is not (and should never be) a registry row — inherit Spartan's
    registry-declared root instead of silently falling back to the node's
    ``~/.scitex``, which is the symlink that started this whole mess.

    Without this, the two-tier HPC targets — the ones agents ACTUALLY run
    on — would be the only hosts left unpinned.
    """
    root = remote_state_root(peer_name)
    if root:
        return root
    spec = peers.get(peer_name)
    if spec is None:
        return None
    for hop in reversed(spec.via):
        root = remote_state_root(hop)
        if root:
            return root
    return None


def _scitex_dir_prefix(
    peer_name: str, command: list[str], peers: dict[str, PeerSpec]
) -> list[str]:
    """``["SCITEX_DIR=<root>"]`` when the registry pins ``peer_name``.

    THE choke point for the host-registry SSOT (see
    :mod:`.host_registry`). Every remote *sac* invocation — start, stop,
    restart, tail, delete, accounts, fleet/registry sync, ``sac --on
    <peer>`` — funnels through :func:`build_ssh_argv`, so pinning the
    state root HERE covers all of them at once, including call sites added
    later. Injecting at each of the ~10 call sites instead would leave the
    next one to be written silently unpinned; this codebase has been bitten
    by exactly that (a primitive fixed at 1 of 6 call sites).

    A SHELL ASSIGNMENT PREFIX, deliberately — not ``env VAR=val``. ssh
    joins everything after the host and hands it to the remote user's
    shell, so a bare ``SCITEX_DIR=… sac …`` is parsed by that shell as an
    assignment prefix and ``sac`` is then resolved by the SAME shell PATH
    lookup that the un-prefixed command used. ``env`` would instead resolve
    ``sac`` itself, from whatever PATH the ``env`` binary inherited, which
    is a DIFFERENT (and weaker) lookup — it cannot see a shell function or
    alias, and it changes the failure mode on any host where sac lives on a
    profile-managed PATH. Measured on Spartan 2026-07-14: both forms fail
    identically when sac is off PATH, but only the assignment form is a
    strict superset of the pre-change behaviour — it works wherever the
    bare command worked, and never anywhere it didn't. Verified the
    assignment actually propagates (``SCITEX_DIR=x printenv SCITEX_DIR``
    → ``x``).

    Returns ``[]`` — a byte-identical argv — whenever the registry has no
    ABSOLUTE root for the peer (unregistered host, home-relative
    ``~/.scitex`` root, or no registry at all). ``~`` is never expanded
    here: on a REMOTE host it means the *peer's* home, and expanding it
    locally would silently yield the lead's.
    """
    if not _is_sac_invocation(command):
        return []
    root = _resolve_peer_root(peer_name, peers)
    if not root:
        return []
    return [f"SCITEX_DIR={root}"]


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
    # Registry-pin the remote state root for sac invocations (SSOT
    # adoption). Prepended to the COMMAND, not to the ssh options, so it
    # rides through both branches below: the bare-argv path and the
    # ``bash -c '<preamble> && …'`` wrapper (which shlex-joins the list,
    # keeping ``env SCITEX_DIR=… sac …`` intact after the module loads).
    command = [*_scitex_dir_prefix(peer_name, command, peers), *command]
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
        # SPACE-JOIN, NOT shlex.join — measured 2026-08-17, rc=127 on every
        # preamble peer, which took scitex-hub down and unstartable by ANY
        # path (agent_start from a container and agent_spawn via the host
        # broker both die here).
        #
        # THE BUG WAS TWO INDIVIDUALLY-CORRECT QUOTINGS COMPOSING WRONGLY.
        # ssh word-joins everything after the host and hands it to the remote
        # shell, so a caller that wants ONE remote token must pre-quote it —
        # which `_spec_handoff.ssh_runner` correctly does, passing a single
        # element like `sh -c 'echo REACHED'`. `shlex.join` then quoted that
        # already-quoted element AGAIN, so the remote bash saw one word and
        # looked for a FILE by that name:
        #
        #   bash: line 1: sh -c 'echo REACHED': command not found
        #
        # Each layer's docstring correctly explained why IT quoted; neither
        # knew the other did too.
        #
        # The join must match the NON-PREAMBLE branch below (`argv +=
        # list(command)`, which ssh then space-joins), because that branch is
        # the contract every caller was already written against. Making the
        # two agree is the fix; quoting here and not there is what made a
        # peer's behaviour depend on whether it happened to carry a preamble.
        inner = f"{preamble} && {' '.join(command)}"
        argv.append(f"bash -c {shlex.quote(inner)}")
    else:
        argv += list(command)
    return argv


__all__ = ["build_ssh_argv", "resolve_peer_scitex_root"]

# Public alias — ``_dispatch`` needs the same via-chain-aware root for the
# rsync DESTINATION (rsync is not an ssh-argv path, so it cannot ride the
# ``build_ssh_argv`` choke point and must resolve the root itself).
resolve_peer_scitex_root = _resolve_peer_root

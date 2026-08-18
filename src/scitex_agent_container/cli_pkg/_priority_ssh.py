"""ssh reach for the singleton reconciler — argv construction and execution.

Extracted from :mod:`.priority_cmds`, which had grown two separable jobs:
DECIDING whether an agent should yield (rank a spec's host chain) and REACHING
the host to find out (turn a peer NAME into an ssh argv and run it). This is
the reaching half. It is also the half the 2026-08-17 defect lived in, so the
peer-table knowledge and the incident record now sit in one place.

``priority_cmds`` re-exports every name here under its old private spelling,
so no import or call site moved.
"""

from __future__ import annotations

import subprocess

# Lightweight SSH reachability options — no TTY, short timeout, no host-key
# prompt. See :func:`peer_ssh_argv` for which of these survive for a REGISTERED
# peer and which the fleet-standard builder overrides.
_SSH_PROBE_OPTS = [
    "-o",
    "ConnectTimeout=3",
    "-o",
    "BatchMode=yes",
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "LogLevel=ERROR",
]

# Wall-clock ceiling on a single probe, enforced by subprocess rather than by
# ssh. It is the BINDING bound for a registered peer (see peer_ssh_argv), so it
# is named rather than inlined: the two timeouts interact, and a future edit to
# either one should be able to see the other.
_PROBE_WALL_TIMEOUT = 5

_SSH_START_TIMEOUT = 30  # seconds to wait for remote sac agent start


def peer_ssh_argv(host: str, command: list[str], opts: list[str]) -> list[str]:
    """Render ssh argv for ``host``, TRANSLATING it through the peer table.

    WHY THIS EXISTS — measured 2026-08-17.

    ``spec.host`` names a PEER. sac's peer table maps that name to an ssh
    ALIAS, and the two are not interchangeable:

        peer name        ywata-note-win
        ssh alias        ywata-note-win-net  -> bastion-win.scitex.ai (works)
        the BARE name                        -> 192.168.11.101 (dead LAN IP)

    This module used to hand the raw ``spec.host`` straight to ``ssh``. So for
    every agent pinned to that peer — 54 of them — it reported "preferred host
    unreachable" while sac's OWN dispatch reached the same machine fine,
    because dispatch translates and this did not. Two consumers, one string,
    opposite outcomes.

    That is not merely a bad report. :func:`ssh_start_agent` fed the same
    untranslated name to a REMOTE START, so a yield decision made on a false
    "unreachable" would have tried to start an agent by ssh-ing to an address
    that goes nowhere.

    A host that is NOT a registered peer is passed through unchanged: it may
    still resolve via ``~/.ssh/config``, and that path works today. Translating
    only what the table knows about fixes the defect without breaking it.

    ONE DELIBERATE TIMEOUT CHANGE, WRITTEN DOWN BECAUSE IT IS INVISIBLE IN THE
    DIFF. ``build_ssh_argv`` emits its own ``-o`` defaults BEFORE ``extra_opts``
    and ssh honours the FIRST occurrence of an option, so for a REGISTERED peer
    the ``ConnectTimeout=3`` above no longer applies — the fleet-standard
    ``ConnectTimeout=10`` wins. Measured on the rendered argv, which carries
    both values in that order; not inferred from the builder's source.

    The effect is bounded and does NOT reach 10s: :func:`probe_ssh` caps the
    call at ``_PROBE_WALL_TIMEOUT`` (5s) and treats the expiry as unreachable,
    which is the same verdict ssh's own 3s timeout produced. So an unreachable
    registered peer costs 5s instead of 3s and still answers False — a latency
    change on fan-out, not a behaviour change. Accepted rather than worked
    around: the builder's values are the fleet's considered ones (its docstring
    calls 10 "probe-friendly") and this module's were an outlier predating
    them. ``test_priority_cmds_peer_translation`` pins both the winning value
    and the wall-clock cap so neither drifts silently.

    The pass-through path is unaffected — there the caller's opts are the only
    ones present, and ``ConnectTimeout=3`` still binds.
    """
    # stx-allow: fallback (reason: an unreadable/absent peer table must degrade
    # to today's behaviour — pass the name through — rather than break a probe
    # that works on hosts resolved by ~/.ssh/config alone)
    try:
        from .._state._host_ssh import build_ssh_argv
        from .._state._peer_resolve import peers_with_registry
        from .._state.host_config import load as _load_host_config

        peers = peers_with_registry(_load_host_config().peers)
    except Exception:
        peers = {}

    if host in peers:
        return build_ssh_argv(host, command, peers, extra_opts=opts)
    return ["ssh", *opts, host, *command]


def probe_ssh(host: str) -> bool:
    """Return True if ``host`` is reachable via SSH (``hostname`` exits 0)."""
    try:
        result = subprocess.run(
            peer_ssh_argv(host, ["hostname"], _SSH_PROBE_OPTS),
            capture_output=True,
            timeout=_PROBE_WALL_TIMEOUT,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def ssh_start_agent(host: str, agent_name: str) -> bool:
    """SSH to *host* and run ``sac agent start <agent_name>`` in the background.

    Returns True if the remote command exited 0.

    ``host`` is a PEER NAME and is translated through the peer table by
    :func:`peer_ssh_argv` — see there for why the untranslated form sent a
    remote START to an address that goes nowhere.
    """
    # Singleton-reconcile fans out across many hosts in parallel; share
    # one ssh master per peer so MaxSessions / MaxStartups stay happy.
    # For a REGISTERED peer the builder emits these itself, so they land
    # twice with identical values — ssh takes the first, which is the same
    # string either way. Kept for the pass-through path, where they are the
    # only copy.
    from .._state.host_config import ssh_control_options

    opts = [
        "-o",
        "ConnectTimeout=10",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "LogLevel=ERROR",
        *ssh_control_options(),
    ]
    cmd = peer_ssh_argv(host, ["sac", "agent", "start", agent_name], opts)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=_SSH_START_TIMEOUT,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


__all__ = [
    "_PROBE_WALL_TIMEOUT",
    "_SSH_PROBE_OPTS",
    "_SSH_START_TIMEOUT",
    "peer_ssh_argv",
    "probe_ssh",
    "ssh_start_agent",
]

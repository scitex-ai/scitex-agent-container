"""Name the remedy when a remote ``sac`` invocation dies with rc=127.

WHY THIS EXISTS — measured 2026-08-06, and the cost was paid twice: once in
debugging, once in a host mutation that should never have happened.

``sac agents restart <agent>`` for an agent placed on ``scitex-01`` failed::

    Error: Remote `sac agents restart compute-pilot-01` failed on 'scitex-01'
    (rc=127)
    stderr: bash: line 1: sac: command not found

sac WAS installed on that host. ssh runs a NON-LOGIN shell, so the venv's
``bin`` was not on ``PATH``. Every cross-host lifecycle op against the new
compute nodes failed this way, which blocked the agent migration those hosts
were provisioned for.

sac already had the fix. :func:`~._host_ssh.build_ssh_argv` — the documented
single choke point every remote-sac invocation funnels through — wraps the
command in ``bash -c '<preamble> && <cmd>'`` whenever the peer declares an
``env_preamble``, and Spartan has used exactly that for exactly this purpose
for as long as it has been a peer::

    env_preamble: 'export PATH="$HOME/.env-3.11/bin:$PATH"'

The new peers simply had no preamble. But NOTHING IN THE ERROR SAID SO. It
reported a shell's "command not found" and left the reader to rediscover the
mechanism, so the first fix reached for was a sudo symlink into
``/usr/local/bin`` on two hosts — a mutation that bypassed a facility that
already existed. That is the failure this module addresses: not the missing
PATH, which is per-peer config and correctly so, but the fact that the
failure did not name its own remedy, so it invites a worse fix every time a
host is added.

Hint discipline (see the fail-loud-hint rule): the suggestion must CLEAR the
condition. The emitted line is the exact YAML that made the real dispatch
succeed, verified by removing the symlinks first, confirming bare ``sac`` was
"command not found" again, and then restarting cross-host with only the
preamble in place.

DELIBERATELY NARROW. The hint is added only when all three hold — rc is 127,
the stderr looks like a shell not-found, and the peer has NO preamble. A peer
that already declares one has a different problem (a wrong path, a broken
profile), and pointing it at ``env_preamble`` would be a confident wrong
answer.
"""

from __future__ import annotations

# POSIX shells exit 127 for "command not found". Named rather than inlined
# so the condition reads as the contract it is.
_RC_COMMAND_NOT_FOUND = 127

_NOT_FOUND_MARKERS = ("command not found", "No such file or directory")


def _peer_has_preamble(peer_name: str, peers: object) -> bool:
    """True when ``peer_name`` already declares an ``env_preamble``.

    Defensive on purpose: this runs on an error path that is already
    reporting a failure, so a lookup problem here must never replace the
    caller's real message with a traceback. Any doubt returns True, which
    SUPPRESSES the hint — a missing hint is a small loss, a misleading one
    sends the reader somewhere wrong.
    """
    try:
        peer = peers[peer_name]  # type: ignore[index]
    except Exception:  # stx-allow: fallback (reason: hint enrichment on an error path must never mask the caller's real failure)
        return True
    try:
        return bool(peer.joined_preamble())
    except Exception:  # stx-allow: fallback (reason: same — a peer shape without a preamble accessor must not crash the error report)
        return True


def remote_sac_not_found_hint(
    peer_name: str,
    returncode: int,
    stderr: str,
    peers: object,
) -> str:
    """Return a paste-ready ``env_preamble`` hint, or ``""`` when not applicable.

    Callers append the result to the ``RuntimeError`` they were already
    raising, so an empty string leaves the existing message byte-identical.
    """
    if returncode != _RC_COMMAND_NOT_FOUND:
        return ""
    text = stderr or ""
    if not any(marker in text for marker in _NOT_FOUND_MARKERS):
        return ""
    if _peer_has_preamble(peer_name, peers):
        return ""
    return (
        f"\n\nHINT: rc=127 is the shell's 'command not found', and peer "
        f"{peer_name!r} declares no `env_preamble`. ssh runs a NON-LOGIN "
        "shell, so a sac installed in a venv is not on PATH there even "
        "though it IS installed. Declare the peer's PATH once, in "
        "~/.scitex/agent-container/config.yaml:\n"
        f"    {peer_name}:\n"
        f"      ssh: {peer_name}\n"
        "      env_preamble: 'export PATH=\"$HOME/.env-sac/bin:$PATH\"'\n"
        "(adjust the venv path to where `sac` actually lives on that host — "
        "`ssh <peer> 'command -v sac || ls -d ~/.env*/bin/sac'`). "
        "build_ssh_argv then wraps every remote sac call in "
        "`bash -c '<preamble> && ...'`. This is the same mechanism the "
        "spartan peer already uses; do NOT symlink sac into /usr/local/bin "
        "to work around it, which mutates the host to paper over config."
    )


__all__ = ["remote_sac_not_found_hint"]

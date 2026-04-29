"""SSH hop-chain rendering for spec.remote list format.

One helper renders ``spec.remote`` (str or list[str]) into ``ssh -J``
args.  Both SSHRemote and SlurmTenantRuntime call it; neither
re-implements hop logic.
"""

from __future__ import annotations

from ..host_identity import is_local_host


def skip_local_hops(hops: list[str]) -> list[str]:
    """Remove leading hops that refer to the current host.

    Walks ``hops`` from the front; each hop is tested with
    ``host_identity.is_local_host()``.  The first non-local hop and
    everything after it is returned unchanged.

    Examples (running on spartan)::

        skip_local_hops(['spartan', 'spartan-bm149']) -> ['spartan-bm149']
        skip_local_hops(['spartan-bm149'])            -> ['spartan-bm149']
        skip_local_hops(['spartan'])                  -> []
    """
    remaining = list(hops)
    while remaining and is_local_host(remaining[0]):
        remaining.pop(0)
    return remaining


def render_ssh_chain(hops: list[str]) -> list[str]:
    """Return the ssh command *suffix* (no leading 'ssh') for ``hops``.

    - Empty list  → ``[]``  (caller runs locally; no ssh)
    - Single hop  → ``['<host>']``
    - Two+ hops   → ``['-J', 'h1,...,hN-1', 'hN']``
    """
    if not hops:
        return []
    if len(hops) == 1:
        return [hops[0]]
    jumps = ",".join(hops[:-1])
    return ["-J", jumps, hops[-1]]


def build_ssh_command(
    hops: list[str],
    remote_cmd: str,
    ssh_opts: list[str] | None = None,
) -> list[str] | None:
    """Build a full subprocess command list for running ``remote_cmd`` via the chain.

    Returns ``None`` when ``hops`` is empty (caller should execute locally).
    Otherwise returns ``['ssh', *opts, <chain-suffix>, remote_cmd]``.
    """
    chain = render_ssh_chain(hops)
    if not chain:
        return None
    cmd = ["ssh"]
    if ssh_opts:
        cmd.extend(ssh_opts)
    cmd.extend(chain)
    cmd.append(remote_cmd)
    return cmd

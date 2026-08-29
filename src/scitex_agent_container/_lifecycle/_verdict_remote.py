"""Cross-host REMOTE liveness — is the agent's tmux session up ON ITS PEER?

Split out of :mod:`._verdict_resolve` (512-line cap) along a real seam: this is
the ONE resolver that reaches ANOTHER HOST. An agent whose ``spec.host`` is a
remote peer keeps its TUI session on that peer, so the LOCAL tmux is blind to it
and the ordinary :func:`._verdict_resolve.process_signal` would read a false
"no local session" DEAD. This module ssh-probes the peer's own tmux bookkeeping
instead (the control-plane cross-host liveness added by #708-#710).

Re-exported from :mod:`._verdict_resolve` so ``from ._verdict_resolve import
remote_process_signal`` and the resolver's own use of it keep working unchanged.
Same doctrine as every resolver here: a probe that could not run is
:data:`._verdict.UNKNOWN`, never :data:`._verdict.DEAD` — a wedged ssh, a broken
ProxyJump, a bare login PATH or an auth failure must not slander a live remote
agent into a destroyable corpse.
"""

from __future__ import annotations

from typing import Any, Callable

from ._verdict import (
    ALIVE,
    DEAD,
    INSTRUMENT_HOST_TMUX,
    SOURCE_PROCESS,
    UNKNOWN,
    Signal,
)
from .._runners._tmux._target import exact_target
from ._verdict_tmux import session_name_for_config

__all__ = [
    "remote_process_signal",
]


def _remote_peer_for_config(config: Any) -> str | None:
    """Return the peer name if the agent's ``spec.host`` is a remote peer.

    Uses the SAME chain resolver ``sac agents start`` and ``sac agents attach``
    route through, so "remote" means one thing across the whole control plane
    — including for a FALLBACK CHAIN, whose head is no longer assumed: a chain
    led by a typo now resolves to the next usable entry rather than reporting
    "not remote" and probing the wrong (local) tmux.

    Deliberately passes NO reachability oracle. This runs once per agent per
    listing, and an ssh probe here would double the cost of every
    ``sac agents list`` to answer a question the caller is about to answer
    anyway by ssh-probing the peer's tmux. Without an oracle the walk is pure
    and its answer is the historical head-of-chain, minus the typo bug.

    Best-effort — any resolution failure returns ``None`` and the caller falls
    back to the ordinary (local) process probe. Imports are LAZY to avoid a
    ``cli_pkg`` -> ``_lifecycle`` import cycle.
    """
    try:
        from ..cli_pkg.lifecycle._common import _local_host_names
        from ..cli_pkg.lifecycle._host_chain import resolve_host_chain
        from ..config._host import resolve_hostname

        host = config.hosts_spec.host
        if not host:
            return None
        from .._state.host_config import load as _load_host_config

        current = resolve_hostname()
        peers = _load_host_config().peers
        route = resolve_host_chain(
            host, current, peers, local_names=_local_host_names(current)
        )
        return route.peer
    except Exception:  # stx-allow: fallback (unresolvable host -> treat as local; the local probe still runs)
        return None


def _run_ssh_rc(argv: list[str]) -> int:
    """Run ``argv`` and return its exit code (default remote-probe runner).

    A generous 10s timeout keeps a wedged peer from hanging a listing; on
    timeout or a missing ssh binary we return 255 (ssh's own connection-failed
    code) so :func:`remote_process_signal` maps it to UNKNOWN. Injection seam —
    tests pass their own runner and never shell out.
    """
    import subprocess

    try:
        return subprocess.run(argv, capture_output=True, timeout=10).returncode
    except (
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ):  # stx-allow: fallback (ssh could not run -> 255 -> UNKNOWN upstream)
        return 255


def remote_process_signal(
    config: Any,
    peer: str,
    *,
    run_ssh: Callable[[list[str]], int] | None = None,
) -> Signal:
    """Is the agent's tmux session up ON ITS REMOTE HOST (ssh probe)?

    An agent whose ``spec.host`` is a remote peer has its TUI session on that
    peer, not here — the LOCAL tmux is blind to it, so the ordinary
    :func:`._verdict_resolve.process_signal` reads DEAD ("no local session") and
    the agent vanishes from cross-host ``sac agents list``. This ssh-probes the
    peer's own tmux bookkeeping for the agent's session instead.

    The argv is rendered by :func:`.._state.host_config.build_ssh_argv` — sac's
    ONE canonical dispatch primitive — never a hand-rolled ssh line. That is
    what makes a two-tier HPC target work: ``build_ssh_argv`` applies the peer's
    ``via:`` ProxyJump chain (``-J``), glob-matches ``spartan-bm043`` onto a
    ``spartan*`` pattern peer, and wraps the command in that peer's
    ``env_preamble`` via ``bash -c`` (NOT ``-lc``: a login shell trips the
    profile's interactive-tmux and false-DEADs a live agent — #709). Hand-rolling
    it skipped the hop, so every ``spartan-bmNNN`` agent probed UNKNOWN and
    rendered stale. The ``preamble && tmux has-session`` chain keeps rc 1 for
    "no session" (a real DEAD); a preamble/hop failure is some other non-0/1 rc,
    read below as UNKNOWN.

    Verdicts (:data:`INSTRUMENT_HOST_TMUX` — the peer's tmux, a real
    independent bookkeeper, exactly as in the local case):

    * ssh connected + session present (rc 0) -> :data:`ALIVE`.
    * ssh connected + no session (rc 1)      -> :data:`DEAD` (positive remote
      absence, from the peer's own ``tmux has-session``).
    * ssh could not run / any other rc, OR the peer is not resolvable (no
      config / unknown peer / glob-miss, so ``build_ssh_argv`` raises)
      -> :data:`UNKNOWN`. A probe that could not run is NEVER DEAD (this
      module's core doctrine); a wedged ssh, a broken ProxyJump, a bare login
      PATH, or an auth failure must not slander a live remote agent into a
      destroyable corpse.

    ``run_ssh`` is the injection seam (a real callable returning the exit
    code); production uses :func:`_run_ssh_rc`.
    """
    session = session_name_for_config(config)

    # Render the probe argv through sac's canonical dispatch primitive so the
    # peer's ProxyJump chain + env_preamble apply (see docstring). A load/build
    # failure (no config, unknown / glob-missed peer) is "I could not look" ->
    # UNKNOWN, never a false DEAD. ``-n`` so ssh never eats our stdin.
    try:
        from .._state.host_config import build_ssh_argv
        from .._state.host_config import load as _load_host_config

        peers = _load_host_config().peers
        argv = build_ssh_argv(
            # EXACT-match target: a bare -t prefix-matches on the peer's tmux,
            # so a sibling session would vouch this agent ALIVE (2026-08-14).
            peer,
            ["tmux", "has-session", "-t", exact_target(session)],
            peers,
            extra_opts=["-n"],
        )
    except (
        Exception
    ) as exc:  # stx-allow: fallback (unresolvable peer -> UNKNOWN, never DEAD)
        return Signal(
            SOURCE_PROCESS,
            UNKNOWN,
            f"could not build an ssh probe for peer {peer!r} "
            f"({type(exc).__name__}) — unresolvable peer is UNKNOWN, never DEAD",
            INSTRUMENT_HOST_TMUX,
        )

    try:
        rc = (run_ssh or _run_ssh_rc)(argv)
    except (
        Exception
    ) as exc:  # stx-allow: fallback (probe shell-out blew up -> UNKNOWN, never DEAD)
        return Signal(
            SOURCE_PROCESS,
            UNKNOWN,
            f"could not ssh remote host {peer!r} to probe tmux "
            f"({type(exc).__name__}) — remote liveness UNKNOWN, never DEAD",
            INSTRUMENT_HOST_TMUX,
        )
    if rc == 0:
        return Signal(
            SOURCE_PROCESS,
            ALIVE,
            f"tmux session {session!r} is up on remote host {peer!r} (ssh probe)",
            INSTRUMENT_HOST_TMUX,
        )
    if rc == 1:
        return Signal(
            SOURCE_PROCESS,
            DEAD,
            f"ssh to {peer!r} succeeded and its tmux has NO session {session!r} "
            f"— positive remote absence, from the peer's own bookkeeping",
            INSTRUMENT_HOST_TMUX,
        )
    return Signal(
        SOURCE_PROCESS,
        UNKNOWN,
        f"ssh probe of {peer!r} returned rc={rc} (not 0/1: wedged ssh, a "
        f"failed ProxyJump/env_preamble, bare PATH, or auth) — remote liveness "
        f"UNKNOWN, never DEAD",
        INSTRUMENT_HOST_TMUX,
    )

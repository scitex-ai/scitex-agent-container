"""``sac agents attach <name>`` — attach your terminal to a running agent.

The TUI runtime holds each agent's interactive ``claude`` process in a
DETACHED tmux session named by :func:`session_name_for` (the bare agent name
for sessions minted after the cutover, ``tui-<name>`` for older ones). This
verb resolves the LIVE session and hands your terminal to
``tmux attach`` (via ``execvp``) so you can watch and drive the live agent;
detach again with the usual tmux keys (``Ctrl-b d``).

**Cross-host (control-plane) attach.** The operator drives the whole fleet
from the master, regardless of where an agent actually runs. So when the
agent's ``spec.host`` classifies as a *remote* peer (the same
``classify_dispatch_host`` resolution ``sac agents start`` dispatches
through), attach does not look at the LOCAL tmux — it ``ssh -t``'s to that
peer and runs ``tmux attach`` there. The remote agent's session lives on the
peer's default tmux server and persists across attach/detach, so the
operator gets the live remote agent from their local terminal; ``Ctrl-b d``
detaches and drops back to the peer shell, ``Ctrl-d`` returns to the master.

Fail-loud: if the agent has no running session, print a red notice with the
next step (``sac agents start <name>``) and exit non-zero — never silently
drop into an empty tmux.
"""

from __future__ import annotations

import os
import subprocess

import click

from ..._runners._tmux._target import exact_target
from .._helpers._completion import agent_name_complete
from .._helpers._console import system_msg


def _session_for(name: str) -> tuple[str, str]:
    """Resolve ``(agent_name, tmux_session)`` for ``name``.

    Prefers the canonical bare session from the loaded spec; falls back to
    the raw name when the spec can't be resolved, so attach still works for
    an agent whose spec moved. Callers resolve the LIVE session among the
    bare + legacy candidates (see :func:`_live_local_session`) — this stays
    probe-free so the remote path (whose session lives on a peer, not here)
    can reuse it without probing the wrong server.
    """
    try:
        from ...config import load_config
        from ...config._resolve import resolve_with_prefix
        from ...runtimes.tui_session import session_name_for

        config = load_config(resolve_with_prefix(name))
        return config.name, session_name_for(config)
    except Exception:  # stx-allow: fallback (best-effort spec resolution)
        return name, name


def _candidates_for(session: str) -> tuple[str, ...]:
    """Bare session first, legacy ``tui-`` second (deduped)."""
    from ...runtimes._tui_session_names import session_and_aliases

    return session_and_aliases(session)


def _has_session_local(session: str) -> bool:
    """True iff the LOCAL tmux has ``session`` (exact match)."""
    try:
        return (
            subprocess.run(
                ["tmux", "has-session", "-t", exact_target(session)],
                capture_output=True,
                text=True,
            ).returncode
            == 0
        )
    except FileNotFoundError:  # stx-allow: fallback (tmux absent → no session)
        return False


def _live_local_session(session: str) -> str | None:
    """The live local session among the candidates, or ``None``.

    Bare canonical first, legacy second — so attach lands in a pre-cutover
    ``tui-<name>`` session instead of failing loud over a missing bare one.
    """
    for candidate in _candidates_for(session):
        if _has_session_local(candidate):
            return candidate
    return None


def _live_remote_session(session: str, peer: str) -> str | None:
    """The live session on ``peer``: the session name, ``""``, or ``None``.

    Returns the live candidate's name (bare preferred, legacy second); ``""``
    when EVERY candidate cleanly missed (a proven miss → the start hint);
    ``None`` when ssh itself failed (attach then surfaces ssh's own failure
    rather than our start hint).
    """
    for candidate in _candidates_for(session):
        try:
            probe = subprocess.run(
                ["ssh", ssh_target_for(peer), "tmux", "has-session",
                 "-t", exact_target(candidate)],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except Exception:  # stx-allow: fallback (ssh broken → let attach surface it)
            return None
        if probe.returncode == 0:
            return candidate
    return ""


def _classify_agent_host(name: str) -> tuple[str, str | None]:
    """Return ``(kind, peer)`` for the agent's ``spec.host``.

    ``kind`` is ``"local"`` / ``"remote"`` / ``"unknown"`` per
    :func:`._host_chain.resolve_host_chain` — the SAME reducer
    ``sac agents start`` uses, so attach and start agree on where an agent
    lives. That agreement is the point of routing through the shared
    resolver rather than re-deriving a target here: once start began walking
    a FALLBACK CHAIN, a head-only reduction in attach would send the operator
    to a different machine than the one the agent was launched on.

    Deliberately passes NO reachability oracle. Attach is interactive and
    already about to open an ssh session that will report its own failure far
    better than a pre-probe would; without an oracle the walk is pure and
    answers the historical head-of-chain, minus the unroutable-head bug.

    Best-effort: an unresolvable spec (moved / deleted) degrades to
    ``("local", None)`` so attach still tries the local tmux, preserving the
    historic single-host behaviour.
    """
    try:
        from ..._state.host_config import load as _load_host_config
        from ...config import load_config
        from ...config._host import resolve_hostname
        from ...config._resolve import resolve_with_prefix
        from ._common import _local_host_names
        from ._host_chain import UNROUTABLE, resolve_host_chain

        config = load_config(resolve_with_prefix(name))
        bound = config.hosts_spec.host
        if not bound:
            return ("local", None)
        current = resolve_hostname()
        peers = _load_host_config().peers
        route = resolve_host_chain(
            bound, current, peers, local_names=_local_host_names(current)
        )
        if route.kind == UNROUTABLE:
            return ("unknown", None)
        return (route.kind, route.peer)
    except Exception:  # stx-allow: fallback (unresolved spec/config → local attach)
        return ("local", None)


def ssh_target_for(peer: str) -> str:
    """Resolve the ssh target for a peer (alias when set, else peer name)."""
    try:
        from ..._state.host_config import load as _load_host_config

        spec = _load_host_config().peers.get(peer)
        if spec is not None and getattr(spec, "ssh", None):
            return spec.ssh
    except (
        Exception
    ):  # stx-allow: fallback (peer without an ssh alias → use the peer name)
        pass
    return peer


def _remote_attach_argv(session: str, peer: str) -> list[str]:
    """Build the ``ssh -t <target> tmux attach -t <session>`` argv for a remote agent.

    The ssh alias comes from ``host_config.peers[peer].ssh`` (see
    :func:`ssh_target_for`, the same source ``sac agents start`` dispatches
    through); it falls back to the peer name when no explicit ``ssh:`` is set. ``tmux`` is invoked DIRECTLY on
    the peer's non-login PATH: a login shell (``bash -lc``) triggers the
    profile's interactive-tmux and fails with "open terminal failed: not a
    terminal", so attach must not wrap the command. ``-t`` still forces a PTY so
    tmux gets a terminal.
    """
    ssh_target = ssh_target_for(peer)
    return [
        "ssh",
        "-t",
        "-o",
        "StrictHostKeyChecking=accept-new",
        ssh_target,
        "tmux",
        "attach",
        "-t",
        # EXACT session target: a bare name prefix-matches on the peer, which
        # would hand the operator a SIBLING agent's TUI when the named session
        # is gone (incident 2026-08-14).
        exact_target(session),
    ]


@click.command()
@click.argument("name", shell_complete=agent_name_complete)
def attach(name: str) -> None:
    """Attach your terminal to a running agent's TUI (tmux) session.

    Works whether the agent runs locally or on a remote peer host — a
    remote agent is attached over ssh, so you drive the whole fleet from
    the master.

    \b
    Example:
      $ sac agents attach neurovista     # local; Ctrl-b d to detach
      $ sac agents attach spartan-dev    # remote (Spartan) over ssh
    """
    agent, session = _session_for(name)
    kind, peer = _classify_agent_host(name)

    if kind == "remote" and peer is not None:
        # Control-plane attach: the session lives on the peer, not here.
        # Pre-check over ssh so a missing session fails HERE with the start
        # hint — not as tmux's bare "can't find session" after the PTY closes
        # (operator report 2026-10-10: attach to a never-started agent showed
        # only tmux's line plus a dropped connection, no next step). BOTH
        # candidates are probed: a pre-cutover ``tui-<name>`` session on the
        # peer is just as attachable as a bare one.
        live = _live_remote_session(session, peer)
        if live == "":
            system_msg(
                f"no running session for agent '{agent}' "
                f"on '{peer}'. "
                f"Start it first: `sac agents start {agent}`.",
                style="red",
            )
            raise SystemExit(1)
        if live:
            session = live
        system_msg(
            f"'{agent}' runs on remote host '{peer}'; attaching over ssh. "
            f"Ctrl-b d detaches (back to {peer}'s shell); Ctrl-d returns here.",
            style="cyan",
        )
        # Hand the terminal to ssh (replaces this process). A missing remote
        # session surfaces as tmux's own "can't find session" over the PTY —
        # loud, not silent.
        os.execvp("ssh", _remote_attach_argv(session, peer))
        return  # unreachable (execvp replaced the process)

    try:
        exists = _live_local_session(session) is not None
    except FileNotFoundError:  # stx-allow: fallback (tmux absent → no session)
        exists = False
    if not exists:
        # Operator order 2026-10-16: a miss should suggest, not just
        # refuse. Offer substring matches over known agent names so a
        # short or misremembered name ("lead") finds its candidates
        # instead of dying at a dead end.
        hint = f"Start it first: `sac agents start {agent}`."
        try:
            from ...config._resolve import enumerate_agent_names

            matches = sorted(
                n for n in enumerate_agent_names() if agent.lower() in n.lower()
            )[:8]
            if matches and agent not in matches:
                hint += " Did you mean: " + ", ".join(
                    f"`sac agents start {m}`" for m in matches
                ) + "?"
        except Exception:  # stx-allow: fallback (reason: hint enrichment must never break the fail-loud refusal)
            pass
        system_msg(
            f"no running session '{session}' for agent '{agent}'. {hint}",
            style="red",
        )
        raise SystemExit(1)

    # Hand the terminal to tmux (replaces this process; detach with Ctrl-b d).
    # Attach the LIVE candidate — a pre-cutover legacy session, not the bare
    # name that merely names what a fresh start WOULD mint.
    live = _live_local_session(session)
    os.execvp("tmux", ["tmux", "attach", "-t", exact_target(live or session)])

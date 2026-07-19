"""Route resolution — PROVE the target exists before claiming anything reached it.

``tmux send-keys -t tui-dotfiles`` against a session that does not exist prints
"can't find pane" and the sender, who never looks at stderr, carries on. That is
not a hypothetical: an operator coordinated with an agent for HOURS on that basis,
reporting every message as delivered into a session that had never existed.

So resolution happens FIRST, its own signal, before a single keystroke is sent.

THE EMPTY ENUMERATION RULE
--------------------------
The subtle half. "``tui-<agent>`` is not in the session list" is only evidence of
absence if the list was CAPABLE of showing it. A process in a different mount
namespace — every containerized agent on this fleet — sees the host's tmux as an
EMPTY session list, and an empty list read as "the agent is dead" is a measured
false DEAD (2026-07-14). So:

* enumeration failed        → ``None``   (we are blind)
* enumeration returned []   → ``None``   (we are probably blind; nothing on a
                                          live host has zero sessions, and this
                                          is exactly what a namespace boundary
                                          looks like from the inside)
* enumeration showed others → ``False``  (a capable instrument looked and this
                                          session was not there)
* enumeration showed ours   → ``True``

Only the third case may convict. This is the "absence of evidence is not evidence
of absence" rule with the one condition under which absence IS evidence spelled
out: the instrument must have demonstrated, in the same reading, that it can see
the kind of thing whose absence it is reporting.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable, Optional

__all__ = [
    "STRATEGY_SDK",
    "STRATEGY_TUI",
    "TUI_SESSION_PREFIX",
    "Route",
    "list_tmux_sessions",
    "read_agent_session_id",
    "resolve_route",
]

#: The TUI runtime names its sessions ``tui-<agent>`` on the DEFAULT tmux server
#: (``runtimes/tui_session.session_name_for``) — NOT the ``-L sac`` server that
#: ``_runners/_tmux/pane_capture`` targets. Using that other server here would
#: read a DIFFERENT server's emptiness as this fleet's death.
TUI_SESSION_PREFIX = "tui-"

#: Resume the agent's recorded Claude session via the existing ``sac agents
#: send`` machinery. Covers SDK-runner agents.
STRATEGY_SDK = "sdk"

#: Verified tmux paste + idle-gated submit. The ONLY path that reaches a TUI
#: agent, which is the population that matters: of the fleet's agents, only a
#: handful have a recorded ``session_id`` at all.
STRATEGY_TUI = "tui"


@dataclass(frozen=True)
class Route:
    """How (and whether) we can reach one agent."""

    #: ``STRATEGY_SDK`` / ``STRATEGY_TUI`` / ``""`` when nothing resolved.
    strategy: str

    #: The tmux session name for the TUI strategy; ``""`` otherwise.
    session: str

    #: True / False / None — the ``is_route_resolved`` signal, per the rule in
    #: the module docstring.
    resolved: bool | None

    #: WHY, in the operator's terms. Carried rather than reconstructed, because
    #: "not found" and "could not look" need different sentences and the caller
    #: cannot regenerate which one applied.
    reason: str

    #: The raw enumeration, kept verbatim for the journal.
    sessions_raw: str = ""


def list_tmux_sessions() -> Optional[list[str]]:
    """Every session on the DEFAULT tmux server; ``None`` on any error.

    ``None`` rather than ``[]`` on failure — the whole point of this module is
    that those two are different findings, and collapsing them at the sensor
    makes the distinction unrecoverable upstream no matter how careful the
    caller is.
    """
    # stx-allow: fallback (reason: tmux may be absent or have no server; None is
    # the honest "could not read" sentinel and is handled distinctly upstream)
    try:
        out = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:  # stx-allow: fallback (reason: catch-all — see comment above)
        return None
    if out.returncode != 0:
        return None
    return sorted(s for s in out.stdout.split() if s)


def read_agent_session_id(agent: str) -> Optional[str]:
    """The agent's recorded Claude session id, or ``None``.

    Its presence is what selects the SDK strategy. Measured on the live host:
    only a handful of agents have this file at all — the TUI population does
    not, which is precisely why ``sac agents send`` alone could never reach the
    agents that matter, and why this module needs a second strategy rather than
    a better error message on the first.
    """
    # stx-allow: fallback (reason: a missing/unreadable state dir simply means no
    # SDK route; None selects the TUI strategy rather than raising)
    try:
        from .._runners._session_state import read_session_id, state_dir_for

        return read_session_id(state_dir_for(agent)) or None
    except Exception:  # stx-allow: fallback (reason: catch-all — see comment above)
        return None


def resolve_route(
    agent: str,
    *,
    strategy: str = "auto",
    list_sessions_fn: Callable[[], Optional[list[str]]] = list_tmux_sessions,
    session_id_fn: Callable[[str], Optional[str]] = read_agent_session_id,
) -> Route:
    """Pick a strategy and PROVE the target exists on it. Pure but for the seams.

    ``strategy`` is ``"auto"`` (prefer SDK when a session id exists, else TUI),
    or one of :data:`STRATEGY_SDK` / :data:`STRATEGY_TUI` to force one. Forcing
    matters for diagnosis: an operator who suspects the SDK route is stale needs
    to be able to say "use tmux and tell me what you see".
    """
    if strategy in ("auto", STRATEGY_SDK):
        session_id = session_id_fn(agent)
        if session_id:
            return Route(
                strategy=STRATEGY_SDK,
                session="",
                resolved=True,
                reason=(
                    f"a recorded session_id was found for {agent!r}, so the "
                    f"existing verified send path applies"
                ),
            )
        if strategy == STRATEGY_SDK:
            return Route(
                strategy=STRATEGY_SDK,
                session="",
                resolved=False,
                reason=(
                    f"--strategy sdk was forced but {agent!r} has NO recorded "
                    f"session_id. This is the normal state for a TUI agent: the "
                    f"SDK path cannot reach it at all. Use --strategy tui (or "
                    f"auto) instead of reading this as the agent being down"
                ),
            )

    sessions = list_sessions_fn()
    wanted = f"{TUI_SESSION_PREFIX}{agent}"
    if sessions is None:
        return Route(
            strategy=STRATEGY_TUI,
            session=wanted,
            resolved=None,
            reason=(
                "the tmux session list could NOT be read (tmux absent, no "
                "server, or the call failed). Nothing is known about whether "
                f"{wanted!r} exists — this is blindness, not absence"
            ),
        )
    if not sessions:
        return Route(
            strategy=STRATEGY_TUI,
            session=wanted,
            resolved=None,
            reason=(
                "the tmux session list came back EMPTY. An empty list is what a "
                "process in a different mount namespace sees of the host's tmux, "
                "so it is a statement about this observer, not about the fleet. "
                f"Refusing to conclude that {wanted!r} is absent from a reading "
                "that could not have shown it present"
            ),
            sessions_raw="",
        )
    raw = "\n".join(sessions)
    if wanted in sessions:
        return Route(
            strategy=STRATEGY_TUI,
            session=wanted,
            resolved=True,
            reason=(
                f"{wanted!r} is present in a session list that enumerated "
                f"{len(sessions)} session(s)"
            ),
            sessions_raw=raw,
        )
    return Route(
        strategy=STRATEGY_TUI,
        session=wanted,
        resolved=False,
        reason=(
            f"{wanted!r} is NOT among the {len(sessions)} session(s) this tmux "
            f"server reports. The enumeration DID show other sessions, so it was "
            f"capable of showing this one — the absence is real, and every "
            f"message sent to this target would have gone nowhere while "
            f"`tmux send-keys` reported success"
        ),
        sessions_raw=raw,
    )


# EOF

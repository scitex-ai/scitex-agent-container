"""The backgroundable command that ACTUALLY reaches a given agent.

WHY THIS IS NOT A CONSTANT — measured, and it cost seven briefs.

The non-blocking dispatch path does not deliver the prompt; it hands the caller
a command to run in a background shell. That command used to be hard-coded::

    f"sac agents send {name} {prompt}"

and ``sac agents send`` posts to the agent's a2a sidecar. For an SDK-runner
agent that is exactly right. For a TUI agent it is not: the TUI population has
no recorded ``session_id``, its input is a tmux pane, and the turn is accepted
and then never processed. The caller gets a success value for a prompt nobody
will ever read.

So the caller was required to know whether the target runs TUI or SDK in order
to pick a verb — a leaked implementation detail, and one that fails SILENTLY
in the direction of "delivered". Operator, 2026-08-16:
``tui と sdk で不必要に変わるってキモいんだけど`` — it is gross that behaviour
changes between tui and sdk.

The repo already knows the answer. :func:`.._delivery.resolve_route` picks the
strategy from the agent's recorded state, and ``sac agents deliver`` is the
verified path for the TUI half. This module is the one line of glue that was
missing: ask the router, then name the verb that works.

ONE SOURCE, TWO RENDERINGS. The dispatch payload carries both a shell string
(``track_command``) and an argv list (``track_command_argv``). Those were built
independently from two separate literals, so a change to either could silently
diverge from the other. Here the string is derived from the argv, which makes
divergence unrepresentable rather than merely unlikely.
"""

from __future__ import annotations

import shlex

__all__ = [
    "build_track_command",
    "build_track_command_argv",
    "resolve_track_strategy",
    "track_verb_for",
]


def track_verb_for(strategy: str | None) -> str:
    """``"deliver"`` for the TUI strategy, ``"send"`` otherwise.

    ``send`` stays the default for everything that is not positively identified
    as TUI. That asymmetry is deliberate: ``send`` is the long-standing verb and
    the one every existing caller expects, so an unresolvable route keeps
    today's behaviour rather than silently switching transports on a guess.
    """
    # Imported lazily: ``_delivery._sdk_strategy`` imports ``cli_pkg._send``,
    # so a module-level import here would tie the two packages together at
    # import time for the sake of one constant.
    from .._delivery import STRATEGY_TUI

    return "deliver" if strategy == STRATEGY_TUI else "send"


def resolve_track_strategy(agent: str) -> str | None:
    """The strategy that reaches ``agent``, or ``None`` when it cannot be read.

    CHEAP BY CONSTRUCTION, which is what lets the non-blocking path use it.
    :func:`.._delivery.resolve_route` returns immediately for an agent with a
    recorded ``session_id`` — no tmux call at all — and the tmux enumeration it
    falls back to is a single fast command. The expensive half of delivery (the
    paste, the arrival wait, the idle-gated submit) is NOT run here. That split
    is the whole reason routing can happen without blocking the caller's turn,
    which the MCP surface requires.

    ONLY A PROVEN ROUTE SWITCHES THE VERB — ``route.resolved is True``, not
    merely ``route.strategy``. The distinction is load-bearing and I got it
    wrong first: :func:`.._delivery.resolve_route` falls through to the TUI
    strategy whenever no session id is READABLE, so a blind tmux enumeration
    (every containerized caller sees an empty session list) and a transient
    failure to read an SDK agent's session-id file both yield
    ``strategy="tui"`` with ``resolved`` of ``None``/``False``. Keying on the
    strategy alone would therefore route a perfectly healthy SDK agent onto
    ``deliver`` because one file read hiccuped — trading a silent failure for a
    louder one rather than removing it.

    So the three cases are:

        resolved is True,  strategy tui  -> "deliver"  (a tmux session was SEEN)
        resolved is True,  strategy sdk  -> "send"     (a session id was READ)
        anything else                    -> None       -> "send", today's verb

    Unknown keeps the status quo. That is the conservative direction here
    because ``send`` is what every existing caller already gets: an unproven
    route must not silently move anyone onto a different transport.
    """
    # stx-allow: fallback (reason: this only chooses which command STRING to
    # suggest; an unreadable route must degrade to today's verb rather than
    # break a dispatch that otherwise succeeded)
    try:
        from .._delivery import resolve_route

        route = resolve_route(agent)
        # `resolved is True` ONLY — see the docstring. `False` (a capable
        # enumeration that did not find the session) and `None` (a blind one)
        # both mean the route is unproven, and unproven keeps today's verb.
        return route.strategy if route.resolved is True else None
    except Exception:  # stx-allow: fallback (reason: catch-all — see above)
        return None


def build_track_command_argv(
    name: str, prompt: str, *, strategy: str | None = None
) -> list[str]:
    """The argv the caller should run to deliver ``prompt`` to ``name``."""
    return ["sac", "agents", track_verb_for(strategy), name, prompt]


def build_track_command(name: str, prompt: str, *, strategy: str | None = None) -> str:
    """Shell rendering of :func:`build_track_command_argv`.

    Derived from the argv rather than formatted separately, so the two cannot
    disagree about the verb.
    """
    return shlex.join(build_track_command_argv(name, prompt, strategy=strategy))


# EOF

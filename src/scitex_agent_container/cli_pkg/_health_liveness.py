"""The ternary liveness verdict, rendered for ``sac agents health``.

Extracted from :mod:`.status_cmds` (512-line per-file cap).

WHY THIS EXISTS NEXT TO ``healthy``, RATHER THAN REPLACING IT
-------------------------------------------------------------
``sac agents health`` answers with a BOOL (``healthy``), and a bool cannot say
"I could not tell". It reports one of two poles no matter how little was
actually observed — so a reader cannot distinguish *"we watched a message reach
this agent"* from *"every probe we ran failed and we guessed"*. Those are the
same output today, and acting on them as if they were the same is the bug.

So we publish the VERDICT and ITS EVIDENCE alongside the bool, and leave the
bool alone. Two reasons it is additive rather than a replacement:

1. ``healthy`` gates this command's EXIT CODE, and automation keys on that exit
   code. Re-deriving it from a new signal would change what every one of those
   callers does, silently, in one step.
2. It is the same restraint :mod:`.._listen._reachability` already practises —
   the observation is published NEXT TO the declaration, never overwriting it —
   and that restraint is precisely what keeps a watchdog from wiring itself to a
   signal that would kill healthy agents.
"""

from __future__ import annotations

from typing import Any

__all__ = ["liveness_payload", "print_inbox", "print_liveness"]

# wedged = present but NOT working — magenta, distinct from unknown's yellow so
# a "known-stuck, needs a restart" reads apart from a "we could not tell". It is
# NOT red: red is DEAD (destroyable), and a wedged agent must never be destroyed.
_VERDICT_COLOUR = {
    "alive": "green",
    "dead": "red",
    "unknown": "yellow",
    "wedged": "magenta",
}


def liveness_payload(name: str, config: Any) -> dict:
    """Resolve ``name``'s ternary verdict + evidence as a JSON-ready dict.

    Tolerant by construction: any failure to gather degrades to an UNKNOWN
    verdict carrying the reason — never to a fabricated DEAD (whose remedy is
    destructive), and never to an exception that takes the health command down.
    """
    from .._lifecycle._runtime_select import _get_runtime
    from .._lifecycle._verdict import (
        INSTRUMENT_NO_OBSERVATION,
        SOURCE_RESOLVER,
        UNKNOWN,
        LivenessVerdict,
        Signal,
    )
    from .._lifecycle._verdict_resolve import resolve_verdict

    try:
        return resolve_verdict(name, config, _get_runtime(config)).to_dict()
    except Exception as exc:  # stx-allow: fallback (reason: an un-gatherable verdict is UNKNOWN with its reason — never a fabricated DEAD, and never a crashed health command)
        return LivenessVerdict(
            agent=name,
            verdict=UNKNOWN,
            signals=(
                Signal(
                    SOURCE_RESOLVER,
                    UNKNOWN,
                    f"could not gather liveness evidence ({type(exc).__name__}: {exc})",
                    INSTRUMENT_NO_OBSERVATION,
                ),
            ),
        ).to_dict()


def print_liveness(console: Any, liveness: dict) -> None:
    """Print the verdict AND why, plus what it does (not) authorise."""
    verdict = str(liveness.get("verdict", "unknown"))
    colour = _VERDICT_COLOUR.get(verdict, "yellow")
    summary = liveness.get("summary", "?")
    console.print(f"[{colour}]liveness: {summary}[/{colour}]")
    veto = liveness.get("destroy_veto_reason")
    if veto:
        console.print(
            f"[dim]  destructive action NOT authorised on this evidence: {veto}[/dim]"
        )


def print_inbox(
    console: Any,
    name: str,
    subscribers: int,
    reachable: str,
    fault: str | None = None,
) -> None:
    """Render the inbox-reachability observation, and WHICH zero it is.

    The unreachable branch used to end with "The process is up; its inbox
    adapter is not attached" — a claim about the process that this command had
    not observed and could not make. For a STOPPED agent it was simply false,
    and it came bundled with advice ("messages are … replayed when its channel
    adapter reconnects") that only holds for a live one. Measured 2026-08-12:
    9 of the 15 registered agents on this host were stopped, so that sentence
    was wrong more often than it was right.

    ``fault`` is the listen daemon's observation of which case this is (see
    ``_listen._inbox_fault``). When it is absent — an older daemon, or a
    reading nobody could take — the text says what was actually seen and stops
    there, rather than re-asserting the old guess.
    """
    from .._listen._inbox_fault import FAULT_DEAF_INBOX, FAULT_NOT_RUNNING
    from .._listen._reachability import UNKNOWN, UNREACHABLE

    if reachable == UNREACHABLE and fault == FAULT_NOT_RUNNING:
        console.print(
            f"[red]inbox: NOT REACHABLE — and '{name}' IS NOT RUNNING. No live "
            f"session was observed for it, so its registry row has outlived "
            f"its process. Messages are queued durably, but NOTHING WILL "
            f"DRAIN THAT QUEUE until the agent is started — there is no "
            f"adapter left to reconnect. Do not wait for a reply.[/red]"
        )
    elif reachable == UNREACHABLE and fault == FAULT_DEAF_INBOX:
        console.print(
            f"[yellow]inbox: NOT REACHABLE — RUNNING BUT DEAF. A live session "
            f"was observed for '{name}' AND it has 0 subscribers, so a2a_send "
            f"reaches nobody while the agent is up. Messages are queued and "
            f"replayed when its channel adapter reconnects. Do NOT "
            f"force-restart: the session is healthy.[/yellow]"
        )
    elif reachable == UNREACHABLE:
        console.print(
            f"[yellow]inbox: NOT REACHABLE — 0 live subscribers, cause "
            f"UNCONFIRMED. a2a_send to '{name}' will reach nobody. This is "
            f"EITHER a detached inbox adapter on a live agent (messages "
            f"replay on reconnect) OR an agent that is not running at all "
            f"(nothing will reconnect) — this reading cannot tell them apart. "
            f"Do NOT force-restart on it.[/yellow]"
        )
    elif reachable == UNKNOWN:
        console.print(
            "[dim]inbox: unknown (could not reach sac listen to observe "
            "subscribers)[/dim]"
        )
    else:
        console.print(f"[green]inbox: reachable ({subscribers} subscriber(s))[/green]")
